/*
 * CycleScope application entry point.
 *
 * Board and LVGL adapter setup remain in C. The instrument application starts
 * from C++ so UI, signal models, and transport code can evolve independently.
 */
#include <assert.h>

#include "esp_check.h"
#include "esp_lv_adapter.h"
#include "bsp/display.h"

#include "instrument_app.hpp"
#include "lvgl_adapter_init.h"

extern "C" void app_main(void)
{
    const bsp_display_cfg_t cfg = {
        .hw_cfg = {
            .hdmi_resolution = BSP_HDMI_RES_NONE,
            .dsi_bus = {
                // Keep the BSP's documented "auto" choice.  On rev < v3
                // silicon the IDF driver selects its legacy-safe default.
                .phy_clk_src = static_cast<mipi_dsi_phy_clock_source_t>(0),
                .lane_bit_rate_mbps = BSP_LCD_MIPI_DSI_LANE_BITRATE_MBPS,
            },
        },
    };

    lv_display_t *display = lvgl_adapter_init(&cfg);
    assert(display != NULL && "Failed to initialize LVGL display adapter");
    bsp_display_backlight_on();

    ESP_ERROR_CHECK(esp_lv_adapter_lock(-1));
    static cyclescope::InstrumentApp app(display);
    app.start();
    esp_lv_adapter_unlock();
}
