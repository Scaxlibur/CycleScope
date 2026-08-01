#include "spectrum_projection.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace cyclescope {
namespace {

constexpr std::array<float, 6> kScaleTiersVolts = {
    0.020F, 0.050F, 0.100F, 0.200F, 0.300F, 0.500F,
};

bool is_scale_tier(float value)
{
    return std::find(kScaleTiersVolts.begin(), kScaleTiersVolts.end(), value)
           != kScaleTiersVolts.end();
}

bool select_scale_tier(float required_volts, float *result)
{
    if (result == nullptr || !std::isfinite(required_volts)
        || !(required_volts > 0.0F)) {
        return false;
    }
    const auto tier = std::find_if(
        kScaleTiersVolts.begin(), kScaleTiersVolts.end(),
        [required_volts](float candidate) {
            return candidate >= required_volts;
        });
    if (tier == kScaleTiersVolts.end()) {
        return false;
    }
    *result = *tier;
    return true;
}

size_t visible_bin_count(size_t positive_bin_count, float bin_width_hz)
{
    const double maximum_hz =
        static_cast<double>(kSpectrumDisplayMaximumHz);
    const double width_hz = static_cast<double>(bin_width_hz);
    size_t count = static_cast<size_t>(std::floor(maximum_hz / width_hz)) + 1U;
    count = std::min(count, positive_bin_count);

    // Correct a possible floating-point boundary rounding without ever
    // admitting the first bin whose center lies above 500 kHz.
    while (count < positive_bin_count
           && static_cast<double>(count) * width_hz <= maximum_hz) {
        ++count;
    }
    while (count > 0U
           && static_cast<double>(count - 1U) * width_hz > maximum_hz) {
        --count;
    }
    return count;
}

}  // namespace

bool project_spectrum_for_display(const float *positive_spectrum,
                                  size_t positive_bin_count,
                                  float bin_width_hz,
                                  SpectrumDisplayFrame *frame)
{
    if (positive_spectrum == nullptr || frame == nullptr
        || positive_bin_count == 0U || !(bin_width_hz > 0.0F)
        || !std::isfinite(bin_width_hz)) {
        return false;
    }

    const double last_available_hz =
        static_cast<double>(positive_bin_count - 1U)
        * static_cast<double>(bin_width_hz);
    if (last_available_hz
        < static_cast<double>(kSpectrumDisplayMaximumHz)) {
        return false;
    }

    const size_t input_count =
        visible_bin_count(positive_bin_count, bin_width_hz);
    if (input_count < kSpectrumDisplayColumns) {
        return false;
    }
    for (size_t bin = 0; bin < input_count; ++bin) {
        if (!std::isfinite(positive_spectrum[bin])
            || positive_spectrum[bin] < 0.0F) {
            return false;
        }
    }

    frame->column_count =
        static_cast<uint16_t>(kSpectrumDisplayColumns);
    frame->frequency_min_hz = 0.0F;
    frame->frequency_max_hz = kSpectrumDisplayMaximumHz;
    frame->bin_width_hz = bin_width_hz;

    for (size_t column = 0; column < kSpectrumDisplayColumns; ++column) {
        const size_t first_bin =
            column * input_count / kSpectrumDisplayColumns;
        const size_t end_bin =
            (column + 1U) * input_count / kSpectrumDisplayColumns;

        float peak = 0.0F;
        float squares = 0.0F;
        for (size_t bin = first_bin; bin < end_bin; ++bin) {
            const float magnitude = positive_spectrum[bin];
            peak = std::max(peak, magnitude);
            squares += magnitude * magnitude;
        }
        frame->columns[column] = {
            .peak_volts = peak,
            .rms_volts = std::sqrt(
                squares / static_cast<float>(end_bin - first_bin)),
        };
    }
    return true;
}

