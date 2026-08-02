`timescale 1ns/1ps

module tb_status_snapshot_cdc;

    logic src_clk = 1'b0;
    logic dst_clk = 1'b0;
    logic src_rst_n = 1'b0;
    logic dst_rst_n = 1'b0;
    logic [31:0] source_counter = '0;
    logic [191:0] source_data;
    logic [191:0] snapshot_data;
    logic snapshot_valid;
    int snapshots_seen = 0;

    always #6.5 src_clk = ~src_clk;
    always #5.0 dst_clk = ~dst_clk;

    assign source_data = {6{source_counter}};

    status_snapshot_cdc #(.WIDTH(192)) dut (
        .src_clk,
        .src_rst_n,
        .src_data(source_data),
        .dst_clk,
        .dst_rst_n,
        .dst_data(snapshot_data),
        .dst_valid(snapshot_valid)
    );

    always_ff @(posedge src_clk) begin
        if (!src_rst_n)
            source_counter <= '0;
        else
            source_counter <= source_counter + 32'h0101_0101;
    end

    always_ff @(posedge dst_clk) begin
        if (dst_rst_n && snapshot_valid) begin
            if (snapshot_data[191:160] !== snapshot_data[31:0] ||
                snapshot_data[159:128] !== snapshot_data[31:0] ||
                snapshot_data[127:96] !== snapshot_data[31:0] ||
                snapshot_data[95:64] !== snapshot_data[31:0] ||
                snapshot_data[63:32] !== snapshot_data[31:0])
                $fatal(1, "torn 192-bit snapshot: %048h", snapshot_data);
            snapshots_seen <= snapshots_seen + 1;
        end
    end

    initial begin
        repeat (5) @(posedge dst_clk);
        src_rst_n <= 1'b1;
        dst_rst_n <= 1'b1;

        wait (snapshots_seen == 20);
        $display("TEST_PASS tb_status_snapshot_cdc");
        $finish;
    end

    initial begin
        #20us;
        $fatal(1, "timeout waiting for coherent snapshots");
    end

endmodule
