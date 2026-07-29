`timescale 1ns/1ps

// Vivado Block Design module references require a Verilog/VHDL top file.
// Keep this adapter logic-free; cyclescope_pipeline.sv remains the RTL source.
module cyclescope_pipeline_bd (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 adc_clk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME adc_clk, ASSOCIATED_BUSIF m_axis, ASSOCIATED_RESET adc_rst_n, FREQ_HZ 65000000" *)
    input  wire        adc_clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 adc_rst_n RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME adc_rst_n, POLARITY ACTIVE_LOW" *)
    input  wire        adc_rst_n,
    input  wire [11:0] adc_data_a,
    input  wire        adc_otr_a,
    input  wire        capture_enable,
    input  wire        clear_stats,
    input  wire        test_pattern,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TDATA" *)
    output wire [15:0] m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TKEEP" *)
    output wire  [1:0] m_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TVALID" *)
    output wire        m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TREADY" *)
    input  wire        m_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TLAST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axis, TDATA_NUM_BYTES 2, HAS_TKEEP 1, HAS_TLAST 1" *)
    output wire        m_axis_tlast,
    input  wire        spi_cs_n,
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    output wire        spi_miso,
    output wire [31:0] frame_id,
    output wire [31:0] status_word
);

    cyclescope_pipeline pipeline_i (
        .adc_clk(adc_clk),
        .adc_rst_n(adc_rst_n),
        .adc_data_a(adc_data_a),
        .adc_otr_a(adc_otr_a),
        .capture_enable(capture_enable),
        .clear_stats(clear_stats),
        .test_pattern(test_pattern),
        .m_axis_tdata(m_axis_tdata),
        .m_axis_tkeep(m_axis_tkeep),
        .m_axis_tvalid(m_axis_tvalid),
        .m_axis_tready(m_axis_tready),
        .m_axis_tlast(m_axis_tlast),
        .spi_cs_n(spi_cs_n),
        .spi_sclk(spi_sclk),
        .spi_mosi(spi_mosi),
        .spi_miso(spi_miso),
        .frame_id(frame_id),
        .status_word(status_word)
    );

endmodule
