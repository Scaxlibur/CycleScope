#pragma once

#include <array>
#include <atomic>
#include <cstdint>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "spectrum_model.hpp"

namespace cyclescope {

// This POD travels across FreeRTOS queues by value.  It deliberately contains
// no LVGL types: receiver and analysis tasks must never touch the UI.
struct DynamicMeasurementFrame {
    uint32_t sequence;
    uint32_t capture_time_ms;
    float voltage_peak_to_peak;
    float true_rms_volts;
    float fundamental_hz;
    float sample_rate_hz;
    std::array<SpectralLine, SpectrumModel::kLineCount> spectral_lines;
};

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
    struct RawCaptureFrame {
        uint32_t sequence;
        uint32_t capture_time_ms;
        float phase;
    };

    static void receiver_task(void *context);
    static void analysis_task(void *context);
    DynamicMeasurementFrame analyze(const RawCaptureFrame &raw) const;

    QueueHandle_t raw_queue_ = nullptr;
    QueueHandle_t ui_queue_ = nullptr;
    TaskHandle_t receiver_task_handle_ = nullptr;
    TaskHandle_t analysis_task_handle_ = nullptr;
    std::atomic<uint32_t> received_frames_{0};
    std::atomic<uint32_t> analyzed_frames_{0};
    std::atomic<uint32_t> published_frames_{0};
    std::atomic<uint32_t> dropped_raw_frames_{0};
};

}  // namespace cyclescope
