`timescale 1ns/1ps

module tb_frame_store_axis_spi;

    localparam int FRAME_SAMPLES = 32;
    localparam int PERIOD_CYCLES = 2000;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic capture_enable = 1'b0;
    logic clear_stats = 1'b0;
    logic s_valid = 1'b1;
    logic signed [15:0] s_sample = 16'sd0;
    logic s_otr = 1'b0;
    logic filter_error = 1'b0;

    logic [15:0] m_axis_tdata;
    logic [1:0] m_axis_tkeep;
    logic m_axis_tvalid;
    logic m_axis_tready = 1'b0;
    logic m_axis_tlast;

    logic spi_cs_n = 1'b1;
    logic spi_sclk = 1'b0;
    logic spi_mosi = 1'b0;
    logic spi_miso;

    logic [31:0] frame_id;
    logic [31:0] status_word;

    integer cycle_count;
    integer frame_index;
    integer sample_index;
    logic [15:0] previous_sample;
    logic [15:0] first_frame_base;
    logic stalled;
    logic [15:0] stalled_data;
    logic stalled_last;

    byte info [0:9];
    byte low_byte;
    byte high_byte;
    logic [15:0] spi_sample;
    integer index;

    always #5 clk = ~clk;

    frame_store_axis_spi #(
        .FRAME_SAMPLES(FRAME_SAMPLES),
        .PERIOD_CYCLES(PERIOD_CYCLES)
    ) dut (.*);

    always @(negedge clk) begin
        if (!rst_n) begin
            s_sample <= 16'sd0;
            s_otr <= 1'b0;
            m_axis_tready <= 1'b0;
        end else begin
            s_sample <= s_sample + 1'b1;
            s_otr <= (s_sample[3:0] == 4'h7);
            m_axis_tready <= ((cycle_count % 5) != 0) && ((cycle_count % 11) != 0);
        end
    end

    always @(posedge clk) begin
        cycle_count <= cycle_count + 1;

        if (stalled) begin
            if (!m_axis_tvalid || m_axis_tdata !== stalled_data || m_axis_tlast !== stalled_last)
                $fatal(1, "AXI payload changed while backpressured");
        end
        stalled <= m_axis_tvalid && !m_axis_tready;
        if (m_axis_tvalid && !m_axis_tready) begin
            stalled_data <= m_axis_tdata;
            stalled_last <= m_axis_tlast;
        end

        if (m_axis_tvalid && m_axis_tready) begin
            if (m_axis_tkeep != 2'b11)
                $fatal(1, "TKEEP must be 2'b11");
            if (sample_index == 0) begin
                if (frame_index == 0)
                    first_frame_base <= m_axis_tdata;
            end else if (m_axis_tdata !== previous_sample + 1'b1) begin
                $fatal(1, "frame sample discontinuity index=%0d got=%h prev=%h", sample_index, m_axis_tdata, previous_sample);
            end
            if (m_axis_tlast !== (sample_index == FRAME_SAMPLES-1))
                $fatal(1, "TLAST mismatch at sample %0d", sample_index);
            previous_sample <= m_axis_tdata;
            if (sample_index == FRAME_SAMPLES-1) begin
                sample_index <= 0;
                frame_index <= frame_index + 1;
            end else begin
                sample_index <= sample_index + 1;
            end
        end
    end

    task automatic spi_transfer_byte(input byte tx, output byte rx);
        integer bit_index;
        begin
            rx = 8'h00;
            for (bit_index = 7; bit_index >= 0; bit_index = bit_index - 1) begin
                spi_mosi = tx[bit_index];
                #10 spi_sclk = 1'b1;
                #1 rx[bit_index] = spi_miso;
                #9 spi_sclk = 1'b0;
            end
        end
    endtask

    task automatic spi_get_info;
        byte ignored;
        integer byte_index;
        begin
            spi_cs_n = 1'b0;
            #20;
            spi_transfer_byte(8'ha0, ignored);
            for (byte_index = 0; byte_index < 10; byte_index = byte_index + 1)
                spi_transfer_byte(8'h00, info[byte_index]);
            #10 spi_cs_n = 1'b1;
            #20;
        end
    endtask

    task automatic spi_read_start(input integer address);
        byte ignored;
        begin
            spi_cs_n = 1'b0;
            #20;
            spi_transfer_byte(8'ha1, ignored);
            spi_transfer_byte((address >> 8) & 8'hff, ignored);
            spi_transfer_byte(address & 8'hff, ignored);
            spi_transfer_byte(8'h00, ignored);
        end
    endtask

    task automatic spi_read_sample(output logic [15:0] sample_value);
        begin
            spi_transfer_byte(8'h00, low_byte);
            spi_transfer_byte(8'h00, high_byte);
            sample_value = {high_byte, low_byte};
        end
    endtask

    initial begin
        cycle_count = 0;
        frame_index = 0;
        sample_index = 0;
        previous_sample = '0;
        first_frame_base = '0;
        stalled = 1'b0;
        stalled_data = '0;
        stalled_last = 1'b0;

        repeat (8) @(posedge clk);
        rst_n = 1'b1;
        capture_enable = 1'b1;

        wait (frame_index >= 1);
        wait (!status_word[0]);
        if (!status_word[2])
            $fatal(1, "frame-level OTR sticky flag was not set");

        spi_get_info();
        if (info[0] != 8'h43 || info[1] != 8'h53 || info[2] != 8'h01)
            $fatal(1, "SPI GET_INFO magic/version mismatch: %h %h %h", info[0], info[1], info[2]);
        if ({info[4], info[5], info[6], info[7]} != 32'd1)
            $fatal(1, "SPI frame id mismatch");
        if ({info[8], info[9]} != FRAME_SAMPLES)
            $fatal(1, "SPI frame length mismatch");

        spi_read_start(0);
        for (index = 0; index < 4; index = index + 1) begin
            spi_read_sample(spi_sample);
            if (spi_sample !== first_frame_base + index)
                $fatal(1, "SPI sample %0d got=%h expected=%h", index, spi_sample, first_frame_base + index);
        end
        #10 spi_cs_n = 1'b1;

        wait (frame_index >= 2);
        if (status_word[3] || status_word[15:4] != 0)
            $fatal(1, "unexpected frame overflow/drop status=%h", status_word);

        $display("AXIS_FRAMES=%0d FIRST_BASE=%h", frame_index, first_frame_base);
        $display("TEST_PASS tb_frame_store_axis_spi");
        $finish;
    end

    initial begin
        #200_000;
        $fatal(1, "testbench timeout");
    end

endmodule
