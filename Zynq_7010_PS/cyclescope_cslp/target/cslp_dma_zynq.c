#include "cslp_dma_zynq.h"
#include "cslp_time.h"

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
#define CSLP_TIMESTAMP_GPIO_BASE_ADDRESS 0x41220000U
#define CSLP_ADC_CLOCK_GPIO_BASE_ADDRESS 0x41230000U
#define CSLP_FRAME_BYTES (CSLP_PROFILE_FRAME_SAMPLES * sizeof(int16_t))
#define CSLP_CONTROL_CAPTURE_ENABLE 0x00000001U
#define CSLP_CONTROL_CLEAR_STATS 0x00000002U
#define CSLP_CONTROL_TEST_PATTERN 0x00000004U
#define CSLP_CONTROL_TEST_MODE_SHIFT 3U
#define CSLP_CONTROL_TEST_MODE_MASK 0x00000018U
#define CSLP_CONTROL_INJECT_OTR_TOGGLE 0x00000020U
#define CSLP_CONTROL_INJECT_OVERFLOW_TOGGLE 0x00000040U
#define CSLP_CONTROL_INJECT_FRAME_DROP_TOGGLE 0x00000080U
#define CSLP_CONTROL_TEST_AMPLITUDE_SHIFT 8U
#define CSLP_CONTROL_TEST_AMPLITUDE_MASK 0x000fff00U
#define CSLP_TEST_MAX_AMPLITUDE 2047U
#define CSLP_DMA_IRQ_MASK (XAXIDMA_IRQ_IOC_MASK | XAXIDMA_IRQ_ERROR_MASK)

typedef struct {
    XAxiDma dma;
    XGpio control_gpio;
    XGpio status_gpio;
    XGpio timestamp_gpio;
    XGpio adc_clock_gpio;
    cslp_frame_pool_t pool;
    volatile int active_dma_slot;
    volatile bool enabled;
    uint32_t control_shadow;
    uint64_t timestamp_anchor_adc_tick;
    uint64_t timestamp_anchor_monotonic_us;
    bool timestamp_anchor_valid;
    cslp_dma_stats_t stats;
} cslp_dma_state_t;

static cslp_dma_state_t dma_state;
static int16_t dma_buffers[CSLP_FRAME_POOL_SLOTS][CSLP_PROFILE_FRAME_SAMPLES]
    __attribute__((aligned(64)));

static bool establish_timestamp_anchor(void)
{
    uint64_t best_span = UINT64_MAX;
    uint64_t best_adc_tick = 0U;
    uint64_t best_midpoint_ticks = 0U;
    unsigned int attempt;

    /*
     * The 192-bit PL snapshot updates atomically in FCLK0. adc_tick itself
     * advances between snapshots, so use high/low/high reads and retain the
     * narrowest XTime bracket. Capture is still disabled at this point.
     */
    for (attempt = 0; attempt < 8U; ++attempt) {
        XTime before;
        XTime after;
        uint32_t high_before;
        uint32_t low;
        uint32_t high_after;
        uint64_t span;

        XTime_GetTime(&before);
        high_before = XGpio_DiscreteRead(&dma_state.adc_clock_gpio, 2U);
        low = XGpio_DiscreteRead(&dma_state.adc_clock_gpio, 1U);
        high_after = XGpio_DiscreteRead(&dma_state.adc_clock_gpio, 2U);
        XTime_GetTime(&after);
        if (high_before != high_after)
            continue;
        span = (uint64_t)after - (uint64_t)before;
        if (span < best_span) {
            best_span = span;
            best_adc_tick = ((uint64_t)high_after << 32) | low;
            best_midpoint_ticks =
                (uint64_t)before + span / 2U;
        }
    }
    if (best_span == UINT64_MAX || best_adc_tick == 0U)
        return false;

    dma_state.timestamp_anchor_adc_tick = best_adc_tick;
    dma_state.timestamp_anchor_monotonic_us =
        cslp_ticks_to_us(best_midpoint_ticks,
                         (uint64_t)(COUNTS_PER_SECOND));
    dma_state.timestamp_anchor_valid = true;
    return true;
}

