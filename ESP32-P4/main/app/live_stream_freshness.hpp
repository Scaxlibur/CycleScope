#pragma once

#include <cstdint>

namespace cyclescope::live_stream_freshness {

// Twenty missing 50 ms frames are enough to stop presenting retained data as
// live.  The 250 ms UI poll detects expiry within 1.0--1.25 seconds.
inline constexpr uint32_t kTimeoutMs = 1000U;

enum class DisplayState : uint8_t {
    Waiting,
    Live,
    OnlineStale,
    OfflineStale,
};

constexpr DisplayState classify(bool has_valid_frame, bool stale_latched,
                                bool transport_ready, uint32_t now_ms,
                                uint32_t freshness_anchor_ms)
{
    const bool freshness_expired =
        static_cast<uint32_t>(now_ms - freshness_anchor_ms) >= kTimeoutMs;
    if (!has_valid_frame && !stale_latched && !freshness_expired) {
        return DisplayState::Waiting;
    }
    if (stale_latched || !transport_ready || freshness_expired) {
        return transport_ready ? DisplayState::OnlineStale
                               : DisplayState::OfflineStale;
    }
    return DisplayState::Live;
}

}  // namespace cyclescope::live_stream_freshness
