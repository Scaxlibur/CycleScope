/*
 * Local-only LVGL screenshot debugging service.
 *
 * This header is deliberately compiled only when the ignored local CMake
 * fragment defines CYCLESCOPE_LVGL_SCREENSHOT_DEBUG.  Normal production
 * images neither link this source nor open its TCP port.
 */
#pragma once

#include <cstddef>
#include <cstdint>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"

namespace cyclescope {

class LvglScreenshotDebug {
public:
    explicit LvglScreenshotDebug(lv_display_t *display);

    // Allocate the persistent PSRAM target and start the local-only TCP task.
    esp_err_t start();

private:
    struct SnapshotInfo {
        uint32_t status = 0;
        uint32_t sequence = 0;
        uint16_t width = 0;
        uint16_t height = 0;
        uint32_t stride = 0;
        uint32_t pixel_bytes = 0;
        uint32_t lvgl_tick_ms = 0;
        uint64_t captured_at_us = 0;
        uint32_t duration_us = 0;
    };

    static void task_entry(void *context);
    static void encode_response_header(uint8_t *destination,
                                       const SnapshotInfo &info);
    void task_main();
    bool allocate_snapshot_buffer();
    int open_listener();
    void handle_client(int client_fd);
    bool take_snapshot(SnapshotInfo *info);

    lv_display_t *display_ = nullptr;
    TaskHandle_t task_handle_ = nullptr;
    int listener_fd_ = -1;
    void *snapshot_storage_ = nullptr;
    size_t snapshot_capacity_ = 0;
    lv_draw_buf_t snapshot_draw_buf_{};
    uint32_t sequence_ = 0;
};

}  // namespace cyclescope
