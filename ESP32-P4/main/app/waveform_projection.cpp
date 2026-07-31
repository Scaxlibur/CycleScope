#include "waveform_projection.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace cyclescope {
namespace {

constexpr float kMinimumVerticalRangeVolts = 0.01F;
constexpr float kVerticalHeadroom = 1.15F;
constexpr double kTwoPi = 6.28318530717958647692;

float calibrated_sample(const int16_t *samples, size_t index,
                        float volts_per_lsb, float offset_volts,
                        float dc_offset_volts)
{
    return static_cast<float>(samples[index]) * volts_per_lsb
           + offset_volts - dc_offset_volts;
}

float interpolate_sample(const int16_t *samples, size_t sample_count,
                         float position, float volts_per_lsb,
                         float offset_volts, float dc_offset_volts)
{
    const float maximum_position = static_cast<float>(sample_count - 1U);
    const float bounded = std::max(0.0F, std::min(position, maximum_position));
    const size_t first = static_cast<size_t>(std::floor(bounded));
    const size_t second = std::min(first + 1U, sample_count - 1U);
    const float fraction = bounded - static_cast<float>(first);
    const float first_value = calibrated_sample(samples, first, volts_per_lsb,
                                                offset_volts, dc_offset_volts);
    const float second_value = calibrated_sample(samples, second, volts_per_lsb,
                                                 offset_volts, dc_offset_volts);
    return first_value + (second_value - first_value) * fraction;
}

bool phase_anchor_sample(float fundamental_phase_radians,
                         float samples_per_period,
                         float span_samples,
                         size_t sample_count,
                         float *anchor_sample)
{
    if (anchor_sample == nullptr
        || !std::isfinite(fundamental_phase_radians)
        || !std::isfinite(samples_per_period)
        || !std::isfinite(span_samples)
        || !(samples_per_period > 0.0F) || !(span_samples > 0.0F)
        || sample_count < 2U) {
        return false;
    }

    double phase_to_next_rising =
        std::fmod(-static_cast<double>(fundamental_phase_radians), kTwoPi);
    if (phase_to_next_rising < 0.0) {
        phase_to_next_rising += kTwoPi;
    }
    const double period = static_cast<double>(samples_per_period);
    const double span = static_cast<double>(span_samples);
    const double last_sample = static_cast<double>(sample_count - 1U);
    const double base_anchor = phase_to_next_rising / kTwoPi * period;
    const double maximum_start = last_sample - span;
    if (!std::isfinite(base_anchor) || !std::isfinite(maximum_start)
        || base_anchor < 0.0 || base_anchor > maximum_start) {
        return false;
    }

    // All base_anchor + k*T positions are the same H1 rising zero. Choose the
    // one that centers the complete 3P capture in the Hann analysis frame so a
    // small frequency-estimate residual is not extrapolated from an edge.
    const double target_start = maximum_start * 0.5;
    const double maximum_periods =
        std::floor((maximum_start - base_anchor) / period);
    const double centered_periods = std::clamp(
        std::round((target_start - base_anchor) / period),
        0.0, maximum_periods);
    const double centered_anchor =
        base_anchor + centered_periods * period;
    if (!std::isfinite(centered_anchor) || centered_anchor < 0.0
        || centered_anchor + span > last_sample) {
        return false;
    }
    *anchor_sample = static_cast<float>(centered_anchor);
    return std::isfinite(*anchor_sample) && *anchor_sample >= 0.0F
           && static_cast<double>(*anchor_sample) + span <= last_sample;
}

bool build_envelope(const int16_t *samples, size_t sample_count,
                    float start_sample, float span_samples,
                    float volts_per_lsb, float offset_volts,
                    float dc_offset_volts, float sample_rate_hz,
                    WaveformEnvelope *envelope, float *minimum_volts,
                    float *maximum_volts)
{
    if (envelope == nullptr || minimum_volts == nullptr || maximum_volts == nullptr
        || !(span_samples > 0.0F)
        || start_sample + span_samples > static_cast<float>(sample_count - 1U)) {
        return false;
    }

    *envelope = {};
    envelope->span_us = span_samples * 1000000.0F / sample_rate_hz;
    envelope->column_count = static_cast<uint16_t>(kWaveformDisplayColumns);
    float envelope_minimum = std::numeric_limits<float>::max();
    float envelope_maximum = std::numeric_limits<float>::lowest();

    for (size_t column = 0; column < kWaveformDisplayColumns; ++column) {
        const float left = start_sample
                           + span_samples * static_cast<float>(column)
                                 / static_cast<float>(kWaveformDisplayColumns);
        const float right = start_sample
                            + span_samples * static_cast<float>(column + 1U)
                                  / static_cast<float>(kWaveformDisplayColumns);
        float column_minimum = interpolate_sample(samples, sample_count, left,
                                                  volts_per_lsb, offset_volts,
                                                  dc_offset_volts);
        float column_maximum = column_minimum;
        const float right_value = interpolate_sample(samples, sample_count, right,
                                                     volts_per_lsb, offset_volts,
                                                     dc_offset_volts);
        column_minimum = std::min(column_minimum, right_value);
        column_maximum = std::max(column_maximum, right_value);

        const size_t first_integer = static_cast<size_t>(std::ceil(left));
        const size_t last_integer = static_cast<size_t>(std::floor(right));
        if (first_integer <= last_integer) {
            for (size_t index = first_integer;
                 index <= last_integer && index < sample_count; ++index) {
                const float value = calibrated_sample(samples, index, volts_per_lsb,
                                                      offset_volts, dc_offset_volts);
                column_minimum = std::min(column_minimum, value);
                column_maximum = std::max(column_maximum, value);
            }
        }

        envelope->columns[column] = {column_minimum, column_maximum};
        envelope_minimum = std::min(envelope_minimum,
                                    envelope->columns[column].minimum_volts);
        envelope_maximum = std::max(envelope_maximum,
                                    envelope->columns[column].maximum_volts);
    }

    const float after_last = start_sample + span_samples;
    float source_minimum = interpolate_sample(
        samples, sample_count, start_sample, volts_per_lsb, offset_volts,
        dc_offset_volts);
    float source_maximum = source_minimum;
    const float end_value = interpolate_sample(
        samples, sample_count, after_last, volts_per_lsb, offset_volts,
        dc_offset_volts);
    source_minimum = std::min(source_minimum, end_value);
    source_maximum = std::max(source_maximum, end_value);
    const size_t first_source_index =
        static_cast<size_t>(std::ceil(start_sample));
    const size_t last_source_index =
        static_cast<size_t>(std::floor(after_last));
    for (size_t index = first_source_index;
         index <= last_source_index && index < sample_count; ++index) {
        const float value = calibrated_sample(
            samples, index, volts_per_lsb, offset_volts, dc_offset_volts);
        source_minimum = std::min(source_minimum, value);
        source_maximum = std::max(source_maximum, value);
    }

    envelope->peak_preserved =
        std::fabs(source_minimum - envelope_minimum) < 0.000001F
        && std::fabs(source_maximum - envelope_maximum) < 0.000001F;
    *minimum_volts = source_minimum;
    *maximum_volts = source_maximum;
    return true;
}

}  // namespace

