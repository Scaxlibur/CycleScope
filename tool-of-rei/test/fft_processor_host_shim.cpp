// Host-only substitutes for the platform services used by FftProcessor8192.
// The radix-2 transform has standard forward-FFT semantics. It deliberately
// does not claim ESP32-P4 kernel parity or timing equivalence.

#include "cyclescope_fft_host_shim.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <utility>

namespace {

constexpr double kTwoPi = 6.28318530717958647692;

bool is_power_of_two(int value)
{
    return value > 0 && (value & (value - 1)) == 0;
}

}  // namespace

const char *esp_err_to_name(esp_err_t error)
{
    switch (error) {
    case ESP_OK:
        return "ESP_OK";
    case ESP_ERR_NO_MEM:
        return "ESP_ERR_NO_MEM";
    case ESP_ERR_INVALID_ARG:
        return "ESP_ERR_INVALID_ARG";
    case ESP_ERR_INVALID_STATE:
        return "ESP_ERR_INVALID_STATE";
    default:
        return "ESP_ERR_UNKNOWN";
    }
}

void *heap_caps_aligned_alloc(std::size_t alignment, std::size_t bytes,
                              uint32_t capabilities)
{
    (void)capabilities;
    if (alignment == 0U || bytes == 0U) {
        return nullptr;
    }
    const std::size_t rounded_bytes =
        (bytes + alignment - 1U) / alignment * alignment;
    return std::aligned_alloc(alignment, rounded_bytes);
}

void heap_caps_free(void *pointer)
{
    std::free(pointer);
}

std::size_t heap_caps_get_free_size(uint32_t capabilities)
{
    (void)capabilities;
    return 0U;
}

std::size_t heap_caps_get_minimum_free_size(uint32_t capabilities)
{
    (void)capabilities;
    return 0U;
}

bool esp_ptr_external_ram(const void *pointer)
{
    (void)pointer;
    return false;
}

int64_t esp_timer_get_time()
{
    using Clock = std::chrono::steady_clock;
    static const Clock::time_point origin = Clock::now();
    return std::chrono::duration_cast<std::chrono::microseconds>(
               Clock::now() - origin)
        .count();
}

esp_err_t dsps_fft2r_init_fc32(float *table, int size)
{
    return table != nullptr && is_power_of_two(size) ? ESP_OK
                                                      : ESP_ERR_INVALID_ARG;
}

void dsps_fft2r_deinit_fc32()
{
}

void dsps_wind_hann_f32(float *window, int size)
{
    if (window == nullptr || size < 2) {
        return;
    }
    for (int index = 0; index < size; ++index) {
        window[index] = static_cast<float>(
            0.5 * (1.0 - std::cos(kTwoPi * static_cast<double>(index)
                                  / static_cast<double>(size - 1))));
    }
}

esp_err_t dsps_fft2r_fc32_ansi(float *data, int size)
{
    if (data == nullptr || !is_power_of_two(size)) {
        return ESP_ERR_INVALID_ARG;
    }

    for (int index = 1, reversed = 0; index < size; ++index) {
        int bit = size >> 1;
        while ((reversed & bit) != 0) {
            reversed ^= bit;
            bit >>= 1;
        }
        reversed ^= bit;
        if (index < reversed) {
            std::swap(data[2 * index], data[2 * reversed]);
            std::swap(data[2 * index + 1], data[2 * reversed + 1]);
        }
    }

    for (int length = 2; length <= size; length <<= 1) {
        const double angle = -kTwoPi / static_cast<double>(length);
        const float step_real = static_cast<float>(std::cos(angle));
        const float step_imaginary = static_cast<float>(std::sin(angle));
        for (int base = 0; base < size; base += length) {
            float twiddle_real = 1.0F;
            float twiddle_imaginary = 0.0F;
            for (int offset = 0; offset < length / 2; ++offset) {
                const int even = base + offset;
                const int odd = even + length / 2;
                const float odd_real =
                    data[2 * odd] * twiddle_real
                    - data[2 * odd + 1] * twiddle_imaginary;
                const float odd_imaginary =
                    data[2 * odd] * twiddle_imaginary
                    + data[2 * odd + 1] * twiddle_real;
                const float even_real = data[2 * even];
                const float even_imaginary = data[2 * even + 1];
                data[2 * even] = even_real + odd_real;
                data[2 * even + 1] = even_imaginary + odd_imaginary;
                data[2 * odd] = even_real - odd_real;
                data[2 * odd + 1] = even_imaginary - odd_imaginary;

                const float next_real =
                    twiddle_real * step_real
                    - twiddle_imaginary * step_imaginary;
                twiddle_imaginary =
                    twiddle_real * step_imaginary
                    + twiddle_imaginary * step_real;
                twiddle_real = next_real;
            }
        }
    }
    return ESP_OK;
}

esp_err_t dsps_bit_rev_fc32(float *data, int size)
{
    // The host transform above already emits natural bin order. The product
    // call remains present, while this adapter intentionally makes it a no-op.
    return data != nullptr && is_power_of_two(size) ? ESP_OK
                                                     : ESP_ERR_INVALID_ARG;
}

void cyclescope_host_log(const char *tag, const char *format, ...)
{
    (void)tag;
    (void)format;
}
