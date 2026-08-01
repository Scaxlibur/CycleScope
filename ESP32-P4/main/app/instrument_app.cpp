#include "instrument_app.hpp"

#include <algorithm>
#include <inttypes.h>
#include <stdio.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
#include "cyclescope_display_startup_fault_test.hpp"
#endif

namespace cyclescope {
namespace {

constexpr int32_t kScreenPadding = 24;
constexpr int32_t kHeaderHeight = 68;
constexpr int32_t kStatusHeight = 32;
constexpr int32_t kFooterHeight = 38;
constexpr int32_t kMetricsWidth = 278;
constexpr int32_t kCardRadius = 12;
constexpr int32_t kMetricCardWidth = 116;
constexpr int32_t kMetricHorizontalPadding = 14;
constexpr uint32_t kLiveUiPeriodMs = 250;
constexpr uint32_t kStartupLiveUiPeriodMs = 20;
constexpr uint32_t kStartupLiveUiWindowMs = 1000;
static_assert(live_stream_freshness::kTimeoutMs + kLiveUiPeriodMs <= 2000U);

constexpr uint32_t kBackground = 0x08111B;
constexpr uint32_t kHeader = 0x0E1D2C;
constexpr uint32_t kCard = 0x102A3D;
constexpr uint32_t kCardBorder = 0x23445B;
constexpr uint32_t kText = 0xE6F1F8;
constexpr uint32_t kMutedText = 0x89A6B9;
constexpr uint32_t kAccent = 0x20D6B5;
constexpr uint32_t kAccentDark = 0x135C58;
constexpr uint32_t kTrace = 0x75E6FF;

lv_obj_t *create_text(lv_obj_t *parent, const char *text, const lv_font_t *font, uint32_t color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    return label;
}

lv_obj_t *style_metric(lv_obj_t *card, const char *title, const char *value, int32_t x, int32_t y)
{
    lv_obj_set_pos(card, x, y);

    lv_obj_t *title_label = create_text(card, title, &lv_font_montserrat_12, kMutedText);
    lv_obj_align(title_label, LV_ALIGN_TOP_LEFT,
                 kMetricHorizontalPadding, 12);

    lv_obj_t *value_label = create_text(card, value, &lv_font_montserrat_22, kText);
    lv_obj_set_width(
        value_label,
        kMetricCardWidth - 2 * kMetricHorizontalPadding);
    lv_label_set_long_mode(value_label, LV_LABEL_LONG_MODE_CLIP);
    lv_obj_align(value_label, LV_ALIGN_BOTTOM_LEFT,
                 kMetricHorizontalPadding, -12);
    return value_label;
}

bool text_fits(const char *text, const lv_font_t *font, int32_t width)
{
    lv_point_t size{};
    lv_text_get_size(
        &size, text, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    return size.x <= width;
}

bool metric_text_contract_passes(int32_t spectrum_legend_width)
{
    constexpr const char *kWorstMetricValues[] = {
        "0.250", "250.00", "4.0625",
    };
    const int32_t metric_content_width =
        kMetricCardWidth - 2 * kMetricHorizontalPadding;
    for (const char *value : kWorstMetricValues) {
        if (!text_fits(value, &lv_font_montserrat_22,
                       metric_content_width)) {
            return false;
        }
    }
    return text_fits(
        "500.00k 250mVpk  •  500.00k 250mVpk  •  500.00k 250mVpk  •  +5",
        &lv_font_montserrat_12, spectrum_legend_width);
}

void format_peak_summary(char *buffer, size_t buffer_size,
                         const SpectrumDisplayFrame &spectrum,
                         size_t visible_peak_count)
{
    const size_t peak_count = std::min(
        visible_peak_count,
        std::min(static_cast<size_t>(spectrum.peak_count),
                 spectrum.peaks.size()));
    if (peak_count >= 3U) {
        const unsigned extra_peaks =
            peak_count > 3U ? static_cast<unsigned>(peak_count - 3U) : 0U;
        if (extra_peaks > 0U) {
            snprintf(buffer, buffer_size, "%.2fk %.0fmVpk  •  %.2fk %.0fmVpk  •  %.2fk %.0fmVpk  •  +%u",
                     static_cast<double>(spectrum.peaks[0].frequency_hz / 1000.0F),
                     static_cast<double>(spectrum.peaks[0].amplitude_volts_peak * 1000.0F),
                     static_cast<double>(spectrum.peaks[1].frequency_hz / 1000.0F),
                     static_cast<double>(spectrum.peaks[1].amplitude_volts_peak * 1000.0F),
                     static_cast<double>(spectrum.peaks[2].frequency_hz / 1000.0F),
                     static_cast<double>(spectrum.peaks[2].amplitude_volts_peak * 1000.0F), extra_peaks);
        } else {
            snprintf(buffer, buffer_size, "%.2fk %.0fmVpk  •  %.2fk %.0fmVpk  •  %.2fk %.0fmVpk",
                     static_cast<double>(spectrum.peaks[0].frequency_hz / 1000.0F),
                     static_cast<double>(spectrum.peaks[0].amplitude_volts_peak * 1000.0F),
                     static_cast<double>(spectrum.peaks[1].frequency_hz / 1000.0F),
                     static_cast<double>(spectrum.peaks[1].amplitude_volts_peak * 1000.0F),
                     static_cast<double>(spectrum.peaks[2].frequency_hz / 1000.0F),
                     static_cast<double>(spectrum.peaks[2].amplitude_volts_peak * 1000.0F));
        }
    } else if (peak_count == 2U) {
        snprintf(buffer, buffer_size, "%.2fk %.0fmVpk  •  %.2fk %.0fmVpk",
                 static_cast<double>(spectrum.peaks[0].frequency_hz / 1000.0F),
                 static_cast<double>(spectrum.peaks[0].amplitude_volts_peak * 1000.0F),
                 static_cast<double>(spectrum.peaks[1].frequency_hz / 1000.0F),
                 static_cast<double>(spectrum.peaks[1].amplitude_volts_peak * 1000.0F));
    } else if (peak_count == 1U) {
        snprintf(buffer, buffer_size, "%.2fk %.0fmVpk",
                 static_cast<double>(spectrum.peaks[0].frequency_hz / 1000.0F),
                 static_cast<double>(spectrum.peaks[0].amplitude_volts_peak * 1000.0F));
    } else {
        snprintf(buffer, buffer_size, "NO SPECTRAL PEAKS");
    }
}

}  // namespace

InstrumentApp::InstrumentApp(lv_display_t *display) : display_(display)
{
}

bool InstrumentApp::start_ui()
{
    if (ui_started_) {
        return true;
    }

    if (!ui_start_resources_released()) {
        ESP_LOGE("cyclescope_ui",
                 "Cannot start UI over a partial or connected layout");
        return false;
    }
    if (!build_layout()) {
        const bool rollback_ok = rollback_ui_start();
        ESP_LOGE("cyclescope_ui",
                 "Instrument UI shell FAILED; canvas rollback: %s",
                 rollback_ok ? "CLEAN" : "FAILED");
        return false;
    }
    select_periods(WaveformView::kPeriodsInCapture);
    select_view(View::Time);
    ui_started_ = true;
    lv_label_set_text(source_label_, "SOURCE  CSLP   •   NETWORK STARTING");
    lv_label_set_text(footer_left_, "UI READY");
    ESP_LOGI("cyclescope_ui", "Instrument UI shell ready; network starting");
    return true;
}

bool InstrumentApp::prepare_live_data()
{
    if (!ui_started_) {
        ESP_LOGE("cyclescope_ui",
                 "Cannot prepare CSLP analysis before the UI shell");
        return false;
    }
    if (pipeline_prepared_) {
        return true;
    }
    pipeline_prepared_ = live_pipeline_.prepare();
    ESP_LOGI("cyclescope_ui",
             "Instrument analysis preparation: %s",
             pipeline_prepared_ ? "READY" : "FAILED");
    return pipeline_prepared_;
}

bool InstrumentApp::connect(CslpUdpReceiver *receiver)
{
    if (!ui_started_) {
        ESP_LOGE("cyclescope_ui",
                 "Cannot connect CSLP before the instrument UI is prepared");
        return false;
    }
    if (live_mode_) {
        return true;
    }
    if (!pipeline_prepared_) {
        lv_label_set_text(
            source_label_,
            "SOURCE  CSLP OFFLINE   •   ANALYSIS INIT FAILED");
        return false;
    }
    if (receiver == nullptr) {
        lv_label_set_text(
            source_label_,
            "SOURCE  CSLP OFFLINE   •   NO LOCAL FALLBACK");
        ESP_LOGI("cyclescope_ui",
                 "Instrument UI started; formal CSLP FFT8192: FAILED");
        return false;
    }

    // Allocate the UI consumer before starting the permanent analysis task.
    // This keeps a timer allocation failure from leaving an orphan producer.
    live_data_timer_ =
        lv_timer_create(on_live_data_timer, kStartupLiveUiPeriodMs, this);
    if (live_data_timer_ == nullptr) {
        lv_label_set_text(
            source_label_,
            "SOURCE  CSLP OFFLINE   •   UI TIMER FAILED");
        ESP_LOGI("cyclescope_ui",
                 "Instrument UI started; formal CSLP FFT8192: UI TIMER FAILED");
        return false;
    }

    live_timer_started_ms_ = lv_tick_get();
    live_mode_ = live_pipeline_.start(receiver);
    if (!live_mode_) {
        lv_timer_delete(live_data_timer_);
        live_data_timer_ = nullptr;
    } else {
        lv_timer_ready(live_data_timer_);
    }
    if (live_mode_) {
        lv_label_set_text(source_label_, "SOURCE  CSLP   •   WAITING FOR FRAME");
    } else {
        lv_label_set_text(
            source_label_,
            "SOURCE  CSLP OFFLINE   •   ANALYSIS START FAILED");
    }
    ESP_LOGI("cyclescope_ui", "Instrument UI started; formal CSLP FFT8192: %s",
             live_mode_ ? "RUNNING" : "FAILED");
    return live_mode_;
}

bool InstrumentApp::build_layout()
{
    if (display_ == nullptr) {
        ESP_LOGE("cyclescope_ui", "Cannot build UI without a display");
        return false;
    }
    const int32_t width = lv_display_get_horizontal_resolution(display_);
    const int32_t height = lv_display_get_vertical_resolution(display_);
    const int32_t content_top = kHeaderHeight + kStatusHeight + 10;
    const int32_t content_bottom = height - kFooterHeight - kScreenPadding;
    const int32_t content_height = content_bottom - content_top;
    const int32_t plot_width = width - kScreenPadding * 3 - kMetricsWidth;
    if (!metric_text_contract_passes(plot_width - 36)) {
        ESP_LOGE("cyclescope_ui",
                 "Metric/legend text exceeds the configured card width");
        return false;
    }

    lv_obj_t *screen = lv_display_get_screen_active(display_);
    if (screen == nullptr) {
        ESP_LOGE("cyclescope_ui", "Display has no active screen");
        return false;
    }
    lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(screen, lv_color_hex(kBackground), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(screen, 0, 0);

    ui_root_ = lv_obj_create(screen);
    if (ui_root_ == nullptr) {
        ESP_LOGE("cyclescope_ui", "Unable to create instrument UI root");
        return false;
    }
    lv_obj_remove_style_all(ui_root_);
    lv_obj_set_pos(ui_root_, 0, 0);
    lv_obj_set_size(ui_root_, width, height);
    lv_obj_clear_flag(ui_root_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(ui_root_, LV_OBJ_FLAG_CLICKABLE);

    lv_obj_t *header = lv_obj_create(ui_root_);
    lv_obj_set_size(header, width, kHeaderHeight);
    lv_obj_set_pos(header, 0, 0);
    lv_obj_clear_flag(header, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(header, lv_color_hex(kHeader), 0);
    lv_obj_set_style_border_width(header, 0, 0);
    lv_obj_set_style_radius(header, 0, 0);
    lv_obj_set_style_pad_all(header, 0, 0);

    lv_obj_t *title = create_text(header, "CycleScope", &lv_font_montserrat_22, kText);
    lv_obj_align(title, LV_ALIGN_LEFT_MID, kScreenPadding, -8);

    lv_obj_t *subtitle = create_text(header, "PERIODIC SIGNAL ANALYZER", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(subtitle, LV_ALIGN_LEFT_MID, kScreenPadding + 2, 18);

    time_button_ = create_mode_button(header, "TIME", width - 280, 116);
    spectrum_button_ = create_mode_button(header, "FFT", width - 152, 116);
    lv_obj_add_event_cb(time_button_, on_time_view_clicked, LV_EVENT_CLICKED, this);
    lv_obj_add_event_cb(spectrum_button_, on_spectrum_view_clicked, LV_EVENT_CLICKED, this);

    lv_obj_t *status = lv_obj_create(ui_root_);
    lv_obj_set_size(status, width - kScreenPadding * 2, kStatusHeight);
    lv_obj_set_pos(status, kScreenPadding, kHeaderHeight + 4);
    lv_obj_clear_flag(status, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_opa(status, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(status, 0, 0);
    lv_obj_set_style_pad_all(status, 0, 0);

    mode_label_ = create_text(status, "MODE  TIME DOMAIN", &lv_font_montserrat_12, kAccent);
    lv_obj_align(mode_label_, LV_ALIGN_LEFT_MID, 0, 0);
    source_label_ = create_text(status, "SOURCE  CSLP   •   WAITING FOR FRAME",
                                &lv_font_montserrat_12, kMutedText);
    lv_obj_align(source_label_, LV_ALIGN_RIGHT_MID, 0, 0);

    lv_obj_t *plot_card = create_card(ui_root_, kScreenPadding, content_top, plot_width, content_height);
    plot_title_ = create_text(plot_card, "TIME DOMAIN", &lv_font_montserrat_18, kText);
    lv_obj_align(plot_title_, LV_ALIGN_TOP_LEFT, 18, 16);

    one_period_button_ = create_period_button(plot_card, "1P", plot_width - 132);
    three_period_button_ = create_period_button(plot_card, "3P", plot_width - 68);
    lv_obj_add_event_cb(one_period_button_, on_one_period_clicked, LV_EVENT_CLICKED, this);
    lv_obj_add_event_cb(three_period_button_, on_three_periods_clicked, LV_EVENT_CLICKED, this);

    spectrum_less_button_ =
        create_period_button(plot_card, "-", plot_width - 224);
    spectrum_more_button_ =
        create_period_button(plot_card, "+", plot_width - 68);
    spectrum_line_count_label_ = create_text(
        plot_card, "LINES --/8", &lv_font_montserrat_12, kAccent);
    lv_obj_set_pos(spectrum_line_count_label_, plot_width - 164, 20);
    lv_obj_set_width(spectrum_line_count_label_, 92);
    lv_obj_set_style_text_align(
        spectrum_line_count_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_add_event_cb(
        spectrum_less_button_, on_spectrum_less_clicked,
        LV_EVENT_CLICKED, this);
    lv_obj_add_event_cb(
        spectrum_more_button_, on_spectrum_more_clicked,
        LV_EVENT_CLICKED, this);

    lv_obj_t *divider = lv_obj_create(plot_card);
    lv_obj_set_size(divider, plot_width - 36, 1);
    lv_obj_align(divider, LV_ALIGN_TOP_MID, 0, 48);
    lv_obj_set_style_bg_color(divider, lv_color_hex(kCardBorder), 0);
    lv_obj_set_style_border_width(divider, 0, 0);
    lv_obj_set_style_radius(divider, 0, 0);

    plot_hint_ = create_text(plot_card, "M4 WAVEFORM READY", &lv_font_montserrat_16, kTrace);
    lv_obj_align(plot_hint_, LV_ALIGN_CENTER, 0, -4);

    plot_subhint_ = create_text(plot_card, "ESP-DSP 8192-POINT ANALYSIS", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(plot_subhint_, LV_ALIGN_CENTER, 0, 26);

    if (!waveform_.create(
            plot_card, 18, 72, plot_width - 36, content_height - 126)) {
        ESP_LOGE("cyclescope_ui", "Waveform canvas creation failed");
        return false;
    }
    if (!spectrum_view_.create(
            plot_card, 18, 72, plot_width - 36, content_height - 126,
            &spectrum_model_)) {
        ESP_LOGE("cyclescope_ui",
                 "Spectrum canvas creation failed; rolling back waveform");
        spectrum_view_.destroy();
        waveform_.destroy();
        return false;
    }
    timebase_label_ = create_text(plot_card, "WAITING FOR CSLP WAVEFORM",
                                  &lv_font_montserrat_12, kMutedText);
    lv_obj_align(timebase_label_, LV_ALIGN_BOTTOM_LEFT, 18, -18);
    spectrum_legend_ = create_text(plot_card, "WAITING FOR CSLP FFT RESULT",
                                   &lv_font_montserrat_12, kMutedText);
    lv_obj_align(spectrum_legend_, LV_ALIGN_BOTTOM_LEFT, 18, -18);
    lv_obj_add_flag(spectrum_legend_, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *metrics = create_card(ui_root_, kScreenPadding * 2 + plot_width, content_top, kMetricsWidth, content_height);
    lv_obj_t *metrics_title = create_text(metrics, "MEASUREMENTS", &lv_font_montserrat_18, kText);
    lv_obj_align(metrics_title, LV_ALIGN_TOP_LEFT, 18, 16);

    constexpr int32_t metric_height = 94;
    lv_obj_t *vpp = create_card(
        metrics, 14, 58, kMetricCardWidth, metric_height);
    vpp_value_ = style_metric(vpp, "Vpp / V", "--", 14, 58);
    lv_obj_t *rms = create_card(
        metrics, 148, 58, kMetricCardWidth, metric_height);
    rms_value_ = style_metric(rms, "TRUE RMS / V", "--", 148, 58);
    lv_obj_t *fundamental = create_card(
        metrics, 14, 166, kMetricCardWidth, metric_height);
    fundamental_value_ = style_metric(
        fundamental, "F0 / kHz", "--", 14, 166);
    lv_obj_t *sample_rate = create_card(
        metrics, 148, 166, kMetricCardWidth, metric_height);
    sample_rate_value_ = style_metric(
        sample_rate, "Fs / MS/s", "--", 148, 166);

    lv_obj_t *summary = create_card(metrics, 14, 278, kMetricsWidth - 28, 118);
    lv_obj_t *summary_title = create_text(summary, "ACTIVE VIEW", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(summary_title, LV_ALIGN_TOP_LEFT, 14, 12);
    active_view_value_ = create_text(summary, "TIME", &lv_font_montserrat_22, kAccent);
    lv_obj_align(active_view_value_, LV_ALIGN_LEFT_MID, 14, 10);
    lv_obj_t *summary_hint = create_text(summary, "TOUCH MODE BUTTONS TO SWITCH", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(summary_hint, LV_ALIGN_BOTTOM_LEFT, 14, -12);

    lv_obj_t *footer = lv_obj_create(ui_root_);
    lv_obj_set_size(footer, width - kScreenPadding * 2, kFooterHeight);
    lv_obj_set_pos(footer, kScreenPadding, height - kFooterHeight - 8);
    lv_obj_clear_flag(footer, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(footer, lv_color_hex(kHeader), 0);
    lv_obj_set_style_border_width(footer, 0, 0);
    lv_obj_set_style_radius(footer, 8, 0);
    lv_obj_set_style_pad_all(footer, 0, 0);

    footer_left_ = create_text(footer, "UI STARTING", &lv_font_montserrat_12, kAccent);
    lv_obj_align(footer_left_, LV_ALIGN_LEFT_MID, 14, 0);
    lv_obj_t *footer_right = create_text(footer, "ESP-DSP  •  FFT 8192", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(footer_right, LV_ALIGN_RIGHT_MID, -14, 0);
    return waveform_.created() && spectrum_view_.created();
}

bool InstrumentApp::rollback_ui_start()
{
    // This is intentionally only a pre-connect rollback. LiveDataPipeline has
    // no stop/join API yet, so deleting an active timer/UI would create a UAF.
    if (pipeline_prepared_ || live_mode_ || live_data_timer_ != nullptr) {
        ESP_LOGE("cyclescope_ui",
                 "Refusing UI startup rollback after analysis ownership began");
        return false;
    }

    spectrum_view_.destroy();
    waveform_.destroy();
    if (ui_root_ != nullptr) {
        lv_obj_delete(ui_root_);
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
        startup_fault_test::note_display_lifecycle_event(
            startup_fault_test::DisplayLifecycleEvent::UiRootDestroyed);
#endif
    }
    clear_ui_object_pointers();
    active_view_ = View::Time;
    live_frame_ = {};
    ui_started_ = false;
    live_timer_started_ms_ = 0;
    ui_frames_applied_ = 0;
    last_ui_tick_ms_ = 0;
    maximum_ui_gap_ms_ = 0;
    last_health_log_ms_ = 0;
    last_render_session_id_ = 0;
    last_render_frame_id_ = 0;
    live_stream_stale_ = false;
    stale_transport_ready_ = false;
    return ui_start_resources_released();
}

bool InstrumentApp::ui_start_resources_released() const
{
    return ui_root_ == nullptr && time_button_ == nullptr
           && spectrum_button_ == nullptr && one_period_button_ == nullptr
           && three_period_button_ == nullptr
           && spectrum_less_button_ == nullptr
           && spectrum_more_button_ == nullptr
           && spectrum_line_count_label_ == nullptr
           && mode_label_ == nullptr
           && plot_title_ == nullptr && plot_hint_ == nullptr
           && plot_subhint_ == nullptr && active_view_value_ == nullptr
           && vpp_value_ == nullptr && rms_value_ == nullptr
           && fundamental_value_ == nullptr && sample_rate_value_ == nullptr
           && timebase_label_ == nullptr && spectrum_legend_ == nullptr
           && source_label_ == nullptr && footer_left_ == nullptr
           && waveform_.resources_released()
           && spectrum_view_.resources_released()
           && live_data_timer_ == nullptr && !ui_started_
           && !pipeline_prepared_ && !live_mode_ && !live_stream_stale_
           && !stale_transport_ready_;
}

void InstrumentApp::clear_ui_object_pointers()
{
    ui_root_ = nullptr;
    time_button_ = nullptr;
    spectrum_button_ = nullptr;
    one_period_button_ = nullptr;
    three_period_button_ = nullptr;
    spectrum_less_button_ = nullptr;
    spectrum_more_button_ = nullptr;
    spectrum_line_count_label_ = nullptr;
    mode_label_ = nullptr;
    plot_title_ = nullptr;
    plot_hint_ = nullptr;
    plot_subhint_ = nullptr;
    active_view_value_ = nullptr;
    vpp_value_ = nullptr;
    rms_value_ = nullptr;
    fundamental_value_ = nullptr;
    sample_rate_value_ = nullptr;
    timebase_label_ = nullptr;
    spectrum_legend_ = nullptr;
    source_label_ = nullptr;
    footer_left_ = nullptr;
}

lv_obj_t *InstrumentApp::create_card(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height) const
{
    lv_obj_t *card = lv_obj_create(parent);
    lv_obj_set_pos(card, x, y);
    lv_obj_set_size(card, width, height);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(card, lv_color_hex(kCard), 0);
    lv_obj_set_style_border_color(card, lv_color_hex(kCardBorder), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_radius(card, kCardRadius, 0);
    lv_obj_set_style_pad_all(card, 0, 0);
    return card;
}

lv_obj_t *InstrumentApp::create_mode_button(lv_obj_t *parent, const char *text, int32_t x, int32_t width)
{
    lv_obj_t *button = lv_button_create(parent);
    lv_obj_set_pos(button, x, 14);
    lv_obj_set_size(button, width, 40);
    lv_obj_set_style_radius(button, 8, 0);
    lv_obj_set_style_border_width(button, 1, 0);
    lv_obj_set_style_border_color(button, lv_color_hex(kCardBorder), 0);
    lv_obj_set_style_pad_all(button, 0, 0);

    lv_obj_t *label = create_text(button, text, &lv_font_montserrat_16, kText);
    lv_obj_center(label);
    return button;
}

void InstrumentApp::set_mode_button_selected(lv_obj_t *button, bool selected) const
{
    lv_obj_set_style_bg_color(button, lv_color_hex(selected ? kAccentDark : kCard), 0);
    lv_obj_set_style_border_color(button, lv_color_hex(selected ? kAccent : kCardBorder), 0);
}

void InstrumentApp::select_view(View view)
{
    active_view_ = view;
    const bool is_time = active_view_ == View::Time;
    set_mode_button_selected(time_button_, is_time);
    set_mode_button_selected(spectrum_button_, !is_time);

    lv_label_set_text(mode_label_, is_time ? "MODE  TIME DOMAIN" : "MODE  FREQUENCY DOMAIN");
    lv_label_set_text(plot_title_, is_time ? "TIME DOMAIN" : "FREQUENCY DOMAIN");
    waveform_.set_visible(is_time);
    spectrum_view_.set_visible(!is_time);
    if (is_time) {
        lv_obj_remove_flag(one_period_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(three_period_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(spectrum_less_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(spectrum_more_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(
            spectrum_line_count_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_hint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_subhint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(timebase_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(spectrum_legend_, LV_OBJ_FLAG_HIDDEN);
        if (waveform_.has_frame()) {
            update_time_metrics();
        }
    } else {
        lv_obj_add_flag(one_period_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(three_period_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(spectrum_less_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(spectrum_more_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(
            spectrum_line_count_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_hint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_subhint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(timebase_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(spectrum_legend_, LV_OBJ_FLAG_HIDDEN);
        if (live_frame_.generation != 0) {
            update_spectrum_metrics();
        }
        update_spectrum_line_controls();
    }
    lv_label_set_text(active_view_value_, is_time ? "TIME" : "FFT");
}

void InstrumentApp::select_periods(uint8_t periods)
{
    waveform_.set_periods(periods);
    set_mode_button_selected(one_period_button_, periods == 1);
    set_mode_button_selected(three_period_button_, periods == 3);

    update_timebase_label();
    if (waveform_.has_frame()) {
        update_time_metrics();
        ESP_LOGI("cyclescope_ui",
                 "CSLP waveform set to %u period(s); envelope peak preservation: %s",
                 periods,
                 waveform_.peak_preservation_verified() ? "PASS" : "FAIL");
    }
}

void InstrumentApp::adjust_spectrum_peak_count(int8_t delta)
{
    const uint8_t available = spectrum_view_.available_peak_count();
    const uint8_t visible = spectrum_view_.visible_peak_count();
    if (available == 0U || visible == 0U || delta == 0) {
        return;
    }

    uint8_t target = visible;
    if (delta < 0 && visible > 1U) {
        target = static_cast<uint8_t>(visible - 1U);
    } else if (delta > 0 && visible < available
               && visible < kMaximumDisplayedSpectralLines) {
        target = static_cast<uint8_t>(visible + 1U);
    }
    if (target == visible
        || !spectrum_view_.set_visible_peak_count(target)) {
        return;
    }

    update_spectrum_line_controls();
    char summary[96];
    format_peak_summary(
        summary, sizeof(summary), live_frame_.spectrum,
        spectrum_view_.visible_peak_count());
    lv_label_set_text(spectrum_legend_, summary);
    ESP_LOGI("cyclescope_ui",
             "Spectrum display lines=%u/%u axis=%.2f..%.2fkHz",
             spectrum_view_.visible_peak_count(), available,
             static_cast<double>(
                 spectrum_view_.visible_frequency_minimum_hz() / 1000.0F),
             static_cast<double>(
                 spectrum_view_.visible_frequency_maximum_hz() / 1000.0F));
}

void InstrumentApp::update_spectrum_line_controls()
{
    const uint8_t available = spectrum_view_.available_peak_count();
    const uint8_t visible = spectrum_view_.visible_peak_count();
    char label[24];
    if (available == 0U || visible == 0U) {
        snprintf(label, sizeof(label), "LINES --/%u",
                 static_cast<unsigned>(kMaximumDisplayedSpectralLines));
    } else {
        snprintf(label, sizeof(label), "LINES %u/%u", visible, available);
    }
    lv_label_set_text(spectrum_line_count_label_, label);

    if (visible <= 1U) {
        lv_obj_add_state(spectrum_less_button_, LV_STATE_DISABLED);
    } else {
        lv_obj_remove_state(spectrum_less_button_, LV_STATE_DISABLED);
    }
    if (available == 0U || visible >= available
        || visible >= kMaximumDisplayedSpectralLines) {
        lv_obj_add_state(spectrum_more_button_, LV_STATE_DISABLED);
    } else {
        lv_obj_remove_state(spectrum_more_button_, LV_STATE_DISABLED);
    }
}

void InstrumentApp::update_timebase_label()
{
    if (!waveform_.has_frame()) {
        lv_label_set_text(timebase_label_, "WAITING FOR CSLP WAVEFORM");
        return;
    }
    char timebase[80];
    snprintf(timebase, sizeof(timebase),
             "%.1f mV/div   •   %u period%s   •   %.1f us span",
             static_cast<double>(waveform_.volts_per_division() * 1000.0F),
             waveform_.periods(), waveform_.periods() == 1 ? "" : "s",
             static_cast<double>(waveform_.span_us()));
    lv_label_set_text(timebase_label_, timebase);
}

void InstrumentApp::update_time_metrics()
{
    char value[24];
    snprintf(value, sizeof(value), "%.3f", static_cast<double>(waveform_.peak_to_peak_volts()));
    lv_label_set_text(vpp_value_, value);
    snprintf(value, sizeof(value), "%.3f", static_cast<double>(waveform_.rms_volts()));
    lv_label_set_text(rms_value_, value);
    snprintf(value, sizeof(value), "%.2f", static_cast<double>(waveform_.fundamental_hz() / 1000.0F));
    lv_label_set_text(fundamental_value_, value);
    snprintf(value, sizeof(value), "%.4f", static_cast<double>(waveform_.sample_rate_hz() / 1000000.0F));
    lv_label_set_text(sample_rate_value_, value);
}

void InstrumentApp::update_spectrum_metrics()
{
    char value[24];
    if (live_frame_.generation != 0) {
        snprintf(value, sizeof(value), "%.3f",
                 static_cast<double>(live_frame_.voltage_peak_to_peak));
        lv_label_set_text(vpp_value_, value);
        snprintf(value, sizeof(value), "%.3f",
                 static_cast<double>(live_frame_.true_rms_volts));
        lv_label_set_text(rms_value_, value);
        snprintf(value, sizeof(value), "%.2f",
                 static_cast<double>(live_frame_.fundamental_hz / 1000.0F));
        lv_label_set_text(fundamental_value_, value);
        snprintf(value, sizeof(value), "%.4f",
                 static_cast<double>(live_frame_.sample_rate_hz / 1000000.0F));
        lv_label_set_text(sample_rate_value_, value);
        return;
    }
    snprintf(value, sizeof(value), "%.3f",
             static_cast<double>(spectrum_model_.voltage_peak_to_peak()));
    lv_label_set_text(vpp_value_, value);
    snprintf(value, sizeof(value), "%.3f", static_cast<double>(spectrum_model_.true_rms_volts()));
    lv_label_set_text(rms_value_, value);
    snprintf(value, sizeof(value), "%.2f", static_cast<double>(spectrum_model_.fundamental_hz() / 1000.0F));
    lv_label_set_text(fundamental_value_, value);
    snprintf(value, sizeof(value), "%.4f", static_cast<double>(spectrum_model_.sample_rate_hz() / 1000000.0F));
    lv_label_set_text(sample_rate_value_, value);

}

void InstrumentApp::on_live_data_timer(lv_timer_t *timer)
{
    auto *app = static_cast<InstrumentApp *>(lv_timer_get_user_data(timer));
    if (app->live_pipeline_.try_receive_latest(&app->live_frame_)) {
        const bool first_frame = app->ui_frames_applied_ == 0U;
        app->apply_live_measurement(app->live_frame_);
        if (first_frame) {
            lv_timer_set_period(timer, kLiveUiPeriodMs);
        }
        return;
    }
    if (app->ui_frames_applied_ == 0U
        && lv_tick_get() - app->live_timer_started_ms_
               >= kStartupLiveUiWindowMs) {
        lv_timer_set_period(timer, kLiveUiPeriodMs);
    }
    app->update_live_stream_state(app->live_pipeline_.stream_ready(),
                                  lv_tick_get());
}

void InstrumentApp::update_live_stream_state(bool transport_ready,
                                             uint32_t now_ms)
{
    const bool has_valid_frame = ui_frames_applied_ != 0U;
    const uint32_t freshness_anchor_ms =
        has_valid_frame ? last_ui_tick_ms_ : live_timer_started_ms_;
    const live_stream_freshness::DisplayState state =
        live_stream_freshness::classify(
            has_valid_frame, live_stream_stale_, transport_ready, now_ms,
            freshness_anchor_ms);
    if (state == live_stream_freshness::DisplayState::Waiting
        || state == live_stream_freshness::DisplayState::Live) {
        return;
    }

    const bool entering_stale = !live_stream_stale_;
    const bool transport_changed =
        live_stream_stale_
        && stale_transport_ready_ != transport_ready;
    if (!entering_stale && !transport_changed) {
        return;
    }

    live_stream_stale_ = true;
    stale_transport_ready_ = transport_ready;
    const char *const transport = transport_ready ? "ONLINE" : "OFFLINE";
    char value[96];
    if (has_valid_frame) {
        snprintf(value, sizeof(value),
                 "SOURCE  CSLP %s   •   STALE   •   LAST FRAME %" PRIu32,
                 transport, last_render_frame_id_);
    } else {
        snprintf(value, sizeof(value),
                 "SOURCE  CSLP %s   •   STALE   •   NO VALID FRAME",
                 transport);
    }
    lv_label_set_text(source_label_, value);
    if (has_valid_frame) {
        snprintf(value, sizeof(value), "STALE   •   LAST FRAME %" PRIu32,
                 last_render_frame_id_);
    } else {
        snprintf(value, sizeof(value), "STALE   •   NO VALID FRAME");
    }
    lv_label_set_text(footer_left_, value);
    if (entering_stale) {
        const uint32_t age_ms =
            static_cast<uint32_t>(now_ms - freshness_anchor_ms);
        ESP_LOGW("cyclescope_ui",
                 "CSLP UI stream state: %s -> STALE; transport=%s"
                 " reason=%s age=%" PRIu32 "ms; last session=%08" PRIX32
                 " frame=%" PRIu32 "; %s",
                 has_valid_frame ? "LIVE" : "WAITING", transport,
                 transport_ready ? "NO_VALID_FRAME" : "TRANSPORT_OFFLINE",
                 age_ms, last_render_session_id_, last_render_frame_id_,
                 has_valid_frame ? "retaining waveform and measurements"
                                 : "no valid frame received");
    } else {
        ESP_LOGW("cyclescope_ui",
                 "CSLP UI stale transport: %s; last session=%08" PRIX32
                 " frame=%" PRIu32,
                 transport, last_render_session_id_, last_render_frame_id_);
    }
}

void InstrumentApp::apply_live_measurement(const DynamicMeasurementFrame &frame)
{
    // This method is called by an LVGL timer, therefore in the adapter's UI
    // task.  It is the only M6 consumer that touches LVGL objects.
    const uint32_t now_ms = lv_tick_get();
    const bool recovered_from_stale = live_stream_stale_;
    const bool session_changed =
        last_render_session_id_ != 0
        && frame.session_id != last_render_session_id_;
    if (session_changed) {
        last_ui_tick_ms_ = 0;
        maximum_ui_gap_ms_ = 0;
    }
    if (last_ui_tick_ms_ != 0) {
        const uint32_t gap_ms = now_ms - last_ui_tick_ms_;
        if (gap_ms > maximum_ui_gap_ms_) {
            maximum_ui_gap_ms_ = gap_ms;
        }
    }
    last_ui_tick_ms_ = now_ms;
    ++ui_frames_applied_;

    // The UI owns its copy. Core 1 can immediately reuse either dense FFT
    // buffer after the fixed-capacity display frame reaches this callback.
    if (ui_frames_applied_ == 1U
        || frame.session_id != last_render_session_id_
        || frame.frame_id != last_render_frame_id_) {
        waveform_.set_frame(frame.waveform);
        spectrum_view_.set_frame(frame.spectrum);
        update_spectrum_line_controls();
        last_render_session_id_ = frame.session_id;
        last_render_frame_id_ = frame.frame_id;
        update_timebase_label();
    }

    if (ui_frames_applied_ == 1U) {
        ESP_LOGI("cyclescope_ui",
                 "Spectrum UI bridge on Core %d: session=%08" PRIX32
                 " frame=%" PRIu32 " gen=%" PRIu32
                 " A/B=%u columns=%u peaks=%u Fs=%.4fMHz axis=%.5fMHz"
                 " Amax=%.1fmVpk",
                 xPortGetCoreID(), frame.session_id, frame.frame_id,
                 frame.spectrum.generation,
                 frame.spectrum.source_buffer_index,
                 frame.spectrum.column_count,
                 frame.spectrum.peak_count,
                 static_cast<double>(frame.sample_rate_hz / 1000000.0F),
                 static_cast<double>(frame.spectrum.frequency_max_hz / 1000000.0F),
                 static_cast<double>(
                     frame.spectrum.amplitude_max_volts * 1000.0F));
    }

    char value[96];
    snprintf(value, sizeof(value), "%.3f", static_cast<double>(frame.voltage_peak_to_peak));
    lv_label_set_text(vpp_value_, value);
    snprintf(value, sizeof(value), "%.3f", static_cast<double>(frame.true_rms_volts));
    lv_label_set_text(rms_value_, value);
    snprintf(value, sizeof(value), "%.2f", static_cast<double>(frame.fundamental_hz / 1000.0F));
    lv_label_set_text(fundamental_value_, value);
    snprintf(value, sizeof(value), "%.4f", static_cast<double>(frame.sample_rate_hz / 1000000.0F));
    lv_label_set_text(sample_rate_value_, value);

    const bool calibrated =
        (frame.source_flags & cslp::kFlagCalibrated) != 0;
    const bool test_pattern =
        (frame.source_flags & cslp::kFlagTestPattern) != 0;
    snprintf(value, sizeof(value),
             "SOURCE  CSLP %s   •   %s   •   FRAME %lu",
             test_pattern ? "TEST" : "LIVE",
             calibrated ? "CAL" : "NOMINAL",
             static_cast<unsigned long>(frame.frame_id));
    lv_label_set_text(source_label_, value);
    snprintf(value, sizeof(value), "%s / GEN #%lu",
             active_view_ == View::Time ? "TIME" : "FFT",
             static_cast<unsigned long>(frame.generation));
    lv_label_set_text(active_view_value_, value);
    snprintf(value, sizeof(value), "LIVE  %lu frames", static_cast<unsigned long>(ui_frames_applied_));
    lv_label_set_text(footer_left_, value);
    live_stream_stale_ = false;
    stale_transport_ready_ = false;
    if (recovered_from_stale) {
        ESP_LOGI("cyclescope_ui",
                 "CSLP UI stream state: STALE -> LIVE; session=%08" PRIX32
                 " frame=%" PRIu32,
                 frame.session_id, frame.frame_id);
    } else if (ui_frames_applied_ == 1U) {
        ESP_LOGI("cyclescope_ui",
                 "CSLP UI stream state: WAITING -> LIVE; session=%08" PRIX32
                 " frame=%" PRIu32,
                 frame.session_id, frame.frame_id);
    }
    format_peak_summary(
        value, sizeof(value), frame.spectrum,
        spectrum_view_.visible_peak_count());
    lv_label_set_text(spectrum_legend_, value);

    if (now_ms - last_health_log_ms_ >= 30000U) {
        last_health_log_ms_ = now_ms;
        const PipelineStats stats = live_pipeline_.stats();
        ESP_LOGI("cyclescope_fft",
                 "health: acquired=%lu analyzed=%lu published=%lu ui=%lu stale=%lu "
                 "invalid=%lu failures=%lu ui_overwrite=%lu "
                 "fft_us(last/avg/max)=%lu/%lu/%lu selftest=%s max_ui_gap=%lums free=%lu",
                 static_cast<unsigned long>(stats.acquired_frames), static_cast<unsigned long>(stats.analyzed_frames),
                 static_cast<unsigned long>(stats.published_frames), static_cast<unsigned long>(ui_frames_applied_),
                 static_cast<unsigned long>(stats.stale_results),
                 static_cast<unsigned long>(stats.invalid_frames),
                 static_cast<unsigned long>(stats.fft_failures),
                 static_cast<unsigned long>(stats.ui_overwrites),
                 static_cast<unsigned long>(stats.last_analysis_us),
                 static_cast<unsigned long>(stats.average_analysis_us),
                 static_cast<unsigned long>(stats.maximum_analysis_us), stats.fft_self_test_passed ? "PASS" : "FAIL",
                 static_cast<unsigned long>(maximum_ui_gap_ms_),
                 static_cast<unsigned long>(esp_get_free_heap_size()));
    }
}

void InstrumentApp::on_time_view_clicked(lv_event_t *event)
{
    auto *app = static_cast<InstrumentApp *>(lv_event_get_user_data(event));
    app->select_view(View::Time);
}

void InstrumentApp::on_spectrum_view_clicked(lv_event_t *event)
{
    auto *app = static_cast<InstrumentApp *>(lv_event_get_user_data(event));
    app->select_view(View::Spectrum);
}

void InstrumentApp::on_one_period_clicked(lv_event_t *event)
{
    auto *app = static_cast<InstrumentApp *>(lv_event_get_user_data(event));
    app->select_periods(1);
}

void InstrumentApp::on_three_periods_clicked(lv_event_t *event)
{
    auto *app = static_cast<InstrumentApp *>(lv_event_get_user_data(event));
    app->select_periods(3);
}

void InstrumentApp::on_spectrum_less_clicked(lv_event_t *event)
{
    auto *app = static_cast<InstrumentApp *>(lv_event_get_user_data(event));
    app->adjust_spectrum_peak_count(-1);
}

void InstrumentApp::on_spectrum_more_clicked(lv_event_t *event)
{
    auto *app = static_cast<InstrumentApp *>(lv_event_get_user_data(event));
    app->adjust_spectrum_peak_count(1);
}

lv_obj_t *InstrumentApp::create_period_button(lv_obj_t *parent, const char *text, int32_t x)
{
    lv_obj_t *button = lv_button_create(parent);
    lv_obj_set_pos(button, x, 12);
    lv_obj_set_size(button, 52, 32);
    lv_obj_set_style_radius(button, 6, 0);
    lv_obj_set_style_border_width(button, 1, 0);
    lv_obj_set_style_border_color(button, lv_color_hex(kCardBorder), 0);
    lv_obj_set_style_pad_all(button, 0, 0);

    lv_obj_t *label = create_text(button, text, &lv_font_montserrat_12, kText);
    lv_obj_center(label);
    return button;
}

}  // namespace cyclescope
