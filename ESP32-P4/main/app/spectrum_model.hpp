#pragma once

#include <array>
#include <cstddef>

#include "spectrum_types.hpp"

namespace cyclescope {

// Lightweight display metadata. Actual spectral data is produced on Core 1 by
// FftProcessor8192 and overwrites these initially empty lines.
class SpectrumModel {
public:
    static constexpr size_t kSampleCount = 8192;
    static constexpr size_t kSpectrumBins = kSampleCount / 2 + 1;
    static constexpr size_t kLineCount = kMaximumSpectralLines;

    SpectrumModel() = default;

    const std::array<SpectralLine, kLineCount> &lines() const;
    float voltage_peak_to_peak() const;
    float true_rms_volts() const;
    float fundamental_hz() const;
    float sample_rate_hz() const;
    float bin_width_hz() const;

private:
    static constexpr float kSampleRateHz = 4062500.0F;

    std::array<SpectralLine, kLineCount> lines_{};
};

}  // namespace cyclescope
