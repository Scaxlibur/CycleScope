#include "cyclescope_pipeline_startup_fault_test.hpp"

#include <array>
#include <atomic>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "live_data_pipeline.hpp"

namespace cyclescope::startup_fault_test {
namespace {

constexpr char kTag[] = "cyclescope_fault";
std::atomic<PipelineFailPoint> g_pipeline_failpoint{
    PipelineFailPoint::None};

constexpr std::array<PipelineFailPoint, 9> kPrepareFailpoints = {
    PipelineFailPoint::FftWorkBuffer,
    PipelineFailPoint::FftTableBuffer,
    PipelineFailPoint::HannWindowBuffer,
    PipelineFailPoint::PositiveSpectrumBuffer,
    PipelineFailPoint::FftTableInitialization,
    PipelineFailPoint::SelfTestSampleBuffer,
    PipelineFailPoint::StartupSelfTestGate,
    PipelineFailPoint::AnalysisFrame,
    PipelineFailPoint::UiQueue,
};

struct HeapSnapshot {
    size_t internal_free;
    size_t psram_free;
};

HeapSnapshot capture_heap()
{
    return {
        heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
    };
}

bool heap_restored(const HeapSnapshot &before, const HeapSnapshot &after)
{
    return before.internal_free == after.internal_free
           && before.psram_free == after.psram_free;
}

const char *failpoint_name(PipelineFailPoint point)
{
    switch (point) {
    case PipelineFailPoint::FftWorkBuffer:
        return "P1 fft-work-buffer";
    case PipelineFailPoint::FftTableBuffer:
        return "P2 fft-table-buffer";
    case PipelineFailPoint::HannWindowBuffer:
        return "P3 hann-window-buffer";
    case PipelineFailPoint::PositiveSpectrumBuffer:
        return "P4 positive-spectrum-buffer";
    case PipelineFailPoint::FftTableInitialization:
        return "P5 fft-table-initialization";
    case PipelineFailPoint::SelfTestSampleBuffer:
        return "P6 self-test-sample-buffer";
    case PipelineFailPoint::StartupSelfTestGate:
        return "P7 startup-self-test-gate";
    case PipelineFailPoint::AnalysisFrame:
        return "P8 analysis-frame";
    case PipelineFailPoint::UiQueue:
        return "P9 ui-queue";
    case PipelineFailPoint::AnalysisTask:
        return "P10 analysis-task";
    case PipelineFailPoint::None:
        return "none";
    }
    return "unknown";
}

bool failpoint_is_disarmed()
{
    return g_pipeline_failpoint.load(std::memory_order_acquire)
           == PipelineFailPoint::None;
}

}  // namespace

void arm_pipeline_failpoint(PipelineFailPoint point)
{
    g_pipeline_failpoint.store(point, std::memory_order_release);
}

bool consume_pipeline_failpoint(PipelineFailPoint point)
{
    PipelineFailPoint expected = point;
    const bool consumed = g_pipeline_failpoint.compare_exchange_strong(
        expected, PipelineFailPoint::None, std::memory_order_acq_rel);
    if (consumed) {
        ESP_LOGW(kTag, "Injected one-shot startup fault: %s",
                 failpoint_name(point));
    }
    return consumed;
}

bool run_pipeline_startup_fault_matrix()
{
    for (PipelineFailPoint point : kPrepareFailpoints) {
        LiveDataPipeline pipeline;
        const HeapSnapshot heap_before = capture_heap();
        arm_pipeline_failpoint(point);
        const bool prepared = pipeline.prepare();
        const bool consumed = failpoint_is_disarmed();
        const bool cleanup_ok = pipeline.failed_preparation_is_clean();
        const bool heap_ok = heap_caps_check_integrity_all(true);
        const HeapSnapshot heap_after = capture_heap();
        const bool free_heap_restored =
            heap_restored(heap_before, heap_after);

        if (prepared || !consumed || !cleanup_ok || !heap_ok
            || !free_heap_restored) {
            ESP_LOGE(kTag,
                     "%s FAIL: prepared=%u consumed=%u cleanup=%u heap=%u "
                     "free_restored=%u internal=%u->%u psram=%u->%u",
                     failpoint_name(point), static_cast<unsigned>(prepared),
                     static_cast<unsigned>(consumed),
                     static_cast<unsigned>(cleanup_ok),
                     static_cast<unsigned>(heap_ok),
                     static_cast<unsigned>(free_heap_restored),
                     static_cast<unsigned>(heap_before.internal_free),
                     static_cast<unsigned>(heap_after.internal_free),
                     static_cast<unsigned>(heap_before.psram_free),
                     static_cast<unsigned>(heap_after.psram_free));
            arm_pipeline_failpoint(PipelineFailPoint::None);
            pipeline.release_resources();
            return false;
        }

        ESP_LOGI(kTag,
                 "%s PASS: owned=EMPTY heap=PASS free=EXACT "
                 "internal=%u psram=%u",
                 failpoint_name(point),
                 static_cast<unsigned>(heap_after.internal_free),
                 static_cast<unsigned>(heap_after.psram_free));
        // Idempotence is useful if a future failure path forgets a release;
        // the result above is captured before this defensive cleanup.
        pipeline.release_resources();
    }

    LiveDataPipeline pipeline;
    const HeapSnapshot p10_heap_before = capture_heap();
    if (!pipeline.prepare()) {
        ESP_LOGE(kTag, "P10 setup FAIL: normal preparation failed");
        pipeline.release_resources();
        return false;
    }

    arm_pipeline_failpoint(PipelineFailPoint::AnalysisTask);
    // The one-shot fault is consumed before FreeRTOS creates a task, so a
    // receiver is deliberately unnecessary here. A real second start against
    // a started receiver remains an integration-stage assertion.
    const bool task_started = pipeline.create_analysis_task(nullptr);
    const bool consumed = failpoint_is_disarmed();
    const bool retryable = pipeline.prepared_for_start_retry();
    const bool heap_ok = heap_caps_check_integrity_all(true);
    if (task_started || !consumed || !retryable || !heap_ok) {
        ESP_LOGE(kTag,
                 "P10 analysis-task FAIL: started=%u consumed=%u "
                 "retryable=%u heap=%u",
                 static_cast<unsigned>(task_started),
                 static_cast<unsigned>(consumed),
                 static_cast<unsigned>(retryable),
                 static_cast<unsigned>(heap_ok));
        arm_pipeline_failpoint(PipelineFailPoint::None);
        pipeline.release_resources();
        return false;
    }

    ESP_LOGI(kTag,
             "P10 analysis-task PASS: first creation failed cleanly; "
             "Prepared state remains retryable (real receiver retry deferred "
             "to integration)");
    pipeline.release_resources();
    const bool final_cleanup_ok = pipeline.resources_released();
    const bool final_heap_ok = heap_caps_check_integrity_all(true);
    const HeapSnapshot p10_heap_after = capture_heap();
    const bool final_free_restored =
        heap_restored(p10_heap_before, p10_heap_after);
    if (!final_cleanup_ok || !final_heap_ok || !final_free_restored) {
        ESP_LOGE(kTag,
                 "Pipeline fault matrix final cleanup FAIL: cleanup=%u "
                 "heap=%u free_restored=%u internal=%u->%u psram=%u->%u",
                 static_cast<unsigned>(final_cleanup_ok),
                 static_cast<unsigned>(final_heap_ok),
                 static_cast<unsigned>(final_free_restored),
                 static_cast<unsigned>(p10_heap_before.internal_free),
                 static_cast<unsigned>(p10_heap_after.internal_free),
                 static_cast<unsigned>(p10_heap_before.psram_free),
                 static_cast<unsigned>(p10_heap_after.psram_free));
        return false;
    }

    ESP_LOGI(kTag, "Pipeline startup fault matrix PASS (10/10)");
    return true;
}

}  // namespace cyclescope::startup_fault_test
