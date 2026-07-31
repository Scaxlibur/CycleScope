#include "waveform_view.hpp"

#include <algorithm>
#include <cstddef>
#include <cmath>
#include <cstdlib>
#include <limits>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "waveform_projection.hpp"
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
#include "cyclescope_display_startup_fault_test.hpp"
#endif

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_wave";
constexpr uint32_t kGridDivisionsX = 10;
constexpr uint32_t kGridDivisionsY = 8;
constexpr uint32_t kCanvasBackground = 0x102A3D;
constexpr uint32_t kGridColor = 0x23445B;
constexpr uint32_t kAxisColor = 0x44758B;
constexpr uint32_t kTraceColor = 0x75E6FF;

uint16_t rgb565(uint32_t color)
{
    return lv_color_to_u16(lv_color_hex(color));
}

bool calculate_canvas_bytes(int32_t width, int32_t height,
                            size_t *canvas_bytes)
{
    if (canvas_bytes == nullptr || width <= 0 || height <= 0) {
        return false;
    }
    const size_t width_size = static_cast<size_t>(width);
    const size_t height_size = static_cast<size_t>(height);
    if (height_size > std::numeric_limits<size_t>::max() / width_size) {
        return false;
    }
    const size_t pixel_count = width_size * height_size;
    if (pixel_count
        > std::numeric_limits<size_t>::max() / sizeof(uint16_t)) {
        return false;
    }
    *canvas_bytes = pixel_count * sizeof(uint16_t);
    return true;
}

}  // namespace

