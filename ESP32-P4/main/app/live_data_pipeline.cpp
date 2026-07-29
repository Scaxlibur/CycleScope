#include "live_data_pipeline.hpp"

#include <algorithm>
#include <cmath>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_memory_utils.h"
#include "freertos/idf_additions.h"

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_pipe";
constexpr TickType_t kReceiverPeriod = pdMS_TO_TICKS(50);
constexpr UBaseType_t kRawQueueDepth = 1;
constexpr UBaseType_t kReceiverPriority = 5;
constexpr UBaseType_t kAnalysisPriority = 4;
constexpr uint32_t kReceiverStackBytes = 4096;
constexpr uint32_t kAnalysisStackBytes = 8192;
constexpr uint32_t kHealthLogFramePeriod = 600;
constexpr BaseType_t kDataCore = 1;
constexpr float kPi = 3.14159265358979323846F;
constexpr float kSampleRateHz = 4062500.0F;
constexpr int32_t kScaleUvPerLsb = 100;
constexpr int32_t kOffsetUv = 500;
// Keep these defaults synchronized with tools/generate_fft_test_vector.py.
constexpr float kTestFundamentalHz = 40750.0F;
constexpr std::array<uint16_t, kMaximumSpectralLines> kTestHarmonics = {1, 3, 4};
constexpr std::array<float, kMaximumSpectralLines> kTestAmplitudesVolts = {0.025F, 0.070F, 0.025F};
constexpr std::array<float, kMaximumSpectralLines> kTestPhasesRadians = {0.17F, 0.92F, -0.51F};
constexpr float kTestExpectedPeakToPeakVolts = 0.181421109F;

void *allocate_sample_buffer(size_t bytes)
{
    void *buffer = heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == nullptr) {
        buffer = heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    return buffer;
}

}  // namespace

