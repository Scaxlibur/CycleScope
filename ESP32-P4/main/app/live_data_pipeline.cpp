#include "live_data_pipeline.hpp"

#include "sdkconfig.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <inttypes.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_memory_utils.h"
#include "freertos/idf_additions.h"

#include "spectrum_projection.hpp"
#include "waveform_projection.hpp"
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
#include "cyclescope_pipeline_startup_fault_test.hpp"
#endif

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_pipe";
constexpr TickType_t kAnalysisPollPeriod = pdMS_TO_TICKS(2);
constexpr UBaseType_t kAnalysisPriority = 4;
constexpr uint32_t kAnalysisStackBytes = 8192;
constexpr uint32_t kHealthLogFramePeriod = 600;
constexpr uint32_t kMeasurementLogFramePeriod = 100;
constexpr BaseType_t kDataCore = 1;
constexpr float kPi = 3.14159265358979323846F;
constexpr float kSelfTestSampleRateHz = 4062500.0F;
constexpr uint32_t kSelfTestScaleUvPerLsb = 100;
constexpr int32_t kSelfTestOffsetUv = 500;
constexpr float kNoiseReferenceVolts = 0.00075F;
constexpr float kPhaseSelfTestToleranceRadians = 0.01F;
static_assert(kSpectrumDisplayMaximumHz
              == FftProcessor8192::kMaximumMeasurementHz);
// Keep these defaults synchronized with tools/generate_fft_test_vector.py.
constexpr float kTestFundamentalHz = 40750.0F;
constexpr std::array<uint16_t, kMaximumSpectralLines> kTestHarmonics = {1, 3, 4};
constexpr std::array<float, kMaximumSpectralLines> kTestAmplitudesVolts = {
    0.025F, 0.070F, 0.025F
};
constexpr std::array<float, kMaximumSpectralLines> kTestPhasesRadians = {
    0.17F, 0.92F, -0.51F
};
constexpr float kTestExpectedPeakToPeakVolts = 0.181421109F;
constexpr double kExactWeakFundamentalHz = 10000.0;
constexpr std::array<uint16_t, 2> kExactWeakHarmonics = {1, 2};
constexpr std::array<double, 2> kExactWeakAmplitudesVolts = {
    0.0055363321799308,
    0.0221453287197232,
};
constexpr std::array<double, 2> kExactWeakPhasesRadians = {
    0.0,
    -3.14159265358979323846 / 2.0,
};
constexpr float kExactWeakExpectedPeakToPeakVolts = 0.050F;
constexpr float kExactWeakExpectedRmsVolts = 0.016141F;
constexpr uint32_t kExactWeakRawCrc32 = 0x4ECFD324U;

uint32_t calculate_raw_sample_crc32(const int16_t *samples)
{
    const auto *bytes = reinterpret_cast<const uint8_t *>(samples);
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t index = 0;
         index < FftProcessor8192::kSampleCount * sizeof(int16_t);
         ++index) {
        crc ^= bytes[index];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1U)
                  ^ ((crc & 1U) != 0U ? 0xEDB88320U : 0U);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

bool phase_matches(float actual_radians, float expected_radians)
{
    return std::isfinite(actual_radians)
           && std::fabs(std::remainder(
                  actual_radians - expected_radians, 2.0F * kPi))
                  <= kPhaseSelfTestToleranceRadians;
}

void *allocate_sample_buffer(size_t bytes)
{
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_pipeline_failpoint(
            startup_fault_test::PipelineFailPoint::SelfTestSampleBuffer)) {
        return nullptr;
    }
