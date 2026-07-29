#include "spectrum_model.hpp"

#include <math.h>

namespace cyclescope {
namespace {

constexpr float kPi = 3.14159265358979323846F;
constexpr float kValidationToleranceVolts = 0.001F;

}  // namespace

SpectrumModel::SpectrumModel()
{
    generate_test_vector();
    run_dft();
    calculate_measurements();
    validate();
}

const std::array<SpectralLine, SpectrumModel::kLineCount> &SpectrumModel::lines() const
{
    return lines_;
}

float SpectrumModel::magnitude_at_bin(size_t bin) const
{
    return bin < magnitudes_.size() ? magnitudes_[bin] : 0.0F;
}

float SpectrumModel::voltage_peak_to_peak() const
{
    return voltage_peak_to_peak_;
}

float SpectrumModel::true_rms_volts() const
{
    return true_rms_volts_;
}

float SpectrumModel::fundamental_hz() const
{
    return fundamental_hz_;
}

float SpectrumModel::sample_rate_hz() const
{
    return kSampleRateHz;
}

float SpectrumModel::bin_width_hz() const
{
    return kSampleRateHz / static_cast<float>(kSampleCount);
}

bool SpectrumModel::validation_passed() const
{
    return validation_passed_;
}

void SpectrumModel::generate_test_vector()
{
    for (size_t sample = 0; sample < samples_.size(); ++sample) {
        const float phase = 2.0F * kPi * static_cast<float>(kFundamentalBin * sample)
                            / static_cast<float>(kSampleCount);
        samples_[sample] = kExpectedFundamentalAmplitude * sinf(phase)
                           + kExpectedSecondHarmonicAmplitude * sinf(2.0F * phase + 0.28F)
                           - kExpectedThirdHarmonicAmplitude * sinf(3.0F * phase - 0.63F);
    }
}

void SpectrumModel::run_dft()
{
    for (size_t bin = 0; bin < magnitudes_.size(); ++bin) {
        float real = 0.0F;
        float imaginary = 0.0F;
        for (size_t sample = 0; sample < samples_.size(); ++sample) {
            const float phase = 2.0F * kPi * static_cast<float>(bin * sample)
                                / static_cast<float>(kSampleCount);
            real += samples_[sample] * cosf(phase);
            imaginary -= samples_[sample] * sinf(phase);
        }

        const float magnitude = sqrtf(real * real + imaginary * imaginary)
                                / static_cast<float>(kSampleCount);
        magnitudes_[bin] = (bin == 0 || bin == magnitudes_.size() - 1) ? magnitude : 2.0F * magnitude;
    }
}

void SpectrumModel::calculate_measurements()
{
    float minimum = samples_[0];
    float maximum = samples_[0];
    float squares = 0.0F;
    for (float sample : samples_) {
        if (sample < minimum) {
            minimum = sample;
        }
        if (sample > maximum) {
            maximum = sample;
        }
        squares += sample * sample;
    }

    voltage_peak_to_peak_ = maximum - minimum;
    true_rms_volts_ = sqrtf(squares / static_cast<float>(samples_.size()));

    constexpr float kFundamentalThreshold = 0.010F;
    size_t fundamental_bin = 1;
    for (size_t bin = 1; bin < magnitudes_.size(); ++bin) {
        if (magnitudes_[bin] >= kFundamentalThreshold) {
            fundamental_bin = bin;
            break;
        }
    }
    fundamental_hz_ = static_cast<float>(fundamental_bin) * bin_width_hz();

    lines_ = {{
        {static_cast<float>(kFundamentalBin) * bin_width_hz(), magnitudes_[kFundamentalBin]},
        {static_cast<float>(kSecondHarmonicBin) * bin_width_hz(), magnitudes_[kSecondHarmonicBin]},
        {static_cast<float>(kThirdHarmonicBin) * bin_width_hz(), magnitudes_[kThirdHarmonicBin]},
    }};
}

void SpectrumModel::validate()
{
    const float expected_rms = sqrtf((kExpectedFundamentalAmplitude * kExpectedFundamentalAmplitude
                                      + kExpectedSecondHarmonicAmplitude * kExpectedSecondHarmonicAmplitude
                                      + kExpectedThirdHarmonicAmplitude * kExpectedThirdHarmonicAmplitude) / 2.0F);
    validation_passed_ = fabsf(lines_[0].amplitude_volts_peak - kExpectedFundamentalAmplitude) < kValidationToleranceVolts
                         && fabsf(lines_[1].amplitude_volts_peak - kExpectedSecondHarmonicAmplitude) < kValidationToleranceVolts
                         && fabsf(lines_[2].amplitude_volts_peak - kExpectedThirdHarmonicAmplitude) < kValidationToleranceVolts
                         && fabsf(true_rms_volts_ - expected_rms) < kValidationToleranceVolts
                         && fabsf(fundamental_hz_ - 40000.0F) < bin_width_hz();
}

}  // namespace cyclescope
