#include "cyclescope_display_startup_fault_test.hpp"
#include "cyclescope_display_lifecycle_fault_test.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <cstring>

#include "esp_heap_caps.h"
#if CONFIG_HEAP_TRACING_STANDALONE
#include "esp_heap_trace.h"
#endif
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_lv_adapter.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "instrument_app.hpp"
#include "spectrum_projection.hpp"

namespace {

constexpr char kDisplayStackTag[] = "cyclescope_stack_fault";
constexpr size_t kMaximumStackLifecycleEvents = 24;

std::atomic<cyclescope_display_lifecycle_failpoint_t>
    g_stack_failpoint{CYCLESCOPE_DISPLAY_FAIL_NONE};
std::array<uint32_t, CYCLESCOPE_DISPLAY_FAIL_COUNT>
    g_stack_failpoint_consumptions{};
std::array<cyclescope_display_lifecycle_event_t,
           kMaximumStackLifecycleEvents>
    g_stack_lifecycle_journal{};
size_t g_stack_lifecycle_event_count = 0;
bool g_stack_lifecycle_journal_overflow = false;

}  // namespace

extern "C" bool cyclescope_display_lifecycle_consume_failpoint(
    cyclescope_display_lifecycle_failpoint_t point)
{
    cyclescope_display_lifecycle_failpoint_t expected = point;
    const bool consumed = g_stack_failpoint.compare_exchange_strong(
        expected, CYCLESCOPE_DISPLAY_FAIL_NONE, std::memory_order_acq_rel);
    if (consumed) {
        const size_t index = static_cast<size_t>(point);
        if (index < g_stack_failpoint_consumptions.size()) {
            ++g_stack_failpoint_consumptions[index];
        }
        cyclescope_display_lifecycle_note_event(
            CYCLESCOPE_DISPLAY_LIFECYCLE_FAULT_CONSUMED);
    }
    return consumed;
}

extern "C" void cyclescope_display_lifecycle_note_event(
    cyclescope_display_lifecycle_event_t event)
{
    if (g_stack_lifecycle_event_count
        >= g_stack_lifecycle_journal.size()) {
        g_stack_lifecycle_journal_overflow = true;
        return;
    }
    g_stack_lifecycle_journal[g_stack_lifecycle_event_count++] = event;
}

namespace cyclescope::startup_fault_test {

struct DisplayStartupFaultTestAccess {
    static bool committed(const InstrumentApp &app)
    {
        return app.ui_started_ && app.ui_root_ != nullptr
               && app.waveform_.created() && app.spectrum_view_.created()
               && app.connection_status_value_ != nullptr
               && app.connection_status_hint_ != nullptr
               && std::strcmp(
                      lv_label_get_text(app.connection_status_value_),
                      "CHECKING") == 0
               && app.footer_left_ != nullptr
               && std::strcmp(
                      lv_label_get_text(app.footer_left_), "UI READY") == 0;
    }

    static bool rollback(InstrumentApp &app)
    {
        return app.rollback_ui_start();
    }

    static bool clean(const InstrumentApp &app)
    {
        return app.ui_start_resources_released();
    }

    static bool run_control_contract(
        InstrumentApp &app, uint32_t *maximum_callback_us)
    {
        if (maximum_callback_us == nullptr || !app.ui_started_
            || app.active_view_ != InstrumentApp::View::Time
            || app.time_button_ == nullptr
            || app.spectrum_button_ == nullptr
            || app.one_period_button_ == nullptr
            || app.three_period_button_ == nullptr
            || app.spectrum_less_button_ == nullptr
            || app.spectrum_more_button_ == nullptr
            || app.spectrum_line_count_label_ == nullptr) {
            return false;
        }

        // DynamicMeasurementFrame embeds both full projection buffers and is
        // much larger than the ESP main-task stack budget.  This matrix runs
        // serially during startup, so reuse one static fixture just like the
        // InstrumentApp fixture below and explicitly reset it between probes.
        static DynamicMeasurementFrame frame;
        std::memset(&frame, 0, sizeof(frame));
        frame.generation = 77;
        frame.session_id = 0x12345678U;
        frame.frame_id = 77;
        frame.config_id = 0x01020304U;
        frame.stream_epoch = 1;
        frame.p4_response_profile_id = 0xA1B2C3D4U;
        frame.source_flags = cslp::kFlagCalibrated;
        frame.frequency_response_compensated = true;
        frame.voltage_peak_to_peak = 0.100F;
        frame.true_rms_volts = 0.035F;
        frame.fundamental_hz = 10000.0F;
        frame.sample_rate_hz = 4062500.0F;
        frame.waveform.generation = frame.generation;
        frame.waveform.sample_rate_hz = frame.sample_rate_hz;
        frame.waveform.fundamental_hz = frame.fundamental_hz;
        frame.waveform.voltage_peak_to_peak =
            frame.voltage_peak_to_peak;
        frame.waveform.true_rms_volts = frame.true_rms_volts;
        frame.waveform.vertical_range_volts = 0.060F;
        frame.waveform.one_period.span_us = 100.0F;
        frame.waveform.one_period.column_count =
            static_cast<uint16_t>(kWaveformDisplayColumns);
        frame.waveform.one_period.peak_preserved = true;
        frame.waveform.three_periods.span_us = 300.0F;
        frame.waveform.three_periods.column_count =
            static_cast<uint16_t>(kWaveformDisplayColumns);
        frame.waveform.three_periods.peak_preserved = true;
        for (size_t column = 0; column < kWaveformDisplayColumns;
             ++column) {
            const float value = (column & 1U) == 0U ? -0.050F : 0.050F;
            frame.waveform.one_period.columns[column] = {value, value};
            frame.waveform.three_periods.columns[column] = {value, value};
        }

        frame.spectrum.generation = frame.generation;
        frame.spectrum.sample_rate_hz =
            static_cast<uint32_t>(frame.sample_rate_hz);
        frame.spectrum.fft_size = 8192;
        frame.spectrum.column_count =
            static_cast<uint16_t>(kSpectrumDisplayColumns);
        frame.spectrum.peak_count =
            static_cast<uint8_t>(kMaximumDisplayedSpectralLines);
        frame.spectrum.source_buffer_index = 0xFF;
        frame.spectrum.frequency_min_hz = 0.0F;
        frame.spectrum.frequency_max_hz =
            kSpectrumDisplayMaximumHz;
        frame.spectrum.bin_width_hz = 495.91064453125F;
        frame.spectrum.amplitude_max_volts = 0.100F;
        for (size_t line = 0U;
             line < kMaximumDisplayedSpectralLines; ++line) {
            const float frequency_hz =
                10000.0F * static_cast<float>(line + 1U);
            const float amplitude_volts_peak =
                line + 1U == kMaximumDisplayedSpectralLines
                    ? 0.060F
                    : 0.044F / static_cast<float>(line + 1U);
            frame.spectrum.peaks[line] = {
                .bin_index = static_cast<uint16_t>(20U * (line + 1U)),
                .frequency_hz = frequency_hz,
                .amplitude_volts_peak = amplitude_volts_peak,
                .snr_db = 0.0F,
            };
        }

        // Production receives directly into live_frame_ before applying it.
        // Mirror that ownership path so view switches exercise the same
        // retained measurement used by update_spectrum_metrics().
        std::array<char, 96> waiting_source{};
        std::array<char, 96> waiting_footer{};
        snprintf(waiting_source.data(), waiting_source.size(), "%s",
                 lv_label_get_text(app.source_label_));
        snprintf(waiting_footer.data(), waiting_footer.size(), "%s",
                 lv_label_get_text(app.footer_left_));
        constexpr uint32_t freshness_start_ms = 1000U;
        app.live_timer_started_ms_ = freshness_start_ms;
        app.update_live_stream_state(
            false,
            freshness_start_ms
                + live_stream_freshness::kTimeoutMs - 1U,
            0U);
        if (app.live_stream_stale_
            || std::strcmp(lv_label_get_text(app.source_label_),
                           waiting_source.data()) != 0
            || std::strcmp(lv_label_get_text(app.footer_left_),
                           waiting_footer.data()) != 0) {
            return false;
        }

        // A ready transport without a first valid frame becomes explicitly
        // stale at the freshness boundary instead of waiting forever.
        app.update_live_stream_state(
            true, freshness_start_ms + live_stream_freshness::kTimeoutMs,
            0U);
        if (!app.live_stream_stale_
            || !app.stale_transport_ready_
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP ONLINE   •   STALE   •   NO VALID FRAME")
                   != 0
            || std::strcmp(lv_label_get_text(app.footer_left_),
                           "STALE   •   NO VALID FRAME") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_value_),
                   "NO VALID DATA") != 0) {
            return false;
        }