#endif
    void *buffer =
        heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == nullptr) {
        buffer = heap_caps_aligned_alloc(
            16, bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    return buffer;
}

DynamicMeasurementFrame *allocate_analysis_frame()
{
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_pipeline_failpoint(
            startup_fault_test::PipelineFailPoint::AnalysisFrame)) {
        return nullptr;
    }
#endif
    void *buffer = heap_caps_calloc(
        1, sizeof(DynamicMeasurementFrame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == nullptr) {
        buffer = heap_caps_calloc(
            1, sizeof(DynamicMeasurementFrame), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    return static_cast<DynamicMeasurementFrame *>(buffer);
}

QueueHandle_t create_ui_queue()
{
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_pipeline_failpoint(
            startup_fault_test::PipelineFailPoint::UiQueue)) {
        return nullptr;
    }
#endif
    return xQueueCreate(1, sizeof(DynamicMeasurementFrame));
}

class FrameLease {
public:
    explicit FrameLease(CslpUdpReceiver &receiver) : receiver_(receiver)
    {
    }

    ~FrameLease()
    {
        if (acquired_) {
            receiver_.release(&view_);
        }
    }

    FrameLease(const FrameLease &) = delete;
    FrameLease &operator=(const FrameLease &) = delete;

    bool acquire(const CslpUdpReceiver::FrameCursor &after)
    {
        acquired_ = receiver_.acquire_latest(after, &view_);
        return acquired_;
    }

    const CslpUdpReceiver::FrameView &view() const
    {
        return view_;
    }

private:
    CslpUdpReceiver &receiver_;
    CslpUdpReceiver::FrameView view_{};
    bool acquired_ = false;
};

uint32_t next_nonzero(uint32_t value)
{
    ++value;
    if (value == 0) {
        ++value;
    }
    return value;
}

}  // namespace

bool LiveDataPipeline::prepare()
{
#ifdef CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER
    ESP_LOGE(kTag,
             "Formal CSLP analysis is disabled while the diagnostic consumer is enabled");
    preparation_state_ = PreparationState::Failed;
    return false;
#endif

    if (preparation_state_ == PreparationState::Prepared) {
        return true;
    }
    if (preparation_state_ != PreparationState::Unprepared
        || analysis_task_handle_ != nullptr || ui_queue_ != nullptr
        || analysis_frame_ != nullptr || receiver_ != nullptr) {
        ESP_LOGE(kTag,
                 "Pipeline preparation is not in a clean initial state");
        preparation_state_ = PreparationState::Failed;
        return false;
    }

    preparation_state_ = PreparationState::Preparing;
    const auto fail_prepare = [this]() {
        release_resources();
        preparation_state_ = PreparationState::Failed;
        return false;
    };

    const esp_err_t fft_error = fft_processor_.initialize();
    if (fft_error != ESP_OK) {
        ESP_LOGE(kTag, "Unable to initialize 8192-point FFT: %s",
                 esp_err_to_name(fft_error));
        return fail_prepare();
    }
    if (!run_startup_self_test()) {
        ESP_LOGE(kTag, "FFT startup self-test failed; refusing to publish measurements");
        return fail_prepare();
    }

    analysis_frame_ = allocate_analysis_frame();
    if (analysis_frame_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate the analysis/display frame");
        return fail_prepare();
    }
    ui_queue_ = create_ui_queue();
    if (ui_queue_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate the latest-result UI queue");
        return fail_prepare();
    }

    preparation_state_ = PreparationState::Prepared;
    ESP_LOGI(kTag,
             "Formal CSLP FFT pipeline prepared; result=%u bytes",
             static_cast<unsigned>(sizeof(DynamicMeasurementFrame)));
    return true;
}

bool LiveDataPipeline::start(CslpUdpReceiver *receiver)
{
    if (analysis_task_handle_ != nullptr) {
        return receiver_ == receiver && receiver_ != nullptr
               && ui_queue_ != nullptr && analysis_frame_ != nullptr;
    }
    if (receiver_ != nullptr) {
        ESP_LOGE(kTag,
                 "Pipeline has a receiver without an analysis task; refusing restart");
        return false;
    }
    if (receiver == nullptr || !receiver->started()) {
        ESP_LOGE(kTag, "Formal CSLP analysis requires a started receiver");
        return false;
    }
    if (!prepare()) {
        return false;
    }

    if (!create_analysis_task(receiver)) {
        return false;
    }

    ESP_LOGI(kTag,
             "Formal CSLP pipeline started on Core %d: mailbox consumer prio %u "
             "-> latest UI queue",
             kDataCore, kAnalysisPriority);
    return true;
}

bool LiveDataPipeline::create_analysis_task(CslpUdpReceiver *receiver)
{
    receiver_ = receiver;
    BaseType_t task_result = pdFAIL;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (!startup_fault_test::consume_pipeline_failpoint(
            startup_fault_test::PipelineFailPoint::AnalysisTask)) {
#endif
        task_result = xTaskCreatePinnedToCore(
            analysis_task, "cs_analyze", kAnalysisStackBytes, this,
            kAnalysisPriority, &analysis_task_handle_, kDataCore);
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    }
#endif
    if (task_result != pdPASS) {
        ESP_LOGE(kTag, "Unable to start CSLP FFT analysis task");
        analysis_task_handle_ = nullptr;
        receiver_ = nullptr;
        return false;
    }
    return true;
}

bool LiveDataPipeline::run_startup_self_test()
{
    auto *samples = static_cast<int16_t *>(
        allocate_sample_buffer(FftProcessor8192::kSampleCount * sizeof(int16_t)));
    if (samples == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate the temporary FFT self-test frame");
        return false;
    }

    generate_test_samples(samples);
    FftAnalysisResult primary_result{};
    const esp_err_t primary_error = fft_processor_.process(
        samples, FftProcessor8192::kSampleCount, kSelfTestSampleRateHz,
        kSelfTestScaleUvPerLsb, kSelfTestOffsetUv, &primary_result);
    const bool primary_passed =
        primary_error == ESP_OK && validate_self_test(primary_result);
    ESP_LOGI(kTag,
             "FFT startup self-test: F0=%.2fHz Vpp=%.3fmV RMS=%.3fmV "
             "elapsed=%" PRIu32 "us %s",
             static_cast<double>(primary_result.fundamental_hz),
             static_cast<double>(primary_result.voltage_peak_to_peak * 1000.0F),
             static_cast<double>(primary_result.true_rms_volts * 1000.0F),
             primary_result.analysis_time_us,
             primary_passed ? "PASS" : "FAIL");

    generate_exact_weak_test_samples(samples);
    const uint32_t exact_raw_crc32 = calculate_raw_sample_crc32(samples);
    FftAnalysisResult exact_result{};
    const esp_err_t exact_error = fft_processor_.process(
        samples, FftProcessor8192::kSampleCount, kSelfTestSampleRateHz,
        kSelfTestScaleUvPerLsb, kSelfTestOffsetUv, &exact_result);
    const bool exact_passed =
        exact_error == ESP_OK && exact_raw_crc32 == kExactWeakRawCrc32
        && validate_exact_weak_self_test(exact_result);
    const bool heap_integrity_passed = heap_caps_check_integrity_all(true);
    bool passed = primary_passed && exact_passed && heap_integrity_passed;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_pipeline_failpoint(
            startup_fault_test::PipelineFailPoint::StartupSelfTestGate)) {
        passed = false;
    }
#endif
    fft_self_test_passed_.store(passed, std::memory_order_relaxed);
    ESP_LOGI(kTag,
             "FFT exact weak startup self-test: crc=0x%08" PRIX32
             " F0=%.2fHz Vpp=%.3fmV RMS=%.3fmV lines=%" PRIu32
             " P1=%.3fmVpk P2=%.3fmVpk elapsed=%" PRIu32
             "us heap=%s %s",
             exact_raw_crc32,
             static_cast<double>(exact_result.fundamental_hz),
             static_cast<double>(exact_result.voltage_peak_to_peak * 1000.0F),
             static_cast<double>(exact_result.true_rms_volts * 1000.0F),
             exact_result.spectral_line_count,
             static_cast<double>(
                 exact_result.spectral_lines[0].amplitude_volts_peak * 1000.0F),
             static_cast<double>(
                 exact_result.spectral_lines[1].amplitude_volts_peak * 1000.0F),
             exact_result.analysis_time_us,
             heap_integrity_passed ? "PASS" : "FAIL",
             exact_passed ? "PASS" : "FAIL");
    heap_caps_free(samples);
    return passed;
}

