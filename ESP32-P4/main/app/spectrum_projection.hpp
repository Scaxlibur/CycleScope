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
inline constexpr int32_t kSpectrumFundamentalLineWidthPixels = 5;
inline constexpr int32_t kSpectrumHarmonicLineWidthPixels = 3;

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

// Max-pool every source column assigned to one canvas column. Both peak and
// RMS envelopes are preserved when 640 source columns are drawn on 638 pixels.
bool aggregate_spectrum_column(const SpectrumDisplayFrame &frame,
                               size_t output_column,
                               size_t output_column_count,
                               SpectrumColumn *result);

// Pure frequency/amplitude-to-pixel mapping shared by the renderer and host
// acceptance fixture. A semantic peak within half one FFT bin of an axis edge
// is clamped to that edge; farther out-of-band peaks are rejected.
bool map_spectral_peak_to_canvas(const SpectrumDisplayFrame &frame,
                                 const SpectralPeak &peak,
                                 size_t canvas_width,
                                 size_t canvas_height,
                                 SpectrumCanvasPoint *result);

}  // namespace cyclescope
