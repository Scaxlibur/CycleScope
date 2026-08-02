`timescale 1ns/1ps

// Verilog adapter for the SystemVerilog CDC module used in Block Design.
module status_snapshot_cdc_bd (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 src_clk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME src_clk, ASSOCIATED_RESET src_rst_n, FREQ_HZ 65000000" *)
    input  wire        src_clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 src_rst_n RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME src_rst_n, POLARITY ACTIVE_LOW" *)
    input  wire        src_rst_n,
    input  wire [191:0] src_data,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 dst_clk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME dst_clk, ASSOCIATED_RESET dst_rst_n, FREQ_HZ 100000000" *)
    input  wire        dst_clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 dst_rst_n RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME dst_rst_n, POLARITY ACTIVE_LOW" *)
    input  wire        dst_rst_n,
    output wire [191:0] dst_data,
    output wire        dst_valid
);

    status_snapshot_cdc #(.WIDTH(192)) snapshot_i (
        .src_clk(src_clk),
        .src_rst_n(src_rst_n),
        .src_data(src_data),
        .dst_clk(dst_clk),
        .dst_rst_n(dst_rst_n),
        .dst_data(dst_data),
        .dst_valid(dst_valid)
    );

endmodule
