#include "fft_processor.hpp"
#include "waveform_projection.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <memory>
#include <vector>

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kTwoPi = 2.0 * kPi;
constexpr float kSampleRateHz = 4'062'500.0F;
constexpr uint32_t kScaleUvPerLsb = 516U;
constexpr int32_t kOffsetUv = -6761;
constexpr double kFundamentalHz = 100000.0;
constexpr std::array<uint16_t, 2> kOrders = {1U, 3U};
constexpr std::array<double, 2> kCodePeaks = {200.0, 80.0};
constexpr std::array<double, 2> kPhases = {0.2, -0.7};

constexpr std::array<cyclescope::FrequencyResponseAnchor, 4> kAnchors = {{
    {10000.0F, 260.0F},
    {100000.0F, 262.0F},
    {300000.0F, 264.0F},
    {500000.0F, 266.0F},
}};

constexpr cyclescope::FrequencyResponseProfile kProfile = {
    .profile_id = 0xA1B2C3D4U,
    .upstream = {
        .calibration_id = 25030,
        .scale_uv_per_lsb = kScaleUvPerLsb,
        .offset_uv = kOffsetUv,
        .filter_profile = 1,
        .sample_rate_hz = 4'062'500,
        .frame_sample_count = 8192,
    },
    .anchors = kAnchors.data(),
    .anchor_count = kAnchors.size(),
};

bool fail(const char *message)
{
    std::fprintf(stderr, "fft frequency-response host test: %s\n", message);
    return false;
}

double dense_peak_to_peak(
    const cyclescope::FftAnalysisResult &result)
{
    double minimum = 1.0e9;
    double maximum = -1.0e9;
    for (size_t point = 0U; point < 65536U; ++point) {
        const double time =
            static_cast<double>(point)
            / (65536.0 * static_cast<double>(result.fundamental_hz));
        double value = 0.0;
        for (size_t line = 0U;
             line < result.spectral_line_count; ++line) {
            value += static_cast<double>(
                         result.spectral_lines[line].amplitude_volts_peak)
                     * std::sin(
                         kTwoPi
                             * static_cast<double>(
                                 result.spectral_lines[line].frequency_hz)
                             * time
                         + static_cast<double>(
                             result.spectral_line_phases_radians[line]));
        }
        minimum = std::min(minimum, value);
        maximum = std::max(maximum, value);
    }
    return maximum - minimum;
}

bool upper_edge_compensation_is_stable(
    cyclescope::FftProcessor8192 *processor)
{
    if (processor == nullptr) {
        return false;
    }
    constexpr double fundamental_hz = 250000.015;
    constexpr std::array<uint16_t, 2> orders = {1U, 2U};
    constexpr std::array<double, 2> code_peaks = {40.0, 100.0};
    std::vector<int16_t> samples(
        cyclescope::FftProcessor8192::kSampleCount);
    const double offset_volts = static_cast<double>(kOffsetUv) * 1.0e-6;
    const double volts_per_lsb =
        static_cast<double>(kScaleUvPerLsb) * 1.0e-6;
    for (size_t index = 0U; index < samples.size(); ++index) {
        const double time = static_cast<double>(index) / kSampleRateHz;
        double code_signal = 0.0;
        for (size_t line = 0U; line < orders.size(); ++line) {
            code_signal += code_peaks[line]
                           * std::sin(
                               kTwoPi * fundamental_hz * orders[line]
                               * time);
        }
        const double calibrated_volts = code_signal * volts_per_lsb;
        samples[index] = static_cast<int16_t>(std::lround(
            (calibrated_volts - offset_volts) / volts_per_lsb));
    }

    cyclescope::FftAnalysisResult raw{};
    if (processor->process(
            samples.data(), samples.size(), kSampleRateHz,
            kScaleUvPerLsb, kOffsetUv, &raw)
            != ESP_OK
        || !raw.valid || raw.spectral_line_count != 2U) {
        return false;
    }
    cyclescope::FftAnalysisResult corrected{};
    if (processor->process(
            samples.data(), samples.size(), kSampleRateHz,
            kScaleUvPerLsb, kOffsetUv, &corrected, &kProfile)
            != ESP_OK
        || !corrected.valid || corrected.spectral_line_count != 2U
        || !(corrected.spectral_lines[1].frequency_hz
             > cyclescope::kResponseMaximumHz)) {
        return false;
    }
    float edge_factor = 0.0F;
    if (!cyclescope::frequency_response_correction_factor(
            kProfile, cyclescope::kResponseMaximumHz,
            kScaleUvPerLsb, &edge_factor)) {
        return false;
    }
    const float actual_factor =
        corrected.spectral_lines[1].amplitude_volts_peak
        / raw.spectral_lines[1].amplitude_volts_peak;
    return std::fabs(actual_factor - edge_factor) <= 1.0e-5F;
}

}  // namespace

