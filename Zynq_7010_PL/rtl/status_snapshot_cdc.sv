`timescale 1ns/1ps

// Coherent multi-bit snapshot transfer from src_clk to dst_clk.
//
// src_hold remains unchanged until the destination has observed the matching
// acknowledgement and issued the next request. The destination samples the
// bundled bus only after the acknowledgement has crossed the same two-stage
// synchronizer latency, then waits one additional cycle before publishing it.
module status_snapshot_cdc #(
    parameter int WIDTH = 64
) (
    input  logic             src_clk,
    input  logic             src_rst_n,
    input  logic [WIDTH-1:0] src_data,

    input  logic             dst_clk,
    input  logic             dst_rst_n,
    output logic [WIDTH-1:0] dst_data,
    output logic             dst_valid
);

    typedef enum logic [1:0] {
        DST_REQUEST,
        DST_WAIT_ACK,
        DST_SETTLE
    } dst_state_t;

    logic request_toggle;
    logic acknowledge_toggle;
    logic [WIDTH-1:0] src_hold;

    (* async_reg = "true" *) logic request_meta;
    (* async_reg = "true" *) logic request_sync;
    (* async_reg = "true" *) logic acknowledge_meta;
    (* async_reg = "true" *) logic acknowledge_sync;
    (* async_reg = "true" *) logic [WIDTH-1:0] data_meta;
    (* async_reg = "true" *) logic [WIDTH-1:0] data_sync;

    dst_state_t dst_state;

    always_ff @(posedge src_clk) begin
        if (!src_rst_n) begin
            request_meta      <= 1'b0;
            request_sync      <= 1'b0;
            acknowledge_toggle <= 1'b0;
            src_hold          <= '0;
        end else begin
            request_meta <= request_toggle;
            request_sync <= request_meta;

            if (request_sync != acknowledge_toggle) begin
                src_hold           <= src_data;
                acknowledge_toggle <= request_sync;
            end
        end
    end

    always_ff @(posedge dst_clk) begin
        if (!dst_rst_n) begin
            acknowledge_meta <= 1'b0;
            acknowledge_sync <= 1'b0;
            data_meta         <= '0;
            data_sync         <= '0;
            request_toggle    <= 1'b0;
            dst_data          <= '0;
            dst_valid         <= 1'b0;
            dst_state         <= DST_REQUEST;
        end else begin
            acknowledge_meta <= acknowledge_toggle;
            acknowledge_sync <= acknowledge_meta;
            data_meta         <= src_hold;
            data_sync         <= data_meta;
            dst_valid         <= 1'b0;

            case (dst_state)
                DST_REQUEST: begin
                    request_toggle <= ~request_toggle;
                    dst_state      <= DST_WAIT_ACK;
                end
                DST_WAIT_ACK: begin
                    if (acknowledge_sync == request_toggle)
                        dst_state <= DST_SETTLE;
                end
                DST_SETTLE: begin
                    dst_data  <= data_sync;
                    dst_valid <= 1'b1;
                    dst_state <= DST_REQUEST;
                end
                default: dst_state <= DST_REQUEST;
            endcase
        end
    end

endmodule
