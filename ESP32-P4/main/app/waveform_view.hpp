#pragma once

#include <cstdint>

#include "lvgl.h"

#include "waveform_frame.hpp"

namespace cyclescope {

// LVGL-only consumer of the immutable 1P/3P envelopes produced on Core 1.
// It never reads receiver slots or performs FFT work.
class WaveformView {
public:
    static constexpr uint32_t kPeriodsInCapture = 3;

    WaveformView() = default;
    WaveformView(const WaveformView &) = delete;
    WaveformView &operator=(const WaveformView &) = delete;

    bool create(lv_obj_t *parent, int32_t x, int32_t y,
                int32_t width, int32_t height);
    void destroy();
    bool created() const;
    bool resources_released() const;
    void set_visible(bool visible);
    void set_periods(uint8_t periods);
    void set_render_gain(float gain);
    void set_frame(const WaveformDisplayFrame &frame);

    uint8_t periods() const;
    float peak_to_peak_volts() const;
    float rms_volts() const;
    float fundamental_hz() const;
    float sample_rate_hz() const;
    float span_us() const;
    float volts_per_division() const;
    bool peak_preservation_verified() const;
    bool has_frame() const;
    bool visible() const;

private:
    const WaveformEnvelope *active_envelope() const;
    void render_frame();
    void draw_vertical_line(int32_t x, int32_t y1, int32_t y2,
                            uint16_t color);
    void draw_line(int32_t x1, int32_t y1, int32_t x2, int32_t y2,
                   uint16_t color);
    void set_pixel(int32_t x, int32_t y, uint16_t color);
    int32_t sample_to_y(float sample) const;

    lv_obj_t *object_ = nullptr;
    lv_obj_t *canvas_ = nullptr;
    uint16_t *canvas_pixels_ = nullptr;
    int32_t canvas_width_ = 0;
    int32_t canvas_height_ = 0;
    uint8_t periods_ = kPeriodsInCapture;
    float render_gain_ = 1.0F;
    WaveformDisplayFrame frame_{};
    bool has_frame_ = false;
    bool visible_ = false;
};

}  // namespace cyclescope
