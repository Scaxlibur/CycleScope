#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "spectrum_types.hpp"

namespace cyclescope {

constexpr size_t kSpectrumDisplayColumns = 640;
constexpr size_t kMaximumSpectralPeaks = 16;

struct SpectralPeak {
    uint16_t bin_index;
    float frequency_hz;
    float amplitude_volts_peak;
    float snr_db;
};

struct SpectrumColumn {
    float peak_volts;
    float rms_volts;
};

// A fixed-capacity, allocation-free copy of everything the UI needs to draw
// one spectrum. Full FFT bins remain owned by Core 1.
struct SpectrumDisplayFrame {
    uint32_t generation;
    uint32_t sample_rate_hz;
    uint16_t fft_size;
    uint16_t column_count;
    uint8_t peak_count;
    uint8_t source_buffer_index;
    float frequency_min_hz;
    float frequency_max_hz;
    float bin_width_hz;
    float amplitude_max_volts;
    std::array<SpectralPeak, kMaximumSpectralPeaks> peaks;
    std::array<SpectrumColumn, kSpectrumDisplayColumns> columns;
};

}  // namespace cyclescope
