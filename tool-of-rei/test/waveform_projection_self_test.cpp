// Host-only test fixture. Keep fixture sources under tool-of-rei/test/.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>

#include "waveform_projection.hpp"

namespace {

constexpr float kPi = 3.14159265358979323846F;
constexpr float kTwoPi = 2.0F * kPi;
constexpr float kSampleRateHz = 4062500.0F;
constexpr size_t kSampleCount = 8192;
constexpr uint32_t kScaleUvPerLsb = 100;
constexpr int32_t kOffsetUv = 500;
constexpr float kMaximumObservedEstimateErrorHz = 0.14F;
constexpr float kMinimumEnvelopeCorrelation = 0.995F;
constexpr float kMaximumEnvelopeDifferenceVolts = 0.005F;

struct TestTone {
    uint16_t harmonic;
    float amplitude_volts_peak;
    float phase_radians;
};

struct TestCase {
    const char *name;
    float fundamental_hz;
    std::array<TestTone, 3> tones;
};

struct GeneratedInput {
    std::array<int16_t, kSampleCount> samples{};
    float dc_offset_volts = 0.0F;
    float peak_to_peak_volts = 0.0F;
    float true_rms_volts = 0.0F;
};

constexpr std::array<TestCase, 2> kCases = {{
    {
        "ordinary H1/H3/H4",
        40750.0F,
        {{{1, 0.025F, 0.17F},
          {3, 0.070F, 0.92F},
          {4, 0.025F, -0.51F}}},
    },
    {
        "weak H1 with H49/H50",
        10000.0F,
        {{{1, 0.005F, 0.9F},
          {49, 0.020F, -0.4F},
          {50, 0.025F, 1.7F}}},
    },
}};

constexpr std::array<float, 7> kTargetFundamentalPhases = {
    -kTwoPi + 0.0001F, -kPi, -0.0001F, 0.0F,
    0.0001F, kPi, kTwoPi - 0.0001F,
};

bool generate_input(const TestCase &test_case,
                    float target_fundamental_phase,
                    GeneratedInput *input)
{
    if (input == nullptr) {
        return false;
    }
    *input = {};
    const float global_phase =
        target_fundamental_phase - test_case.tones[0].phase_radians;
    const float volts_per_lsb =
        static_cast<float>(kScaleUvPerLsb) * 1.0e-6F;
    const float offset_volts = static_cast<float>(kOffsetUv) * 1.0e-6F;
    double sum = 0.0;
    double sum_of_squares = 0.0;
    float minimum = std::numeric_limits<float>::max();
    float maximum = std::numeric_limits<float>::lowest();
    for (size_t index = 0; index < input->samples.size(); ++index) {
        const float time_seconds = static_cast<float>(index) / kSampleRateHz;
        float voltage = 0.0F;
        for (const TestTone &tone : test_case.tones) {
            voltage += tone.amplitude_volts_peak * std::sin(
                kTwoPi * static_cast<float>(tone.harmonic)
                    * test_case.fundamental_hz * time_seconds
                + tone.phase_radians
                + static_cast<float>(tone.harmonic) * global_phase);
        }
        const long code = std::lround((voltage - offset_volts)
                                      / volts_per_lsb);
        if (code < -2048L || code > 2047L) {
            std::fprintf(stderr, "%s exceeded the 12-bit test range\n",
                         test_case.name);
            return false;
        }
        input->samples[index] = static_cast<int16_t>(code);
        const float calibrated = static_cast<float>(code) * volts_per_lsb
                                 + offset_volts;
        sum += calibrated;
        sum_of_squares += static_cast<double>(calibrated) * calibrated;
        minimum = std::min(minimum, calibrated);
        maximum = std::max(maximum, calibrated);
    }
    const double mean = sum / static_cast<double>(input->samples.size());
    const double variance =
        sum_of_squares / static_cast<double>(input->samples.size())
        - mean * mean;
    input->dc_offset_volts = static_cast<float>(mean);
    input->peak_to_peak_volts = maximum - minimum;
    input->true_rms_volts =
        static_cast<float>(std::sqrt(std::max(0.0, variance)));
    return input->peak_to_peak_volts > 0.0F
           && input->true_rms_volts > 0.0F;
}

bool project_case(const TestCase &test_case,
                  float target_fundamental_phase,
                  GeneratedInput *input,
                  cyclescope::WaveformDisplayFrame *frame)
{
    if (!generate_input(test_case, target_fundamental_phase, input)) {
        return false;
    }
    const float estimated_fundamental_hz =
        test_case.fundamental_hz + kMaximumObservedEstimateErrorHz;
    const float center_sample =
        static_cast<float>(kSampleCount - 1U) * 0.5F;
    const float phase_bias_radians =
        kTwoPi * (test_case.fundamental_hz - estimated_fundamental_hz)
        * center_sample / kSampleRateHz;
    const float estimated_phase_radians =
        target_fundamental_phase + phase_bias_radians;
    if (!cyclescope::project_waveform(
            input->samples.data(), input->samples.size(),
            kScaleUvPerLsb, kOffsetUv, input->dc_offset_volts,
            kSampleRateHz, estimated_fundamental_hz,
            estimated_phase_radians, 7,
            input->peak_to_peak_volts, input->true_rms_volts, frame)) {
        std::fprintf(stderr, "%s rejected phase %.6f\n", test_case.name,
                     static_cast<double>(target_fundamental_phase));
        return false;
    }
    if (frame->generation != 7U
        || frame->one_period.column_count
               != cyclescope::kWaveformDisplayColumns
        || frame->three_periods.column_count
               != cyclescope::kWaveformDisplayColumns
        || !frame->one_period.peak_preserved
        || !frame->three_periods.peak_preserved
        || std::fabs(
               frame->one_period.span_us * test_case.fundamental_hz
                   / 1000000.0F
               - 1.0F)
               > 0.0001F
        || std::fabs(
               frame->three_periods.span_us * test_case.fundamental_hz
                   / 1000000.0F
               - 3.0F)
               > 0.0003F
        || !(frame->vertical_range_volts > 0.0F)) {
        std::fprintf(stderr, "%s produced inconsistent metadata\n",
                     test_case.name);
        return false;
    }
    return true;
}

bool envelopes_are_consistent(const char *case_name, const char *span_name,
                              const cyclescope::WaveformEnvelope &reference,
                              const cyclescope::WaveformEnvelope &candidate)
{
    double reference_mean = 0.0;
    double candidate_mean = 0.0;
    float maximum_difference = 0.0F;
    for (size_t column = 0; column < reference.column_count; ++column) {
        const auto &left = reference.columns[column];
        const auto &right = candidate.columns[column];
        reference_mean += 0.5 * (left.minimum_volts + left.maximum_volts);
        candidate_mean += 0.5 * (right.minimum_volts + right.maximum_volts);
        maximum_difference = std::max(
            maximum_difference,
            std::max(std::fabs(left.minimum_volts - right.minimum_volts),
                     std::fabs(left.maximum_volts - right.maximum_volts)));
    }
    reference_mean /= reference.column_count;
    candidate_mean /= candidate.column_count;

    double covariance = 0.0;
    double reference_energy = 0.0;
    double candidate_energy = 0.0;
    for (size_t column = 0; column < reference.column_count; ++column) {
        const auto &left = reference.columns[column];
        const auto &right = candidate.columns[column];
        const double reference_value =
            0.5 * (left.minimum_volts + left.maximum_volts) - reference_mean;
        const double candidate_value =
            0.5 * (right.minimum_volts + right.maximum_volts) - candidate_mean;
        covariance += reference_value * candidate_value;
        reference_energy += reference_value * reference_value;
        candidate_energy += candidate_value * candidate_value;
    }
    const double denominator =
        std::sqrt(reference_energy * candidate_energy);
    const double correlation = denominator > 0.0 ? covariance / denominator : 0.0;
    if (correlation < kMinimumEnvelopeCorrelation
        || maximum_difference > kMaximumEnvelopeDifferenceVolts) {
        std::fprintf(
            stderr,
            "%s %s phase stability failed: corr=%.6f max_diff=%.3fmV\n",
            case_name, span_name, correlation,
            static_cast<double>(maximum_difference * 1000.0F));
        return false;
    }
    return true;
}

bool run_phase_stability_case(const TestCase &test_case)
{
    GeneratedInput reference_input{};
    cyclescope::WaveformDisplayFrame reference{};
    if (!project_case(test_case, 0.0F, &reference_input, &reference)) {
        return false;
    }
    for (float target_phase : kTargetFundamentalPhases) {
        GeneratedInput input{};
        cyclescope::WaveformDisplayFrame candidate{};
        if (!project_case(test_case, target_phase, &input, &candidate)
            || !envelopes_are_consistent(
                test_case.name, "1P", reference.one_period,
                candidate.one_period)
            || !envelopes_are_consistent(
                test_case.name, "3P", reference.three_periods,
                candidate.three_periods)
            || std::fabs(reference.vertical_range_volts
                         - candidate.vertical_range_volts)
                   > kMaximumEnvelopeDifferenceVolts) {
            return false;
        }
    }
    return true;
}

bool run_invalid_phase_case()
{
    const TestCase &test_case = kCases[0];
    GeneratedInput input{};
    if (!generate_input(test_case, 0.0F, &input)) {
        return false;
    }
    for (float invalid_phase : {
             std::numeric_limits<float>::quiet_NaN(),
             std::numeric_limits<float>::infinity(),
             -std::numeric_limits<float>::infinity()}) {
        cyclescope::WaveformDisplayFrame frame{};
        frame.generation = 99U;
        if (cyclescope::project_waveform(
                input.samples.data(), input.samples.size(),
                kScaleUvPerLsb, kOffsetUv, input.dc_offset_volts,
                kSampleRateHz, test_case.fundamental_hz,
                invalid_phase, 8, input.peak_to_peak_volts,
                input.true_rms_volts, &frame)
            || frame.generation != 0U) {
            std::fprintf(stderr,
                         "waveform projection accepted an invalid H1 phase\n");
            return false;
        }
    }
    return true;
}

bool run_display_aggregation_case()
{
    cyclescope::WaveformEnvelope envelope{};
    envelope.column_count =
        static_cast<uint16_t>(cyclescope::kWaveformDisplayColumns);
    for (auto &column : envelope.columns) {
        column = {-0.1F, 0.1F};
    }
    envelope.columns[317] = {-0.75F, 0.1F};
    envelope.columns.back() = {-0.1F, 0.85F};

    float minimum = 1.0F;
    float maximum = -1.0F;
    constexpr size_t kCanvasColumns = 638;
    for (size_t column = 0; column < kCanvasColumns; ++column) {
        cyclescope::WaveformEnvelopeColumn value{};
        if (!cyclescope::aggregate_waveform_column(
                envelope, column, kCanvasColumns, &value)) {
            return false;
        }
        minimum = std::min(minimum, value.minimum_volts);
        maximum = std::max(maximum, value.maximum_volts);
    }
    return minimum == -0.75F && maximum == 0.85F;
}

}  // namespace

int main()
{
    for (const TestCase &test_case : kCases) {
        if (!run_phase_stability_case(test_case)) {
            return 1;
        }
    }
    if (!run_invalid_phase_case() || !run_display_aggregation_case()) {
        return 1;
    }
    std::printf(
        "waveform projection self-test passed: centered H1 phase anchor, wrap, weak H1/H50, 1P/3P stability\n");
    return 0;
}
