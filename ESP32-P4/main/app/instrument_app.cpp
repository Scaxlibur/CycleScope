#include "instrument_app.hpp"

#include <stdio.h>

#include "esp_heap_caps.h"
#include "esp_log.h"

namespace cyclescope {
namespace {

constexpr int32_t kScreenPadding = 24;
constexpr int32_t kHeaderHeight = 68;
constexpr int32_t kStatusHeight = 32;
constexpr int32_t kFooterHeight = 38;
constexpr int32_t kMetricsWidth = 278;
constexpr int32_t kCardRadius = 12;

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
    lv_obj_align(title_label, LV_ALIGN_TOP_LEFT, 14, 12);

    lv_obj_t *value_label = create_text(card, value, &lv_font_montserrat_22, kText);
    lv_obj_align(value_label, LV_ALIGN_BOTTOM_LEFT, 14, -12);
    return value_label;
}

}  // namespace

InstrumentApp::InstrumentApp(lv_display_t *display) : display_(display)
{
}

void InstrumentApp::start()
{
    build_layout();
    select_periods(WaveformView::kPeriodsInCapture);
    select_view(View::Time);
    live_mode_ = live_pipeline_.start();
    if (live_mode_) {
        live_data_timer_ = lv_timer_create(on_live_data_timer, 50, this);
    }
    ESP_LOGI("cyclescope_ui", "Instrument UI started; waveform envelope: %s, spectrum vector: %s, M6: %s",
             waveform_.peak_preservation_verified() ? "PASS" : "FAIL",
             spectrum_model_.validation_passed() ? "PASS" : "FAIL", live_mode_ ? "RUNNING" : "FAILED");
}

