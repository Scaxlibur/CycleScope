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
    input  logic        [63:0] s_timestamp_tick,
    input  logic               filter_error,
    input  logic               inject_otr,
    input  logic               inject_overflow,
    input  logic               inject_frame_drop,

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
    output logic        [31:0] status_word,
    output logic        [63:0] frame_timestamp_tick
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
    logic capture_drop;
    logic capture_bank;
    logic next_capture_bank;
    logic [ADDR_W-1:0] capture_addr;
    logic capture_otr;
    logic [63:0] capture_timestamp_tick;
    logic inject_otr_pending;
    logic inject_frame_drop_pending;

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

    // T10 is not a clock-capable input. Treat the three SPI inputs as
    // asynchronous data and detect their synchronized edges in the 65 MHz
    // sample domain. The 5 MHz interface limit leaves at least six sample
    // clocks per half-cycle for synchronization and MISO setup.
    (* async_reg = "true" *) logic spi_cs_n_meta;
    (* async_reg = "true" *) logic spi_cs_n_sync;
    (* async_reg = "true" *) logic spi_sclk_meta;
    (* async_reg = "true" *) logic spi_sclk_sync;
    (* async_reg = "true" *) logic spi_mosi_meta;
    (* async_reg = "true" *) logic spi_mosi_sync;
    logic spi_cs_n_sync_d;
    logic spi_sclk_sync_d;
    logic spi_cs_start;
    logic spi_sclk_rise;
    logic spi_sclk_fall;

    spi_state_t spi_state;
    logic [2:0] spi_bit_count;
    logic [7:0] spi_rx_shift;
    logic [7:0] spi_tx_shift;
    logic [3:0] spi_info_index;
    logic [7:0] spi_addr_high;
    logic [ADDR_W-1:0] spi_addr;
    logic spi_read_bank;
    logic [31:0] spi_generation;
    logic [31:0] spi_frame_id_snapshot;
    logic [7:0] spi_status_snapshot;
    logic [15:0] spi_ram_data;
    logic spi_stream_high;
    logic [7:0] spi_received_byte;

    assign m_axis_tkeep  = 2'b11;
    assign m_axis_tvalid = (axis_state == AXIS_VALID);
    assign m_axis_tlast  = (axis_state == AXIS_VALID) && (axis_addr == FRAME_SAMPLES-1);
    assign m_axis_tdata  = stream_bank ? formal_bank1_data : formal_bank0_data;

    assign capture_start      = s_valid && !capture_active && capture_request &&
                                !inject_frame_drop_pending;
    assign capture_drop       = s_valid && !capture_active && capture_request &&
                                inject_frame_drop_pending;
    assign capture_write_en   = s_valid && (capture_active ||
                                (capture_request && !inject_frame_drop_pending));
    assign capture_write_bank = capture_active ? capture_bank
                                               : (frame_pending ? ~stream_bank : next_capture_bank);
    assign capture_write_addr = capture_active ? capture_addr : '0;
    assign formal_read_en     = (axis_state == AXIS_FETCH);

    assign frame_id_next = (frame_id == 32'hffff_ffff) ? 32'd1 : frame_id + 1'b1;
    assign spi_cs_start  = spi_cs_n_sync_d && !spi_cs_n_sync;
    assign spi_sclk_rise = spi_sclk_sync && !spi_sclk_sync_d;
    assign spi_sclk_fall = !spi_sclk_sync && spi_sclk_sync_d;
    assign status_word = {
        16'b0,
        frames_dropped,
        overflow_sticky,
        last_frame_otr,
        capture_active,
        frame_pending
    };

    always_comb begin
        spi_received_byte = {spi_rx_shift[6:0], spi_mosi_sync};
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

    frame_ram_sdp #(.DEPTH(FRAME_SAMPLES), .ADDR_WIDTH(ADDR_W)) diag_bank0 (
        .clk,
        .wr_en(capture_write_en && !capture_write_bank),
        .wr_addr(capture_write_addr),
        .wr_data(s_sample),
        .rd_en(!spi_cs_n_sync),
        .rd_addr(spi_addr),
        .rd_data(diag_bank0_data)
    );

    frame_ram_sdp #(.DEPTH(FRAME_SAMPLES), .ADDR_WIDTH(ADDR_W)) diag_bank1 (
        .clk,
        .wr_en(capture_write_en && capture_write_bank),
        .wr_addr(capture_write_addr),
        .wr_data(s_sample),
        .rd_en(!spi_cs_n_sync),
        .rd_addr(spi_addr),
        .rd_data(diag_bank1_data)
    );

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            period_counter    <= '0;
            capture_request   <= 1'b0;
            capture_active    <= 1'b0;
            capture_bank      <= 1'b0;
            next_capture_bank <= 1'b0;
            capture_addr      <= '0;
            capture_otr       <= 1'b0;
            capture_timestamp_tick <= '0;
            inject_otr_pending <= 1'b0;
            inject_frame_drop_pending <= 1'b0;
            stream_bank       <= 1'b0;
            frame_pending     <= 1'b0;
            axis_state        <= AXIS_IDLE;
            axis_addr         <= '0;
            frame_id          <= 32'd0;
            frame_timestamp_tick <= '0;
            last_frame_otr    <= 1'b0;
            overflow_sticky   <= 1'b0;
            frames_dropped    <= '0;
        end else begin
            if (clear_stats) begin
                overflow_sticky <= 1'b0;
                frames_dropped  <= '0;
            end
            if (filter_error || inject_overflow)
                overflow_sticky <= 1'b1;
            if (inject_otr)
                inject_otr_pending <= 1'b1;
            if (inject_frame_drop)
                inject_frame_drop_pending <= 1'b1;

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
                if (capture_drop) begin
                    capture_request <= 1'b0;
                    inject_frame_drop_pending <= 1'b0;
                    if (frames_dropped != 12'hfff)
                        frames_dropped <= frames_dropped + 1'b1;
                end else if (capture_start) begin
                    capture_request <= 1'b0;
                    capture_active  <= (FRAME_SAMPLES > 1);
                    capture_bank    <= capture_write_bank;
                    capture_addr    <= (FRAME_SAMPLES > 1) ? 1 : 0;
                    capture_otr     <= s_otr;
                    capture_timestamp_tick <= s_timestamp_tick;

                    if (FRAME_SAMPLES == 1) begin
                        if (frame_pending) begin
                            overflow_sticky <= 1'b1;
                            if (frames_dropped != 12'hfff)
                                frames_dropped <= frames_dropped + 1'b1;
                        end else begin
                            stream_bank       <= capture_write_bank;
                            next_capture_bank <= ~capture_write_bank;
                            frame_id          <= frame_id_next;
                            frame_timestamp_tick <= s_timestamp_tick;
                            last_frame_otr    <= s_otr | inject_otr_pending |
                                                 inject_otr;
                            inject_otr_pending <= 1'b0;
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
                            frame_timestamp_tick <= capture_timestamp_tick;
                            last_frame_otr    <= capture_otr | s_otr |
                                                 inject_otr_pending | inject_otr;
                            inject_otr_pending <= 1'b0;
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

    // Synchronize and oversample the external mode-0 SPI pins. All diagnostic
    // BRAM reads and protocol state remain in clk; spi_sclk never drives a
    // clock buffer or sequential clock pin inside the FPGA.
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            spi_cs_n_meta         <= 1'b1;
            spi_cs_n_sync         <= 1'b1;
            spi_cs_n_sync_d       <= 1'b1;
            spi_sclk_meta         <= 1'b0;
            spi_sclk_sync         <= 1'b0;
            spi_sclk_sync_d       <= 1'b0;
            spi_mosi_meta         <= 1'b0;
            spi_mosi_sync         <= 1'b0;
            spi_state             <= SPI_COMMAND;
            spi_bit_count         <= '0;
            spi_rx_shift          <= '0;
            spi_tx_shift          <= '0;
            spi_info_index        <= '0;
            spi_addr_high         <= '0;
            spi_addr              <= '0;
            spi_read_bank         <= 1'b0;
            spi_generation        <= '0;
            spi_frame_id_snapshot <= '0;
            spi_status_snapshot   <= '0;
            spi_stream_high       <= 1'b0;
            spi_miso              <= 1'b0;
        end else begin
            spi_cs_n_meta   <= spi_cs_n;
            spi_cs_n_sync   <= spi_cs_n_meta;
            spi_cs_n_sync_d <= spi_cs_n_sync;
            spi_sclk_meta   <= spi_sclk;
            spi_sclk_sync   <= spi_sclk_meta;
            spi_sclk_sync_d <= spi_sclk_sync;
            spi_mosi_meta   <= spi_mosi;
            spi_mosi_sync   <= spi_mosi_meta;

            if (spi_cs_n_sync) begin
                spi_state       <= SPI_COMMAND;
                spi_bit_count   <= '0;
                spi_rx_shift    <= '0;
                spi_tx_shift    <= '0;
                spi_info_index  <= '0;
                spi_addr_high   <= '0;
                spi_addr        <= '0;
                spi_stream_high <= 1'b0;
                spi_miso        <= 1'b0;
            end else if (spi_cs_start) begin
                // Coherently freeze the diagnostic identity for this complete
                // CS_N transaction. A later generation change invalidates data
                // but can never backpressure or overwrite the formal AXIS path.
                spi_state             <= SPI_COMMAND;
                spi_bit_count         <= '0;
                spi_rx_shift          <= '0;
                spi_tx_shift          <= '0;
                spi_info_index        <= '0;
                spi_addr_high         <= '0;
                spi_addr              <= '0;
                spi_read_bank         <= stream_bank;
                spi_generation        <= frame_id;
                spi_frame_id_snapshot <= frame_id;
                spi_status_snapshot   <= status_word[7:0];
                spi_stream_high       <= 1'b0;
                spi_miso              <= 1'b0;
            end else if (spi_state == SPI_STREAM &&
                         frame_id != spi_generation) begin
                // A generation switch invalidates the complete transaction.
                // Clear the already-prefetched MSB even while SCLK is paused
                // low, so the next mode-0 sampling edge cannot expose one bit
                // from the previous frozen frame.
                spi_tx_shift <= '0;
                spi_miso     <= 1'b0;
            end else begin
                if (spi_sclk_rise) begin
                    spi_rx_shift <= {spi_rx_shift[6:0], spi_mosi_sync};
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
                                        spi_state    <= SPI_ADDR_HI;
                                        spi_tx_shift <= 8'h00;
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
                                    4'd2: spi_tx_shift <= spi_status_snapshot;
                                    4'd3: spi_tx_shift <= spi_frame_id_snapshot[31:24];
                                    4'd4: spi_tx_shift <= spi_frame_id_snapshot[23:16];
                                    4'd5: spi_tx_shift <= spi_frame_id_snapshot[15:8];
                                    4'd6: spi_tx_shift <= spi_frame_id_snapshot[7:0];
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
                                spi_stream_high <= 1'b0;
                                spi_state       <= SPI_DUMMY;
                                spi_tx_shift    <= 8'h00;
                            end
                            SPI_DUMMY: begin
                                spi_state       <= SPI_STREAM;
                                spi_stream_high <= 1'b0;
                                spi_tx_shift    <= (frame_id == spi_generation)
                                                 ? spi_ram_data[7:0] : 8'h00;
                            end
                            SPI_STREAM: begin
                                if (!spi_stream_high) begin
                                    spi_tx_shift <= (frame_id == spi_generation)
                                                    ? spi_ram_data[15:8] : 8'h00;
                                    spi_stream_high <= 1'b1;
                                    if (spi_addr == FRAME_SAMPLES-1)
                                        spi_addr <= '0;
                                    else
                                        spi_addr <= spi_addr + 1'b1;
                                end else begin
                                    spi_tx_shift <= (frame_id == spi_generation)
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

                if (spi_sclk_fall)
                    spi_miso <= spi_tx_shift[7-spi_bit_count];
            end
        end
    end

endmodule
