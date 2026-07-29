#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "lvgl.h"

namespace cyclescope {

// A fixed synthetic capture used until the FPGA receiver is connected.  The
// source remains oversampled relative to the display, so each screen column is
// rendered from its sample-domain minimum and maximum rather than one point.
class WaveformView {
public:
    static constexpr uint32_t kPeriodsInCapture = 3;
    static constexpr size_t kSamplesPerPeriod = 2048;
    static constexpr size_t kSampleCount = kPeriodsInCapture * kSamplesPerPeriod;

    WaveformView();

    void create(lv_obj_t *parent, int32_t x, int32_t y, int32_t width, int32_t height);
    void set_visible(bool visible);
    void set_periods(uint8_t periods);

    uint8_t periods() const;
    float peak_to_peak_volts() const;
    float rms_volts() const;
    float fundamental_hz() const;
    float sample_rate_hz() const;
    bool peak_preservation_verified() const;

private:
    struct EnvelopeColumn {
        float minimum;
        float maximum;
    };

    static void on_draw(lv_event_t *event);

    void generate_synthetic_capture();
    void rebuild_envelope();
    void draw(lv_event_t *event) const;
    int32_t sample_to_y(float sample, const lv_area_t &coords) const;

    static constexpr size_t kMaxDisplayColumns = 720;
    static constexpr float kFundamentalHz = 100000.0F;
    static constexpr float kSampleRateHz = kFundamentalHz * static_cast<float>(kSamplesPerPeriod);
    static constexpr float kVerticalRangeVolts = 0.8F;

    std::array<float, kSampleCount> samples_{};
    std::array<EnvelopeColumn, kMaxDisplayColumns> envelope_{};
    lv_obj_t *object_ = nullptr;
    int32_t viewport_width_ = 0;
    size_t envelope_columns_ = 0;
    uint8_t periods_ = kPeriodsInCapture;
    float peak_to_peak_volts_ = 0.0F;
    float rms_volts_ = 0.0F;
    bool peak_preservation_verified_ = false;
};

}  // namespace cyclescope
