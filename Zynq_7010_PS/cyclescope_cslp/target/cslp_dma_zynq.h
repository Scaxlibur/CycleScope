#ifndef CSLP_DMA_ZYNQ_H
#define CSLP_DMA_ZYNQ_H

#include "cslp_frame_pool.h"
#include "cslp_protocol.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    unsigned int slot;
    const int16_t *samples;
    uint32_t frame_id;
    uint32_t status_word;
    uint64_t timestamp_us;
} cslp_dma_frame_view_t;

typedef struct {
    uint32_t completions;
    uint32_t errors;
    uint32_t submit_failures;
    uint32_t metadata_failures;
    uint32_t no_free_buffer;
    uint32_t dropped_ready;
    uint32_t last_status_word;
} cslp_dma_stats_t;

typedef enum {
    CSLP_TEST_MODE_RAMP = 0,
    CSLP_TEST_MODE_SINE = 1,
    CSLP_TEST_MODE_MULTITONE = 2
} cslp_test_mode_t;

#define CSLP_TEST_FAULT_OTR 0x01U
#define CSLP_TEST_FAULT_OVERFLOW 0x02U
#define CSLP_TEST_FAULT_FRAME_DROP 0x04U
#define CSLP_TEST_FAULT_ALL 0x07U

int cslp_dma_zynq_init(void);
void cslp_dma_zynq_set_enabled(bool enabled);
void cslp_dma_zynq_set_capture(bool enabled);
void cslp_dma_zynq_set_test_pattern(bool enabled);
bool cslp_dma_zynq_configure_test_source(bool enabled,
                                         cslp_test_mode_t mode,
                                         uint16_t amplitude,
                                         uint32_t phase_increment);
bool cslp_dma_zynq_inject_test_faults(uint32_t fault_mask);
void cslp_dma_zynq_clear_pl_stats(void);
int cslp_dma_zynq_acquire_frame(cslp_dma_frame_view_t *view);
void cslp_dma_zynq_release_frame(unsigned int slot);
void cslp_dma_zynq_discard_ready(void);
void cslp_dma_zynq_get_stats(cslp_dma_stats_t *stats);

#endif
