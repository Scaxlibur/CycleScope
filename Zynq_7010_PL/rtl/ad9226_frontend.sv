`timescale 1ns/1ps

module ad9226_frontend #(
    parameter bit ADC_OFFSET_BINARY = 1'b1,
    parameter bit INVERT_POLARITY   = 1'b0
) (
    input  logic               clk,
    input  logic               rst_n,
    input  logic               sample_valid,
    input  logic        [11:0] adc_data,
    input  logic               adc_otr,
    output logic               sample_valid_out,
    output logic signed [15:0] sample_out,
    output logic               otr_out
);

    logic signed [12:0] centered_code;
    logic signed [12:0] normalized_code;

    always_comb begin
        if (ADC_OFFSET_BINARY) begin
            centered_code = $signed({1'b0, adc_data}) - 13'sd2048;
        end else begin
            centered_code = $signed({adc_data[11], adc_data});
        end

        if (INVERT_POLARITY) begin
            // -(-2048) cannot be represented on the frozen 12-bit scale.
            normalized_code = (centered_code == -13'sd2048)
                            ? 13'sd2047 : -centered_code;
        end else begin
            normalized_code = centered_code;
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            sample_valid_out <= 1'b0;
            sample_out       <= '0;
            otr_out          <= 1'b0;
        end else begin
            sample_valid_out <= sample_valid;
            if (sample_valid) begin
                sample_out <= {{3{normalized_code[12]}}, normalized_code};
                otr_out    <= adc_otr;
            end else begin
                otr_out    <= 1'b0;
            end
        end
    end

endmodule