void LiveDataPipeline::generate_test_samples(int16_t *samples)
{
    const float volts_per_lsb =
        static_cast<float>(kSelfTestScaleUvPerLsb) * 1.0e-6F;
    const float offset_volts =
        static_cast<float>(kSelfTestOffsetUv) * 1.0e-6F;
    for (size_t sample = 0; sample < FftProcessor8192::kSampleCount; ++sample) {
        const float time_seconds =
            static_cast<float>(sample) / kSelfTestSampleRateHz;
        float voltage = 0.0F;
        for (size_t line = 0; line < kMaximumSpectralLines; ++line) {
            const float phase =
                2.0F * kPi * kTestFundamentalHz
                    * static_cast<float>(kTestHarmonics[line]) * time_seconds
                + kTestPhasesRadians[line];
            voltage += kTestAmplitudesVolts[line] * std::sin(phase);
        }
        const long code =
            std::lround((voltage - offset_volts) / volts_per_lsb);
        samples[sample] =
            static_cast<int16_t>(std::max(-2048L, std::min(2047L, code)));
    }
}

void LiveDataPipeline::generate_exact_weak_test_samples(int16_t *samples)
{
    const double volts_per_lsb =
        static_cast<double>(kSelfTestScaleUvPerLsb) * 1.0e-6;
    const double offset_volts =
        static_cast<double>(kSelfTestOffsetUv) * 1.0e-6;
    for (size_t sample = 0; sample < FftProcessor8192::kSampleCount; ++sample) {
        const double time_seconds =
            static_cast<double>(sample)
            / static_cast<double>(kSelfTestSampleRateHz);
        double voltage = 0.0;
        for (size_t line = 0; line < kExactWeakHarmonics.size(); ++line) {
            const double phase =
                2.0 * 3.14159265358979323846 * kExactWeakFundamentalHz
                    * static_cast<double>(kExactWeakHarmonics[line])
                    * time_seconds
                + kExactWeakPhasesRadians[line];
            voltage += kExactWeakAmplitudesVolts[line] * std::sin(phase);
        }
        const long code =
            std::lround((voltage - offset_volts) / volts_per_lsb);
        samples[sample] =
            static_cast<int16_t>(std::max(-2048L, std::min(2047L, code)));
    }
}

