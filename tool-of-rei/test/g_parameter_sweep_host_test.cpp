// Deterministic host-only G-problem sweep. Fixture sources stay in
// tool-of-rei/test; the runner puts all generated artifacts under /tmp.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <memory>
#include <vector>

#include "fft_processor.hpp"
#include "spectrum_projection.hpp"
#include "waveform_projection.hpp"

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kTwoPi = 2.0 * kPi;
constexpr float kSampleRateHz = 4062500.0F;
constexpr uint32_t kScaleUvPerLsb = 100U;
constexpr int32_t kOffsetUv = 500;
constexpr double kScaleVoltsPerLsb =
    static_cast<double>(kScaleUvPerLsb) * 1.0e-6;
constexpr double kOffsetVolts = static_cast<double>(kOffsetUv) * 1.0e-6;
constexpr double kMaximumMetricErrorVolts = 0.005;
constexpr double kMaximumFrequencyErrorHz = 1000.0;
constexpr double kMaximumLineAmplitudeErrorVolts = 0.002;
constexpr double kMaximumPhaseErrorRadians = 0.12;
constexpr double kMinimumEnvelopeCorrelation = 0.995;
constexpr std::size_t kCanvasWidth = 638U;
constexpr std::size_t kCanvasHeight = 284U;

struct Tone {
    uint16_t harmonic;
    double amplitude_volts_peak;
};

struct Family {
    const char *name;
    double fundamental_hz;
    std::array<Tone, 3> tones;
    std::size_t tone_count;
    double minimum_vpp_volts;
    bool full_relative_phase_grid;
};

constexpr std::array<Family, 8> kFamilies = {{
    {"ua-near-250mVpp", 33333.25,
     {{{1, 0.060}, {3, 0.059}, {5, 0.005}}}, 3U, 0.100, true},
    {"ub-weak-H1-H49-H50", 10000.0,
     {{{1, 0.005}, {49, 0.025}, {50, 0.030}}}, 3U, 0.050, true},
    {"ub-250k-H2-edge", 250000.0,
     {{{1, 0.080}, {2, 0.020}, {0, 0.0}}}, 2U, 0.050, false},
    {"ub-125k-H4-edge", 125000.0,
     {{{1, 0.050}, {4, 0.005}, {0, 0.0}}}, 2U, 0.050, false},
    {"ua-midband-H3-H4", 40750.0,
     {{{1, 0.025}, {3, 0.070}, {4, 0.025}}}, 3U, 0.100, false},
    {"ub-fractional-H6-edge", 83333.25,
     {{{1, 0.045}, {2, 0.030}, {6, 0.010}}}, 3U, 0.050, false},
    {"ub-245k-H2-near-edge", 245000.0,
     {{{1, 0.040}, {2, 0.015}, {0, 0.0}}}, 2U, 0.050, false},
    {"ub-12k5-H16-H40", 12500.0,
     {{{1, 0.010}, {16, 0.040}, {40, 0.030}}}, 3U, 0.050, false},
}};

constexpr std::array<double, 2> kFundamentalPhases = {-2.73, 0.91};
constexpr std::array<double, 3> kFirstRelativePhases = {
    -2.40, 0.37, kPi,
};
constexpr std::array<double, 3> kSecondRelativePhases = {
    -1.61, 0.0, 2.29,
};

struct SweepStats {
    std::size_t cases = 0U;
    double maximum_frequency_error_hz = 0.0;
    double maximum_vpp_error_volts = 0.0;
    double maximum_rms_error_volts = 0.0;
    double maximum_line_error_volts = 0.0;
    double maximum_phase_error_radians = 0.0;
    double maximum_closure_gap_volts = 0.0;
    double minimum_correlation = 1.0;
    double maximum_oracle_vpp_volts = 0.0;
    bool saw_near_250_millivolts = false;
    bool saw_exact_5_millivolt_h1 = false;
    bool saw_exact_5_millivolt_harmonic = false;
    bool saw_500_kilohertz_component = false;
};