bool choose_spectrum_amplitude_max(
    const SpectrumDisplayFrame &frame,
    float previous_amplitude_max_volts,
    float *amplitude_max_volts)
{
    if (amplitude_max_volts == nullptr) {
        return false;
    }
    *amplitude_max_volts = 0.0F;
    if (frame.peak_count == 0U || frame.peak_count > frame.peaks.size()
        || !std::isfinite(previous_amplitude_max_volts)
        || previous_amplitude_max_volts < 0.0F
        || (previous_amplitude_max_volts != 0.0F
            && !is_scale_tier(previous_amplitude_max_volts))) {
        return false;
    }

    float maximum = 0.0F;
    for (size_t peak = 0; peak < frame.peak_count; ++peak) {
        const float amplitude = frame.peaks[peak].amplitude_volts_peak;
        if (!std::isfinite(amplitude) || !(amplitude > 0.0F)) {
            return false;
        }
        maximum = std::max(maximum, amplitude);
    }
    if (!(maximum > 0.0F)) {
        return false;
    }

    if (previous_amplitude_max_volts == 0.0F) {
        return select_scale_tier(
            maximum * kSpectrumDisplayAmplitudeHeadroom,
            amplitude_max_volts);
    }

    if (maximum * kSpectrumDisplayAmplitudeUpshiftTrigger
        > previous_amplitude_max_volts) {
        return select_scale_tier(
            maximum * kSpectrumDisplayAmplitudeHeadroom,
            amplitude_max_volts);
    }

    float downshift_candidate = 0.0F;
    if (!select_scale_tier(
            maximum * kSpectrumDisplayAmplitudeDownshiftHeadroom,
            &downshift_candidate)) {
        return false;
    }
    *amplitude_max_volts =
        downshift_candidate < previous_amplitude_max_volts
            ? downshift_candidate
            : previous_amplitude_max_volts;
    return true;
}

bool aggregate_spectrum_column(const SpectrumDisplayFrame &frame,
                               size_t output_column,
                               size_t output_column_count,
                               SpectrumColumn *result)
{
    const size_t source_column_count = frame.column_count;
    if (result == nullptr || output_column_count == 0U
        || output_column >= output_column_count
        || source_column_count == 0U
        || source_column_count > frame.columns.size()) {
        return false;
    }

    const size_t first_column =
        output_column * source_column_count / output_column_count;
    size_t end_column =
        (output_column + 1U) * source_column_count / output_column_count;
    if (end_column <= first_column) {
        end_column = first_column + 1U;
    }
    end_column = std::min(end_column, source_column_count);

    float peak = frame.columns[first_column].peak_volts;
    float rms = frame.columns[first_column].rms_volts;
    for (size_t column = first_column + 1U; column < end_column; ++column) {
        peak = std::max(peak, frame.columns[column].peak_volts);
        rms = std::max(rms, frame.columns[column].rms_volts);
    }
    *result = {.peak_volts = peak, .rms_volts = rms};
    return true;
}

bool choose_spectrum_frequency_window(const SpectrumDisplayFrame &frame,
                                      size_t visible_peak_count,
                                      SpectrumFrequencyWindow *window)
{
    if (window == nullptr) {
        return false;
    }
    *window = {};
    if (visible_peak_count == 0U
        || visible_peak_count > static_cast<size_t>(frame.peak_count)
        || frame.peak_count > frame.peaks.size()
        || !std::isfinite(frame.frequency_min_hz)
        || !std::isfinite(frame.frequency_max_hz)
        || frame.frequency_max_hz <= frame.frequency_min_hz) {
        return false;
    }

    float selected_minimum_hz = frame.frequency_max_hz;
    float selected_maximum_hz = frame.frequency_min_hz;
    for (size_t index = 0; index < visible_peak_count; ++index) {
        const float frequency_hz = frame.peaks[index].frequency_hz;
        if (!std::isfinite(frequency_hz)
            || !std::isfinite(frame.bin_width_hz)
            || !(frame.bin_width_hz > 0.0F)
            || frequency_hz
                   < frame.frequency_min_hz - frame.bin_width_hz * 0.5F
            || frequency_hz
                   > frame.frequency_max_hz + frame.bin_width_hz * 0.5F) {
            return false;
        }
        const float visible_frequency_hz = std::clamp(
            frequency_hz, frame.frequency_min_hz,
            frame.frequency_max_hz);
        selected_minimum_hz =
            std::min(selected_minimum_hz, visible_frequency_hz);
        selected_maximum_hz =
            std::max(selected_maximum_hz, visible_frequency_hz);
    }

    const float full_span_hz =
        frame.frequency_max_hz - frame.frequency_min_hz;
    const float selected_span_hz =
        selected_maximum_hz - selected_minimum_hz;
    const float padded_span_hz =
        selected_span_hz * (1.0F + 2.0F * kSpectrumViewportPaddingFraction);
    const float viewport_span_hz = std::min(
        full_span_hz,
        std::max(kSpectrumViewportMinimumSpanHz, padded_span_hz));
    const float center_hz =
        (selected_minimum_hz + selected_maximum_hz) * 0.5F;
    float minimum_hz = center_hz - viewport_span_hz * 0.5F;
    float maximum_hz = minimum_hz + viewport_span_hz;

    if (minimum_hz < frame.frequency_min_hz) {
        maximum_hz += frame.frequency_min_hz - minimum_hz;
        minimum_hz = frame.frequency_min_hz;
    }
    if (maximum_hz > frame.frequency_max_hz) {
        minimum_hz -= maximum_hz - frame.frequency_max_hz;
        maximum_hz = frame.frequency_max_hz;
    }
    minimum_hz = std::max(minimum_hz, frame.frequency_min_hz);
    maximum_hz = std::min(maximum_hz, frame.frequency_max_hz);
    if (!std::isfinite(minimum_hz) || !std::isfinite(maximum_hz)
        || maximum_hz <= minimum_hz
        || selected_minimum_hz < minimum_hz
        || selected_maximum_hz > maximum_hz) {
        return false;
    }

    *window = {
        .minimum_hz = minimum_hz,
        .maximum_hz = maximum_hz,
    };
    return true;
}

