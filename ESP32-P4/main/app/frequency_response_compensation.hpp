#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>

namespace cyclescope {

struct UpstreamCalibrationIdentity {
    uint16_t calibration_id = 0;
    uint32_t scale_uv_per_lsb = 0;
    int32_t offset_uv = 0;
    uint16_t filter_profile = 0;
    uint32_t sample_rate_hz = 0;
    uint32_t frame_sample_count = 0;
};

struct FrequencyResponseAnchor {
    float frequency_hz = 0.0F;
    float input_uv_per_code = 0.0F;
};

struct FrequencyResponseProfile {
    uint32_t profile_id = 0;
    UpstreamCalibrationIdentity upstream{};
    const FrequencyResponseAnchor *anchors = nullptr;
    size_t anchor_count = 0;
};

inline constexpr float kResponseMinimumHz = 10000.0F;
inline constexpr float kResponseMaximumHz = 500000.0F;

inline bool frequency_response_profile_valid(
    const FrequencyResponseProfile &profile)
{
    if (profile.profile_id == 0U || profile.anchors == nullptr
        || profile.anchor_count < 2U
        || profile.upstream.calibration_id == 0U
        || profile.upstream.scale_uv_per_lsb == 0U
        || profile.upstream.filter_profile == 0U
        || profile.upstream.sample_rate_hz == 0U
        || profile.upstream.frame_sample_count == 0U) {
        return false;
    }
    for (size_t index = 0; index < profile.anchor_count; ++index) {
        const FrequencyResponseAnchor &anchor = profile.anchors[index];
        if (!std::isfinite(anchor.frequency_hz)
            || !std::isfinite(anchor.input_uv_per_code)
            || !(anchor.frequency_hz > 0.0F)
            || !(anchor.input_uv_per_code > 0.0F)
            || (index > 0U
                && !(anchor.frequency_hz
                     > profile.anchors[index - 1U].frequency_hz))) {
            return false;
        }
    }
    return profile.anchors[0].frequency_hz == kResponseMinimumHz
           && profile.anchors[profile.anchor_count - 1U].frequency_hz
                  == kResponseMaximumHz;
}

inline bool upstream_identity_matches(
    const FrequencyResponseProfile &profile,
    const UpstreamCalibrationIdentity &actual)
{
    return frequency_response_profile_valid(profile)
           && actual.calibration_id == profile.upstream.calibration_id
           && actual.scale_uv_per_lsb == profile.upstream.scale_uv_per_lsb
           && actual.offset_uv == profile.upstream.offset_uv
           && actual.filter_profile == profile.upstream.filter_profile
           && actual.sample_rate_hz == profile.upstream.sample_rate_hz
           && actual.frame_sample_count
                  == profile.upstream.frame_sample_count;
}

inline bool interpolate_input_uv_per_code(
    const FrequencyResponseProfile &profile, float frequency_hz,
    float *input_uv_per_code)
{
    if (input_uv_per_code == nullptr
        || !frequency_response_profile_valid(profile)
        || !std::isfinite(frequency_hz)
        || frequency_hz < profile.anchors[0].frequency_hz
        || frequency_hz
               > profile.anchors[profile.anchor_count - 1U].frequency_hz) {
        return false;
    }
    for (size_t right = 0U; right < profile.anchor_count; ++right) {
        const FrequencyResponseAnchor &upper = profile.anchors[right];
        if (frequency_hz == upper.frequency_hz) {
            *input_uv_per_code = upper.input_uv_per_code;
            return true;
        }
        if (frequency_hz < upper.frequency_hz) {
            if (right == 0U) {
                return false;
            }
            const FrequencyResponseAnchor &lower =
                profile.anchors[right - 1U];
            const float fraction =
                (frequency_hz - lower.frequency_hz)
                / (upper.frequency_hz - lower.frequency_hz);
            *input_uv_per_code =
                lower.input_uv_per_code
                + fraction
                      * (upper.input_uv_per_code
                         - lower.input_uv_per_code);
            return std::isfinite(*input_uv_per_code)
                   && *input_uv_per_code > 0.0F;
        }
    }
    return false;
}

inline bool frequency_response_correction_factor(
    const FrequencyResponseProfile &profile, float frequency_hz,
    uint32_t incoming_scale_uv_per_lsb, float *factor)
{
    if (factor == nullptr || incoming_scale_uv_per_lsb == 0U
        || incoming_scale_uv_per_lsb
               != profile.upstream.scale_uv_per_lsb) {
        return false;
    }
    float input_uv_per_code = 0.0F;
    if (!interpolate_input_uv_per_code(
            profile, frequency_hz, &input_uv_per_code)) {
        return false;
    }
    *factor = input_uv_per_code
              / static_cast<float>(incoming_scale_uv_per_lsb);
    return std::isfinite(*factor) && *factor > 0.0F;
}

}  // namespace cyclescope
