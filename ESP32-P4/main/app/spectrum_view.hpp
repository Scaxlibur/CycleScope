#pragma once

#include "lvgl.h"

#include "spectrum_model.hpp"

namespace cyclescope {

class SpectrumView {
public:
    void create(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height, const SpectrumModel *model);
    void set_visible(bool visible);

private:
    static void on_draw(lv_event_t *event);
    void draw(lv_event_t *event) const;

    lv_obj_t *object_ = nullptr;
    const SpectrumModel *model_ = nullptr;
};

}  // namespace cyclescope
