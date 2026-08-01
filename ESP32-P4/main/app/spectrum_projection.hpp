#pragma once

#include <cstddef>
#include <cstdint>

#include "spectrum_frame.hpp"

namespace cyclescope {

inline constexpr float kSpectrumDisplayMaximumHz = 500000.0F;
inline constexpr float kSpectrumDisplayMinimumAmplitudeVolts = 0.020F;
inline constexpr float kSpectrumDisplayAmplitudeHeadroom = 1.20F;
inline constexpr float kSpectrumDisplayAmplitudeUpshiftTrigger = 1.15F;
inline constexpr float kSpectrumDisplayAmplitudeDownshiftHeadroom = 1.25F;
inline constexpr float kSpectrumViewportPaddingFraction = 0.10F;
inline constexpr float kSpectrumViewportMinimumSpanHz = 20000.0F;
inline constexpr float kSpectrumViewportPeakHeightFraction = 0.80F;
inline constexpr uint32_t kSpectrumViewportVerticalDivisions = 5U;
inline constexpr int32_t kSpectrumFundamentalLineWidthPixels = 5;
inline constexpr int32_t kSpectrumHarmonicLineWidthPixels = 3;

struct SpectrumFrequencyWindow {
    float minimum_hz;
    float maximum_hz;
};

struct SpectrumCanvasPoint {
    int32_t x;
    int32_t y;
};

// Compress only the G-problem measurement band into the fixed UI columns.
// Display-axis metadata and column data are updated together so they cannot
// silently describe different frequency ranges.
bool project_spectrum_for_display(const float *positive_spectrum,
                                  size_t positive_bin_count,
                                  float bin_width_hz,
                                  SpectrumDisplayFrame *frame);

// Derive a quantized vertical scale from the semantic peaks. Zero previous
// scale explicitly marks the first frame of a stream and selects with 20%
// headroom. A continuing stream moves up only after the 15% trigger and moves
// down only when a smaller tier retains 25% headroom. Non-zero previous values
// must be one of the fixed 20/50/100/200/300/500 mV tiers.
bool choose_spectrum_amplitude_max(const SpectrumDisplayFrame &frame,
                                   float previous_amplitude_max_volts,
                                   float *amplitude_max_volts);

// Derive the UI-only vertical viewport from the currently selected leading
// semantic lines. The strongest visible line occupies exactly 80% of the
// continuous amplitude range; analysis-side quantized scale/hysteresis state
// in SpectrumDisplayFrame remains unchanged.
bool choose_spectrum_viewport_amplitude_max(
    const SpectrumDisplayFrame &frame,
    size_t visible_peak_count,
    float *amplitude_max_volts);

// Max-pool every source column assigned to one canvas column. Both peak and
// RMS envelopes are preserved when 640 source columns are drawn on 638 pixels.
bool aggregate_spectrum_column(const SpectrumDisplayFrame &frame,
                               size_t output_column,
                               size_t output_column_count,
                               SpectrumColumn *result);

// Fit the selected leading semantic lines into a frequency viewport with
// symmetric headroom. A single line receives a minimum useful span. The
// viewport is shifted, then clamped, to remain inside the projected band.
bool choose_spectrum_frequency_window(const SpectrumDisplayFrame &frame,
                                      size_t visible_peak_count,
                                      SpectrumFrequencyWindow *window);

// Pure frequency/amplitude-to-pixel mapping shared by the renderer and host
// acceptance fixture. A semantic peak within half one FFT bin of an axis edge
// is clamped to that edge; farther out-of-band peaks are rejected.
bool map_spectral_peak_to_canvas(const SpectrumDisplayFrame &frame,
                                 const SpectralPeak &peak,
                                 size_t canvas_width,
                                 size_t canvas_height,
                                 SpectrumCanvasPoint *result);

// Map against a UI-selected viewport without changing the full-band column
// metadata carried by SpectrumDisplayFrame.
bool map_spectral_peak_to_canvas(const SpectrumDisplayFrame &frame,
                                 const SpectrumFrequencyWindow &window,
                                 const SpectralPeak &peak,
                                 size_t canvas_width,
                                 size_t canvas_height,
                                 SpectrumCanvasPoint *result);

// Map against both a UI-selected frequency viewport and an explicit UI-only
// amplitude maximum without copying or mutating the full display frame.
bool map_spectral_peak_to_canvas(const SpectrumDisplayFrame &frame,
                                 const SpectrumFrequencyWindow &window,
                                 float amplitude_max_volts,
                                 const SpectralPeak &peak,
                                 size_t canvas_width,
                                 size_t canvas_height,
                                 SpectrumCanvasPoint *result);

}  // namespace cyclescope
