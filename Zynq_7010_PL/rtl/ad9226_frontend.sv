`timescale 1ns/1ps

module ad9226_frontend #(
    parameter bit ADC_OFFSET_BINARY = 1'b1,
    parameter bit INVERT_POLARITY   = 1'b0,
    parameter bit ADC_REVERSE_BITS  = 1'b0
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
    logic        [11:0] ordered_adc_data;

    genvar data_bit;
    generate
        if (ADC_REVERSE_BITS) begin : generate_reversed_adc_bits
            for (data_bit = 0; data_bit < 12; data_bit = data_bit + 1) begin : generate_bit
                assign ordered_adc_data[data_bit] = adc_data[11-data_bit];
            end
        end else begin : generate_direct_adc_bits
            assign ordered_adc_data = adc_data;
        end
    endgenerate

    always_comb begin
        if (ADC_OFFSET_BINARY) begin
            centered_code = $signed({1'b0, ordered_adc_data}) - 13'sd2048;
        end else begin
            centered_code = $signed({ordered_adc_data[11], ordered_adc_data});
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
