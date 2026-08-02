`timescale 1ns/1ps

module cyclescope_pipeline #(
    parameter int FRAME_SAMPLES = 8192,
    parameter int PERIOD_CYCLES = 3_250_000,
    parameter bit ADC_OFFSET_BINARY = 1'b1,
    parameter bit INVERT_POLARITY   = 1'b0,
    // Legacy diagnostic option only. The AD9226_2CH_V1.0 manual defines the
    // production mapping as direct A1=D0 ... A12=D11; local wiring swaps must
    // use an explicit permutation or a physical wiring correction.
    parameter bit ADC_REVERSE_BITS  = 1'b0
) (
    input  logic        adc_clk,
    input  logic        adc_rst_n,
    input  logic [11:0] adc_data_a,
    input  logic        adc_otr_a,

    input  logic        capture_enable,
    input  logic        clear_stats,
    input  logic        test_pattern,
    input  logic  [1:0] test_mode,
    input  logic [11:0] test_amplitude,
    input  logic [31:0] test_phase_increment,
    input  logic        inject_otr_toggle,
    input  logic        inject_overflow_toggle,
    input  logic        inject_frame_drop_toggle,

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
    output logic [31:0] status_word,
    output logic [63:0] frame_timestamp_tick,
    output logic [63:0] adc_tick
);

    import fir_coeffs_pkg::*;

    logic frontend_valid;
    logic signed [15:0] frontend_sample;
    logic frontend_otr;
    (* IOB = "TRUE" *) logic [11:0] adc_data_a_iob;
    (* IOB = "TRUE" *) logic adc_otr_a_iob;
    logic adc_iob_valid;
    logic [63:0] adc_data_tick_iob;
    logic [63:0] frontend_tick;
    logic signed [15:0] test_source_sample;
    logic signed [15:0] filter_input;
    logic filter_input_otr;
    logic filter_valid;
    logic signed [15:0] filter_sample;
    logic filter_otr;
    logic [63:0] filter_tick;
    logic [63:0] filter_timestamp_tick;
    logic filter_schedule_error;
    (* async_reg = "true" *) logic capture_enable_meta;
    (* async_reg = "true" *) logic capture_enable_sync;
    (* async_reg = "true" *) logic clear_stats_meta;
    (* async_reg = "true" *) logic clear_stats_sync;
    logic clear_stats_sync_d;
    (* async_reg = "true" *) logic test_pattern_meta;
    (* async_reg = "true" *) logic test_pattern_sync;
    (* async_reg = "true" *) logic [1:0] test_mode_meta;
    (* async_reg = "true" *) logic [1:0] test_mode_sync;
    (* async_reg = "true" *) logic [11:0] test_amplitude_meta;
    (* async_reg = "true" *) logic [11:0] test_amplitude_sync;
    (* async_reg = "true" *) logic [31:0] test_phase_increment_meta;
    (* async_reg = "true" *) logic [31:0] test_phase_increment_sync;
    (* async_reg = "true" *) logic inject_otr_meta;
    (* async_reg = "true" *) logic inject_otr_sync;
    logic inject_otr_sync_d;
    (* async_reg = "true" *) logic inject_overflow_meta;
    (* async_reg = "true" *) logic inject_overflow_sync;
    logic inject_overflow_sync_d;
    (* async_reg = "true" *) logic inject_frame_drop_meta;
    (* async_reg = "true" *) logic inject_frame_drop_sync;
    logic inject_frame_drop_sync_d;
    logic clear_stats_pulse;
    logic inject_otr_pulse;
    logic inject_overflow_pulse;
    logic inject_frame_drop_pulse;

    always_ff @(posedge adc_clk) begin
        if (!adc_rst_n) begin
            capture_enable_meta <= 1'b0;
            capture_enable_sync <= 1'b0;
            clear_stats_meta    <= 1'b0;
            clear_stats_sync    <= 1'b0;
            clear_stats_sync_d  <= 1'b0;
            test_pattern_meta   <= 1'b0;
            test_pattern_sync   <= 1'b0;
            test_mode_meta      <= '0;
            test_mode_sync      <= '0;
            test_amplitude_meta <= 12'd2047;
            test_amplitude_sync <= 12'd2047;
            test_phase_increment_meta <= '0;
            test_phase_increment_sync <= '0;
            inject_otr_meta          <= 1'b0;
            inject_otr_sync          <= 1'b0;
            inject_otr_sync_d        <= 1'b0;
            inject_overflow_meta     <= 1'b0;
            inject_overflow_sync     <= 1'b0;
            inject_overflow_sync_d   <= 1'b0;
            inject_frame_drop_meta   <= 1'b0;
            inject_frame_drop_sync   <= 1'b0;
            inject_frame_drop_sync_d <= 1'b0;
        end else begin
            capture_enable_meta <= capture_enable;
            capture_enable_sync <= capture_enable_meta;
            clear_stats_meta    <= clear_stats;
            clear_stats_sync    <= clear_stats_meta;
            clear_stats_sync_d  <= clear_stats_sync;
            test_pattern_meta   <= test_pattern;
            test_pattern_sync   <= test_pattern_meta;
            test_mode_meta      <= test_mode;
            test_mode_sync      <= test_mode_meta;
            test_amplitude_meta <= test_amplitude;
            test_amplitude_sync <= test_amplitude_meta;
            test_phase_increment_meta <= test_phase_increment;
            test_phase_increment_sync <= test_phase_increment_meta;
            inject_otr_meta          <= inject_otr_toggle;
            inject_otr_sync          <= inject_otr_meta;
            inject_otr_sync_d        <= inject_otr_sync;
            inject_overflow_meta     <= inject_overflow_toggle;
            inject_overflow_sync     <= inject_overflow_meta;
            inject_overflow_sync_d   <= inject_overflow_sync;
            inject_frame_drop_meta   <= inject_frame_drop_toggle;
            inject_frame_drop_sync   <= inject_frame_drop_meta;
            inject_frame_drop_sync_d <= inject_frame_drop_sync;
        end
    end

    assign clear_stats_pulse = clear_stats_sync & ~clear_stats_sync_d;
    assign inject_otr_pulse = inject_otr_sync ^ inject_otr_sync_d;
    assign inject_overflow_pulse =
        inject_overflow_sync ^ inject_overflow_sync_d;
    assign inject_frame_drop_pulse =
        inject_frame_drop_sync ^ inject_frame_drop_sync_d;
    assign filter_timestamp_tick = filter_tick - FIR_GROUP_DELAY_ADC_TICKS;

    // Capture the source-synchronous ADC bus in IOB registers first. Keeping
    // offset-binary conversion out of the input path preserves the external
    // tOD setup/hold margin at 65 MHz.
    always_ff @(posedge adc_clk) begin
        if (!adc_rst_n) begin
            adc_data_a_iob <= '0;
            adc_otr_a_iob  <= 1'b0;
            adc_iob_valid  <= 1'b0;
            adc_tick       <= '0;
            adc_data_tick_iob <= '0;
            frontend_tick  <= '0;
        end else begin
            adc_data_a_iob <= adc_data_a;
            adc_otr_a_iob  <= adc_otr_a;
            adc_iob_valid  <= 1'b1;
            adc_data_tick_iob <= adc_tick;
            frontend_tick  <= adc_data_tick_iob;
            adc_tick       <= adc_tick + 1'b1;
        end
    end

    ad9226_frontend #(
        .ADC_OFFSET_BINARY(ADC_OFFSET_BINARY),
        .INVERT_POLARITY(INVERT_POLARITY),
        .ADC_REVERSE_BITS(ADC_REVERSE_BITS)
    ) frontend (
        .clk(adc_clk),
        .rst_n(adc_rst_n),
        .sample_valid(adc_iob_valid),
        .adc_data(adc_data_a_iob),
        .adc_otr(adc_otr_a_iob),
        .sample_valid_out(frontend_valid),
        .sample_out(frontend_sample),
        .otr_out(frontend_otr)
    );

    test_pattern_generator test_source (
        .clk(adc_clk),
        .rst_n(adc_rst_n),
        .sample_advance(frontend_valid),
        .mode(test_mode_sync),
        .amplitude(test_amplitude_sync),
        .phase_increment(test_phase_increment_sync),
        .sample(test_source_sample)
    );

    always_comb begin
        filter_input     = test_pattern_sync ? test_source_sample : frontend_sample;
        filter_input_otr = test_pattern_sync ? 1'b0 : frontend_otr;
    end

    fir_decimator_16 filter (
        .clk(adc_clk),
        .rst_n(adc_rst_n),
        .s_valid(frontend_valid),
        .s_sample(filter_input),
        .s_otr(filter_input_otr),
        .s_tick(frontend_tick),
        .m_valid(filter_valid),
        .m_sample(filter_sample),
        .m_otr(filter_otr),
        .m_tick(filter_tick),
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
        .s_timestamp_tick(filter_timestamp_tick),
        .filter_error(filter_schedule_error),
        .inject_otr(inject_otr_pulse),
        .inject_overflow(inject_overflow_pulse),
        .inject_frame_drop(inject_frame_drop_pulse),
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
        .status_word,
        .frame_timestamp_tick
    );

endmodule