bool LiveDataPipeline::start()
{
    if (receiver_task_handle_ != nullptr && analysis_task_handle_ != nullptr) {
        return true;
    }
    if (raw_queue_ != nullptr || ui_queue_ != nullptr || receiver_task_handle_ != nullptr
        || analysis_task_handle_ != nullptr) {
        ESP_LOGE(kTag, "Pipeline is only partially initialized; refusing an unsafe restart");
        return false;
    }

    const esp_err_t fft_error = fft_processor_.initialize();
    if (fft_error != ESP_OK) {
        ESP_LOGE(kTag, "Unable to initialize 8192-point FFT: %s", esp_err_to_name(fft_error));
        return false;
    }
    if (!allocate_test_samples()) {
        return false;
    }

    raw_queue_ = xQueueCreate(kRawQueueDepth, sizeof(RawCaptureFrame));
    ui_queue_ = xQueueCreate(1, sizeof(DynamicMeasurementFrame));
    if (raw_queue_ == nullptr || ui_queue_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate pipeline queues");
        if (raw_queue_ != nullptr) {
            vQueueDelete(raw_queue_);
            raw_queue_ = nullptr;
        }
        if (ui_queue_ != nullptr) {
            vQueueDelete(ui_queue_);
            ui_queue_ = nullptr;
        }
        return false;
    }

    if (xTaskCreatePinnedToCore(analysis_task, "cs_fft8192", kAnalysisStackBytes, this,
                                kAnalysisPriority, &analysis_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start FFT analysis task");
        vQueueDelete(raw_queue_);
        vQueueDelete(ui_queue_);
        raw_queue_ = nullptr;
        ui_queue_ = nullptr;
        return false;
    }
    if (xTaskCreatePinnedToCore(receiver_task, "cs_local_source", kReceiverStackBytes, this,
                                kReceiverPriority, &receiver_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start local sample source task");
        vTaskDelete(analysis_task_handle_);
        analysis_task_handle_ = nullptr;
        vQueueDelete(raw_queue_);
        vQueueDelete(ui_queue_);
        raw_queue_ = nullptr;
        ui_queue_ = nullptr;
        return false;
    }

    ESP_LOGI(kTag,
             "Local-only pipeline pinned to Core %d: S16 test frame -> latest queue -> esp-dsp FFT8192 -> UI",
             kDataCore);
    return true;
}

bool LiveDataPipeline::allocate_test_samples()
{
    if (test_samples_ != nullptr) {
        return true;
    }
    test_samples_ = static_cast<int16_t *>(
        allocate_sample_buffer(FftProcessor8192::kSampleCount * sizeof(int16_t)));
    if (test_samples_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate the 16 KiB local S16 test frame");
        return false;
    }
    generate_test_samples();
    ESP_LOGI(kTag, "Local S16 test frame allocated in %s",
             esp_ptr_external_ram(test_samples_) ? "PSRAM" : "internal RAM");
    return true;
}

void LiveDataPipeline::generate_test_samples()
{
    const float volts_per_lsb = static_cast<float>(kScaleUvPerLsb) * 1.0e-6F;
    const float offset_volts = static_cast<float>(kOffsetUv) * 1.0e-6F;
    for (size_t sample = 0; sample < FftProcessor8192::kSampleCount; ++sample) {
        const float time_seconds = static_cast<float>(sample) / kSampleRateHz;
        float voltage = 0.0F;
        for (size_t line = 0; line < kMaximumSpectralLines; ++line) {
            const float phase = 2.0F * kPi * kTestFundamentalHz
                                * static_cast<float>(kTestHarmonics[line]) * time_seconds
                                + kTestPhasesRadians[line];
            voltage += kTestAmplitudesVolts[line] * std::sin(phase);
        }
        const long code = std::lround((voltage - offset_volts) / volts_per_lsb);
        test_samples_[sample] = static_cast<int16_t>(std::max(-2048L, std::min(2047L, code)));
    }
}

bool LiveDataPipeline::try_receive_latest(DynamicMeasurementFrame *frame)
{
    return frame != nullptr && ui_queue_ != nullptr && xQueueReceive(ui_queue_, frame, 0) == pdPASS;
}

PipelineStats LiveDataPipeline::stats() const
{
    return {
        .received_frames = received_frames_.load(std::memory_order_relaxed),
        .analyzed_frames = analyzed_frames_.load(std::memory_order_relaxed),
        .published_frames = published_frames_.load(std::memory_order_relaxed),
        .dropped_raw_frames = dropped_raw_frames_.load(std::memory_order_relaxed),
        .fft_failures = fft_failures_.load(std::memory_order_relaxed),
        .last_analysis_us = last_analysis_us_.load(std::memory_order_relaxed),
        .average_analysis_us = average_analysis_us_.load(std::memory_order_relaxed),
        .maximum_analysis_us = maximum_analysis_us_.load(std::memory_order_relaxed),
        .fft_self_test_passed = fft_self_test_passed_.load(std::memory_order_relaxed),
    };
}

void LiveDataPipeline::receiver_task(void *context)
{
    auto *pipeline = static_cast<LiveDataPipeline *>(context);
    TickType_t next_wake = xTaskGetTickCount();
    uint32_t sequence = 0;

    ESP_LOGI(kTag, "Local sample source running on Core %d", xPortGetCoreID());

    while (true) {
        const RawCaptureFrame raw = {
            .sequence = sequence++,
            .capture_time_ms = static_cast<uint32_t>(pdTICKS_TO_MS(xTaskGetTickCount())),
        };

        if (uxQueueMessagesWaiting(pipeline->raw_queue_) != 0) {
            pipeline->dropped_raw_frames_.fetch_add(1, std::memory_order_relaxed);
        }
        if (xQueueOverwrite(pipeline->raw_queue_, &raw) == pdPASS) {
            pipeline->received_frames_.fetch_add(1, std::memory_order_relaxed);
        }
        vTaskDelayUntil(&next_wake, kReceiverPeriod);
    }
}

void LiveDataPipeline::analysis_task(void *context)
{
    auto *pipeline = static_cast<LiveDataPipeline *>(context);
    RawCaptureFrame raw{};

    ESP_LOGI(kTag, "8192-point FFT analysis running on Core %d", xPortGetCoreID());

    while (true) {
        if (xQueueReceive(pipeline->raw_queue_, &raw, portMAX_DELAY) != pdPASS) {
            continue;
        }
        DynamicMeasurementFrame result{};
        if (!pipeline->analyze(raw, &result)) {
            pipeline->fft_failures_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }

        const uint32_t analyzed_frames =
            pipeline->analyzed_frames_.fetch_add(1, std::memory_order_relaxed) + 1U;
        pipeline->last_analysis_us_.store(result.analysis_time_us, std::memory_order_relaxed);
        pipeline->cumulative_analysis_us_ += result.analysis_time_us;
        const uint32_t next_average =
            static_cast<uint32_t>(pipeline->cumulative_analysis_us_ / analyzed_frames);
        pipeline->average_analysis_us_.store(next_average, std::memory_order_relaxed);
        uint32_t previous_maximum = pipeline->maximum_analysis_us_.load(std::memory_order_relaxed);
        while (result.analysis_time_us > previous_maximum
               && !pipeline->maximum_analysis_us_.compare_exchange_weak(
                   previous_maximum, result.analysis_time_us, std::memory_order_relaxed)) {
        }

        if (xQueueOverwrite(pipeline->ui_queue_, &result) == pdPASS) {
            pipeline->published_frames_.fetch_add(1, std::memory_order_relaxed);
        }
        if (analyzed_frames % kHealthLogFramePeriod == 0U) {
            const PipelineStats stats = pipeline->stats();
            ESP_LOGI(kTag,
                     "FFT health: src=%lu analyzed=%lu published=%lu stale=%lu failures=%lu "
                     "fft_us(last/avg/max)=%lu/%lu/%lu internal_free=%u psram_free=%u",
                     static_cast<unsigned long>(stats.received_frames),
                     static_cast<unsigned long>(stats.analyzed_frames),
                     static_cast<unsigned long>(stats.published_frames),
                     static_cast<unsigned long>(stats.dropped_raw_frames),
                     static_cast<unsigned long>(stats.fft_failures),
                     static_cast<unsigned long>(stats.last_analysis_us),
                     static_cast<unsigned long>(stats.average_analysis_us),
                     static_cast<unsigned long>(stats.maximum_analysis_us),
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        }
    }
}

bool LiveDataPipeline::analyze(const RawCaptureFrame &raw, DynamicMeasurementFrame *result)
{
    FftAnalysisResult fft_result{};
    const esp_err_t error = fft_processor_.process(test_samples_, FftProcessor8192::kSampleCount,
                                                   kSampleRateHz, kScaleUvPerLsb, kOffsetUv, &fft_result);
    if (error != ESP_OK || !fft_result.valid) {
        ESP_LOGE(kTag, "FFT frame %lu failed: %s, valid=%d", static_cast<unsigned long>(raw.sequence),
                 esp_err_to_name(error), fft_result.valid);
        return false;
    }

    if (!self_test_logged_) {
        self_test_logged_ = true;
        const bool passed = validate_self_test(fft_result);
        const bool heap_integrity_passed = heap_caps_check_integrity_all(true);
        fft_self_test_passed_.store(passed, std::memory_order_relaxed);
        ESP_LOGI(kTag,
                 "FFT8192 self-test: F0=%.2f Hz, H1/H3/H4=%.2f/%.2f/%.2f mVpk, "
                 "Vpp=%.3f mV, RMS=%.3f mV, bin=%.9f Hz, elapsed=%lu us, %s",
                 static_cast<double>(fft_result.fundamental_hz),
                 static_cast<double>(fft_result.spectral_lines[0].amplitude_volts_peak * 1000.0F),
                 static_cast<double>(fft_result.spectral_lines[1].amplitude_volts_peak * 1000.0F),
                 static_cast<double>(fft_result.spectral_lines[2].amplitude_volts_peak * 1000.0F),
                 static_cast<double>(fft_result.voltage_peak_to_peak * 1000.0F),
                 static_cast<double>(fft_result.true_rms_volts * 1000.0F),
                 static_cast<double>(fft_result.bin_width_hz),
                 static_cast<unsigned long>(fft_result.analysis_time_us), passed ? "PASS" : "FAIL");
        ESP_LOGI(kTag, "Heap integrity after first FFT: %s", heap_integrity_passed ? "PASS" : "FAIL");
    }

    *result = {
        .sequence = raw.sequence,
        .capture_time_ms = raw.capture_time_ms,
        .voltage_peak_to_peak = fft_result.voltage_peak_to_peak,
        .true_rms_volts = fft_result.true_rms_volts,
        .fundamental_hz = fft_result.fundamental_hz,
        .sample_rate_hz = fft_result.sample_rate_hz,
        .analysis_time_us = fft_result.analysis_time_us,
        .spectral_line_count = fft_result.spectral_line_count,
        .spectral_lines = fft_result.spectral_lines,
    };
    return true;
}

bool LiveDataPipeline::validate_self_test(const FftAnalysisResult &result)
{
    if (!result.valid || result.spectral_line_count != kMaximumSpectralLines
        || std::fabs(result.fundamental_hz - kTestFundamentalHz) > 1000.0F
        || std::fabs(result.bin_width_hz - 495.91064453125F) > 0.01F
        || std::fabs(result.voltage_peak_to_peak - kTestExpectedPeakToPeakVolts) > 0.005F
        || result.analysis_time_us >= 50000U) {
        return false;
    }

    float expected_rms_square = 0.0F;
    for (size_t line = 0; line < kMaximumSpectralLines; ++line) {
        if (result.spectral_lines[line].harmonic_order != kTestHarmonics[line]
            || std::fabs(result.spectral_lines[line].amplitude_volts_peak - kTestAmplitudesVolts[line])
                   > 0.005F) {
            return false;
        }
        expected_rms_square += kTestAmplitudesVolts[line] * kTestAmplitudesVolts[line] * 0.5F;
    }
    return std::fabs(result.true_rms_volts - std::sqrt(expected_rms_square)) <= 0.005F;
}

}  // namespace cyclescope
