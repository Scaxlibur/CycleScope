#include "frequency_response_compensation.hpp"

#include <array>
#include <cmath>
#include <cstdio>
#include <limits>

namespace {

constexpr std::array<cyclescope::FrequencyResponseAnchor, 3> kAnchors = {{
    {10000.0F, 260.0F},
    {100000.0F, 262.0F},
    {500000.0F, 266.0F},
}};

constexpr cyclescope::UpstreamCalibrationIdentity kIdentity = {
    .calibration_id = 25030,
    .scale_uv_per_lsb = 516,
    .offset_uv = -6761,
    .filter_profile = 1,
    .sample_rate_hz = 4062500,
    .frame_sample_count = 8192,
};

constexpr cyclescope::FrequencyResponseProfile kProfile = {
    .profile_id = 0x12345678U,
    .upstream = kIdentity,
    .anchors = kAnchors.data(),
    .anchor_count = kAnchors.size(),
};

bool close(float left, float right, float tolerance = 1.0e-6F)
{
    return std::fabs(left - right) <= tolerance;
}

bool fail(const char *message)
{
    std::fprintf(stderr, "frequency-response host test: %s\n", message);
    return false;
}

}  // namespace

int main()
{
    using namespace cyclescope;
    if (!frequency_response_profile_valid(kProfile)
        || !upstream_identity_matches(kProfile, kIdentity)) {
        return fail("valid profile/identity was rejected");
    }
    float value = 0.0F;
    if (!interpolate_input_uv_per_code(kProfile, 10000.0F, &value)
        || !close(value, 260.0F)
        || !interpolate_input_uv_per_code(kProfile, 55000.0F, &value)
        || !close(value, 261.0F)
        || !interpolate_input_uv_per_code(kProfile, 500000.0F, &value)
        || !close(value, 266.0F)) {
        return fail("anchor or linear interpolation result changed");
    }
    if (interpolate_input_uv_per_code(kProfile, 9999.0F, &value)
        || interpolate_input_uv_per_code(kProfile, 500001.0F, &value)
        || interpolate_input_uv_per_code(
            kProfile, std::numeric_limits<float>::quiet_NaN(), &value)) {
        return fail("out-of-range/non-finite frequency was extrapolated");
    }
    float factor = 0.0F;
    if (!frequency_response_correction_factor(
            kProfile, 100000.0F, 516U, &factor)
        || !close(factor, 262.0F / 516.0F)
        || frequency_response_correction_factor(
            kProfile, 100000.0F, 515U, &factor)) {
        return fail("correction factor did not bind incoming scalar");
    }
    for (int field = 0; field < 6; ++field) {
        UpstreamCalibrationIdentity wrong = kIdentity;
        switch (field) {
        case 0: wrong.calibration_id += 1U; break;
        case 1: wrong.scale_uv_per_lsb += 1U; break;
        case 2: wrong.offset_uv += 1; break;
        case 3: wrong.filter_profile += 1U; break;
        case 4: wrong.sample_rate_hz += 1U; break;
        case 5: wrong.frame_sample_count += 1U; break;
        }
        if (upstream_identity_matches(kProfile, wrong)) {
            return fail("identity mismatch was accepted");
        }
    }
    auto duplicate = kAnchors;
    duplicate[1].frequency_hz = duplicate[0].frequency_hz;
    FrequencyResponseProfile bad = kProfile;
    bad.anchors = duplicate.data();
    if (frequency_response_profile_valid(bad)) {
        return fail("duplicate anchor was accepted");
    }
    auto nonfinite = kAnchors;
    nonfinite[1].input_uv_per_code =
        std::numeric_limits<float>::infinity();
    bad.anchors = nonfinite.data();
    if (frequency_response_profile_valid(bad)) {
        return fail("non-finite anchor was accepted");
    }
    std::puts("frequency_response_compensation=PASS");
    return 0;
}
