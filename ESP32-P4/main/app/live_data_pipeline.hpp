#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <type_traits>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "cslp_udp_receiver.hpp"
#include "fft_processor.hpp"
#include "spectrum_frame.hpp"
#include "waveform_frame.hpp"

namespace cyclescope {

namespace startup_fault_test {
bool run_pipeline_startup_fault_matrix();
}

// This POD travels across FreeRTOS queues by value. It deliberately contains
// no LVGL types: producer and analysis tasks must never touch the UI.
struct DynamicMeasurementFrame {
    uint32_t generation;
    uint32_t session_id;
    uint32_t frame_id;
    uint64_t source_timestamp_us;
    uint32_t config_id;
    uint32_t stream_epoch;
    uint32_t p4_response_profile_id;
    uint16_t calibration_id;
    uint16_t source_flags;
    bool frequency_response_compensated;
    float voltage_peak_to_peak;
    float true_rms_volts;
    float fundamental_hz;
    float sample_rate_hz;
    uint32_t analysis_time_us;
    SpectrumDisplayFrame spectrum;
    WaveformDisplayFrame waveform;
};

static_assert(std::is_trivially_copyable_v<DynamicMeasurementFrame>);

struct PipelineStats {
    uint32_t acquired_frames;
    uint32_t analyzed_frames;
    uint32_t published_frames;
    uint32_t stale_results;
    uint32_t invalid_frames;
    uint32_t fft_failures;
    uint32_t ui_overwrites;
    uint32_t last_analysis_us;
    uint32_t average_analysis_us;
    uint32_t maximum_analysis_us;
    bool fft_self_test_passed;
};

class LiveDataPipeline {
public:
    bool prepare(
        const FrequencyResponseProfile *response_profile = nullptr);
    bool start(CslpUdpReceiver *receiver);
    bool stream_ready() const;
    bool try_receive_latest(DynamicMeasurementFrame *frame);
    PipelineStats stats() const;

private:
    friend bool startup_fault_test::run_pipeline_startup_fault_matrix();

    enum class PreparationState : uint8_t {
        Unprepared,
        Preparing,
        Prepared,
        Failed,
    };

    enum class AnalysisOutcome {
        Success,
        InvalidFrame,
        FftFailure,
    };

    static void analysis_task(void *context);
    AnalysisOutcome analyze(const CslpUdpReceiver::FrameView &view,
                            uint32_t generation,
                            float previous_spectrum_amplitude_max_volts,
                            DynamicMeasurementFrame *result);
    bool run_startup_self_test();
    static void generate_test_samples(int16_t *samples);
    static void generate_exact_weak_test_samples(int16_t *samples);
    static bool validate_self_test(const FftAnalysisResult &result);
    static bool validate_exact_weak_self_test(const FftAnalysisResult &result);
    bool create_analysis_task(CslpUdpReceiver *receiver);
    bool resources_released() const;
    bool failed_preparation_is_clean() const;
    bool prepared_for_start_retry() const;
    void release_resources();

    QueueHandle_t ui_queue_ = nullptr;
    DynamicMeasurementFrame *analysis_frame_ = nullptr;
    TaskHandle_t analysis_task_handle_ = nullptr;
    CslpUdpReceiver *receiver_ = nullptr;
    FftProcessor8192 fft_processor_;
    const FrequencyResponseProfile *response_profile_ = nullptr;
    PreparationState preparation_state_ = PreparationState::Unprepared;
    std::atomic<uint32_t> acquired_frames_{0};
    std::atomic<uint32_t> analyzed_frames_{0};
    std::atomic<uint32_t> published_frames_{0};
    std::atomic<uint32_t> stale_results_{0};
    std::atomic<uint32_t> invalid_frames_{0};
    std::atomic<uint32_t> fft_failures_{0};
    std::atomic<uint32_t> ui_overwrites_{0};
    std::atomic<uint32_t> last_analysis_us_{0};
    std::atomic<uint32_t> average_analysis_us_{0};
    std::atomic<uint32_t> maximum_analysis_us_{0};
    std::atomic<bool> fft_self_test_passed_{false};
    uint64_t cumulative_analysis_us_ = 0;
    uint32_t last_profile_mismatch_session_id_ = 0;
    uint32_t last_profile_mismatch_config_id_ = 0;
};

}  // namespace cyclescope