bool WaveformView::create(lv_obj_t *parent, int32_t x, int32_t y,
                          int32_t width, int32_t height)
{
    if (!resources_released()) {
        ESP_LOGE(kTag, "Refusing to create waveform canvas over owned resources");
        return false;
    }

    size_t canvas_bytes = 0;
    if (parent == nullptr || !lv_obj_is_valid(parent)
        || !calculate_canvas_bytes(width, height, &canvas_bytes)) {
        ESP_LOGE(kTag, "Invalid waveform canvas parent or dimensions: %ldx%ld",
                 static_cast<long>(width), static_cast<long>(height));
        return false;
    }

    bool inject_allocation_failure = false;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    inject_allocation_failure =
        startup_fault_test::consume_display_failpoint(
            startup_fault_test::DisplayFailPoint::WaveformCanvasBuffer);
#endif
    uint16_t *pixels = inject_allocation_failure
                           ? nullptr
                           : static_cast<uint16_t *>(heap_caps_malloc(
                                 canvas_bytes,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (pixels == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate %u-byte waveform canvas in PSRAM",
                 static_cast<unsigned>(canvas_bytes));
        return false;
    }

    lv_obj_t *object = lv_obj_create(parent);
    if (object == nullptr) {
        heap_caps_free(pixels);
        ESP_LOGE(kTag, "Unable to create waveform view object");
        return false;
    }
    lv_obj_remove_style_all(object);
    lv_obj_set_pos(object, x, y);
    lv_obj_set_size(object, width, height);
    lv_obj_clear_flag(object, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(object, LV_OBJ_FLAG_CLICKABLE);

    lv_obj_t *canvas = lv_canvas_create(object);
    if (canvas == nullptr) {
        lv_obj_delete(object);
        heap_caps_free(pixels);
        ESP_LOGE(kTag, "Unable to create waveform LVGL canvas");
        return false;
    }
    lv_canvas_set_buffer(canvas, pixels, width, height,
                         LV_COLOR_FORMAT_RGB565);
    lv_obj_set_pos(canvas, 0, 0);
    lv_obj_remove_flag(canvas, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);

    object_ = object;
    canvas_ = canvas;
    canvas_pixels_ = pixels;
    canvas_width_ = width;
    canvas_height_ = height;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    startup_fault_test::note_display_lifecycle_event(
        startup_fault_test::DisplayLifecycleEvent::WaveformCreated);
#endif
    render_frame();
    ESP_LOGI(kTag, "RGB565 waveform canvas ready: %ldx%ld, %u bytes in PSRAM",
             static_cast<long>(canvas_width_),
             static_cast<long>(canvas_height_),
             static_cast<unsigned>(canvas_bytes));
    return true;
}

void WaveformView::destroy()
{
    const bool had_resources = !resources_released();
    if (object_ != nullptr) {
        // lv_obj_delete() synchronously runs the child canvas destructor. The
        // external PSRAM buffer must remain alive until deletion returns.
        lv_obj_delete(object_);
    } else if (canvas_ != nullptr) {
        lv_obj_delete(canvas_);
    }
    object_ = nullptr;
    canvas_ = nullptr;

    if (canvas_pixels_ != nullptr) {
        heap_caps_free(canvas_pixels_);
    }
    canvas_pixels_ = nullptr;
    canvas_width_ = 0;
    canvas_height_ = 0;
    periods_ = kPeriodsInCapture;
    render_gain_ = 1.0F;
    frame_ = {};
    has_frame_ = false;
    visible_ = false;
    if (had_resources) {
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
        startup_fault_test::note_display_lifecycle_event(
            startup_fault_test::DisplayLifecycleEvent::WaveformDestroyed);
#endif
        ESP_LOGI(kTag, "RGB565 waveform canvas released");
    }
}

bool WaveformView::created() const
{
    return object_ != nullptr && canvas_ != nullptr
           && canvas_pixels_ != nullptr && canvas_width_ > 0
           && canvas_height_ > 0;
}

bool WaveformView::resources_released() const
{
    return object_ == nullptr && canvas_ == nullptr
           && canvas_pixels_ == nullptr && canvas_width_ == 0
           && canvas_height_ == 0 && !visible_;
}

void WaveformView::set_visible(bool visible)
{
    if (object_ == nullptr || canvas_ == nullptr) {
        return;
    }
    visible_ = visible;
    if (visible) {
        render_frame();
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
    if (visible_) {
        render_frame();
    }
}

void WaveformView::set_render_gain(float gain)
{
    gain = std::max(0.5F, std::min(gain, 1.25F));
    if (std::fabs(gain - render_gain_) < 0.002F) {
        return;
    }
    render_gain_ = gain;
    if (visible_) {
        render_frame();
    }
}

void WaveformView::set_frame(const WaveformDisplayFrame &frame)
{
    frame_ = frame;
    if (frame_.one_period.column_count > kWaveformDisplayColumns) {
        frame_.one_period.column_count =
            static_cast<uint16_t>(kWaveformDisplayColumns);
    }
    if (frame_.three_periods.column_count > kWaveformDisplayColumns) {
        frame_.three_periods.column_count =
            static_cast<uint16_t>(kWaveformDisplayColumns);
    }
    has_frame_ = frame_.generation != 0
                 && frame_.fundamental_hz > 0.0F
                 && frame_.sample_rate_hz > 0.0F
                 && frame_.vertical_range_volts > 0.0F
                 && frame_.one_period.column_count > 1U
                 && frame_.three_periods.column_count > 1U;
    if (visible_) {
        render_frame();
    }
}

uint8_t WaveformView::periods() const
{
    return periods_;
}

float WaveformView::peak_to_peak_volts() const
{
    return has_frame_ ? frame_.voltage_peak_to_peak : 0.0F;
}

float WaveformView::rms_volts() const
{
    return has_frame_ ? frame_.true_rms_volts : 0.0F;
}

float WaveformView::fundamental_hz() const
{
    return has_frame_ ? frame_.fundamental_hz : 0.0F;
}

float WaveformView::sample_rate_hz() const
{
    return has_frame_ ? frame_.sample_rate_hz : 0.0F;
}

float WaveformView::span_us() const
{
    const WaveformEnvelope *envelope = active_envelope();
    return envelope != nullptr ? envelope->span_us : 0.0F;
}

float WaveformView::volts_per_division() const
{
    return has_frame_
               ? 2.0F * frame_.vertical_range_volts
                     / static_cast<float>(kGridDivisionsY)
               : 0.0F;
}

bool WaveformView::peak_preservation_verified() const
{
    const WaveformEnvelope *envelope = active_envelope();
    return envelope != nullptr && envelope->peak_preserved;
}

bool WaveformView::has_frame() const
{
    return has_frame_;
}

bool WaveformView::visible() const
{
    return visible_;
}

const WaveformEnvelope *WaveformView::active_envelope() const
{
    if (!has_frame_) {
        return nullptr;
    }
    return periods_ == 1 ? &frame_.one_period : &frame_.three_periods;
}

void WaveformView::render_frame()
{
    if (canvas_ == nullptr || canvas_pixels_ == nullptr
        || canvas_width_ <= 0 || canvas_height_ <= 0) {
        return;
    }

    std::fill_n(
        canvas_pixels_,
        static_cast<size_t>(canvas_width_)
            * static_cast<size_t>(canvas_height_),
        rgb565(kCanvasBackground));

    const uint16_t grid = rgb565(kGridColor);
    for (uint32_t division = 0; division <= kGridDivisionsX; ++division) {
        const int32_t x =
            static_cast<int32_t>(division) * (canvas_width_ - 1)
            / static_cast<int32_t>(kGridDivisionsX);
        draw_vertical_line(x, 0, canvas_height_ - 1, grid);
    }
    for (uint32_t division = 0; division <= kGridDivisionsY; ++division) {
        const int32_t y =
            static_cast<int32_t>(division) * (canvas_height_ - 1)
            / static_cast<int32_t>(kGridDivisionsY);
        for (int32_t x = 0; x < canvas_width_; ++x) {
            set_pixel(x, y, grid);
        }
    }

    const int32_t zero_y = sample_to_y(0.0F);
    for (int32_t x = 0; x < canvas_width_; ++x) {
        set_pixel(x, zero_y, rgb565(kAxisColor));
        if (zero_y + 1 < canvas_height_) {
            set_pixel(x, zero_y + 1, rgb565(kAxisColor));
        }
    }

    const WaveformEnvelope *envelope = active_envelope();
    if (envelope != nullptr && envelope->column_count > 1U) {
        const uint16_t trace = rgb565(kTraceColor);
        int32_t previous_y = 0;
        bool have_previous = false;
        for (int32_t x = 0; x < canvas_width_; ++x) {
            WaveformEnvelopeColumn value{};
            if (!aggregate_waveform_column(
                    *envelope, static_cast<size_t>(x),
                    static_cast<size_t>(canvas_width_), &value)) {
                continue;
            }
            const int32_t minimum_y =
                sample_to_y(value.maximum_volts * render_gain_);
            const int32_t maximum_y =
                sample_to_y(value.minimum_volts * render_gain_);
            draw_vertical_line(x, minimum_y, maximum_y, trace);
            const int32_t center_y = (minimum_y + maximum_y) / 2;
            if (have_previous) {
                draw_line(x - 1, previous_y, x, center_y, trace);
            }
            previous_y = center_y;
            have_previous = true;
        }
    }
    lv_obj_invalidate(canvas_);
}

void WaveformView::draw_vertical_line(int32_t x, int32_t y1, int32_t y2,
                                      uint16_t color)
{
    if (y1 > y2) {
        std::swap(y1, y2);
    }
    for (int32_t y = y1; y <= y2; ++y) {
        set_pixel(x, y, color);
    }
}

void WaveformView::draw_line(int32_t x1, int32_t y1, int32_t x2,
                             int32_t y2, uint16_t color)
{
    const int32_t dx = std::abs(x2 - x1);
    const int32_t sx = x1 < x2 ? 1 : -1;
    const int32_t dy = -std::abs(y2 - y1);
    const int32_t sy = y1 < y2 ? 1 : -1;
    int32_t error = dx + dy;
    while (true) {
        set_pixel(x1, y1, color);
        if (x1 == x2 && y1 == y2) {
            break;
        }
        const int32_t twice_error = 2 * error;
        if (twice_error >= dy) {
            error += dy;
            x1 += sx;
        }
        if (twice_error <= dx) {
            error += dx;
            y1 += sy;
        }
    }
}

void WaveformView::set_pixel(int32_t x, int32_t y, uint16_t color)
{
    if (x < 0 || x >= canvas_width_ || y < 0 || y >= canvas_height_) {
        return;
    }
    canvas_pixels_[
        static_cast<size_t>(y) * static_cast<size_t>(canvas_width_)
        + static_cast<size_t>(x)] = color;
}

int32_t WaveformView::sample_to_y(float sample) const
{
    const float vertical_range =
        has_frame_ ? frame_.vertical_range_volts : 1.0F;
    const float normalized =
        (vertical_range - sample) / (2.0F * vertical_range);
    const float bounded = std::max(0.0F, std::min(normalized, 1.0F));
    return static_cast<int32_t>(
        std::lround(
            bounded * static_cast<float>(canvas_height_ - 1)));
}

}  // namespace cyclescope
