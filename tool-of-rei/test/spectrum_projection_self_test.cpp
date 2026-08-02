// Host-only test fixture. Keep fixture sources under tool-of-rei/test/.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>

#include "spectrum_projection.hpp"

namespace {

constexpr float kSampleRateHz = 4062500.0F;
constexpr float kBinWidthHz = kSampleRateHz / 8192.0F;
constexpr size_t kPositiveBinCount = 4097;
constexpr size_t kCanvasWidth = 638;
constexpr size_t kCanvasHeight = 284;
constexpr float kAmplitudeMaximumVolts = 0.5F;

struct ExpectedLine {
    float frequency_hz;
    float amplitude_volts_peak;
    int32_t expected_x;
    int32_t expected_y;
};

bool nearly_equal(float left, float right, float tolerance = 0.000001F)
{
    return std::fabs(left - right) <= tolerance;
}

bool run_band_projection_case()
{
    std::array<float, kPositiveBinCount> bins{};
    bins[1007] = 0.3F;
    bins[1008] = 0.4F;
    bins[1009] = 0.95F;
    bins.back() = std::numeric_limits<float>::quiet_NaN();

    cyclescope::SpectrumDisplayFrame frame{};
    if (!cyclescope::project_spectrum_for_display(
            bins.data(), bins.size(), kBinWidthHz, &frame)) {
        std::fprintf(stderr, "valid 0..500 kHz spectrum was rejected\n");
        return false;
    }
    const float expected_last_rms = std::sqrt((0.3F * 0.3F + 0.4F * 0.4F) / 2.0F);
    float global_peak = 0.0F;
    for (const cyclescope::SpectrumColumn &column : frame.columns) {
        global_peak = std::max(global_peak, column.peak_volts);
    }
    if (frame.column_count != cyclescope::kSpectrumDisplayColumns
        || frame.frequency_min_hz != 0.0F
        || frame.frequency_max_hz
               != cyclescope::kSpectrumDisplayMaximumHz
        || !nearly_equal(frame.bin_width_hz, kBinWidthHz)
        || !nearly_equal(frame.columns.back().peak_volts, 0.4F)
        || !nearly_equal(frame.columns.back().rms_volts,
                         expected_last_rms)
        || !nearly_equal(global_peak, 0.4F)) {
        std::fprintf(stderr,
                     "500 kHz projection included a band-external bin or lost the edge\n");
        return false;
    }

    bins[1008] = std::numeric_limits<float>::quiet_NaN();
    if (cyclescope::project_spectrum_for_display(
            bins.data(), bins.size(), kBinWidthHz, &frame)) {
        std::fprintf(stderr, "visible NaN spectrum bin was accepted\n");
        return false;
    }
    return true;
}

bool run_canvas_aggregation_case()
{
    cyclescope::SpectrumDisplayFrame frame{};
    frame.column_count =
        static_cast<uint16_t>(cyclescope::kSpectrumDisplayColumns);
    for (cyclescope::SpectrumColumn &column : frame.columns) {
        column = {.peak_volts = 0.01F, .rms_volts = 0.005F};
    }
    frame.columns[319] = {.peak_volts = 0.73F, .rms_volts = 0.31F};
    frame.columns[639] = {.peak_volts = 0.85F, .rms_volts = 0.41F};

    float maximum_peak = 0.0F;
    float maximum_rms = 0.0F;
    bool saw_middle = false;
    bool saw_edge = false;
    for (size_t x = 0; x < kCanvasWidth; ++x) {
        cyclescope::SpectrumColumn value{};
        if (!cyclescope::aggregate_spectrum_column(
                frame, x, kCanvasWidth, &value)) {
            std::fprintf(stderr, "640-to-638 spectrum aggregation failed\n");
            return false;
        }
        maximum_peak = std::max(maximum_peak, value.peak_volts);
        maximum_rms = std::max(maximum_rms, value.rms_volts);
        saw_middle = saw_middle || value.peak_volts == 0.73F;
        saw_edge = saw_edge || value.peak_volts == 0.85F;
    }
    cyclescope::SpectrumColumn last_pixel{};
    if (!cyclescope::aggregate_spectrum_column(
            frame, kCanvasWidth - 1U, kCanvasWidth, &last_pixel)
        || !saw_middle || !saw_edge || maximum_peak != 0.85F
        || maximum_rms != 0.41F || last_pixel.peak_volts != 0.85F
        || cyclescope::aggregate_spectrum_column(
            frame, kCanvasWidth, kCanvasWidth, &last_pixel)) {
        std::fprintf(stderr,
                     "640-to-638 aggregation did not preserve skipped columns\n");
        return false;
    }
    return true;
}

bool run_dynamic_amplitude_scale_case()
{
    cyclescope::SpectrumDisplayFrame frame{};
    frame.peak_count = 3;
    frame.frequency_min_hz = 0.0F;
    frame.frequency_max_hz = cyclescope::kSpectrumDisplayMaximumHz;
    frame.bin_width_hz = kBinWidthHz;
    frame.peaks[0] = {20, 10000.0F, 0.005F, 0.0F};
    frame.peaks[1] = {201, 100000.0F, 0.050F, 0.0F};
    frame.peaks[2] = {403, 200000.0F, 0.020F, 0.0F};

    float amplitude_max_volts = 0.0F;
    if (!cyclescope::choose_spectrum_amplitude_max(
            frame, 0.0F, &amplitude_max_volts)
        || !nearly_equal(amplitude_max_volts, 0.100F)) {
        std::fprintf(stderr, "quantized spectrum amplitude scale is incorrect\n");
        return false;
    }

    frame.amplitude_max_volts = amplitude_max_volts;
    cyclescope::SpectrumCanvasPoint weak{};
    if (!cyclescope::map_spectral_peak_to_canvas(
            frame, frame.peaks[0], kCanvasWidth, kCanvasHeight, &weak)) {
        std::fprintf(stderr, "5 mVpk semantic peak did not map to canvas\n");
        return false;
    }
    const int32_t visible_pixels_above_axis =
        static_cast<int32_t>(kCanvasHeight) - 2 - weak.y;
    if (visible_pixels_above_axis < 12) {
        std::fprintf(stderr,
                     "typical 5 mVpk line remains too short: %ld pixels\n",
                     static_cast<long>(visible_pixels_above_axis));
        return false;
    }

    frame.peaks[1].amplitude_volts_peak = 0.250F;
    if (!cyclescope::choose_spectrum_amplitude_max(
            frame, 0.0F, &amplitude_max_volts)
        || !nearly_equal(amplitude_max_volts, 0.300F)) {
        std::fprintf(stderr, "250 mVpk upper scale tier is incorrect\n");
        return false;
    }
    frame.amplitude_max_volts = amplitude_max_volts;
    cyclescope::SpectrumCanvasPoint strong{};
    if (!cyclescope::map_spectral_peak_to_canvas(
            frame, frame.peaks[0], kCanvasWidth, kCanvasHeight, &weak)
        || !cyclescope::map_spectral_peak_to_canvas(
            frame, frame.peaks[1], kCanvasWidth, kCanvasHeight, &strong)
        || static_cast<int32_t>(kCanvasHeight) - 2 - weak.y < 3
        || strong.y >= weak.y) {
        std::fprintf(stderr,
                     "worst-case 5 mVpk line is not visibly proportional\n");
        return false;
    }
    const float pixel_height_ratio =
        static_cast<float>(static_cast<int32_t>(kCanvasHeight) - 1 - strong.y)
        / static_cast<float>(static_cast<int32_t>(kCanvasHeight) - 1 - weak.y);
    if (std::fabs(pixel_height_ratio - 50.0F) > 12.5F) {
        std::fprintf(stderr,
                     "semantic line heights lost their amplitude ratio: %.3f\n",
                     static_cast<double>(pixel_height_ratio));
        return false;
    }

    float lower_scale = 0.0F;
    float upper_scale = 0.0F;
    frame.peaks[1].amplitude_volts_peak = 0.049F;
    if (!cyclescope::choose_spectrum_amplitude_max(
            frame, 0.0F, &lower_scale)) {
        return false;
    }
    frame.peaks[1].amplitude_volts_peak = 0.051F;
    if (!cyclescope::choose_spectrum_amplitude_max(
            frame, 0.0F, &upper_scale)
        || lower_scale != upper_scale || lower_scale != 0.100F) {
        std::fprintf(stderr, "small amplitude drift changed the scale tier\n");
        return false;
    }

    frame.peaks[0].amplitude_volts_peak =
        std::numeric_limits<float>::quiet_NaN();
    if (cyclescope::choose_spectrum_amplitude_max(
            frame, 0.0F, &amplitude_max_volts)) {
        std::fprintf(stderr, "dynamic scale accepted a non-finite peak\n");
        return false;
    }
    return true;
}

bool run_dynamic_viewport_amplitude_case()
{
    cyclescope::SpectrumDisplayFrame frame{};
    frame.peak_count = 3U;
    frame.frequency_min_hz = 0.0F;
    frame.frequency_max_hz = cyclescope::kSpectrumDisplayMaximumHz;
    frame.bin_width_hz = kBinWidthHz;
    // Deliberately differ from the UI viewport scale to prove the explicit
    // mapping overload does not fall back to the analysis-side fixed tier.
    frame.amplitude_max_volts = kAmplitudeMaximumVolts;
    frame.peaks[0] = {20, 10000.0F, 0.020F, 0.0F};
    frame.peaks[1] = {40, 20000.0F, 0.080F, 0.0F};
    frame.peaks[2] = {60, 30000.0F, 0.120F, 0.0F};

    struct ViewportCase {
        size_t visible_peak_count;
        float expected_amplitude_max_volts;
        float expected_volts_per_division;
    };
    constexpr std::array<ViewportCase, 3> cases = {{
        {1U, 0.025F, 0.005F},
        {2U, 0.100F, 0.020F},
        {3U, 0.150F, 0.030F},
    }};
    for (const ViewportCase &test : cases) {
        float amplitude_max_volts = 0.0F;
        cyclescope::SpectrumFrequencyWindow window{};
        cyclescope::SpectrumCanvasPoint point{};
        const cyclescope::SpectralPeak &strongest =
            frame.peaks[test.visible_peak_count - 1U];
        if (!cyclescope::choose_spectrum_viewport_amplitude_max(
                frame, test.visible_peak_count, &amplitude_max_volts)
            || !nearly_equal(
                amplitude_max_volts,
                test.expected_amplitude_max_volts)
            || !nearly_equal(
                amplitude_max_volts
                    / static_cast<float>(
                        cyclescope::kSpectrumViewportVerticalDivisions),
                test.expected_volts_per_division)
            || !cyclescope::choose_spectrum_frequency_window(
                frame, test.visible_peak_count, &window)
            || !cyclescope::map_spectral_peak_to_canvas(
                frame, window, amplitude_max_volts, strongest,
                kCanvasWidth, kCanvasHeight, &point)) {
            std::fprintf(stderr,
                         "dynamic UI amplitude viewport case %zu failed\n",
                         test.visible_peak_count);
            return false;
        }

        const int32_t expected_pixel_height = static_cast<int32_t>(
            cyclescope::kSpectrumViewportPeakHeightFraction
            * static_cast<float>(kCanvasHeight - 1U));
        const int32_t actual_pixel_height =
            static_cast<int32_t>(kCanvasHeight - 1U) - point.y;
        if (actual_pixel_height != expected_pixel_height
            || !nearly_equal(
                strongest.amplitude_volts_peak / amplitude_max_volts,
                cyclescope::kSpectrumViewportPeakHeightFraction)) {
            std::fprintf(
                stderr,
                "strongest of %zu visible lines is not at 80%%: %ld px\n",
                test.visible_peak_count,
                static_cast<long>(actual_pixel_height));
            return false;
        }
    }

    const auto rejected_fail_closed = [&frame](size_t count) {
        float result = 0.321F;
        return !cyclescope::choose_spectrum_viewport_amplitude_max(
                   frame, count, &result)
               && result == 0.0F;
    };
    if (!rejected_fail_closed(0U) || !rejected_fail_closed(4U)
        || cyclescope::choose_spectrum_viewport_amplitude_max(
            frame, 1U, nullptr)) {
        std::fprintf(stderr,
                     "invalid UI amplitude viewport count/result was accepted\n");
        return false;
    }

    frame.peaks[2].amplitude_volts_peak =
        std::numeric_limits<float>::quiet_NaN();
    float selected_only = 0.0F;
    if (!cyclescope::choose_spectrum_viewport_amplitude_max(
            frame, 2U, &selected_only)
        || !nearly_equal(selected_only, 0.100F)
        || !rejected_fail_closed(3U)) {
        std::fprintf(stderr,
                     "UI amplitude viewport did not validate exactly the visible lines\n");
        return false;
    }

    frame.peaks[2].amplitude_volts_peak = 0.120F;
    frame.peaks[0].amplitude_volts_peak = 0.0F;
    if (!rejected_fail_closed(1U)) {
        return false;
    }
    frame.peaks[0].amplitude_volts_peak =
        std::numeric_limits<float>::infinity();
    if (!rejected_fail_closed(1U)) {
        return false;
    }
    frame.peaks[0].amplitude_volts_peak =
        std::numeric_limits<float>::max();
    if (!rejected_fail_closed(1U)) {
        return false;
    }
    frame.peaks[0].amplitude_volts_peak = 0.020F;
    frame.peak_count = static_cast<uint8_t>(frame.peaks.size() + 1U);
    if (!rejected_fail_closed(1U)) {
        std::fprintf(stderr,
                     "oversized UI amplitude viewport frame was accepted\n");
        return false;
    }
    return true;
}

bool run_amplitude_hysteresis_case()
{
    struct ScaleCase {
        const char *label;
        float previous_volts;
        float peak_volts;
        bool expected_success;
        float expected_volts;
    };

    constexpr float kOneHundredMillivolts = 0.100F;
    constexpr float kTwoHundredMillivolts = 0.200F;
    float upshift_boundary =
        kOneHundredMillivolts
        / cyclescope::kSpectrumDisplayAmplitudeUpshiftTrigger;
    while (upshift_boundary
               * cyclescope::kSpectrumDisplayAmplitudeUpshiftTrigger
           > kOneHundredMillivolts) {
        upshift_boundary = std::nextafter(upshift_boundary, 0.0F);
    }
    float above_upshift_boundary = std::nextafter(
        upshift_boundary, std::numeric_limits<float>::infinity());
    while (!(above_upshift_boundary
                 * cyclescope::kSpectrumDisplayAmplitudeUpshiftTrigger
             > kOneHundredMillivolts)) {
        above_upshift_boundary = std::nextafter(
            above_upshift_boundary,
            std::numeric_limits<float>::infinity());
    }

    float downshift_boundary =
        kOneHundredMillivolts
        / cyclescope::kSpectrumDisplayAmplitudeDownshiftHeadroom;
    while (downshift_boundary
               * cyclescope::kSpectrumDisplayAmplitudeDownshiftHeadroom
           > kOneHundredMillivolts) {
        downshift_boundary = std::nextafter(downshift_boundary, 0.0F);
    }
    float above_downshift_boundary = std::nextafter(
        downshift_boundary, std::numeric_limits<float>::infinity());
    while (!(above_downshift_boundary
                 * cyclescope::kSpectrumDisplayAmplitudeDownshiftHeadroom
             > kOneHundredMillivolts)) {
        above_downshift_boundary = std::nextafter(
            above_downshift_boundary,
            std::numeric_limits<float>::infinity());
    }

    const std::array<ScaleCase, 17> cases = {{
        {"first frame floor", 0.0F, 0.005F, true, 0.020F},
        {"first frame 50 mV", 0.0F, 0.040F, true, 0.050F},
        {"first frame 100 mV", 0.0F, 0.050F, true, 0.100F},
        {"first frame 200 mV", 0.0F, 0.100F, true, 0.200F},
        {"first frame 300 mV", 0.0F, 0.200F, true, 0.300F},
        {"first frame 500 mV", 0.0F, 0.300F, true, 0.500F},
        {"upshift strict boundary holds", 0.100F, upshift_boundary,
         true, 0.100F},
        {"upshift one-ULP crossing", 0.100F, above_upshift_boundary,
         true, 0.200F},
        {"downshift boundary crosses", kTwoHundredMillivolts,
         downshift_boundary, true, 0.100F},
        {"downshift one-ULP jitter holds", kTwoHundredMillivolts,
         above_downshift_boundary, true, 0.200F},
        {"multi-tier upshift", 0.020F, 0.180F, true, 0.300F},
        {"multi-tier downshift", 0.500F, 0.039F, true, 0.050F},
        {"continuing stream hysteresis", 0.300F, 0.081F, true, 0.200F},
        {"new stream reset", 0.0F, 0.081F, true, 0.100F},
        {"first frame over 500 mV tier", 0.0F, 0.417F, false, 0.0F},
        {"upshift over 500 mV tier", 0.500F, 0.440F, false, 0.0F},
        {"downshift decision over 500 mV tier", 0.500F, 0.420F,
         false, 0.0F},
    }};

    cyclescope::SpectrumDisplayFrame frame{};
    frame.peak_count = 1;
    for (const ScaleCase &test : cases) {
        frame.peaks[0].amplitude_volts_peak = test.peak_volts;
        float actual = 0.321F;
        const bool success = cyclescope::choose_spectrum_amplitude_max(
            frame, test.previous_volts, &actual);
        if (success != test.expected_success
            || !nearly_equal(actual, test.expected_volts)) {
            std::fprintf(
                stderr,
                "amplitude hysteresis case '%s' failed: success=%d scale=%.9f\n",
                test.label, success, static_cast<double>(actual));
            return false;
        }
    }

    const std::array<float, 6> invalid_previous = {{
        -0.020F,
        0.030F,
        std::nextafter(0.100F, std::numeric_limits<float>::infinity()),
        std::numeric_limits<float>::quiet_NaN(),
        std::numeric_limits<float>::infinity(),
        -std::numeric_limits<float>::infinity(),
    }};
    frame.peaks[0].amplitude_volts_peak = 0.050F;
    for (float previous : invalid_previous) {
        float actual = 0.321F;
        if (cyclescope::choose_spectrum_amplitude_max(
                frame, previous, &actual)
            || actual != 0.0F) {
            std::fprintf(stderr,
                         "invalid previous scale was not rejected fail-closed\n");
            return false;
        }
    }

    const std::array<float, 6> invalid_peaks = {{
        0.0F,
        -0.001F,
        std::numeric_limits<float>::quiet_NaN(),
        std::numeric_limits<float>::infinity(),
        -std::numeric_limits<float>::infinity(),
        std::numeric_limits<float>::max(),
    }};
    for (float peak : invalid_peaks) {
        frame.peak_count = 1;
        frame.peaks[0].amplitude_volts_peak = peak;
        float actual = 0.321F;
        if (cyclescope::choose_spectrum_amplitude_max(
                frame, 0.0F, &actual)
            || actual != 0.0F) {
            std::fprintf(stderr,
                         "invalid/unsupported peak was not rejected fail-closed\n");
            return false;
        }
    }

    frame.peak_count = 0;
    float actual = 0.321F;
    if (cyclescope::choose_spectrum_amplitude_max(frame, 0.0F, &actual)
        || actual != 0.0F) {
        std::fprintf(stderr, "empty peak frame was not rejected fail-closed\n");
        return false;
    }
    frame.peak_count = static_cast<uint8_t>(frame.peaks.size() + 1U);
    actual = 0.321F;
    if (cyclescope::choose_spectrum_amplitude_max(frame, 0.0F, &actual)
        || actual != 0.0F) {
        std::fprintf(stderr,
                     "oversized peak frame was not rejected fail-closed\n");
        return false;
    }
    frame.peak_count = 2;
    frame.peaks[0].amplitude_volts_peak = 0.050F;
    frame.peaks[1].amplitude_volts_peak =
        std::numeric_limits<float>::quiet_NaN();
    actual = 0.321F;
    if (cyclescope::choose_spectrum_amplitude_max(frame, 0.0F, &actual)
        || actual != 0.0F
        || cyclescope::choose_spectrum_amplitude_max(
            frame, 0.0F, nullptr)) {
        std::fprintf(stderr,
                     "partially invalid frame/result pointer was accepted\n");
        return false;
    }
    return true;
}

bool run_semantic_line_case()
{
    cyclescope::SpectrumDisplayFrame frame{};
    frame.frequency_min_hz = 0.0F;
    frame.frequency_max_hz = cyclescope::kSpectrumDisplayMaximumHz;
    frame.bin_width_hz = kBinWidthHz;
    frame.amplitude_max_volts = kAmplitudeMaximumVolts;

    constexpr std::array<ExpectedLine, 10> lines = {{
        {10000.0F, 0.044444F, 12, 258},
        {20000.0F, 0.022222F, 25, 271},
        {40000.0F, 0.0125F, 50, 276},
        {120000.0F, 0.075F, 152, 241},
        {200000.0F, 0.0375F, 254, 262},
        {10000.0F, 0.005536F, 12, 280},
        {20000.0F, 0.022145F, 25, 271},
        {100000.0F, 0.080F, 127, 238},
        {300000.0F, 0.030F, 382, 267},
        {500000.0F, 0.015F, 637, 275},
    }};

    for (const ExpectedLine &expected : lines) {
        const cyclescope::SpectralPeak peak{
            .bin_index = 0,
            .frequency_hz = expected.frequency_hz,
            .amplitude_volts_peak = expected.amplitude_volts_peak,
            .snr_db = 0.0F,
        };
        cyclescope::SpectrumCanvasPoint point{};
        if (!cyclescope::map_spectral_peak_to_canvas(
                frame, peak, kCanvasWidth, kCanvasHeight, &point)
            || point.x != expected.expected_x
            || point.y != expected.expected_y) {
            std::fprintf(stderr,
                         "semantic peak mapped to unexpected canvas point: %.3f Hz -> %ld,%ld\n",
                         static_cast<double>(expected.frequency_hz),
                         static_cast<long>(point.x),
                         static_cast<long>(point.y));
            return false;
        }
    }

    cyclescope::SpectrumCanvasPoint low{};
    cyclescope::SpectrumCanvasPoint high{};
    const cyclescope::SpectralPeak fundamental{
        .bin_index = 0,
        .frequency_hz = 10000.0F,
        .amplitude_volts_peak = 0.044444F,
        .snr_db = 0.0F,
    };
    const cyclescope::SpectralPeak harmonic{
        .bin_index = 0,
        .frequency_hz = 20000.0F,
        .amplitude_volts_peak = 0.022222F,
        .snr_db = 0.0F,
    };
    if (!cyclescope::map_spectral_peak_to_canvas(
            frame, fundamental, kCanvasWidth, kCanvasHeight, &low)
        || !cyclescope::map_spectral_peak_to_canvas(
            frame, harmonic, kCanvasWidth, kCanvasHeight, &high)) {
        return false;
    }
    const int32_t fundamental_last =
        low.x + cyclescope::kSpectrumFundamentalLineWidthPixels / 2;
    const int32_t harmonic_first =
        high.x - cyclescope::kSpectrumHarmonicLineWidthPixels / 2;
    if (harmonic_first <= fundamental_last + 1) {
        std::fprintf(stderr, "10/20 kHz semantic lines touch on the 500 kHz axis\n");
        return false;
    }

    const cyclescope::SpectralPeak outside{
        .bin_index = 0,
        .frequency_hz = 500300.0F,
        .amplitude_volts_peak = 0.1F,
        .snr_db = 0.0F,
    };
    if (cyclescope::map_spectral_peak_to_canvas(
            frame, outside, kCanvasWidth, kCanvasHeight, &low)) {
        std::fprintf(stderr, "out-of-band semantic peak was mapped to canvas\n");
        return false;
    }

    const cyclescope::SpectralPeak tolerated_edge{
        .bin_index = 1009,
        .frequency_hz = 500200.0F,
        .amplitude_volts_peak = 0.015F,
        .snr_db = 0.0F,
    };
    if (!cyclescope::map_spectral_peak_to_canvas(
            frame, tolerated_edge, kCanvasWidth, kCanvasHeight, &low)
        || low.x != static_cast<int32_t>(kCanvasWidth - 1U)) {
        std::fprintf(stderr,
                     "half-bin-tolerated 500 kHz estimate was not clamped to the edge\n");
        return false;
    }

    const cyclescope::SpectralPeak measured_edge{
        .bin_index = 1008,
        .frequency_hz = 499999.875F,
        .amplitude_volts_peak = 0.015F,
        .snr_db = 0.0F,
    };
    if (!cyclescope::map_spectral_peak_to_canvas(
            frame, measured_edge, kCanvasWidth, kCanvasHeight, &low)
        || low.x != 636
        || std::min<int32_t>(
               static_cast<int32_t>(kCanvasWidth - 1U),
               low.x + cyclescope::kSpectrumHarmonicLineWidthPixels / 2)
               != static_cast<int32_t>(kCanvasWidth - 1U)) {
        std::fprintf(stderr,
                     "measured 500 kHz edge line did not reach the rightmost pixel\n");
        return false;
    }
    return true;
}

bool run_frequency_window_case()
{
    cyclescope::SpectrumDisplayFrame frame{};
    frame.peak_count = 3U;
    frame.frequency_min_hz = 0.0F;
    frame.frequency_max_hz = cyclescope::kSpectrumDisplayMaximumHz;
    frame.bin_width_hz = kBinWidthHz;
    frame.amplitude_max_volts = kAmplitudeMaximumVolts;
    frame.peaks[0] = {81, 40000.0F, 0.070F, 0.0F};
    frame.peaks[1] = {242, 120000.0F, 0.039F, 0.0F};
    frame.peaks[2] = {403, 200000.0F, 0.021F, 0.0F};

    cyclescope::SpectrumFrequencyWindow window{};
    if (!cyclescope::choose_spectrum_frequency_window(frame, 1U, &window)
        || !nearly_equal(window.minimum_hz, 30000.0F)
        || !nearly_equal(window.maximum_hz, 50000.0F)
        || !cyclescope::choose_spectrum_frequency_window(frame, 2U, &window)
        || !nearly_equal(window.minimum_hz, 32000.0F)
        || !nearly_equal(window.maximum_hz, 128000.0F)
        || !cyclescope::choose_spectrum_frequency_window(frame, 3U, &window)
        || !nearly_equal(window.minimum_hz, 24000.0F)
        || !nearly_equal(window.maximum_hz, 216000.0F)) {
        std::fprintf(stderr,
                     "selected semantic lines produced the wrong frequency window\n");
        return false;
    }

    frame.peak_count = 1U;
    frame.peaks[0].frequency_hz = 10000.0F;
    if (!cyclescope::choose_spectrum_frequency_window(frame, 1U, &window)
        || !nearly_equal(window.minimum_hz, 0.0F)
        || !nearly_equal(window.maximum_hz, 20000.0F)) {
        std::fprintf(stderr, "low-edge single-line viewport did not shift\n");
        return false;
    }
    frame.peaks[0].frequency_hz = 500000.0F;
    if (!cyclescope::choose_spectrum_frequency_window(frame, 1U, &window)
        || !nearly_equal(window.minimum_hz, 480000.0F)
        || !nearly_equal(window.maximum_hz, 500000.0F)) {
        std::fprintf(stderr, "high-edge single-line viewport did not shift\n");
        return false;
    }

    frame.peak_count = 2U;
    frame.peaks[0].frequency_hz = 10000.0F;
    frame.peaks[1] = {1008, 500000.0F, 0.020F, 0.0F};
    if (!cyclescope::choose_spectrum_frequency_window(frame, 2U, &window)
        || !nearly_equal(window.minimum_hz, 0.0F)
        || !nearly_equal(window.maximum_hz, 500000.0F)) {
        std::fprintf(stderr, "full-band selected lines did not clamp\n");
        return false;
    }

    frame.peak_count = 3U;
    frame.peaks[0] = {81, 40000.0F, 0.070F, 0.0F};
    frame.peaks[1] = {242, 120000.0F, 0.039F, 0.0F};
    frame.peaks[2] = {403, 200000.0F, 0.021F, 0.0F};
    if (!cyclescope::choose_spectrum_frequency_window(frame, 2U, &window)) {
        return false;
    }
    cyclescope::SpectrumCanvasPoint point{};
    if (!cyclescope::map_spectral_peak_to_canvas(
            frame, window, frame.peaks[0], kCanvasWidth, kCanvasHeight,
            &point)
        || cyclescope::map_spectral_peak_to_canvas(
            frame, window, frame.peaks[2], kCanvasWidth, kCanvasHeight,
            &point)) {
        std::fprintf(stderr,
                     "dynamic viewport mapped an excluded semantic line\n");
        return false;
    }

    frame.peaks[0].frequency_hz =
        std::numeric_limits<float>::quiet_NaN();
    if (cyclescope::choose_spectrum_frequency_window(frame, 1U, &window)
        || cyclescope::choose_spectrum_frequency_window(frame, 0U, &window)
        || cyclescope::choose_spectrum_frequency_window(frame, 4U, &window)
        || cyclescope::choose_spectrum_frequency_window(frame, 1U, nullptr)) {
        std::fprintf(stderr, "invalid frequency-window request was accepted\n");
        return false;
    }
    return true;
}

}  // namespace

int main()
{
    if (!run_band_projection_case() || !run_canvas_aggregation_case()
        || !run_dynamic_amplitude_scale_case()
        || !run_dynamic_viewport_amplitude_case()
        || !run_amplitude_hysteresis_case()
        || !run_semantic_line_case()
        || !run_frequency_window_case()) {
        return 1;
    }
    std::printf(
        "spectrum projection self-test passed: 0..500kHz, 640->638, hysteretic analysis scale, 80%% UI height, semantic peaks, dynamic viewport\n");
    return 0;
}