void InstrumentApp::build_layout()
{
    const int32_t width = lv_display_get_horizontal_resolution(display_);
    const int32_t height = lv_display_get_vertical_resolution(display_);
    const int32_t content_top = kHeaderHeight + kStatusHeight + 10;
    const int32_t content_bottom = height - kFooterHeight - kScreenPadding;
    const int32_t content_height = content_bottom - content_top;
    const int32_t plot_width = width - kScreenPadding * 3 - kMetricsWidth;

    lv_obj_t *screen = lv_screen_active();
    lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(screen, lv_color_hex(kBackground), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(screen, 0, 0);

    lv_obj_t *header = lv_obj_create(screen);
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

    lv_obj_t *status = lv_obj_create(screen);
    lv_obj_set_size(status, width - kScreenPadding * 2, kStatusHeight);
    lv_obj_set_pos(status, kScreenPadding, kHeaderHeight + 4);
    lv_obj_clear_flag(status, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_opa(status, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(status, 0, 0);
    lv_obj_set_style_pad_all(status, 0, 0);

    mode_label_ = create_text(status, "MODE  TIME DOMAIN", &lv_font_montserrat_12, kAccent);
    lv_obj_align(mode_label_, LV_ALIGN_LEFT_MID, 0, 0);
    source_label_ = create_text(status, "SOURCE  SYNTHETIC   •   LINK  STANDBY", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(source_label_, LV_ALIGN_RIGHT_MID, 0, 0);

    lv_obj_t *plot_card = create_card(screen, kScreenPadding, content_top, plot_width, content_height);
    plot_title_ = create_text(plot_card, "TIME DOMAIN", &lv_font_montserrat_18, kText);
    lv_obj_align(plot_title_, LV_ALIGN_TOP_LEFT, 18, 16);

    one_period_button_ = create_period_button(plot_card, "1P", plot_width - 132);
    three_period_button_ = create_period_button(plot_card, "3P", plot_width - 68);
    lv_obj_add_event_cb(one_period_button_, on_one_period_clicked, LV_EVENT_CLICKED, this);
    lv_obj_add_event_cb(three_period_button_, on_three_periods_clicked, LV_EVENT_CLICKED, this);

    lv_obj_t *divider = lv_obj_create(plot_card);
    lv_obj_set_size(divider, plot_width - 36, 1);
    lv_obj_align(divider, LV_ALIGN_TOP_MID, 0, 48);
    lv_obj_set_style_bg_color(divider, lv_color_hex(kCardBorder), 0);
    lv_obj_set_style_border_width(divider, 0, 0);
    lv_obj_set_style_radius(divider, 0, 0);

    plot_hint_ = create_text(plot_card, "M4 WAVEFORM READY", &lv_font_montserrat_16, kTrace);
    lv_obj_align(plot_hint_, LV_ALIGN_CENTER, 0, -4);

    plot_subhint_ = create_text(plot_card, "M5 ADDS THE DISCRETE SPECTRUM", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(plot_subhint_, LV_ALIGN_CENTER, 0, 26);

    waveform_.create(plot_card, 18, 72, plot_width - 36, content_height - 126);
    spectrum_view_.create(plot_card, 18, 72, plot_width - 36, content_height - 126, &spectrum_model_);
    timebase_label_ = create_text(plot_card, "500 mV/div   •   3 periods   •   30.0 us span", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(timebase_label_, LV_ALIGN_BOTTOM_LEFT, 18, -18);
    spectrum_legend_ = create_text(plot_card, "40.0 kHz  400 mV  •  80.0 kHz  120 mV  •  120.0 kHz  60 mV",
                                   &lv_font_montserrat_12, kMutedText);
    lv_obj_align(spectrum_legend_, LV_ALIGN_BOTTOM_LEFT, 18, -18);
    lv_obj_add_flag(spectrum_legend_, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *metrics = create_card(screen, kScreenPadding * 2 + plot_width, content_top, kMetricsWidth, content_height);
    lv_obj_t *metrics_title = create_text(metrics, "MEASUREMENTS", &lv_font_montserrat_18, kText);
    lv_obj_align(metrics_title, LV_ALIGN_TOP_LEFT, 18, 16);

    constexpr int32_t metric_width = 116;
    constexpr int32_t metric_height = 94;
    lv_obj_t *vpp = create_card(metrics, 14, 58, metric_width, metric_height);
    vpp_value_ = style_metric(vpp, "Vpp", "-- V", 14, 58);
    lv_obj_t *rms = create_card(metrics, 148, 58, metric_width, metric_height);
    rms_value_ = style_metric(rms, "RMS", "-- V", 148, 58);
    lv_obj_t *fundamental = create_card(metrics, 14, 166, metric_width, metric_height);
    fundamental_value_ = style_metric(fundamental, "F0", "-- kHz", 14, 166);
    lv_obj_t *sample_rate = create_card(metrics, 148, 166, metric_width, metric_height);
    sample_rate_value_ = style_metric(sample_rate, "Fs", "-- MS/s", 148, 166);

    lv_obj_t *summary = create_card(metrics, 14, 278, kMetricsWidth - 28, 118);
    lv_obj_t *summary_title = create_text(summary, "ACTIVE VIEW", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(summary_title, LV_ALIGN_TOP_LEFT, 14, 12);
    active_view_value_ = create_text(summary, "TIME", &lv_font_montserrat_22, kAccent);
    lv_obj_align(active_view_value_, LV_ALIGN_LEFT_MID, 14, 10);
    lv_obj_t *summary_hint = create_text(summary, "TOUCH MODE BUTTONS TO SWITCH", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(summary_hint, LV_ALIGN_BOTTOM_LEFT, 14, -12);

    lv_obj_t *footer = lv_obj_create(screen);
    lv_obj_set_size(footer, width - kScreenPadding * 2, kFooterHeight);
    lv_obj_set_pos(footer, kScreenPadding, height - kFooterHeight - 8);
    lv_obj_clear_flag(footer, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(footer, lv_color_hex(kHeader), 0);
    lv_obj_set_style_border_width(footer, 0, 0);
    lv_obj_set_style_radius(footer, 8, 0);
    lv_obj_set_style_pad_all(footer, 0, 0);

    footer_left_ = create_text(footer, "UI READY", &lv_font_montserrat_12, kAccent);
    lv_obj_align(footer_left_, LV_ALIGN_LEFT_MID, 14, 0);
    lv_obj_t *footer_right = create_text(footer, "M6  •  QUEUED LIVE DATA", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(footer_right, LV_ALIGN_RIGHT_MID, -14, 0);
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
        lv_obj_add_flag(plot_hint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_subhint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(timebase_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(spectrum_legend_, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(source_label_, "SOURCE  SYNTHETIC   •   LINK  STANDBY");
        if (!live_mode_) {
            update_time_metrics();
        }
    } else {
        lv_obj_add_flag(one_period_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(three_period_button_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_hint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(plot_subhint_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(timebase_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(spectrum_legend_, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(source_label_, "SOURCE  TEST VECTOR   •   FFT 512   •   500 Hz/bin");
        if (!live_mode_) {
            update_spectrum_metrics();
        }
    }
    lv_label_set_text(active_view_value_, is_time ? "TIME" : "FFT");
}

void InstrumentApp::select_periods(uint8_t periods)
{
    waveform_.set_periods(periods);
    set_mode_button_selected(one_period_button_, periods == 1);
    set_mode_button_selected(three_period_button_, periods == 3);

    char timebase[72];
    const float span_us = static_cast<float>(periods) * 1000000.0F / waveform_.fundamental_hz();
    snprintf(timebase, sizeof(timebase), "500 mV/div   •   %u period%s   •   %.1f us span", periods,
             periods == 1 ? "" : "s", static_cast<double>(span_us));
    lv_label_set_text(timebase_label_, timebase);
    update_time_metrics();
    ESP_LOGI("cyclescope_ui", "Waveform view set to %u period(s); envelope peak preservation: %s", periods,
             waveform_.peak_preservation_verified() ? "PASS" : "FAIL");
}

void InstrumentApp::update_time_metrics()
{
    char value[24];
    snprintf(value, sizeof(value), "%.3f V", static_cast<double>(waveform_.peak_to_peak_volts()));
    lv_label_set_text(vpp_value_, value);
    snprintf(value, sizeof(value), "%.3f V", static_cast<double>(waveform_.rms_volts()));
    lv_label_set_text(rms_value_, value);
    snprintf(value, sizeof(value), "%.1f kHz", static_cast<double>(waveform_.fundamental_hz() / 1000.0F));
    lv_label_set_text(fundamental_value_, value);
    snprintf(value, sizeof(value), "%.1f MS/s", static_cast<double>(waveform_.sample_rate_hz() / 1000000.0F));
    lv_label_set_text(sample_rate_value_, value);
}

void InstrumentApp::update_spectrum_metrics()
{
    char value[24];
    snprintf(value, sizeof(value), "%.3f V", static_cast<double>(spectrum_model_.voltage_peak_to_peak()));
    lv_label_set_text(vpp_value_, value);
    snprintf(value, sizeof(value), "%.3f V", static_cast<double>(spectrum_model_.true_rms_volts()));
    lv_label_set_text(rms_value_, value);
    snprintf(value, sizeof(value), "%.1f kHz", static_cast<double>(spectrum_model_.fundamental_hz() / 1000.0F));
    lv_label_set_text(fundamental_value_, value);
    snprintf(value, sizeof(value), "%.1f kS/s", static_cast<double>(spectrum_model_.sample_rate_hz() / 1000.0F));
    lv_label_set_text(sample_rate_value_, value);

    const auto &lines = spectrum_model_.lines();
    ESP_LOGI("cyclescope_fft", "M5 FFT: F0 %.1f kHz, lines %.0f/%.0f/%.0f mV, Vpp %.6f V, RMS %.6f V, %s",
             static_cast<double>(spectrum_model_.fundamental_hz() / 1000.0F),
             static_cast<double>(lines[0].amplitude_volts_peak * 1000.0F),
             static_cast<double>(lines[1].amplitude_volts_peak * 1000.0F),
             static_cast<double>(lines[2].amplitude_volts_peak * 1000.0F),
             static_cast<double>(spectrum_model_.voltage_peak_to_peak()),
             static_cast<double>(spectrum_model_.true_rms_volts()),
             spectrum_model_.validation_passed() ? "PASS" : "FAIL");
}

void InstrumentApp::on_live_data_timer(lv_timer_t *timer)
{
    auto *app = static_cast<InstrumentApp *>(lv_timer_get_user_data(timer));
    DynamicMeasurementFrame frame{};
    if (app->live_pipeline_.try_receive_latest(&frame)) {
        app->apply_live_measurement(frame);
    }
}

void InstrumentApp::apply_live_measurement(const DynamicMeasurementFrame &frame)
{
    // This method is called by an LVGL timer, therefore in the adapter's UI
    // task.  It is the only M6 consumer that touches LVGL objects.
    const uint32_t now_ms = lv_tick_get();
    if (last_ui_tick_ms_ != 0) {
        const uint32_t gap_ms = now_ms - last_ui_tick_ms_;
        if (gap_ms > maximum_ui_gap_ms_) {
            maximum_ui_gap_ms_ = gap_ms;
        }
    }
    last_ui_tick_ms_ = now_ms;
    ++ui_frames_applied_;

    // The numeric result remains live for every UI frame.  The time-domain
    // view retains its M4 min/max reference trace: redrawing ~640 columns for
    // each live result would monopolize the early-P4 DSI path.  In contrast,
    // the FFT view has only three spectral lines and can update every frame.
    if (active_view_ == View::Spectrum && frame.sequence != last_spectrum_render_sequence_) {
        spectrum_view_.set_lines(frame.spectral_lines);
        last_spectrum_render_sequence_ = frame.sequence;
    }

    char value[96];
    snprintf(value, sizeof(value), "%.3f V", static_cast<double>(frame.voltage_peak_to_peak));
    lv_label_set_text(vpp_value_, value);
    snprintf(value, sizeof(value), "%.3f V", static_cast<double>(frame.true_rms_volts));
    lv_label_set_text(rms_value_, value);
    snprintf(value, sizeof(value), "%.2f kHz", static_cast<double>(frame.fundamental_hz / 1000.0F));
    lv_label_set_text(fundamental_value_, value);
    snprintf(value, sizeof(value), "%.1f kS/s", static_cast<double>(frame.sample_rate_hz / 1000.0F));
    lv_label_set_text(sample_rate_value_, value);

    snprintf(value, sizeof(value), "SOURCE  FPGA SIM   •   FRAME %lu   •   LINK LIVE",
             static_cast<unsigned long>(frame.sequence));
    lv_label_set_text(source_label_, value);
    snprintf(value, sizeof(value), "%s / LIVE #%lu", active_view_ == View::Time ? "TIME" : "FFT",
             static_cast<unsigned long>(frame.sequence));
    lv_label_set_text(active_view_value_, value);
    snprintf(value, sizeof(value), "LIVE  %lu frames", static_cast<unsigned long>(ui_frames_applied_));
    lv_label_set_text(footer_left_, value);
    snprintf(value, sizeof(value), "%.2fk %.0fmV  •  %.2fk %.0fmV  •  %.2fk %.0fmV",
             static_cast<double>(frame.spectral_lines[0].frequency_hz / 1000.0F),
             static_cast<double>(frame.spectral_lines[0].amplitude_volts_peak * 1000.0F),
             static_cast<double>(frame.spectral_lines[1].frequency_hz / 1000.0F),
             static_cast<double>(frame.spectral_lines[1].amplitude_volts_peak * 1000.0F),
             static_cast<double>(frame.spectral_lines[2].frequency_hz / 1000.0F),
             static_cast<double>(frame.spectral_lines[2].amplitude_volts_peak * 1000.0F));
    lv_label_set_text(spectrum_legend_, value);

    if (now_ms - last_health_log_ms_ >= 30000U) {
        last_health_log_ms_ = now_ms;
        const PipelineStats stats = live_pipeline_.stats();
        ESP_LOGI("cyclescope_m6", "health: rx=%lu analyzed=%lu published=%lu ui=%lu dropped=%lu max_ui_gap=%lums free=%lu",
                 static_cast<unsigned long>(stats.received_frames), static_cast<unsigned long>(stats.analyzed_frames),
                 static_cast<unsigned long>(stats.published_frames), static_cast<unsigned long>(ui_frames_applied_),
                 static_cast<unsigned long>(stats.dropped_raw_frames), static_cast<unsigned long>(maximum_ui_gap_ms_),
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
