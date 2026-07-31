#pragma once

#include <cstddef>
#include <cstdint>

#include "waveform_frame.hpp"

namespace cyclescope {

// Project one immutable, calibrated S16 frame into 1-period and 3-period
// min/max envelopes. This contains no ESP-IDF or LVGL dependencies so period
// and peak-preservation behavior can be tested on the host.
bool project_waveform(const int16_t *samples, size_t sample_count,
                      uint32_t scale_uv_per_lsb, int32_t offset_uv,
                      float dc_offset_volts, float sample_rate_hz,
                      float fundamental_hz,
                      float fundamental_phase_radians,
                      uint32_t generation,
                      float voltage_peak_to_peak, float true_rms_volts,
                      WaveformDisplayFrame *frame);

// Aggregate every source envelope column assigned to one display column.
// This preserves extrema when, for example, 640 projected columns are rendered
// on the 638-pixel waveform canvas.
bool aggregate_waveform_column(const WaveformEnvelope &envelope,
                               size_t output_column, size_t output_column_count,
                               WaveformEnvelopeColumn *result);

}  // namespace cyclescope
