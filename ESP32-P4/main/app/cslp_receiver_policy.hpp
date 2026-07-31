#pragma once

#include <cstdint>

#include "cslp_protocol.hpp"

namespace cyclescope::receiver_policy {

struct FrameCursor {
    uint32_t session_id = 0;
    uint32_t frame_id = 0;
};

inline bool cursor_allows(const FrameCursor &candidate, const FrameCursor &after)
{
    return candidate.session_id != 0 && candidate.frame_id != 0
           && (after.session_id == 0 || after.frame_id == 0
               || candidate.session_id != after.session_id
               || cslp::sequence_is_newer(candidate.frame_id, after.frame_id));
}

inline bool session_is_current(uint32_t active_session_id, uint32_t frame_session_id)
{
    return active_session_id != 0 && active_session_id == frame_session_id;
}

inline bool stream_identity_is_current(uint32_t active_session_id,
                                       uint32_t active_config_id,
                                       uint32_t active_stream_epoch,
                                       uint32_t frame_session_id,
                                       uint32_t frame_config_id,
                                       uint32_t frame_stream_epoch)
{
    return session_is_current(active_session_id, frame_session_id)
           && active_config_id != 0 && active_config_id == frame_config_id
           && active_stream_epoch == frame_stream_epoch;
}

inline bool rejection_targets_observed(uint32_t candidate, bool have_observed,
                                       uint32_t observed_frame_id)
{
    return candidate != 0 && have_observed && candidate == observed_frame_id;
}

inline uint32_t next_session_id(uint32_t current, uint32_t initial_seed)
{
    if (current == 0) {
        return initial_seed == 0 ? 1 : initial_seed;
    }
    ++current;
    return current == 0 ? 1 : current;
}

inline bool self_test()
{
    return cursor_allows({1, 1}, {})
           && !cursor_allows({1, 100}, {1, 100})
           && !cursor_allows({1, 99}, {1, 100})
           && cursor_allows({1, 101}, {1, 100})
           && cursor_allows({1, 1}, {1, 0xFFFFFFFFU})
           && cursor_allows({2, 1}, {1, 10000})
           && !session_is_current(0, 1)
           && session_is_current(1, 1)
           && !session_is_current(2, 1)
           && stream_identity_is_current(1, 10, 20, 1, 10, 20)
           && !stream_identity_is_current(0, 10, 20, 1, 10, 20)
           && !stream_identity_is_current(1, 0, 20, 1, 10, 20)
           && !stream_identity_is_current(1, 11, 20, 1, 10, 20)
           && !stream_identity_is_current(1, 10, 21, 1, 10, 20)
           && rejection_targets_observed(100, true, 100)
           && !rejection_targets_observed(99, true, 100)
           && !rejection_targets_observed(101, true, 100)
           && !rejection_targets_observed(100, false, 100)
           && next_session_id(0, 0) == 1
           && next_session_id(0, 42) == 42
           && next_session_id(42, 7) == 43
           && next_session_id(0xFFFFFFFFU, 7) == 1;
}

}  // namespace cyclescope::receiver_policy
