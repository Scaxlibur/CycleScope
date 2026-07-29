#include "instrument_app.hpp"

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

void style_metric(lv_obj_t *card, const char *title, const char *value, int32_t x, int32_t y)
{
    lv_obj_set_pos(card, x, y);

    lv_obj_t *title_label = create_text(card, title, &lv_font_montserrat_12, kMutedText);
    lv_obj_align(title_label, LV_ALIGN_TOP_LEFT, 14, 12);

    lv_obj_t *value_label = create_text(card, value, &lv_font_montserrat_22, kText);
    lv_obj_align(value_label, LV_ALIGN_BOTTOM_LEFT, 14, -12);
}

}  // namespace

InstrumentApp::InstrumentApp(lv_display_t *display) : display_(display)
{
}

void InstrumentApp::start()
{
    build_layout();
    select_view(View::Time);
    ESP_LOGI("cyclescope_ui", "Instrument UI skeleton started");
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
    lv_obj_t *source_label = create_text(status, "SOURCE  SYNTHETIC   •   LINK  STANDBY", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(source_label, LV_ALIGN_RIGHT_MID, 0, 0);

    lv_obj_t *plot_card = create_card(screen, kScreenPadding, content_top, plot_width, content_height);
    plot_title_ = create_text(plot_card, "TIME DOMAIN", &lv_font_montserrat_18, kText);
    lv_obj_align(plot_title_, LV_ALIGN_TOP_LEFT, 18, 16);

    lv_obj_t *divider = lv_obj_create(plot_card);
    lv_obj_set_size(divider, plot_width - 36, 1);
    lv_obj_align(divider, LV_ALIGN_TOP_MID, 0, 48);
    lv_obj_set_style_bg_color(divider, lv_color_hex(kCardBorder), 0);
    lv_obj_set_style_border_width(divider, 0, 0);
    lv_obj_set_style_radius(divider, 0, 0);

    plot_hint_ = create_text(plot_card, "WAITING FOR SYNTHETIC SIGNAL PIPELINE", &lv_font_montserrat_16, kTrace);
    lv_obj_align(plot_hint_, LV_ALIGN_CENTER, 0, -4);

    lv_obj_t *plot_subhint = create_text(plot_card, "M4 ADDS TRACE, GRID, SCALING, AND PERIOD SELECT", &lv_font_montserrat_12, kMutedText);
    lv_obj_align(plot_subhint, LV_ALIGN_CENTER, 0, 26);

    lv_obj_t *metrics = create_card(screen, kScreenPadding * 2 + plot_width, content_top, kMetricsWidth, content_height);
    lv_obj_t *metrics_title = create_text(metrics, "MEASUREMENTS", &lv_font_montserrat_18, kText);
    lv_obj_align(metrics_title, LV_ALIGN_TOP_LEFT, 18, 16);

    constexpr int32_t metric_width = 116;
    constexpr int32_t metric_height = 94;
    lv_obj_t *vpp = create_card(metrics, 14, 58, metric_width, metric_height);
    style_metric(vpp, "Vpp", "-- mV", 14, 58);
    lv_obj_t *rms = create_card(metrics, 148, 58, metric_width, metric_height);
    style_metric(rms, "RMS", "-- mV", 148, 58);
    lv_obj_t *fundamental = create_card(metrics, 14, 166, metric_width, metric_height);
    style_metric(fundamental, "F0", "-- kHz", 14, 166);
    lv_obj_t *sample_rate = create_card(metrics, 148, 166, metric_width, metric_height);
    style_metric(sample_rate, "Fs", "-- MS/s", 148, 166);

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

    lv_obj_t *footer_left = create_text(footer, "UI READY", &lv_font_montserrat_12, kAccent);
    lv_obj_align(footer_left, LV_ALIGN_LEFT_MID, 14, 0);
    lv_obj_t *footer_right = create_text(footer, "M3  •  DISPLAY / TOUCH VERIFIED", &lv_font_montserrat_12, kMutedText);
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
    lv_label_set_text(plot_hint_, is_time ? "WAITING FOR SYNTHETIC SIGNAL PIPELINE"
                                          : "WAITING FOR SYNTHETIC SPECTRUM PIPELINE");
    lv_label_set_text(active_view_value_, is_time ? "TIME" : "FFT");
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

}  // namespace cyclescope
