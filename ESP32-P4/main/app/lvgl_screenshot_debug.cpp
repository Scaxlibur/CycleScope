/*
 * Local-only LVGL screenshot debugging service.
 *
 * Wire protocol v1:
 *   client request: ASCII "SHOT\\n"
 *   server reply:   48-byte big-endian header followed by RGB565 little-endian
 *                   pixels on success.  The companion Python fixture is the
 *                   protocol reference and writes PNG/raw/JSON evidence.
 */
#include "lvgl_screenshot_debug.hpp"

#include <cerrno>
#include <cstring>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_lv_adapter.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

namespace cyclescope {
namespace {

constexpr char kTag[] = "lvgl_shot";
constexpr char kAllowedClientIp[] = "192.168.10.4";
constexpr uint16_t kListenPort = 50002;
constexpr uint32_t kTaskStackBytes = 6144;
constexpr UBaseType_t kTaskPriority = 3;
constexpr BaseType_t kTaskCore = 1;
constexpr size_t kSnapshotBufferAlignment = 64;
constexpr size_t kWireHeaderBytes = 48;
constexpr char kRequest[] = "SHOT\n";
constexpr size_t kRequestBytes = sizeof(kRequest) - 1;

enum class ResponseStatus : uint32_t {
    Ok = 0,
    BadRequest = 1,
    LvglLockFailed = 2,
    SnapshotFailed = 3,
    InvalidSnapshot = 4,
};

constexpr uint32_t kWireColorFormatRgb565Le = 1;

void write_be16(uint8_t *destination, uint16_t value)
{
    destination[0] = static_cast<uint8_t>(value >> 8U);
    destination[1] = static_cast<uint8_t>(value);
}

void write_be32(uint8_t *destination, uint32_t value)
{
    destination[0] = static_cast<uint8_t>(value >> 24U);
    destination[1] = static_cast<uint8_t>(value >> 16U);
    destination[2] = static_cast<uint8_t>(value >> 8U);
    destination[3] = static_cast<uint8_t>(value);
}

void write_be64(uint8_t *destination, uint64_t value)
{
    write_be32(destination, static_cast<uint32_t>(value >> 32U));
    write_be32(destination + 4, static_cast<uint32_t>(value));
}

bool receive_all(int socket_fd, void *buffer, size_t length)
{
    auto *bytes = static_cast<uint8_t *>(buffer);
    size_t received_total = 0;
    while (received_total < length) {
        const ssize_t received = recv(socket_fd, bytes + received_total,
                                      length - received_total, 0);
        if (received > 0) {
            received_total += static_cast<size_t>(received);
            continue;
        }
        if (received < 0 && errno == EINTR) {
            continue;
        }
        return false;
    }
    return true;
}

bool send_all(int socket_fd, const void *buffer, size_t length)
{
    const auto *bytes = static_cast<const uint8_t *>(buffer);
    size_t sent_total = 0;
    while (sent_total < length) {
        const ssize_t sent = send(socket_fd, bytes + sent_total,
                                  length - sent_total, 0);
        if (sent > 0) {
            sent_total += static_cast<size_t>(sent);
            continue;
        }
        if (sent < 0 && errno == EINTR) {
            continue;
        }
        return false;
    }
    return true;
}

bool client_is_allowed(const sockaddr_in &client)
{
    in_addr allowed{};
    return inet_pton(AF_INET, kAllowedClientIp, &allowed) == 1
           && client.sin_addr.s_addr == allowed.s_addr;
}

}  // namespace

LvglScreenshotDebug::LvglScreenshotDebug(lv_display_t *display)
    : display_(display)
{
}

void LvglScreenshotDebug::encode_response_header(
    uint8_t *destination, const SnapshotInfo &info)
{
    std::memset(destination, 0, kWireHeaderBytes);
    std::memcpy(destination, "CSCP", 4);
    write_be16(destination + 4, 1);
    write_be16(destination + 6, kWireHeaderBytes);
    write_be32(destination + 8, info.status);
    write_be32(destination + 12, info.sequence);
    write_be32(destination + 16, kWireColorFormatRgb565Le);
    write_be16(destination + 20, info.width);
    write_be16(destination + 22, info.height);
    write_be32(destination + 24, info.stride);
    write_be32(destination + 28, info.pixel_bytes);
    write_be32(destination + 32, info.lvgl_tick_ms);
    write_be64(destination + 36, info.captured_at_us);
    write_be32(destination + 44, info.duration_us);
}

esp_err_t LvglScreenshotDebug::start()
{
    if (task_handle_ != nullptr) {
        return ESP_OK;
    }
    if (display_ == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!allocate_snapshot_buffer()) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreatePinnedToCore(task_entry, "lvgl_shot", kTaskStackBytes,
                                this, kTaskPriority, &task_handle_,
                                kTaskCore) != pdPASS) {
        task_handle_ = nullptr;
        ESP_LOGE(kTag, "Unable to create screenshot TCP task");
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(kTag,
             "LVGL screenshot debug enabled: TCP %s:%u, Core %d, PSRAM=%u bytes",
             kAllowedClientIp, static_cast<unsigned>(kListenPort), kTaskCore,
             static_cast<unsigned>(snapshot_capacity_));
    return ESP_OK;
}

bool LvglScreenshotDebug::allocate_snapshot_buffer()
{
    if (snapshot_storage_ != nullptr) {
        return true;
    }

    if (esp_lv_adapter_lock(-1) != ESP_OK) {
        ESP_LOGE(kTag, "Cannot lock LVGL to determine screenshot dimensions");
        return false;
    }
    const uint32_t width = lv_display_get_horizontal_resolution(display_);
    const uint32_t height = lv_display_get_vertical_resolution(display_);
    esp_lv_adapter_unlock();
    if (width == 0 || height == 0 || width > UINT16_MAX || height > UINT16_MAX) {
        ESP_LOGE(kTag, "Unsupported LVGL screenshot dimensions: %ux%u",
                 static_cast<unsigned>(width), static_cast<unsigned>(height));
        return false;
    }

    const size_t capacity = static_cast<size_t>(LV_DRAW_BUF_SIZE(
        width, height, LV_COLOR_FORMAT_RGB565));
    snapshot_storage_ = heap_caps_aligned_alloc(
        kSnapshotBufferAlignment, capacity,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (snapshot_storage_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate %u-byte screenshot target in PSRAM",
                 static_cast<unsigned>(capacity));
        return false;
    }

    if (esp_lv_adapter_lock(-1) != ESP_OK) {
        ESP_LOGE(kTag, "Cannot lock LVGL to initialize screenshot target");
        heap_caps_free(snapshot_storage_);
        snapshot_storage_ = nullptr;
        return false;
    }
    const lv_result_t init_result = lv_draw_buf_init(
        &snapshot_draw_buf_, width, height, LV_COLOR_FORMAT_RGB565,
        LV_STRIDE_AUTO, snapshot_storage_, capacity);
    if (init_result == LV_RESULT_OK) {
        lv_draw_buf_set_flag(&snapshot_draw_buf_, LV_IMAGE_FLAGS_MODIFIABLE);
        snapshot_capacity_ = capacity;
    }
    esp_lv_adapter_unlock();
    if (init_result != LV_RESULT_OK) {
        ESP_LOGE(kTag, "LVGL rejected screenshot target buffer");
        heap_caps_free(snapshot_storage_);
        snapshot_storage_ = nullptr;
        return false;
    }
    return true;
}

void LvglScreenshotDebug::task_entry(void *context)
{
    static_cast<LvglScreenshotDebug *>(context)->task_main();
}

int LvglScreenshotDebug::open_listener()
{
    const int listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listener < 0) {
        ESP_LOGE(kTag, "screenshot socket() failed: errno=%d", errno);
        return -1;
    }