struct OracleMetrics {
    double peak_to_peak_volts = 0.0;
    double true_rms_volts = 0.0;
};

bool fail(const char *label, const char *message)
{
    std::fprintf(stderr, "%s: %s\n", label, message);
    return false;
}

double absolute_phase(const Family &family, std::size_t tone_index,
                      double fundamental_phase, double first_relative,
                      double second_relative)
{
    if (tone_index == 0U) {
        return fundamental_phase;
    }
    const double relative = tone_index == 1U ? first_relative
                                              : second_relative;
    return static_cast<double>(family.tones[tone_index].harmonic)
               * fundamental_phase
           + relative;
}

OracleMetrics calculate_oracle(const Family &family, double first_relative,
                               double second_relative)
{
    constexpr std::size_t kReconstructionPoints = 65536U;
    double minimum = std::numeric_limits<double>::max();
    double maximum = std::numeric_limits<double>::lowest();
    double rms_square = 0.0;
    for (std::size_t tone = 0U; tone < family.tone_count; ++tone) {
        const double amplitude = family.tones[tone].amplitude_volts_peak;
        rms_square += amplitude * amplitude * 0.5;
    }
    for (std::size_t point = 0U; point < kReconstructionPoints; ++point) {
        const double base_phase =
            kTwoPi * static_cast<double>(point)
            / static_cast<double>(kReconstructionPoints);
        double value = 0.0;
        for (std::size_t tone = 0U; tone < family.tone_count; ++tone) {
            const double relative = tone == 0U
                                        ? 0.0
                                        : (tone == 1U ? first_relative
                                                      : second_relative);
            value += family.tones[tone].amplitude_volts_peak
                     * std::sin(
                         static_cast<double>(family.tones[tone].harmonic)
                             * base_phase
                         + relative);
        }
        minimum = std::min(minimum, value);
        maximum = std::max(maximum, value);
    }
    return {maximum - minimum, std::sqrt(rms_square)};
}

bool generate_samples(const Family &family, double fundamental_phase,
                      double first_relative, double second_relative,
                      std::vector<int16_t> *samples)
{
    if (samples == nullptr
        || samples->size() != cyclescope::FftProcessor8192::kSampleCount) {
        return false;
    }
    for (std::size_t sample = 0U; sample < samples->size(); ++sample) {
        const double time_seconds =
            static_cast<double>(sample) / static_cast<double>(kSampleRateHz);
        double voltage = 0.0;
        for (std::size_t tone = 0U; tone < family.tone_count; ++tone) {
            const double frequency =
                family.fundamental_hz
                * static_cast<double>(family.tones[tone].harmonic);
            voltage += family.tones[tone].amplitude_volts_peak
                       * std::sin(kTwoPi * frequency * time_seconds
                                  + absolute_phase(
                                      family, tone, fundamental_phase,
                                      first_relative, second_relative));
        }
        const long code = std::lround(
            (voltage - kOffsetVolts) / kScaleVoltsPerLsb);
        if (code < -2048L || code > 2047L) {
            return false;
        }
        (*samples)[sample] = static_cast<int16_t>(code);
    }
    return true;
}

const cyclescope::SpectralLine *find_line(
    const cyclescope::FftAnalysisResult &result, uint16_t harmonic)
{
    for (std::size_t line = 0U; line < result.spectral_line_count; ++line) {
        if (result.spectral_lines[line].harmonic_order == harmonic) {
            return &result.spectral_lines[line];
        }
    }
    return nullptr;
}

double envelope_range(const cyclescope::WaveformEnvelope &envelope)
{
    double minimum = std::numeric_limits<double>::max();
    double maximum = std::numeric_limits<double>::lowest();
    for (std::size_t column = 0U; column < envelope.column_count; ++column) {
        minimum = std::min(
            minimum,
            static_cast<double>(envelope.columns[column].minimum_volts));
        maximum = std::max(
            maximum,
            static_cast<double>(envelope.columns[column].maximum_volts));
    }
    return maximum - minimum;
}

