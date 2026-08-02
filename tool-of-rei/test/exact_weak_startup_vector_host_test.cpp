#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>

namespace {

constexpr size_t kSampleCount = 8192;
constexpr double kSampleRateHz = 4062500.0;
constexpr double kFundamentalHz = 10000.0;
constexpr double kScaleVoltsPerLsb = 100.0e-6;
constexpr double kOffsetVolts = 500.0e-6;
constexpr double kPi = 3.14159265358979323846;
constexpr std::array<uint16_t, 2> kHarmonics = {1, 2};
constexpr std::array<double, 2> kAmplitudesVolts = {
    0.0055363321799308,
    0.0221453287197232,
};
constexpr std::array<double, 2> kPhasesRadians = {0.0, -kPi / 2.0};

uint32_t raw_crc32(const int16_t *samples)
{
    const auto *bytes = reinterpret_cast<const uint8_t *>(samples);
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t index = 0; index < kSampleCount * sizeof(int16_t); ++index) {
        crc ^= bytes[index];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1U) ^ ((crc & 1U) != 0U ? 0xEDB88320U : 0U);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

}  // namespace

int main()
{
    std::array<int16_t, kSampleCount> samples{};
    int16_t minimum = 2047;
    int16_t maximum = -2048;
    int64_t sum = 0;
    for (size_t sample = 0; sample < samples.size(); ++sample) {
        const double time_seconds = static_cast<double>(sample) / kSampleRateHz;
        double voltage = 0.0;
        for (size_t line = 0; line < kHarmonics.size(); ++line) {
            const double phase =
                2.0 * kPi * kFundamentalHz
                    * static_cast<double>(kHarmonics[line]) * time_seconds
                + kPhasesRadians[line];
            voltage += kAmplitudesVolts[line] * std::sin(phase);
        }
        const long code = std::lround((voltage - kOffsetVolts) / kScaleVoltsPerLsb);
        samples[sample] = static_cast<int16_t>(
            std::max(-2048L, std::min(2047L, code)));
        minimum = std::min(minimum, samples[sample]);
        maximum = std::max(maximum, samples[sample]);
        sum += samples[sample];
    }

    const uint32_t crc = raw_crc32(samples.data());
    std::printf(
        "exact weak vector: min=%d max=%d sum=%lld crc32=0x%08X\n",
        minimum,
        maximum,
        static_cast<long long>(sum),
        crc);
    if (minimum != -228 || maximum != 272 || sum != -45607
        || crc != 0x4ECFD324U) {
        return 1;
    }
    return 0;
}
