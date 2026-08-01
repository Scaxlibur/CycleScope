#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "esp_err.h"

#include "spectrum_types.hpp"

namespace cyclescope {

struct FftAnalysisResult {
    // The formal G-problem measurement remains one fundamental plus at most
    // two harmonics. Extra harmonic-family lines are display-only and never
    // alter the formal Vpp/RMS/F0 result.
    std::array<SpectralLine, kMaximumSpectralLines> spectral_lines{};
    std::array<SpectralLine, kMaximumDisplayedSpectralLines>
        displayed_spectral_lines{};
    float voltage_peak_to_peak = 0.0F;
    float true_rms_volts = 0.0F;
    float fundamental_hz = 0.0F;
    float fundamental_phase_radians = 0.0F;
    float dc_offset_volts = 0.0F;
    float sample_rate_hz = 0.0F;
    float bin_width_hz = 0.0F;
    uint32_t analysis_time_us = 0;
    uint32_t spectral_line_count = 0;
    uint32_t displayed_spectral_line_count = 0;
    bool valid = false;
};

// Fixed-size processor for the G-problem profile. Large buffers and the
// esp-dsp twiddle table are allocated exactly once by initialize().
class FftProcessor8192 {
public:
    static constexpr size_t kSampleCount = 8192;
    static constexpr size_t kPositiveBinCount = kSampleCount / 2 + 1;
    static constexpr float kMinimumMeasurementHz = 10000.0F;
    static constexpr float kMaximumMeasurementHz = 500000.0F;

    FftProcessor8192() = default;
    ~FftProcessor8192();

    FftProcessor8192(const FftProcessor8192 &) = delete;
    FftProcessor8192 &operator=(const FftProcessor8192 &) = delete;

    esp_err_t initialize();
    void deinitialize();
    esp_err_t process(const int16_t *samples, size_t sample_count, float sample_rate_hz,
                      uint32_t scale_uV_per_lsb, int32_t offset_uV, FftAnalysisResult *result);

    bool initialized() const;
    bool resources_released() const;
    // The 4097-bin, single-sided voltage spectrum remains owned by this
    // processor and is valid until the next process() call.
    const float *positive_spectrum() const;
    size_t positive_spectrum_size() const;

private:
    static constexpr size_t kMaximumCandidates = 12;
    static constexpr uint16_t kMaximumHarmonicOrder = 50;

    struct PeakCandidate {
        size_t bin = 0;
        float frequency_hz = 0.0F;
        float amplitude_volts_peak = 0.0F;
    };

    struct Projection {
        float amplitude_volts_peak = 0.0F;
        float phase_radians = 0.0F;
    };

    esp_err_t allocate_buffers();
    void release_buffers();
    size_t collect_peak_candidates(float bin_width_hz,
                                   std::array<PeakCandidate, kMaximumCandidates> *candidates) const;
    size_t select_harmonic_family(
        const std::array<PeakCandidate, kMaximumCandidates> &candidates, size_t candidate_count,
        float bin_width_hz,
        std::array<size_t, kMaximumSpectralLines> *selected_indices,
        std::array<uint16_t, kMaximumSpectralLines> *harmonic_orders,
        std::array<size_t, kMaximumDisplayedSpectralLines> *displayed_indices,
        std::array<uint16_t, kMaximumDisplayedSpectralLines> *displayed_harmonic_orders,
        size_t *displayed_count) const;
    Projection project_at_frequency(const int16_t *samples, float volts_per_lsb, float offset_volts,
                                    float dc_offset_volts, float sample_rate_hz, float frequency_hz) const;
    float refine_frequency(const int16_t *samples, float volts_per_lsb, float offset_volts,
                           float dc_offset_volts, float sample_rate_hz, float initial_frequency_hz,
                           float bin_width_hz) const;
    static float reconstruct_peak_to_peak(
        const std::array<Projection, kMaximumSpectralLines> &components,
        const std::array<uint16_t, kMaximumSpectralLines> &harmonic_orders, size_t component_count);

    float *fft_data_ = nullptr;
    float *fft_table_ = nullptr;
    float *window_ = nullptr;
    float *positive_spectrum_ = nullptr;
    float window_sum_ = 0.0F;
    bool fft_table_initialized_ = false;
};

}  // namespace cyclescope
