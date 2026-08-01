#include "spectrum_view.hpp"

#include <math.h>
#include <stdio.h>

#include <algorithm>
#include <cstddef>
#include <limits>

#include "esp_heap_caps.h"
#include "esp_log.h"

#include "spectrum_projection.hpp"
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
#include "cyclescope_display_startup_fault_test.hpp"
#endif

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_spectrum";
constexpr uint32_t kGridDivisionsY = 5;
constexpr uint32_t kCanvasBackground = 0x102A3D;
constexpr uint32_t kGridColor = 0x23445B;
constexpr uint32_t kAxisColor = 0x44758B;
constexpr uint32_t kFundamentalColor = 0x20D6B5;
constexpr uint32_t kHarmonicColor = 0x75E6FF;
constexpr uint32_t kAxisTextColor = 0x89A6B9;
constexpr int32_t kAxisLabelHeight = 18;
constexpr int32_t kAxisLabelWidth = 68;

float nice_tick_step(float range_hz)
{
    if (range_hz <= 0.0F) {
        return 1.0F;
    }

    const float rough_step = range_hz / 4.0F;
    const float exponent = floorf(log10f(rough_step));
    const float decade = powf(10.0F, exponent);
    const float fraction = rough_step / decade;
    const float nice_fraction =
        fraction < 1.5F ? 1.0F : (fraction < 3.0F ? 2.0F : (fraction < 7.0F ? 5.0F : 10.0F));
    return nice_fraction * decade;
}

void format_frequency(char *buffer, size_t buffer_size, float frequency_hz)
{
    if (frequency_hz >= 1000000.0F) {
        snprintf(buffer, buffer_size, "%.2fM", static_cast<double>(frequency_hz / 1000000.0F));
    } else if (frequency_hz >= 1000.0F) {
        snprintf(buffer, buffer_size, "%.0fk", static_cast<double>(frequency_hz / 1000.0F));
    } else {
        snprintf(buffer, buffer_size, "%.0f", static_cast<double>(frequency_hz));
    }
}

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

