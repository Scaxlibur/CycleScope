`timescale 1ns/1ps

module tb_fault_injection;

    localparam int FRAME_SAMPLES = 4;
    localparam int PERIOD_CYCLES = 16;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic capture_enable = 1'b0;
    logic clear_stats = 1'b0;
    logic s_valid = 1'b1;
    logic signed [15:0] s_sample = '0;
    logic s_otr = 1'b0;
    logic [63:0] s_timestamp_tick = '0;
    logic filter_error = 1'b0;
    logic inject_otr = 1'b0;
    logic inject_overflow = 1'b0;
    logic inject_frame_drop = 1'b0;
    logic [15:0] m_axis_tdata;
    logic [1:0] m_axis_tkeep;
    logic m_axis_tvalid;
    logic m_axis_tready = 1'b1;
    logic m_axis_tlast;
    logic spi_cs_n = 1'b1;
    logic spi_sclk = 1'b0;
    logic spi_mosi = 1'b0;
    logic spi_miso;
    logic [31:0] frame_id;
    logic [31:0] status_word;
    logic [63:0] frame_timestamp_tick;

    logic [31:0] saved_frame_id;
    logic [63:0] timestamp_before_drop;

    always #7.692 clk = ~clk;

    frame_store_axis_spi #(
        .FRAME_SAMPLES(FRAME_SAMPLES),
        .PERIOD_CYCLES(PERIOD_CYCLES)
    ) dut (.*);

    always @(negedge clk) begin
        if (!rst_n) begin
            s_sample <= '0;
            s_timestamp_tick <= '0;
        end else begin
            s_sample <= s_sample + 1'b1;
            s_timestamp_tick <= s_timestamp_tick + 1'b1;
        end
    end

    task automatic pulse_otr;
        begin
            @(negedge clk);
            inject_otr = 1'b1;
            @(negedge clk);
            inject_otr = 1'b0;
        end
    endtask

    task automatic pulse_overflow;
        begin
            @(negedge clk);
            inject_overflow = 1'b1;
            @(negedge clk);
            inject_overflow = 1'b0;
        end
    endtask

    task automatic pulse_drop;
        begin
            @(negedge clk);
            inject_frame_drop = 1'b1;
            @(negedge clk);
            inject_frame_drop = 1'b0;
        end
    endtask

    initial begin
        repeat (6) @(posedge clk);
        rst_n = 1'b1;
        capture_enable = 1'b1;

        wait (frame_id == 1);
        @(negedge clk);
        if (status_word[3:2] != 2'b00 || status_word[15:4] != 0)
            $fatal(1, "unexpected initial status=%h", status_word);

        pulse_overflow();
        @(posedge clk);
        #1;
        if (!status_word[3])
            $fatal(1, "injected overflow did not set the sticky flag");
        @(negedge clk);
        clear_stats = 1'b1;
        @(negedge clk);
        clear_stats = 1'b0;
        @(posedge clk);
        #1;
        if (status_word[3] || status_word[15:4] != 0)
            $fatal(1, "clear_stats did not clear injected overflow/drop counters");
        $display("OVERFLOW_INJECTION_PASS");

        saved_frame_id = frame_id;
        pulse_otr();
        wait (frame_id != saved_frame_id);
        @(negedge clk);
        if (!status_word[2])
            $fatal(1, "injected OTR did not mark the next accepted frame");
        saved_frame_id = frame_id;
        wait (frame_id != saved_frame_id);
        @(negedge clk);
        if (status_word[2])
            $fatal(1, "injected OTR leaked into more than one frame");
        $display("OTR_INJECTION_PASS");

        // Arm the event well before the next request. Exactly one delivery
        // opportunity must be skipped and the following frame must retain its
        // true first-sample time rather than the release/backpressure time.
        wait (dut.period_counter == 12 && !dut.capture_active &&
              !dut.frame_pending);
        saved_frame_id = frame_id;
        timestamp_before_drop = frame_timestamp_tick;
        pulse_drop();
        wait (status_word[15:4] == 1);
        if (frame_id != saved_frame_id)
            $fatal(1, "frame_id advanced for an injected dropped opportunity");
        wait (frame_id != saved_frame_id);
        @(negedge clk);
        if ((frame_timestamp_tick - timestamp_before_drop) != 2 * PERIOD_CYCLES)
            $fatal(1, "post-drop timestamp gap=%0d expected=%0d",
                   frame_timestamp_tick - timestamp_before_drop,
                   2 * PERIOD_CYCLES);
        $display("FRAME_DROP_INJECTION_PASS timestamp_gap=%0d",
                 frame_timestamp_tick - timestamp_before_drop);

        $display("TEST_PASS tb_fault_injection");
        $finish;
    end

    initial begin
        #20us;
        $fatal(1, "testbench timeout");
    end

endmodule
