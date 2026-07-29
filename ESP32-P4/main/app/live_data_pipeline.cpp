#include "live_data_pipeline.hpp"

#include <math.h>

#include "esp_log.h"
#include "freertos/idf_additions.h"

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_pipe";
constexpr TickType_t kReceiverPeriod = pdMS_TO_TICKS(25);
constexpr UBaseType_t kRawQueueDepth = 4;
constexpr UBaseType_t kReceiverPriority = 3;
constexpr UBaseType_t kAnalysisPriority = 4;
constexpr uint32_t kTaskStackBytes = 4096;
constexpr BaseType_t kDataCore = 1;
constexpr float kPi = 3.14159265358979323846F;

}  // namespace

bool LiveDataPipeline::start()
{
    if (raw_queue_ != nullptr || ui_queue_ != nullptr) {
        return true;
    }

    raw_queue_ = xQueueCreate(kRawQueueDepth, sizeof(RawCaptureFrame));
    ui_queue_ = xQueueCreate(1, sizeof(DynamicMeasurementFrame));
    if (raw_queue_ == nullptr || ui_queue_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate pipeline queues");
        return false;
    }

    if (xTaskCreatePinnedToCore(analysis_task, "cs_analyze", kTaskStackBytes, this,
                                kAnalysisPriority, &analysis_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start analysis task");
        return false;
    }
    if (xTaskCreatePinnedToCore(receiver_task, "cs_receiver", kTaskStackBytes, this,
                                kReceiverPriority, &receiver_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start receiver task");
        vTaskDelete(analysis_task_handle_);
        analysis_task_handle_ = nullptr;
        return false;
    }

    ESP_LOGI(kTag, "M6 pipeline pinned to Core %d: receiver -> raw queue -> analysis -> UI queue",
             kDataCore);
    return true;
}

bool LiveDataPipeline::try_receive_latest(DynamicMeasurementFrame *frame)
{
    return xQueueReceive(ui_queue_, frame, 0) == pdPASS;
}

PipelineStats LiveDataPipeline::stats() const
{
    return {
        received_frames_.load(std::memory_order_relaxed),
        analyzed_frames_.load(std::memory_order_relaxed),
        published_frames_.load(std::memory_order_relaxed),
        dropped_raw_frames_.load(std::memory_order_relaxed),
    };
}

void LiveDataPipeline::receiver_task(void *context)
{
    auto *pipeline = static_cast<LiveDataPipeline *>(context);
    TickType_t next_wake = xTaskGetTickCount();
    uint32_t sequence = 0;

    ESP_LOGI(kTag, "Receiver task running on Core %d", xPortGetCoreID());

    while (true) {
        const RawCaptureFrame raw = {
            .sequence = sequence++,
            .capture_time_ms = static_cast<uint32_t>(pdTICKS_TO_MS(xTaskGetTickCount())),
            .phase = static_cast<float>(sequence) * 0.16F,
        };

        if (xQueueSend(pipeline->raw_queue_, &raw, 0) == pdPASS) {
            pipeline->received_frames_.fetch_add(1, std::memory_order_relaxed);
        } else {
            pipeline->dropped_raw_frames_.fetch_add(1, std::memory_order_relaxed);
        }
        vTaskDelayUntil(&next_wake, kReceiverPeriod);
    }
}

void LiveDataPipeline::analysis_task(void *context)
{
    auto *pipeline = static_cast<LiveDataPipeline *>(context);
    RawCaptureFrame raw{};

    ESP_LOGI(kTag, "Analysis task running on Core %d", xPortGetCoreID());

    while (true) {
        if (xQueueReceive(pipeline->raw_queue_, &raw, portMAX_DELAY) != pdPASS) {
            continue;
        }
        const DynamicMeasurementFrame result = pipeline->analyze(raw);
        pipeline->analyzed_frames_.fetch_add(1, std::memory_order_relaxed);
        if (xQueueOverwrite(pipeline->ui_queue_, &result) == pdPASS) {
            pipeline->published_frames_.fetch_add(1, std::memory_order_relaxed);
        }
    }
}

DynamicMeasurementFrame LiveDataPipeline::analyze(const RawCaptureFrame &raw) const
{
    // Simulates a receiver-delivered FPGA frame followed by measurement code.
    // The UI sees only this immutable calculated result, never a producer-owned
    // sample buffer.
    const float fundamental_amplitude = 0.400F + 0.035F * sinf(raw.phase);
    const float second_harmonic_amplitude = 0.120F + 0.018F * sinf(raw.phase * 1.7F + 0.5F);
    const float third_harmonic_amplitude = 0.060F + 0.010F * sinf(raw.phase * 2.1F + 1.1F);
    const float fundamental_hz = 40000.0F + 2000.0F * sinf(raw.phase * 0.31F);
    const float rms = sqrtf((fundamental_amplitude * fundamental_amplitude
                             + second_harmonic_amplitude * second_harmonic_amplitude
                             + third_harmonic_amplitude * third_harmonic_amplitude) / 2.0F);

    return {
        .sequence = raw.sequence,
        .capture_time_ms = raw.capture_time_ms,
        .voltage_peak_to_peak = 2.0F * (fundamental_amplitude + second_harmonic_amplitude + third_harmonic_amplitude),
        .true_rms_volts = rms,
        .fundamental_hz = fundamental_hz,
        .sample_rate_hz = 256000.0F,
        .spectral_lines = {{
            {fundamental_hz, fundamental_amplitude},
            {2.0F * fundamental_hz, second_harmonic_amplitude},
            {3.0F * fundamental_hz, third_harmonic_amplitude},
        }},
    };
}

}  // namespace cyclescope
