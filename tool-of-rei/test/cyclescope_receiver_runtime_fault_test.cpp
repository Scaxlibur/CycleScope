#include "cyclescope_receiver_runtime_fault_test.hpp"

#include <array>
#include <atomic>
#include <cinttypes>
#include <cstddef>
#include <cstdint>

#include "cslp_udp_receiver.hpp"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

namespace cyclescope::runtime_fault_test {
namespace {

constexpr char kTag[] = "cslp_runtime_test";
constexpr BaseType_t kStartCore = 1;
constexpr UBaseType_t kStartPriority = 5;
constexpr uint32_t kStartStackBytes = 6144;
constexpr uint32_t kPollMs = 10;
constexpr uint32_t kShortTimeoutMs = 2000;
constexpr uint32_t kSessionTimeoutMs = 15000;
constexpr uint32_t kRepeatedFatalCycles = 32;
constexpr uint32_t kRecoveryFrames = 100;

std::atomic<ReceiverRuntimeFailPoint> g_failpoint{
    ReceiverRuntimeFailPoint::None};
std::array<std::atomic<uint32_t>, 4> g_consumed{};
std::atomic<uint32_t> g_sockets_opened{0};
std::atomic<uint32_t> g_sockets_closed{0};
std::atomic<uint32_t> g_socket_close_errors{0};

struct StartContext {
    CslpUdpReceiver *receiver;
    SemaphoreHandle_t done;
    std::atomic<esp_err_t> result{ESP_ERR_INVALID_STATE};
};

struct HeapSnapshot {
    size_t internal_free;
    size_t psram_free;
};

size_t failpoint_index(ReceiverRuntimeFailPoint point)
{
    switch (point) {
    case ReceiverRuntimeFailPoint::SocketCreate:
        return 0;
    case ReceiverRuntimeFailPoint::ReceiveTimeout:
        return 1;
    case ReceiverRuntimeFailPoint::Bind:
        return 2;
    case ReceiverRuntimeFailPoint::RecvfromFatalActive:
        return 3;
    case ReceiverRuntimeFailPoint::None:
        break;
    }
    return g_consumed.size();
}

const char *failpoint_name(ReceiverRuntimeFailPoint point)
{
    switch (point) {
    case ReceiverRuntimeFailPoint::SocketCreate:
        return "RT1 socket-create";
    case ReceiverRuntimeFailPoint::ReceiveTimeout:
        return "RT2 SO_RCVTIMEO";
    case ReceiverRuntimeFailPoint::Bind:
        return "RT3 bind";
    case ReceiverRuntimeFailPoint::RecvfromFatalActive:
        return "RT4 recvfrom-fatal-active";
    case ReceiverRuntimeFailPoint::None:
        return "none";
    }
    return "unknown";
}

void receiver_start_task(void *argument)
{
    auto *context = static_cast<StartContext *>(argument);
    context->result.store(context->receiver->start(), std::memory_order_release);
    xSemaphoreGive(context->done);
    vTaskDelete(nullptr);
}

template <typename Predicate>
bool wait_until(Predicate predicate, uint32_t timeout_ms)
{
    const TickType_t started = xTaskGetTickCount();
    const TickType_t timeout_ticks = pdMS_TO_TICKS(timeout_ms);
    while (xTaskGetTickCount() - started < timeout_ticks) {
        if (predicate()) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(kPollMs));
    }
    return predicate();
}

HeapSnapshot capture_heap()
{
    return {
        heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
    };
}

bool wait_for_stable_heap(HeapSnapshot *snapshot)
{
    if (snapshot == nullptr) {
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(200));
    HeapSnapshot previous = capture_heap();
    uint32_t stable_samples = 0;
    for (uint32_t waited_ms = 0; waited_ms < 1000; waited_ms += 20) {
        vTaskDelay(pdMS_TO_TICKS(20));
        const HeapSnapshot current = capture_heap();
        if (current.internal_free == previous.internal_free
            && current.psram_free == previous.psram_free) {
            ++stable_samples;
            if (stable_samples >= 5) {
                *snapshot = current;
                return true;
            }
        } else {
            stable_samples = 0;
        }
        previous = current;
    }
    *snapshot = previous;
    return false;
}

bool wait_for_exact_heap(const HeapSnapshot &baseline, HeapSnapshot *snapshot)
{
    if (snapshot == nullptr) {
        return false;
    }
    uint32_t exact_samples = 0;
    HeapSnapshot current = capture_heap();
    for (uint32_t waited_ms = 0; waited_ms < 3000; waited_ms += 20) {
        if (current.internal_free == baseline.internal_free
            && current.psram_free == baseline.psram_free) {
            ++exact_samples;
            if (exact_samples >= 5) {
                *snapshot = current;
                return true;
            }
        } else {
            exact_samples = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
        current = capture_heap();
    }
    *snapshot = current;
    return false;
}

uint32_t sample_hash(const CslpUdpReceiver::FrameView &view)
{
    uint32_t hash = 2166136261U;
    const auto *bytes = reinterpret_cast<const uint8_t *>(view.samples);
    const size_t byte_count = view.sample_count * sizeof(int16_t);
    for (size_t index = 0; index < byte_count; ++index) {
        hash ^= bytes[index];
        hash *= 16777619U;
    }
    return hash;
}

bool frame_rejection_stats_equal(const CslpUdpReceiver::Stats &before,
                                 const CslpUdpReceiver::Stats &after)
{
    return before.bad_source == after.bad_source
           && before.bad_magic == after.bad_magic
           && before.bad_version == after.bad_version
           && before.bad_length == after.bad_length
           && before.crc_failures == after.crc_failures
           && before.config_mismatches == after.config_mismatches
           && before.metadata_conflicts == after.metadata_conflicts
           && before.duplicate_chunks == after.duplicate_chunks
           && before.stale_chunks == after.stale_chunks
           && before.incomplete_frames == after.incomplete_frames
           && before.overrange_frames == after.overrange_frames
           && before.fifo_overflow_frames == after.fifo_overflow_frames
           && before.dropped_busy == after.dropped_busy;
}

}  // namespace

void arm_receiver_runtime_failpoint(ReceiverRuntimeFailPoint point)
{
    g_failpoint.store(point, std::memory_order_release);
}

bool consume_receiver_runtime_failpoint(ReceiverRuntimeFailPoint point)
{
    ReceiverRuntimeFailPoint expected = point;
    if (!g_failpoint.compare_exchange_strong(
            expected, ReceiverRuntimeFailPoint::None,
            std::memory_order_acq_rel)) {
        return false;
    }
    const size_t index = failpoint_index(point);
    if (index < g_consumed.size()) {
        g_consumed[index].fetch_add(1, std::memory_order_relaxed);
    }
    ESP_LOGW(kTag, "%s injected through real lwIP syscall",
             failpoint_name(point));
    return true;
}

void note_receiver_socket_opened(int socket_fd)
{
    g_sockets_opened.fetch_add(1, std::memory_order_relaxed);
    ESP_LOGI(kTag, "socket opened: fd=%d opened=%" PRIu32, socket_fd,
             g_sockets_opened.load(std::memory_order_relaxed));
}

void note_receiver_socket_closed(int socket_fd, int close_result)
{
    if (close_result == 0) {
        g_sockets_closed.fetch_add(1, std::memory_order_relaxed);
    } else {
        g_socket_close_errors.fetch_add(1, std::memory_order_relaxed);
    }
    ESP_LOGI(kTag,
             "socket closed: fd=%d result=%d closed=%" PRIu32
             " errors=%" PRIu32,
             socket_fd, close_result,
             g_sockets_closed.load(std::memory_order_relaxed),
             g_socket_close_errors.load(std::memory_order_relaxed));
}

bool run_receiver_runtime_fault_matrix(CslpUdpReceiver &receiver)
{
    if (receiver.started() || receiver.session_ready()) {
        ESP_LOGE(kTag, "Runtime matrix requires a stopped receiver");
        return false;
    }
    for (std::atomic<uint32_t> &count : g_consumed) {
        count.store(0, std::memory_order_relaxed);
    }
    g_sockets_opened.store(0, std::memory_order_relaxed);
    g_sockets_closed.store(0, std::memory_order_relaxed);
    g_socket_close_errors.store(0, std::memory_order_relaxed);
    const CslpUdpReceiver::Stats initial = receiver.stats();

    StaticSemaphore_t done_storage;
    SemaphoreHandle_t done = xSemaphoreCreateBinaryStatic(&done_storage);
    StartContext context{&receiver, done};
    arm_receiver_runtime_failpoint(ReceiverRuntimeFailPoint::SocketCreate);
    if (done == nullptr
        || xTaskCreatePinnedToCore(
               receiver_start_task, "cslp_rt_start", kStartStackBytes,
               &context, kStartPriority, nullptr, kStartCore)
               != pdPASS) {
        ESP_LOGE(kTag, "Unable to launch receiver start on Core 1");
        arm_receiver_runtime_failpoint(ReceiverRuntimeFailPoint::None);
        return false;
    }
    xSemaphoreTake(done, portMAX_DELAY);
    const esp_err_t start_error =
        context.result.load(std::memory_order_acquire);
    if (start_error != ESP_OK || !receiver.started()) {
        ESP_LOGE(kTag, "Receiver start failed: %s",
                 esp_err_to_name(start_error));
        return false;
    }

    if (!wait_until(
            [&receiver, &initial]() {
                return receiver.stats().socket_open_failures
                           == initial.socket_open_failures + 1
                       && g_consumed[0].load(std::memory_order_relaxed) == 1;
            },
            kShortTimeoutMs)) {
        ESP_LOGE(kTag, "RT1 socket-create was not consumed exactly once");
        return false;
    }
    ESP_LOGI(kTag, "RT1 socket-create PASS: no fd allocated");

    arm_receiver_runtime_failpoint(ReceiverRuntimeFailPoint::ReceiveTimeout);
    if (!wait_until(
            [&receiver, &initial]() {
                return receiver.stats().socket_open_failures
                           == initial.socket_open_failures + 2
                       && g_consumed[1].load(std::memory_order_relaxed) == 1
                       && g_sockets_opened.load(std::memory_order_relaxed) == 1
                       && g_sockets_closed.load(std::memory_order_relaxed) == 1;
            },
            kShortTimeoutMs)) {
        ESP_LOGE(kTag, "RT2 SO_RCVTIMEO cleanup did not close its real fd");
        return false;
    }
    ESP_LOGI(kTag, "RT2 SO_RCVTIMEO PASS: fd open/close exact");

    arm_receiver_runtime_failpoint(ReceiverRuntimeFailPoint::Bind);
    if (!wait_until(
            [&receiver, &initial]() {
                return receiver.stats().socket_open_failures
                           == initial.socket_open_failures + 3
                       && g_consumed[2].load(std::memory_order_relaxed) == 1
                       && g_sockets_opened.load(std::memory_order_relaxed) == 2
                       && g_sockets_closed.load(std::memory_order_relaxed) == 2;
            },
            kShortTimeoutMs)) {
        ESP_LOGE(kTag, "RT3 bind cleanup did not close its real fd");
        return false;
    }
    ESP_LOGI(kTag, "RT3 bind PASS: fd open/close exact");

    if (!wait_until(
            [&receiver, &initial]() {
                return receiver.session_ready()
                       && receiver.stats().sessions_established
                              == initial.sessions_established + 1;
            },
            kSessionTimeoutMs)) {
        ESP_LOGE(kTag, "RT4 prerequisite session S1 was not established");
        return false;
    }
    const uint32_t old_session =
        receiver.active_session_id_.load(std::memory_order_acquire);
    const uint32_t old_config =
        receiver.active_config_id_.load(std::memory_order_acquire);
    const uint32_t frames_before_hold = receiver.stats().frames_completed;
    if (!wait_until(
            [&receiver, frames_before_hold]() {
                return receiver.stats().frames_completed
                       >= frames_before_hold + 2;
            },
            kSessionTimeoutMs)) {
        ESP_LOGE(kTag, "S1 did not deliver enough frames for an InUse lease");
        return false;
    }

    CslpUdpReceiver::FrameView held{};
    if (!receiver.acquire_latest({0, 0}, &held)) {
        ESP_LOGE(kTag, "Unable to hold an S1 frame across RT4");
        return false;
    }
    const uint32_t held_hash = sample_hash(held);
    const uint32_t held_completed = receiver.stats().frames_completed;
    if (!wait_until(
            [&receiver, held_completed]() {
                return receiver.stats().frames_completed > held_completed;
            },
            kSessionTimeoutMs)) {
        receiver.release(&held);
        ESP_LOGE(kTag, "S1 did not publish a pending Latest behind InUse");
        return false;
    }

    const uint32_t closes_before_fatal =
        g_sockets_closed.load(std::memory_order_relaxed);
    arm_receiver_runtime_failpoint(
        ReceiverRuntimeFailPoint::RecvfromFatalActive);
    if (!wait_until(
            [&receiver, &initial, closes_before_fatal]() {
                return receiver.stats().recv_fatal_errors
                           == initial.recv_fatal_errors + 1
                       && !receiver.session_ready()
                       && g_sockets_closed.load(std::memory_order_relaxed)
                              == closes_before_fatal + 1;
            },
            kShortTimeoutMs)) {
        receiver.release(&held);
        ESP_LOGE(kTag, "RT4 did not reach formal Fatal teardown");
        return false;
    }

    bool slot_state_ok = true;
    xSemaphoreTake(receiver.slot_mutex_, portMAX_DELAY);
    for (size_t index = 0; index < receiver.slots_.size(); ++index) {
        const CslpUdpReceiver::FrameSlot &slot = receiver.slots_[index];
        if (index == held.slot_index) {
            slot_state_ok = slot_state_ok
                            && slot.state == CslpUdpReceiver::SlotState::InUse
                            && slot.lease_generation == held.lease_generation
                            && slot.metadata.session_id == old_session
                            && slot.metadata.config_id == old_config;
        } else {
            slot_state_ok =
                slot_state_ok && slot.state == CslpUdpReceiver::SlotState::Free;
        }
    }
    xSemaphoreGive(receiver.slot_mutex_);
    const bool held_immutable = sample_hash(held) == held_hash;
    const bool old_is_stale = !receiver.frame_is_current(held);
    const bool stream_cleared =
        receiver.active_session_id_.load(std::memory_order_acquire) == 0
        && receiver.active_config_id_.load(std::memory_order_acquire) == 0;
    const bool fd_balance_at_teardown =
        g_sockets_opened.load(std::memory_order_relaxed)
        == g_sockets_closed.load(std::memory_order_relaxed);
    if (!slot_state_ok || !held_immutable || !old_is_stale
        || !stream_cleared || !fd_balance_at_teardown
        || g_socket_close_errors.load(std::memory_order_relaxed) != 0
        || !heap_caps_check_integrity_all(true)) {
        receiver.release(&held);
        ESP_LOGE(kTag,
                 "RT4 teardown FAIL: slots=%u immutable=%u stale=%u stream=%u "
                 "fd_balance=%u close_errors=%" PRIu32,
                 static_cast<unsigned>(slot_state_ok),
                 static_cast<unsigned>(held_immutable),
                 static_cast<unsigned>(old_is_stale),
                 static_cast<unsigned>(stream_cleared),
                 static_cast<unsigned>(fd_balance_at_teardown),
                 g_socket_close_errors.load(std::memory_order_relaxed));
        return false;
    }
    ESP_LOGI(kTag,
             "RT4 teardown PASS: S1=0x%08" PRIX32
             " InUse immutable, pending empty, stream stale, fd closed",
             old_session);
    receiver.release(&held);

    if (!wait_until(
            [&receiver, &initial, old_session]() {
                return receiver.session_ready()
                       && receiver.stats().sessions_established
                              == initial.sessions_established + 2
                       && receiver.active_session_id_.load(
                              std::memory_order_acquire)
                              != old_session;
            },
            kSessionTimeoutMs)) {
        ESP_LOGE(kTag, "S2 did not recover after RT4");
        return false;
    }
    const uint32_t recovered_session =
        receiver.active_session_id_.load(std::memory_order_acquire);
    const uint32_t bad_session_before = receiver.stats().bad_session;
    if (!wait_until(
            [&receiver, bad_session_before]() {
                return receiver.stats().bad_session
                       >= bad_session_before + CslpUdpReceiver::kChunkCount;
            },
            kSessionTimeoutMs)) {
        ESP_LOGE(kTag, "S2 did not reject the explicit old-S1 frame");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
    const CslpUdpReceiver::Stats before_recovery_frames = receiver.stats();
    if (before_recovery_frames.bad_session
        != bad_session_before + CslpUdpReceiver::kChunkCount) {
        ESP_LOGE(kTag,
                 "Old-session rejection was not exact: %" PRIu32 "->%" PRIu32,
                 bad_session_before, before_recovery_frames.bad_session);
        return false;
    }
    if (!wait_until(
            [&receiver, &before_recovery_frames]() {
                return receiver.stats().frames_completed
                       >= before_recovery_frames.frames_completed
                              + kRecoveryFrames;
            },
            kSessionTimeoutMs)) {
        ESP_LOGE(kTag, "S2 did not complete 100 recovery frames");
        return false;
    }
    const CslpUdpReceiver::Stats after_recovery_frames = receiver.stats();
    if (after_recovery_frames.frames_completed
            != before_recovery_frames.frames_completed + kRecoveryFrames
        || after_recovery_frames.bad_session
               != before_recovery_frames.bad_session
        || !frame_rejection_stats_equal(before_recovery_frames,
                                        after_recovery_frames)) {
        ESP_LOGE(kTag,
                 "S2 recovery frame accounting failed: completed=%" PRIu32
                 "->%" PRIu32 " bad_session=%" PRIu32 "->%" PRIu32,
                 before_recovery_frames.frames_completed,
                 after_recovery_frames.frames_completed,
                 before_recovery_frames.bad_session,
                 after_recovery_frames.bad_session);
        return false;
    }
    ESP_LOGI(kTag,
             "S2 recovery PASS: S1=0x%08" PRIX32 " S2=0x%08" PRIX32
             " old_chunks=12 new_frames=100",
             old_session, recovered_session);

    HeapSnapshot active_heap_baseline{};
    if (!wait_for_stable_heap(&active_heap_baseline)
        || !heap_caps_check_integrity_all(true)) {
        ESP_LOGE(kTag, "Unable to establish active-session heap baseline");
        return false;
    }

    uint32_t current_session = recovered_session;
    for (uint32_t cycle = 1; cycle <= kRepeatedFatalCycles; ++cycle) {
        const uint32_t fatal_before = receiver.stats().recv_fatal_errors;
        const uint32_t sessions_before = receiver.stats().sessions_established;
        const uint32_t closed_before =
            g_sockets_closed.load(std::memory_order_relaxed);
        arm_receiver_runtime_failpoint(
            ReceiverRuntimeFailPoint::RecvfromFatalActive);
        if (!wait_until(
                [&receiver, fatal_before, closed_before]() {
                    return receiver.stats().recv_fatal_errors
                               == fatal_before + 1
                           && !receiver.session_ready()
                           && g_sockets_closed.load(std::memory_order_relaxed)
                                  == closed_before + 1;
                },
                kShortTimeoutMs)
            || !wait_until(
                [&receiver, sessions_before, current_session]() {
                    return receiver.session_ready()
                           && receiver.stats().sessions_established
                                  == sessions_before + 1
                           && receiver.active_session_id_.load(
                                  std::memory_order_acquire)
                                  != current_session;
                },
                kSessionTimeoutMs)
            || !heap_caps_check_integrity_all(true)) {
            ESP_LOGE(kTag, "RT4 repeated cycle %" PRIu32 " failed", cycle);
            return false;
        }
        current_session =
            receiver.active_session_id_.load(std::memory_order_acquire);
        if (cycle == 1 || cycle % 8 == 0
            || cycle == kRepeatedFatalCycles) {
            ESP_LOGI(kTag,
                     "RT4 repeated cycle %" PRIu32 "/%" PRIu32
                     " PASS: session=0x%08" PRIX32,
                     cycle, kRepeatedFatalCycles, current_session);
        }
    }

    const CslpUdpReceiver::Stats before_final_frames = receiver.stats();
    if (!wait_until(
            [&receiver, &before_final_frames]() {
                return receiver.stats().frames_completed
                       >= before_final_frames.frames_completed
                              + kRecoveryFrames;
            },
            kSessionTimeoutMs)) {
        ESP_LOGE(kTag, "Final recovered session did not complete 100 frames");
        return false;
    }
    const CslpUdpReceiver::Stats final = receiver.stats();
    if (final.frames_completed
            != before_final_frames.frames_completed + kRecoveryFrames
        || final.bad_session != before_final_frames.bad_session
        || !frame_rejection_stats_equal(before_final_frames, final)) {
        ESP_LOGE(kTag,
                 "Final recovery frame accounting failed: completed=%" PRIu32
                 "->%" PRIu32 " bad_session=%" PRIu32 "->%" PRIu32,
                 before_final_frames.frames_completed,
                 final.frames_completed, before_final_frames.bad_session,
                 final.bad_session);
        return false;
    }
    HeapSnapshot final_heap{};
    const bool heap_restored =
        wait_for_exact_heap(active_heap_baseline, &final_heap);
    const uint32_t expected_fatal_count = kRepeatedFatalCycles + 1;
    const uint32_t expected_sessions = kRepeatedFatalCycles + 2;
    const uint32_t expected_reconnects = kRepeatedFatalCycles + 4;
    const uint32_t expected_opened = kRepeatedFatalCycles + 4;
    const uint32_t expected_closed = expected_opened - 1;
    const bool counters_ok =
        final.socket_open_failures == initial.socket_open_failures + 3
        && final.recv_fatal_errors
               == initial.recv_fatal_errors + expected_fatal_count
        && final.socket_close_failures == initial.socket_close_failures
        && final.sessions_established
               == initial.sessions_established + expected_sessions
        && final.reconnects == initial.reconnects + expected_reconnects
        && g_consumed[0].load(std::memory_order_relaxed) == 1
        && g_consumed[1].load(std::memory_order_relaxed) == 1
        && g_consumed[2].load(std::memory_order_relaxed) == 1
        && g_consumed[3].load(std::memory_order_relaxed)
               == expected_fatal_count
        && g_sockets_opened.load(std::memory_order_relaxed)
               == expected_opened
        && g_sockets_closed.load(std::memory_order_relaxed)
               == expected_closed
        && g_socket_close_errors.load(std::memory_order_relaxed) == 0;
    if (!counters_ok || !heap_restored
        || !heap_caps_check_integrity_all(true)) {
        ESP_LOGE(
            kTag,
            "Runtime matrix FAIL: counters=%u heap_exact=%u "
            "open_fail=%" PRIu32 " fatal=%" PRIu32 " close_fail=%" PRIu32
            " sessions=%" PRIu32 " reconnects=%" PRIu32
            " fd=%" PRIu32 "/%" PRIu32
            " heap=%u/%u->%u/%u",
            static_cast<unsigned>(counters_ok),
            static_cast<unsigned>(heap_restored), final.socket_open_failures,
            final.recv_fatal_errors, final.socket_close_failures,
            final.sessions_established, final.reconnects,
            g_sockets_opened.load(std::memory_order_relaxed),
            g_sockets_closed.load(std::memory_order_relaxed),
            static_cast<unsigned>(active_heap_baseline.internal_free),
            static_cast<unsigned>(active_heap_baseline.psram_free),
            static_cast<unsigned>(final_heap.internal_free),
            static_cast<unsigned>(final_heap.psram_free));
        return false;
    }

    ESP_LOGI(
        kTag,
        "Receiver runtime fault matrix PASS: RT1/RT2/RT3=1/1/1 "
        "RT4=%" PRIu32 " sessions=%" PRIu32
        " fd=%" PRIu32 "/%" PRIu32
        " heap=EXACT %u/%u final_frames=100",
        expected_fatal_count, expected_sessions, expected_opened,
        expected_closed,
        static_cast<unsigned>(final_heap.internal_free),
        static_cast<unsigned>(final_heap.psram_free));
    return true;
}

}  // namespace cyclescope::runtime_fault_test
