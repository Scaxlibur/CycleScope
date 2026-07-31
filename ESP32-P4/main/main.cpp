/*
 * CycleScope application entry point.
 *
 * Board and LVGL adapter setup remain in C. The instrument application starts
 * from C++ so UI, signal models, and transport code can evolve independently.
 */
#include "sdkconfig.h"

#include "esp_check.h"
#include "esp_log.h"
#include "esp_lv_adapter.h"
#include "bsp/display.h"

#include "cslp_udp_receiver.hpp"
#if CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER
#include "cslp_frame_diagnostic.hpp"
#endif
#include "instrument_app.hpp"
#include "lvgl_adapter_init.h"
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
#include "cyclescope_display_startup_fault_test.hpp"
#endif
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
#include "cyclescope_pipeline_startup_fault_test.hpp"
#include "cyclescope_receiver_startup_fault_test.hpp"
#endif
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
#include "cyclescope_receiver_runtime_fault_test.hpp"
#endif

namespace {

constexpr char kTag[] = "cyclescope";

}  // namespace

extern "C" void app_main(void)
{
    cyclescope::CslpUdpReceiver &receiver =
        cyclescope::cslp_udp_receiver();

#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
    if (!cyclescope::runtime_fault_test::run_receiver_runtime_fault_matrix(
            receiver)) {
        ESP_LOGE(kTag, "Receiver runtime fault matrix failed");
    }
    return;
#endif

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (!cyclescope::startup_fault_test::run_pipeline_startup_fault_matrix()) {
        ESP_LOGE(kTag,
                 "Pipeline startup fault matrix failed; refusing normal startup");
        return;
    }
    if (!cyclescope::startup_fault_test::run_receiver_startup_fault_matrix(
            receiver)) {
        ESP_LOGE(kTag,
                 "Receiver startup fault matrix failed; refusing normal startup");
        return;
    }
#endif

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

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    if (!cyclescope::startup_fault_test::
            run_display_stack_startup_fault_matrix(&cfg)) {
        ESP_LOGE(kTag,
                 "Display stack lifecycle matrix failed; refusing normal startup");
        return;
    }
#endif

    static cyclescope_display_stack_t display_stack =
        CYCLESCOPE_DISPLAY_STACK_INITIALIZER;
    const esp_err_t display_error =
        cyclescope_display_stack_init(&display_stack, &cfg);
    if (display_error != ESP_OK) {
        ESP_LOGE(kTag, "Display stack did not start: %s",
                 esp_err_to_name(display_error));
        return;
    }
    lv_display_t *const display = display_stack.display;

    ESP_ERROR_CHECK(esp_lv_adapter_lock(-1));
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    if (!cyclescope::startup_fault_test::
            run_display_canvas_startup_fault_matrix(display)) {
        ESP_LOGE(kTag,
                 "Display canvas startup fault matrix failed; refusing normal startup");
        esp_lv_adapter_unlock();
        const esp_err_t destroy_error =
            cyclescope_display_stack_destroy(&display_stack);
        if (destroy_error != ESP_OK) {
            ESP_LOGE(kTag, "Display teardown after canvas failure failed: %s",
                     esp_err_to_name(destroy_error));
        }
        return;
    }
#if CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
    esp_lv_adapter_unlock();
    const esp_err_t destroy_error =
        cyclescope_display_stack_destroy(&display_stack);
    if (destroy_error != ESP_OK) {
        ESP_LOGE(kTag, "Display-only final teardown failed: %s",
                 esp_err_to_name(destroy_error));
        return;
    }
    ESP_LOGI(kTag,
             "Display-only startup fault matrices completed; owned=EMPTY");
    return;
#endif
#endif
    static cyclescope::InstrumentApp app(display);
    const bool ui_started = app.start_ui();
    esp_lv_adapter_unlock();

    if (!ui_started) {
        ESP_LOGE(kTag,
                 "Instrument UI startup failed; analysis/connect are disabled");
        const esp_err_t destroy_error =
            cyclescope_display_stack_destroy(&display_stack);
        if (destroy_error != ESP_OK) {
            ESP_LOGE(kTag, "Display teardown after UI failure failed: %s",
                     esp_err_to_name(destroy_error));
        }
        return;
    }

    // Startup duration is not part of the two-second expert interaction
    // budget. Commit display, UI and FFT preparation before creating the
    // receiver's permanent task, so every earlier failure leaves it Stopped.
    if (!app.prepare_live_data()) {
        ESP_LOGE(kTag,
                 "Instrument analysis preparation failed; receiver remains stopped");
        ESP_ERROR_CHECK(esp_lv_adapter_lock(-1));
        (void)app.connect(nullptr);
        esp_lv_adapter_unlock();
        return;
    }

    const esp_err_t receiver_error = receiver.start();
    if (receiver_error != ESP_OK) {
        ESP_LOGE(kTag, "CSLP UDP receiver did not start: %s",
                 esp_err_to_name(receiver_error));
    }

    ESP_ERROR_CHECK(esp_lv_adapter_lock(-1));
    // Always let connect() classify receiver startup failure so the UI does
    // not remain stuck at "NETWORK STARTING".
    const bool live_started =
        app.connect(receiver_error == ESP_OK ? &receiver : nullptr);
    esp_lv_adapter_unlock();
    if (receiver_error == ESP_OK && !live_started) {
        ESP_LOGE(kTag, "CSLP receiver is ready but the formal UI pipeline failed");
    }

#if CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER
    if (receiver_error == ESP_OK) {
        const esp_err_t diagnostic_error =
            cyclescope::start_cslp_frame_diagnostic();
        if (diagnostic_error != ESP_OK) {
            ESP_LOGE(kTag, "CSLP frame diagnostic did not start: %s",
                     esp_err_to_name(diagnostic_error));
        }
    }
#endif
}
