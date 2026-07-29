#include "spectrum_model.hpp"

namespace cyclescope {

const std::array<SpectralLine, SpectrumModel::kLineCount> &SpectrumModel::lines() const
{
    return lines_;
}

float SpectrumModel::voltage_peak_to_peak() const
{
    return 0.0F;
}

float SpectrumModel::true_rms_volts() const
{
    return 0.0F;
}

float SpectrumModel::fundamental_hz() const
{
    return 0.0F;
}

float SpectrumModel::sample_rate_hz() const
{
    return kSampleRateHz;
}

float SpectrumModel::bin_width_hz() const
{
    return kSampleRateHz / static_cast<float>(kSampleCount);
}

}  // namespace cyclescope