    const int reuse_address = 1;
    if (setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse_address,
                   sizeof(reuse_address)) != 0) {
        ESP_LOGW(kTag, "screenshot SO_REUSEADDR failed: errno=%d", errno);
    }

    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_port = htons(kListenPort);
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listener, reinterpret_cast<sockaddr *>(&local), sizeof(local)) != 0) {
        ESP_LOGE(kTag, "screenshot bind(%u) failed: errno=%d",
                 static_cast<unsigned>(kListenPort), errno);
        close(listener);
        return -1;
    }
    if (listen(listener, 1) != 0) {
        ESP_LOGE(kTag, "screenshot listen() failed: errno=%d", errno);
        close(listener);
        return -1;
    }
    return listener;
}

void LvglScreenshotDebug::task_main()
{
    listener_fd_ = open_listener();
    if (listener_fd_ < 0) {
        task_handle_ = nullptr;
        vTaskDelete(nullptr);
        return;
    }
    ESP_LOGI(kTag, "Screenshot listener ready on TCP %u",
             static_cast<unsigned>(kListenPort));

    while (true) {
        sockaddr_in client{};
        socklen_t client_length = sizeof(client);
        const int client_fd = accept(
            listener_fd_, reinterpret_cast<sockaddr *>(&client), &client_length);
        if (client_fd < 0) {
            if (errno != EINTR) {
                ESP_LOGW(kTag, "screenshot accept() failed: errno=%d", errno);
                vTaskDelay(pdMS_TO_TICKS(100));
            }
            continue;
        }
        if (!client_is_allowed(client)) {
            ESP_LOGW(kTag, "Rejected screenshot client outside local debug host");
            close(client_fd);
            continue;
        }
        handle_client(client_fd);
        close(client_fd);
    }
}