static bool read_coherent_status(uint32_t *frame_id,
                                 uint32_t *status_word,
                                 uint64_t *timestamp_tick)
{
    unsigned int attempt;

    for (attempt = 0; attempt < 4U; ++attempt) {
        uint32_t before = XGpio_DiscreteRead(&dma_state.status_gpio, 2U);
        uint32_t status = XGpio_DiscreteRead(&dma_state.status_gpio, 1U);
        uint32_t timestamp_low =
            XGpio_DiscreteRead(&dma_state.timestamp_gpio, 1U);
        uint32_t timestamp_high =
            XGpio_DiscreteRead(&dma_state.timestamp_gpio, 2U);
        uint32_t after = XGpio_DiscreteRead(&dma_state.status_gpio, 2U);
        if (before == after && after != 0U) {
            *frame_id = after;
            *status_word = status;
            *timestamp_tick =
                ((uint64_t)timestamp_high << 32) | timestamp_low;
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
        uint64_t timestamp_tick;
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
        if (!dma_state.timestamp_anchor_valid ||
            !read_coherent_status(&frame_id, &status_word, &timestamp_tick) ||
            !cslp_adc_tick_to_monotonic_us(
                timestamp_tick, dma_state.timestamp_anchor_adc_tick,
                dma_state.timestamp_anchor_monotonic_us,
                &first_sample_us) ||
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
    uint32_t dma_irq_id;
    unsigned int s2mm_irq_index;
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
    status = XGpio_Initialize(&dma_state.timestamp_gpio,
                              CSLP_TIMESTAMP_GPIO_BASE_ADDRESS);
    if (status != XST_SUCCESS)
        return status;
    status = XGpio_Initialize(&dma_state.adc_clock_gpio,
                              CSLP_ADC_CLOCK_GPIO_BASE_ADDRESS);
    if (status != XST_SUCCESS)
        return status;
    XGpio_SetDataDirection(&dma_state.control_gpio, 1U, 0x00000000U);
    XGpio_SetDataDirection(&dma_state.control_gpio, 2U, 0x00000000U);
    XGpio_SetDataDirection(&dma_state.status_gpio, 1U, 0xffffffffU);
    XGpio_SetDataDirection(&dma_state.status_gpio, 2U, 0xffffffffU);
    XGpio_SetDataDirection(&dma_state.timestamp_gpio, 1U, 0xffffffffU);
    XGpio_SetDataDirection(&dma_state.timestamp_gpio, 2U, 0xffffffffU);
    XGpio_SetDataDirection(&dma_state.adc_clock_gpio, 1U, 0xffffffffU);
    XGpio_SetDataDirection(&dma_state.adc_clock_gpio, 2U, 0xffffffffU);
    XGpio_DiscreteWrite(&dma_state.control_gpio, 1U, 0U);
    XGpio_DiscreteWrite(&dma_state.control_gpio, 2U, 0U);
    if (!establish_timestamp_anchor())
        return XST_FAILURE;

    dma_config = XAxiDma_LookupConfig(CSLP_DMA_BASE_ADDRESS);
    if (dma_config == NULL)
        return XST_FAILURE;
    status = XAxiDma_CfgInitialize(&dma_state.dma, dma_config);
    if (status != XST_SUCCESS || XAxiDma_HasSg(&dma_state.dma) ||
        !dma_config->HasS2Mm)
        return XST_FAILURE;

    /*
     * The 2025.1 SDT generator packs only present DMA interrupts into
     * IntrId[]. With MM2S disabled, the sole S2MM IRQ is therefore slot 0;
     * slot 1 is 0xffff and would trip the GIC interrupt-range assertion.
     */
    s2mm_irq_index = dma_config->HasMm2S ? 1U : 0U;
    dma_irq_id = dma_config->IntrId[s2mm_irq_index];
    if (dma_irq_id == UINT16_MAX)
        return XST_FAILURE;

    XAxiDma_IntrDisable(&dma_state.dma, XAXIDMA_IRQ_ALL_MASK,
                        XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrAckIrq(&dma_state.dma, XAXIDMA_IRQ_ALL_MASK,
                       XAXIDMA_DEVICE_TO_DMA);
    status = XSetupInterruptSystem(
        &dma_state.dma, dma_interrupt_handler, dma_irq_id,
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

bool cslp_dma_zynq_configure_test_source(bool enabled,
                                         cslp_test_mode_t mode,
                                         uint16_t amplitude,
                                         uint32_t phase_increment)
{
    uint32_t profile;

    if ((unsigned int)mode > (unsigned int)CSLP_TEST_MODE_MULTITONE ||
        amplitude > CSLP_TEST_MAX_AMPLITUDE ||
        (mode == CSLP_TEST_MODE_SINE && phase_increment == 0U))
        return false;

    /* Static multi-bit profile changes are made before capture is enabled. */
    profile = ((uint32_t)mode << CSLP_CONTROL_TEST_MODE_SHIFT) |
              ((uint32_t)amplitude << CSLP_CONTROL_TEST_AMPLITUDE_SHIFT);
    dma_state.control_shadow &=
        ~(CSLP_CONTROL_TEST_MODE_MASK | CSLP_CONTROL_TEST_AMPLITUDE_MASK |
          CSLP_CONTROL_TEST_PATTERN);
    dma_state.control_shadow |= profile;
    XGpio_DiscreteWrite(&dma_state.control_gpio, 2U, phase_increment);
    if (enabled)
        dma_state.control_shadow |= CSLP_CONTROL_TEST_PATTERN;
    write_control_shadow();
    return true;
}

bool cslp_dma_zynq_inject_test_faults(uint32_t fault_mask)
{
    uint32_t toggles = 0U;

    if ((fault_mask & ~CSLP_TEST_FAULT_ALL) != 0U)
        return false;
    if ((fault_mask & CSLP_TEST_FAULT_OTR) != 0U)
        toggles |= CSLP_CONTROL_INJECT_OTR_TOGGLE;
    if ((fault_mask & CSLP_TEST_FAULT_OVERFLOW) != 0U)
        toggles |= CSLP_CONTROL_INJECT_OVERFLOW_TOGGLE;
    if ((fault_mask & CSLP_TEST_FAULT_FRAME_DROP) != 0U)
        toggles |= CSLP_CONTROL_INJECT_FRAME_DROP_TOGGLE;
    dma_state.control_shadow ^= toggles;
    write_control_shadow();
    return true;
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
