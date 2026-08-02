#include "cslp_frame_pool.h"

#include <string.h>

void cslp_frame_pool_init(cslp_frame_pool_t *pool)
{
    if (pool == NULL)
        return;
    memset(pool, 0, sizeof(*pool));
    pool->next_generation = 1U;
}
int cslp_frame_pool_acquire_dma(cslp_frame_pool_t *pool)
{
    unsigned int slot;

    if (pool == NULL)
        return -1;
    for (slot = 0; slot < CSLP_FRAME_POOL_SLOTS; ++slot) {
        if (pool->slots[slot].owner == CSLP_FRAME_FREE) {
            pool->slots[slot].owner = CSLP_FRAME_DMA_OWNED;
            return (int)slot;
        }
    }
    return -1;
}

bool cslp_frame_pool_complete_dma(cslp_frame_pool_t *pool,
                                  unsigned int slot,
                                  uint32_t frame_id,
                                  uint32_t status_word,
                                  uint64_t timestamp_us)
{
    cslp_frame_slot_t *frame;

    if (pool == NULL || slot >= CSLP_FRAME_POOL_SLOTS || frame_id == 0U ||
        pool->slots[slot].owner != CSLP_FRAME_DMA_OWNED)
        return false;

    frame = &pool->slots[slot];
    frame->frame_id = frame_id;
    frame->status_word = status_word;
    frame->timestamp_us = timestamp_us;
    frame->generation = pool->next_generation++;
    if (pool->next_generation == 0U)
        pool->next_generation = 1U;
    frame->owner = CSLP_FRAME_READY;
    return true;
}

int cslp_frame_pool_acquire_latest_tx(cslp_frame_pool_t *pool)
{
    int newest = -1;
    unsigned int slot;

    if (pool == NULL)
        return -1;
    for (slot = 0; slot < CSLP_FRAME_POOL_SLOTS; ++slot) {
        if (pool->slots[slot].owner != CSLP_FRAME_READY)
            continue;
        if (newest < 0 ||
            (int32_t)(pool->slots[slot].generation -
                      pool->slots[(unsigned int)newest].generation) > 0)
            newest = (int)slot;
    }
    if (newest < 0)
        return -1;

    for (slot = 0; slot < CSLP_FRAME_POOL_SLOTS; ++slot) {
        if ((int)slot != newest && pool->slots[slot].owner == CSLP_FRAME_READY) {
            pool->slots[slot].owner = CSLP_FRAME_FREE;
            ++pool->dropped_ready;
        }
    }
    pool->slots[(unsigned int)newest].owner = CSLP_FRAME_TX_OWNED;
    return newest;
}

bool cslp_frame_pool_release_tx(cslp_frame_pool_t *pool, unsigned int slot)
{
    if (pool == NULL || slot >= CSLP_FRAME_POOL_SLOTS ||
        pool->slots[slot].owner != CSLP_FRAME_TX_OWNED)
        return false;
    pool->slots[slot].owner = CSLP_FRAME_FREE;
    return true;
}

bool cslp_frame_pool_cancel_dma(cslp_frame_pool_t *pool, unsigned int slot)
{
    if (pool == NULL || slot >= CSLP_FRAME_POOL_SLOTS ||
        pool->slots[slot].owner != CSLP_FRAME_DMA_OWNED)
        return false;
    pool->slots[slot].owner = CSLP_FRAME_FREE;
    return true;
}

void cslp_frame_pool_drop_ready(cslp_frame_pool_t *pool)
{
    unsigned int slot;

    if (pool == NULL)
        return;
    for (slot = 0; slot < CSLP_FRAME_POOL_SLOTS; ++slot) {
        if (pool->slots[slot].owner == CSLP_FRAME_READY) {
            pool->slots[slot].owner = CSLP_FRAME_FREE;
            ++pool->dropped_ready;
        }
    }
}
