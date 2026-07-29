#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <type_traits>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "fft_processor.hpp"
#include "spectrum_frame.hpp"

namespace cyclescope {

// This POD travels across FreeRTOS queues by value. It deliberately contains
// no LVGL types: producer and analysis tasks must never touch the UI.
struct DynamicMeasurementFrame {
    uint32_t sequence;
    uint32_t capture_time_ms;
    uint32_t config_id;
    float voltage_peak_to_peak;
    float true_rms_volts;
    float fundamental_hz;
    float sample_rate_hz;
    uint32_t analysis_time_us;
    SpectrumDisplayFrame spectrum;
};

static_assert(std::is_trivially_copyable_v<DynamicMeasurementFrame>);

struct PipelineStats {
    uint32_t received_frames;
    uint32_t analyzed_frames;
    uint32_t published_frames;
    uint32_t dropped_raw_frames;
    uint32_t fft_failures;
    uint32_t last_analysis_us;
    uint32_t average_analysis_us;
    uint32_t maximum_analysis_us;
    bool fft_self_test_passed;
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
    };

    static void receiver_task(void *context);
    static void analysis_task(void *context);
    bool analyze(const RawCaptureFrame &raw, DynamicMeasurementFrame *result);
    bool allocate_test_samples();
    void generate_test_samples();
    static bool validate_self_test(const FftAnalysisResult &result);
    void release_resources();

    QueueHandle_t raw_queue_ = nullptr;
    QueueHandle_t ui_queue_ = nullptr;
    DynamicMeasurementFrame *analysis_frame_ = nullptr;
    TaskHandle_t receiver_task_handle_ = nullptr;
    TaskHandle_t analysis_task_handle_ = nullptr;
    FftProcessor8192 fft_processor_;
    int16_t *test_samples_ = nullptr;
    bool self_test_logged_ = false;
    std::atomic<uint32_t> received_frames_{0};
    std::atomic<uint32_t> analyzed_frames_{0};
    std::atomic<uint32_t> published_frames_{0};
    std::atomic<uint32_t> dropped_raw_frames_{0};
    std::atomic<uint32_t> fft_failures_{0};
    std::atomic<uint32_t> last_analysis_us_{0};
    std::atomic<uint32_t> average_analysis_us_{0};
    std::atomic<uint32_t> maximum_analysis_us_{0};
    std::atomic<bool> fft_self_test_passed_{false};
    uint64_t cumulative_analysis_us_ = 0;
};

}  // namespace cyclescope
