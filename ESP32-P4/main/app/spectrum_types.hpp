#pragma once

#include <cstddef>
#include <cstdint>

namespace cyclescope {

inline constexpr size_t kMaximumSpectralLines = 3;
inline constexpr size_t kMaximumDisplayedSpectralLines = 8;

struct SpectralLine {
    float frequency_hz = 0.0F;
    float amplitude_volts_peak = 0.0F;
    uint16_t harmonic_order = 0;
    uint16_t reserved = 0;
};

}  // namespace cyclescope
