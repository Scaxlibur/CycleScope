#pragma once

#include "lvgl.h"

#include "live_stream_freshness.hpp"
#include "waveform_view.hpp"
#include "spectrum_model.hpp"
#include "spectrum_view.hpp"
#include "live_data_pipeline.hpp"

namespace cyclescope {

namespace startup_fault_test {
struct DisplayStartupFaultTestAccess;
bool run_display_canvas_startup_fault_matrix(lv_display_t *display);
}

class InstrumentApp {
public:
    explicit InstrumentApp(lv_display_t *display);

    bool start_ui();
    bool prepare_live_data();
    bool connect(CslpUdpReceiver *receiver);

private:
    enum class View {
        Time,
        Spectrum,
    };

    static void on_time_view_clicked(lv_event_t *event);
    static void on_spectrum_view_clicked(lv_event_t *event);
    static void on_one_period_clicked(lv_event_t *event);
    static void on_three_periods_clicked(lv_event_t *event);
    static void on_spectrum_less_clicked(lv_event_t *event);
    static void on_spectrum_more_clicked(lv_event_t *event);
    static void on_live_data_timer(lv_timer_t *timer);

    bool build_layout();
    bool rollback_ui_start();
    bool ui_start_resources_released() const;
    void clear_ui_object_pointers();
    void select_view(View view);
    void select_periods(uint8_t periods);
    void adjust_spectrum_peak_count(int8_t delta);
    void update_spectrum_line_controls();
    void update_measurement_values(float voltage_peak_to_peak,
                                   float true_rms_volts,
                                   float fundamental_hz,
                                   float sample_rate_hz);
    void update_time_metrics();
    void update_spectrum_metrics();
    void update_timebase_label();
    void update_live_stream_state(bool transport_ready, uint32_t now_ms);
    void apply_live_measurement(const DynamicMeasurementFrame &frame);
    lv_obj_t *create_card(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height) const;
    lv_obj_t *create_mode_button(lv_obj_t *parent, const char *text, int32_t x, int32_t width);
    lv_obj_t *create_period_button(lv_obj_t *parent, const char *text, int32_t x);
    void set_mode_button_selected(lv_obj_t *button, bool selected) const;

    friend bool startup_fault_test::run_display_canvas_startup_fault_matrix(
        lv_display_t *display);
    friend struct startup_fault_test::DisplayStartupFaultTestAccess;

    lv_display_t *display_;
    View active_view_ = View::Time;
    lv_obj_t *ui_root_ = nullptr;
    lv_obj_t *time_button_ = nullptr;
    lv_obj_t *spectrum_button_ = nullptr;
    lv_obj_t *one_period_button_ = nullptr;
    lv_obj_t *three_period_button_ = nullptr;
    lv_obj_t *spectrum_less_button_ = nullptr;
    lv_obj_t *spectrum_more_button_ = nullptr;
    lv_obj_t *spectrum_line_count_label_ = nullptr;
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
    bool ui_started_ = false;
    bool pipeline_prepared_ = false;
    bool live_mode_ = false;
    bool live_stream_stale_ = false;
    bool stale_transport_ready_ = false;
    uint32_t live_timer_started_ms_ = 0;
    uint32_t ui_frames_applied_ = 0;
    uint32_t last_ui_tick_ms_ = 0;
    uint32_t maximum_ui_gap_ms_ = 0;
    uint32_t last_health_log_ms_ = 0;
    uint32_t last_render_session_id_ = 0;
    uint32_t last_render_frame_id_ = 0;
};

}  // namespace cyclescope
