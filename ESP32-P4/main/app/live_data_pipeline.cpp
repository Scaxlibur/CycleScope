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
constexpr uint32_t kSimulatedConfigId = 1;
constexpr float kMaximumDisplayAmplitude = 0.5F;
constexpr float kNoiseReferenceVolts = 0.00075F;
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

DynamicMeasurementFrame *allocate_analysis_frame()
{
    void *buffer = heap_caps_calloc(1, sizeof(DynamicMeasurementFrame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == nullptr) {
        buffer = heap_caps_calloc(1, sizeof(DynamicMeasurementFrame), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    return static_cast<DynamicMeasurementFrame *>(buffer);
}

}  // namespace

bool LiveDataPipeline::start()
{
    if (receiver_task_handle_ != nullptr && analysis_task_handle_ != nullptr && raw_queue_ != nullptr
        && ui_queue_ != nullptr && analysis_frame_ != nullptr) {
        return true;
    }
    if (raw_queue_ != nullptr || ui_queue_ != nullptr || receiver_task_handle_ != nullptr
        || analysis_task_handle_ != nullptr || analysis_frame_ != nullptr) {
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
    analysis_frame_ = allocate_analysis_frame();
    if (analysis_frame_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate the spectrum display frame");
        release_resources();
        return false;
    }

    raw_queue_ = xQueueCreate(kRawQueueDepth, sizeof(RawCaptureFrame));
    ui_queue_ = xQueueCreate(1, sizeof(DynamicMeasurementFrame));
    if (raw_queue_ == nullptr || ui_queue_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate pipeline queues");
        release_resources();
        return false;
    }

    if (xTaskCreatePinnedToCore(analysis_task, "cs_fft8192", kAnalysisStackBytes, this,
                                kAnalysisPriority, &analysis_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start FFT analysis task");
        release_resources();
        return false;
    }
    if (xTaskCreatePinnedToCore(receiver_task, "cs_local_source", kReceiverStackBytes, this,
                                kReceiverPriority, &receiver_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start local sample source task");
        vTaskDelete(analysis_task_handle_);
        analysis_task_handle_ = nullptr;
        release_resources();
        return false;
    }

    ESP_LOGI(kTag,
             "Local FFT pipeline on Core %d: source prio %u -> FFT prio %u -> latest UI queue; "
             "display frame=%u bytes",
             kDataCore, kReceiverPriority, kAnalysisPriority,
             static_cast<unsigned>(sizeof(DynamicMeasurementFrame)));
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
        if (!pipeline->analyze(raw, pipeline->analysis_frame_)) {
            pipeline->fft_failures_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }

        const uint32_t analyzed_frames =
            pipeline->analyzed_frames_.fetch_add(1, std::memory_order_relaxed) + 1U;
        pipeline->last_analysis_us_.store(pipeline->analysis_frame_->analysis_time_us,
                                          std::memory_order_relaxed);
        pipeline->cumulative_analysis_us_ += pipeline->analysis_frame_->analysis_time_us;
        const uint32_t next_average =
            static_cast<uint32_t>(pipeline->cumulative_analysis_us_ / analyzed_frames);
        pipeline->average_analysis_us_.store(next_average, std::memory_order_relaxed);
        uint32_t previous_maximum = pipeline->maximum_analysis_us_.load(std::memory_order_relaxed);
        while (pipeline->analysis_frame_->analysis_time_us > previous_maximum
               && !pipeline->maximum_analysis_us_.compare_exchange_weak(
                   previous_maximum, pipeline->analysis_frame_->analysis_time_us,
                   std::memory_order_relaxed)) {
        }

        if (xQueueOverwrite(pipeline->ui_queue_, pipeline->analysis_frame_) == pdPASS) {
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
    if (result == nullptr) {
        return false;
    }

    FftAnalysisResult fft_result{};
    const esp_err_t error = fft_processor_.process(test_samples_, FftProcessor8192::kSampleCount,
                                                   kSampleRateHz, kScaleUvPerLsb, kOffsetUv, &fft_result);
    if (error != ESP_OK || !fft_result.valid
        || fft_processor_.positive_spectrum_size() != FftProcessor8192::kPositiveBinCount) {
        ESP_LOGE(kTag, "FFT frame %lu failed: %s, valid=%d, bins=%u",
                 static_cast<unsigned long>(raw.sequence), esp_err_to_name(error), fft_result.valid,
                 static_cast<unsigned>(fft_processor_.positive_spectrum_size()));
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

    *result = {};
    result->sequence = raw.sequence;
    result->capture_time_ms = raw.capture_time_ms;
    result->config_id = kSimulatedConfigId;
    result->voltage_peak_to_peak = fft_result.voltage_peak_to_peak;
    result->true_rms_volts = fft_result.true_rms_volts;
    result->fundamental_hz = fft_result.fundamental_hz;
    result->sample_rate_hz = fft_result.sample_rate_hz;
    result->analysis_time_us = fft_result.analysis_time_us;

    SpectrumDisplayFrame &spectrum = result->spectrum;
    spectrum.generation = raw.sequence + 1U;
    spectrum.sample_rate_hz = static_cast<uint32_t>(fft_result.sample_rate_hz);
    spectrum.fft_size = static_cast<uint16_t>(FftProcessor8192::kSampleCount);
    spectrum.column_count = static_cast<uint16_t>(kSpectrumDisplayColumns);
    spectrum.peak_count = static_cast<uint8_t>(
        std::min<uint32_t>(fft_result.spectral_line_count, kMaximumSpectralPeaks));
    spectrum.source_buffer_index = 0xFF;
    spectrum.frequency_min_hz = 0.0F;
    spectrum.frequency_max_hz = fft_result.sample_rate_hz * 0.5F;
    spectrum.bin_width_hz = fft_result.bin_width_hz;
    spectrum.amplitude_max_volts = kMaximumDisplayAmplitude;

    for (size_t peak_index = 0; peak_index < spectrum.peak_count; ++peak_index) {
        const SpectralLine &source = fft_result.spectral_lines[peak_index];
        const long rounded_bin = std::lround(source.frequency_hz / fft_result.bin_width_hz);
        const size_t bin = std::min<size_t>(
            FftProcessor8192::kPositiveBinCount - 1U,
            static_cast<size_t>(std::max(0L, rounded_bin)));
        spectrum.peaks[peak_index] = {
            .bin_index = static_cast<uint16_t>(bin),
            .frequency_hz = source.frequency_hz,
            .amplitude_volts_peak = source.amplitude_volts_peak,
            .snr_db = 20.0F * std::log10(source.amplitude_volts_peak / kNoiseReferenceVolts),
        };
    }

    const float *positive_spectrum = fft_processor_.positive_spectrum();
    float dense_maximum = 0.0F;
    float compressed_maximum = 0.0F;
    for (size_t column = 0; column < kSpectrumDisplayColumns; ++column) {
        const size_t first_bin =
            column * FftProcessor8192::kPositiveBinCount / kSpectrumDisplayColumns;
        size_t end_bin =
            (column + 1U) * FftProcessor8192::kPositiveBinCount / kSpectrumDisplayColumns;
        if (end_bin <= first_bin) {
            end_bin = first_bin + 1U;
        }

        float peak = 0.0F;
        float squares = 0.0F;
        for (size_t bin = first_bin; bin < end_bin; ++bin) {
            const float magnitude = positive_spectrum[bin];
            peak = std::max(peak, magnitude);
            dense_maximum = std::max(dense_maximum, magnitude);
            squares += magnitude * magnitude;
        }
        spectrum.columns[column] = {
            .peak_volts = peak,
            .rms_volts = std::sqrt(squares / static_cast<float>(end_bin - first_bin)),
        };
        compressed_maximum = std::max(compressed_maximum, peak);
    }

    if (raw.sequence < 2U) {
        const bool peak_preserved = std::fabs(dense_maximum - compressed_maximum) < 0.000001F;
        ESP_LOGI(kTag,
                 "Spectrum frame: gen=%lu FFT=%u bins=%u columns=%u peaks=%u df=%.6fHz "
                 "axis=0..%.5fMHz peak-preservation=%s",
                 static_cast<unsigned long>(spectrum.generation), spectrum.fft_size,
                 static_cast<unsigned>(FftProcessor8192::kPositiveBinCount), spectrum.column_count,
                 spectrum.peak_count, static_cast<double>(spectrum.bin_width_hz),
                 static_cast<double>(spectrum.frequency_max_hz / 1000000.0F),
                 peak_preserved ? "PASS" : "FAIL");
    }
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

void LiveDataPipeline::release_resources()
{
    if (raw_queue_ != nullptr) {
        vQueueDelete(raw_queue_);
        raw_queue_ = nullptr;
    }
    if (ui_queue_ != nullptr) {
        vQueueDelete(ui_queue_);
        ui_queue_ = nullptr;
    }
    if (analysis_frame_ != nullptr) {
        heap_caps_free(analysis_frame_);
        analysis_frame_ = nullptr;
    }
    if (test_samples_ != nullptr) {
        heap_caps_free(test_samples_);
        test_samples_ = nullptr;
    }
}

}  // namespace cyclescope
