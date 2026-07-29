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

int cslp_dma_zynq_init(void);
void cslp_dma_zynq_set_enabled(bool enabled);
void cslp_dma_zynq_set_capture(bool enabled);
void cslp_dma_zynq_set_test_pattern(bool enabled);
void cslp_dma_zynq_clear_pl_stats(void);
int cslp_dma_zynq_acquire_frame(cslp_dma_frame_view_t *view);
void cslp_dma_zynq_release_frame(unsigned int slot);
void cslp_dma_zynq_discard_ready(void);
void cslp_dma_zynq_get_stats(cslp_dma_stats_t *stats);

#endif