bool LiveDataPipeline::try_receive_latest(DynamicMeasurementFrame *frame)
{
    if (frame == nullptr || ui_queue_ == nullptr
        || xQueueReceive(ui_queue_, frame, 0) != pdPASS) {
        return false;
    }
    if (receiver_ == nullptr
        || !receiver_->stream_is_current(frame->session_id, frame->config_id,
                                         frame->stream_epoch)) {
        stale_results_.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGW(kTag,
                 "Discarded stale queued result: session=%08" PRIX32
                 " frame=%" PRIu32 " config=%" PRIu32
                 " epoch=%" PRIu32,
                 frame->session_id, frame->frame_id, frame->config_id,
                 frame->stream_epoch);
        *frame = {};
        return false;
    }
    return true;
}

bool LiveDataPipeline::stream_ready() const
{
    // This is transport readiness only. InstrumentApp independently checks
    // the age of the last successfully applied current frame before showing
    // retained data as LIVE.
    CslpUdpReceiver *const receiver = receiver_;
    return receiver != nullptr && receiver->session_ready();
}

PipelineStats LiveDataPipeline::stats() const
{
    return {
        .acquired_frames =
            acquired_frames_.load(std::memory_order_relaxed),
        .analyzed_frames =
            analyzed_frames_.load(std::memory_order_relaxed),
        .published_frames =
            published_frames_.load(std::memory_order_relaxed),
        .stale_results =
            stale_results_.load(std::memory_order_relaxed),
        .invalid_frames =
            invalid_frames_.load(std::memory_order_relaxed),
        .fft_failures =
            fft_failures_.load(std::memory_order_relaxed),
        .ui_overwrites =
            ui_overwrites_.load(std::memory_order_relaxed),
        .last_analysis_us =
            last_analysis_us_.load(std::memory_order_relaxed),
        .average_analysis_us =
            average_analysis_us_.load(std::memory_order_relaxed),
        .maximum_analysis_us =
            maximum_analysis_us_.load(std::memory_order_relaxed),
        .fft_self_test_passed =
            fft_self_test_passed_.load(std::memory_order_relaxed),
    };
}

