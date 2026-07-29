/*
 * LVGL display and touch adapter for the ESP32-P4 Function EV Board.
 */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include "bsp/config.h"
#include "bsp/display.h"
#include "esp_lv_adapter_display.h"
#include "lvgl.h"

#if (BSP_CONFIG_NO_GRAPHIC_LIB == 1)
typedef struct {
    bsp_display_config_t hw_cfg;
} bsp_display_cfg_t;
#endif

lv_display_t *lvgl_adapter_init(const bsp_display_cfg_t *cfg);

#ifdef __cplusplus
}
#endif
