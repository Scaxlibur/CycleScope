#pragma once

#include <array>
#include <cstdint>

#include "lvgl.h"

#include "spectrum_frame.hpp"
#include "spectrum_model.hpp"

namespace cyclescope {

class SpectrumView {
public:
    SpectrumView() = default;
    SpectrumView(const SpectrumView &) = delete;
    SpectrumView &operator=(const SpectrumView &) = delete;

    bool create(lv_obj_t *parent, int32_t x, int32_t y,
                int32_t width, int32_t height,
                const SpectrumModel *model);
    void destroy();
    bool created() const;
    bool resources_released() const;
    bool visible() const;
    void set_visible(bool visible);
    void set_frame(const SpectrumDisplayFrame &frame);

private:
    void initialize_from_model(const SpectrumModel &model);
    void render_frame();
    void update_axis_labels();
    void draw_vertical_line(int32_t x, int32_t y1, int32_t y2, uint16_t color, int32_t line_width = 1);
    void set_pixel(int32_t x, int32_t y, uint16_t color);

    lv_obj_t *object_ = nullptr;
    lv_obj_t *canvas_ = nullptr;
    uint16_t *canvas_pixels_ = nullptr;
    int32_t canvas_width_ = 0;
    int32_t canvas_height_ = 0;
    bool visible_ = false;
    std::array<lv_obj_t *, 8> axis_labels_{};
    SpectrumDisplayFrame frame_{};
};

}  // namespace cyclescope
