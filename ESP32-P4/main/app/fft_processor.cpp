#include "fft_processor.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>

#include "esp_dsp.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_memory_utils.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
#include "cyclescope_pipeline_startup_fault_test.hpp"
#endif

#if CONFIG_DSP_MAX_FFT_SIZE < 8192
#error "CycleScope requires CONFIG_DSP_MAX_FFT_SIZE_8192=y (or larger)"
#endif

#if !CONFIG_DSP_OPTIMIZED
#error "CycleScope requires the optimized esp-dsp implementation on ESP32-P4"
#endif

namespace cyclescope {
namespace {

constexpr char kTag[] = "cyclescope_fft";
constexpr size_t kAlignmentBytes = 16;
constexpr float kPi = 3.14159265358979323846F;
constexpr float kTwoPi = 2.0F * kPi;
constexpr float kMinimumPeakVolts = 0.0005F;
constexpr float kRelativePeakThreshold = 0.005F;
constexpr float kHarmonicToleranceBins = 1.5F;
constexpr float kBandEdgeToleranceBins = 0.5F;
constexpr size_t kReconstructionPoints = 4096;

enum class FftBufferRole {
    Work,
    Table,
    Window,
    PositiveSpectrum,
};

bool inject_buffer_failure(FftBufferRole role)
{
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    startup_fault_test::PipelineFailPoint point =
        startup_fault_test::PipelineFailPoint::None;
    switch (role) {
    case FftBufferRole::Work:
        point = startup_fault_test::PipelineFailPoint::FftWorkBuffer;
        break;
    case FftBufferRole::Table:
        point = startup_fault_test::PipelineFailPoint::FftTableBuffer;
        break;
    case FftBufferRole::Window:
        point = startup_fault_test::PipelineFailPoint::HannWindowBuffer;
        break;
    case FftBufferRole::PositiveSpectrum:
        point = startup_fault_test::PipelineFailPoint::PositiveSpectrumBuffer;
        break;
    }
    return startup_fault_test::consume_pipeline_failpoint(point);
#else
    (void)role;
    return false;
#endif
}

void *allocate_cached_buffer(size_t bytes, FftBufferRole role)
{
    if (inject_buffer_failure(role)) {
        return nullptr;
    }
    void *buffer = heap_caps_aligned_alloc(kAlignmentBytes, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == nullptr) {
        buffer = heap_caps_aligned_alloc(kAlignmentBytes, bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    return buffer;
}

const char *memory_region(const void *pointer)
{
    return esp_ptr_external_ram(pointer) ? "PSRAM" : "internal RAM";
}

float clamp_unit_offset(float value)
{
    return std::max(-1.0F, std::min(1.0F, value));
}

bool response_query_frequency(float component_frequency_hz,
                              float edge_tolerance_hz,
                              float *query_frequency_hz)
{
    if (query_frequency_hz == nullptr
        || !std::isfinite(component_frequency_hz)
        || !std::isfinite(edge_tolerance_hz)
        || edge_tolerance_hz < 0.0F
        || component_frequency_hz
               < kResponseMinimumHz - edge_tolerance_hz
        || component_frequency_hz
               > kResponseMaximumHz + edge_tolerance_hz) {
        return false;
    }
    *query_frequency_hz = std::clamp(
        component_frequency_hz,
        kResponseMinimumHz, kResponseMaximumHz);
    return true;
}

}  // namespace

FftProcessor8192::~FftProcessor8192()
{
    deinitialize();
}

void FftProcessor8192::deinitialize()
{
    if (fft_table_initialized_) {
        dsps_fft2r_deinit_fc32();
        fft_table_initialized_ = false;
    }
    release_buffers();
}

esp_err_t FftProcessor8192::initialize()
{
    if (initialized()) {
        return ESP_OK;
    }

    esp_err_t error = allocate_buffers();
    if (error != ESP_OK) {
        return error;
    }

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_pipeline_failpoint(
            startup_fault_test::PipelineFailPoint::FftTableInitialization)) {
        error = ESP_ERR_NO_MEM;
    } else
#endif
    {
        error = dsps_fft2r_init_fc32(
            fft_table_, static_cast<int>(kSampleCount));
    }
    if (error != ESP_OK) {
        ESP_LOGE(kTag, "esp-dsp FFT table initialization failed: %s", esp_err_to_name(error));
        release_buffers();
        return error;
    }
    fft_table_initialized_ = true;

    dsps_wind_hann_f32(window_, static_cast<int>(kSampleCount));
    window_sum_ = 0.0F;
    for (size_t index = 0; index < kSampleCount; ++index) {
        window_sum_ += window_[index];
    }
    if (!(window_sum_ > 0.0F)) {
        ESP_LOGE(kTag, "Hann window coherent gain is invalid");
        dsps_fft2r_deinit_fc32();
        fft_table_initialized_ = false;
        release_buffers();
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(kTag,
             "esp-dsp ready: N=%u, positive bins=%u, Hann gain=%.6f, work=%s, table=%s, window=%s, spectrum=%s",
             static_cast<unsigned>(kSampleCount), static_cast<unsigned>(kPositiveBinCount),
             static_cast<double>(window_sum_ / static_cast<float>(kSampleCount)), memory_region(fft_data_),
             memory_region(fft_table_), memory_region(window_), memory_region(positive_spectrum_));
    ESP_LOGI(kTag, "heap after FFT init: internal free/min=%u/%u, PSRAM free/min=%u/%u bytes",
             static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned>(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)),
             static_cast<unsigned>(heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM)));
    return ESP_OK;
}

esp_err_t FftProcessor8192::allocate_buffers()
{
    // Keep all persistent FFT storage in cached PSRAM. The 1024x600 LVGL
    // renderer needs the scarce internal RAM more than this sequential buffer.
    fft_data_ = static_cast<float *>(allocate_cached_buffer(
        2 * kSampleCount * sizeof(float), FftBufferRole::Work));
    fft_table_ = static_cast<float *>(allocate_cached_buffer(
        kSampleCount * sizeof(float), FftBufferRole::Table));
    window_ = static_cast<float *>(allocate_cached_buffer(
        kSampleCount * sizeof(float), FftBufferRole::Window));
    positive_spectrum_ = static_cast<float *>(allocate_cached_buffer(
        kPositiveBinCount * sizeof(float),
        FftBufferRole::PositiveSpectrum));
    if (fft_data_ == nullptr || fft_table_ == nullptr || window_ == nullptr || positive_spectrum_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate persistent 8192-point FFT buffers");
        release_buffers();
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void FftProcessor8192::release_buffers()
{
    heap_caps_free(positive_spectrum_);
    heap_caps_free(window_);
    heap_caps_free(fft_table_);
    heap_caps_free(fft_data_);
    positive_spectrum_ = nullptr;
    window_ = nullptr;
    fft_table_ = nullptr;
    fft_data_ = nullptr;
    window_sum_ = 0.0F;
}

bool FftProcessor8192::initialized() const
{
    return fft_table_initialized_ && fft_data_ != nullptr && fft_table_ != nullptr && window_ != nullptr
           && positive_spectrum_ != nullptr;
}

bool FftProcessor8192::resources_released() const
{
    return !fft_table_initialized_ && fft_data_ == nullptr
           && fft_table_ == nullptr && window_ == nullptr
           && positive_spectrum_ == nullptr && window_sum_ == 0.0F;
}

const float *FftProcessor8192::positive_spectrum() const
{
    return positive_spectrum_;
}

size_t FftProcessor8192::positive_spectrum_size() const
{
    return initialized() ? kPositiveBinCount : 0;
}

esp_err_t FftProcessor8192::process(const int16_t *samples, size_t sample_count, float sample_rate_hz,
                                    uint32_t scale_uV_per_lsb, int32_t offset_uV,
                                    FftAnalysisResult *result,
                                    const FrequencyResponseProfile *response_profile)
{
    if (!initialized()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (samples == nullptr || result == nullptr || sample_count != kSampleCount || !(sample_rate_hz > 0.0F)
        || scale_uV_per_lsb == 0 || sample_rate_hz * 0.5F < kMaximumMeasurementHz) {
        return ESP_ERR_INVALID_ARG;
    }
    if (response_profile != nullptr
        && (!frequency_response_profile_valid(*response_profile)
            || response_profile->upstream.scale_uv_per_lsb
                   != scale_uV_per_lsb
            || response_profile->upstream.offset_uv != offset_uV
            || response_profile->upstream.sample_rate_hz
                   != static_cast<uint32_t>(sample_rate_hz)
            || response_profile->upstream.frame_sample_count
                   != sample_count)) {
        return ESP_ERR_INVALID_ARG;
    }

    *result = {};
    const int64_t start_time_us = esp_timer_get_time();
    const float volts_per_lsb = static_cast<float>(scale_uV_per_lsb) * 1.0e-6F;
    const float offset_volts = static_cast<float>(offset_uV) * 1.0e-6F;

    float sum = 0.0F;
    for (size_t index = 0; index < kSampleCount; ++index) {
        sum += static_cast<float>(samples[index]) * volts_per_lsb + offset_volts;
    }
    const float dc_offset_volts = sum / static_cast<float>(kSampleCount);

    float sample_minimum = std::numeric_limits<float>::max();
    float sample_maximum = std::numeric_limits<float>::lowest();
    float sum_of_squares = 0.0F;
    for (size_t index = 0; index < kSampleCount; ++index) {
        const float calibrated = static_cast<float>(samples[index]) * volts_per_lsb + offset_volts;
        const float centered = calibrated - dc_offset_volts;
        sample_minimum = std::min(sample_minimum, centered);
        sample_maximum = std::max(sample_maximum, centered);
        sum_of_squares += centered * centered;
        fft_data_[2 * index] = centered * window_[index];
        fft_data_[2 * index + 1] = 0.0F;
    }

    // Force the reference kernel until the ESP32-P4 ARP4 path is proven
    // equivalent for weak, phase-sensitive low-frequency inputs.
    esp_err_t error =
        dsps_fft2r_fc32_ansi(fft_data_, static_cast<int>(kSampleCount));
    if (error != ESP_OK) {
        return error;
    }
    error = dsps_bit_rev_fc32(fft_data_, static_cast<int>(kSampleCount));
    if (error != ESP_OK) {
        return error;
    }

    for (size_t bin = 0; bin < kPositiveBinCount; ++bin) {
        const float real = fft_data_[2 * bin];
        const float imaginary = fft_data_[2 * bin + 1];
        const float single_sided_scale = (bin == 0 || bin == kPositiveBinCount - 1) ? 1.0F : 2.0F;
        positive_spectrum_[bin] = single_sided_scale * std::hypot(real, imaginary) / window_sum_;
    }

    const float bin_width_hz = sample_rate_hz / static_cast<float>(kSampleCount);
    const float band_edge_tolerance_hz =
        kBandEdgeToleranceBins * bin_width_hz;
    std::array<PeakCandidate, kMaximumCandidates> candidates{};
    const size_t candidate_count = collect_peak_candidates(bin_width_hz, &candidates);
    std::array<size_t, kMaximumSpectralLines> selected_indices{};
    std::array<uint16_t, kMaximumSpectralLines> harmonic_orders{};
    std::array<size_t, kMaximumDisplayedSpectralLines> displayed_indices{};
    std::array<uint16_t, kMaximumDisplayedSpectralLines>
        displayed_harmonic_orders{};
    size_t displayed_count = 0U;
    const size_t selected_count =
        select_harmonic_family(
            candidates, candidate_count, bin_width_hz, &selected_indices,
            &harmonic_orders, &displayed_indices,
            &displayed_harmonic_orders, &displayed_count);

    std::array<float, kMaximumSpectralLines> refined_frequencies{};
    float weighted_fundamental_sum = 0.0F;
    float fundamental_weight_sum = 0.0F;
    for (size_t line = 0; line < selected_count; ++line) {
        const PeakCandidate &candidate = candidates[selected_indices[line]];
        refined_frequencies[line] = refine_frequency(samples, volts_per_lsb, offset_volts, dc_offset_volts,
                                                      sample_rate_hz, candidate.frequency_hz, bin_width_hz);
        const float harmonic = static_cast<float>(harmonic_orders[line]);
        const float amplitude_weight = candidate.amplitude_volts_peak * candidate.amplitude_volts_peak;
        const float weight = amplitude_weight * harmonic * harmonic;
        weighted_fundamental_sum += (refined_frequencies[line] / harmonic) * weight;
        fundamental_weight_sum += weight;
    }

    std::array<Projection, kMaximumSpectralLines> components{};
    float reconstructed_rms_square = 0.0F;
    float fundamental_hz = 0.0F;
    float fundamental_phase_radians =
        std::numeric_limits<float>::quiet_NaN();
    if (fundamental_weight_sum > 0.0F) {
        fundamental_hz = weighted_fundamental_sum / fundamental_weight_sum;
        for (size_t line = 0; line < selected_count; ++line) {
            const float component_frequency = fundamental_hz * static_cast<float>(harmonic_orders[line]);
            components[line] = project_at_frequency(samples, volts_per_lsb, offset_volts, dc_offset_volts,
                                                     sample_rate_hz, component_frequency);
            float response_frequency_hz = 0.0F;
            if (response_profile != nullptr
                && response_query_frequency(
                    component_frequency, band_edge_tolerance_hz,
                    &response_frequency_hz)) {
                float correction_factor = 0.0F;
                if (!frequency_response_correction_factor(
                        *response_profile, response_frequency_hz,
                        scale_uV_per_lsb, &correction_factor)) {
                    return ESP_ERR_INVALID_ARG;
                }
                components[line].amplitude_volts_peak *=
                    correction_factor;
            }
            result->spectral_lines[line] = {
                .frequency_hz = component_frequency,
                .amplitude_volts_peak = components[line].amplitude_volts_peak,
                .harmonic_order = harmonic_orders[line],
            };
            result->spectral_line_phases_radians[line] =
                components[line].phase_radians;
            reconstructed_rms_square += components[line].amplitude_volts_peak
                                        * components[line].amplitude_volts_peak * 0.5F;
        }
        if (selected_count > 0U && harmonic_orders[0] == 1U) {
            fundamental_phase_radians = components[0].phase_radians;
        }

        for (size_t line = 0; line < displayed_count; ++line) {
            const uint16_t harmonic_order = displayed_harmonic_orders[line];
            const float component_frequency =
                fundamental_hz * static_cast<float>(harmonic_order);
            float component_amplitude = 0.0F;
            bool copied_formal_line = false;
            for (size_t formal_line = 0; formal_line < selected_count;
                 ++formal_line) {
                if (harmonic_orders[formal_line] == harmonic_order) {
                    component_amplitude =
                        result->spectral_lines[formal_line]
                            .amplitude_volts_peak;
                    copied_formal_line = true;
                    break;
                }
            }
            if (!copied_formal_line) {
                component_amplitude = project_at_frequency(
                    samples, volts_per_lsb, offset_volts, dc_offset_volts,
                    sample_rate_hz, component_frequency)
                                          .amplitude_volts_peak;
                float response_frequency_hz = 0.0F;
                if (response_profile != nullptr
                    && response_query_frequency(
                        component_frequency, band_edge_tolerance_hz,
                        &response_frequency_hz)) {
                    float correction_factor = 0.0F;
                    if (!frequency_response_correction_factor(
                            *response_profile, response_frequency_hz,
                            scale_uV_per_lsb, &correction_factor)) {
                        return ESP_ERR_INVALID_ARG;
                    }
                    component_amplitude *= correction_factor;
                }
            }
            result->displayed_spectral_lines[line] = {
                .frequency_hz = component_frequency,
                .amplitude_volts_peak = component_amplitude,
                .harmonic_order = harmonic_order,
            };
        }
    }

    result->spectral_line_count = static_cast<uint32_t>(selected_count);
    result->displayed_spectral_line_count =
        static_cast<uint32_t>(displayed_count);
    result->p4_response_profile_id =
        response_profile == nullptr ? 0U : response_profile->profile_id;
    result->frequency_response_compensated =
        response_profile != nullptr;
    result->fundamental_hz = fundamental_hz;
    result->fundamental_phase_radians = fundamental_phase_radians;
    result->dc_offset_volts = dc_offset_volts;
    result->sample_rate_hz = sample_rate_hz;
    result->bin_width_hz = bin_width_hz;
    bool components_in_measurement_band = selected_count >= 2U
                                          && harmonic_orders[0] == 1U
                                          && std::isfinite(fundamental_hz)
                                          && fundamental_hz > 0.0F
                                          && std::isfinite(
                                              fundamental_phase_radians);
    for (size_t line = 0; line < selected_count; ++line) {
        const float frequency_hz = result->spectral_lines[line].frequency_hz;
        const float amplitude_volts_peak =
            result->spectral_lines[line].amplitude_volts_peak;
        components_in_measurement_band = components_in_measurement_band
                                         && std::isfinite(frequency_hz)
                                         && std::isfinite(amplitude_volts_peak)
                                         && amplitude_volts_peak > 0.0F
                                         && frequency_hz >= kMinimumMeasurementHz - band_edge_tolerance_hz
                                         && frequency_hz <= kMaximumMeasurementHz + band_edge_tolerance_hz;
    }
    result->valid = components_in_measurement_band;
    if (response_profile != nullptr) {
        for (size_t bin = 0U; bin < kPositiveBinCount; ++bin) {
            const float frequency_hz = static_cast<float>(bin) * bin_width_hz;
            if (frequency_hz < kResponseMinimumHz
                || frequency_hz > kResponseMaximumHz) {
                continue;
            }
            float correction_factor = 0.0F;
            if (!frequency_response_correction_factor(
                    *response_profile, frequency_hz, scale_uV_per_lsb,
                    &correction_factor)) {
                return ESP_ERR_INVALID_ARG;
            }
            positive_spectrum_[bin] *= correction_factor;
        }
    }
    if (selected_count > 0) {
        result->voltage_peak_to_peak = reconstruct_peak_to_peak(components, harmonic_orders, selected_count);
        result->true_rms_volts = std::sqrt(reconstructed_rms_square);
    } else {
        result->voltage_peak_to_peak = sample_maximum - sample_minimum;
        result->true_rms_volts = std::sqrt(sum_of_squares / static_cast<float>(kSampleCount));
    }
    result->analysis_time_us = static_cast<uint32_t>(esp_timer_get_time() - start_time_us);
    return ESP_OK;
}

size_t FftProcessor8192::collect_peak_candidates(
    float bin_width_hz, std::array<PeakCandidate, kMaximumCandidates> *candidates) const
{
    // Include the neighboring bin at each band edge. A 10 kHz tone maps to
    // bin 20.165, so starting at ceil(20.165) would skip its actual peak bin.
    const size_t first_bin = std::max<size_t>(1, static_cast<size_t>(std::floor(kMinimumMeasurementHz / bin_width_hz)));
    const size_t last_bin = std::min<size_t>(kPositiveBinCount - 2,
                                              static_cast<size_t>(std::ceil(kMaximumMeasurementHz / bin_width_hz)));
    float maximum_magnitude = 0.0F;
    for (size_t bin = first_bin; bin <= last_bin; ++bin) {
        maximum_magnitude = std::max(maximum_magnitude, positive_spectrum_[bin]);
    }
    if (!(maximum_magnitude > 0.0F)) {
        return 0;
    }
    const float threshold = std::max(kMinimumPeakVolts, maximum_magnitude * kRelativePeakThreshold);

    size_t candidate_count = 0;
    for (size_t bin = first_bin; bin <= last_bin; ++bin) {
        const float magnitude = positive_spectrum_[bin];
        if (magnitude < threshold || magnitude <= positive_spectrum_[bin - 1]
            || magnitude < positive_spectrum_[bin + 1]) {
            continue;
        }

        const float left = std::log(std::max(positive_spectrum_[bin - 1], 1.0e-20F));
        const float center = std::log(std::max(magnitude, 1.0e-20F));
        const float right = std::log(std::max(positive_spectrum_[bin + 1], 1.0e-20F));
        const float denominator = left - 2.0F * center + right;
        float bin_offset = 0.0F;
        if (std::fabs(denominator) > 1.0e-12F) {
            const float interpolated_offset = 0.5F * (left - right) / denominator;
            bin_offset = std::max(-0.5F, std::min(0.5F, interpolated_offset));
        }
        const PeakCandidate candidate = {
            .bin = bin,
            .frequency_hz = (static_cast<float>(bin) + bin_offset) * bin_width_hz,
            .amplitude_volts_peak = magnitude,
        };
        const float band_edge_tolerance_hz = kBandEdgeToleranceBins * bin_width_hz;
        if (candidate.frequency_hz < kMinimumMeasurementHz - band_edge_tolerance_hz
            || candidate.frequency_hz > kMaximumMeasurementHz + band_edge_tolerance_hz) {
            continue;
        }

        if (candidate_count < candidates->size()) {
            (*candidates)[candidate_count++] = candidate;
            continue;
        }
        size_t weakest = 0;
        for (size_t index = 1; index < candidates->size(); ++index) {
            if ((*candidates)[index].amplitude_volts_peak < (*candidates)[weakest].amplitude_volts_peak) {
                weakest = index;
            }
        }
        if (candidate.amplitude_volts_peak > (*candidates)[weakest].amplitude_volts_peak) {
            (*candidates)[weakest] = candidate;
        }
    }
    return candidate_count;
}

size_t FftProcessor8192::select_harmonic_family(
    const std::array<PeakCandidate, kMaximumCandidates> &candidates, size_t candidate_count,
    float bin_width_hz,
    std::array<size_t, kMaximumSpectralLines> *selected_indices,
    std::array<uint16_t, kMaximumSpectralLines> *harmonic_orders,
    std::array<size_t, kMaximumDisplayedSpectralLines> *displayed_indices,
    std::array<uint16_t, kMaximumDisplayedSpectralLines> *displayed_harmonic_orders,
    size_t *displayed_count) const
{
    if (displayed_count == nullptr) {
        return 0;
    }
    *displayed_count = 0U;
    if (candidate_count == 0) {
        return 0;
    }

    size_t best_base = 0;
    size_t best_match_count = 0;
    float best_energy = 0.0F;
    std::array<int, kMaximumHarmonicOrder + 1> best_matches{};
    best_matches.fill(-1);

    for (size_t base_index = 0; base_index < candidate_count; ++base_index) {
        const float base_frequency = candidates[base_index].frequency_hz;
        const float band_edge_tolerance_hz = kBandEdgeToleranceBins * bin_width_hz;
        if (base_frequency < kMinimumMeasurementHz - band_edge_tolerance_hz
            || base_frequency > kMaximumMeasurementHz + band_edge_tolerance_hz) {
            continue;
        }

        std::array<int, kMaximumHarmonicOrder + 1> matches{};
        matches.fill(-1);
        matches[1] = static_cast<int>(base_index);
        for (size_t candidate_index = 0; candidate_index < candidate_count; ++candidate_index) {
            if (candidate_index == base_index || candidates[candidate_index].frequency_hz <= base_frequency) {
                continue;
            }
            const float ratio = candidates[candidate_index].frequency_hz / base_frequency;
            const long rounded_harmonic = std::lround(ratio);
            if (rounded_harmonic < 2 || rounded_harmonic > kMaximumHarmonicOrder) {
                continue;
            }
            const float expected_frequency = base_frequency * static_cast<float>(rounded_harmonic);
            const float tolerance_hz = kHarmonicToleranceBins
                                       * (candidates[candidate_index].frequency_hz
                                          / static_cast<float>(candidates[candidate_index].bin));
            if (std::fabs(candidates[candidate_index].frequency_hz - expected_frequency) > tolerance_hz) {
                continue;
            }
            int &match = matches[static_cast<size_t>(rounded_harmonic)];
            if (match < 0 || candidates[candidate_index].amplitude_volts_peak
                                 > candidates[static_cast<size_t>(match)].amplitude_volts_peak) {
                match = static_cast<int>(candidate_index);
            }
        }

        size_t match_count = 0;
        float matched_energy = 0.0F;
        for (int match : matches) {
            if (match >= 0) {
                ++match_count;
                const float amplitude = candidates[static_cast<size_t>(match)].amplitude_volts_peak;
                matched_energy += amplitude * amplitude;
            }
        }
        if (match_count > best_match_count || (match_count == best_match_count && matched_energy > best_energy)) {
            best_base = base_index;
            best_match_count = match_count;
            best_energy = matched_energy;
            best_matches = matches;
        }
    }

    const auto fill_strongest = [&](size_t capacity, size_t *indices,
                                    uint16_t *orders) {
        indices[0] = best_base;
        orders[0] = 1U;
        size_t count = 1U;
        for (uint16_t harmonic = 2U;
             harmonic <= kMaximumHarmonicOrder; ++harmonic) {
            const int match = best_matches[harmonic];
            if (match < 0) {
                continue;
            }
            if (count < capacity) {
                indices[count] = static_cast<size_t>(match);
                orders[count] = harmonic;
                ++count;
                continue;
            }
            size_t weakest = 1U;
            for (size_t line = 2U; line < count; ++line) {
                if (candidates[indices[line]].amplitude_volts_peak
                    < candidates[indices[weakest]].amplitude_volts_peak) {
                    weakest = line;
                }
            }
            if (candidates[static_cast<size_t>(match)]
                    .amplitude_volts_peak
                > candidates[indices[weakest]].amplitude_volts_peak) {
                indices[weakest] = static_cast<size_t>(match);
                orders[weakest] = harmonic;
            }
        }
        for (size_t left = 1U; left < count; ++left) {
            for (size_t right = left + 1U; right < count; ++right) {
                if (orders[right] < orders[left]) {
                    std::swap(orders[left], orders[right]);
                    std::swap(indices[left], indices[right]);
                }
            }
        }
        return count;
    };

    const size_t selected_count = fill_strongest(
        selected_indices->size(), selected_indices->data(),
        harmonic_orders->data());
    *displayed_count = fill_strongest(
        displayed_indices->size(), displayed_indices->data(),
        displayed_harmonic_orders->data());
    return selected_count;
}

FftProcessor8192::Projection FftProcessor8192::project_at_frequency(
    const int16_t *samples, float volts_per_lsb, float offset_volts, float dc_offset_volts,
    float sample_rate_hz, float frequency_hz) const
{
    const float angular_step = kTwoPi * frequency_hz / sample_rate_hz;
    const float step_real = std::cos(angular_step);
    const float step_imaginary = -std::sin(angular_step);
    float oscillator_real = 1.0F;
    float oscillator_imaginary = 0.0F;
    float sum_real = 0.0F;
    float sum_imaginary = 0.0F;

    for (size_t index = 0; index < kSampleCount; ++index) {
        const float calibrated = static_cast<float>(samples[index]) * volts_per_lsb + offset_volts;
        const float weighted = (calibrated - dc_offset_volts) * window_[index];
        sum_real += weighted * oscillator_real;
        sum_imaginary += weighted * oscillator_imaginary;

        const float next_real = oscillator_real * step_real - oscillator_imaginary * step_imaginary;
        oscillator_imaginary = oscillator_real * step_imaginary + oscillator_imaginary * step_real;
        oscillator_real = next_real;
        if ((index & 1023U) == 1023U) {
            const float inverse_norm = 1.0F / std::hypot(oscillator_real, oscillator_imaginary);
            oscillator_real *= inverse_norm;
            oscillator_imaginary *= inverse_norm;
        }
    }

    return {
        .amplitude_volts_peak = 2.0F * std::hypot(sum_real, sum_imaginary) / window_sum_,
        .phase_radians = std::atan2(sum_imaginary, sum_real) + 0.5F * kPi,
    };
}

float FftProcessor8192::refine_frequency(const int16_t *samples, float volts_per_lsb, float offset_volts,
                                         float dc_offset_volts, float sample_rate_hz,
                                         float initial_frequency_hz, float bin_width_hz) const
{
    const float step_hz = 0.15F * bin_width_hz;
    const Projection left = project_at_frequency(samples, volts_per_lsb, offset_volts, dc_offset_volts,
                                                 sample_rate_hz, initial_frequency_hz - step_hz);
    const Projection center = project_at_frequency(samples, volts_per_lsb, offset_volts, dc_offset_volts,
                                                   sample_rate_hz, initial_frequency_hz);
    const Projection right = project_at_frequency(samples, volts_per_lsb, offset_volts, dc_offset_volts,
                                                  sample_rate_hz, initial_frequency_hz + step_hz);
    const float left_power = left.amplitude_volts_peak * left.amplitude_volts_peak;
    const float center_power = center.amplitude_volts_peak * center.amplitude_volts_peak;
    const float right_power = right.amplitude_volts_peak * right.amplitude_volts_peak;
    const float denominator = left_power - 2.0F * center_power + right_power;
    if (std::fabs(denominator) < 1.0e-20F) {
        return initial_frequency_hz;
    }
    const float offset_steps = clamp_unit_offset(0.5F * (left_power - right_power) / denominator);
    return initial_frequency_hz + offset_steps * step_hz;
}

float FftProcessor8192::reconstruct_peak_to_peak(
    const std::array<Projection, kMaximumSpectralLines> &components,
    const std::array<uint16_t, kMaximumSpectralLines> &harmonic_orders, size_t component_count)
{
    std::array<float, kMaximumSpectralLines> oscillator_sine{};
    std::array<float, kMaximumSpectralLines> oscillator_cosine{};
    std::array<float, kMaximumSpectralLines> step_sine{};
    std::array<float, kMaximumSpectralLines> step_cosine{};
    for (size_t component = 0; component < component_count; ++component) {
        oscillator_sine[component] = std::sin(components[component].phase_radians);
        oscillator_cosine[component] = std::cos(components[component].phase_radians);
        const float phase_step = kTwoPi * static_cast<float>(harmonic_orders[component])
                                 / static_cast<float>(kReconstructionPoints);
        step_sine[component] = std::sin(phase_step);
        step_cosine[component] = std::cos(phase_step);
    }

    float minimum = std::numeric_limits<float>::max();
    float maximum = std::numeric_limits<float>::lowest();
    for (size_t point = 0; point < kReconstructionPoints; ++point) {
        float value = 0.0F;
        for (size_t component = 0; component < component_count; ++component) {
            value += components[component].amplitude_volts_peak * oscillator_sine[component];
            const float next_sine = oscillator_sine[component] * step_cosine[component]
                                    + oscillator_cosine[component] * step_sine[component];
            oscillator_cosine[component] = oscillator_cosine[component] * step_cosine[component]
                                           - oscillator_sine[component] * step_sine[component];
            oscillator_sine[component] = next_sine;
        }
        if ((point & 1023U) == 1023U) {
            for (size_t component = 0; component < component_count; ++component) {
                const float inverse_norm =
                    1.0F / std::hypot(oscillator_sine[component], oscillator_cosine[component]);
                oscillator_sine[component] *= inverse_norm;
                oscillator_cosine[component] *= inverse_norm;
            }
        }
        minimum = std::min(minimum, value);
        maximum = std::max(maximum, value);
    }
    return maximum - minimum;
}

}  // namespace cyclescope
