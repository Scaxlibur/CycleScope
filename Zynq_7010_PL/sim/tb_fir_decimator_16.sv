`timescale 1ns/1ps

module tb_fir_decimator_16;

    localparam real FS_HZ = 65_000_000.0;
    localparam real PI = 3.14159265358979323846;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic s_valid = 1'b0;
    logic signed [15:0] s_sample = '0;
    logic s_otr = 1'b0;
    logic m_valid;
    logic signed [15:0] m_sample;
    logic m_otr;
    logic schedule_error;

    integer cycle_count;
    integer last_valid_cycle;
    integer output_count;
    integer measured_count;
    integer measured_min;
    integer measured_max;
    integer measured_abs_max;
    bit seen_otr;
    bit check_spacing;

    always #7.692 clk = ~clk;

    fir_decimator_16 dut (.*);

    always @(posedge clk) begin
        cycle_count <= cycle_count + 1;
        if (m_valid) begin
            if (check_spacing && last_valid_cycle >= 0 && (cycle_count - last_valid_cycle) != 16)
                $fatal(1, "output spacing=%0d, expected 16", cycle_count - last_valid_cycle);
            last_valid_cycle <= cycle_count;
            output_count <= output_count + 1;
            if (output_count >= 512) begin
                measured_count <= measured_count + 1;
                if ($signed(m_sample) < measured_min)
                    measured_min <= $signed(m_sample);
                if ($signed(m_sample) > measured_max)
                    measured_max <= $signed(m_sample);
                if (($signed(m_sample) < 0 ? -$signed(m_sample) : $signed(m_sample)) > measured_abs_max)
                    measured_abs_max <= ($signed(m_sample) < 0 ? -$signed(m_sample) : $signed(m_sample));
            end
            if (m_otr)
                seen_otr <= 1'b1;
        end
        if (schedule_error)
            $fatal(1, "FIR MAC schedule overlap");
    end

    task automatic reset_measurement;
        begin
            rst_n = 1'b0;
            s_valid = 1'b0;
            s_sample = '0;
            s_otr = 1'b0;
            cycle_count = 0;
            last_valid_cycle = -1;
            output_count = 0;
            measured_count = 0;
            measured_min = 32767;
            measured_max = -32768;
            measured_abs_max = 0;
            seen_otr = 1'b0;
            check_spacing = 1'b0;
            repeat (8) @(posedge clk);
            rst_n = 1'b1;
            repeat (2) @(posedge clk);
            check_spacing = 1'b1;
        end
    endtask

    task automatic drive_tone(
        input real frequency_hz,
        input integer amplitude,
        input integer input_samples,
        input bit inject_otr
    );
        integer index;
        integer code;
        real phase;
        begin
            for (index = 0; index < input_samples; index = index + 1) begin
                phase = 2.0 * PI * frequency_hz * index / FS_HZ;
                code = $rtoi(amplitude * $sin(phase));
                @(negedge clk);
                s_valid = 1'b1;
                s_sample = code;
                s_otr = inject_otr && (index == 1001);
            end
            @(negedge clk);
            s_valid = 1'b0;
            s_sample = '0;
            s_otr = 1'b0;
            repeat (160) @(posedge clk);
        end
    endtask

    initial begin
        reset_measurement();
        drive_tone(500_000.0, 1000, 32_768, 1'b1);
        if (output_count != 2048)
            $fatal(1, "decimation count=%0d expected=2048", output_count);
        if (measured_count < 1400)
            $fatal(1, "too few passband samples measured: %0d", measured_count);
        if ((measured_max - measured_min) < 1900 || (measured_max - measured_min) > 2050)
            $fatal(1, "500 kHz passband p-p=%0d outside expected range", measured_max - measured_min);
        if (!seen_otr)
            $fatal(1, "OTR was not propagated across decimation");
        $display("PASS_TONE p-p=%0d outputs=%0d", measured_max - measured_min, output_count);

        reset_measurement();
        drive_tone(1_000_000.0, 1000, 32_768, 1'b0);
        if (output_count != 2048)
            $fatal(1, "stopband decimation count=%0d expected=2048", output_count);
        if (measured_abs_max > 4)
            $fatal(1, "1 MHz stopband residual=%0d exceeds fixed-point limit", measured_abs_max);
        $display("STOP_TONE abs_max=%0d outputs=%0d", measured_abs_max, output_count);

        $display("TEST_PASS tb_fir_decimator_16");
        $finish;
    end

endmodule
