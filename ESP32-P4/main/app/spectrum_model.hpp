#pragma once

#include <array>
#include <cstddef>

#include "spectrum_frame.hpp"

namespace cyclescope {

// A coherent 512-point test vector.  It gives a deterministic 500 Hz FFT-bin
// spacing and a known fundamental plus two harmonics for M5 validation.
class SpectrumModel {
public:
    static constexpr size_t kSampleCount = 512;
    static constexpr size_t kSpectrumBins = kSampleCount / 2 + 1;
    static constexpr size_t kLineCount = 3;

    SpectrumModel();

    const std::array<SpectralLine, kLineCount> &lines() const;
    float magnitude_at_bin(size_t bin) const;
    float voltage_peak_to_peak() const;
    float true_rms_volts() const;
    float fundamental_hz() const;
    float sample_rate_hz() const;
    float bin_width_hz() const;
    bool validation_passed() const;

private:
    void generate_test_vector();
    void run_dft();
    void calculate_measurements();
    void validate();

    static constexpr float kSampleRateHz = 256000.0F;
    static constexpr float kExpectedFundamentalAmplitude = 0.400F;
    static constexpr float kExpectedSecondHarmonicAmplitude = 0.120F;
    static constexpr float kExpectedThirdHarmonicAmplitude = 0.060F;
    static constexpr size_t kFundamentalBin = 80;
    static constexpr size_t kSecondHarmonicBin = 160;
    static constexpr size_t kThirdHarmonicBin = 240;

    std::array<float, kSampleCount> samples_{};
    std::array<float, kSpectrumBins> magnitudes_{};
    std::array<SpectralLine, kLineCount> lines_{};
    float voltage_peak_to_peak_ = 0.0F;
    float true_rms_volts_ = 0.0F;
    float fundamental_hz_ = 0.0F;
    bool validation_passed_ = false;
};

}  // namespace cyclescope
