`timescale 1ns/1ps

module frame_store_axis_spi #(
    parameter int FRAME_SAMPLES = 8192,
    parameter int PERIOD_CYCLES = 3_250_000
) (
    input  logic               clk,
    input  logic               rst_n,
    input  logic               capture_enable,
    input  logic               clear_stats,
    input  logic               s_valid,
    input  logic signed [15:0] s_sample,
    input  logic               s_otr,
    input  logic               filter_error,

    output logic        [15:0] m_axis_tdata,
    output logic         [1:0] m_axis_tkeep,
    output logic               m_axis_tvalid,
    input  logic               m_axis_tready,
    output logic               m_axis_tlast,

    input  logic               spi_cs_n,
    input  logic               spi_sclk,
    input  logic               spi_mosi,
    output logic               spi_miso,

    output logic        [31:0] frame_id,
    output logic        [31:0] status_word
);

    localparam int ADDR_W   = (FRAME_SAMPLES <= 2) ? 1 : $clog2(FRAME_SAMPLES);
    localparam int PERIOD_W = (PERIOD_CYCLES <= 2) ? 1 : $clog2(PERIOD_CYCLES);

    typedef enum logic [1:0] {
        AXIS_IDLE,
        AXIS_FETCH,
        AXIS_VALID
    } axis_state_t;

    typedef enum logic [2:0] {
        SPI_COMMAND,
        SPI_INFO,
        SPI_ADDR_HI,
        SPI_ADDR_LO,
        SPI_DUMMY,
        SPI_STREAM
    } spi_state_t;

    logic [PERIOD_W-1:0] period_counter;
    logic capture_request;
    logic capture_active;
    logic capture_bank;
    logic next_capture_bank;
    logic [ADDR_W-1:0] capture_addr;
    logic capture_otr;

    logic stream_bank;
    logic frame_pending;
    axis_state_t axis_state;
    logic [ADDR_W-1:0] axis_addr;

    logic last_frame_otr;
    logic overflow_sticky;
    logic [11:0] frames_dropped;

    logic [31:0] frame_id_next;
    logic capture_start;
    logic capture_write_en;
    logic capture_write_bank;
    logic [ADDR_W-1:0] capture_write_addr;
    logic formal_read_en;
    logic [15:0] formal_bank0_data;
    logic [15:0] formal_bank1_data;
    logic [15:0] diag_bank0_data;
    logic [15:0] diag_bank1_data;

    // SPI-domain synchronizers and protocol state.
    (* async_reg = "true" *) logic spi_bank_meta;
    (* async_reg = "true" *) logic spi_bank_sync;
    logic [31:0] frame_id_gray;
    (* async_reg = "true" *) logic [31:0] spi_frame_gray_meta;
    (* async_reg = "true" *) logic [31:0] spi_frame_gray_sync;
    logic [31:0] spi_frame_id_sync;
    (* async_reg = "true" *) logic [7:0] spi_status_meta;
    (* async_reg = "true" *) logic [7:0] spi_status_sync;

    spi_state_t spi_state;
    logic [2:0] spi_bit_count;
    logic [7:0] spi_rx_shift;
    logic [7:0] spi_tx_shift;
    logic [3:0] spi_info_index;
    logic [7:0] spi_addr_high;
    logic [ADDR_W-1:0] spi_addr;
    logic spi_read_bank;
    logic [31:0] spi_generation;
    logic [15:0] spi_ram_data;
    logic spi_stream_high;
    logic [7:0] spi_received_byte;

    function automatic logic [31:0] gray_to_binary(input logic [31:0] gray);
        integer gray_index;
        begin
            gray_to_binary[31] = gray[31];
            for (gray_index = 30; gray_index >= 0; gray_index = gray_index - 1)
                gray_to_binary[gray_index] = gray_to_binary[gray_index+1] ^ gray[gray_index];
        end
    endfunction

    assign m_axis_tkeep  = 2'b11;
    assign m_axis_tvalid = (axis_state == AXIS_VALID);
    assign m_axis_tlast  = (axis_state == AXIS_VALID) && (axis_addr == FRAME_SAMPLES-1);
    assign m_axis_tdata  = stream_bank ? formal_bank1_data : formal_bank0_data;

    assign capture_start      = s_valid && !capture_active && capture_request;
    assign capture_write_en   = s_valid && (capture_active || capture_request);
    assign capture_write_bank = capture_active ? capture_bank
                                               : (frame_pending ? ~stream_bank : next_capture_bank);
    assign capture_write_addr = capture_active ? capture_addr : '0;
    assign formal_read_en     = (axis_state == AXIS_FETCH);

    assign frame_id_next = (frame_id == 32'hffff_ffff) ? 32'd1 : frame_id + 1'b1;
    assign spi_frame_id_sync = gray_to_binary(spi_frame_gray_sync);
    assign status_word = {
        16'b0,
        frames_dropped,
        overflow_sticky,
        last_frame_otr,
        capture_active,
        frame_pending
    };

    always_comb begin
        spi_received_byte = {spi_rx_shift[6:0], spi_mosi};
        spi_ram_data = spi_read_bank ? diag_bank1_data : diag_bank0_data;
    end

    frame_ram_sdp #(.DEPTH(FRAME_SAMPLES), .ADDR_WIDTH(ADDR_W)) formal_bank0 (
        .clk,
        .wr_en(capture_write_en && !capture_write_bank),
        .wr_addr(capture_write_addr),
        .wr_data(s_sample),
        .rd_en(formal_read_en && !stream_bank),
        .rd_addr(axis_addr),
        .rd_data(formal_bank0_data)
    );

    frame_ram_sdp #(.DEPTH(FRAME_SAMPLES), .ADDR_WIDTH(ADDR_W)) formal_bank1 (
        .clk,
        .wr_en(capture_write_en && capture_write_bank),
        .wr_addr(capture_write_addr),
        .wr_data(s_sample),
        .rd_en(formal_read_en && stream_bank),
        .rd_addr(axis_addr),
        .rd_data(formal_bank1_data)
    );

    frame_ram_async_sdp #(.DEPTH(FRAME_SAMPLES), .ADDR_WIDTH(ADDR_W)) diag_bank0 (
        .wr_clk(clk),
        .wr_en(capture_write_en && !capture_write_bank),
        .wr_addr(capture_write_addr),
        .wr_data(s_sample),
        .rd_clk(spi_sclk),
        .rd_en(!spi_cs_n),
        .rd_addr(spi_addr),
        .rd_data(diag_bank0_data)
    );

    frame_ram_async_sdp #(.DEPTH(FRAME_SAMPLES), .ADDR_WIDTH(ADDR_W)) diag_bank1 (
        .wr_clk(clk),
        .wr_en(capture_write_en && capture_write_bank),
        .wr_addr(capture_write_addr),
        .wr_data(s_sample),
        .rd_clk(spi_sclk),
        .rd_en(!spi_cs_n),
        .rd_addr(spi_addr),
        .rd_data(diag_bank1_data)
    );

    // Register the Gray encoder in the source domain. This prevents decode
    // glitches before the SPI synchronizer and guarantees one-bit transitions.
    always_ff @(posedge clk) begin
        if (!rst_n)
            frame_id_gray <= '0;
        else
            frame_id_gray <= (frame_id >> 1) ^ frame_id;
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            period_counter    <= '0;
            capture_request   <= 1'b0;
            capture_active    <= 1'b0;
            capture_bank      <= 1'b0;
            next_capture_bank <= 1'b0;
            capture_addr      <= '0;
            capture_otr       <= 1'b0;
            stream_bank       <= 1'b0;
            frame_pending     <= 1'b0;
            axis_state        <= AXIS_IDLE;
            axis_addr         <= '0;
            frame_id          <= 32'd0;
            last_frame_otr    <= 1'b0;
            overflow_sticky   <= 1'b0;
            frames_dropped    <= '0;
        end else begin
            if (clear_stats) begin
                overflow_sticky <= 1'b0;
                frames_dropped  <= '0;
            end
            if (filter_error)
                overflow_sticky <= 1'b1;

            if (capture_enable) begin
                if (period_counter == PERIOD_CYCLES-1) begin
                    period_counter  <= '0;
                    capture_request <= 1'b1;
                end else begin
                    period_counter <= period_counter + 1'b1;
                end
            end else begin
                period_counter  <= '0;
                capture_request <= 1'b0;
            end

            case (axis_state)
                AXIS_IDLE: begin
                    axis_addr <= '0;
                end
                AXIS_FETCH: begin
                    axis_state <= AXIS_VALID;
                end
                AXIS_VALID: begin
                    if (m_axis_tready) begin
                        if (axis_addr == FRAME_SAMPLES-1) begin
                            axis_state    <= AXIS_IDLE;
                            frame_pending <= 1'b0;
                            axis_addr     <= '0;
                        end else begin
                            axis_addr  <= axis_addr + 1'b1;
                            axis_state <= AXIS_FETCH;
                        end
                    end
                end
                default: axis_state <= AXIS_IDLE;
            endcase

            if (s_valid) begin
                if (capture_start) begin
                    capture_request <= 1'b0;
                    capture_active  <= (FRAME_SAMPLES > 1);
                    capture_bank    <= capture_write_bank;
                    capture_addr    <= (FRAME_SAMPLES > 1) ? 1 : 0;
                    capture_otr     <= s_otr;

                    if (FRAME_SAMPLES == 1) begin
                        if (frame_pending) begin
                            overflow_sticky <= 1'b1;
                            if (frames_dropped != 12'hfff)
                                frames_dropped <= frames_dropped + 1'b1;
                        end else begin
                            stream_bank       <= capture_write_bank;
                            next_capture_bank <= ~capture_write_bank;
                            frame_id          <= frame_id_next;
                            last_frame_otr    <= s_otr;
                            frame_pending     <= 1'b1;
                            axis_state        <= AXIS_FETCH;
                            axis_addr         <= '0;
                        end
                    end
                end else if (capture_active) begin
                    capture_otr <= capture_otr | s_otr;

                    if (capture_addr == FRAME_SAMPLES-1) begin
                        capture_active <= 1'b0;
                        capture_addr   <= '0;
                        if (frame_pending) begin
                            overflow_sticky <= 1'b1;
                            if (frames_dropped != 12'hfff)
                                frames_dropped <= frames_dropped + 1'b1;
                        end else begin
                            stream_bank       <= capture_bank;
                            next_capture_bank <= ~capture_bank;
                            frame_id          <= frame_id_next;
                            last_frame_otr    <= capture_otr | s_otr;
                            frame_pending     <= 1'b1;
                            axis_state        <= AXIS_FETCH;
                            axis_addr         <= '0;
                        end
                    end else begin
                        capture_addr <= capture_addr + 1'b1;
                    end
                end
            end
        end
    end

    // The SPI port is a true second read port on each inferred frame BRAM.
    // Metadata is synchronized into the externally clocked SPI domain.
    always_ff @(posedge spi_sclk or posedge spi_cs_n) begin
        if (spi_cs_n) begin
            spi_bank_meta     <= 1'b0;
            spi_bank_sync     <= 1'b0;
            spi_frame_gray_meta <= '0;
            spi_frame_gray_sync <= '0;
            spi_status_meta   <= '0;
            spi_status_sync   <= '0;
            spi_state         <= SPI_COMMAND;
            spi_bit_count     <= '0;
            spi_rx_shift      <= '0;
            spi_tx_shift      <= '0;
            spi_info_index    <= '0;
            spi_addr_high     <= '0;
            spi_addr          <= '0;
            spi_read_bank     <= 1'b0;
            spi_generation    <= '0;
            spi_stream_high   <= 1'b0;
        end else begin
            spi_bank_meta     <= stream_bank;
            spi_bank_sync     <= spi_bank_meta;
            spi_frame_gray_meta <= frame_id_gray;
            spi_frame_gray_sync <= spi_frame_gray_meta;
            spi_status_meta   <= status_word[7:0];
            spi_status_sync   <= spi_status_meta;

            spi_rx_shift <= {spi_rx_shift[6:0], spi_mosi};
            if (spi_bit_count == 3'd7) begin
                spi_bit_count <= '0;
                case (spi_state)
                    SPI_COMMAND: begin
                        case (spi_received_byte)
                            8'ha0: begin
                                spi_state      <= SPI_INFO;
                                spi_info_index <= 4'd0;
                                spi_tx_shift   <= 8'h43;
                            end
                            8'ha1: begin
                                spi_state      <= SPI_ADDR_HI;
                                spi_tx_shift   <= 8'h00;
                            end
                            default: begin
                                spi_state    <= SPI_COMMAND;
                                spi_tx_shift <= 8'h00;
                            end
                        endcase
                    end
                    SPI_INFO: begin
                        spi_info_index <= spi_info_index + 1'b1;
                        case (spi_info_index)
                            4'd0: spi_tx_shift <= 8'h53;
                            4'd1: spi_tx_shift <= 8'h01;
                            4'd2: spi_tx_shift <= spi_status_sync;
                            4'd3: spi_tx_shift <= spi_frame_id_sync[31:24];
                            4'd4: spi_tx_shift <= spi_frame_id_sync[23:16];
                            4'd5: spi_tx_shift <= spi_frame_id_sync[15:8];
                            4'd6: spi_tx_shift <= spi_frame_id_sync[7:0];
                            4'd7: spi_tx_shift <= (FRAME_SAMPLES >> 8) & 8'hff;
                            4'd8: spi_tx_shift <= FRAME_SAMPLES & 8'hff;
                            default: spi_tx_shift <= 8'h00;
                        endcase
                    end
                    SPI_ADDR_HI: begin
                        spi_addr_high <= spi_received_byte;
                        spi_state     <= SPI_ADDR_LO;
                        spi_tx_shift  <= 8'h00;
                    end
                    SPI_ADDR_LO: begin
                        spi_addr        <= {spi_addr_high, spi_received_byte[7:0]};
                        spi_read_bank   <= spi_bank_sync;
                        spi_generation  <= spi_frame_id_sync;
                        spi_stream_high <= 1'b0;
                        spi_state       <= SPI_DUMMY;
                        spi_tx_shift    <= 8'h00;
                    end
                    SPI_DUMMY: begin
                        spi_state       <= SPI_STREAM;
                        spi_stream_high <= 1'b0;
                        spi_tx_shift    <= spi_ram_data[7:0];
                    end
                    SPI_STREAM: begin
                        if (!spi_stream_high) begin
                            spi_tx_shift    <= spi_ram_data[15:8];
                            spi_stream_high <= 1'b1;
                            if (spi_addr == FRAME_SAMPLES-1)
                                spi_addr <= '0;
                            else
                                spi_addr <= spi_addr + 1'b1;
                        end else begin
                            spi_tx_shift    <= (spi_frame_id_sync == spi_generation)
                                             ? spi_ram_data[7:0] : 8'h00;
                            spi_stream_high <= 1'b0;
                        end
                    end
                    default: spi_state <= SPI_COMMAND;
                endcase
            end else begin
                spi_bit_count <= spi_bit_count + 1'b1;
            end
        end
    end

    always_ff @(negedge spi_sclk or posedge spi_cs_n) begin
        if (spi_cs_n)
            spi_miso <= 1'b0;
        else
            spi_miso <= spi_tx_shift[7-spi_bit_count];
    end

endmodule
