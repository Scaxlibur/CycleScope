/*
 * ESP32-P4 display and touch bring-up.
 *
 * The display path follows Espressif's ESP32-P4 Function EV Board LVGL v9
 * example. It remains a hardware smoke test until LVGL-M2 passes; the
 * instrument UI begins at LVGL-M3.
 */
#include <assert.h>

#include "esp_check.h"
#include "esp_lv_adapter.h"
#include "lv_demos.h"
#include "lvgl.h"
#include "bsp/display.h"

#include "lvgl_adapter_init.h"

void app_main(void)
{
    const bsp_display_cfg_t cfg = {
        .hw_cfg = {
            .hdmi_resolution = BSP_HDMI_RES_NONE,
            .dsi_bus = {
                .lane_bit_rate_mbps = BSP_LCD_MIPI_DSI_LANE_BITRATE_MBPS,
            },
        },
    };

    lv_display_t *display = lvgl_adapter_init(&cfg);
    assert(display != NULL && "Failed to initialize LVGL display adapter");
    bsp_display_backlight_on();

    ESP_ERROR_CHECK(esp_lv_adapter_lock(-1));
    lv_demo_widgets();
    esp_lv_adapter_unlock();
}