double closure_gap(const cyclescope::WaveformEnvelope &envelope)
{
    const cyclescope::WaveformEnvelopeColumn &first = envelope.columns.front();
    const cyclescope::WaveformEnvelopeColumn &last =
        envelope.columns[envelope.column_count - 1U];
    if (first.maximum_volts < last.minimum_volts) {
        return static_cast<double>(last.minimum_volts - first.maximum_volts);
    }
    if (last.maximum_volts < first.minimum_volts) {
        return static_cast<double>(first.minimum_volts - last.maximum_volts);
    }
    return 0.0;
}

bool compare_envelopes(const char *label,
                       const cyclescope::WaveformEnvelope &reference,
                       const cyclescope::WaveformEnvelope &candidate,
                       SweepStats *stats)
{
    double reference_mean = 0.0;
    double candidate_mean = 0.0;
    double maximum_difference = 0.0;
    for (std::size_t column = 0U; column < reference.column_count; ++column) {
        const auto &left = reference.columns[column];
        const auto &right = candidate.columns[column];
        reference_mean += 0.5 * static_cast<double>(
                                    left.minimum_volts + left.maximum_volts);
        candidate_mean += 0.5 * static_cast<double>(
                                    right.minimum_volts + right.maximum_volts);
        maximum_difference = std::max(
            maximum_difference,
            std::max(std::fabs(static_cast<double>(
                                   left.minimum_volts - right.minimum_volts)),
                     std::fabs(static_cast<double>(
                                   left.maximum_volts
                                   - right.maximum_volts))));
    }
    reference_mean /= static_cast<double>(reference.column_count);
    candidate_mean /= static_cast<double>(candidate.column_count);

    double covariance = 0.0;
    double reference_energy = 0.0;
    double candidate_energy = 0.0;
    for (std::size_t column = 0U; column < reference.column_count; ++column) {
        const auto &left = reference.columns[column];
        const auto &right = candidate.columns[column];
        const double left_value =
            0.5 * static_cast<double>(
                      left.minimum_volts + left.maximum_volts)
            - reference_mean;
        const double right_value =
            0.5 * static_cast<double>(
                      right.minimum_volts + right.maximum_volts)
            - candidate_mean;
        covariance += left_value * right_value;
        reference_energy += left_value * left_value;
        candidate_energy += right_value * right_value;
    }
    const double denominator =
        std::sqrt(reference_energy * candidate_energy);
    const double correlation = denominator > 0.0
                                   ? covariance / denominator
                                   : 0.0;
    stats->minimum_correlation =
        std::min(stats->minimum_correlation, correlation);
    if (correlation < kMinimumEnvelopeCorrelation
        || maximum_difference > kMaximumMetricErrorVolts) {
        std::fprintf(stderr,
                     "%s: phase-normalized envelope mismatch "
                     "corr=%.6f max_diff=%.3fmV\n",
                     label, correlation, maximum_difference * 1000.0);
        return false;
    }
    return true;
}

