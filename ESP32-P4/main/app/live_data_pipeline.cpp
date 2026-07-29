#include "live_data_pipeline.hpp"

#include <math.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/idf_additions.h"

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_pipe";
constexpr TickType_t kReceiverPeriod = pdMS_TO_TICKS(50);
constexpr UBaseType_t kRawQueueDepth = 4;
constexpr UBaseType_t kReceiverPriority = 5;
constexpr UBaseType_t kAnalysisPriority = 4;
constexpr uint32_t kTaskStackBytes = 4096;
constexpr BaseType_t kDataCore = 1;
constexpr size_t kDenseSpectrumBufferCount = 2;
constexpr float kSampleRateHz = 4062500.0F;
constexpr float kMaximumDisplayAmplitude = 0.5F;
constexpr float kNoiseReferenceVolts = 0.00075F;
constexpr uint32_t kSimulatedConfigId = 1;

}  // namespace

bool LiveDataPipeline::start()
{
    if (raw_queue_ != nullptr && ui_queue_ != nullptr && spectrum_buffers_ != nullptr
        && analysis_frame_ != nullptr) {
        return true;
    }

    spectrum_buffers_ = static_cast<DenseSpectrumBuffer *>(heap_caps_calloc(
        kDenseSpectrumBufferCount, sizeof(DenseSpectrumBuffer), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    analysis_frame_ = static_cast<DynamicMeasurementFrame *>(
        heap_caps_calloc(1, sizeof(DynamicMeasurementFrame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (spectrum_buffers_ == nullptr || analysis_frame_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate PSRAM spectrum buffers");
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

    if (xTaskCreatePinnedToCore(analysis_task, "cs_analyze", kTaskStackBytes, this,
                                kAnalysisPriority, &analysis_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start analysis task");
        release_resources();
        return false;
    }
    if (xTaskCreatePinnedToCore(receiver_task, "cs_receiver", kTaskStackBytes, this,
                                kReceiverPriority, &receiver_task_handle_, kDataCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to start receiver task");
        vTaskDelete(analysis_task_handle_);
        analysis_task_handle_ = nullptr;
        release_resources();
        return false;
    }

    ESP_LOGI(kTag,
             "Spectrum pipeline on Core %d: receiver prio %u -> analysis prio %u -> latest UI queue; "
             "frame=%u bytes, dense A/B=%u bytes in PSRAM",
             kDataCore, kReceiverPriority, kAnalysisPriority, static_cast<unsigned>(sizeof(DynamicMeasurementFrame)),
             static_cast<unsigned>(kDenseSpectrumBufferCount * sizeof(DenseSpectrumBuffer)));
    return true;
}

bool LiveDataPipeline::try_receive_latest(DynamicMeasurementFrame *frame)
{
    return frame != nullptr && ui_queue_ != nullptr && xQueueReceive(ui_queue_, frame, 0) == pdPASS;
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
        pipeline->analyze(raw, pipeline->analysis_frame_);
        pipeline->analyzed_frames_.fetch_add(1, std::memory_order_relaxed);
        if (xQueueOverwrite(pipeline->ui_queue_, pipeline->analysis_frame_) == pdPASS) {
            pipeline->published_frames_.fetch_add(1, std::memory_order_relaxed);
        }
    }
}

void LiveDataPipeline::analyze(const RawCaptureFrame &raw, DynamicMeasurementFrame *result)
{
    if (result == nullptr || spectrum_buffers_ == nullptr) {
        return;
    }

    *result = {};
    const uint8_t buffer_index = spectrum_write_index_;
    spectrum_write_index_ = static_cast<uint8_t>((spectrum_write_index_ + 1U) % kDenseSpectrumBufferCount);
    DenseSpectrumBuffer &dense = spectrum_buffers_[buffer_index];
    dense.generation = raw.sequence + 1U;

    // Until CSLP is connected, exercise the production-sized 8192-point
    // interface with a deterministic dense spectrum and several peaks. Full
    // bins never cross into the LVGL task.
    for (size_t bin = 0; bin < dense.magnitudes.size(); ++bin) {
        uint32_t noise = static_cast<uint32_t>(bin) * 747796405U + raw.sequence * 2891336453U + 277803737U;
        noise = ((noise >> ((noise >> 28U) + 4U)) ^ noise) * 277803737U;
        noise ^= noise >> 22U;
        const float unit_noise = static_cast<float>(noise & 0x3FFU) / 1023.0F;
        dense.magnitudes[bin] = 0.00035F + 0.00065F * unit_noise;
    }

    const float bin_width_hz = kSampleRateHz / static_cast<float>(kFftSize);
    const float fundamental_amplitude = 0.400F + 0.035F * sinf(raw.phase);
    const float second_harmonic_amplitude = 0.120F + 0.018F * sinf(raw.phase * 1.7F + 0.5F);
    const float third_harmonic_amplitude = 0.060F + 0.010F * sinf(raw.phase * 2.1F + 1.1F);
    const float requested_fundamental_hz = 40000.0F + 2000.0F * sinf(raw.phase * 0.31F);

    const auto frequency_to_bin = [bin_width_hz](float frequency_hz) {
        size_t bin = static_cast<size_t>(lroundf(frequency_hz / bin_width_hz));
        if (bin >= kPositiveSpectrumBins) {
            bin = kPositiveSpectrumBins - 1U;
        }
        return bin;
    };

    struct SimulatedPeak {
        size_t bin;
        float amplitude_volts_peak;
    };
    const std::array<SimulatedPeak, 6> simulated_peaks = {{
        {frequency_to_bin(requested_fundamental_hz), fundamental_amplitude},
        {frequency_to_bin(2.0F * requested_fundamental_hz), second_harmonic_amplitude},
        {frequency_to_bin(3.0F * requested_fundamental_hz), third_harmonic_amplitude},
        {frequency_to_bin(275000.0F), 0.030F},
        {frequency_to_bin(1200000.0F), 0.100F},
        {frequency_to_bin(1550000.0F), 0.018F},
    }};
    const size_t active_peak_count = 5U + ((raw.sequence / 40U) & 1U);
    constexpr std::array<float, 5> kPeakShape = {{0.06F, 0.22F, 1.0F, 0.22F, 0.06F}};
    for (size_t peak_index = 0; peak_index < active_peak_count; ++peak_index) {
        const SimulatedPeak &peak = simulated_peaks[peak_index];
        for (int offset = -2; offset <= 2; ++offset) {
            const int bin = static_cast<int>(peak.bin) + offset;
            if (bin < 0 || bin >= static_cast<int>(dense.magnitudes.size())) {
                continue;
            }
            const float shaped_amplitude = peak.amplitude_volts_peak * kPeakShape[static_cast<size_t>(offset + 2)];
            if (shaped_amplitude > dense.magnitudes[static_cast<size_t>(bin)]) {
                dense.magnitudes[static_cast<size_t>(bin)] = shaped_amplitude;
            }
        }
    }

    const float rms = sqrtf((fundamental_amplitude * fundamental_amplitude
                             + second_harmonic_amplitude * second_harmonic_amplitude
                             + third_harmonic_amplitude * third_harmonic_amplitude) / 2.0F);

    result->sequence = raw.sequence;
    result->capture_time_ms = raw.capture_time_ms;
    result->config_id = kSimulatedConfigId;
    result->voltage_peak_to_peak =
        2.0F * (fundamental_amplitude + second_harmonic_amplitude + third_harmonic_amplitude);
    result->true_rms_volts = rms;
    result->fundamental_hz = static_cast<float>(simulated_peaks[0].bin) * bin_width_hz;
    result->sample_rate_hz = kSampleRateHz;

    SpectrumDisplayFrame &spectrum = result->spectrum;
    spectrum.generation = dense.generation;
    spectrum.sample_rate_hz = static_cast<uint32_t>(kSampleRateHz);
    spectrum.fft_size = static_cast<uint16_t>(kFftSize);
    spectrum.column_count = static_cast<uint16_t>(kSpectrumDisplayColumns);
    spectrum.peak_count = static_cast<uint8_t>(active_peak_count);
    spectrum.source_buffer_index = buffer_index;
    spectrum.frequency_min_hz = 0.0F;
    spectrum.frequency_max_hz = kSampleRateHz / 2.0F;
    spectrum.bin_width_hz = bin_width_hz;
    spectrum.amplitude_max_volts = kMaximumDisplayAmplitude;

    for (size_t peak_index = 0; peak_index < active_peak_count; ++peak_index) {
        const SimulatedPeak &source = simulated_peaks[peak_index];
        spectrum.peaks[peak_index] = {
            .bin_index = static_cast<uint16_t>(source.bin),
            .frequency_hz = static_cast<float>(source.bin) * bin_width_hz,
            .amplitude_volts_peak = source.amplitude_volts_peak,
            .snr_db = 20.0F * log10f(source.amplitude_volts_peak / kNoiseReferenceVolts),
        };
    }

    float dense_maximum = 0.0F;
    float compressed_maximum = 0.0F;
    for (size_t column = 0; column < kSpectrumDisplayColumns; ++column) {
        const size_t first_bin = column * dense.magnitudes.size() / kSpectrumDisplayColumns;
        size_t end_bin = (column + 1U) * dense.magnitudes.size() / kSpectrumDisplayColumns;
        if (end_bin <= first_bin) {
            end_bin = first_bin + 1U;
        }

        float peak = 0.0F;
        float squares = 0.0F;
        for (size_t bin = first_bin; bin < end_bin; ++bin) {
            const float magnitude = dense.magnitudes[bin];
            if (magnitude > peak) {
                peak = magnitude;
            }
            if (magnitude > dense_maximum) {
                dense_maximum = magnitude;
            }
            squares += magnitude * magnitude;
        }
        spectrum.columns[column] = {
            .peak_volts = peak,
            .rms_volts = sqrtf(squares / static_cast<float>(end_bin - first_bin)),
        };
        if (peak > compressed_maximum) {
            compressed_maximum = peak;
        }
    }

    if (raw.sequence < 2U) {
        const bool peak_preserved = fabsf(dense_maximum - compressed_maximum) < 0.000001F;
        ESP_LOGI(kTag,
                 "Spectrum frame: A/B=%u gen=%lu FFT=%u bins=%u columns=%u peaks=%u df=%.6fHz "
                 "axis=0..%.5fMHz peak-preservation=%s",
                 buffer_index, static_cast<unsigned long>(spectrum.generation), spectrum.fft_size,
                 static_cast<unsigned>(dense.magnitudes.size()), spectrum.column_count, spectrum.peak_count,
                 static_cast<double>(spectrum.bin_width_hz),
                 static_cast<double>(spectrum.frequency_max_hz / 1000000.0F), peak_preserved ? "PASS" : "FAIL");
    }
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
    if (spectrum_buffers_ != nullptr) {
        heap_caps_free(spectrum_buffers_);
        spectrum_buffers_ = nullptr;
    }
    if (analysis_frame_ != nullptr) {
        heap_caps_free(analysis_frame_);
        analysis_frame_ = nullptr;
    }
}

}  // namespace cyclescope