void LiveDataPipeline::analysis_task(void *context)
{
    auto *pipeline = static_cast<LiveDataPipeline *>(context);
    CslpUdpReceiver::FrameCursor cursor{};
    uint32_t publish_generation = 0;
    uint32_t scale_session_id = 0;
    uint32_t scale_config_id = 0;
    uint32_t scale_stream_epoch = 0;
    float committed_spectrum_amplitude_max_volts = 0.0F;
    uint32_t last_logged_session_id = 0;
    uint32_t last_logged_config_id = 0;

    ESP_LOGI(kTag, "CSLP 8192-point analysis consumer running on Core %d",
             xPortGetCoreID());

    while (true) {
        FrameLease lease(*pipeline->receiver_);
        if (!lease.acquire(cursor)) {
            vTaskDelay(kAnalysisPollPeriod);
            continue;
        }

        const CslpUdpReceiver::FrameView &view = lease.view();
        cursor = view.cursor();
        pipeline->acquired_frames_.fetch_add(1, std::memory_order_relaxed);
        const uint32_t candidate_generation = next_nonzero(publish_generation);
        const bool continuing_scale_stream =
            committed_spectrum_amplitude_max_volts != 0.0F
            && view.metadata.session_id == scale_session_id
            && view.metadata.config_id == scale_config_id
            && view.stream_epoch == scale_stream_epoch;
        const float previous_spectrum_amplitude_max_volts =
            continuing_scale_stream
                ? committed_spectrum_amplitude_max_volts
                : 0.0F;
        const AnalysisOutcome outcome =
            pipeline->analyze(view, candidate_generation,
                              previous_spectrum_amplitude_max_volts,
                              pipeline->analysis_frame_);
        if (outcome == AnalysisOutcome::FftFailure) {
            pipeline->fft_failures_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }
        if (outcome == AnalysisOutcome::InvalidFrame) {
            pipeline->invalid_frames_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }

        const uint32_t analyzed_frames =
            pipeline->analyzed_frames_.fetch_add(1, std::memory_order_relaxed)
            + 1U;
        pipeline->last_analysis_us_.store(
            pipeline->analysis_frame_->analysis_time_us,
            std::memory_order_relaxed);
        pipeline->cumulative_analysis_us_ +=
            pipeline->analysis_frame_->analysis_time_us;
        pipeline->average_analysis_us_.store(
            static_cast<uint32_t>(
                pipeline->cumulative_analysis_us_ / analyzed_frames),
            std::memory_order_relaxed);
        uint32_t previous_maximum =
            pipeline->maximum_analysis_us_.load(std::memory_order_relaxed);
        while (pipeline->analysis_frame_->analysis_time_us > previous_maximum
               && !pipeline->maximum_analysis_us_.compare_exchange_weak(
                   previous_maximum,
                   pipeline->analysis_frame_->analysis_time_us,
                   std::memory_order_relaxed)) {
        }

        // The test-only latch deterministically places an old-config InUse
        // frame on the far side of a same-session reconfiguration.
#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
        pipeline->receiver_->synchronize_disable_push_test(view);
#endif

        // This check must remain immediately before publishing. A reconnect
        // or same-session reconfiguration may invalidate the stream identity
        // while the FFT is in progress.
        if (!pipeline->receiver_->frame_is_current(view)) {
            pipeline->stale_results_.fetch_add(1, std::memory_order_relaxed);
            ESP_LOGW(kTag,
                     "Discarded stale analysis result: session=%08" PRIX32
                     " frame=%" PRIu32 " config=%" PRIu32
                     " epoch=%" PRIu32,
                     view.metadata.session_id, view.metadata.frame_id,
                     view.metadata.config_id, view.stream_epoch);
            continue;
        }
        if (uxQueueMessagesWaiting(pipeline->ui_queue_) != 0) {
            pipeline->ui_overwrites_.fetch_add(1, std::memory_order_relaxed);
        }
        if (xQueueOverwrite(pipeline->ui_queue_,
                            pipeline->analysis_frame_) != pdPASS) {
            pipeline->invalid_frames_.fetch_add(1, std::memory_order_relaxed);
            ESP_LOGE(kTag, "Unable to publish analysis result to UI queue");
            continue;
        }

        const float candidate_spectrum_amplitude_max_volts =
            pipeline->analysis_frame_->spectrum.amplitude_max_volts;
        const bool spectrum_scale_changed =
            !continuing_scale_stream
            || candidate_spectrum_amplitude_max_volts
                   != committed_spectrum_amplitude_max_volts;
        const char *const spectrum_scale_reason =
            !continuing_scale_stream
                ? "NEW_STREAM"
                : (candidate_spectrum_amplitude_max_volts
                           > committed_spectrum_amplitude_max_volts
                       ? "UPSHIFT"
                       : "DOWNSHIFT");

        publish_generation = candidate_generation;
        scale_session_id = view.metadata.session_id;
        scale_config_id = view.metadata.config_id;
        scale_stream_epoch = view.stream_epoch;
        committed_spectrum_amplitude_max_volts =
            candidate_spectrum_amplitude_max_volts;
        if (spectrum_scale_changed) {
            ESP_LOGI(kTag,
                     "Spectrum scale committed: session=%08" PRIX32
                     " config=%08" PRIX32 " epoch=%" PRIu32
                     " frame=%" PRIu32
                     " previous=%.1fmVpk Amax=%.1fmVpk reason=%s",
                     view.metadata.session_id, view.metadata.config_id,
                     view.stream_epoch, view.metadata.frame_id,
                     static_cast<double>(
                         previous_spectrum_amplitude_max_volts * 1000.0F),
                     static_cast<double>(
                         candidate_spectrum_amplitude_max_volts * 1000.0F),
                     spectrum_scale_reason);
        }
        const uint32_t published_frames =
            pipeline->published_frames_.fetch_add(1,
                                                  std::memory_order_relaxed)
            + 1U;
        const DynamicMeasurementFrame &frame =
            *pipeline->analysis_frame_;
        if (frame.session_id != last_logged_session_id
            || frame.config_id != last_logged_config_id
            || published_frames == 1U
            || published_frames % kMeasurementLogFramePeriod == 0U) {
            last_logged_session_id = frame.session_id;
            last_logged_config_id = frame.config_id;
            ESP_LOGI(kTag,
                     "measurement: session=%08" PRIX32
                     " config=%08" PRIX32 " epoch=%" PRIu32
                     " frame=%" PRIu32 " gen=%" PRIu32
                     " F0=%.2fHz Vpp=%.3fmV RMS=%.3fmV peaks=%u "
                     "P1=%.2fHz/%.3fmVpk P2=%.2fHz/%.3fmVpk "
                     "P3=%.2fHz/%.3fmVpk cal=%u test=%u",
                     frame.session_id, frame.config_id, frame.stream_epoch,
                     frame.frame_id, frame.generation,
                     static_cast<double>(frame.fundamental_hz),
                     static_cast<double>(
                         frame.voltage_peak_to_peak * 1000.0F),
                     static_cast<double>(
                         frame.true_rms_volts * 1000.0F),
                     frame.spectrum.peak_count,
                     static_cast<double>(
                         frame.spectrum.peaks[0].frequency_hz),
                     static_cast<double>(
                         frame.spectrum.peaks[0].amplitude_volts_peak
                         * 1000.0F),
                     static_cast<double>(
                         frame.spectrum.peaks[1].frequency_hz),
                     static_cast<double>(
                         frame.spectrum.peaks[1].amplitude_volts_peak
                         * 1000.0F),
                     static_cast<double>(
                         frame.spectrum.peaks[2].frequency_hz),
                     static_cast<double>(
                         frame.spectrum.peaks[2].amplitude_volts_peak
                         * 1000.0F),
                     (frame.source_flags & cslp::kFlagCalibrated) != 0,
                     (frame.source_flags & cslp::kFlagTestPattern) != 0);
        }
        if (analyzed_frames % kHealthLogFramePeriod == 0U) {
            const PipelineStats stats = pipeline->stats();
            ESP_LOGI(kTag,
                     "health: acquired=%" PRIu32 " analyzed=%" PRIu32
                     " published=%" PRIu32 " stale=%" PRIu32
                     " invalid=%" PRIu32 " fft_fail=%" PRIu32
                     " ui_overwrite=%" PRIu32
                     " fft_us(last/avg/max)=%" PRIu32 "/%" PRIu32
                     "/%" PRIu32 " internal_free=%u psram_free=%u",
                     stats.acquired_frames, stats.analyzed_frames,
                     stats.published_frames, stats.stale_results,
                     stats.invalid_frames, stats.fft_failures,
                     stats.ui_overwrites, stats.last_analysis_us,
                     stats.average_analysis_us, stats.maximum_analysis_us,
                     static_cast<unsigned>(
                         heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                     static_cast<unsigned>(
                         heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        }
    }
}

LiveDataPipeline::AnalysisOutcome LiveDataPipeline::analyze(
    const CslpUdpReceiver::FrameView &view, uint32_t generation,
    float previous_spectrum_amplitude_max_volts,
    DynamicMeasurementFrame *result)
{
    if (result == nullptr || view.samples == nullptr
        || view.sample_count != FftProcessor8192::kSampleCount
        || view.metadata.sample_count != FftProcessor8192::kSampleCount
        || view.metadata.sample_rate_hz != CslpUdpReceiver::kSampleRateHz
        || view.metadata.scale_uv_per_lsb == 0
        || view.metadata.config_id == 0
        || view.metadata.filter_profile != CslpUdpReceiver::kFilterProfile
        || (view.metadata.flags & cslp::kFlagFiltered) == 0
        || (view.metadata.flags
            & (cslp::kFlagAdcOverrange | cslp::kFlagFifoOverflow))
               != 0) {
        return AnalysisOutcome::InvalidFrame;
    }

    FftAnalysisResult fft_result{};
    const esp_err_t error = fft_processor_.process(
        view.samples, view.sample_count,
        static_cast<float>(view.metadata.sample_rate_hz),
        view.metadata.scale_uv_per_lsb, view.metadata.offset_uv,
        &fft_result);
    if (error != ESP_OK) {
        ESP_LOGE(kTag, "FFT failed for session=%08" PRIX32
                 " frame=%" PRIu32 ": %s",
                 view.metadata.session_id, view.metadata.frame_id,
                 esp_err_to_name(error));
        return AnalysisOutcome::FftFailure;
    }
    if (!fft_result.valid
        || fft_processor_.positive_spectrum_size()
               != FftProcessor8192::kPositiveBinCount) {
        ESP_LOGW(kTag,
                 "Measurement rejected: session=%08" PRIX32
                 " frame=%" PRIu32 " valid=%u lines=%" PRIu32,
                 view.metadata.session_id, view.metadata.frame_id,
                 fft_result.valid, fft_result.spectral_line_count);
        return AnalysisOutcome::InvalidFrame;
    }

    *result = {};
    result->generation = generation;
    result->session_id = view.metadata.session_id;
    result->frame_id = view.metadata.frame_id;
    result->source_timestamp_us = view.metadata.timestamp_us;
    result->config_id = view.metadata.config_id;
    result->stream_epoch = view.stream_epoch;
    result->calibration_id = view.metadata.calibration_id;
    result->source_flags = view.metadata.flags;
    result->voltage_peak_to_peak = fft_result.voltage_peak_to_peak;
    result->true_rms_volts = fft_result.true_rms_volts;
    result->fundamental_hz = fft_result.fundamental_hz;
    result->sample_rate_hz = fft_result.sample_rate_hz;
    result->analysis_time_us = fft_result.analysis_time_us;

    SpectrumDisplayFrame &spectrum = result->spectrum;
    spectrum.generation = generation;
    spectrum.sample_rate_hz =
        static_cast<uint32_t>(fft_result.sample_rate_hz);
    spectrum.fft_size =
        static_cast<uint16_t>(FftProcessor8192::kSampleCount);
    spectrum.peak_count = static_cast<uint8_t>(
        std::min<uint32_t>(fft_result.displayed_spectral_line_count,
                           std::min<size_t>(kMaximumDisplayedSpectralLines,
                                            kMaximumSpectralPeaks)));
    spectrum.source_buffer_index = 0xFF;

    for (size_t peak_index = 0; peak_index < spectrum.peak_count;
         ++peak_index) {
        const SpectralLine &source =
            fft_result.displayed_spectral_lines[peak_index];
        const long rounded_bin =
            std::lround(source.frequency_hz / fft_result.bin_width_hz);
        const size_t bin = std::min<size_t>(
            FftProcessor8192::kPositiveBinCount - 1U,
            static_cast<size_t>(std::max(0L, rounded_bin)));
        spectrum.peaks[peak_index] = {
            .bin_index = static_cast<uint16_t>(bin),
            .frequency_hz = source.frequency_hz,
            .amplitude_volts_peak = source.amplitude_volts_peak,
            .snr_db = 20.0F * std::log10(
                source.amplitude_volts_peak / kNoiseReferenceVolts),
        };
    }

    if (!project_spectrum_for_display(
            fft_processor_.positive_spectrum(),
            fft_processor_.positive_spectrum_size(),
            fft_result.bin_width_hz, &spectrum)) {
        ESP_LOGW(kTag,
                 "Spectrum display projection rejected session=%08" PRIX32
                 " frame=%" PRIu32,
                 view.metadata.session_id, view.metadata.frame_id);
        return AnalysisOutcome::InvalidFrame;
    }
    if (!choose_spectrum_amplitude_max(
            spectrum, previous_spectrum_amplitude_max_volts,
            &spectrum.amplitude_max_volts)) {
        ESP_LOGW(kTag,
                 "Spectrum amplitude scaling rejected session=%08" PRIX32
                 " frame=%" PRIu32,
                 view.metadata.session_id, view.metadata.frame_id);
        return AnalysisOutcome::InvalidFrame;
    }

    if (!project_waveform(
            view.samples, view.sample_count,
            view.metadata.scale_uv_per_lsb, view.metadata.offset_uv,
            fft_result.dc_offset_volts, fft_result.sample_rate_hz,
            fft_result.fundamental_hz,
            fft_result.fundamental_phase_radians, generation,
            fft_result.voltage_peak_to_peak, fft_result.true_rms_volts,
            &result->waveform)) {
        ESP_LOGW(kTag,
                 "Waveform projection rejected session=%08" PRIX32
                 " frame=%" PRIu32,
                 view.metadata.session_id, view.metadata.frame_id);
        return AnalysisOutcome::InvalidFrame;
    }
    return AnalysisOutcome::Success;
}

bool LiveDataPipeline::validate_self_test(
    const FftAnalysisResult &result)
{
    if (!result.valid
        || result.spectral_line_count != kMaximumSpectralLines
        || result.displayed_spectral_line_count != kMaximumSpectralLines
        || std::fabs(result.fundamental_hz - kTestFundamentalHz) > 1000.0F
        || !phase_matches(result.fundamental_phase_radians,
                          kTestPhasesRadians[0])
        || std::fabs(result.bin_width_hz - 495.91064453125F) > 0.01F
        || std::fabs(result.voltage_peak_to_peak
                     - kTestExpectedPeakToPeakVolts) > 0.005F
        || result.analysis_time_us >= 50000U) {
        return false;
    }

    float expected_rms_square = 0.0F;
    for (size_t line = 0; line < kMaximumSpectralLines; ++line) {
        if (result.spectral_lines[line].harmonic_order
                != kTestHarmonics[line]
            || result.displayed_spectral_lines[line].harmonic_order
                   != kTestHarmonics[line]
            || std::fabs(
                   result.spectral_lines[line].amplitude_volts_peak
                   - kTestAmplitudesVolts[line])
                   > 0.005F
            || std::fabs(
                   result.displayed_spectral_lines[line]
                           .amplitude_volts_peak
                   - kTestAmplitudesVolts[line])
                   > 0.005F) {
            return false;
        }
        expected_rms_square +=
            kTestAmplitudesVolts[line] * kTestAmplitudesVolts[line]
            * 0.5F;
    }
    return std::fabs(result.true_rms_volts
                     - std::sqrt(expected_rms_square))
           <= 0.005F;
}

bool LiveDataPipeline::validate_exact_weak_self_test(
    const FftAnalysisResult &result)
{
    if (!result.valid
        || result.spectral_line_count != kExactWeakHarmonics.size()
        || result.displayed_spectral_line_count
               != kExactWeakHarmonics.size()
        || std::fabs(
               result.fundamental_hz
               - static_cast<float>(kExactWeakFundamentalHz))
               > 10.0F
        || !phase_matches(
               result.fundamental_phase_radians,
               static_cast<float>(kExactWeakPhasesRadians[0]))
        || std::fabs(result.bin_width_hz - 495.91064453125F) > 0.01F
        || std::fabs(
               result.voltage_peak_to_peak
               - kExactWeakExpectedPeakToPeakVolts)
               > 0.001F
        || std::fabs(result.true_rms_volts - kExactWeakExpectedRmsVolts)
               > 0.001F
        || result.analysis_time_us >= 50000U) {
        return false;
    }

    for (size_t line = 0; line < kExactWeakHarmonics.size(); ++line) {
        if (result.spectral_lines[line].harmonic_order
                != kExactWeakHarmonics[line]
            || result.displayed_spectral_lines[line].harmonic_order
                   != kExactWeakHarmonics[line]
            || std::fabs(
                   result.spectral_lines[line].amplitude_volts_peak
                   - static_cast<float>(kExactWeakAmplitudesVolts[line]))
                   > 0.001F
            || std::fabs(
                   result.displayed_spectral_lines[line]
                           .amplitude_volts_peak
                   - static_cast<float>(kExactWeakAmplitudesVolts[line]))
                   > 0.001F) {
            return false;
        }
    }
    return true;
}

bool LiveDataPipeline::resources_released() const
{
    return ui_queue_ == nullptr && analysis_frame_ == nullptr
           && analysis_task_handle_ == nullptr && receiver_ == nullptr
           && fft_processor_.resources_released();
}

bool LiveDataPipeline::failed_preparation_is_clean() const
{
    return preparation_state_ == PreparationState::Failed
           && resources_released()
           && !fft_self_test_passed_.load(std::memory_order_relaxed);
}

bool LiveDataPipeline::prepared_for_start_retry() const
{
    return preparation_state_ == PreparationState::Prepared
           && ui_queue_ != nullptr && analysis_frame_ != nullptr
           && analysis_task_handle_ == nullptr && receiver_ == nullptr
           && fft_processor_.initialized()
           && fft_self_test_passed_.load(std::memory_order_relaxed);
}

void LiveDataPipeline::release_resources()
{
    if (ui_queue_ != nullptr) {
        vQueueDelete(ui_queue_);
        ui_queue_ = nullptr;
    }
    if (analysis_frame_ != nullptr) {
        heap_caps_free(analysis_frame_);
        analysis_frame_ = nullptr;
    }
    receiver_ = nullptr;
    fft_processor_.deinitialize();
    fft_self_test_passed_.store(false, std::memory_order_relaxed);
}

}  // namespace cyclescope
