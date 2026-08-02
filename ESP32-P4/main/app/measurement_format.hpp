#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>

namespace cyclescope::measurement_format {

inline void write_placeholder(char *buffer, size_t buffer_size)
{
    if (buffer == nullptr || buffer_size == 0U) {
        return;
    }
    const int written = std::snprintf(buffer, buffer_size, "--");
    if (written < 0) {
        buffer[0] = '\0';
    }
}

inline bool copy_output(char *buffer, size_t buffer_size,
                        const char *formatted, int written)
{
    if (buffer != nullptr && buffer_size > 0U && formatted != nullptr
        && written >= 0 && static_cast<size_t>(written) < buffer_size) {
        std::memcpy(buffer, formatted, static_cast<size_t>(written) + 1U);
        return true;
    }
    write_placeholder(buffer, buffer_size);
    return false;
}

// Product voltages remain volts internally. Only the UI boundary converts to
// millivolts and fixes the presentation precision.
inline bool millivolts(char *buffer, size_t buffer_size, float volts)
{
    if (buffer == nullptr || buffer_size == 0U) {
        return false;
    }
    if (!std::isfinite(volts) || volts < 0.0F) {
        write_placeholder(buffer, buffer_size);
        return false;
    }
    const double value = static_cast<double>(volts) * 1000.0;
    char formatted[64];
    const int written =
        std::snprintf(formatted, sizeof(formatted), "%.2fmV", value);
    return copy_output(buffer, buffer_size, formatted, written);
}

// Use an explicit locale-independent thousands separator. The measurement
// band is below 500 kHz, but all uint32_t values are handled for testability.
inline bool hertz(char *buffer, size_t buffer_size, float frequency_hz)
{
    if (buffer == nullptr || buffer_size == 0U) {
        return false;
    }
    constexpr double kMaximumRoundable =
        static_cast<double>(std::numeric_limits<uint32_t>::max()) - 0.5;
    if (!std::isfinite(frequency_hz) || frequency_hz < 0.0F
        || static_cast<double>(frequency_hz) > kMaximumRoundable) {
        write_placeholder(buffer, buffer_size);
        return false;
    }

    const uint32_t rounded = static_cast<uint32_t>(
        std::floor(static_cast<double>(frequency_hz) + 0.5));
    char formatted[32];
    int written = 0;
    if (rounded >= 1000000000U) {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu,%03lu,%03lu,%03luHz",
            static_cast<unsigned long>(rounded / 1000000000U),
            static_cast<unsigned long>((rounded / 1000000U) % 1000U),
            static_cast<unsigned long>((rounded / 1000U) % 1000U),
            static_cast<unsigned long>(rounded % 1000U));
    } else if (rounded >= 1000000U) {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu,%03lu,%03luHz",
            static_cast<unsigned long>(rounded / 1000000U),
            static_cast<unsigned long>((rounded / 1000U) % 1000U),
            static_cast<unsigned long>(rounded % 1000U));
    } else if (rounded >= 1000U) {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu,%03luHz",
            static_cast<unsigned long>(rounded / 1000U),
            static_cast<unsigned long>(rounded % 1000U));
    } else {
        written = std::snprintf(
            formatted, sizeof(formatted), "%luHz",
            static_cast<unsigned long>(rounded));
    }
    return copy_output(buffer, buffer_size, formatted, written);
}

// F0 uses the same grouped Hz presentation as spectrum peaks, with two
// decimal places retained at the UI boundary. The FFT result itself is not
// rounded or otherwise modified.
inline bool hertz_hundredths(char *buffer, size_t buffer_size,
                             float frequency_hz)
{
    if (buffer == nullptr || buffer_size == 0U) {
        return false;
    }
    if (!std::isfinite(frequency_hz) || frequency_hz < 0.0F
        || static_cast<double>(frequency_hz)
               > static_cast<double>(std::numeric_limits<uint32_t>::max())) {
        write_placeholder(buffer, buffer_size);
        return false;
    }

    const uint64_t rounded_hundredths = static_cast<uint64_t>(std::floor(
        static_cast<double>(frequency_hz) * 100.0 + 0.5));
    const uint32_t whole_hertz =
        static_cast<uint32_t>(rounded_hundredths / 100U);
    const uint32_t hundredths =
        static_cast<uint32_t>(rounded_hundredths % 100U);
    char formatted[32];
    int written = 0;
    if (whole_hertz >= 1000000000U) {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu,%03lu,%03lu,%03lu.%02luHz",
            static_cast<unsigned long>(whole_hertz / 1000000000U),
            static_cast<unsigned long>((whole_hertz / 1000000U) % 1000U),
            static_cast<unsigned long>((whole_hertz / 1000U) % 1000U),
            static_cast<unsigned long>(whole_hertz % 1000U),
            static_cast<unsigned long>(hundredths));
    } else if (whole_hertz >= 1000000U) {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu,%03lu,%03lu.%02luHz",
            static_cast<unsigned long>(whole_hertz / 1000000U),
            static_cast<unsigned long>((whole_hertz / 1000U) % 1000U),
            static_cast<unsigned long>(whole_hertz % 1000U),
            static_cast<unsigned long>(hundredths));
    } else if (whole_hertz >= 1000U) {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu,%03lu.%02luHz",
            static_cast<unsigned long>(whole_hertz / 1000U),
            static_cast<unsigned long>(whole_hertz % 1000U),
            static_cast<unsigned long>(hundredths));
    } else {
        written = std::snprintf(
            formatted, sizeof(formatted), "%lu.%02luHz",
            static_cast<unsigned long>(whole_hertz),
            static_cast<unsigned long>(hundredths));
    }
    return copy_output(buffer, buffer_size, formatted, written);
}

}  // namespace cyclescope::measurement_format
