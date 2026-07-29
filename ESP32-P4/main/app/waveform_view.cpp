#include "waveform_view.hpp"

#include <math.h>

#include "esp_log.h"

namespace cyclescope {
namespace {

constexpr float kPi = 3.14159265358979323846F;
constexpr uint32_t kGridDivisionsX = 10;
constexpr uint32_t kGridDivisionsY = 8;
constexpr uint32_t kGridColor = 0x23445B;
constexpr uint32_t kAxisColor = 0x44758B;
constexpr uint32_t kTraceColor = 0x75E6FF;

}  // namespace

WaveformView::WaveformView()
{
    generate_synthetic_capture();
}

void WaveformView::create(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height)
{
    object_ = lv_obj_create(parent);
    lv_obj_remove_style_all(object_);
    lv_obj_set_pos(object_, x, y);
    lv_obj_set_size(object_, width, height);
    lv_obj_clear_flag(object_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(object_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(object_, on_draw, LV_EVENT_DRAW_MAIN, this);

    // LVGL layout resolution is asynchronous.  This viewport has a fixed
    // pixel width, so retain the requested geometry for the first envelope
    // build instead of querying an object whose layout is still dirty.
    viewport_width_ = width;
    rebuild_envelope();
}

void WaveformView::set_visible(bool visible)
{
    if (object_ == nullptr) {
        return;
    }

    if (visible) {
        lv_obj_remove_flag(object_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_invalidate(object_);
    } else {
        lv_obj_add_flag(object_, LV_OBJ_FLAG_HIDDEN);
    }
}

void WaveformView::set_periods(uint8_t periods)
{
    if (periods != 1 && periods != kPeriodsInCapture) {
        return;
    }

    periods_ = periods;
    rebuild_envelope();
    if (object_ != nullptr) {
        lv_obj_invalidate(object_);
    }
}

uint8_t WaveformView::periods() const
{
    return periods_;
}

float WaveformView::peak_to_peak_volts() const
{
    return peak_to_peak_volts_;
}

float WaveformView::rms_volts() const
{
    return rms_volts_;
}

float WaveformView::fundamental_hz() const
{
    return kFundamentalHz;
}

float WaveformView::sample_rate_hz() const
{
    return kSampleRateHz;
}

bool WaveformView::peak_preservation_verified() const
{
    return peak_preservation_verified_;
}

void WaveformView::on_draw(lv_event_t *event)
{
    auto *view = static_cast<WaveformView *>(lv_event_get_user_data(event));
    view->draw(event);
}

void WaveformView::generate_synthetic_capture()
{
    for (size_t index = 0; index < samples_.size(); ++index) {
        const float phase = 2.0F * kPi * static_cast<float>(index % kSamplesPerPeriod)
                            / static_cast<float>(kSamplesPerPeriod);
        // 100 kHz fundamental plus deterministic second and third harmonics.
        samples_[index] = 0.50F * sinf(phase)
                          + 0.14F * sinf(2.0F * phase + 0.35F)
                          - 0.08F * sinf(3.0F * phase - 0.70F);
    }
}

void WaveformView::rebuild_envelope()
{
    if (object_ == nullptr) {
        return;
    }

    const int32_t width = viewport_width_;
    if (width <= 0) {
        envelope_columns_ = 0;
        peak_preservation_verified_ = false;
        return;
    }

    envelope_columns_ = static_cast<size_t>(width);
    if (envelope_columns_ > envelope_.size()) {
        envelope_columns_ = envelope_.size();
    }

    const size_t visible_samples = static_cast<size_t>(periods_) * kSamplesPerPeriod;
    float source_minimum = samples_[0];
    float source_maximum = samples_[0];
    float envelope_minimum = samples_[0];
    float envelope_maximum = samples_[0];
    float squares = 0.0F;

    for (size_t column = 0; column < envelope_columns_; ++column) {
        const size_t first = column * visible_samples / envelope_columns_;
        size_t after_last = (column + 1U) * visible_samples / envelope_columns_;
        if (after_last <= first) {
            after_last = first + 1U;
        }

        float minimum = samples_[first];
        float maximum = samples_[first];
        for (size_t sample = first; sample < after_last; ++sample) {
            const float value = samples_[sample];
            if (value < minimum) {
                minimum = value;
            }
            if (value > maximum) {
                maximum = value;
            }
        }

        envelope_[column] = {minimum, maximum};
        if (minimum < envelope_minimum) {
            envelope_minimum = minimum;
        }
        if (maximum > envelope_maximum) {
            envelope_maximum = maximum;
        }
    }

    for (size_t sample = 0; sample < visible_samples; ++sample) {
        const float value = samples_[sample];
        if (value < source_minimum) {
            source_minimum = value;
        }
        if (value > source_maximum) {
            source_maximum = value;
        }
        squares += value * value;
    }

    peak_to_peak_volts_ = source_maximum - source_minimum;
    rms_volts_ = sqrtf(squares / static_cast<float>(visible_samples));
    peak_preservation_verified_ = fabsf(source_minimum - envelope_minimum) < 0.000001F
                                 && fabsf(source_maximum - envelope_maximum) < 0.000001F;
    ESP_LOGI("cyclescope_wave", "%uP envelope: source [%.7f, %.7f], envelope [%.7f, %.7f], %s", periods_,
             static_cast<double>(source_minimum), static_cast<double>(source_maximum),
             static_cast<double>(envelope_minimum), static_cast<double>(envelope_maximum),
             peak_preservation_verified_ ? "PASS" : "FAIL");
}

void WaveformView::draw(lv_event_t *event) const
{
    if (envelope_columns_ == 0) {
        return;
    }

    lv_obj_t *object = lv_event_get_target_obj(event);
    lv_layer_t *layer = lv_event_get_layer(event);
    lv_area_t coords;
    lv_obj_get_coords(object, &coords);

    lv_draw_line_dsc_t line;
    lv_draw_line_dsc_init(&line);
    line.color = lv_color_hex(kGridColor);
    line.width = 1;

    const int32_t width = coords.x2 - coords.x1;
    const int32_t height = coords.y2 - coords.y1;
    for (uint32_t division = 0; division <= kGridDivisionsX; ++division) {
        const int32_t x = coords.x1 + static_cast<int32_t>(division) * width / kGridDivisionsX;
        lv_point_precise_set(&line.p1, x, coords.y1);
        lv_point_precise_set(&line.p2, x, coords.y2);
        lv_draw_line(layer, &line);
    }
    for (uint32_t division = 0; division <= kGridDivisionsY; ++division) {
        const int32_t y = coords.y1 + static_cast<int32_t>(division) * height / kGridDivisionsY;
        lv_point_precise_set(&line.p1, coords.x1, y);
        lv_point_precise_set(&line.p2, coords.x2, y);
        lv_draw_line(layer, &line);
    }

    line.color = lv_color_hex(kAxisColor);
    line.width = 2;
    const int32_t zero_y = sample_to_y(0.0F, coords);
    lv_point_precise_set(&line.p1, coords.x1, zero_y);
    lv_point_precise_set(&line.p2, coords.x2, zero_y);
    lv_draw_line(layer, &line);

    line.color = lv_color_hex(kTraceColor);
    line.width = 1;
    for (size_t column = 0; column < envelope_columns_; ++column) {
        const int32_t x = coords.x1 + static_cast<int32_t>(column);
        lv_point_precise_set(&line.p1, x, sample_to_y(envelope_[column].minimum, coords));
        lv_point_precise_set(&line.p2, x, sample_to_y(envelope_[column].maximum, coords));
        lv_draw_line(layer, &line);
    }
}

int32_t WaveformView::sample_to_y(float sample, const lv_area_t &coords) const
{
    const float normalized = (kVerticalRangeVolts - sample) / (2.0F * kVerticalRangeVolts);
    const int32_t height = coords.y2 - coords.y1;
    return coords.y1 + static_cast<int32_t>(lroundf(normalized * static_cast<float>(height)));
}

}  // namespace cyclescope
