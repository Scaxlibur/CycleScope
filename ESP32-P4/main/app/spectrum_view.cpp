#include "spectrum_view.hpp"

namespace cyclescope {
namespace {

constexpr uint32_t kGridDivisionsX = 8;
constexpr uint32_t kGridDivisionsY = 5;
constexpr uint32_t kGridColor = 0x23445B;
constexpr uint32_t kAxisColor = 0x44758B;
constexpr uint32_t kFundamentalColor = 0x20D6B5;
constexpr uint32_t kHarmonicColor = 0x75E6FF;
constexpr float kMaximumDisplayAmplitude = 0.5F;

}  // namespace

void SpectrumView::create(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height,
                          const SpectrumModel *model)
{
    model_ = model;
    lines_ = model_->lines();
    object_ = lv_obj_create(parent);
    lv_obj_remove_style_all(object_);
    lv_obj_set_pos(object_, x, y);
    lv_obj_set_size(object_, width, height);
    lv_obj_clear_flag(object_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(object_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(object_, on_draw, LV_EVENT_DRAW_MAIN, this);
    lv_obj_add_flag(object_, LV_OBJ_FLAG_HIDDEN);
}

void SpectrumView::set_lines(const std::array<SpectralLine, SpectrumModel::kLineCount> &lines)
{
    lines_ = lines;
    if (object_ != nullptr) {
        lv_obj_invalidate(object_);
    }
}

void SpectrumView::set_visible(bool visible)
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

void SpectrumView::on_draw(lv_event_t *event)
{
    auto *view = static_cast<SpectrumView *>(lv_event_get_user_data(event));
    view->draw(event);
}

void SpectrumView::draw(lv_event_t *event) const
{
    if (model_ == nullptr) {
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
    lv_point_precise_set(&line.p1, coords.x1, coords.y2);
    lv_point_precise_set(&line.p2, coords.x2, coords.y2);
    lv_draw_line(layer, &line);

    const float nyquist_hz = model_->sample_rate_hz() / 2.0F;
    for (size_t index = 0; index < lines_.size(); ++index) {
        const SpectralLine &spectral_line = lines_[index];
        const int32_t x = coords.x1 + static_cast<int32_t>(spectral_line.frequency_hz * static_cast<float>(width) / nyquist_hz);
        const float normalized = spectral_line.amplitude_volts_peak / kMaximumDisplayAmplitude;
        const int32_t y = coords.y2 - static_cast<int32_t>(normalized * static_cast<float>(height));
        line.color = lv_color_hex(index == 0 ? kFundamentalColor : kHarmonicColor);
        line.width = index == 0 ? 5 : 3;
        lv_point_precise_set(&line.p1, x, coords.y2);
        lv_point_precise_set(&line.p2, x, y);
        lv_draw_line(layer, &line);
    }
}

}  // namespace cyclescope
