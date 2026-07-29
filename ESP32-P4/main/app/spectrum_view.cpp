#include "spectrum_view.hpp"

#include <math.h>
#include <stdio.h>

#include <algorithm>

#include "esp_heap_caps.h"
#include "esp_log.h"

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_spectrum";
constexpr uint32_t kGridDivisionsY = 5;
constexpr uint32_t kCanvasBackground = 0x102A3D;
constexpr uint32_t kGridColor = 0x23445B;
constexpr uint32_t kAxisColor = 0x44758B;
constexpr uint32_t kFundamentalColor = 0x20D6B5;
constexpr uint32_t kHarmonicColor = 0x75E6FF;
constexpr uint32_t kNoiseFloorColor = 0x2B7891;
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

}  // namespace

void SpectrumView::create(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height,
                          const SpectrumModel *model)
{
    if (model != nullptr) {
        initialize_from_model(*model);
    }

    object_ = lv_obj_create(parent);
    lv_obj_remove_style_all(object_);
    lv_obj_set_pos(object_, x, y);
    lv_obj_set_size(object_, width, height);
    lv_obj_clear_flag(object_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(object_, LV_OBJ_FLAG_CLICKABLE);

    canvas_width_ = width;
    canvas_height_ = height - kAxisLabelHeight;
    const size_t canvas_bytes = static_cast<size_t>(canvas_width_) * static_cast<size_t>(canvas_height_)
                                * sizeof(uint16_t);
    canvas_pixels_ = static_cast<uint16_t *>(
        heap_caps_malloc(canvas_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (canvas_pixels_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate %u-byte spectrum canvas in PSRAM", static_cast<unsigned>(canvas_bytes));
        lv_obj_add_flag(object_, LV_OBJ_FLAG_HIDDEN);
        return;
    }

    canvas_ = lv_canvas_create(object_);
    lv_canvas_set_buffer(canvas_, canvas_pixels_, canvas_width_, canvas_height_, LV_COLOR_FORMAT_RGB565);
    lv_obj_set_pos(canvas_, 0, 0);
    lv_obj_remove_flag(canvas_, LV_OBJ_FLAG_CLICKABLE);

    for (lv_obj_t *&label : axis_labels_) {
        label = lv_label_create(object_);
        lv_obj_set_size(label, kAxisLabelWidth, kAxisLabelHeight);
        lv_obj_set_y(label, canvas_height_);
        lv_obj_set_style_text_font(label, &lv_font_montserrat_12, 0);
        lv_obj_set_style_text_color(label, lv_color_hex(kAxisTextColor), 0);
        lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_add_flag(label, LV_OBJ_FLAG_HIDDEN);
    }

    if (visible_) {
        render_frame();
    }
    lv_obj_add_flag(object_, LV_OBJ_FLAG_HIDDEN);
    ESP_LOGI(kTag, "RGB565 spectrum canvas ready: %ldx%ld, %u bytes in PSRAM",
             static_cast<long>(canvas_width_), static_cast<long>(canvas_height_),
             static_cast<unsigned>(canvas_bytes));
}

void SpectrumView::set_frame(const SpectrumDisplayFrame &frame)
{
    frame_ = frame;
    if (frame_.column_count > kSpectrumDisplayColumns) {
        frame_.column_count = static_cast<uint16_t>(kSpectrumDisplayColumns);
    }
    if (frame_.peak_count > kMaximumSpectralPeaks) {
        frame_.peak_count = static_cast<uint8_t>(kMaximumSpectralPeaks);
    }
    if (visible_) {
        render_frame();
    }
}

void SpectrumView::initialize_from_model(const SpectrumModel &model)
{
    frame_ = {};
    frame_.sample_rate_hz = static_cast<uint32_t>(model.sample_rate_hz());
    frame_.fft_size = static_cast<uint16_t>(SpectrumModel::kSampleCount);
    frame_.column_count = static_cast<uint16_t>(SpectrumModel::kSpectrumBins);
    frame_.peak_count = static_cast<uint8_t>(SpectrumModel::kLineCount);
    frame_.frequency_min_hz = 0.0F;
    frame_.frequency_max_hz = model.sample_rate_hz() / 2.0F;
    frame_.bin_width_hz = model.bin_width_hz();
    frame_.amplitude_max_volts = 0.5F;

    for (size_t bin = 0; bin < SpectrumModel::kSpectrumBins; ++bin) {
        const float magnitude = model.magnitude_at_bin(bin);
        frame_.columns[bin] = {.peak_volts = magnitude, .rms_volts = magnitude};
    }
    const auto &lines = model.lines();
    for (size_t index = 0; index < lines.size(); ++index) {
        frame_.peaks[index] = {
            .bin_index = static_cast<uint16_t>(lroundf(lines[index].frequency_hz / model.bin_width_hz())),
            .frequency_hz = lines[index].frequency_hz,
            .amplitude_volts_peak = lines[index].amplitude_volts_peak,
            .snr_db = 0.0F,
        };
    }
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

    const float frequency_span_hz = frame_.frequency_max_hz - frame_.frequency_min_hz;
    const float tick_step_hz = nice_tick_step(frequency_span_hz);
    const float first_tick_hz = ceilf(frame_.frequency_min_hz / tick_step_hz) * tick_step_hz;
    for (float tick_hz = first_tick_hz; tick_hz <= frame_.frequency_max_hz + tick_step_hz * 0.001F;
         tick_hz += tick_step_hz) {
        const int32_t x = static_cast<int32_t>(
            (tick_hz - frame_.frequency_min_hz) * static_cast<float>(canvas_width_ - 1) / frequency_span_hz);
        draw_vertical_line(x, 0, canvas_height_ - 1, grid);
    }

    const uint16_t spectrum_color = rgb565(kNoiseFloorColor);
    for (int32_t x = 0; x < canvas_width_; ++x) {
        const size_t column = static_cast<size_t>(x) * frame_.column_count / static_cast<size_t>(canvas_width_);
        const SpectrumColumn &value = frame_.columns[column];
        float peak_normalized = value.peak_volts / frame_.amplitude_max_volts;
        float rms_normalized = value.rms_volts / frame_.amplitude_max_volts;
        peak_normalized = std::max(0.0F, std::min(peak_normalized, 1.0F));
        rms_normalized = std::max(0.0F, std::min(rms_normalized, peak_normalized));
        const int32_t peak_y =
            canvas_height_ - 1 - static_cast<int32_t>(peak_normalized * static_cast<float>(canvas_height_ - 1));
        const int32_t rms_y =
            canvas_height_ - 1 - static_cast<int32_t>(rms_normalized * static_cast<float>(canvas_height_ - 1));
        draw_vertical_line(x, rms_y, peak_y, spectrum_color);
    }

    for (size_t index = 0; index < frame_.peak_count; ++index) {
        const SpectralPeak &peak = frame_.peaks[index];
        if (peak.frequency_hz < frame_.frequency_min_hz || peak.frequency_hz > frame_.frequency_max_hz) {
            continue;
        }
        float normalized = peak.amplitude_volts_peak / frame_.amplitude_max_volts;
        normalized = std::max(0.0F, std::min(normalized, 1.0F));
        const int32_t x = static_cast<int32_t>(
            (peak.frequency_hz - frame_.frequency_min_hz) * static_cast<float>(canvas_width_ - 1)
            / frequency_span_hz);
        const int32_t y =
            canvas_height_ - 1 - static_cast<int32_t>(normalized * static_cast<float>(canvas_height_ - 1));
        draw_vertical_line(x, canvas_height_ - 1, y,
                           rgb565(index == 0 ? kFundamentalColor : kHarmonicColor), index == 0 ? 4 : 2);
    }

    const uint16_t axis = rgb565(kAxisColor);
    for (int32_t x = 0; x < canvas_width_; ++x) {
        set_pixel(x, canvas_height_ - 1, axis);
        if (canvas_height_ > 1) {
            set_pixel(x, canvas_height_ - 2, axis);
        }
    }

    update_axis_labels();
    lv_obj_invalidate(canvas_);
}

void SpectrumView::update_axis_labels()
{
    for (lv_obj_t *label : axis_labels_) {
        lv_obj_add_flag(label, LV_OBJ_FLAG_HIDDEN);
    }

    const float frequency_span_hz = frame_.frequency_max_hz - frame_.frequency_min_hz;
    const float tick_step_hz = nice_tick_step(frequency_span_hz);
    const float first_tick_hz = ceilf(frame_.frequency_min_hz / tick_step_hz) * tick_step_hz;
    size_t label_index = 0;
    for (float tick_hz = first_tick_hz;
         tick_hz <= frame_.frequency_max_hz + tick_step_hz * 0.001F && label_index < axis_labels_.size();
         tick_hz += tick_step_hz, ++label_index) {
        lv_obj_t *label = axis_labels_[label_index];
        char text[16];
        format_frequency(text, sizeof(text), tick_hz);
        lv_label_set_text(label, text);
        int32_t x = static_cast<int32_t>(
                        (tick_hz - frame_.frequency_min_hz) * static_cast<float>(canvas_width_ - 1)
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
    if (y1 > y2) {
        const int32_t temporary = y1;
        y1 = y2;
        y2 = temporary;
    }
    const int32_t half_width = line_width / 2;
    for (int32_t draw_x = x - half_width; draw_x <= x + half_width; ++draw_x) {
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
