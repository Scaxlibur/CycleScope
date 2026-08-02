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

// The summary card deliberately separates transport/session failure from
// frames that reached the analysis pipeline but could not produce a valid
// measurement. NoValidData remains distinct so an online-but-silent FPGA is
// not falsely blamed on the signal acceptance rules.
enum class ConnectionState : uint8_t {
    Checking,
    NoFpgaLink,
    NoValidData,
    DataRejected,
    Normal,
    SystemError,
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

constexpr ConnectionState classify_connection(DisplayState display_state,
                                               bool rejection_observed)
{
    switch (display_state) {
    case DisplayState::Waiting:
        return rejection_observed ? ConnectionState::DataRejected
                                  : ConnectionState::Checking;
    case DisplayState::Live:
        return ConnectionState::Normal;
    case DisplayState::OnlineStale:
        return rejection_observed ? ConnectionState::DataRejected
                                  : ConnectionState::NoValidData;
    case DisplayState::OfflineStale:
        return ConnectionState::NoFpgaLink;
    }
    return ConnectionState::Checking;
}

}  // namespace cyclescope::live_stream_freshness
