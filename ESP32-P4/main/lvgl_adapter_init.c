/*
 * LVGL display and touch adapter for the ESP32-P4 Function EV Board.
 *
 * Based on Espressif's lvgl_demo_v9 at commit
 * 2e9e9dcd066db0d34bbea93a5f7a4c5385ab1e1d.
 */
#include "lvgl_adapter_init.h"

#include <string.h>

#include "esp_err.h"
#include "esp_idf_version.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_touch.h"
#include "esp_log.h"
#include "esp_lv_adapter.h"
#include "driver/ledc.h"
#include "core/lv_global.h"
#include "bsp/esp32_p4_function_ev_board.h"
#include "bsp/display.h"
#include "bsp/touch.h"

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST \
    || CONFIG_CYCLESCOPE_DISPLAY_STARTUP_FAULT_TEST
#include "cyclescope_display_lifecycle_fault_test.h"
#define DISPLAY_TEST_NOTE(event) \
    cyclescope_display_lifecycle_note_event(event)
#define DISPLAY_TEST_FAIL(point) \
    cyclescope_display_lifecycle_consume_failpoint(point)
#else
#define DISPLAY_TEST_NOTE(event) ((void)0)
#define DISPLAY_TEST_FAIL(point) false
#endif

static const char *TAG = "cyclescope_lvgl";

/* Small partial buffers leave PSRAM available for the signal pipeline. */
#define LVGL_ADAPTER_BUFFER_HEIGHT 20
#define LVGL_TASK_CORE 0

static void lvgl_adapter_get_resolution(uint32_t *out_hres, uint32_t *out_vres)
{
    if (out_hres != NULL) {
        *out_hres = BSP_LCD_H_RES;
    }
    if (out_vres != NULL) {
        *out_vres = BSP_LCD_V_RES;
    }
}

static void record_first_error(esp_err_t error, esp_err_t *first_error)
{
    if (error != ESP_OK && *first_error == ESP_OK) {
        *first_error = error;
    }
}

/*
 * LVGL 9.4.0 creates lv_general_mutex in lv_os_init(), but lv_deinit() does
 * not release it.  The next LV_GLOBAL_INIT then clears the only handle and
 * leaks one 92-byte FreeRTOS queue per adapter lifecycle.  Keep this pinned
 * compatibility cleanup in tracked product code until the locked LVGL
 * release gains a symmetric OS deinit path.
 */
static bool lvgl_general_mutex_is_initialized(void)
{
#if LV_USE_OS == LV_OS_FREERTOS
    return LV_GLOBAL_DEFAULT()->lv_general_mutex.xIsInitialized != pdFALSE;
#else
    return false;
#endif
}

static esp_err_t release_lvgl_general_mutex(void)
{
#if LV_USE_OS == LV_OS_FREERTOS
    if (!lvgl_general_mutex_is_initialized()) {
        return ESP_OK;
    }
    if (lv_mutex_delete(&LV_GLOBAL_DEFAULT()->lv_general_mutex)
        != LV_RESULT_OK) {
        ESP_LOGE(TAG, "LVGL general mutex deletion failed");
        return ESP_FAIL;
    }
#endif
    return ESP_OK;
}

bool cyclescope_display_stack_resources_released(
    const cyclescope_display_stack_t *stack)
{
    if (stack == NULL) {
        return false;
    }
    return stack->lcd.mipi_dsi_bus == NULL && stack->lcd.io == NULL
#if CONFIG_BSP_LCD_TYPE_HDMI
           && stack->lcd.io_cec == NULL && stack->lcd.io_avi == NULL
#endif
           && stack->lcd.panel == NULL && stack->lcd.control == NULL
           && stack->touch == NULL && stack->display == NULL
           && stack->touch_indev == NULL && !stack->display_attempted
           && !stack->display_created && !stack->panel_dma2d_enabled
           && !stack->backlight_channel_configured
           && !stack->adapter_initialized
           && !stack->touch_attempted && !stack->touch_created
           && !stack->adapter_started && !stack->backlight_on
           && !stack->poisoned
           && !lvgl_general_mutex_is_initialized();
}

