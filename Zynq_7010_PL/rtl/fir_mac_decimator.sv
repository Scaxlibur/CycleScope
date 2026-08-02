`timescale 1ns/1ps

module fir_mac_decimator #(
    parameter int INPUT_WIDTH  = 16,
    parameter int OUTPUT_WIDTH = 16,
    parameter int COEFF_WIDTH  = 18,
    parameter int COEFF_FRAC   = 17,
    parameter int ACC_WIDTH    = 48,
    parameter int TAPS         = 21,
    parameter int DECIMATION   = 4,
    parameter int LANES        = 7,
    parameter logic signed [COEFF_WIDTH-1:0] COEFFS [0:TAPS-1] = '{default: '0}
) (
    input  logic                           clk,
    input  logic                           rst_n,
    input  logic                           s_valid,
    input  logic signed [INPUT_WIDTH-1:0]  s_sample,
    input  logic                           s_otr,
    input  logic                    [63:0] s_tick,
    output logic                           m_valid,
    output logic signed [OUTPUT_WIDTH-1:0] m_sample,
    output logic                           m_otr,
    output logic                    [63:0] m_tick,
    output logic                           schedule_error
);

    localparam int PRODUCT_WIDTH = INPUT_WIDTH + COEFF_WIDTH;
    localparam int PAIRS         = (LANES + 1) / 2;
    localparam int DECIM_W       = (DECIMATION <= 1) ? 1 : $clog2(DECIMATION);
    localparam int TAP_W         = (TAPS <= 1) ? 1 : $clog2(TAPS + LANES);

    logic signed [INPUT_WIDTH-1:0] delay_line [0:TAPS-1];
    logic signed [INPUT_WIDTH-1:0] snapshot   [0:TAPS-1];

    logic [DECIM_W-1:0] decim_count;
    logic otr_accum;

    logic issue_active;
    logic [TAP_W-1:0] issue_base;
    logic issue_otr;
    logic [63:0] issue_tick;

    logic signed [PRODUCT_WIDTH-1:0] product_pipe [0:LANES-1];
    logic product_valid;
    logic product_first;
    logic product_last;
    logic product_otr;
    logic [63:0] product_tick;

    logic signed [ACC_WIDTH-1:0] pair_pipe [0:PAIRS-1];
    logic pair_valid;
    logic pair_first;
    logic pair_last;
    logic pair_otr;
    logic [63:0] pair_tick;

    logic signed [ACC_WIDTH-1:0] pair_sum_comb;
    logic signed [ACC_WIDTH-1:0] group_sum_pipe;
    logic group_valid;
    logic group_first;
    logic group_last;
    logic group_otr;
    logic [63:0] group_tick;

    logic signed [ACC_WIDTH-1:0] accumulator;
    logic signed [ACC_WIDTH-1:0] accumulated_group;

    integer i;
    integer lane;
    integer pair_comb;
    integer pair_seq;

    function automatic logic signed [OUTPUT_WIDTH-1:0] round_and_saturate(
        input logic signed [ACC_WIDTH-1:0] value
    );
        logic signed [ACC_WIDTH-1:0] rounded;
        logic signed [ACC_WIDTH-1:0] magnitude;
        logic signed [ACC_WIDTH-1:0] half_lsb;
        logic signed [ACC_WIDTH-1:0] max_value;
        logic signed [ACC_WIDTH-1:0] min_value;
        begin
            half_lsb = {{(ACC_WIDTH-1){1'b0}}, 1'b1} <<< (COEFF_FRAC-1);
            if (value >= 0) begin
                rounded = (value + half_lsb) >>> COEFF_FRAC;
            end else begin
                magnitude = -value;
                rounded = -((magnitude + half_lsb) >>> COEFF_FRAC);
            end
            max_value = ({{(ACC_WIDTH-1){1'b0}}, 1'b1} <<< (OUTPUT_WIDTH-1)) - 1;
            min_value = -({{(ACC_WIDTH-1){1'b0}}, 1'b1} <<< (OUTPUT_WIDTH-1));
            if (rounded > max_value)
                round_and_saturate = {1'b0, {(OUTPUT_WIDTH-1){1'b1}}};
            else if (rounded < min_value)
                round_and_saturate = {1'b1, {(OUTPUT_WIDTH-1){1'b0}}};
            else
                round_and_saturate = rounded[OUTPUT_WIDTH-1:0];
        end
    endfunction

    always_comb begin
        pair_sum_comb = '0;
        for (pair_comb = 0; pair_comb < PAIRS; pair_comb = pair_comb + 1)
            pair_sum_comb = pair_sum_comb + pair_pipe[pair_comb];

        accumulated_group = group_first ? group_sum_pipe
                                        : accumulator + group_sum_pipe;
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            m_valid        <= 1'b0;
            m_sample       <= '0;
            m_otr          <= 1'b0;
            m_tick         <= '0;
            schedule_error <= 1'b0;
            decim_count    <= '0;
            otr_accum      <= 1'b0;
            issue_active   <= 1'b0;
            issue_base     <= '0;
            issue_otr      <= 1'b0;
            issue_tick     <= '0;
            product_valid  <= 1'b0;
            product_first  <= 1'b0;
            product_last   <= 1'b0;
            product_otr    <= 1'b0;
            product_tick   <= '0;
            pair_valid     <= 1'b0;
            pair_first     <= 1'b0;
            pair_last      <= 1'b0;
            pair_otr       <= 1'b0;
            pair_tick      <= '0;
            group_sum_pipe <= '0;
            group_valid    <= 1'b0;
            group_first    <= 1'b0;
            group_last     <= 1'b0;
            group_otr      <= 1'b0;
            group_tick     <= '0;
            accumulator    <= '0;
            for (i = 0; i < TAPS; i = i + 1) begin
                delay_line[i] <= '0;
                snapshot[i]   <= '0;
            end
            for (lane = 0; lane < LANES; lane = lane + 1)
                product_pipe[lane] <= '0;
            for (pair_seq = 0; pair_seq < PAIRS; pair_seq = pair_seq + 1)
                pair_pipe[pair_seq] <= '0;
        end else begin
            m_valid       <= 1'b0;
            m_otr         <= 1'b0;
            product_valid <= 1'b0;

            // Input history advances only for real samples. A stable snapshot is
            // taken at each decimation boundary so MAC issue can overlap new input.
            if (s_valid) begin
                delay_line[0] <= s_sample;
                for (i = 1; i < TAPS; i = i + 1)
                    delay_line[i] <= delay_line[i-1];

                if (decim_count == DECIMATION-1) begin
                    decim_count <= '0;
                    if (issue_active) begin
                        schedule_error <= 1'b1;
                    end else begin
                        snapshot[0] <= s_sample;
                        for (i = 1; i < TAPS; i = i + 1)
                            snapshot[i] <= delay_line[i-1];
                        issue_active <= 1'b1;
                        issue_base   <= '0;
                        issue_otr    <= otr_accum | s_otr;
                        issue_tick   <= s_tick;
                    end
                    otr_accum <= 1'b0;
                end else begin
                    decim_count <= decim_count + 1'b1;
                    otr_accum   <= otr_accum | s_otr;
                end
            end

            // Pipeline stage 1: one registered multiplier per lane.
            if (issue_active) begin
                for (lane = 0; lane < LANES; lane = lane + 1) begin
                    if ((issue_base + lane) < TAPS)
                        product_pipe[lane] <= $signed(snapshot[issue_base + lane])
                                            * $signed(COEFFS[issue_base + lane]);
                    else
                        product_pipe[lane] <= '0;
                end
                product_valid <= 1'b1;
                product_first <= (issue_base == 0);
                product_last  <= ((issue_base + LANES) >= TAPS);
                product_otr   <= issue_otr;
                product_tick  <= issue_tick;
                if ((issue_base + LANES) >= TAPS) begin
                    issue_active <= 1'b0;
                    issue_base   <= '0;
                end else begin
                    issue_base <= issue_base + LANES;
                end
            end

            // Pipeline stage 2: balanced pair sums.
            pair_valid <= product_valid;
            pair_first <= product_first;
            pair_last  <= product_last;
            pair_otr   <= product_otr;
            pair_tick  <= product_tick;
            if (product_valid) begin
                for (pair_seq = 0; pair_seq < PAIRS; pair_seq = pair_seq + 1) begin
                    if ((2 * pair_seq + 1) < LANES)
                        pair_pipe[pair_seq] <= $signed(product_pipe[2 * pair_seq])
                                             + $signed(product_pipe[2 * pair_seq + 1]);
                    else
                        pair_pipe[pair_seq] <= $signed(product_pipe[2 * pair_seq]);
                end
            end

            // Pipeline stage 3: sum the small set of registered pairs.
            group_valid <= pair_valid;
            group_first <= pair_first;
            group_last  <= pair_last;
            group_otr   <= pair_otr;
            group_tick  <= pair_tick;
            if (pair_valid)
                group_sum_pipe <= pair_sum_comb;

            // Accumulate groups belonging to one frozen sample history.
            if (group_valid) begin
                if (group_last) begin
                    m_sample    <= round_and_saturate(accumulated_group);
                    m_valid     <= 1'b1;
                    m_otr       <= group_otr;
                    m_tick      <= group_tick;
                    accumulator <= '0;
                end else begin
                    accumulator <= accumulated_group;
                end
            end
        end
    end

endmodule