bool analyze_case(const Family &family, double fundamental_phase,
                  double first_relative, double second_relative,
                  cyclescope::FftProcessor8192 *processor,
                  std::vector<int16_t> *samples,
                  cyclescope::WaveformDisplayFrame *waveform,
                  SweepStats *stats)
{
    char label[192]{};
    std::snprintf(label, sizeof(label),
                  "%s phi1=%.2f relA=%.2f relB=%.2f", family.name,
                  fundamental_phase, first_relative, second_relative);
    if (!generate_samples(family, fundamental_phase, first_relative,
                          second_relative, samples)) {
        return fail(label, "sample generation exceeded the 12-bit range");
    }

    const OracleMetrics oracle =
        calculate_oracle(family, first_relative, second_relative);
    if (oracle.peak_to_peak_volts + 1.0e-9 < family.minimum_vpp_volts
        || oracle.peak_to_peak_volts > 0.250001) {
        return fail(label, "phase tuple fell outside its declared G input band");
    }
    stats->maximum_oracle_vpp_volts =
        std::max(stats->maximum_oracle_vpp_volts,
                 oracle.peak_to_peak_volts);
    stats->saw_near_250_millivolts =
        stats->saw_near_250_millivolts
        || oracle.peak_to_peak_volts >= 0.245;

    cyclescope::FftAnalysisResult result{};
    const esp_err_t error = processor->process(
        samples->data(), samples->size(), kSampleRateHz,
        kScaleUvPerLsb, kOffsetUv, &result);
    if (error != ESP_OK || !result.valid
        || result.spectral_line_count != family.tone_count
        || !(result.bin_width_hz > 0.0F)
        || result.bin_width_hz > 500.0F) {
        return fail(label, "FFT result was invalid or incomplete");
    }

    const double frequency_error =
        std::fabs(static_cast<double>(result.fundamental_hz)
                  - family.fundamental_hz);
    const double vpp_error =
        std::fabs(static_cast<double>(result.voltage_peak_to_peak)
                  - oracle.peak_to_peak_volts);
    const double rms_error =
        std::fabs(static_cast<double>(result.true_rms_volts)
                  - oracle.true_rms_volts);
    const double phase_error = std::fabs(std::remainder(
        static_cast<double>(result.fundamental_phase_radians)
            - fundamental_phase,
        kTwoPi));
    stats->maximum_frequency_error_hz =
        std::max(stats->maximum_frequency_error_hz, frequency_error);
    stats->maximum_vpp_error_volts =
        std::max(stats->maximum_vpp_error_volts, vpp_error);
    stats->maximum_rms_error_volts =
        std::max(stats->maximum_rms_error_volts, rms_error);
    stats->maximum_phase_error_radians =
        std::max(stats->maximum_phase_error_radians, phase_error);
    if (frequency_error > kMaximumFrequencyErrorHz
        || vpp_error > kMaximumMetricErrorVolts
        || rms_error > kMaximumMetricErrorVolts
        || phase_error > kMaximumPhaseErrorRadians) {
        std::fprintf(stderr,
                     "%s: metric mismatch dF=%.3fHz dVpp=%.3fmV "
                     "dRMS=%.3fmV dPhase=%.4frad\n",
                     label, frequency_error, vpp_error * 1000.0,
                     rms_error * 1000.0, phase_error);
        return false;
    }

    auto spectrum = std::make_unique<cyclescope::SpectrumDisplayFrame>();
    spectrum->generation = 91U;
    spectrum->sample_rate_hz = static_cast<uint32_t>(kSampleRateHz);
    spectrum->fft_size =
        static_cast<uint16_t>(cyclescope::FftProcessor8192::kSampleCount);
    spectrum->peak_count = static_cast<uint8_t>(result.spectral_line_count);
    for (std::size_t tone = 0U; tone < family.tone_count; ++tone) {
        const Tone &expected = family.tones[tone];
        const cyclescope::SpectralLine *line =
            find_line(result, expected.harmonic);
        if (line == nullptr || !(line->frequency_hz > 0.0F)
            || !(line->amplitude_volts_peak > 0.0F)) {
            return fail(label, "an expected positive semantic line was absent");
        }
        const double expected_frequency =
            family.fundamental_hz
            * static_cast<double>(expected.harmonic);
        const double line_frequency_error =
            std::fabs(static_cast<double>(line->frequency_hz)
                      - expected_frequency);
        const double line_amplitude_error =
            std::fabs(static_cast<double>(line->amplitude_volts_peak)
                      - expected.amplitude_volts_peak);
        stats->maximum_line_error_volts =
            std::max(stats->maximum_line_error_volts,
                     line_amplitude_error);
        if (line_frequency_error > kMaximumFrequencyErrorHz
            || line_amplitude_error > kMaximumLineAmplitudeErrorVolts) {
            return fail(label, "semantic line frequency/amplitude mismatch");
        }
        const long rounded_bin = std::lround(
            static_cast<double>(line->frequency_hz)
            / static_cast<double>(result.bin_width_hz));
        spectrum->peaks[tone] = {
            .bin_index = static_cast<uint16_t>(std::clamp(
                rounded_bin, 0L,
                static_cast<long>(
                    cyclescope::FftProcessor8192::kPositiveBinCount - 1U))),
            .frequency_hz = line->frequency_hz,
            .amplitude_volts_peak = line->amplitude_volts_peak,
            .snr_db = 0.0F,
        };
        stats->saw_exact_5_millivolt_h1 =
            stats->saw_exact_5_millivolt_h1
            || (expected.harmonic == 1U
                && expected.amplitude_volts_peak == 0.005);
        stats->saw_exact_5_millivolt_harmonic =
            stats->saw_exact_5_millivolt_harmonic
            || (expected.harmonic > 1U
                && expected.amplitude_volts_peak == 0.005);
        stats->saw_500_kilohertz_component =
            stats->saw_500_kilohertz_component
            || std::fabs(expected_frequency - 500000.0) <= 1.0;
    }

    if (!cyclescope::project_spectrum_for_display(
            processor->positive_spectrum(),
            processor->positive_spectrum_size(), result.bin_width_hz,
            spectrum.get())
        || !cyclescope::choose_spectrum_amplitude_max(
            *spectrum, 0.0F, &spectrum->amplitude_max_volts)
        || spectrum->column_count != cyclescope::kSpectrumDisplayColumns
        || spectrum->frequency_min_hz != 0.0F
        || spectrum->frequency_max_hz
               != cyclescope::kSpectrumDisplayMaximumHz) {
        return fail(label, "spectrum display projection failed");
    }
    for (std::size_t peak = 0U; peak < spectrum->peak_count; ++peak) {
        cyclescope::SpectrumCanvasPoint point{};
        if (!cyclescope::map_spectral_peak_to_canvas(
                *spectrum, spectrum->peaks[peak], kCanvasWidth,
                kCanvasHeight, &point)
            || point.x < 0
            || point.x >= static_cast<int32_t>(kCanvasWidth)
            || point.y < 0
            || point.y >= static_cast<int32_t>(kCanvasHeight - 1U)) {
            return fail(label, "semantic spectrum line was not visibly mapped");
        }
    }

    if (!cyclescope::project_waveform(
            samples->data(), samples->size(), kScaleUvPerLsb, kOffsetUv,
            result.dc_offset_volts, result.sample_rate_hz,
            result.fundamental_hz, result.fundamental_phase_radians, 91U,
            result.voltage_peak_to_peak, result.true_rms_volts, waveform)
        || waveform->generation != 91U
        || waveform->one_period.column_count
               != cyclescope::kWaveformDisplayColumns
        || waveform->three_periods.column_count
               != cyclescope::kWaveformDisplayColumns
        || !waveform->one_period.peak_preserved
        || !waveform->three_periods.peak_preserved) {
        return fail(label, "1P/3P waveform projection failed");
    }
    const double one_cycles =
        static_cast<double>(waveform->one_period.span_us)
        * static_cast<double>(result.fundamental_hz) / 1.0e6;
    const double three_cycles =
        static_cast<double>(waveform->three_periods.span_us)
        * static_cast<double>(result.fundamental_hz) / 1.0e6;
    const double one_range_error =
        std::fabs(envelope_range(waveform->one_period)
                  - static_cast<double>(result.voltage_peak_to_peak));
    const double three_range_error =
        std::fabs(envelope_range(waveform->three_periods)
                  - static_cast<double>(result.voltage_peak_to_peak));
    const double one_closure = closure_gap(waveform->one_period);
    const double three_closure = closure_gap(waveform->three_periods);
    stats->maximum_closure_gap_volts = std::max(
        stats->maximum_closure_gap_volts,
        std::max(one_closure, three_closure));
    if (std::fabs(one_cycles - 1.0) > 0.0002
        || std::fabs(three_cycles - 3.0) > 0.0005
        || one_range_error > kMaximumMetricErrorVolts
        || three_range_error > kMaximumMetricErrorVolts
        || one_closure > kMaximumMetricErrorVolts
        || three_closure > kMaximumMetricErrorVolts) {
        return fail(label, "1P/3P span, extrema, or endpoint closure mismatch");
    }

    ++stats->cases;
    return true;
}

