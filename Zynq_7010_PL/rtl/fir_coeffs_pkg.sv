`timescale 1ns/1ps

package fir_coeffs_pkg;

    localparam int COEFF_WIDTH = 18;
    localparam int COEFF_FRAC  = 17;

    // Fs=65 MHz, decimate by 4. Passband needed by later stages: 0..1 MHz.
    // Stopband begins at 15.25 MHz to protect the final 0..1 MHz band from aliasing.
    localparam int STAGE1_TAPS = 21;
    localparam logic signed [COEFF_WIDTH-1:0] STAGE1_COEFFS [0:STAGE1_TAPS-1] = '{
         18'sd2,    -18'sd35,   -18'sd251,  -18'sd719, -18'sd1081,
        -18'sd301,   18'sd2964,  18'sd9292,  18'sd17463, 18'sd24531,
         18'sd27342, 18'sd24531, 18'sd17463, 18'sd9292,  18'sd2964,
        -18'sd301,  -18'sd1081, -18'sd719,  -18'sd251,  -18'sd35,
         18'sd2
    };

    // Fs=16.25 MHz, decimate by 4. Stopband begins at 3.0625 MHz so tones
    // aliasing into the final 0..1 MHz band are rejected before decimation.
    localparam int STAGE2_TAPS = 31;
    localparam logic signed [COEFF_WIDTH-1:0] STAGE2_COEFFS [0:STAGE2_TAPS-1] = '{
        -18'sd5,    -18'sd31,   -18'sd60,    18'sd0,      18'sd253,
         18'sd634,   18'sd743,   18'sd0,     -18'sd1782, -18'sd3731,
        -18'sd3850,  18'sd0,     18'sd8455,  18'sd19512,  18'sd29013,
         18'sd32770, 18'sd29013, 18'sd19512, 18'sd8455,   18'sd0,
        -18'sd3850, -18'sd3731, -18'sd1782,  18'sd0,      18'sd743,
         18'sd634,   18'sd253,   18'sd0,     -18'sd60,   -18'sd31,
        -18'sd5
    };

    // Fs=4.0625 MHz. Final measurement low-pass: 0..500 kHz passband,
    // 1 MHz..Nyquist stopband.
    localparam int STAGE3_TAPS = 79;
    localparam logic signed [COEFF_WIDTH-1:0] STAGE3_COEFFS [0:STAGE3_TAPS-1] = '{
         18'sd2,      18'sd0,     -18'sd8,     -18'sd11,      18'sd5,
         18'sd30,     18'sd23,    -18'sd31,    -18'sd74,     -18'sd23,
         18'sd100,    18'sd138,   -18'sd19,    -18'sd232,    -18'sd198,
         18'sd152,    18'sd435,    18'sd196,   -18'sd431,    -18'sd681,
        -18'sd41,     18'sd896,    18'sd890,   -18'sd387,   -18'sd1556,
        -18'sd919,    18'sd1231,   18'sd2363,   18'sd538,   -18'sd2675,
        -18'sd3218,   18'sd643,    18'sd5093,   18'sd3983,  -18'sd3646,
        -18'sd10001, -18'sd4515,   18'sd15126,  18'sd38156,  18'sd48404,
         18'sd38156,  18'sd15126, -18'sd4515, -18'sd10001, -18'sd3646,
         18'sd3983,   18'sd5093,   18'sd643,   -18'sd3218,  -18'sd2675,
         18'sd538,    18'sd2363,   18'sd1231,  -18'sd919,   -18'sd1556,
        -18'sd387,    18'sd890,    18'sd896,   -18'sd41,    -18'sd681,
        -18'sd431,    18'sd196,    18'sd435,    18'sd152,   -18'sd198,
        -18'sd232,   -18'sd19,     18'sd138,    18'sd100,   -18'sd23,
        -18'sd74,    -18'sd31,     18'sd23,     18'sd30,     18'sd5,
        -18'sd11,    -18'sd8,      18'sd0,      18'sd2
    };

endpackage
