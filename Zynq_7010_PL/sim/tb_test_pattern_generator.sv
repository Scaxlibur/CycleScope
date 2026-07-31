`timescale 1ns/1ps

module tb_test_pattern_generator;

    localparam real PI = 3.14159265358979323846;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic sample_advance = 1'b0;
    logic [1:0] mode = 2'd0;
    logic [11:0] amplitude = 12'd2047;
    logic [31:0] phase_increment = 32'd0;
    logic signed [15:0] sample;

    integer reference_512 [0:511];
    integer reference_256 [0:255];
    integer index;
    integer value;
    integer minimum;
    integer maximum;
    integer sum;
    real real_part_3;
    real imag_part_3;
    real real_part_10;
    real imag_part_10;
    real real_part_23;
    real imag_part_23;
    real real_part_7;
    real imag_part_7;
    real magnitude_3;
    real magnitude_10;
    real magnitude_23;
    real magnitude_7;
    real angle;

    always #7.692 clk = ~clk;

    test_pattern_generator dut (.*);

    task automatic reset_generator;
        begin
            rst_n = 1'b0;
            sample_advance = 1'b0;
            repeat (5) @(posedge clk);
            rst_n = 1'b1;
            @(negedge clk);
        end
    endtask

    initial begin
        reset_generator();
        mode = 2'd0;
        if ($signed(sample) != -2048)
            $fatal(1, "ramp reset sample=%0d", $signed(sample));
        sample_advance = 1'b1;
        for (index = 0; index < 4096; index = index + 1) begin
            @(negedge clk);
            value = $signed(sample);
            if (value != ((index == 4095) ? -2048 : -2047 + index))
                $fatal(1, "ramp mismatch index=%0d value=%0d", index, value);
        end
        $display("RAMP_PATTERN_PASS");

        reset_generator();
        mode = 2'd1;
        amplitude = 12'd1600;
        // Output-frame bin 256: 256*32768, exactly 512 raw clocks/period.
        phase_increment = 32'd8_388_608;
        sample_advance = 1'b1;
        minimum = 32767;
        maximum = -32768;
        sum = 0;
        for (index = 0; index < 512; index = index + 1) begin
            @(negedge clk);
            value = $signed(sample);
            reference_512[index] = value;
            if (value < minimum)
                minimum = value;
            if (value > maximum)
                maximum = value;
            sum = sum + value;
        end
        for (index = 0; index < 512; index = index + 1) begin
            @(negedge clk);
            if ($signed(sample) != reference_512[index])
                $fatal(1, "sine period mismatch index=%0d", index);
        end
        if (minimum > -1590 || maximum < 1590 || sum < -512 || sum > 512)
            $fatal(1, "sine amplitude/DC mismatch min=%0d max=%0d sum=%0d",
                   minimum, maximum, sum);

        // A second increment proves frequency is a live configuration, not a
        // label attached to one hard-coded table traversal.
        phase_increment = 32'd16_777_216;
        for (index = 0; index < 256; index = index + 1) begin
            @(negedge clk);
            reference_256[index] = $signed(sample);
        end
        for (index = 0; index < 256; index = index + 1) begin
            @(negedge clk);
            if ($signed(sample) != reference_256[index])
                $fatal(1, "configured sine period mismatch index=%0d", index);
        end
        $display("SINE_PATTERN_PASS min=%0d max=%0d", minimum, maximum);

        reset_generator();
        mode = 2'd2;
        amplitude = 12'd1800;
        sample_advance = 1'b1;
        real_part_3 = 0.0;
        imag_part_3 = 0.0;
        real_part_10 = 0.0;
        imag_part_10 = 0.0;
        real_part_23 = 0.0;
        imag_part_23 = 0.0;
        real_part_7 = 0.0;
        imag_part_7 = 0.0;
        for (index = 0; index < 4096; index = index + 1) begin
            @(negedge clk);
            value = $signed(sample);
            if (value < -2048 || value > 2047)
                $fatal(1, "multitone outside 12-bit range: %0d", value);
            angle = 2.0 * PI * index / 4096.0;
            real_part_3 = real_part_3 + value * $cos(3.0 * angle);
            imag_part_3 = imag_part_3 - value * $sin(3.0 * angle);
            real_part_10 = real_part_10 + value * $cos(10.0 * angle);
            imag_part_10 = imag_part_10 - value * $sin(10.0 * angle);
            real_part_23 = real_part_23 + value * $cos(23.0 * angle);
            imag_part_23 = imag_part_23 - value * $sin(23.0 * angle);
            real_part_7 = real_part_7 + value * $cos(7.0 * angle);
            imag_part_7 = imag_part_7 - value * $sin(7.0 * angle);
        end
        magnitude_3 = $sqrt(real_part_3 * real_part_3 + imag_part_3 * imag_part_3);
        magnitude_10 = $sqrt(real_part_10 * real_part_10 + imag_part_10 * imag_part_10);
        magnitude_23 = $sqrt(real_part_23 * real_part_23 + imag_part_23 * imag_part_23);
        magnitude_7 = $sqrt(real_part_7 * real_part_7 + imag_part_7 * imag_part_7);
        if (magnitude_3 < 1_000_000.0 || magnitude_10 < 500_000.0 ||
            magnitude_23 < 500_000.0 || magnitude_7 > 20_000.0)
            $fatal(1, "multitone spectrum mismatch m3=%f m10=%f m23=%f m7=%f",
                   magnitude_3, magnitude_10, magnitude_23, magnitude_7);
        $display("MULTITONE_PATTERN_PASS m3=%f m10=%f m23=%f m7=%f",
                 magnitude_3, magnitude_10, magnitude_23, magnitude_7);

        $display("TEST_PASS tb_test_pattern_generator");
        $finish;
    end

    initial begin
        #500us;
        $fatal(1, "testbench timeout");
    end

endmodule
