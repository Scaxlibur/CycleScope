#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace cyclescope {

constexpr size_t kWaveformDisplayColumns = 640;

struct WaveformEnvelopeColumn {
    float minimum_volts;
    float maximum_volts;
};

struct WaveformEnvelope {
    float span_us;
    uint16_t column_count;
    bool peak_preserved;
    std::array<WaveformEnvelopeColumn, kWaveformDisplayColumns> columns;
};

// Fixed-capacity, allocation-free time-domain data copied from Core 1 to the
// LVGL worker. Both period selections are prepared from the same immutable
// CSLP frame, so UI interaction never reads a receiver-owned sample buffer.
struct WaveformDisplayFrame {
    uint32_t generation;
    float sample_rate_hz;
    float fundamental_hz;
    float voltage_peak_to_peak;
    float true_rms_volts;
    float vertical_range_volts;
    WaveformEnvelope one_period;
    WaveformEnvelope three_periods;
};

}  // namespace cyclescope