bool project_waveform(const int16_t *samples, size_t sample_count,
                      uint32_t scale_uv_per_lsb, int32_t offset_uv,
                      float dc_offset_volts, float sample_rate_hz,
                      float fundamental_hz,
                      float fundamental_phase_radians,
                      uint32_t generation,
                      float voltage_peak_to_peak, float true_rms_volts,
                      WaveformDisplayFrame *frame)
{
    if (frame == nullptr) {
        return false;
    }
    *frame = {};
    if (samples == nullptr || sample_count < 2U || scale_uv_per_lsb == 0
        || !std::isfinite(dc_offset_volts)
        || !std::isfinite(sample_rate_hz) || !(sample_rate_hz > 0.0F)
        || !std::isfinite(fundamental_hz) || !(fundamental_hz > 0.0F)
        || fundamental_hz > sample_rate_hz * 0.5F
        || !std::isfinite(fundamental_phase_radians)
        || !std::isfinite(voltage_peak_to_peak)
        || !(voltage_peak_to_peak > 0.0F)
        || !std::isfinite(true_rms_volts) || !(true_rms_volts > 0.0F)) {
        return false;
    }

    const float samples_per_period = sample_rate_hz / fundamental_hz;
    const float three_period_samples = 3.0F * samples_per_period;
    if (three_period_samples + 1.0F >= static_cast<float>(sample_count)) {
        return false;
    }

    const float volts_per_lsb = static_cast<float>(scale_uv_per_lsb) * 1.0e-6F;
    const float offset_volts = static_cast<float>(offset_uv) * 1.0e-6F;
    float start_sample = 0.0F;
    if (!phase_anchor_sample(fundamental_phase_radians, samples_per_period,
                             three_period_samples, sample_count,
                             &start_sample)
        || start_sample + three_period_samples
               > static_cast<float>(sample_count - 1U)) {
        return false;
    }

    frame->generation = generation;
    frame->sample_rate_hz = sample_rate_hz;
    frame->fundamental_hz = fundamental_hz;
    frame->voltage_peak_to_peak = voltage_peak_to_peak;
    frame->true_rms_volts = true_rms_volts;
    float one_minimum = 0.0F;
    float one_maximum = 0.0F;
    float three_minimum = 0.0F;
    float three_maximum = 0.0F;
    if (!build_envelope(samples, sample_count, start_sample,
                        samples_per_period, volts_per_lsb, offset_volts,
                        dc_offset_volts, sample_rate_hz, &frame->one_period,
                        &one_minimum, &one_maximum)
        || !build_envelope(samples, sample_count, start_sample,
                           three_period_samples, volts_per_lsb, offset_volts,
                           dc_offset_volts, sample_rate_hz, &frame->three_periods,
                           &three_minimum, &three_maximum)) {
        *frame = {};
        return false;
    }

    if (!frame->one_period.peak_preserved
        || !frame->three_periods.peak_preserved
        || !std::isfinite(one_minimum) || !std::isfinite(one_maximum)
        || !std::isfinite(three_minimum) || !std::isfinite(three_maximum)
        || !(one_maximum > one_minimum)
        || !(three_maximum > three_minimum)) {
        *frame = {};
        return false;
    }

    const float maximum_magnitude = std::max(std::fabs(three_minimum),
                                             std::fabs(three_maximum));
    frame->vertical_range_volts =
        std::max(kMinimumVerticalRangeVolts, maximum_magnitude * kVerticalHeadroom);
    return true;
}

bool aggregate_waveform_column(const WaveformEnvelope &envelope,
                               size_t output_column,
                               size_t output_column_count,
                               WaveformEnvelopeColumn *result)
{
    const size_t source_column_count = envelope.column_count;
    if (result == nullptr || output_column_count == 0
        || output_column >= output_column_count
        || source_column_count == 0
        || source_column_count > envelope.columns.size()) {
        return false;
    }

    const size_t first_column =
        output_column * source_column_count / output_column_count;
    size_t end_column =
        (output_column + 1U) * source_column_count / output_column_count;
    if (end_column <= first_column) {
        end_column = first_column + 1U;
    }
    end_column = std::min(end_column, source_column_count);

    float minimum_volts = envelope.columns[first_column].minimum_volts;
    float maximum_volts = envelope.columns[first_column].maximum_volts;
    for (size_t column = first_column + 1U;
         column < end_column; ++column) {
        minimum_volts = std::min(
            minimum_volts, envelope.columns[column].minimum_volts);
        maximum_volts = std::max(
            maximum_volts, envelope.columns[column].maximum_volts);
    }
    *result = {minimum_volts, maximum_volts};
    return true;
}

}  // namespace cyclescope
