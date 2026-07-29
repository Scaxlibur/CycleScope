`timescale 1ns/1ps

module tb_ad9226_frontend;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic sample_valid = 1'b0;
    logic [11:0] adc_data = '0;
    logic adc_otr = 1'b0;

    logic valid_offset;
    logic signed [15:0] sample_offset;
    logic otr_offset;
    logic valid_inverted;
    logic signed [15:0] sample_inverted;
    logic valid_twos;
    logic signed [15:0] sample_twos;

    always #5 clk = ~clk;

    ad9226_frontend #(
        .ADC_OFFSET_BINARY(1'b1),
        .INVERT_POLARITY(1'b0)
    ) dut_offset (
        .clk, .rst_n, .sample_valid, .adc_data, .adc_otr,
        .sample_valid_out(valid_offset), .sample_out(sample_offset), .otr_out(otr_offset)
    );

    ad9226_frontend #(
        .ADC_OFFSET_BINARY(1'b1),
        .INVERT_POLARITY(1'b1)
    ) dut_inverted (
        .clk, .rst_n, .sample_valid, .adc_data, .adc_otr,
        .sample_valid_out(valid_inverted), .sample_out(sample_inverted), .otr_out()
    );

    ad9226_frontend #(
        .ADC_OFFSET_BINARY(1'b0),
        .INVERT_POLARITY(1'b0)
    ) dut_twos (
        .clk, .rst_n, .sample_valid, .adc_data, .adc_otr,
        .sample_valid_out(valid_twos), .sample_out(sample_twos), .otr_out()
    );

    task automatic drive_and_check(
        input logic [11:0] raw,
        input logic otr,
        input integer expected_offset,
        input integer expected_inverted,
        input integer expected_twos
    );
        begin
            @(negedge clk);
            sample_valid = 1'b1;
            adc_data = raw;
            adc_otr = otr;
            @(posedge clk);
            #1;
            if (!valid_offset || !valid_inverted || !valid_twos)
                $fatal(1, "valid pipeline mismatch for raw=%h", raw);
            if ($signed(sample_offset) !== expected_offset)
                $fatal(1, "offset-binary raw=%h got=%0d expected=%0d", raw, $signed(sample_offset), expected_offset);
            if ($signed(sample_inverted) !== expected_inverted)
                $fatal(1, "inverted raw=%h got=%0d expected=%0d", raw, $signed(sample_inverted), expected_inverted);
            if ($signed(sample_twos) !== expected_twos)
                $fatal(1, "two's-complement raw=%h got=%0d expected=%0d", raw, $signed(sample_twos), expected_twos);
            if (otr_offset !== otr)
                $fatal(1, "OTR alignment mismatch for raw=%h", raw);
        end
    endtask

    initial begin
        repeat (4) @(posedge clk);
        rst_n = 1'b1;

        drive_and_check(12'h000, 1'b0, -2048,  2047,     0);
        drive_and_check(12'h7ff, 1'b0,    -1,     1,  2047);
        drive_and_check(12'h800, 1'b1,     0,     0, -2048);
        drive_and_check(12'h801, 1'b0,     1,    -1, -2047);
        drive_and_check(12'hfff, 1'b0,  2047, -2047,    -1);

        @(negedge clk);
        sample_valid = 1'b0;
        adc_otr = 1'b1;
        @(posedge clk);
        #1;
        if (valid_offset || otr_offset)
            $fatal(1, "invalid cycle leaked valid/OTR");

        $display("TEST_PASS tb_ad9226_frontend");
        $finish;
    end

endmodule