        app.live_frame_ = frame;
        app.apply_live_measurement(app.live_frame_);
        if (!app.waveform_.visible() || app.spectrum_view_.visible()
            || app.waveform_.periods() != 3
            || app.live_frame_.generation != frame.generation
            || app.live_stream_stale_
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP LIVE   •   UP CAL   •   P4CAL A1B2C3D4   •   FRAME 77") != 0
            || std::strcmp(lv_label_get_text(app.footer_left_),
                           "LIVE  1 frames") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_value_),
                   "NORMAL") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_hint_),
                   "LAN + VALID FRAME OK") != 0) {
            return false;
        }

        // STATUS-only, incomplete, rejected and failed-analysis traffic all
        // share this UI contract: none refreshes last_ui_tick_ms_.
        const uint32_t applied_ms = app.last_ui_tick_ms_;
        app.update_live_stream_state(
            true, applied_ms + live_stream_freshness::kTimeoutMs - 1U,
            0U);
        if (app.live_stream_stale_
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP LIVE   •   UP CAL   •   P4CAL A1B2C3D4   •   FRAME 77") != 0) {
            return false;
        }

        app.update_live_stream_state(
            true, applied_ms + live_stream_freshness::kTimeoutMs,
            1U);
        if (!app.live_stream_stale_
            || !app.stale_transport_ready_
            || app.ui_frames_applied_ != 1U
            || app.last_render_session_id_ != frame.session_id
            || app.last_render_frame_id_ != frame.frame_id
            || !app.waveform_.has_frame()
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP ONLINE   •   STALE   •   LAST FRAME 77")
                   != 0
            || std::strstr(lv_label_get_text(app.source_label_), "LIVE")
                   != nullptr
            || std::strstr(lv_label_get_text(app.source_label_), "CAL")
                   != nullptr
            || std::strcmp(lv_label_get_text(app.footer_left_),
                           "STALE   •   LAST FRAME 77") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_value_),
                   "DATA REJECTED") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_hint_),
                   "OUTSIDE CAPTURE RULES") != 0
            || std::strcmp(lv_label_get_text(app.vpp_value_),
                           "100.00mV") != 0
            || std::strcmp(lv_label_get_text(app.rms_value_),
                           "35.00mV") != 0
            || std::strcmp(lv_label_get_text(app.fundamental_value_),
                           "10,000.00Hz") != 0
            || std::strcmp(lv_label_get_text(app.sample_rate_value_),
                           "4.0625") != 0) {
            return false;
        }

        // Transport state can change while stale, but only a newly applied
        // valid frame may make the retained data LIVE again.
        app.update_live_stream_state(
            false, applied_ms + live_stream_freshness::kTimeoutMs,
            1U);
        if (!app.live_stream_stale_
            || app.stale_transport_ready_
            || app.ui_frames_applied_ != 1U
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP OFFLINE   •   STALE   •   LAST FRAME 77")
                   != 0
            || std::strcmp(lv_label_get_text(app.footer_left_),
                           "STALE   •   LAST FRAME 77") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_value_),
                   "NO FPGA LINK") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_hint_),
                   "CSLP SESSION NOT ESTABLISHED") != 0) {
            return false;
        }
        app.update_live_stream_state(
            false, applied_ms + live_stream_freshness::kTimeoutMs + 1U,
            1U);
        app.update_live_stream_state(
            true, applied_ms + live_stream_freshness::kTimeoutMs + 1U,
            1U);
        if (!app.live_stream_stale_
            || !app.stale_transport_ready_
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP ONLINE   •   STALE   •   LAST FRAME 77")
                   != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_value_),
                   "NO VALID DATA") != 0) {
            return false;
        }

        frame.generation = 78;
        frame.frame_id = 78;
        frame.waveform.generation = frame.generation;
        frame.spectrum.generation = frame.generation;
        app.live_frame_ = frame;
        app.apply_live_measurement(app.live_frame_);
        if (app.live_stream_stale_
            || app.stale_transport_ready_
            || app.ui_frames_applied_ != 2U
            || app.last_render_frame_id_ != 78U
            || std::strcmp(
                   lv_label_get_text(app.source_label_),
                   "SOURCE  CSLP LIVE   •   UP CAL   •   P4CAL A1B2C3D4   •   FRAME 78") != 0
            || std::strcmp(lv_label_get_text(app.footer_left_),
                           "LIVE  2 frames") != 0
            || std::strcmp(
                   lv_label_get_text(app.connection_status_value_),
                   "NORMAL") != 0) {
            return false;
        }

        uint32_t maximum_us = 0;
        const auto click = [&maximum_us](lv_obj_t *button) {
            const int64_t start_us = esp_timer_get_time();
            const lv_result_t result =
                lv_obj_send_event(button, LV_EVENT_CLICKED, nullptr);
            const int64_t elapsed_us = esp_timer_get_time() - start_us;
            if (elapsed_us < 0 || elapsed_us > 500000) {
                return false;
            }
            maximum_us = std::max(
                maximum_us, static_cast<uint32_t>(elapsed_us));
            return result == LV_RESULT_OK;
        };

        if (!click(app.spectrum_button_)
            || app.active_view_ != InstrumentApp::View::Spectrum
            || app.waveform_.visible() || !app.spectrum_view_.visible()
            || !lv_obj_has_flag(
                app.one_period_button_, LV_OBJ_FLAG_HIDDEN)
            || !lv_obj_has_flag(
                app.three_period_button_, LV_OBJ_FLAG_HIDDEN)
            || lv_obj_has_flag(
                app.spectrum_less_button_, LV_OBJ_FLAG_HIDDEN)
            || lv_obj_has_flag(
                app.spectrum_more_button_, LV_OBJ_FLAG_HIDDEN)
            || lv_obj_has_flag(
                app.spectrum_line_count_label_, LV_OBJ_FLAG_HIDDEN)
            || app.spectrum_view_.visible_peak_count() != 3U
            || app.spectrum_view_.available_peak_count()
                   != kMaximumDisplayedSpectralLines
            || std::strcmp(
                   lv_label_get_text(app.spectrum_line_count_label_),
                   "LINES 3/8") != 0
            || std::strcmp(
                   lv_label_get_text(app.spectrum_legend_),
                   "11.00mV/div  •  10,000Hz 44.00mV  •  "
                   "20,000Hz 22.00mV  •  "
                   "30,000Hz 14.67mV") != 0
            || std::fabs(
                   app.spectrum_view_.visible_amplitude_max_volts()
                   - 0.055F) > 0.000001F
            || std::fabs(
                   app.spectrum_view_.volts_per_division() - 0.011F)
                   > 0.000001F
            || std::fabs(
                   app.spectrum_view_.visible_frequency_minimum_hz()
                   - 8000.0F) > 0.1F
            || std::fabs(
                   app.spectrum_view_.visible_frequency_maximum_hz()
                   - 32000.0F) > 0.1F
            || !click(app.spectrum_more_button_)
            || !click(app.spectrum_more_button_)
            || !click(app.spectrum_more_button_)
            || !click(app.spectrum_more_button_)
            || !click(app.spectrum_more_button_)
            || app.spectrum_view_.visible_peak_count() != 8U
            || std::strcmp(
                   lv_label_get_text(app.spectrum_line_count_label_),
                   "LINES 8/8") != 0
            || !lv_obj_has_state(
                app.spectrum_more_button_, LV_STATE_DISABLED)
            || std::fabs(
                   app.spectrum_view_.visible_amplitude_max_volts()
                   - 0.075F) > 0.000001F
            || std::fabs(
                   app.spectrum_view_.volts_per_division() - 0.015F)
                   > 0.000001F
            || std::strcmp(
                   lv_label_get_text(app.spectrum_legend_),
                   "15.00mV/div  •  10,000Hz 44.00mV  •  "
                   "20,000Hz 22.00mV  •  30,000Hz 14.67mV  •  +5")
                   != 0
            || std::fabs(
                   app.spectrum_view_.visible_frequency_minimum_hz()
                   - 3000.0F) > 0.1F
            || std::fabs(
                   app.spectrum_view_.visible_frequency_maximum_hz()
                   - 87000.0F) > 0.1F
            || !click(app.spectrum_less_button_)
            || app.spectrum_view_.visible_peak_count() != 7U
            || std::strcmp(
                   lv_label_get_text(app.spectrum_line_count_label_),
                   "LINES 7/8") != 0
            || std::fabs(
                   app.spectrum_view_.visible_amplitude_max_volts()
                   - 0.055F) > 0.000001F
            || std::fabs(
                   app.spectrum_view_.volts_per_division() - 0.011F)
                   > 0.000001F
            || std::strcmp(
                   lv_label_get_text(app.spectrum_legend_),
                   "11.00mV/div  •  10,000Hz 44.00mV  •  "
                   "20,000Hz 22.00mV  •  30,000Hz 14.67mV  •  +4")
                   != 0
            || lv_obj_has_state(
                app.spectrum_more_button_, LV_STATE_DISABLED)
            || !click(app.time_button_)
            || app.active_view_ != InstrumentApp::View::Time
            || !app.waveform_.visible() || app.spectrum_view_.visible()
            || lv_obj_has_flag(
                app.one_period_button_, LV_OBJ_FLAG_HIDDEN)
            || lv_obj_has_flag(
                app.three_period_button_, LV_OBJ_FLAG_HIDDEN)
            || !lv_obj_has_flag(
                app.spectrum_less_button_, LV_OBJ_FLAG_HIDDEN)
            || !lv_obj_has_flag(
                app.spectrum_more_button_, LV_OBJ_FLAG_HIDDEN)
            || !lv_obj_has_flag(
                app.spectrum_line_count_label_, LV_OBJ_FLAG_HIDDEN)
            || !click(app.one_period_button_)
            || app.waveform_.periods() != 1
            || std::fabs(app.waveform_.span_us() - 100.0F) > 0.001F
            || !click(app.three_period_button_)
            || app.waveform_.periods() != 3
            || std::fabs(app.waveform_.span_us() - 300.0F) > 0.001F
            || app.live_frame_.generation != frame.generation) {
            return false;
        }

        *maximum_callback_us = maximum_us;
        return true;
    }
};

