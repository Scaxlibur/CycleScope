#pragma once

#include "lvgl.h"

#include "waveform_view.hpp"
#include "spectrum_model.hpp"
#include "spectrum_view.hpp"
#include "live_data_pipeline.hpp"

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
    static void on_live_data_timer(lv_timer_t *timer);

    void build_layout();
    void select_view(View view);
    void select_periods(uint8_t periods);
    void update_time_metrics();
    void update_spectrum_metrics();
    void apply_live_measurement(const DynamicMeasurementFrame &frame);
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
    lv_obj_t *spectrum_legend_ = nullptr;
    lv_obj_t *source_label_ = nullptr;
    lv_obj_t *footer_left_ = nullptr;
    WaveformView waveform_;
    SpectrumModel spectrum_model_;
    SpectrumView spectrum_view_;
    LiveDataPipeline live_pipeline_;
    DynamicMeasurementFrame live_frame_{};
    lv_timer_t *live_data_timer_ = nullptr;
    bool live_mode_ = false;
    uint32_t ui_frames_applied_ = 0;
    uint32_t last_ui_tick_ms_ = 0;
    uint32_t maximum_ui_gap_ms_ = 0;
    uint32_t last_health_log_ms_ = 0;
    uint32_t last_spectrum_render_sequence_ = 0;
};

}  // namespace cyclescope
