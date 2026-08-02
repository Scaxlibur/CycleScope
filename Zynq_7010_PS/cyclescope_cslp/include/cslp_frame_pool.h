#ifndef CSLP_FRAME_POOL_H
#define CSLP_FRAME_POOL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif
#define CSLP_FRAME_POOL_SLOTS 2U

typedef enum {
    CSLP_FRAME_FREE = 0,
    CSLP_FRAME_DMA_OWNED,
    CSLP_FRAME_READY,
    CSLP_FRAME_TX_OWNED
} cslp_frame_owner_t;

typedef struct {
    cslp_frame_owner_t owner;
    uint32_t frame_id;
    uint32_t status_word;
    uint64_t timestamp_us;
    uint32_t generation;
} cslp_frame_slot_t;

typedef struct {
    cslp_frame_slot_t slots[CSLP_FRAME_POOL_SLOTS];
    uint32_t next_generation;
    uint32_t dropped_ready;
} cslp_frame_pool_t;

void cslp_frame_pool_init(cslp_frame_pool_t *pool);
int cslp_frame_pool_acquire_dma(cslp_frame_pool_t *pool);
bool cslp_frame_pool_complete_dma(cslp_frame_pool_t *pool,
                                  unsigned int slot,
                                  uint32_t frame_id,
                                  uint32_t status_word,
                                  uint64_t timestamp_us);
int cslp_frame_pool_acquire_latest_tx(cslp_frame_pool_t *pool);
bool cslp_frame_pool_release_tx(cslp_frame_pool_t *pool, unsigned int slot);
bool cslp_frame_pool_cancel_dma(cslp_frame_pool_t *pool, unsigned int slot);
void cslp_frame_pool_drop_ready(cslp_frame_pool_t *pool);

#ifdef __cplusplus
}
#endif

#endif
