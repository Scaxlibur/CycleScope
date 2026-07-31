/*
 * LVGL display and touch adapter for the ESP32-P4 Function EV Board.
 */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#include "bsp/config.h"
#include "bsp/display.h"
#include "bsp/touch.h"
#include "esp_err.h"
#include "esp_lv_adapter_display.h"
#include "lvgl.h"

#if (BSP_CONFIG_NO_GRAPHIC_LIB == 1)
typedef struct {
    bsp_display_config_t hw_cfg;
} bsp_display_cfg_t;
#endif

/**
 * Resources owned by the CycleScope display stack.
 *
 * The BSP and LVGL adapter are process-wide singletons, so exactly one of
 * these objects may be initialized at a time. Keep the object alive for at
 * least as long as the display is in use.
 */
typedef struct {
    bsp_lcd_handles_t lcd;
    esp_lcd_touch_handle_t touch;
    lv_display_t *display;
    lv_indev_t *touch_indev;

    bool display_attempted;
    bool display_created;
    bool panel_dma2d_enabled;
    bool backlight_channel_configured;
    bool adapter_initialized;
    bool touch_attempted;
    bool touch_created;
    bool adapter_started;
    bool backlight_on;
    bool poisoned;
} cyclescope_display_stack_t;

#ifdef __cplusplus
#define CYCLESCOPE_DISPLAY_STACK_INITIALIZER {}
#else
#define CYCLESCOPE_DISPLAY_STACK_INITIALIZER {0}
#endif

/** Initialize the complete BSP display, LVGL adapter and touch stack. */
esp_err_t cyclescope_display_stack_init(cyclescope_display_stack_t *stack,
                                        const bsp_display_cfg_t *cfg);

/**
 * Release an initialized or partially initialized stack in dependency order.
 * The operation is idempotent unless a lower layer reports an unsafe partial
 * teardown, in which case the stack is marked poisoned and requires reboot.
 */
esp_err_t cyclescope_display_stack_destroy(cyclescope_display_stack_t *stack);

/** True only when the stack owns no resource and is safe to initialize. */
bool cyclescope_display_stack_resources_released(
    const cyclescope_display_stack_t *stack);

#ifdef __cplusplus
}
#endif
