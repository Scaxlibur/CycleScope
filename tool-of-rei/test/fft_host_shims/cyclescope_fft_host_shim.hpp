#pragma once

#include <cstddef>
#include <cstdint>

using esp_err_t = int;

inline constexpr esp_err_t ESP_OK = 0;
inline constexpr esp_err_t ESP_ERR_NO_MEM = 0x101;
inline constexpr esp_err_t ESP_ERR_INVALID_ARG = 0x102;
inline constexpr esp_err_t ESP_ERR_INVALID_STATE = 0x103;

inline constexpr uint32_t MALLOC_CAP_SPIRAM = 1U << 0U;
inline constexpr uint32_t MALLOC_CAP_INTERNAL = 1U << 1U;
inline constexpr uint32_t MALLOC_CAP_8BIT = 1U << 2U;

const char *esp_err_to_name(esp_err_t error);

void *heap_caps_aligned_alloc(std::size_t alignment, std::size_t bytes,
                              uint32_t capabilities);
void heap_caps_free(void *pointer);
std::size_t heap_caps_get_free_size(uint32_t capabilities);
std::size_t heap_caps_get_minimum_free_size(uint32_t capabilities);
bool esp_ptr_external_ram(const void *pointer);
int64_t esp_timer_get_time();

esp_err_t dsps_fft2r_init_fc32(float *table, int size);
void dsps_fft2r_deinit_fc32();
void dsps_wind_hann_f32(float *window, int size);
esp_err_t dsps_fft2r_fc32_ansi(float *interleaved_complex, int size);
esp_err_t dsps_bit_rev_fc32(float *interleaved_complex, int size);

void cyclescope_host_log(const char *tag, const char *format, ...);

#define ESP_LOGE(tag, format, ...) \
    cyclescope_host_log((tag), (format) __VA_OPT__(,) __VA_ARGS__)
#define ESP_LOGW(tag, format, ...) \
    cyclescope_host_log((tag), (format) __VA_OPT__(,) __VA_ARGS__)
#define ESP_LOGI(tag, format, ...) \
    cyclescope_host_log((tag), (format) __VA_OPT__(,) __VA_ARGS__)

#define CONFIG_DSP_MAX_FFT_SIZE 8192
#define CONFIG_DSP_OPTIMIZED 1
#define CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST 0
