`timescale 1ns/1ps

module cyclescope_pipeline #(
    parameter int FRAME_SAMPLES = 8192,
    parameter int PERIOD_CYCLES = 3_250_000,
    parameter bit ADC_OFFSET_BINARY = 1'b1,
    parameter bit INVERT_POLARITY   = 1'b0
) (
    input  logic        adc_clk,
    input  logic        adc_rst_n,
    input  logic [11:0] adc_data_a,
    input  logic        adc_otr_a,

    input  logic        capture_enable,
    input  logic        clear_stats,
    input  logic        test_pattern,

    output logic [15:0] m_axis_tdata,
    output logic  [1:0] m_axis_tkeep,
    output logic        m_axis_tvalid,
    input  logic        m_axis_tready,
    output logic        m_axis_tlast,

    input  logic        spi_cs_n,
    input  logic        spi_sclk,
    input  logic        spi_mosi,
    output logic        spi_miso,

    output logic [31:0] frame_id,
    output logic [31:0] status_word
);

    logic frontend_valid;
    logic signed [15:0] frontend_sample;
    logic frontend_otr;
    logic signed [15:0] test_counter;
    logic signed [15:0] filter_input;
    logic filter_input_otr;
    logic filter_valid;
    logic signed [15:0] filter_sample;
    logic filter_otr;
    logic filter_schedule_error;
    (* async_reg = "true" *) logic capture_enable_meta;
    (* async_reg = "true" *) logic capture_enable_sync;
    (* async_reg = "true" *) logic clear_stats_meta;
    (* async_reg = "true" *) logic clear_stats_sync;
    logic clear_stats_sync_d;
    (* async_reg = "true" *) logic test_pattern_meta;
    (* async_reg = "true" *) logic test_pattern_sync;
    logic clear_stats_pulse;

    always_ff @(posedge adc_clk) begin
        if (!adc_rst_n) begin
            capture_enable_meta <= 1'b0;
            capture_enable_sync <= 1'b0;
            clear_stats_meta    <= 1'b0;
            clear_stats_sync    <= 1'b0;
            clear_stats_sync_d  <= 1'b0;
            test_pattern_meta   <= 1'b0;
            test_pattern_sync   <= 1'b0;
        end else begin
            capture_enable_meta <= capture_enable;
            capture_enable_sync <= capture_enable_meta;
            clear_stats_meta    <= clear_stats;
            clear_stats_sync    <= clear_stats_meta;
            clear_stats_sync_d  <= clear_stats_sync;
            test_pattern_meta   <= test_pattern;
            test_pattern_sync   <= test_pattern_meta;
        end
    end

    assign clear_stats_pulse = clear_stats_sync & ~clear_stats_sync_d;

    ad9226_frontend #(
        .ADC_OFFSET_BINARY(ADC_OFFSET_BINARY),
        .INVERT_POLARITY(INVERT_POLARITY)
    ) frontend (
        .clk(adc_clk),
        .rst_n(adc_rst_n),
        .sample_valid(1'b1),
        .adc_data(adc_data_a),
        .adc_otr(adc_otr_a),
        .sample_valid_out(frontend_valid),
        .sample_out(frontend_sample),
        .otr_out(frontend_otr)
    );

    always_ff @(posedge adc_clk) begin
        if (!adc_rst_n)
            test_counter <= -16'sd1024;
        else if (frontend_valid)
            test_counter <= (test_counter == 16'sd1023) ? -16'sd1024
                                                        : test_counter + 1'b1;
    end

    always_comb begin
        filter_input     = test_pattern_sync ? test_counter : frontend_sample;
        filter_input_otr = test_pattern_sync ? 1'b0 : frontend_otr;
    end

    fir_decimator_16 filter (
        .clk(adc_clk),
        .rst_n(adc_rst_n),
        .s_valid(frontend_valid),
        .s_sample(filter_input),
        .s_otr(filter_input_otr),
        .m_valid(filter_valid),
        .m_sample(filter_sample),
        .m_otr(filter_otr),
        .schedule_error(filter_schedule_error)
    );

    frame_store_axis_spi #(
        .FRAME_SAMPLES(FRAME_SAMPLES),
        .PERIOD_CYCLES(PERIOD_CYCLES)
    ) frame_store (
        .clk(adc_clk),
        .rst_n(adc_rst_n),
        .capture_enable(capture_enable_sync),
        .clear_stats(clear_stats_pulse),
        .s_valid(filter_valid),
        .s_sample(filter_sample),
        .s_otr(filter_otr),
        .filter_error(filter_schedule_error),
        .m_axis_tdata,
        .m_axis_tkeep,
        .m_axis_tvalid,
        .m_axis_tready,
        .m_axis_tlast,
        .spi_cs_n,
        .spi_sclk,
        .spi_mosi,
        .spi_miso,
        .frame_id,
        .status_word
    );

endmodule