int main()
{
    using cyclescope::FftAnalysisResult;
    using cyclescope::FftProcessor8192;
    auto processor = std::make_unique<FftProcessor8192>();
    if (processor->initialize() != ESP_OK) {
        return fail("FFT processor initialization failed");
    }
    std::vector<int16_t> samples(FftProcessor8192::kSampleCount);
    const double offset_volts = static_cast<double>(kOffsetUv) * 1.0e-6;
    const double volts_per_lsb =
        static_cast<double>(kScaleUvPerLsb) * 1.0e-6;
    for (size_t index = 0U; index < samples.size(); ++index) {
        const double time = static_cast<double>(index) / kSampleRateHz;
        double code_signal = 0.0;
        for (size_t line = 0U; line < kOrders.size(); ++line) {
            code_signal += kCodePeaks[line]
                           * std::sin(
                               kTwoPi * kFundamentalHz * kOrders[line]
                                   * time
                               + kPhases[line]);
        }
        // Include a 1 MHz diagnostic tone.  It must remain uncorrected in the
        // positive spectrum and must not enter the formal harmonic family.
        code_signal += 40.0 * std::sin(kTwoPi * 1'000'000.0 * time + 0.4);
        const double calibrated_volts = code_signal * volts_per_lsb;
        const long code = std::lround(
            (calibrated_volts - offset_volts) / volts_per_lsb);
        samples[index] = static_cast<int16_t>(code);
    }

    FftAnalysisResult uncorrected{};
    if (processor->process(
            samples.data(), samples.size(), kSampleRateHz,
            kScaleUvPerLsb, kOffsetUv, &uncorrected)
            != ESP_OK
        || !uncorrected.valid
        || uncorrected.frequency_response_compensated
        || uncorrected.spectral_line_count != 2U) {
        return fail("uncorrected baseline is invalid");
    }
    std::vector<float> uncorrected_spectrum(
        processor->positive_spectrum(),
        processor->positive_spectrum()
            + processor->positive_spectrum_size());

    FftAnalysisResult corrected{};
    if (processor->process(
            samples.data(), samples.size(), kSampleRateHz,
            kScaleUvPerLsb, kOffsetUv, &corrected, &kProfile)
            != ESP_OK
        || !corrected.valid
        || !corrected.frequency_response_compensated
        || corrected.p4_response_profile_id != kProfile.profile_id
        || corrected.spectral_line_count != 2U) {
        return fail("corrected result is invalid");
    }
    for (size_t line = 0U; line < 2U; ++line) {
        float factor = 0.0F;
        if (!cyclescope::frequency_response_correction_factor(
                kProfile, corrected.spectral_lines[line].frequency_hz,
                kScaleUvPerLsb, &factor)) {
            return fail("line correction factor was unavailable");
        }
        const double actual =
            corrected.spectral_lines[line].amplitude_volts_peak
            / uncorrected.spectral_lines[line].amplitude_volts_peak;
        if (std::fabs(actual - factor) > 1.0e-5
            || std::fabs(
                   corrected.spectral_line_phases_radians[line]
                   - uncorrected.spectral_line_phases_radians[line])
                   > 1.0e-7F) {
            return fail("line amplitude/phase compensation changed");
        }
    }
    double expected_rms_square = 0.0;
    for (size_t line = 0U; line < corrected.spectral_line_count; ++line) {
        const double amplitude =
            corrected.spectral_lines[line].amplitude_volts_peak;
        expected_rms_square += amplitude * amplitude * 0.5;
    }
    if (std::fabs(
            corrected.true_rms_volts
            - std::sqrt(expected_rms_square))
            > 1.0e-6
        || std::fabs(
               corrected.voltage_peak_to_peak
               - dense_peak_to_peak(corrected))
               > 0.0001) {
        return fail("corrected RMS/Vpp was not rebuilt from corrected lines");
    }
    cyclescope::WaveformDisplayFrame waveform{};
    if (!cyclescope::project_reconstructed_waveform(
            corrected.spectral_lines.data(),
            corrected.spectral_line_phases_radians.data(),
            corrected.spectral_line_count, corrected.sample_rate_hz,
            corrected.fundamental_hz, 7U,
            corrected.voltage_peak_to_peak, corrected.true_rms_volts,
            &waveform)
        || waveform.generation != 7U
        || waveform.sample_rate_hz != corrected.sample_rate_hz
        || !waveform.one_period.peak_preserved
        || !waveform.three_periods.peak_preserved) {
        return fail("corrected 1P/3P reconstruction was rejected");
    }
    float waveform_minimum = 1.0e9F;
    float waveform_maximum = -1.0e9F;
    for (size_t column = 0U;
         column < waveform.three_periods.column_count; ++column) {
        waveform_minimum = std::min(
            waveform_minimum,
            waveform.three_periods.columns[column].minimum_volts);
        waveform_maximum = std::max(
            waveform_maximum,
            waveform.three_periods.columns[column].maximum_volts);
    }
    if (std::fabs(
            (waveform_maximum - waveform_minimum)
            - corrected.voltage_peak_to_peak)
        > 0.0002F) {
        return fail("corrected 3P envelope does not represent corrected Vpp");
    }
    const size_t band_bin = static_cast<size_t>(
        std::lround(100000.0F / corrected.bin_width_hz));
    float bin_factor = 0.0F;
    if (!cyclescope::frequency_response_correction_factor(
            kProfile, static_cast<float>(band_bin) * corrected.bin_width_hz,
            kScaleUvPerLsb, &bin_factor)
        || std::fabs(
               processor->positive_spectrum()[band_bin]
                   / uncorrected_spectrum[band_bin]
               - bin_factor)
               > 1.0e-5F) {
        return fail("in-band display spectrum was not compensated");
    }
    const size_t out_of_band_bin = static_cast<size_t>(
        std::lround(1'000'000.0F / corrected.bin_width_hz));
    const float baseline = uncorrected_spectrum[out_of_band_bin];
    if (!(baseline > 0.0F)
        || std::fabs(
               processor->positive_spectrum()[out_of_band_bin]
                   / baseline
               - 1.0F)
               > 1.0e-6F) {
        return fail("1 MHz diagnostic spectrum was modified");
    }
    if (!upper_edge_compensation_is_stable(processor.get())) {
        return fail("500 kHz half-bin edge did not retain compensation");
    }
    auto wrong_profile = kProfile;
    wrong_profile.upstream.offset_uv += 1;
    FftAnalysisResult rejected{};
    if (processor->process(
            samples.data(), samples.size(), kSampleRateHz,
            kScaleUvPerLsb, kOffsetUv, &rejected, &wrong_profile)
        != ESP_ERR_INVALID_ARG) {
        return fail("profile with wrong upstream offset was accepted");
    }
    processor->deinitialize();
    if (!processor->resources_released()) {
        return fail("FFT resources did not release");
    }
    std::puts("fft_frequency_response=PASS");
    return 0;
}