esp_err_t cyclescope_display_stack_destroy(cyclescope_display_stack_t *stack)
{
    if (stack == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (stack->poisoned) {
        ESP_LOGE(TAG, "Display stack is poisoned; reboot required");
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t first_error = ESP_OK;

    if (stack->backlight_on) {
        const esp_err_t error = bsp_display_backlight_off();
        record_first_error(error, &first_error);
        if (error == ESP_OK) {
            stack->backlight_on = false;
            DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_BACKLIGHT_OFF);
        }
    }

    if (stack->adapter_initialized && stack->touch_indev != NULL) {
        const esp_err_t error =
            esp_lv_adapter_unregister_touch(stack->touch_indev);
        record_first_error(error, &first_error);
        if (error == ESP_OK) {
            stack->touch_indev = NULL;
            DISPLAY_TEST_NOTE(
                CYCLESCOPE_DISPLAY_LIFECYCLE_TOUCH_UNREGISTERED);
        }
    }

    if (stack->adapter_initialized && stack->display != NULL
        && stack->touch_indev == NULL) {
        const esp_err_t error =
            esp_lv_adapter_unregister_display(stack->display);
        record_first_error(error, &first_error);
        if (error == ESP_OK) {
            stack->display = NULL;
            DISPLAY_TEST_NOTE(
                CYCLESCOPE_DISPLAY_LIFECYCLE_DISPLAY_UNREGISTERED);
        }
    }

    if (stack->adapter_initialized) {
        const esp_err_t error = esp_lv_adapter_deinit();
        record_first_error(error, &first_error);
        esp_err_t mutex_error = ESP_OK;
        if (!esp_lv_adapter_is_initialized()) {
            stack->adapter_initialized = false;
            stack->adapter_started = false;
            stack->touch_indev = NULL;
            stack->display = NULL;
            mutex_error = release_lvgl_general_mutex();
            record_first_error(mutex_error, &first_error);
            if (error == ESP_OK && mutex_error == ESP_OK) {
                DISPLAY_TEST_NOTE(
                    CYCLESCOPE_DISPLAY_LIFECYCLE_ADAPTER_DEINITIALIZED);
            }
        }
        if (error != ESP_OK || mutex_error != ESP_OK
            || stack->adapter_initialized
            || lvgl_general_mutex_is_initialized()) {
            stack->poisoned = true;
            ESP_LOGE(TAG,
                     "LVGL adapter teardown was incomplete; preserving BSP hardware");
            return first_error != ESP_OK ? first_error : ESP_FAIL;
        }
    }

    if (stack->touch != NULL) {
        const esp_err_t error = esp_lcd_touch_del(stack->touch);
        record_first_error(error, &first_error);
        if (error != ESP_OK) {
            stack->poisoned = true;
            ESP_LOGE(TAG,
                     "Raw touch deletion failed; preserving its IO and display hardware");
            return first_error;
        }
        stack->touch = NULL;
        stack->touch_created = false;
        DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_RAW_TOUCH_DELETED);
    }

    if (stack->touch_attempted) {
        bsp_touch_delete();
        DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_TOUCH_DELETED);
        const esp_err_t error = bsp_i2c_deinit();
        record_first_error(error, &first_error);
        if (error == ESP_OK) {
            stack->touch_attempted = false;
            DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_I2C_DEINITIALIZED);
        }
    }

    if (stack->panel_dma2d_enabled) {
        if (stack->lcd.panel == NULL) {
            stack->poisoned = true;
            ESP_LOGE(TAG,
                     "DMA2D panel ownership has no panel handle; reboot required");
            return ESP_FAIL;
        }
        const esp_err_t error =
            esp_lcd_dpi_panel_disable_dma2d(stack->lcd.panel);
        record_first_error(error, &first_error);
        if (error != ESP_OK) {
            stack->poisoned = true;
            ESP_LOGE(TAG,
                     "Panel DMA2D disable failed; preserving BSP display hardware");
            return first_error;
        }
        stack->panel_dma2d_enabled = false;
        DISPLAY_TEST_NOTE(
            CYCLESCOPE_DISPLAY_LIFECYCLE_PANEL_DMA2D_DISABLED);
    }

    if (stack->backlight_channel_configured) {
        const ledc_channel_config_t channel_config = {
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = CONFIG_BSP_DISPLAY_BRIGHTNESS_LEDC_CH,
            .deconfigure = true,
        };
        const esp_err_t error = ledc_channel_config(&channel_config);
        record_first_error(error, &first_error);
        if (error != ESP_OK) {
            stack->poisoned = true;
            ESP_LOGE(TAG,
                     "Backlight channel deconfigure failed; preserving BSP display hardware");
            return first_error;
        }
        stack->backlight_channel_configured = false;
        DISPLAY_TEST_NOTE(
            CYCLESCOPE_DISPLAY_LIFECYCLE_BACKLIGHT_CHANNEL_DECONFIGURED);
    }

    if (stack->display_attempted) {
        bsp_display_delete();
        memset(&stack->lcd, 0, sizeof(stack->lcd));
        stack->display_attempted = false;
        stack->display_created = false;
        stack->backlight_on = false;
        DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_DISPLAY_DELETED);
    }

    if (first_error != ESP_OK) {
        stack->poisoned = true;
    }
    return first_error;
}

