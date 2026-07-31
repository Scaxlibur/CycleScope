`timescale 1ns/1ps

module test_pattern_generator (
    input  logic               clk,
    input  logic               rst_n,
    input  logic               sample_advance,
    input  logic        [1:0]  mode,
    input  logic        [11:0] amplitude,
    input  logic        [31:0] phase_increment,
    output logic signed [15:0] sample
);

    localparam logic [1:0] MODE_RAMP      = 2'd0;
    localparam logic [1:0] MODE_SINE      = 2'd1;
    localparam logic [1:0] MODE_MULTITONE = 2'd2;

    // 8192-point output-frame bins 96, 320 and 736. Since the raw input rate
    // is 16 times the output rate, a coherent-bin NCO increment is bin*32768.
    localparam logic [31:0] MULTI_INCREMENT_0 = 32'd3_145_728;
    localparam logic [31:0] MULTI_INCREMENT_1 = 32'd10_485_760;
    localparam logic [31:0] MULTI_INCREMENT_2 = 32'd24_117_248;

    logic signed [15:0] ramp_sample;
    logic [31:0] sine_phase;
    logic [31:0] multi_phase_0;
    logic [31:0] multi_phase_1;
    logic [31:0] multi_phase_2;
    logic signed [11:0] sine_raw;
    logic signed [11:0] multi_raw_0;
    logic signed [11:0] multi_raw_1;
    logic signed [11:0] multi_raw_2;
    logic signed [13:0] multi_raw;
    logic signed [12:0] amplitude_signed;
    logic signed [26:0] scaled_product;
    logic signed [12:0] selected_raw;

    function automatic logic signed [11:0] quarter_sine(input logic [5:0] index);
        begin
            case (index)
                6'd0: quarter_sine = 12'sd25;
                6'd1: quarter_sine = 12'sd75;
                6'd2: quarter_sine = 12'sd126;
                6'd3: quarter_sine = 12'sd176;
                6'd4: quarter_sine = 12'sd226;
                6'd5: quarter_sine = 12'sd275;
                6'd6: quarter_sine = 12'sd325;
                6'd7: quarter_sine = 12'sd375;
                6'd8: quarter_sine = 12'sd424;
                6'd9: quarter_sine = 12'sd473;
                6'd10: quarter_sine = 12'sd522;
                6'd11: quarter_sine = 12'sd570;
                6'd12: quarter_sine = 12'sd618;
                6'd13: quarter_sine = 12'sd666;
                6'd14: quarter_sine = 12'sd713;
                6'd15: quarter_sine = 12'sd760;
                6'd16: quarter_sine = 12'sd807;
                6'd17: quarter_sine = 12'sd852;
                6'd18: quarter_sine = 12'sd898;
                6'd19: quarter_sine = 12'sd943;
                6'd20: quarter_sine = 12'sd987;
                6'd21: quarter_sine = 12'sd1031;
                6'd22: quarter_sine = 12'sd1074;
                6'd23: quarter_sine = 12'sd1116;
                6'd24: quarter_sine = 12'sd1158;
                6'd25: quarter_sine = 12'sd1199;
                6'd26: quarter_sine = 12'sd1239;
                6'd27: quarter_sine = 12'sd1279;
                6'd28: quarter_sine = 12'sd1318;
                6'd29: quarter_sine = 12'sd1356;
                6'd30: quarter_sine = 12'sd1393;
                6'd31: quarter_sine = 12'sd1430;
                6'd32: quarter_sine = 12'sd1465;
                6'd33: quarter_sine = 12'sd1500;
                6'd34: quarter_sine = 12'sd1533;
                6'd35: quarter_sine = 12'sd1566;
                6'd36: quarter_sine = 12'sd1598;
                6'd37: quarter_sine = 12'sd1629;
                6'd38: quarter_sine = 12'sd1659;
                6'd39: quarter_sine = 12'sd1688;
                6'd40: quarter_sine = 12'sd1716;
                6'd41: quarter_sine = 12'sd1743;
                6'd42: quarter_sine = 12'sd1769;
                6'd43: quarter_sine = 12'sd1793;
                6'd44: quarter_sine = 12'sd1817;
                6'd45: quarter_sine = 12'sd1840;
                6'd46: quarter_sine = 12'sd1861;
                6'd47: quarter_sine = 12'sd1881;
                6'd48: quarter_sine = 12'sd1901;
                6'd49: quarter_sine = 12'sd1919;
                6'd50: quarter_sine = 12'sd1936;
                6'd51: quarter_sine = 12'sd1951;
                6'd52: quarter_sine = 12'sd1966;
                6'd53: quarter_sine = 12'sd1979;
                6'd54: quarter_sine = 12'sd1992;
                6'd55: quarter_sine = 12'sd2003;
                6'd56: quarter_sine = 12'sd2012;
                6'd57: quarter_sine = 12'sd2021;
                6'd58: quarter_sine = 12'sd2028;
                6'd59: quarter_sine = 12'sd2035;
                6'd60: quarter_sine = 12'sd2039;
                6'd61: quarter_sine = 12'sd2043;
                6'd62: quarter_sine = 12'sd2046;
                default: quarter_sine = 12'sd2047;
            endcase
        end
    endfunction

    function automatic logic signed [11:0] sine_lookup(input logic [31:0] phase);
        logic [5:0] index;
        logic signed [11:0] magnitude;
        begin
            index = phase[30] ? ~phase[29:24] : phase[29:24];
            magnitude = quarter_sine(index);
            sine_lookup = phase[31] ? -magnitude : magnitude;
        end
    endfunction

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            ramp_sample   <= -16'sd2048;
            sine_phase    <= '0;
            multi_phase_0 <= '0;
            multi_phase_1 <= '0;
            multi_phase_2 <= '0;
        end else if (sample_advance) begin
            ramp_sample <= (ramp_sample == 16'sd2047) ? -16'sd2048
                                                       : ramp_sample + 1'b1;
            sine_phase    <= sine_phase + phase_increment;
            multi_phase_0 <= multi_phase_0 + MULTI_INCREMENT_0;
            multi_phase_1 <= multi_phase_1 + MULTI_INCREMENT_1;
            multi_phase_2 <= multi_phase_2 + MULTI_INCREMENT_2;
        end
    end

    always_comb begin
        sine_raw = sine_lookup(sine_phase);
        multi_raw_0 = sine_lookup(multi_phase_0);
        multi_raw_1 = sine_lookup(multi_phase_1);
        multi_raw_2 = sine_lookup(multi_phase_2);
        multi_raw = ($signed(multi_raw_0) >>> 1) +
                    ($signed(multi_raw_1) >>> 2) +
                    ($signed(multi_raw_2) >>> 2);
        amplitude_signed = $signed({1'b0, amplitude});

        case (mode)
            MODE_RAMP: begin
                selected_raw = '0;
                scaled_product = '0;
                sample = ramp_sample;
            end
            MODE_SINE: begin
                selected_raw = $signed(sine_raw);
                scaled_product = selected_raw * amplitude_signed;
                sample = scaled_product >>> 11;
            end
            MODE_MULTITONE: begin
                selected_raw = multi_raw[12:0];
                scaled_product = selected_raw * amplitude_signed;
                sample = scaled_product >>> 11;
            end
            default: begin
                selected_raw = '0;
                scaled_product = '0;
                sample = '0;
            end
        endcase
    end

endmodule
