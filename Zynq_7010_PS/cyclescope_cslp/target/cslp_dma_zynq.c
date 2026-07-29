#include "cslp_dma_zynq.h"

#include "xaxidma.h"
#include "xgpio.h"
#include "xil_cache.h"
#include "xil_exception.h"
#include "xinterrupt_wrap.h"
#include "xstatus.h"
#include "xiltimer.h"

#include <string.h>

#define CSLP_DMA_BASE_ADDRESS 0x40400000U
#define CSLP_CONTROL_GPIO_BASE_ADDRESS 0x41200000U
#define CSLP_STATUS_GPIO_BASE_ADDRESS 0x41210000U
#define CSLP_FRAME_BYTES (CSLP_PROFILE_FRAME_SAMPLES * sizeof(int16_t))
#define CSLP_CONTROL_CAPTURE_ENABLE 0x00000001U
#define CSLP_CONTROL_CLEAR_STATS 0x00000002U
#define CSLP_CONTROL_TEST_PATTERN 0x00000004U
#define CSLP_DMA_IRQ_MASK (XAXIDMA_IRQ_IOC_MASK | XAXIDMA_IRQ_ERROR_MASK)

/*
 * Completion follows the last captured sample by roughly one 8192-point
 * window plus the two-cycle BRAM AXIS reader and FIR group delay. Until PL
 * exports a hardware timestamp this documented constant is the best bounded
 * estimate; M6 must calibrate it against ILA/packet capture.
 */
#define CSLP_FIRST_SAMPLE_TO_DMA_COMPLETION_US 2279ULL

typedef struct {
    XAxiDma dma;
    XGpio control_gpio;
    XGpio status_gpio;
    cslp_frame_pool_t pool;
    volatile int active_dma_slot;
    volatile bool enabled;
    uint32_t control_shadow;
    cslp_dma_stats_t stats;
} cslp_dma_state_t;

static cslp_dma_state_t dma_state;
static int16_t dma_buffers[CSLP_FRAME_POOL_SLOTS][CSLP_PROFILE_FRAME_SAMPLES]
    __attribute__((aligned(64)));

static uint64_t monotonic_us(void)
{
    XTime ticks;
    XTime_GetTime(&ticks);
    return ((uint64_t)ticks * 1000000ULL) / (uint64_t)COUNTS_PER_SECOND;
}

static bool read_coherent_status(uint32_t *frame_id, uint32_t *status_word)
{
    unsigned int attempt;

    for (attempt = 0; attempt < 4U; ++attempt) {
        uint32_t before = XGpio_DiscreteRead(&dma_state.status_gpio, 2U);
        uint32_t status = XGpio_DiscreteRead(&dma_state.status_gpio, 1U);
        uint32_t after = XGpio_DiscreteRead(&dma_state.status_gpio, 2U);
        if (before == after && after != 0U) {
            *frame_id = after;
            *status_word = status;
            return true;
        }
    }
    return false;
}

static void arm_next_buffer(void)
{
    int slot;
    int status;

    if (!dma_state.enabled || dma_state.active_dma_slot >= 0)
        return;

    slot = cslp_frame_pool_acquire_dma(&dma_state.pool);
    if (slot < 0) {
        ++dma_state.stats.no_free_buffer;
        return;
    }

    /* Device owns the range after this clean+invalidate sequence. */
    Xil_DCacheFlushRange((INTPTR)dma_buffers[(unsigned int)slot],
                         CSLP_FRAME_BYTES);
    Xil_DCacheInvalidateRange((INTPTR)dma_buffers[(unsigned int)slot],
                              CSLP_FRAME_BYTES);
    status = XAxiDma_SimpleTransfer(
        &dma_state.dma, (UINTPTR)dma_buffers[(unsigned int)slot],
        CSLP_FRAME_BYTES, XAXIDMA_DEVICE_TO_DMA);
    if (status != XST_SUCCESS) {
        (void)cslp_frame_pool_cancel_dma(&dma_state.pool,
                                         (unsigned int)slot);
        ++dma_state.stats.submit_failures;
        return;
    }
    dma_state.active_dma_slot = slot;
}