esp_err_t cyclescope_display_stack_init(cyclescope_display_stack_t *stack,
                                        const bsp_display_cfg_t *cfg)
{
    if (stack == NULL || cfg == NULL) {
        ESP_LOGE(TAG, "Display stack/config is NULL");
        return ESP_ERR_INVALID_ARG;
    }
    if (!cyclescope_display_stack_resources_released(stack)
        || esp_lv_adapter_is_initialized()) {
        ESP_LOGE(TAG, "Display stack is already initialized or not reusable");
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = ESP_OK;
    bool poison_after_cleanup = false;
    stack->display_attempted = true;
    err = bsp_display_new_with_handles(&cfg->hw_cfg, &stack->lcd);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "BSP display init failed: %s", esp_err_to_name(err));
        // The current BSP loses some local handles on internal partial
        // failures. Best-effort cleanup is still useful, but retry in the same
        // boot cannot be certified safe.
        (void)cyclescope_display_stack_destroy(stack);
        stack->poisoned = true;
        return err;
    }
    stack->display_created = true;
    stack->backlight_channel_configured = true;
#if CONFIG_BSP_LCD_USE_DMA2D \
    && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
    // BSP 5.2.3 enables panel DMA2D internally but its delete path does not
    // disable it. The IDF panel driver refuses deletion until the owner does.
    stack->panel_dma2d_enabled = true;
#endif
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_DISPLAY_CREATED);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_BSP_DISPLAY)) {
        err = ESP_FAIL;
        goto fail;
    }

    esp_lv_adapter_config_t adapter_cfg = ESP_LV_ADAPTER_DEFAULT_CONFIG();
    adapter_cfg.task_core_id = LVGL_TASK_CORE;
    err = esp_lv_adapter_init(&adapter_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LVGL adapter init failed: %s", esp_err_to_name(err));
        if (!esp_lv_adapter_is_initialized()) {
            const esp_err_t mutex_error = release_lvgl_general_mutex();
            if (mutex_error != ESP_OK) {
                poison_after_cleanup = true;
            }
        }
        goto fail;
    }
    stack->adapter_initialized = true;
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_ADAPTER_INITIALIZED);
    ESP_LOGI(TAG, "LVGL worker pinned to Core %d", LVGL_TASK_CORE);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_ADAPTER_INIT)) {
        err = ESP_FAIL;
        goto fail;
    }

    uint32_t hres = 0;
    uint32_t vres = 0;
    lvgl_adapter_get_resolution(&hres, &vres);

    esp_lv_adapter_display_config_t display_cfg = ESP_LV_ADAPTER_DISPLAY_MIPI_DEFAULT_CONFIG(
        stack->lcd.panel, stack->lcd.io, hres, vres,
        ESP_LV_ADAPTER_ROTATE_0);
    display_cfg.profile.buffer_height = LVGL_ADAPTER_BUFFER_HEIGHT;

    stack->display = esp_lv_adapter_register_display(&display_cfg);
    if (stack->display == NULL) {
        ESP_LOGE(TAG, "Register display failed");
        // esp_lvgl_adapter 0.6.2 has internal bridge failure paths whose
        // shared DMA/PPA ownership is not fully reversible by its public API.
        // Do not certify an in-boot retry after a real registration failure.
        poison_after_cleanup = true;
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_DISPLAY_REGISTERED);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_DISPLAY_REGISTER)) {
        err = ESP_FAIL;
        goto fail;
    }

    stack->touch_attempted = true;
    err = bsp_touch_new(NULL, &stack->touch);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Touch init failed: %s", esp_err_to_name(err));
        goto fail;
    }
    stack->touch_created = true;
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_BSP_TOUCH_CREATED);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_BSP_TOUCH)) {
        err = ESP_FAIL;
        goto fail;
    }

    const esp_lv_adapter_touch_config_t touch_cfg =
        ESP_LV_ADAPTER_TOUCH_DEFAULT_CONFIG(stack->display, stack->touch);
    stack->touch_indev = esp_lv_adapter_register_touch(&touch_cfg);
    if (stack->touch_indev == NULL) {
        ESP_LOGE(TAG, "Register touch failed");
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_TOUCH_REGISTERED);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_TOUCH_REGISTER)) {
        err = ESP_FAIL;
        goto fail;
    }

    err = esp_lv_adapter_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LVGL adapter start failed: %s", esp_err_to_name(err));
        goto fail;
    }
    stack->adapter_started = true;
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_WORKER_STARTED);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_WORKER_START)) {
        err = ESP_FAIL;
        goto fail;
    }

    err = bsp_display_backlight_on();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Display backlight failed: %s", esp_err_to_name(err));
        goto fail;
    }
    stack->backlight_on = true;
    DISPLAY_TEST_NOTE(CYCLESCOPE_DISPLAY_LIFECYCLE_BACKLIGHT_ON);
    if (DISPLAY_TEST_FAIL(CYCLESCOPE_DISPLAY_FAIL_AFTER_BACKLIGHT_ON)) {
        err = ESP_FAIL;
        goto fail;
    }

    return ESP_OK;

fail: {
        const esp_err_t cleanup_error =
            cyclescope_display_stack_destroy(stack);
        if (cleanup_error != ESP_OK) {
            ESP_LOGE(TAG, "Display startup rollback failed: %s",
                     esp_err_to_name(cleanup_error));
        }
        if (poison_after_cleanup) {
            stack->poisoned = true;
        }
        return err;
    }
}
