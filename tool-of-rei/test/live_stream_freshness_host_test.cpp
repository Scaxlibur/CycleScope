#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>

#include "live_stream_freshness.hpp"

namespace {

using cyclescope::live_stream_freshness::DisplayState;
using cyclescope::live_stream_freshness::ConnectionState;
using cyclescope::live_stream_freshness::classify;
using cyclescope::live_stream_freshness::classify_connection;
using cyclescope::live_stream_freshness::kTimeoutMs;

bool expect(DisplayState actual, DisplayState expected, const char *label)
{
    if (actual == expected) {
        return true;
    }
    std::cerr << "live freshness case failed: " << label << '\n';
    return false;
}

bool expect(ConnectionState actual, ConnectionState expected,
            const char *label)
{
    if (actual == expected) {
        return true;
    }
    std::cerr << "connection state case failed: " << label << '\n';
    return false;
}

}  // namespace

int main()
{
    constexpr uint32_t start = 5000U;
    bool pass = true;
    pass &= expect(classify(false, false, false, start + kTimeoutMs - 1U,
                            start),
                   DisplayState::Waiting, "initial offline wait");
    pass &= expect(classify(false, false, true, start + kTimeoutMs - 1U,
                            start),
                   DisplayState::Waiting, "initial online wait");
    pass &= expect(classify(false, false, true, start + kTimeoutMs, start),
                   DisplayState::OnlineStale, "no first valid frame");
    pass &= expect(classify(true, false, true, start + kTimeoutMs - 1U,
                            start),
                   DisplayState::Live, "fresh online frame");
    pass &= expect(classify(true, false, true, start + kTimeoutMs, start),
                   DisplayState::OnlineStale, "online data expiry");
    pass &= expect(classify(true, false, false, start + 1U, start),
                   DisplayState::OfflineStale, "transport loss");
    pass &= expect(classify(true, true, true, start + 1U, start),
                   DisplayState::OnlineStale, "stale latch until recovery");
    pass &= expect(classify(true, false, true, start, start),
                   DisplayState::Live, "fresh-frame recovery");

    constexpr uint32_t before_wrap =
        std::numeric_limits<uint32_t>::max() - 499U;
    constexpr uint32_t after_wrap = 500U;
    pass &= expect(classify(true, false, true, after_wrap, before_wrap),
                   DisplayState::OnlineStale, "tick wrap expiry");

    pass &= expect(classify_connection(DisplayState::Waiting, false),
                   ConnectionState::Checking, "connection checking");
    pass &= expect(classify_connection(DisplayState::Waiting, true),
                   ConnectionState::DataRejected,
                   "first received frame rejected");
    pass &= expect(classify_connection(DisplayState::OfflineStale, true),
                   ConnectionState::NoFpgaLink, "connection offline");
    pass &= expect(classify_connection(DisplayState::OnlineStale, false),
                   ConnectionState::NoValidData, "online without frame");
    pass &= expect(classify_connection(DisplayState::OnlineStale, true),
                   ConnectionState::DataRejected, "online rejected data");
    pass &= expect(classify_connection(DisplayState::Live, true),
                   ConnectionState::Normal, "fresh valid data wins");

    if (!pass) {
        return EXIT_FAILURE;
    }
    std::cout << "live stream freshness host test PASS: timeout="
              << kTimeoutMs << "ms cases=15\n";
    return EXIT_SUCCESS;
}
