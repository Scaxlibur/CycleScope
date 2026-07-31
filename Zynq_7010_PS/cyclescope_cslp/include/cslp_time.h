#ifndef CSLP_TIME_H
#define CSLP_TIME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define CSLP_MICROSECONDS_PER_SECOND 1000000ULL
#define CSLP_ADC_TICKS_PER_SECOND 65000000ULL

/* counts_per_second must be nonzero. */
static inline uint64_t cslp_ticks_to_us(uint64_t ticks,
                                        uint64_t counts_per_second)
{
    return (ticks / counts_per_second) * CSLP_MICROSECONDS_PER_SECOND +
           ((ticks % counts_per_second) * CSLP_MICROSECONDS_PER_SECOND) /
               counts_per_second;
}

/* Round a 65 MHz ADC-clock interval to the nearest protocol microsecond. */
static inline uint64_t cslp_adc_elapsed_ticks_to_us(uint64_t ticks)
{
    const uint64_t whole_seconds = ticks / CSLP_ADC_TICKS_PER_SECOND;
    const uint64_t remainder = ticks % CSLP_ADC_TICKS_PER_SECOND;

    return whole_seconds * CSLP_MICROSECONDS_PER_SECOND +
           (remainder * CSLP_MICROSECONDS_PER_SECOND +
            CSLP_ADC_TICKS_PER_SECOND / 2ULL) /
               CSLP_ADC_TICKS_PER_SECOND;
}

/*
 * Map a frame's equivalent ADC tick onto the PS boot-monotonic epoch. Natural
 * u64 ADC-counter wrap is accepted; a delta of at least 2^63 is ambiguous and
 * rejected. The caller must establish the anchor before enabling capture.
 */
static inline bool cslp_adc_tick_to_monotonic_us(
    uint64_t sample_tick,
    uint64_t anchor_adc_tick,
    uint64_t anchor_monotonic_us,
    uint64_t *timestamp_us)
{
    const uint64_t elapsed_ticks = sample_tick - anchor_adc_tick;
    uint64_t elapsed_us;

    if (timestamp_us == NULL || elapsed_ticks > INT64_MAX)
        return false;
    elapsed_us = cslp_adc_elapsed_ticks_to_us(elapsed_ticks);
    if (UINT64_MAX - anchor_monotonic_us < elapsed_us)
        return false;
    *timestamp_us = anchor_monotonic_us + elapsed_us;
    return true;
}

#endif
