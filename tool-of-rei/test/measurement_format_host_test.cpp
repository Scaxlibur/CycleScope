// Host-only UI formatting fixture. Keep fixture sources under tool-of-rei/test/.

#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>

#include "measurement_format.hpp"

namespace {

struct FormatCase {
    float input;
    const char *expected;
};

bool test_millivolts()
{
    constexpr std::array<FormatCase, 5> cases = {{
        {0.0F, "0.00mV"},
        {0.01234F, "12.34mV"},
        {0.035F, "35.00mV"},
        {0.100F, "100.00mV"},
        {0.500F, "500.00mV"},
    }};
    for (const FormatCase &test : cases) {
        char output[24]{};
        if (!cyclescope::measurement_format::millivolts(
                output, sizeof(output), test.input)
            || std::strcmp(output, test.expected) != 0) {
            std::fprintf(stderr, "millivolts mismatch: %.9g -> %s\n",
                         static_cast<double>(test.input), output);
            return false;
        }
    }
    return true;
}

bool test_hertz()
{
    constexpr std::array<FormatCase, 8> cases = {{
        {0.0F, "0Hz"},
        {999.0F, "999Hz"},
        {999.5F, "1,000Hz"},
        {10000.0F, "10,000Hz"},
        {23456.4F, "23,456Hz"},
        {23456.6F, "23,457Hz"},
        {499999.875F, "500,000Hz"},
        {1000000.0F, "1,000,000Hz"},
    }};
    for (const FormatCase &test : cases) {
        char output[24]{};
        if (!cyclescope::measurement_format::hertz(
                output, sizeof(output), test.input)
            || std::strcmp(output, test.expected) != 0) {
            std::fprintf(stderr, "hertz mismatch: %.9g -> %s\n",
                         static_cast<double>(test.input), output);
            return false;
        }
    }
    return true;
}

bool test_hertz_hundredths()
{
    constexpr std::array<FormatCase, 8> cases = {{
        {0.0F, "0.00Hz"},
        {999.0F, "999.00Hz"},
        {999.996F, "1,000.00Hz"},
        {10000.0F, "10,000.00Hz"},
        {23456.78F, "23,456.78Hz"},
        {23456.875F, "23,456.88Hz"},
        {499999.875F, "499,999.88Hz"},
        {1000000.0F, "1,000,000.00Hz"},
    }};
    for (const FormatCase &test : cases) {
        char output[24]{};
        if (!cyclescope::measurement_format::hertz_hundredths(
                output, sizeof(output), test.input)
            || std::strcmp(output, test.expected) != 0) {
            std::fprintf(stderr,
                         "hertz hundredths mismatch: %.9g -> %s\n",
                         static_cast<double>(test.input), output);
            return false;
        }
    }
    return true;
}

bool test_fail_closed()
{
    constexpr std::array<float, 3> invalid = {{
        -1.0F,
        std::numeric_limits<float>::quiet_NaN(),
        std::numeric_limits<float>::infinity(),
    }};
    for (float value : invalid) {
        char output[24]{};
        if (cyclescope::measurement_format::millivolts(
                output, sizeof(output), value)
            || std::strcmp(output, "--") != 0
            || cyclescope::measurement_format::hertz(
                output, sizeof(output), value)
            || std::strcmp(output, "--") != 0
            || cyclescope::measurement_format::hertz_hundredths(
                output, sizeof(output), value)
            || std::strcmp(output, "--") != 0) {
            std::fprintf(stderr, "invalid value did not fail closed\n");
            return false;
        }
    }

    char small[4]{};
    if (cyclescope::measurement_format::hertz(
            small, sizeof(small), 23456.0F)
        || std::strcmp(small, "--") != 0
        || cyclescope::measurement_format::millivolts(
            small, sizeof(small), 0.01234F)
        || std::strcmp(small, "--") != 0
        || cyclescope::measurement_format::hertz_hundredths(
            small, sizeof(small), 23456.0F)
        || std::strcmp(small, "--") != 0
        || cyclescope::measurement_format::hertz(
            nullptr, 0U, 1.0F)
        || cyclescope::measurement_format::millivolts(
            nullptr, 0U, 1.0F)
        || cyclescope::measurement_format::hertz_hundredths(
            nullptr, 0U, 1.0F)) {
        std::fprintf(stderr, "buffer boundary did not fail closed\n");
        return false;
    }
    return true;
}

}  // namespace

int main()
{
    if (!test_millivolts() || !test_hertz()
        || !test_hertz_hundredths() || !test_fail_closed()) {
        return 1;
    }
    std::puts(
        "measurement format host test passed: mV=2dp, peak Hz=1Hz, F0=0.01Hz, invalid=fail-closed");
    return 0;
}