namespace {

constexpr char kTag[] = "cyclescope_fault";
constexpr size_t kMaximumLifecycleEvents = 16;
constexpr size_t kMaximumScreenChildren = 16;
constexpr size_t kDisplayFailpointSlots = 3;
#if CONFIG_HEAP_TRACING_STANDALONE
// One complete display transaction stays well below this many outstanding
// allocations.  Keeping the buffer bounded also leaves enough internal BSS
// for the P4 image when eight allocation/free backtrace frames are enabled.
constexpr size_t kDisplayStackHeapTraceRecordCount = 256;
heap_trace_record_t
    g_display_stack_heap_trace_records[kDisplayStackHeapTraceRecordCount];
#endif

std::atomic<DisplayFailPoint> g_display_failpoint{DisplayFailPoint::None};
std::array<uint32_t, kDisplayFailpointSlots> g_failpoint_consumptions{};
std::array<DisplayLifecycleEvent, kMaximumLifecycleEvents>
    g_lifecycle_journal{};
size_t g_lifecycle_event_count = 0;
bool g_lifecycle_journal_overflow = false;

constexpr std::array<DisplayFailPoint, 2> kDisplayFailpoints = {
    DisplayFailPoint::WaveformCanvasBuffer,
    DisplayFailPoint::SpectrumCanvasBuffer,
};

constexpr std::array<DisplayLifecycleEvent, 5> kNormalLifecycle = {
    DisplayLifecycleEvent::WaveformCreated,
    DisplayLifecycleEvent::SpectrumCreated,
    DisplayLifecycleEvent::SpectrumDestroyed,
    DisplayLifecycleEvent::WaveformDestroyed,
    DisplayLifecycleEvent::UiRootDestroyed,
};

constexpr std::array<DisplayLifecycleEvent, 2> kD1Lifecycle = {
    DisplayLifecycleEvent::WaveformFailpointConsumed,
    DisplayLifecycleEvent::UiRootDestroyed,
};

constexpr std::array<DisplayLifecycleEvent, 4> kD2Lifecycle = {
    DisplayLifecycleEvent::WaveformCreated,
    DisplayLifecycleEvent::SpectrumFailpointConsumed,
    DisplayLifecycleEvent::WaveformDestroyed,
    DisplayLifecycleEvent::UiRootDestroyed,
};

constexpr std::array<cyclescope_display_lifecycle_failpoint_t, 7>
    kDisplayStackFailpoints = {
        CYCLESCOPE_DISPLAY_FAIL_AFTER_BSP_DISPLAY,
        CYCLESCOPE_DISPLAY_FAIL_AFTER_ADAPTER_INIT,
        CYCLESCOPE_DISPLAY_FAIL_AFTER_DISPLAY_REGISTER,
        CYCLESCOPE_DISPLAY_FAIL_AFTER_BSP_TOUCH,
        CYCLESCOPE_DISPLAY_FAIL_AFTER_TOUCH_REGISTER,
        CYCLESCOPE_DISPLAY_FAIL_AFTER_WORKER_START,
        CYCLESCOPE_DISPLAY_FAIL_AFTER_BACKLIGHT_ON,
    };

constexpr std::array<cyclescope_display_lifecycle_event_t, 7>
    kDisplayStackAcquisitions = {
        CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_DISPLAY_CREATED,
        CYCLESCOPE_DISPLAY_LIFECYCLE_ADAPTER_INITIALIZED,
        CYCLESCOPE_DISPLAY_LIFECYCLE_DISPLAY_REGISTERED,
        CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_TOUCH_CREATED,
        CYCLESCOPE_DISPLAY_LIFECYCLE_TOUCH_REGISTERED,
        CYCLESCOPE_DISPLAY_LIFECYCLE_WORKER_STARTED,
        CYCLESCOPE_DISPLAY_LIFECYCLE_BACKLIGHT_ON,
    };

struct HeapSnapshot {
    size_t internal_free;
    size_t psram_free;
};

HeapSnapshot capture_heap();

struct ScreenTreeSnapshot {
    uint32_t child_count;
    std::array<lv_obj_t *, kMaximumScreenChildren> children{};
    bool complete;
};

void reset_stack_observations()
{
    g_stack_failpoint.store(CYCLESCOPE_DISPLAY_FAIL_NONE,
                            std::memory_order_release);
    g_stack_failpoint_consumptions.fill(0);
    g_stack_lifecycle_event_count = 0;
    g_stack_lifecycle_journal_overflow = false;
}

void arm_stack_failpoint(cyclescope_display_lifecycle_failpoint_t point)
{
    g_stack_failpoint.store(point, std::memory_order_release);
}

const char *stack_failpoint_name(
    cyclescope_display_lifecycle_failpoint_t point)
{
    switch (point) {
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_BSP_DISPLAY:
        return "D3 after-bsp-display";
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_ADAPTER_INIT:
        return "D4 after-adapter-init";
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_DISPLAY_REGISTER:
        return "D5 after-display-register";
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_BSP_TOUCH:
        return "D6 after-bsp-touch";
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_TOUCH_REGISTER:
        return "D7 after-touch-register";
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_WORKER_START:
        return "D8 after-worker-start";
    case CYCLESCOPE_DISPLAY_FAIL_AFTER_BACKLIGHT_ON:
        return "D9 after-backlight-on";
    case CYCLESCOPE_DISPLAY_FAIL_NONE:
    case CYCLESCOPE_DISPLAY_FAIL_COUNT:
        return "none";
    }
    return "unknown";
}

bool stack_failpoint_consumed_exactly_once(
    cyclescope_display_lifecycle_failpoint_t expected_point)
{
    if (g_stack_failpoint.load(std::memory_order_acquire)
        != CYCLESCOPE_DISPLAY_FAIL_NONE) {
        return false;
    }
    for (size_t index = 0;
         index < g_stack_failpoint_consumptions.size(); ++index) {
        const uint32_t expected =
            index == static_cast<size_t>(expected_point) ? 1U : 0U;
        if (g_stack_failpoint_consumptions[index] != expected) {
            return false;
        }
    }
    return true;
}

bool no_stack_failpoint_consumed()
{
    if (g_stack_failpoint.load(std::memory_order_acquire)
        != CYCLESCOPE_DISPLAY_FAIL_NONE) {
        return false;
    }
    for (uint32_t count : g_stack_failpoint_consumptions) {
        if (count != 0U) {
            return false;
        }
    }
    return true;
}

void append_stack_teardown_events(
    size_t acquired_stages,
    std::array<cyclescope_display_lifecycle_event_t,
               kMaximumStackLifecycleEvents> *expected,
    size_t *count)
{
    if (acquired_stages >= 7U) {
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_BACKLIGHT_OFF;
    }
    if (acquired_stages >= 5U) {
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_TOUCH_UNREGISTERED;
    }
    if (acquired_stages >= 3U) {
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_DISPLAY_UNREGISTERED;
    }
    if (acquired_stages >= 2U) {
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_ADAPTER_DEINITIALIZED;
    }
    if (acquired_stages >= 4U) {
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_RAW_TOUCH_DELETED;
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_TOUCH_DELETED;
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_I2C_DEINITIALIZED;
    }
    if (acquired_stages >= 1U) {
#if CONFIG_BSP_LCD_USE_DMA2D \
    && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_PANEL_DMA2D_DISABLED;
#endif
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_BACKLIGHT_CHANNEL_DECONFIGURED;
        (*expected)[(*count)++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_DISPLAY_DELETED;
    }
}

bool stack_journal_matches(size_t acquired_stages, bool injected)
{
    std::array<cyclescope_display_lifecycle_event_t,
               kMaximumStackLifecycleEvents> expected{};
    size_t expected_count = 0;
    for (size_t index = 0; index < acquired_stages; ++index) {
        expected[expected_count++] = kDisplayStackAcquisitions[index];
    }
    if (injected) {
        expected[expected_count++] =
            CYCLESCOPE_DISPLAY_LIFECYCLE_FAULT_CONSUMED;
    }
    append_stack_teardown_events(
        acquired_stages, &expected, &expected_count);

    if (g_stack_lifecycle_journal_overflow
        || g_stack_lifecycle_event_count != expected_count) {
        return false;
    }
    for (size_t index = 0; index < expected_count; ++index) {
        if (g_stack_lifecycle_journal[index] != expected[index]) {
            return false;
        }
    }
    return true;
}

void wait_for_stack_cleanup()
{
    // Let the idle task reclaim any cross-task FreeRTOS deletion bookkeeping
    // before an exact heap comparison. This is a bounded test-only barrier.
    vTaskDelay(pdMS_TO_TICKS(30));
}

bool run_normal_stack_probe(cyclescope_display_stack_t &stack,
                            const bsp_display_cfg_t *cfg,
                            const char *name,
                            bool require_heap_exact)
{
    reset_stack_observations();
    const HeapSnapshot heap_before = capture_heap();
#if CONFIG_HEAP_TRACING_STANDALONE
    bool heap_trace_running = false;
    if (require_heap_exact) {
        const esp_err_t init_error = heap_trace_init_standalone(
            g_display_stack_heap_trace_records,
            kDisplayStackHeapTraceRecordCount);
        const esp_err_t start_error =
            init_error == ESP_OK ? heap_trace_start(HEAP_TRACE_LEAKS)
                                 : init_error;
        heap_trace_running = start_error == ESP_OK;
        ESP_LOGI(kDisplayStackTag,
                 "%s heap trace: init=%s start=%s records=%u",
                 name, esp_err_to_name(init_error),
                 esp_err_to_name(start_error),
                 static_cast<unsigned>(kDisplayStackHeapTraceRecordCount));
    }
#endif
    const esp_err_t init_error = cyclescope_display_stack_init(&stack, cfg);
    const bool committed = init_error == ESP_OK && stack.display != nullptr
                           && stack.touch != nullptr
                           && stack.touch_indev != nullptr
                           && stack.adapter_started && stack.backlight_on;
    const esp_err_t destroy_error = committed
                                        ? cyclescope_display_stack_destroy(&stack)
                                        : ESP_ERR_INVALID_STATE;
    wait_for_stack_cleanup();
    const HeapSnapshot heap_after = capture_heap();
    const bool heap_exact =
        heap_before.internal_free == heap_after.internal_free
        && heap_before.psram_free == heap_after.psram_free;
#if CONFIG_HEAP_TRACING_STANDALONE
    if (heap_trace_running) {
        const esp_err_t stop_error = heap_trace_stop();
        ESP_LOGI(kDisplayStackTag, "%s heap trace stop: %s",
                 name, esp_err_to_name(stop_error));
        if (!heap_exact) {
            heap_trace_dump_caps(MALLOC_CAP_INTERNAL);
        }
    }
#endif
    const bool clean = cyclescope_display_stack_resources_released(&stack)
                       && !esp_lv_adapter_is_initialized();
    const bool integrity_ok = heap_caps_check_integrity_all(true);
    const bool lifecycle_exact = stack_journal_matches(7U, false);
    const bool no_injection = no_stack_failpoint_consumed();

    if (!committed || destroy_error != ESP_OK || !clean || !integrity_ok
        || (require_heap_exact && !heap_exact) || !lifecycle_exact
        || !no_injection) {
        ESP_LOGE(kDisplayStackTag,
                 "%s FAIL: init=%s destroy=%s committed=%u clean=%u "
                 "integrity=%u heap=%u lifecycle=%u injection=%u "
                 "events=%u free=%u/%u->%u/%u",
                 name, esp_err_to_name(init_error),
                 esp_err_to_name(destroy_error),
                 static_cast<unsigned>(committed),
                 static_cast<unsigned>(clean),
                 static_cast<unsigned>(integrity_ok),
                 static_cast<unsigned>(heap_exact),
                 static_cast<unsigned>(lifecycle_exact),
                 static_cast<unsigned>(no_injection),
                 static_cast<unsigned>(g_stack_lifecycle_event_count),
                 static_cast<unsigned>(heap_before.internal_free),
                 static_cast<unsigned>(heap_before.psram_free),
                 static_cast<unsigned>(heap_after.internal_free),
                 static_cast<unsigned>(heap_after.psram_free));
        return false;
    }

    ESP_LOGI(kDisplayStackTag,
             "%s PASS: journal=EXACT owned=EMPTY heap=%s integrity=PASS "
             "free=%u/%u",
             name, heap_exact ? "EXACT" : "WARMING",
             static_cast<unsigned>(heap_after.internal_free),
             static_cast<unsigned>(heap_after.psram_free));
    return true;
}

HeapSnapshot capture_heap()
{
    return {
        heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
    };
}

ScreenTreeSnapshot capture_screen_tree(lv_obj_t *screen)
{
    ScreenTreeSnapshot snapshot{};
    snapshot.child_count = lv_obj_get_child_count(screen);
    snapshot.complete = snapshot.child_count <= snapshot.children.size();
    if (!snapshot.complete) {
        return snapshot;
    }
    for (uint32_t index = 0; index < snapshot.child_count; ++index) {
        snapshot.children[index] =
            lv_obj_get_child(screen, static_cast<int32_t>(index));
    }
    return snapshot;
}

bool same_screen_tree(const ScreenTreeSnapshot &before,
                      const ScreenTreeSnapshot &after)
{
    if (!before.complete || !after.complete
        || before.child_count != after.child_count) {
        return false;
    }
    for (uint32_t index = 0; index < before.child_count; ++index) {
        if (before.children[index] != after.children[index]) {
            return false;
        }
    }
    return true;
}

size_t failpoint_index(DisplayFailPoint point)
{
    return static_cast<size_t>(point);
}

const char *failpoint_name(DisplayFailPoint point)
{
    switch (point) {
    case DisplayFailPoint::WaveformCanvasBuffer:
        return "D1 waveform-canvas-buffer";
    case DisplayFailPoint::SpectrumCanvasBuffer:
        return "D2 spectrum-canvas-buffer";
    case DisplayFailPoint::None:
        return "none";
    }
    return "unknown";
}

void reset_observations()
{
    g_display_failpoint.store(DisplayFailPoint::None,
                              std::memory_order_release);
    g_failpoint_consumptions.fill(0);
    g_lifecycle_event_count = 0;
    g_lifecycle_journal_overflow = false;
}

bool failpoint_is_disarmed()
{
    return g_display_failpoint.load(std::memory_order_acquire)
           == DisplayFailPoint::None;
}

bool failpoint_consumed_exactly_once(DisplayFailPoint expected_point)
{
    for (size_t index = 0; index < g_failpoint_consumptions.size(); ++index) {
        const uint32_t expected =
            index == failpoint_index(expected_point) ? 1U : 0U;
        if (g_failpoint_consumptions[index] != expected) {
            return false;
        }
    }
    return true;
}

bool no_failpoint_consumed()
{
    for (uint32_t count : g_failpoint_consumptions) {
        if (count != 0U) {
            return false;
        }
    }
    return true;
}

template <size_t EventCount>
bool lifecycle_matches(
    const std::array<DisplayLifecycleEvent, EventCount> &expected)
{
    if (g_lifecycle_journal_overflow
        || g_lifecycle_event_count != expected.size()) {
        return false;
    }
    for (size_t index = 0; index < expected.size(); ++index) {
        if (g_lifecycle_journal[index] != expected[index]) {
            return false;
        }
    }
    return true;
}

bool fault_lifecycle_matches(DisplayFailPoint point)
{
    switch (point) {
    case DisplayFailPoint::WaveformCanvasBuffer:
        return lifecycle_matches(kD1Lifecycle);
    case DisplayFailPoint::SpectrumCanvasBuffer:
        return lifecycle_matches(kD2Lifecycle);
    case DisplayFailPoint::None:
        return false;
    }
    return false;
}

bool run_normal_create_destroy_probe(InstrumentApp &app,
                                     lv_obj_t *screen,
                                     const char *name,
                                     bool require_internal_exact,
                                     bool run_control_contract)
{
    reset_observations();
    const ScreenTreeSnapshot tree_before = capture_screen_tree(screen);
    const HeapSnapshot heap_before = capture_heap();
    const bool started = app.start_ui();
    const bool committed = DisplayStartupFaultTestAccess::committed(app);
    uint32_t maximum_callback_us = 0;
    const bool controls_ok = !run_control_contract
                             || (committed
                                 && DisplayStartupFaultTestAccess::
                                     run_control_contract(
                                         app, &maximum_callback_us));
    const bool rolled_back = DisplayStartupFaultTestAccess::rollback(app);
    const bool clean = DisplayStartupFaultTestAccess::clean(app);
    const bool integrity_ok = heap_caps_check_integrity_all(true);
    const HeapSnapshot heap_after = capture_heap();
    const ScreenTreeSnapshot tree_after = capture_screen_tree(screen);
    const bool psram_exact =
        heap_before.psram_free == heap_after.psram_free;
    const bool internal_exact =
        heap_before.internal_free == heap_after.internal_free;
    const bool tree_exact = same_screen_tree(tree_before, tree_after);
    const bool lifecycle_exact = lifecycle_matches(kNormalLifecycle);
    const bool no_injection = failpoint_is_disarmed()
                              && no_failpoint_consumed();

    if (!started || !committed || !controls_ok || !rolled_back || !clean
        || !integrity_ok
        || !psram_exact || (require_internal_exact && !internal_exact)
        || !tree_exact || !lifecycle_exact || !no_injection) {
        ESP_LOGE(kTag,
                 "%s FAIL: started=%u committed=%u controls=%u "
                 "rollback=%u clean=%u "
                 "integrity=%u tree=%u psram=%u internal=%u lifecycle=%u "
                 "events=%u children=%u->%u free=%u/%u->%u/%u",
                 name, static_cast<unsigned>(started),
                 static_cast<unsigned>(committed),
                 static_cast<unsigned>(controls_ok),
                 static_cast<unsigned>(rolled_back),
                 static_cast<unsigned>(clean),
                 static_cast<unsigned>(integrity_ok),
                 static_cast<unsigned>(tree_exact),
                 static_cast<unsigned>(psram_exact),
                 static_cast<unsigned>(internal_exact),
                 static_cast<unsigned>(lifecycle_exact),
                 static_cast<unsigned>(g_lifecycle_event_count),
                 static_cast<unsigned>(tree_before.child_count),
                 static_cast<unsigned>(tree_after.child_count),
                 static_cast<unsigned>(heap_before.internal_free),
                 static_cast<unsigned>(heap_before.psram_free),
                 static_cast<unsigned>(heap_after.internal_free),
                 static_cast<unsigned>(heap_after.psram_free));
        return false;
    }

    ESP_LOGI(kTag,
             "%s PASS: journal=EXACT tree=IDENTICAL psram=EXACT "
             "internal=%s integrity=PASS controls=%s max_callback=%uus "
             "free=%u/%u",
             name, internal_exact ? "EXACT" : "WARMING",
             run_control_contract ? "PASS" : "SKIP",
             static_cast<unsigned>(maximum_callback_us),
             static_cast<unsigned>(heap_after.internal_free),
             static_cast<unsigned>(heap_after.psram_free));
    return true;
}

}  // namespace

void arm_display_failpoint(DisplayFailPoint point)
{
    g_display_failpoint.store(point, std::memory_order_release);
}

bool consume_display_failpoint(DisplayFailPoint point)
{
    DisplayFailPoint expected = point;
    const bool consumed = g_display_failpoint.compare_exchange_strong(
        expected, DisplayFailPoint::None, std::memory_order_acq_rel);
    if (consumed) {
        const size_t index = failpoint_index(point);
        if (index < g_failpoint_consumptions.size()) {
            ++g_failpoint_consumptions[index];
        }
        note_display_lifecycle_event(
            point == DisplayFailPoint::WaveformCanvasBuffer
                ? DisplayLifecycleEvent::WaveformFailpointConsumed
                : DisplayLifecycleEvent::SpectrumFailpointConsumed);
        ESP_LOGW(kTag, "Injected one-shot startup fault: %s",
                 failpoint_name(point));
    }
    return consumed;
}

void note_display_lifecycle_event(DisplayLifecycleEvent event)
{
    if (g_lifecycle_event_count >= g_lifecycle_journal.size()) {
        g_lifecycle_journal_overflow = true;
        return;
    }
    g_lifecycle_journal[g_lifecycle_event_count++] = event;
}

bool run_display_canvas_startup_fault_matrix(lv_display_t *display)
{
    if (display == nullptr) {
        ESP_LOGE(kTag, "Display canvas matrix received a null display");
        return false;
    }
    lv_obj_t *screen = lv_display_get_screen_active(display);
    if (screen == nullptr) {
        ESP_LOGE(kTag, "Display canvas matrix has no active screen");
        return false;
    }

    // InstrumentApp embeds the complete waveform and spectrum frames and is
    // larger than the ESP main-task stack. Keep one fixture instance in static
    // storage and reuse it across every transaction, matching production.
    static InstrumentApp app(display);
    static lv_display_t *const fixture_display = display;
    if (fixture_display != display) {
        ESP_LOGE(kTag, "Display canvas matrix cannot switch displays");
        return false;
    }

    // The first transaction warms persistent styles attached to the active
    // screen. The second must then restore both heaps exactly.
    if (!run_normal_create_destroy_probe(
            app, screen, "D0 warm canvas transaction", false, true)
        || !run_normal_create_destroy_probe(
            app, screen, "D0 strict canvas transaction", true, true)) {
        return false;
    }

    for (DisplayFailPoint point : kDisplayFailpoints) {
        reset_observations();
        const ScreenTreeSnapshot tree_before = capture_screen_tree(screen);
        const HeapSnapshot heap_before = capture_heap();
        arm_display_failpoint(point);
        const bool started = app.start_ui();
        const bool consumed = failpoint_is_disarmed()
                              && failpoint_consumed_exactly_once(point);
        const bool not_ready = !app.ui_started_;
        const bool clean = app.ui_start_resources_released();
        const bool idempotent_rollback = app.rollback_ui_start();
        const bool integrity_ok = heap_caps_check_integrity_all(true);
        const HeapSnapshot heap_after_fault = capture_heap();
        const ScreenTreeSnapshot tree_after_fault =
            capture_screen_tree(screen);
        const bool psram_exact =
            heap_before.psram_free == heap_after_fault.psram_free;
        const bool internal_exact =
            heap_before.internal_free == heap_after_fault.internal_free;
        const bool tree_exact =
            same_screen_tree(tree_before, tree_after_fault);
        const bool lifecycle_exact = fault_lifecycle_matches(point);

        if (started || !consumed || !not_ready || !clean
            || !idempotent_rollback || !integrity_ok || !psram_exact
            || !internal_exact || !tree_exact || !lifecycle_exact) {
            ESP_LOGE(kTag,
                     "%s FAIL: started=%u consumed_once=%u not_ready=%u "
                     "clean=%u idempotent=%u integrity=%u tree=%u psram=%u "
                     "internal=%u lifecycle=%u events=%u free=%u/%u->%u/%u",
                     failpoint_name(point), static_cast<unsigned>(started),
                     static_cast<unsigned>(consumed),
                     static_cast<unsigned>(not_ready),
                     static_cast<unsigned>(clean),
                     static_cast<unsigned>(idempotent_rollback),
                     static_cast<unsigned>(integrity_ok),
                     static_cast<unsigned>(tree_exact),
                     static_cast<unsigned>(psram_exact),
                     static_cast<unsigned>(internal_exact),
                     static_cast<unsigned>(lifecycle_exact),
                     static_cast<unsigned>(g_lifecycle_event_count),
                     static_cast<unsigned>(heap_before.internal_free),
                     static_cast<unsigned>(heap_before.psram_free),
                     static_cast<unsigned>(heap_after_fault.internal_free),
                     static_cast<unsigned>(heap_after_fault.psram_free));
            arm_display_failpoint(DisplayFailPoint::None);
            app.rollback_ui_start();
            return false;
        }

        // A failed transaction must leave the same object reusable, and the
        // retry must execute the complete reverse-order normal journal.
        reset_observations();
        const bool retry_started = app.start_ui();
        const bool retry_committed =
            DisplayStartupFaultTestAccess::committed(app);
        const bool retry_rolled_back = app.rollback_ui_start();
        const bool retry_clean = app.ui_start_resources_released();
        const bool retry_integrity_ok = heap_caps_check_integrity_all(true);
        const HeapSnapshot heap_after_retry = capture_heap();
        const ScreenTreeSnapshot tree_after_retry =
            capture_screen_tree(screen);
        const bool retry_heap_exact =
            heap_before.internal_free == heap_after_retry.internal_free
            && heap_before.psram_free == heap_after_retry.psram_free;
        const bool retry_tree_exact =
            same_screen_tree(tree_before, tree_after_retry);
        const bool retry_lifecycle_exact = lifecycle_matches(kNormalLifecycle)
                                           && no_failpoint_consumed();

        if (!retry_started || !retry_committed || !retry_rolled_back
            || !retry_clean || !retry_integrity_ok || !retry_heap_exact
            || !retry_tree_exact || !retry_lifecycle_exact) {
            ESP_LOGE(kTag,
                     "%s retry FAIL: started=%u committed=%u rollback=%u "
                     "clean=%u integrity=%u heap=%u tree=%u lifecycle=%u "
                     "events=%u free=%u/%u->%u/%u",
                     failpoint_name(point),
                     static_cast<unsigned>(retry_started),
                     static_cast<unsigned>(retry_committed),
                     static_cast<unsigned>(retry_rolled_back),
                     static_cast<unsigned>(retry_clean),
                     static_cast<unsigned>(retry_integrity_ok),
                     static_cast<unsigned>(retry_heap_exact),
                     static_cast<unsigned>(retry_tree_exact),
                     static_cast<unsigned>(retry_lifecycle_exact),
                     static_cast<unsigned>(g_lifecycle_event_count),
                     static_cast<unsigned>(heap_before.internal_free),
                     static_cast<unsigned>(heap_before.psram_free),
                     static_cast<unsigned>(heap_after_retry.internal_free),
                     static_cast<unsigned>(heap_after_retry.psram_free));
            app.rollback_ui_start();
            return false;
        }

        ESP_LOGI(kTag,
                 "%s PASS: ready=FALSE journal=EXACT owned=EMPTY "
                 "tree=IDENTICAL heap=EXACT retry=PASS free=%u/%u",
                 failpoint_name(point),
                 static_cast<unsigned>(heap_after_retry.internal_free),
                 static_cast<unsigned>(heap_after_retry.psram_free));
    }

    ESP_LOGI(kTag,
             "Display canvas startup fault matrix PASS "
             "(D0 strict + D1/D2 journal + retry)");
    return true;
}

bool run_display_stack_startup_fault_matrix(const bsp_display_cfg_t *cfg)
{
    if (cfg == nullptr || esp_lv_adapter_is_initialized()) {
        ESP_LOGE(kDisplayStackTag,
                 "Display stack matrix requires a clean adapter and config");
        return false;
    }

    cyclescope_display_stack_t stack =
        CYCLESCOPE_DISPLAY_STACK_INITIALIZER;
    if (!run_normal_stack_probe(
            stack, cfg, "D0 stack warm transaction", false)
        || !run_normal_stack_probe(
            stack, cfg, "D0 stack settle transaction", false)
        || !run_normal_stack_probe(
            stack, cfg, "D0 stack strict transaction", true)) {
        return false;
    }

    for (cyclescope_display_lifecycle_failpoint_t point
         : kDisplayStackFailpoints) {
        const size_t acquired_stages = static_cast<size_t>(point);
        reset_stack_observations();
        const HeapSnapshot heap_before = capture_heap();
        arm_stack_failpoint(point);
        const esp_err_t error = cyclescope_display_stack_init(&stack, cfg);
        wait_for_stack_cleanup();
        const HeapSnapshot heap_after_fault = capture_heap();
        const bool consumed = stack_failpoint_consumed_exactly_once(point);
        const bool clean = cyclescope_display_stack_resources_released(&stack)
                           && !esp_lv_adapter_is_initialized();
        const bool integrity_ok = heap_caps_check_integrity_all(true);
        const bool heap_exact =
            heap_before.internal_free == heap_after_fault.internal_free
            && heap_before.psram_free == heap_after_fault.psram_free;
        const bool lifecycle_exact =
            stack_journal_matches(acquired_stages, true);
        const bool idempotent =
            cyclescope_display_stack_destroy(&stack) == ESP_OK;

        if (error == ESP_OK || !consumed || !clean || !integrity_ok
            || !heap_exact || !lifecycle_exact || !idempotent) {
            ESP_LOGE(kDisplayStackTag,
                     "%s FAIL: error=%s consumed=%u clean=%u integrity=%u "
                     "heap=%u lifecycle=%u idempotent=%u events=%u "
                     "free=%u/%u->%u/%u",
                     stack_failpoint_name(point), esp_err_to_name(error),
                     static_cast<unsigned>(consumed),
                     static_cast<unsigned>(clean),
                     static_cast<unsigned>(integrity_ok),
                     static_cast<unsigned>(heap_exact),
                     static_cast<unsigned>(lifecycle_exact),
                     static_cast<unsigned>(idempotent),
                     static_cast<unsigned>(g_stack_lifecycle_event_count),
                     static_cast<unsigned>(heap_before.internal_free),
                     static_cast<unsigned>(heap_before.psram_free),
                     static_cast<unsigned>(heap_after_fault.internal_free),
                     static_cast<unsigned>(heap_after_fault.psram_free));
            arm_stack_failpoint(CYCLESCOPE_DISPLAY_FAIL_NONE);
            return false;
        }

        // The same lifecycle object must support a complete retry after every
        // injected post-acquisition failure.
        reset_stack_observations();
        const esp_err_t retry_error =
            cyclescope_display_stack_init(&stack, cfg);
        const bool retry_committed =
            retry_error == ESP_OK && stack.display != nullptr
            && stack.touch != nullptr && stack.touch_indev != nullptr;
        const esp_err_t retry_destroy_error =
            retry_committed
                ? cyclescope_display_stack_destroy(&stack)
                : ESP_ERR_INVALID_STATE;
        wait_for_stack_cleanup();
        const HeapSnapshot heap_after_retry = capture_heap();
        const bool retry_clean =
            cyclescope_display_stack_resources_released(&stack)
            && !esp_lv_adapter_is_initialized();
        const bool retry_integrity_ok = heap_caps_check_integrity_all(true);
        const bool retry_heap_exact =
            heap_before.internal_free == heap_after_retry.internal_free
            && heap_before.psram_free == heap_after_retry.psram_free;
        const bool retry_lifecycle_exact = stack_journal_matches(7U, false)
                                           && no_stack_failpoint_consumed();

        if (!retry_committed || retry_destroy_error != ESP_OK
            || !retry_clean || !retry_integrity_ok || !retry_heap_exact
            || !retry_lifecycle_exact) {
            ESP_LOGE(kDisplayStackTag,
                     "%s retry FAIL: init=%s destroy=%s committed=%u "
                     "clean=%u integrity=%u heap=%u lifecycle=%u events=%u "
                     "free=%u/%u->%u/%u",
                     stack_failpoint_name(point),
                     esp_err_to_name(retry_error),
                     esp_err_to_name(retry_destroy_error),
                     static_cast<unsigned>(retry_committed),
                     static_cast<unsigned>(retry_clean),
                     static_cast<unsigned>(retry_integrity_ok),
                     static_cast<unsigned>(retry_heap_exact),
                     static_cast<unsigned>(retry_lifecycle_exact),
                     static_cast<unsigned>(g_stack_lifecycle_event_count),
                     static_cast<unsigned>(heap_before.internal_free),
                     static_cast<unsigned>(heap_before.psram_free),
                     static_cast<unsigned>(heap_after_retry.internal_free),
                     static_cast<unsigned>(heap_after_retry.psram_free));
            return false;
        }

        ESP_LOGI(kDisplayStackTag,
                 "%s PASS: journal=EXACT owned=EMPTY heap=EXACT "
                 "retry=PASS free=%u/%u",
                 stack_failpoint_name(point),
                 static_cast<unsigned>(heap_after_retry.internal_free),
                 static_cast<unsigned>(heap_after_retry.psram_free));
    }

    ESP_LOGI(kDisplayStackTag,
             "Display stack lifecycle matrix PASS "
             "(D0 strict + D3-D9 journal + retry)");
    return true;
}

}  // namespace cyclescope::startup_fault_test