bool SpectrumView::create(lv_obj_t *parent, int32_t x, int32_t y,
                          int32_t width, int32_t height,
                          const SpectrumModel *model)
{
    if (!resources_released()) {
        ESP_LOGE(kTag, "Refusing to create spectrum canvas over owned resources");
        return false;
    }

    if (height <= kAxisLabelHeight) {
        ESP_LOGE(kTag, "Invalid spectrum view height: %ld",
                 static_cast<long>(height));
        return false;
    }
    const int32_t canvas_height = height - kAxisLabelHeight;
    size_t canvas_bytes = 0;
    if (parent == nullptr || !lv_obj_is_valid(parent)
        || !calculate_canvas_bytes(width, canvas_height, &canvas_bytes)) {
        ESP_LOGE(kTag, "Invalid spectrum canvas parent or dimensions: %ldx%ld",
                 static_cast<long>(width), static_cast<long>(height));
        return false;
    }

    bool inject_allocation_failure = false;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    inject_allocation_failure =
        startup_fault_test::consume_display_failpoint(
            startup_fault_test::DisplayFailPoint::SpectrumCanvasBuffer);
#endif
    uint16_t *pixels = inject_allocation_failure
                           ? nullptr
                           : static_cast<uint16_t *>(heap_caps_malloc(
                                 canvas_bytes,
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (pixels == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate %u-byte spectrum canvas in PSRAM", static_cast<unsigned>(canvas_bytes));
        return false;
    }

    lv_obj_t *object = lv_obj_create(parent);
    if (object == nullptr) {
        heap_caps_free(pixels);
        ESP_LOGE(kTag, "Unable to create spectrum view object");
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
        ESP_LOGE(kTag, "Unable to create spectrum LVGL canvas");
        return false;
    }
    lv_canvas_set_buffer(canvas, pixels, width, canvas_height,
                         LV_COLOR_FORMAT_RGB565);
    lv_obj_set_pos(canvas, 0, 0);
    lv_obj_remove_flag(canvas, LV_OBJ_FLAG_CLICKABLE);

    std::array<lv_obj_t *, 8> labels{};
    for (lv_obj_t *&label : labels) {
        label = lv_label_create(object);
        if (label == nullptr) {
            lv_obj_delete(object);
            heap_caps_free(pixels);
            ESP_LOGE(kTag, "Unable to create spectrum axis label");
            return false;
        }
        lv_obj_set_size(label, kAxisLabelWidth, kAxisLabelHeight);
        lv_obj_set_y(label, canvas_height);
        lv_obj_set_style_text_font(label, &lv_font_montserrat_12, 0);
        lv_obj_set_style_text_color(label, lv_color_hex(kAxisTextColor), 0);
        lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_add_flag(label, LV_OBJ_FLAG_HIDDEN);
    }

    lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
    object_ = object;
    canvas_ = canvas;
    canvas_pixels_ = pixels;
    canvas_width_ = width;
    canvas_height_ = canvas_height;
    axis_labels_ = labels;
    if (model != nullptr) {
        initialize_from_model(*model);
    }
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    startup_fault_test::note_display_lifecycle_event(
        startup_fault_test::DisplayLifecycleEvent::SpectrumCreated);
#endif
    ESP_LOGI(kTag, "RGB565 spectrum canvas ready: %ldx%ld, %u bytes in PSRAM",
             static_cast<long>(canvas_width_), static_cast<long>(canvas_height_),
             static_cast<unsigned>(canvas_bytes));
    return true;
}

void SpectrumView::destroy()
{
    const bool had_resources = !resources_released();
    if (object_ != nullptr) {
        // The canvas does not own its external pixel buffer. Keep PSRAM alive
        // until synchronous LVGL object deletion has completed.
        lv_obj_delete(object_);
    } else if (canvas_ != nullptr) {
        lv_obj_delete(canvas_);
    }
    object_ = nullptr;
    canvas_ = nullptr;
    axis_labels_.fill(nullptr);

    if (canvas_pixels_ != nullptr) {
        heap_caps_free(canvas_pixels_);
    }
    canvas_pixels_ = nullptr;
    canvas_width_ = 0;
    canvas_height_ = 0;
    visible_ = false;
    frame_ = {};
    frequency_window_ = {
        .minimum_hz = 0.0F,
        .maximum_hz = kSpectrumDisplayMaximumHz,
    };
    requested_peak_count_ = static_cast<uint8_t>(kMaximumSpectralLines);
    visible_peak_count_ = 0U;
    if (had_resources) {
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
        startup_fault_test::note_display_lifecycle_event(
            startup_fault_test::DisplayLifecycleEvent::SpectrumDestroyed);
#endif
        ESP_LOGI(kTag, "RGB565 spectrum canvas released");
    }
}

bool SpectrumView::created() const
{
    return object_ != nullptr && canvas_ != nullptr
           && canvas_pixels_ != nullptr && canvas_width_ > 0
           && canvas_height_ > 0
           && std::all_of(axis_labels_.begin(), axis_labels_.end(),
                          [](const lv_obj_t *label) {
                              return label != nullptr;
                          });
}

bool SpectrumView::resources_released() const
{
    return object_ == nullptr && canvas_ == nullptr
           && canvas_pixels_ == nullptr && canvas_width_ == 0
           && canvas_height_ == 0 && !visible_
           && std::all_of(axis_labels_.begin(), axis_labels_.end(),
                          [](const lv_obj_t *label) {
                              return label == nullptr;
                          });
}

bool SpectrumView::visible() const
{
    return visible_;
}

void SpectrumView::set_frame(const SpectrumDisplayFrame &frame)
{
    frame_ = frame;
    if (frame_.column_count > kSpectrumDisplayColumns) {
        frame_.column_count = static_cast<uint16_t>(kSpectrumDisplayColumns);
    }
    if (frame_.peak_count > kMaximumDisplayedSpectralLines) {
        frame_.peak_count =
            static_cast<uint8_t>(kMaximumDisplayedSpectralLines);
    }
    update_frequency_window();
    if (visible_) {
        render_frame();
    }
}

bool SpectrumView::set_visible_peak_count(uint8_t count)
{
    if (count == 0U || count > kMaximumDisplayedSpectralLines) {
        return false;
    }
    requested_peak_count_ = count;
    update_frequency_window();
    if (visible_) {
        render_frame();
    }
    return true;
}

uint8_t SpectrumView::visible_peak_count() const
{
    return visible_peak_count_;
}

uint8_t SpectrumView::available_peak_count() const
{
    return static_cast<uint8_t>(
        std::min<size_t>(frame_.peak_count, kMaximumDisplayedSpectralLines));
}

float SpectrumView::visible_frequency_minimum_hz() const
{
    return frequency_window_.minimum_hz;
}

float SpectrumView::visible_frequency_maximum_hz() const
{
    return frequency_window_.maximum_hz;
}

void SpectrumView::initialize_from_model(const SpectrumModel &model)
{
    frame_ = {};
    frame_.sample_rate_hz = static_cast<uint32_t>(model.sample_rate_hz());
    frame_.fft_size = static_cast<uint16_t>(SpectrumModel::kSampleCount);
    frame_.column_count = static_cast<uint16_t>(kSpectrumDisplayColumns);
    frame_.peak_count = 0;
    frame_.frequency_min_hz = 0.0F;
    frame_.frequency_max_hz = kSpectrumDisplayMaximumHz;
    frame_.bin_width_hz = model.bin_width_hz();
    frame_.amplitude_max_volts =
        kSpectrumDisplayMinimumAmplitudeVolts;
    update_frequency_window();
}

void SpectrumView::update_frequency_window()
{
    const uint8_t available = available_peak_count();
    visible_peak_count_ = std::min(requested_peak_count_, available);
    if (visible_peak_count_ > 0U
        && choose_spectrum_frequency_window(
            frame_, visible_peak_count_, &frequency_window_)) {
        return;
    }
    visible_peak_count_ = 0U;
    frequency_window_ = {
        .minimum_hz = isfinite(frame_.frequency_min_hz)
                              ? frame_.frequency_min_hz
                              : 0.0F,
        .maximum_hz =
            isfinite(frame_.frequency_max_hz)
                    && frame_.frequency_max_hz > frame_.frequency_min_hz
                ? frame_.frequency_max_hz
                : kSpectrumDisplayMaximumHz,
    };
}

void SpectrumView::render_frame()
{
    if (canvas_ == nullptr || canvas_pixels_ == nullptr || frame_.column_count < 2U
        || frame_.frequency_max_hz <= frame_.frequency_min_hz || frame_.amplitude_max_volts <= 0.0F) {
        return;
    }

    const uint16_t background = rgb565(kCanvasBackground);
    std::fill_n(canvas_pixels_, static_cast<size_t>(canvas_width_) * static_cast<size_t>(canvas_height_), background);

    const uint16_t grid = rgb565(kGridColor);
    for (uint32_t division = 0; division <= kGridDivisionsY; ++division) {
        const int32_t y = static_cast<int32_t>(division) * (canvas_height_ - 1) / kGridDivisionsY;
        for (int32_t x = 0; x < canvas_width_; ++x) {
            set_pixel(x, y, grid);
        }
    }

    const float frequency_span_hz =
        frequency_window_.maximum_hz - frequency_window_.minimum_hz;
    const float tick_step_hz = nice_tick_step(frequency_span_hz);
    const float first_tick_hz =
        ceilf(frequency_window_.minimum_hz / tick_step_hz) * tick_step_hz;
    for (float tick_hz = first_tick_hz;
         tick_hz <= frequency_window_.maximum_hz + tick_step_hz * 0.001F;
         tick_hz += tick_step_hz) {
        const int32_t x = static_cast<int32_t>(
            (tick_hz - frequency_window_.minimum_hz)
            * static_cast<float>(canvas_width_ - 1) / frequency_span_hz);
        draw_vertical_line(x, 0, canvas_height_ - 1, grid);
    }

    const uint16_t axis = rgb565(kAxisColor);
    for (int32_t x = 0; x < canvas_width_; ++x) {
        set_pixel(x, canvas_height_ - 1, axis);
        if (canvas_height_ > 1) {
            set_pixel(x, canvas_height_ - 2, axis);
        }
    }

    // G-problem inputs contain one fundamental and one or two harmonics. Draw
    // only those validated semantic lines: rendering all Hann-window bins
    // would turn leakage skirts into apparent extra components. Lines are
    // drawn after the axis so an exactly 5 mVpk component keeps its base.
    for (size_t index = 0; index < visible_peak_count_; ++index) {
        const SpectralPeak &peak = frame_.peaks[index];
        SpectrumCanvasPoint point{};
        if (!map_spectral_peak_to_canvas(
                frame_, frequency_window_, peak,
                static_cast<size_t>(canvas_width_),
                static_cast<size_t>(canvas_height_), &point)) {
            continue;
        }
        draw_vertical_line(
            point.x, canvas_height_ - 1, point.y,
            rgb565(index == 0 ? kFundamentalColor : kHarmonicColor),
            index == 0 ? kSpectrumFundamentalLineWidthPixels
                       : kSpectrumHarmonicLineWidthPixels);
    }

    update_axis_labels();
    lv_obj_invalidate(canvas_);
}

void SpectrumView::update_axis_labels()
{
    for (lv_obj_t *label : axis_labels_) {
        lv_obj_add_flag(label, LV_OBJ_FLAG_HIDDEN);
    }

    const float frequency_span_hz =
        frequency_window_.maximum_hz - frequency_window_.minimum_hz;
    const float tick_step_hz = nice_tick_step(frequency_span_hz);
    const float first_tick_hz =
        ceilf(frequency_window_.minimum_hz / tick_step_hz) * tick_step_hz;
    size_t label_index = 0;
    for (float tick_hz = first_tick_hz;
         tick_hz <= frequency_window_.maximum_hz + tick_step_hz * 0.001F
         && label_index < axis_labels_.size();
         tick_hz += tick_step_hz, ++label_index) {
        lv_obj_t *label = axis_labels_[label_index];
        char text[16];
        format_frequency(text, sizeof(text), tick_hz);
        lv_label_set_text(label, text);
        int32_t x = static_cast<int32_t>(
                        (tick_hz - frequency_window_.minimum_hz)
                        * static_cast<float>(canvas_width_ - 1)
                        / frequency_span_hz)
                    - kAxisLabelWidth / 2;
        if (x < 0) {
            x = 0;
        }
        const int32_t maximum_x = canvas_width_ - kAxisLabelWidth;
        if (x > maximum_x) {
            x = maximum_x;
        }
        lv_obj_set_x(label, x);
        lv_obj_remove_flag(label, LV_OBJ_FLAG_HIDDEN);
    }
}

void SpectrumView::draw_vertical_line(int32_t x, int32_t y1, int32_t y2, uint16_t color, int32_t line_width)
{
    if (line_width <= 0) {
        return;
    }
    if (y1 > y2) {
        const int32_t temporary = y1;
        y1 = y2;
        y2 = temporary;
    }
    const int32_t left_width = (line_width - 1) / 2;
    const int32_t right_width = line_width - 1 - left_width;
    for (int32_t draw_x = x - left_width;
         draw_x <= x + right_width; ++draw_x) {
        for (int32_t y = y1; y <= y2; ++y) {
            set_pixel(draw_x, y, color);
        }
    }
}

void SpectrumView::set_pixel(int32_t x, int32_t y, uint16_t color)
{
    if (x < 0 || x >= canvas_width_ || y < 0 || y >= canvas_height_) {
        return;
    }
    canvas_pixels_[static_cast<size_t>(y) * static_cast<size_t>(canvas_width_) + static_cast<size_t>(x)] = color;
}

void SpectrumView::set_visible(bool visible)
{
    if (object_ == nullptr || canvas_ == nullptr) {
        return;
    }

    if (visible) {
        visible_ = true;
        render_frame();
        lv_obj_remove_flag(object_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_invalidate(object_);
    } else {
        visible_ = false;
        lv_obj_add_flag(object_, LV_OBJ_FLAG_HIDDEN);
    }
}

}  // namespace cyclescope