bool run_relative_tuple(const Family &family, double first_relative,
                        double second_relative,
                        cyclescope::FftProcessor8192 *processor,
                        std::vector<int16_t> *samples, SweepStats *stats)
{
    auto reference =
        std::unique_ptr<cyclescope::WaveformDisplayFrame>();
    for (double fundamental_phase : kFundamentalPhases) {
        auto candidate =
            std::make_unique<cyclescope::WaveformDisplayFrame>();
        if (!analyze_case(family, fundamental_phase, first_relative,
                          second_relative, processor, samples,
                          candidate.get(), stats)) {
            return false;
        }
        if (reference == nullptr) {
            reference = std::make_unique<cyclescope::WaveformDisplayFrame>(
                *candidate);
            continue;
        }
        char label[160]{};
        std::snprintf(label, sizeof(label), "%s relA=%.2f relB=%.2f",
                      family.name, first_relative, second_relative);
        if (!compare_envelopes(label, reference->one_period,
                               candidate->one_period, stats)
            || !compare_envelopes(label, reference->three_periods,
                                  candidate->three_periods, stats)) {
            return false;
        }
    }
    return true;
}

bool run_family(const Family &family,
                cyclescope::FftProcessor8192 *processor,
                std::vector<int16_t> *samples, SweepStats *stats)
{
    if (family.tone_count == 2U) {
        for (double first_relative : kFirstRelativePhases) {
            if (!run_relative_tuple(family, first_relative, 0.0, processor,
                                    samples, stats)) {
                return false;
            }
        }
        return true;
    }
    if (family.full_relative_phase_grid) {
        for (double first_relative : kFirstRelativePhases) {
            for (double second_relative : kSecondRelativePhases) {
                if (!run_relative_tuple(family, first_relative,
                                        second_relative, processor, samples,
                                        stats)) {
                    return false;
                }
            }
        }
        return true;
    }
    return run_relative_tuple(family, kFirstRelativePhases[0],
                              kSecondRelativePhases[1], processor, samples,
                              stats)
           && run_relative_tuple(family, kFirstRelativePhases[2],
                                 kSecondRelativePhases[2], processor, samples,
                                 stats);
}

