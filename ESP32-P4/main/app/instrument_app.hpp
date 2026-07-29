#pragma once

#include "lvgl.h"

#include "waveform_view.hpp"

namespace cyclescope {

class InstrumentApp {
public:
    explicit InstrumentApp(lv_display_t *display);

    void start();

private:
    enum class View {
        Time,
        Spectrum,
    };

    static void on_time_view_clicked(lv_event_t *event);
    static void on_spectrum_view_clicked(lv_event_t *event);
    static void on_one_period_clicked(lv_event_t *event);
    static void on_three_periods_clicked(lv_event_t *event);

    void build_layout();
    void select_view(View view);
    void select_periods(uint8_t periods);
    void update_time_metrics();
    lv_obj_t *create_card(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height) const;
    lv_obj_t *create_mode_button(lv_obj_t *parent, const char *text, int32_t x, int32_t width);
    lv_obj_t *create_period_button(lv_obj_t *parent, const char *text, int32_t x);
    void set_mode_button_selected(lv_obj_t *button, bool selected) const;

    lv_display_t *display_;
    View active_view_ = View::Time;
    lv_obj_t *time_button_ = nullptr;
    lv_obj_t *spectrum_button_ = nullptr;
    lv_obj_t *one_period_button_ = nullptr;
    lv_obj_t *three_period_button_ = nullptr;
    lv_obj_t *mode_label_ = nullptr;
    lv_obj_t *plot_title_ = nullptr;
    lv_obj_t *plot_hint_ = nullptr;
    lv_obj_t *plot_subhint_ = nullptr;
    lv_obj_t *active_view_value_ = nullptr;
    lv_obj_t *vpp_value_ = nullptr;
    lv_obj_t *rms_value_ = nullptr;
    lv_obj_t *fundamental_value_ = nullptr;
    lv_obj_t *sample_rate_value_ = nullptr;
    lv_obj_t *timebase_label_ = nullptr;
    lv_obj_t *source_label_ = nullptr;
    WaveformView waveform_;
};

}  // namespace cyclescope