bool map_spectral_peak_to_canvas(const SpectrumDisplayFrame &frame,
                                 const SpectrumFrequencyWindow &window,
                                 const SpectralPeak &peak,
                                 size_t canvas_width,
                                 size_t canvas_height,
                                 SpectrumCanvasPoint *result)
{
    if (result == nullptr || canvas_width == 0U || canvas_height == 0U
        || canvas_width
               > static_cast<size_t>(std::numeric_limits<int32_t>::max())
        || canvas_height
               > static_cast<size_t>(std::numeric_limits<int32_t>::max())
        || !std::isfinite(window.minimum_hz)
        || !std::isfinite(window.maximum_hz)
        || !std::isfinite(frame.bin_width_hz)
        || !std::isfinite(frame.amplitude_max_volts)
        || !std::isfinite(peak.frequency_hz)
        || !std::isfinite(peak.amplitude_volts_peak)
        || window.maximum_hz <= window.minimum_hz
        || !(frame.bin_width_hz > 0.0F)
        || !(frame.amplitude_max_volts > 0.0F)
        || peak.frequency_hz
               < window.minimum_hz - frame.bin_width_hz * 0.5F
        || peak.frequency_hz
               > window.maximum_hz + frame.bin_width_hz * 0.5F) {
        return false;
    }

    const double visible_frequency_hz = std::clamp(
        static_cast<double>(peak.frequency_hz),
        static_cast<double>(window.minimum_hz),
        static_cast<double>(window.maximum_hz));
    const double normalized_frequency =
        (visible_frequency_hz - static_cast<double>(window.minimum_hz))
        / (static_cast<double>(window.maximum_hz)
           - static_cast<double>(window.minimum_hz));
    const double normalized_amplitude = std::clamp(
        static_cast<double>(peak.amplitude_volts_peak)
            / static_cast<double>(frame.amplitude_max_volts),
        0.0, 1.0);
    result->x = static_cast<int32_t>(
        normalized_frequency * static_cast<double>(canvas_width - 1U));
    result->y = static_cast<int32_t>(canvas_height - 1U)
                - static_cast<int32_t>(
                    normalized_amplitude
                    * static_cast<double>(canvas_height - 1U));
    return true;
}

bool map_spectral_peak_to_canvas(const SpectrumDisplayFrame &frame,
                                 const SpectralPeak &peak,
                                 size_t canvas_width,
                                 size_t canvas_height,
                                 SpectrumCanvasPoint *result)
{
    const SpectrumFrequencyWindow window{
        .minimum_hz = frame.frequency_min_hz,
        .maximum_hz = frame.frequency_max_hz,
    };
    return map_spectral_peak_to_canvas(
        frame, window, peak, canvas_width, canvas_height, result);
}

}  // namespace cyclescope
