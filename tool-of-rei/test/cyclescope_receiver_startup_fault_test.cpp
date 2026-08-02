#include "cyclescope_receiver_startup_fault_test.hpp"

#include <array>
#include <atomic>
#include <inttypes.h>

#include "cslp_udp_receiver.hpp"
#include "esp_err.h"
#include "esp_heap_caps.h"
#if CONFIG_HEAP_TRACING_STANDALONE
#include "esp_heap_trace.h"
#endif
#include "esp_log.h"

namespace cyclescope::startup_fault_test {
namespace {

constexpr char kTag[] = "cyclescope_fault";
std::atomic<ReceiverFailPoint> g_receiver_failpoint{ReceiverFailPoint::None};

constexpr std::array<ReceiverFailPoint, 14> kReceiverFailpoints = {
    ReceiverFailPoint::Mutex,
    ReceiverFailPoint::EventGroup,
    ReceiverFailPoint::NetifInit,
    ReceiverFailPoint::EventLoop,
    ReceiverFailPoint::EthernetInit,
    ReceiverFailPoint::EmptyEthernetHandles,
    ReceiverFailPoint::NetifCreate,
    ReceiverFailPoint::NetifGlue,
    ReceiverFailPoint::NetifAttach,
    ReceiverFailPoint::EthEventHandler,
    ReceiverFailPoint::IpEventHandler,
    ReceiverFailPoint::EthernetStart,
    ReceiverFailPoint::ReceiverTask,
    ReceiverFailPoint::StaticIp,
};

struct HeapSnapshot {
    size_t internal_free;
    size_t psram_free;
};

struct HeapQuiescence {
    HeapSnapshot heap;
    uint32_t waited_ms;
    bool stable;
};

struct HeapRestoration {
    HeapSnapshot heap;
    uint32_t waited_ms;
    bool restored;
};

constexpr uint32_t kMinimumHeapQuiescenceMs = 500;
constexpr uint32_t kHeapPollPeriodMs = 20;
constexpr uint32_t kStableHeapSamples = 5;
constexpr uint32_t kStableBaselineMatches = 5;
constexpr uint32_t kMaximumHeapQuiescenceMs = 2000;
constexpr BaseType_t kFaultMatrixCore = 1;
constexpr UBaseType_t kFaultMatrixTaskPriority = 5;
constexpr uint32_t kFaultMatrixTaskStackBytes = 8192;
#if CONFIG_HEAP_TRACING_STANDALONE
constexpr size_t kHeapTraceRecordCount = 256;
heap_trace_record_t g_heap_trace_records[kHeapTraceRecordCount];
#endif

struct FaultMatrixTaskContext {
    CslpUdpReceiver *receiver;
    SemaphoreHandle_t done;
    std::atomic<bool> result{false};
};

void fault_matrix_task(void *argument)
{
    auto *context = static_cast<FaultMatrixTaskContext *>(argument);
    context->result.store(
        run_receiver_startup_fault_matrix(*context->receiver),
        std::memory_order_release);
    xSemaphoreGive(context->done);
    vTaskDelete(nullptr);
}

HeapSnapshot capture_heap()
{
    return {
        heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
    };
}

HeapQuiescence wait_for_heap_quiescence()
{
    vTaskDelay(pdMS_TO_TICKS(kMinimumHeapQuiescenceMs));
    uint32_t waited_ms = kMinimumHeapQuiescenceMs;
    uint32_t stable_samples = 0;
    HeapSnapshot previous = capture_heap();

    while (waited_ms < kMaximumHeapQuiescenceMs) {
        vTaskDelay(pdMS_TO_TICKS(kHeapPollPeriodMs));
        waited_ms += kHeapPollPeriodMs;
        const HeapSnapshot current = capture_heap();
        if (current.internal_free == previous.internal_free
            && current.psram_free == previous.psram_free) {
            ++stable_samples;
            if (stable_samples >= kStableHeapSamples) {
                return {current, waited_ms, true};
            }
        } else {
            stable_samples = 0;
        }
        previous = current;
    }
    return {previous, waited_ms, false};
}

HeapRestoration wait_for_heap_restoration(const HeapSnapshot &baseline)
{
    // Do not accept an early one-in/one-out plateau: the R7 probe stayed flat
    // for eight 20 ms observations before an older 4-byte command item was
    // finally reclaimed.  Drain for the proven 500 ms window first, then
    // require five consecutive exact matches to the pre-operation baseline.
    vTaskDelay(pdMS_TO_TICKS(kMinimumHeapQuiescenceMs));
    uint32_t waited_ms = kMinimumHeapQuiescenceMs;
    uint32_t baseline_matches = 0;
    HeapSnapshot current = capture_heap();

    while (waited_ms <= kMaximumHeapQuiescenceMs) {
        if (current.internal_free == baseline.internal_free
            && current.psram_free == baseline.psram_free) {
            ++baseline_matches;
            if (baseline_matches >= kStableBaselineMatches) {
                return {current, waited_ms, true};
            }
        } else {
            baseline_matches = 0;
        }
        if (waited_ms == kMaximumHeapQuiescenceMs) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(kHeapPollPeriodMs));
        waited_ms += kHeapPollPeriodMs;
        current = capture_heap();
    }
    return {current, waited_ms, false};
}

const char *failpoint_name(ReceiverFailPoint point)
{
    switch (point) {
    case ReceiverFailPoint::Mutex:
        return "R1 mutex";
    case ReceiverFailPoint::EventGroup:
        return "R2 event-group";
    case ReceiverFailPoint::NetifInit:
        return "R3 netif-init";
    case ReceiverFailPoint::EventLoop:
        return "R4 event-loop";
    case ReceiverFailPoint::EthernetInit:
        return "R5 ethernet-init";
    case ReceiverFailPoint::EmptyEthernetHandles:
        return "R6 empty-handles";
    case ReceiverFailPoint::NetifCreate:
        return "R7 netif-create";
    case ReceiverFailPoint::NetifGlue:
        return "R8 netif-glue";
    case ReceiverFailPoint::NetifAttach:
        return "R9 netif-attach";
    case ReceiverFailPoint::EthEventHandler:
        return "R10 eth-handler";
    case ReceiverFailPoint::IpEventHandler:
        return "R11 ip-handler";
    case ReceiverFailPoint::EthernetStart:
        return "R12 ethernet-start";
    case ReceiverFailPoint::ReceiverTask:
        return "R13 receiver-task";
    case ReceiverFailPoint::StaticIp:
        return "R14 static-ip";
    case ReceiverFailPoint::None:
        return "none";
    }
    return "unknown";
}

esp_err_t expected_error(ReceiverFailPoint point)
{
    return point == ReceiverFailPoint::EmptyEthernetHandles
               ? ESP_ERR_NOT_FOUND
               : ESP_ERR_NO_MEM;
}

}  // namespace

