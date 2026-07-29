#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <type_traits>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "spectrum_frame.hpp"

namespace cyclescope {

// This POD travels across FreeRTOS queues by value.  It deliberately contains
// no LVGL types: receiver and analysis tasks must never touch the UI.
struct DynamicMeasurementFrame {
    uint32_t sequence;
    uint32_t capture_time_ms;
    uint32_t config_id;
    float voltage_peak_to_peak;
    float true_rms_volts;
    float fundamental_hz;
    float sample_rate_hz;
    SpectrumDisplayFrame spectrum;
};

static_assert(std::is_trivially_copyable_v<DynamicMeasurementFrame>);

struct PipelineStats {
    uint32_t received_frames;
    uint32_t analyzed_frames;
    uint32_t published_frames;
    uint32_t dropped_raw_frames;
};

class LiveDataPipeline {
public:
    bool start();
    bool try_receive_latest(DynamicMeasurementFrame *frame);
    PipelineStats stats() const;

private:
    static constexpr size_t kFftSize = 8192;
    static constexpr size_t kPositiveSpectrumBins = kFftSize / 2 + 1;

    struct RawCaptureFrame {
        uint32_t sequence;
        uint32_t capture_time_ms;
        float phase;
    };

    struct DenseSpectrumBuffer {
        uint32_t generation;
        std::array<float, kPositiveSpectrumBins> magnitudes;
    };

    static void receiver_task(void *context);
    static void analysis_task(void *context);
    void analyze(const RawCaptureFrame &raw, DynamicMeasurementFrame *result);
    void release_resources();

    QueueHandle_t raw_queue_ = nullptr;
    QueueHandle_t ui_queue_ = nullptr;
    DenseSpectrumBuffer *spectrum_buffers_ = nullptr;
    DynamicMeasurementFrame *analysis_frame_ = nullptr;
    TaskHandle_t receiver_task_handle_ = nullptr;
    TaskHandle_t analysis_task_handle_ = nullptr;
    uint8_t spectrum_write_index_ = 0;
    std::atomic<uint32_t> received_frames_{0};
    std::atomic<uint32_t> analyzed_frames_{0};
    std::atomic<uint32_t> published_frames_{0};
    std::atomic<uint32_t> dropped_raw_frames_{0};
};

}  // namespace cyclescope
