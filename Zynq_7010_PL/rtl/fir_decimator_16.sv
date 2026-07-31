`timescale 1ns/1ps

module fir_decimator_16 (
    input  logic               clk,
    input  logic               rst_n,
    input  logic               s_valid,
    input  logic signed [15:0] s_sample,
    input  logic               s_otr,
    input  logic        [63:0] s_tick,
    output logic               m_valid,
    output logic signed [15:0] m_sample,
    output logic               m_otr,
    output logic        [63:0] m_tick,
    output logic               schedule_error
);

    import fir_coeffs_pkg::*;

    logic v1, v2;
    logic signed [15:0] d1, d2;
    logic o1, o2;
    logic [63:0] t1, t2;
    logic e1, e2, e3;

    fir_mac_decimator #(
        .TAPS(STAGE1_TAPS),
        .DECIMATION(STAGE1_DECIMATION),
        .LANES(7),
        .COEFFS(STAGE1_COEFFS)
    ) stage1 (
        .clk, .rst_n,
        .s_valid, .s_sample, .s_otr, .s_tick,
        .m_valid(v1), .m_sample(d1), .m_otr(o1), .m_tick(t1),
        .schedule_error(e1)
    );

    fir_mac_decimator #(
        .TAPS(STAGE2_TAPS),
        .DECIMATION(STAGE2_DECIMATION),
        .LANES(3),
        .COEFFS(STAGE2_COEFFS)
    ) stage2 (
        .clk, .rst_n,
        .s_valid(v1), .s_sample(d1), .s_otr(o1), .s_tick(t1),
        .m_valid(v2), .m_sample(d2), .m_otr(o2), .m_tick(t2),
        .schedule_error(e2)
    );

    fir_mac_decimator #(
        .TAPS(STAGE3_TAPS),
        .DECIMATION(STAGE3_DECIMATION),
        .LANES(6),
        .COEFFS(STAGE3_COEFFS)
    ) stage3 (
        .clk, .rst_n,
        .s_valid(v2), .s_sample(d2), .s_otr(o2), .s_tick(t2),
        .m_valid, .m_sample, .m_otr, .m_tick,
        .schedule_error(e3)
    );

    assign schedule_error = e1 | e2 | e3;

endmodule