static void reset_after_error(void)
{
    unsigned int timeout = 1000000U;

    XAxiDma_Reset(&dma_state.dma);
    while (timeout-- != 0U && !XAxiDma_ResetIsDone(&dma_state.dma)) {
    }
    if (dma_state.active_dma_slot >= 0) {
        (void)cslp_frame_pool_cancel_dma(
            &dma_state.pool, (unsigned int)dma_state.active_dma_slot);
        dma_state.active_dma_slot = -1;
    }
    XAxiDma_IntrEnable(&dma_state.dma, CSLP_DMA_IRQ_MASK,
                       XAXIDMA_DEVICE_TO_DMA);
    arm_next_buffer();
}

static void dma_interrupt_handler(void *callback)
{
    uint32_t irq_status;
    int slot;

    (void)callback;
    irq_status = XAxiDma_IntrGetIrq(&dma_state.dma,
                                    XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrAckIrq(&dma_state.dma, irq_status,
                       XAXIDMA_DEVICE_TO_DMA);
    if ((irq_status & CSLP_DMA_IRQ_MASK) == 0U)
        return;
    if ((irq_status & XAXIDMA_IRQ_ERROR_MASK) != 0U) {
        ++dma_state.stats.errors;
        reset_after_error();
        return;
    }
    if ((irq_status & XAXIDMA_IRQ_IOC_MASK) == 0U)
        return;

    slot = dma_state.active_dma_slot;
    dma_state.active_dma_slot = -1;
    if (slot >= 0) {
        uint32_t frame_id;
        uint32_t status_word;
        uint64_t completion_us;
        uint64_t first_sample_us;

        /* CPU owns immutable data only after completion invalidation. */
        Xil_DCacheInvalidateRange((INTPTR)dma_buffers[(unsigned int)slot],
                                  CSLP_FRAME_BYTES);
        if (!dma_state.enabled) {
            (void)cslp_frame_pool_cancel_dma(&dma_state.pool,
                                             (unsigned int)slot);
            arm_next_buffer();
            return;
        }
        completion_us = monotonic_us();
        first_sample_us = completion_us > CSLP_FIRST_SAMPLE_TO_DMA_COMPLETION_US
                              ? completion_us -
                                    CSLP_FIRST_SAMPLE_TO_DMA_COMPLETION_US
                              : 0U;
        if (!read_coherent_status(&frame_id, &status_word) ||
            !cslp_frame_pool_complete_dma(
                &dma_state.pool, (unsigned int)slot, frame_id, status_word,
                first_sample_us)) {
            (void)cslp_frame_pool_cancel_dma(&dma_state.pool,
                                             (unsigned int)slot);
            ++dma_state.stats.metadata_failures;
        } else {
            ++dma_state.stats.completions;
            dma_state.stats.last_status_word = status_word;
        }
    }
    arm_next_buffer();
}

int cslp_dma_zynq_init(void)
{
    XAxiDma_Config *dma_config;
    int status;

    memset(&dma_state, 0, sizeof(dma_state));
    dma_state.active_dma_slot = -1;
    cslp_frame_pool_init(&dma_state.pool);

    status = XGpio_Initialize(&dma_state.control_gpio,
                              CSLP_CONTROL_GPIO_BASE_ADDRESS);
    if (status != XST_SUCCESS)
        return status;
    status = XGpio_Initialize(&dma_state.status_gpio,
                              CSLP_STATUS_GPIO_BASE_ADDRESS);
    if (status != XST_SUCCESS)
        return status;
    XGpio_SetDataDirection(&dma_state.control_gpio, 1U, 0x00000000U);
    XGpio_SetDataDirection(&dma_state.status_gpio, 1U, 0xffffffffU);
    XGpio_SetDataDirection(&dma_state.status_gpio, 2U, 0xffffffffU);
    XGpio_DiscreteWrite(&dma_state.control_gpio, 1U, 0U);

    dma_config = XAxiDma_LookupConfig(CSLP_DMA_BASE_ADDRESS);
    if (dma_config == NULL)
        return XST_FAILURE;
    status = XAxiDma_CfgInitialize(&dma_state.dma, dma_config);
    if (status != XST_SUCCESS || XAxiDma_HasSg(&dma_state.dma))
        return XST_FAILURE;

    XAxiDma_IntrDisable(&dma_state.dma, XAXIDMA_IRQ_ALL_MASK,
                        XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrAckIrq(&dma_state.dma, XAXIDMA_IRQ_ALL_MASK,
                       XAXIDMA_DEVICE_TO_DMA);
    status = XSetupInterruptSystem(
        &dma_state.dma, dma_interrupt_handler, dma_config->IntrId[1],
        dma_config->IntrParent, 0x30U);
    if (status != XST_SUCCESS)
        return status;
    XAxiDma_IntrEnable(&dma_state.dma, CSLP_DMA_IRQ_MASK,
                       XAXIDMA_DEVICE_TO_DMA);
    return XST_SUCCESS;
}

void cslp_dma_zynq_set_enabled(bool enabled)
{
    Xil_ExceptionDisableMask(XIL_EXCEPTION_IRQ);
    dma_state.enabled = enabled;
    if (!enabled)
        cslp_frame_pool_drop_ready(&dma_state.pool);
    else
        arm_next_buffer();
    dma_state.stats.dropped_ready = dma_state.pool.dropped_ready;
    Xil_ExceptionEnableMask(XIL_EXCEPTION_IRQ);
}

static void write_control_shadow(void)
{
    XGpio_DiscreteWrite(&dma_state.control_gpio, 1U,
                        dma_state.control_shadow);
}

void cslp_dma_zynq_set_capture(bool enabled)
{
    if (enabled)
        dma_state.control_shadow |= CSLP_CONTROL_CAPTURE_ENABLE;
    else
        dma_state.control_shadow &= ~CSLP_CONTROL_CAPTURE_ENABLE;
    write_control_shadow();
}

void cslp_dma_zynq_set_test_pattern(bool enabled)
{
    if (enabled)
        dma_state.control_shadow |= CSLP_CONTROL_TEST_PATTERN;
    else
        dma_state.control_shadow &= ~CSLP_CONTROL_TEST_PATTERN;
    write_control_shadow();
}

void cslp_dma_zynq_clear_pl_stats(void)
{
    dma_state.control_shadow |= CSLP_CONTROL_CLEAR_STATS;
    write_control_shadow();
    dma_state.control_shadow &= ~CSLP_CONTROL_CLEAR_STATS;
    write_control_shadow();
}

int cslp_dma_zynq_acquire_frame(cslp_dma_frame_view_t *view)
{
    int slot;

    if (view == NULL)
        return XST_INVALID_PARAM;
    Xil_ExceptionDisableMask(XIL_EXCEPTION_IRQ);
    slot = cslp_frame_pool_acquire_latest_tx(&dma_state.pool);
    if (slot >= 0) {
        const cslp_frame_slot_t *frame =
            &dma_state.pool.slots[(unsigned int)slot];
        view->slot = (unsigned int)slot;
        view->samples = dma_buffers[(unsigned int)slot];
        view->frame_id = frame->frame_id;
        view->status_word = frame->status_word;
        view->timestamp_us = frame->timestamp_us;
    }
    dma_state.stats.dropped_ready = dma_state.pool.dropped_ready;
    Xil_ExceptionEnableMask(XIL_EXCEPTION_IRQ);
    return slot >= 0 ? XST_SUCCESS : XST_FAILURE;
}

void cslp_dma_zynq_release_frame(unsigned int slot)
{
    Xil_ExceptionDisableMask(XIL_EXCEPTION_IRQ);
    (void)cslp_frame_pool_release_tx(&dma_state.pool, slot);
    arm_next_buffer();
    Xil_ExceptionEnableMask(XIL_EXCEPTION_IRQ);
}

void cslp_dma_zynq_discard_ready(void)
{
    Xil_ExceptionDisableMask(XIL_EXCEPTION_IRQ);
    cslp_frame_pool_drop_ready(&dma_state.pool);
    dma_state.stats.dropped_ready = dma_state.pool.dropped_ready;
    Xil_ExceptionEnableMask(XIL_EXCEPTION_IRQ);
}

void cslp_dma_zynq_get_stats(cslp_dma_stats_t *stats)
{
    if (stats == NULL)
        return;
    Xil_ExceptionDisableMask(XIL_EXCEPTION_IRQ);
    dma_state.stats.dropped_ready = dma_state.pool.dropped_ready;
    *stats = dma_state.stats;
    Xil_ExceptionEnableMask(XIL_EXCEPTION_IRQ);
}