bool run_eight_line_display_case(
    cyclescope::FftProcessor8192 *processor,
    std::vector<int16_t> *samples)
{
    constexpr double kFundamentalHz = 20000.0;
    constexpr std::array<double, 9> kAmplitudes = {{
        0.020, 0.018, 0.016, 0.014, 0.012,
        0.010, 0.008, 0.006, 0.004,
    }};
    for (std::size_t sample = 0U; sample < samples->size(); ++sample) {
        const double time_seconds =
            static_cast<double>(sample) / kSampleRateHz;
        double voltage = 0.0;
        for (std::size_t tone = 0U; tone < kAmplitudes.size(); ++tone) {
            const double harmonic = static_cast<double>(tone + 1U);
            voltage += kAmplitudes[tone]
                       * std::sin(kTwoPi * kFundamentalHz * harmonic
                                      * time_seconds
                                  + 0.11 * harmonic);
        }
        const long code = std::lround(
            (voltage - kOffsetVolts) / kScaleVoltsPerLsb);
        if (code < -2048L || code > 2047L) {
            return fail("eight-line-display", "sample range overflow");
        }
        (*samples)[sample] = static_cast<int16_t>(code);
    }

    cyclescope::FftAnalysisResult result{};
    if (processor->process(
            samples->data(), samples->size(), kSampleRateHz,
            kScaleUvPerLsb, kOffsetUv, &result)
            != ESP_OK
        || !result.valid
        || result.spectral_line_count != cyclescope::kMaximumSpectralLines
        || result.displayed_spectral_line_count
               != cyclescope::kMaximumDisplayedSpectralLines) {
        return fail("eight-line-display",
                    "formal/display line counts were not isolated");
    }

    double expected_formal_rms_square = 0.0;
    for (std::size_t line = 0U;
         line < cyclescope::kMaximumSpectralLines; ++line) {
        expected_formal_rms_square +=
            kAmplitudes[line] * kAmplitudes[line] * 0.5;
        if (result.spectral_lines[line].harmonic_order != line + 1U) {
            return fail("eight-line-display",
                        "formal strongest-three family changed");
        }
    }
    if (std::fabs(
            static_cast<double>(result.true_rms_volts)
            - std::sqrt(expected_formal_rms_square))
        > 0.001) {
        return fail("eight-line-display",
                    "display-only lines changed formal RMS");
    }

    for (std::size_t line = 0U;
         line < cyclescope::kMaximumDisplayedSpectralLines; ++line) {
        const cyclescope::SpectralLine &actual =
            result.displayed_spectral_lines[line];
        const uint16_t expected_harmonic =
            static_cast<uint16_t>(line + 1U);
        if (actual.harmonic_order != expected_harmonic
            || std::fabs(
                   static_cast<double>(actual.frequency_hz)
                   - kFundamentalHz
                         * static_cast<double>(expected_harmonic))
                   > kMaximumFrequencyErrorHz
            || std::fabs(
                   static_cast<double>(actual.amplitude_volts_peak)
                   - kAmplitudes[line])
                   > kMaximumLineAmplitudeErrorVolts) {
            return fail("eight-line-display",
                        "display harmonic family mismatch");
        }
    }
    return true;
}

}  // namespace