void arm_receiver_failpoint(ReceiverFailPoint point)
{
    g_receiver_failpoint.store(point, std::memory_order_release);
}

bool consume_receiver_failpoint(ReceiverFailPoint point)
{
    ReceiverFailPoint expected = point;
    return g_receiver_failpoint.compare_exchange_strong(
        expected, ReceiverFailPoint::None, std::memory_order_acq_rel);
}

bool run_receiver_startup_fault_matrix(CslpUdpReceiver &receiver)
{
    if (xPortGetCoreID() != kFaultMatrixCore) {
        StaticSemaphore_t done_storage;
        SemaphoreHandle_t done = xSemaphoreCreateBinaryStatic(&done_storage);
        FaultMatrixTaskContext context{&receiver, done};
        if (done == nullptr
            || xTaskCreatePinnedToCore(
                   fault_matrix_task, "cslp_fault", kFaultMatrixTaskStackBytes,
                   &context, kFaultMatrixTaskPriority, nullptr,
                   kFaultMatrixCore)
                   != pdPASS) {
            ESP_LOGE(kTag, "Unable to start receiver fault matrix on Core 1");
            return false;
        }
        xSemaphoreTake(done, portMAX_DELAY);
        return context.result.load(std::memory_order_acquire);
    }

    ESP_LOGI(kTag, "Receiver startup fault matrix running on Core %d",
             xPortGetCoreID());
    if (receiver.started() || receiver.session_ready()) {
        ESP_LOGE(kTag, "Receiver fault matrix requires a stopped receiver");
        return false;
    }

    const auto owned_resources_are_clean = [&receiver]() {
        bool slots_clean = true;
        for (const CslpUdpReceiver::FrameSlot &slot : receiver.slots_) {
            slots_clean = slots_clean
                          && slot.state == CslpUdpReceiver::SlotState::Free;
        }
        return receiver.start_state_.load(std::memory_order_acquire)
                   == CslpUdpReceiver::StartState::Stopped
               && receiver.active_session_id_.load(std::memory_order_acquire)
                      == 0
               && receiver.active_config_id_.load(std::memory_order_acquire)
                      == 0
               && receiver.slot_mutex_ == nullptr
               && receiver.network_events_ == nullptr
               && receiver.receiver_task_handle_ == nullptr
               && receiver.eth_handles_ == nullptr
               && receiver.eth_handle_count_ == 0
               && receiver.eth_netif_ == nullptr
               && receiver.eth_glue_ == nullptr
               && receiver.eth_event_instance_ == nullptr
               && receiver.ip_event_instance_ == nullptr
               && receiver.socket_fd_ == -1
               && !receiver.network_callbacks_enabled_.load(
                      std::memory_order_acquire)
               && !receiver.ethernet_start_attempted_
               && receiver.assembling_index_ == -1
               && receiver.latest_index_ == -1
               && !receiver.have_completed_frame_
               && !receiver.have_observed_frame_
               && receiver.rejected_frame_id_ == 0 && slots_clean;
    };

    // These process-wide services intentionally outlive a receiver rollback.
    // Prime them once so every per-case heap baseline covers only resources
    // owned by CslpUdpReceiver.
    const esp_err_t netif_error = esp_netif_init();
    const esp_err_t event_loop_error = esp_event_loop_create_default();
    if ((netif_error != ESP_OK && netif_error != ESP_ERR_INVALID_STATE)
        || (event_loop_error != ESP_OK
            && event_loop_error != ESP_ERR_INVALID_STATE)
        || !heap_caps_check_integrity_all(true)) {
        ESP_LOGE(kTag,
                 "Unable to prime shared network services: netif=%s loop=%s",
                 esp_err_to_name(netif_error),
                 esp_err_to_name(event_loop_error));
        return false;
    }

    // Exercise the deepest startup/rollback path once before measuring. This
    // absorbs process-wide first-use caches and makes the second R13 attempt a
    // real per-cycle leak check rather than a comparison against cold IDF
    // infrastructure.
    arm_receiver_failpoint(ReceiverFailPoint::ReceiverTask);
    const esp_err_t warmup_error = receiver.start();
    const bool warmup_consumed =
        g_receiver_failpoint.load(std::memory_order_acquire)
        == ReceiverFailPoint::None;
    const HeapQuiescence deepest_quiescence = wait_for_heap_quiescence();
    if (!warmup_consumed
        || warmup_error != expected_error(ReceiverFailPoint::ReceiverTask)
        || !owned_resources_are_clean()
        || !heap_caps_check_integrity_all(true)
        || !deepest_quiescence.stable) {
        ESP_LOGE(kTag,
                 "Unable to prime deepest receiver rollback: consumed=%u "
                 "error=%s owned=%u heap_stable=%u waited=%" PRIu32 "ms",
                 static_cast<unsigned>(warmup_consumed),
                 esp_err_to_name(warmup_error),
                 static_cast<unsigned>(owned_resources_are_clean()),
                 static_cast<unsigned>(deepest_quiescence.stable),
                 deepest_quiescence.waited_ms);
        arm_receiver_failpoint(ReceiverFailPoint::None);
        return false;
    }
    ESP_LOGI(kTag,
             "Receiver deepest rollback warmup PASS after %" PRIu32
             "ms stable quiescence; strict baselines begin",
             deepest_quiescence.waited_ms);

    for (ReceiverFailPoint point : kReceiverFailpoints) {
        // Prime the exact named failure path once. Some ESP-IDF/log internals
        // allocate a few bytes on the first occurrence of a specific error
        // path; the immediately repeated attempt below is the leak check.
        arm_receiver_failpoint(point);
        const esp_err_t path_warmup_error = receiver.start();
        const HeapQuiescence path_warmup_quiescence =
            wait_for_heap_quiescence();
        const bool path_warmup_consumed =
            g_receiver_failpoint.load(std::memory_order_acquire)
            == ReceiverFailPoint::None;
        if (!path_warmup_consumed
            || path_warmup_error != expected_error(point)
            || !owned_resources_are_clean()
            || !heap_caps_check_integrity_all(true)
            || !path_warmup_quiescence.stable) {
            ESP_LOGE(kTag,
                     "%s path warmup FAIL: consumed=%u error=%s owned=%u "
                     "heap_stable=%u waited=%" PRIu32 "ms",
                     failpoint_name(point),
                     static_cast<unsigned>(path_warmup_consumed),
                     esp_err_to_name(path_warmup_error),
                     static_cast<unsigned>(owned_resources_are_clean()),
                     static_cast<unsigned>(path_warmup_quiescence.stable),
                     path_warmup_quiescence.waited_ms);
            arm_receiver_failpoint(ReceiverFailPoint::None);
            return false;
        }

        const HeapSnapshot heap_before = capture_heap();
#if CONFIG_HEAP_TRACING_STANDALONE
        const bool trace_deepest_path =
            point == ReceiverFailPoint::ReceiverTask;
        bool heap_trace_running = false;
        if (trace_deepest_path) {
            const esp_err_t init_error = heap_trace_init_standalone(
                g_heap_trace_records, kHeapTraceRecordCount);
            const esp_err_t start_error =
                init_error == ESP_OK ? heap_trace_start(HEAP_TRACE_LEAKS)
                                     : init_error;
            heap_trace_running = start_error == ESP_OK;
            ESP_LOGI(kTag,
                     "R13 heap trace start: init=%s start=%s records=%u",
                     esp_err_to_name(init_error), esp_err_to_name(start_error),
                     static_cast<unsigned>(kHeapTraceRecordCount));
        }
#endif
        arm_receiver_failpoint(point);
        const esp_err_t error = receiver.start();
        // Ethernet stop/deinit wakes IDF event/driver tasks.  A 24-cycle R7
        // probe proved that adjacent 20 ms samples can look flat while an older
        // 4-byte command item remains pending.  Drain for 500 ms, then require
        // five consecutive exact matches to the pre-operation baseline.
        const HeapRestoration restoration =
            wait_for_heap_restoration(heap_before);
#if CONFIG_HEAP_TRACING_STANDALONE
        if (heap_trace_running) {
            const esp_err_t stop_error = heap_trace_stop();
            ESP_LOGI(kTag, "R13 heap trace stop: %s",
                     esp_err_to_name(stop_error));
            heap_trace_dump_caps(MALLOC_CAP_INTERNAL);
        }
#endif
        const bool failpoint_consumed =
            g_receiver_failpoint.load(std::memory_order_acquire)
            == ReceiverFailPoint::None;
        const bool heap_ok = heap_caps_check_integrity_all(true);
        const HeapSnapshot heap_after = restoration.heap;
        const bool owned_resources_clean = owned_resources_are_clean();
        const bool free_heap_restored =
            heap_before.internal_free == heap_after.internal_free
            && heap_before.psram_free == heap_after.psram_free;
        if (!failpoint_consumed || error != expected_error(point)
            || receiver.started() || receiver.session_ready() || !heap_ok
            || !owned_resources_clean || !free_heap_restored
            || !restoration.restored) {
            ESP_LOGE(kTag,
                     "%s FAIL: consumed=%u error=%s started=%u ready=%u "
                     "heap=%u owned=%u free_restored=%u baseline_restored=%u "
                     "waited=%" PRIu32 "ms internal=%u->%u psram=%u->%u",
                     failpoint_name(point),
                     static_cast<unsigned>(failpoint_consumed),
                     esp_err_to_name(error),
                     static_cast<unsigned>(receiver.started()),
                     static_cast<unsigned>(receiver.session_ready()),
                     static_cast<unsigned>(heap_ok),
                     static_cast<unsigned>(owned_resources_clean),
                     static_cast<unsigned>(free_heap_restored),
                     static_cast<unsigned>(restoration.restored),
                     restoration.waited_ms,
                     static_cast<unsigned>(heap_before.internal_free),
                     static_cast<unsigned>(heap_after.internal_free),
                     static_cast<unsigned>(heap_before.psram_free),
                     static_cast<unsigned>(heap_after.psram_free));
            arm_receiver_failpoint(ReceiverFailPoint::None);
            return false;
        }
        ESP_LOGI(kTag,
                 "%s PASS: error=%s owned=EMPTY heap=PASS free=EXACT "
                 "waited=%" PRIu32 "ms internal=%u psram=%u",
                 failpoint_name(point), esp_err_to_name(error),
                 restoration.waited_ms,
                 static_cast<unsigned>(heap_after.internal_free),
                 static_cast<unsigned>(heap_after.psram_free));

        if (point == ReceiverFailPoint::StaticIp) {
            // Prove that the one-shot R14 failure does not poison a retry. The
            // next start must pass the real static-IP call and reach R12; this
            // avoids launching the permanent receiver task inside the matrix.
            const HeapSnapshot retry_before = capture_heap();
            arm_receiver_failpoint(ReceiverFailPoint::EthernetStart);
            const esp_err_t retry_error = receiver.start();
            const HeapRestoration retry_restoration =
                wait_for_heap_restoration(retry_before);
            const HeapSnapshot retry_after = retry_restoration.heap;
            const bool retry_consumed =
                g_receiver_failpoint.load(std::memory_order_acquire)
                == ReceiverFailPoint::None;
            const bool retry_heap_ok = heap_caps_check_integrity_all(true);
            const bool retry_free_restored =
                retry_before.internal_free == retry_after.internal_free
                && retry_before.psram_free == retry_after.psram_free;
            if (!retry_consumed
                || retry_error
                       != expected_error(ReceiverFailPoint::EthernetStart)
                || receiver.started() || receiver.session_ready()
                || !retry_heap_ok || !owned_resources_are_clean()
                || !retry_free_restored || !retry_restoration.restored) {
                ESP_LOGE(
                    kTag,
                    "R14->R12 retry FAIL: consumed=%u error=%s started=%u "
                    "ready=%u heap=%u owned=%u free_restored=%u "
                    "baseline_restored=%u waited=%" PRIu32
                    "ms internal=%u->%u psram=%u->%u",
                    static_cast<unsigned>(retry_consumed),
                    esp_err_to_name(retry_error),
                    static_cast<unsigned>(receiver.started()),
                    static_cast<unsigned>(receiver.session_ready()),
                    static_cast<unsigned>(retry_heap_ok),
                    static_cast<unsigned>(owned_resources_are_clean()),
                    static_cast<unsigned>(retry_free_restored),
                    static_cast<unsigned>(retry_restoration.restored),
                    retry_restoration.waited_ms,
                    static_cast<unsigned>(retry_before.internal_free),
                    static_cast<unsigned>(retry_after.internal_free),
                    static_cast<unsigned>(retry_before.psram_free),
                    static_cast<unsigned>(retry_after.psram_free));
                arm_receiver_failpoint(ReceiverFailPoint::None);
                return false;
            }
            ESP_LOGI(kTag,
                     "R14->R12 retry PASS: static IP recovered, "
                     "owned=EMPTY heap=PASS free=EXACT waited=%" PRIu32
                     "ms",
                     retry_restoration.waited_ms);
        }
    }

    ESP_LOGI(kTag, "Receiver startup rollback matrix PASS (14/14)");
    return true;
}

}  // namespace cyclescope::startup_fault_test
