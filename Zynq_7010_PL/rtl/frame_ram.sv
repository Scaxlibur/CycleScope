`timescale 1ns/1ps

// Same-clock simple dual-port RAM: one capture write port and one AXI read port.
module frame_ram_sdp #(
    parameter int DATA_WIDTH = 16,
    parameter int DEPTH = 8192,
    parameter int ADDR_WIDTH = $clog2(DEPTH)
) (
    input  logic                  clk,
    input  logic                  wr_en,
    input  logic [ADDR_WIDTH-1:0] wr_addr,
    input  logic [DATA_WIDTH-1:0] wr_data,
    input  logic                  rd_en,
    input  logic [ADDR_WIDTH-1:0] rd_addr,
    output logic [DATA_WIDTH-1:0] rd_data
);

    (* ram_style = "block" *) logic [DATA_WIDTH-1:0] memory [0:DEPTH-1];

    always_ff @(posedge clk) begin
        if (wr_en)
            memory[wr_addr] <= wr_data;
        if (rd_en)
            rd_data <= memory[rd_addr];
    end

endmodule