void LvglScreenshotDebug::handle_client(int client_fd)
{
    timeval timeout{};
    timeout.tv_sec = 5;
    if (setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                   sizeof(timeout)) != 0
        || setsockopt(client_fd, SOL_SOCKET, SO_SNDTIMEO, &timeout,
                      sizeof(timeout)) != 0) {
        ESP_LOGW(kTag, "Unable to configure screenshot client timeout: errno=%d",
                 errno);
        return;
    }

    char request[kRequestBytes]{};
    SnapshotInfo info{};
    if (!receive_all(client_fd, request, sizeof(request))
        || std::memcmp(request, kRequest, sizeof(request)) != 0) {
        info.status = static_cast<uint32_t>(ResponseStatus::BadRequest);
        info.sequence = ++sequence_;
    } else {
        (void)take_snapshot(&info);
    }

    uint8_t header[kWireHeaderBytes]{};
    encode_response_header(header, info);
    if (!send_all(client_fd, header, sizeof(header))) {
        ESP_LOGW(kTag, "Unable to return screenshot header: errno=%d", errno);
        return;
    }
    if (info.status != static_cast<uint32_t>(ResponseStatus::Ok)) {
        ESP_LOGW(kTag, "Screenshot request %u failed: status=%u",
                 static_cast<unsigned>(info.sequence),
                 static_cast<unsigned>(info.status));
        return;
    }
    if (!send_all(client_fd, snapshot_draw_buf_.data, info.pixel_bytes)) {
        ESP_LOGW(kTag, "Screenshot request %u payload interrupted: errno=%d",
                 static_cast<unsigned>(info.sequence), errno);
        return;
    }
    ESP_LOGI(kTag, "Screenshot request %u sent: %ux%u, %u bytes, %u us",
             static_cast<unsigned>(info.sequence),
             static_cast<unsigned>(info.width),
             static_cast<unsigned>(info.height),
             static_cast<unsigned>(info.pixel_bytes),
             static_cast<unsigned>(info.duration_us));
}

bool LvglScreenshotDebug::take_snapshot(SnapshotInfo *info)
{
    if (info == nullptr) {
        return false;
    }
    info->sequence = ++sequence_;
    const int64_t started_at_us = esp_timer_get_time();
    if (esp_lv_adapter_lock(-1) != ESP_OK) {
        info->status = static_cast<uint32_t>(ResponseStatus::LvglLockFailed);
        return false;
    }

    lv_obj_t *const screen = lv_display_get_screen_active(display_);
    if (screen == nullptr) {
        esp_lv_adapter_unlock();
        info->status = static_cast<uint32_t>(ResponseStatus::SnapshotFailed);
        return false;
    }
    info->lvgl_tick_ms = lv_tick_get();
    const lv_result_t snapshot_result = lv_snapshot_take_to_draw_buf(
        screen, LV_COLOR_FORMAT_RGB565, &snapshot_draw_buf_);
    const int64_t finished_at_us = esp_timer_get_time();
    esp_lv_adapter_unlock();
    if (snapshot_result != LV_RESULT_OK) {
        info->status = static_cast<uint32_t>(ResponseStatus::SnapshotFailed);
        return false;
    }

    const lv_image_header_t &header = snapshot_draw_buf_.header;
    const uint64_t pixel_bytes =
        static_cast<uint64_t>(header.stride) * header.h;
    if (header.cf != LV_COLOR_FORMAT_RGB565 || header.w == 0 || header.h == 0
        || pixel_bytes > snapshot_capacity_ || pixel_bytes > UINT32_MAX) {
        info->status = static_cast<uint32_t>(ResponseStatus::InvalidSnapshot);
        return false;
    }

    info->status = static_cast<uint32_t>(ResponseStatus::Ok);
    info->width = static_cast<uint16_t>(header.w);
    info->height = static_cast<uint16_t>(header.h);
    info->stride = header.stride;
    info->pixel_bytes = static_cast<uint32_t>(pixel_bytes);
    info->captured_at_us = static_cast<uint64_t>(finished_at_us);
    const int64_t duration_us = finished_at_us - started_at_us;
    info->duration_us = duration_us > static_cast<int64_t>(UINT32_MAX)
                            ? UINT32_MAX
                            : static_cast<uint32_t>(duration_us);
    return true;
}

}  // namespace cyclescope
