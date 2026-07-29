#include "cslp_frame_diagnostic.hpp"

#include "sdkconfig.h"

#if CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <inttypes.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "cslp_udp_receiver.hpp"

namespace cyclescope {
namespace {

constexpr char kTag[] = "cslp_diag";
constexpr uint32_t kPollPeriodMs = 10;
constexpr uint32_t kCheckPeriodMs = 25;
constexpr uint64_t kSummaryPeriodUs = 5000000;
constexpr uint32_t kTaskStackBytes = 4096;
constexpr UBaseType_t kTaskPriority = 1;
constexpr BaseType_t kTaskCore = 1;

TaskHandle_t diagnostic_task_handle = nullptr;

struct Counters {
    uint32_t acquired = 0;
    uint32_t empty_polls = 0;
    uint32_t hash_checks = 0;
    uint32_t immutable_failures = 0;
    uint32_t invalid_views = 0;
    uint32_t holds_with_rx_progress = 0;
    uint32_t holds_without_rx_progress = 0;
    uint32_t current_to_stale = 0;
    uint32_t stale_revalidated_failures = 0;
    uint32_t session_transitions = 0;
    uint32_t stale_releases = 0;
    uint32_t post_stale_reacquires = 0;
    uint32_t release_ok = 0;
    uint32_t release_failures = 0;
};

uint64_t frame_fingerprint(const CslpUdpReceiver::FrameView &view)
{
    constexpr uint64_t kFnvOffset = UINT64_C(14695981039346656037);
    constexpr uint64_t kFnvPrime = UINT64_C(1099511628211);
    const auto *bytes = reinterpret_cast<const uint8_t *>(view.samples);
    const size_t byte_count = view.sample_count * sizeof(int16_t);
    uint64_t hash = kFnvOffset;
    for (size_t index = 0; index < byte_count; ++index) {
        hash ^= bytes[index];
        hash *= kFnvPrime;
    }
    return hash;
}

void log_summary(const Counters &counters, uint32_t busy_baseline)
{
    const uint32_t busy_delta =
        cslp_udp_receiver().stats().dropped_busy - busy_baseline;
    ESP_LOGI(kTag,
             "summary acquired=%" PRIu32 " empty=%" PRIu32
             " hash_checks=%" PRIu32 " immutable_fail=%" PRIu32
             " invalid=%" PRIu32 " progress=%" PRIu32 "/%" PRIu32,
             counters.acquired, counters.empty_polls, counters.hash_checks,
             counters.immutable_failures, counters.invalid_views,
             counters.holds_with_rx_progress,
             counters.holds_without_rx_progress);
    ESP_LOGI(kTag,
             "session stale=%" PRIu32 " revalidated_fail=%" PRIu32
             " transitions=%" PRIu32 " stale_release=%" PRIu32
             " post_stale=%" PRIu32 " release=%" PRIu32 "/%" PRIu32
             " busy_delta=%" PRIu32,
             counters.current_to_stale,
             counters.stale_revalidated_failures,
             counters.session_transitions, counters.stale_releases,
             counters.post_stale_reacquires, counters.release_ok,
             counters.release_failures, busy_delta);
}

void diagnostic_task(void *)
{
    CslpUdpReceiver &receiver = cslp_udp_receiver();
    CslpUdpReceiver::FrameCursor cursor{};
    Counters counters{};
    uint32_t last_session_id = 0;
    bool awaiting_post_stale_reacquire = false;
    const uint32_t busy_baseline = receiver.stats().dropped_busy;
    uint64_t last_summary_us = esp_timer_get_time();

    ESP_LOGI(kTag,
             "ownership diagnostic started on Core %d hold=%d ms",
             kTaskCore, CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_HOLD_MS);

    while (true) {
        CslpUdpReceiver::FrameView view{};
        if (!receiver.acquire_latest(cursor, &view)) {
            ++counters.empty_polls;
            vTaskDelay(pdMS_TO_TICKS(kPollPeriodMs));
        } else {
            ++counters.acquired;
            const CslpUdpReceiver::FrameCursor acquired_cursor = view.cursor();
            cursor = acquired_cursor;

            if (last_session_id != 0
                && acquired_cursor.session_id != last_session_id) {
                ++counters.session_transitions;
                if (awaiting_post_stale_reacquire) {
                    ++counters.post_stale_reacquires;
                    awaiting_post_stale_reacquire = false;
                }
                ESP_LOGI(kTag,
                         "session transition old=0x%08" PRIX32
                         " new=0x%08" PRIX32 " frame=%" PRIu32,
                         last_session_id, acquired_cursor.session_id,
                         acquired_cursor.frame_id);
            }
            last_session_id = acquired_cursor.session_id;

            const bool valid_view =
                view.samples != nullptr
                && view.sample_count == CslpUdpReceiver::kFrameSampleCount;
            if (!valid_view) {
                ++counters.invalid_views;
                ESP_LOGE(kTag,
                         "invalid acquired view: samples=0x%" PRIXPTR
                         " count=%u",
                         reinterpret_cast<uintptr_t>(view.samples),
                         static_cast<unsigned>(view.sample_count));
            }

            const CslpUdpReceiver::Stats before = receiver.stats();
            const uint64_t initial_hash = valid_view ? frame_fingerprint(view) : 0;
            bool current_seen = false;
            bool stale_seen = false;
            bool mutation_reported = false;
            bool revalidated_reported = false;

            const auto observe_current = [&](bool current) {
                if (!current && !stale_seen) {
                    stale_seen = true;
                    awaiting_post_stale_reacquire = true;
                    ++counters.current_to_stale;
                    ESP_LOGI(kTag,
                             "old result suppressed session=0x%08" PRIX32
                             " frame=%" PRIu32,
                             acquired_cursor.session_id,
                             acquired_cursor.frame_id);
                } else if (current && stale_seen && !revalidated_reported) {
                    ++counters.stale_revalidated_failures;
                    revalidated_reported = true;
                    ESP_LOGE(kTag,
                             "stale view became current again session=0x%08"
                             PRIX32 " frame=%" PRIu32,
                             acquired_cursor.session_id,
                             acquired_cursor.frame_id);
                }
                current_seen = current_seen || current;
            };

            observe_current(receiver.frame_is_current(view));

            uint32_t held_ms = 0;
            while (held_ms < CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_HOLD_MS) {
                const uint32_t delay_ms = std::min(
                    kCheckPeriodMs,
                    static_cast<uint32_t>(
                        CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_HOLD_MS - held_ms));
                vTaskDelay(pdMS_TO_TICKS(delay_ms));
                held_ms += delay_ms;

                if (valid_view) {
                    ++counters.hash_checks;
                    if (frame_fingerprint(view) != initial_hash
                        && !mutation_reported) {
                        ++counters.immutable_failures;
                        mutation_reported = true;
                        ESP_LOGE(kTag,
                                 "InUse mutation session=0x%08" PRIX32
                                 " frame=%" PRIu32,
                                 acquired_cursor.session_id,
                                 acquired_cursor.frame_id);
                    }
                }

                observe_current(receiver.frame_is_current(view));
            }

            const CslpUdpReceiver::Stats after = receiver.stats();
            if (after.frames_completed != before.frames_completed) {
                ++counters.holds_with_rx_progress;
            } else {
                ++counters.holds_without_rx_progress;
            }
            const bool current_before_release = receiver.frame_is_current(view);
            observe_current(current_before_release);
            if (!current_before_release) {
                ++counters.stale_releases;
            }

            receiver.release(&view);
            const bool release_cleared =
                view.samples == nullptr && view.sample_count == 0
                && view.slot_index == 0xFF && view.lease_generation == 0
                && !receiver.frame_is_current(view);
            if (release_cleared) {
                ++counters.release_ok;
            } else {
                ++counters.release_failures;
                ESP_LOGE(kTag, "release did not clear the frame token");
            }
            receiver.release(&view);

            if (counters.acquired == 1) {
                ESP_LOGI(kTag,
                         "first frame session=0x%08" PRIX32
                         " frame=%" PRIu32 " hash=0x%016" PRIX64
                         " current=%u",
                         acquired_cursor.session_id, acquired_cursor.frame_id,
                         initial_hash, current_seen ? 1U : 0U);
            }
        }

        const uint64_t current_us = esp_timer_get_time();
        if (current_us - last_summary_us >= kSummaryPeriodUs) {
            last_summary_us = current_us;
            log_summary(counters, busy_baseline);
        }
    }
}

}  // namespace

esp_err_t start_cslp_frame_diagnostic()
{
    if (diagnostic_task_handle != nullptr) {
        return ESP_OK;
    }
    if (xTaskCreatePinnedToCore(diagnostic_task, "cslp_diag", kTaskStackBytes,
                                nullptr, kTaskPriority,
                                &diagnostic_task_handle, kTaskCore) != pdPASS) {
        diagnostic_task_handle = nullptr;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

}  // namespace cyclescope

#else

namespace cyclescope {

esp_err_t start_cslp_frame_diagnostic()
{
    return ESP_ERR_NOT_SUPPORTED;
}

}  // namespace cyclescope

#endif