int main()
{
    auto processor = std::make_unique<cyclescope::FftProcessor8192>();
    if (processor->initialize() != ESP_OK || !processor->initialized()) {
        std::fprintf(stderr, "host FFT processor initialization failed\n");
        return 1;
    }
    auto samples = std::make_unique<std::vector<int16_t>>(
        cyclescope::FftProcessor8192::kSampleCount);
    SweepStats stats{};
    for (const Family &family : kFamilies) {
        if (!run_family(family, processor.get(), samples.get(), &stats)) {
            return 1;
        }
    }
    if (!run_eight_line_display_case(processor.get(), samples.get())) {
        return 1;
    }
    processor->deinitialize();
    if (!processor->resources_released()
        || !stats.saw_near_250_millivolts
        || !stats.saw_exact_5_millivolt_h1
        || !stats.saw_exact_5_millivolt_harmonic
        || !stats.saw_500_kilohertz_component) {
        std::fprintf(stderr, "sweep coverage/lifecycle sentinel failed\n");
        return 1;
    }

    std::printf(
        "G parameter sweep passed: families=%zu cases=%zu "
        "max[dF=%.3fHz dVpp=%.3fmV dRMS=%.3fmV dLine=%.3fmV "
        "dPhase=%.4frad closure=%.3fmV] minCorr=%.6f "
        "maxVpp=%.3fmV\n",
        kFamilies.size(), stats.cases, stats.maximum_frequency_error_hz,
        stats.maximum_vpp_error_volts * 1000.0,
        stats.maximum_rms_error_volts * 1000.0,
        stats.maximum_line_error_volts * 1000.0,
        stats.maximum_phase_error_radians,
        stats.maximum_closure_gap_volts * 1000.0,
        stats.minimum_correlation,
        stats.maximum_oracle_vpp_volts * 1000.0);
    return 0;
}
