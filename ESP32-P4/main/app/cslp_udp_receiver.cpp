#include "cslp_udp_receiver.hpp"

#include "sdkconfig.h"

#include <cerrno>
#include <cstring>
#include <inttypes.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "ethernet_init.h"
#include "lwip/inet.h"

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
#include "cyclescope_receiver_startup_fault_test.hpp"
#endif
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
#include "cyclescope_receiver_runtime_fault_test.hpp"
#endif

namespace cyclescope {
namespace {

constexpr char kTag[] = "cslp_rx";
constexpr char kLocalIp[] = "192.168.10.3";
constexpr char kNetmask[] = "255.255.255.0";
constexpr char kFpgaIp[] = CONFIG_CYCLESCOPE_CSLP_PEER_IPV4;
constexpr int kSocketReceiveBufferBytes = 64 * 1024;
constexpr int kSocketTimeoutMs = 20;
constexpr uint32_t kEthernetStopTimeoutMs = 1000;
constexpr uint64_t kControlTimeoutUs = 100000;
constexpr int kControlMaxRetries = 3;
constexpr uint64_t kFrameAssemblyTimeoutUs = 50000;
constexpr uint64_t kLinkSilenceTimeoutUs = 1500000;
constexpr uint64_t kHealthLogPeriodUs = 30000000;
constexpr uint32_t kReceiverTaskStackBytes = 8192;
constexpr UBaseType_t kReceiverTaskPriority = 6;
constexpr BaseType_t kReceiverCore = 1;

uint32_t relaxed_load(const std::atomic<uint32_t> &value)
{
    return value.load(std::memory_order_relaxed);
}

}  // namespace

CslpUdpReceiver &cslp_udp_receiver()
{
    static CslpUdpReceiver receiver;
    return receiver;
}

esp_err_t CslpUdpReceiver::start()
{
    StartState expected = StartState::Stopped;
    if (!start_state_.compare_exchange_strong(expected, StartState::Starting,
                                              std::memory_order_acq_rel)) {
        return expected == StartState::Started ? ESP_OK : ESP_ERR_INVALID_STATE;
    }

    const auto fail_start = [this](esp_err_t error) {
        const bool rolled_back = rollback_start();
        start_state_.store(rolled_back ? StartState::Stopped : StartState::Failed,
                           std::memory_order_release);
        return rolled_back ? error : ESP_FAIL;
    };

    if (!cslp::protocol_self_test() || !receiver_policy::self_test()) {
        ESP_LOGE(kTag, "CSLP protocol/receiver self-test failed");
        return fail_start(ESP_FAIL);
    }

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (!startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::Mutex)) {
        slot_mutex_ = xSemaphoreCreateMutex();
    }
    if (!startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::EventGroup)) {
        network_events_ = xEventGroupCreate();
    }
#else
    slot_mutex_ = xSemaphoreCreateMutex();
    network_events_ = xEventGroupCreate();
#endif
    if (slot_mutex_ == nullptr || network_events_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate receiver synchronization primitives");
        return fail_start(ESP_ERR_NO_MEM);
    }

    const esp_err_t network_error = initialize_ethernet();
    if (network_error != ESP_OK) {
        ESP_LOGE(kTag, "Ethernet initialization failed: %s", esp_err_to_name(network_error));
        return fail_start(network_error);
    }

    BaseType_t task_result = pdFAIL;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (!startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::ReceiverTask)) {
        task_result = xTaskCreatePinnedToCore(
            receiver_task, "cslp_udp_rx", kReceiverTaskStackBytes, this,
            kReceiverTaskPriority, &receiver_task_handle_, kReceiverCore);
    }
#else
    task_result = xTaskCreatePinnedToCore(
        receiver_task, "cslp_udp_rx", kReceiverTaskStackBytes, this,
        kReceiverTaskPriority, &receiver_task_handle_, kReceiverCore);
#endif
    if (task_result != pdPASS) {
        ESP_LOGE(kTag, "Unable to create CSLP receiver task");
        receiver_task_handle_ = nullptr;
        return fail_start(ESP_ERR_NO_MEM);
    }

    start_state_.store(StartState::Started, std::memory_order_release);
    ESP_LOGI(kTag, "CSLP v1 receiver ready on Core %d; golden packet PASS", kReceiverCore);
    return ESP_OK;
}

bool CslpUdpReceiver::rollback_start()
{
    // Close state-changing callbacks first. invalidate_active_stream() takes
    // slot_mutex_, so it also joins a CONNECTED/GOT_IP callback that passed
    // the gate immediately before this store on the other core.
    network_callbacks_enabled_.store(false, std::memory_order_release);
    invalidate_active_stream();
    close_socket();

    // Keep our handlers registered until the target driver's STOP reaches us.
    // The following handler unregisters also synchronize with any callback
    // still running on the default event loop before its context is destroyed.
    if (ethernet_start_attempted_ && eth_handles_ != nullptr
        && eth_handle_count_ > 0) {
        xEventGroupClearBits(network_events_,
                             kIpReadyBit | kEthernetStoppedBit);
        if (eth_netif_ != nullptr) {
            // Clear the configured address while the netif is still alive.
            // Otherwise esp-netif observes the address disappearing during
            // STOP and retains one lwIP timeout node for the default 120 s
            // lost-IP grace period on every startup rollback.
            const esp_netif_ip_info_t cleared_ip{};
            const esp_err_t clear_error =
                esp_netif_set_ip_info(eth_netif_, &cleared_ip);
            if (clear_error != ESP_OK) {
                ESP_LOGE(kTag,
                         "Unable to clear static IPv4 during startup rollback: %s",
                         esp_err_to_name(clear_error));
                return false;
            }
        }
        const esp_err_t error = esp_eth_stop(eth_handles_[0]);
        if (error == ESP_OK) {
            const EventBits_t stopped = xEventGroupWaitBits(
                network_events_, kEthernetStoppedBit, pdTRUE, pdTRUE,
                pdMS_TO_TICKS(kEthernetStopTimeoutMs));
            if ((stopped & kEthernetStoppedBit) == 0) {
                ESP_LOGE(kTag,
                         "Timed out waiting for Ethernet STOP during startup rollback");
                return false;
            }
        } else if (error != ESP_ERR_INVALID_STATE) {
            ESP_LOGE(kTag,
                     "Unable to stop Ethernet during startup rollback: %s",
                     esp_err_to_name(error));
            return false;
        }
    }
    ethernet_start_attempted_ = false;

    bool handlers_removed = true;
    if (ip_event_instance_ != nullptr) {
        const esp_err_t error = esp_event_handler_instance_unregister(
            IP_EVENT, IP_EVENT_ETH_GOT_IP, ip_event_instance_);
        if (error == ESP_OK) {
            ip_event_instance_ = nullptr;
        } else {
            handlers_removed = false;
            ESP_LOGE(kTag, "Unable to unregister IP event handler: %s",
                     esp_err_to_name(error));
        }
    }
    if (eth_event_instance_ != nullptr) {
        const esp_err_t error = esp_event_handler_instance_unregister(
            ETH_EVENT, ESP_EVENT_ANY_ID, eth_event_instance_);
        if (error == ESP_OK) {
            eth_event_instance_ = nullptr;
        } else {
            handlers_removed = false;
            ESP_LOGE(kTag, "Unable to unregister Ethernet event handler: %s",
                     esp_err_to_name(error));
        }
    }
    if (!handlers_removed) {
        ESP_LOGE(kTag, "CSLP startup rollback stopped to avoid dangling event callbacks");
        return false;
    }

    if (eth_glue_ != nullptr) {
        const esp_err_t error = esp_eth_del_netif_glue(eth_glue_);
        if (error != ESP_OK) {
            ESP_LOGE(kTag, "Unable to delete Ethernet netif glue: %s",
                     esp_err_to_name(error));
            return false;
        }
        eth_glue_ = nullptr;
    }
    if (eth_netif_ != nullptr) {
        esp_netif_destroy(eth_netif_);
        eth_netif_ = nullptr;
    }
    if (eth_handles_ != nullptr) {
        const esp_err_t error = ethernet_deinit_all(eth_handles_);
        if (error != ESP_OK) {
            ESP_LOGE(kTag, "Unable to deinitialize Ethernet: %s", esp_err_to_name(error));
            return false;
        }
        eth_handles_ = nullptr;
        eth_handle_count_ = 0;
    }

    if (network_events_ != nullptr) {
        vEventGroupDelete(network_events_);
        network_events_ = nullptr;
    }
    if (slot_mutex_ != nullptr) {
        vSemaphoreDelete(slot_mutex_);
        slot_mutex_ = nullptr;
    }
    receiver_task_handle_ = nullptr;
    return true;
}

esp_err_t CslpUdpReceiver::initialize_ethernet()
{
    const int64_t initialize_started_us = esp_timer_get_time();
    esp_err_t error = ESP_OK;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::NetifInit)) {
        error = ESP_ERR_NO_MEM;
    } else {
        error = esp_netif_init();
    }
#else
    error = esp_netif_init();
#endif
    if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) {
        return error;
    }
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::EventLoop)) {
        error = ESP_ERR_NO_MEM;
    } else {
        error = esp_event_loop_create_default();
    }
#else
    error = esp_event_loop_create_default();
#endif
    if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) {
        return error;
    }
    const int64_t platform_ready_us = esp_timer_get_time();

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::EthernetInit)) {
        return ESP_ERR_NO_MEM;
    }
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::EmptyEthernetHandles)) {
        // Model a driver wrapper that reports success but produces no usable
        // devices. Continue into the real post-call guard below.
        eth_handles_ = nullptr;
        eth_handle_count_ = 0;
        error = ESP_OK;
    } else {
        error = ethernet_init_all(&eth_handles_, &eth_handle_count_);
    }
#else
    error = ethernet_init_all(&eth_handles_, &eth_handle_count_);
#endif
    if (error != ESP_OK) {
        return error;
    }
    const int64_t drivers_ready_us = esp_timer_get_time();
    if (eth_handle_count_ == 0 || eth_handles_ == nullptr) {
        return ESP_ERR_NOT_FOUND;
    }
    if (eth_handle_count_ > 1) {
        ESP_LOGW(kTag, "%u Ethernet devices detected; CSLP uses the first",
                 static_cast<unsigned>(eth_handle_count_));
    }

    esp_netif_inherent_config_t netif_inherent_config =
        ESP_NETIF_INHERENT_DEFAULT_ETH();
    netif_inherent_config.flags = static_cast<esp_netif_flags_t>(
        static_cast<uint32_t>(netif_inherent_config.flags)
        & ~static_cast<uint32_t>(ESP_NETIF_DHCP_CLIENT));
    esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
    netif_config.base = &netif_inherent_config;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (!startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::NetifCreate)) {
        eth_netif_ = esp_netif_new(&netif_config);
    }
#else
    eth_netif_ = esp_netif_new(&netif_config);
#endif
    if (eth_netif_ == nullptr) {
        return ESP_ERR_NO_MEM;
    }

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (!startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::NetifGlue)) {
        eth_glue_ = esp_eth_new_netif_glue(eth_handles_[0]);
    }
#else
    eth_glue_ = esp_eth_new_netif_glue(eth_handles_[0]);
#endif
    if (eth_glue_ == nullptr) {
        return ESP_ERR_NO_MEM;
    }
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::NetifAttach)) {
        error = ESP_ERR_NO_MEM;
    } else {
        error = esp_netif_attach(eth_netif_, eth_glue_);
    }
#else
    error = esp_netif_attach(eth_netif_, eth_glue_);
#endif
    if (error != ESP_OK) {
        return error;
    }

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::EthEventHandler)) {
        error = ESP_ERR_NO_MEM;
    } else {
        error = esp_event_handler_instance_register(
            ETH_EVENT, ESP_EVENT_ANY_ID, network_event_handler, this,
            &eth_event_instance_);
    }
#else
    error = esp_event_handler_instance_register(
        ETH_EVENT, ESP_EVENT_ANY_ID, network_event_handler, this,
        &eth_event_instance_);
#endif
    if (error != ESP_OK) {
        return error;
    }
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::IpEventHandler)) {
        error = ESP_ERR_NO_MEM;
    } else {
        error = esp_event_handler_instance_register(
            IP_EVENT, IP_EVENT_ETH_GOT_IP, network_event_handler, this,
            &ip_event_instance_);
    }
#else
    error = esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_ETH_GOT_IP, network_event_handler, this,
        &ip_event_instance_);
#endif
    if (error != ESP_OK) {
        return error;
    }

#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::StaticIp)) {
        error = ESP_ERR_NO_MEM;
    } else {
        error = configure_static_ip();
    }
#else
    error = configure_static_ip();
#endif
    if (error != ESP_OK) {
        return error;
    }

    network_callbacks_enabled_.store(true, std::memory_order_release);
    ethernet_start_attempted_ = true;
    const int64_t attach_ready_us = esp_timer_get_time();
    esp_err_t start_error = ESP_OK;
#if CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST
    if (startup_fault_test::consume_receiver_failpoint(
            startup_fault_test::ReceiverFailPoint::EthernetStart)) {
        start_error = ESP_ERR_NO_MEM;
    } else {
        start_error = esp_eth_start(eth_handles_[0]);
    }
#else
    start_error = esp_eth_start(eth_handles_[0]);
#endif
    const int64_t ethernet_started_us = esp_timer_get_time();
    ESP_LOGI(
        kTag,
        "Ethernet startup us: platform=%" PRIi64 " drivers=%" PRIi64
        " attach=%" PRIi64 " start=%" PRIi64 " total=%" PRIi64,
        platform_ready_us - initialize_started_us,
        drivers_ready_us - platform_ready_us,
        attach_ready_us - drivers_ready_us,
        ethernet_started_us - attach_ready_us,
        ethernet_started_us - initialize_started_us);
    return start_error;
}

esp_err_t CslpUdpReceiver::configure_static_ip()
{
    esp_netif_ip_info_t ip_info{};
    ip_info.ip.addr = ipaddr_addr(kLocalIp);
    ip_info.netmask.addr = ipaddr_addr(kNetmask);
    ip_info.gw.addr = 0;
    return esp_netif_set_ip_info(eth_netif_, &ip_info);
}

void CslpUdpReceiver::network_event_handler(void *context, esp_event_base_t event_base,
                                            int32_t event_id, void *event_data)
{
    auto *receiver = static_cast<CslpUdpReceiver *>(context);
    if (event_base == ETH_EVENT) {
        if (event_data == nullptr || receiver->eth_handles_ == nullptr
            || receiver->eth_handle_count_ == 0
            || *static_cast<esp_eth_handle_t *>(event_data)
                   != receiver->eth_handles_[0]) {
            return;
        }
        if (event_id == ETHERNET_EVENT_CONNECTED) {
            if (receiver->slot_mutex_ == nullptr) {
                return;
            }
            xSemaphoreTake(receiver->slot_mutex_, portMAX_DELAY);
            if (receiver->network_callbacks_enabled_.load(
                    std::memory_order_acquire)) {
                // The startup call validates and caches the address before
                // esp_eth_start().  This second, idempotent call happens after
                // esp_netif_up(); without a DHCP client it is what publishes
                // IP_EVENT_ETH_GOT_IP and releases the receiver task.
                const esp_err_t error = receiver->configure_static_ip();
                if (error != ESP_OK) {
                    ESP_LOGE(kTag, "Unable to configure static IPv4: %s",
                             esp_err_to_name(error));
                }
            }
            xSemaphoreGive(receiver->slot_mutex_);
        } else if (event_id == ETHERNET_EVENT_DISCONNECTED
                   || event_id == ETHERNET_EVENT_STOP) {
            xEventGroupClearBits(receiver->network_events_, kIpReadyBit);
            receiver->invalidate_active_stream();
            if (event_id == ETHERNET_EVENT_STOP) {
                xEventGroupSetBits(receiver->network_events_,
                                   kEthernetStoppedBit);
            }
            ESP_LOGW(kTag, "Ethernet link down");
        }
        return;
    }

    if (event_base == IP_EVENT && event_id == IP_EVENT_ETH_GOT_IP) {
        const auto *event = static_cast<const ip_event_got_ip_t *>(event_data);
        if (event->esp_netif != receiver->eth_netif_) {
            return;
        }
        if (receiver->slot_mutex_ == nullptr) {
            return;
        }
        xSemaphoreTake(receiver->slot_mutex_, portMAX_DELAY);
        if (receiver->network_callbacks_enabled_.load(
                std::memory_order_acquire)) {
            xEventGroupSetBits(receiver->network_events_, kIpReadyBit);
            ESP_LOGI(kTag, "Ethernet static IPv4 ready: " IPSTR,
                     IP2STR(&event->ip_info.ip));
        }
        xSemaphoreGive(receiver->slot_mutex_);
    }
}

void CslpUdpReceiver::receiver_task(void *context)
{
    static_cast<CslpUdpReceiver *>(context)->task_main();
}

void CslpUdpReceiver::task_main()
{
    bool attempted_session = false;
#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
    bool disable_push_test_pending = true;
#endif
    while (true) {
        const EventBits_t ready = xEventGroupWaitBits(network_events_, kIpReadyBit, pdFALSE,
                                                      pdTRUE, pdMS_TO_TICKS(500));
        if ((ready & kIpReadyBit) == 0) {
            continue;
        }

        if (attempted_session) {
            stats_.reconnects.fetch_add(1, std::memory_order_relaxed);
        }
        attempted_session = true;

        if (!open_socket() || !establish_session()) {
            invalidate_active_stream();
            reset_pending_frames();
            close_socket();
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
        const uint32_t disable_push_test_completed_baseline =
            relaxed_load(stats_.frames_completed);
#endif

        while ((xEventGroupGetBits(network_events_) & kIpReadyBit) != 0
               && active_session_id_.load(std::memory_order_acquire) != 0) {
            cslp::CommonHeader common{};
            size_t length = 0;
            const ReceiveResult result = receive_valid_datagram(&common, &length);

            if (result == ReceiveResult::Valid) {
                dispatch_datagram(common, length);
            } else if (result == ReceiveResult::Fatal) {
                break;
            }

            const uint64_t current_time = now_us();
            expire_assembly(current_time);
#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
            if (disable_push_test_pending
                && relaxed_load(stats_.frames_completed)
                       - disable_push_test_completed_baseline
                       >= 8U) {
                disable_push_test_pending = false;
                if (!run_disable_push_reconfigure_test()) {
                    ESP_LOGE(kTag,
                             "DISABLE/CONFIG/ENABLE lifecycle test failed");
                    break;
                }
            }
#endif
            if (last_stream_message_us_ != 0
                && current_time >= last_stream_message_us_
                && current_time - last_stream_message_us_ > kLinkSilenceTimeoutUs) {
                ESP_LOGW(kTag, "CSLP peer silent for more than 1500 ms; starting a new session");
                break;
            }
            if (current_time - last_health_log_us_ >= kHealthLogPeriodUs) {
                last_health_log_us_ = current_time;
                log_health();
            }
        }

        invalidate_active_stream();
        reset_pending_frames();
        close_socket();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

bool CslpUdpReceiver::open_socket()
{
    close_socket();
    int socket_type = SOCK_DGRAM;
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
    if (runtime_fault_test::consume_receiver_runtime_failpoint(
            runtime_fault_test::ReceiverRuntimeFailPoint::SocketCreate)) {
        socket_type = -1;
    }
#endif
    socket_fd_ = socket(AF_INET, socket_type, IPPROTO_IP);
    if (socket_fd_ < 0) {
        stats_.socket_open_failures.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGE(kTag, "socket() failed: errno=%d", errno);
        return false;
    }
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
    runtime_fault_test::note_receiver_socket_opened(socket_fd_);
#endif

    const int reuse_address = 1;
    if (setsockopt(socket_fd_, SOL_SOCKET, SO_REUSEADDR,
                   &reuse_address, sizeof(reuse_address)) != 0) {
        ESP_LOGW(kTag, "SO_REUSEADDR failed: errno=%d", errno);
    }
    const int receive_buffer_bytes = kSocketReceiveBufferBytes;
    if (setsockopt(socket_fd_, SOL_SOCKET, SO_RCVBUF,
                   &receive_buffer_bytes, sizeof(receive_buffer_bytes)) != 0) {
        ESP_LOGW(kTag, "SO_RCVBUF failed: errno=%d", errno);
    }
    const timeval timeout = {
        .tv_sec = 0,
        .tv_usec = kSocketTimeoutMs * 1000,
    };
    int receive_timeout_fd = socket_fd_;
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
    if (runtime_fault_test::consume_receiver_runtime_failpoint(
            runtime_fault_test::ReceiverRuntimeFailPoint::ReceiveTimeout)) {
        receive_timeout_fd = -1;
    }
#endif
    if (setsockopt(receive_timeout_fd, SOL_SOCKET, SO_RCVTIMEO,
                   &timeout, sizeof(timeout)) != 0) {
        stats_.socket_open_failures.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGE(kTag, "SO_RCVTIMEO failed: errno=%d", errno);
        close_socket();
        return false;
    }

    sockaddr_in local_address{};
    local_address.sin_family = AF_INET;
    local_address.sin_port = htons(kLocalPort);
    local_address.sin_addr.s_addr = htonl(INADDR_ANY);
    int bind_fd = socket_fd_;
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
    if (runtime_fault_test::consume_receiver_runtime_failpoint(
            runtime_fault_test::ReceiverRuntimeFailPoint::Bind)) {
        bind_fd = -1;
    }
#endif
    if (bind(bind_fd, reinterpret_cast<sockaddr *>(&local_address),
             sizeof(local_address)) != 0) {
        stats_.socket_open_failures.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGE(kTag, "bind(%u) failed: errno=%d",
                 static_cast<unsigned>(kLocalPort), errno);
        close_socket();
        return false;
    }

    std::memset(&fpga_address_, 0, sizeof(fpga_address_));
    fpga_address_.sin_family = AF_INET;
    fpga_address_.sin_port = htons(kFpgaPort);
    if (inet_pton(AF_INET, kFpgaIp, &fpga_address_.sin_addr) != 1) {
        stats_.socket_open_failures.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGE(kTag, "Invalid FPGA IPv4 constant");
        close_socket();
        return false;
    }
    ESP_LOGI(kTag, "UDP %s:%u -> %s:%u", kLocalIp,
             static_cast<unsigned>(kLocalPort), kFpgaIp,
             static_cast<unsigned>(kFpgaPort));
    return true;
}

void CslpUdpReceiver::close_socket()
{
    if (socket_fd_ >= 0) {
        const int socket_to_close = socket_fd_;
        shutdown(socket_to_close, SHUT_RDWR);
        const int close_result = close(socket_to_close);
        if (close_result != 0) {
            stats_.socket_close_failures.fetch_add(1,
                                                   std::memory_order_relaxed);
            ESP_LOGE(kTag, "close(%d) failed: errno=%d", socket_to_close,
                     errno);
        }
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
        runtime_fault_test::note_receiver_socket_closed(socket_to_close,
                                                        close_result);
#endif
        socket_fd_ = -1;
    }
}

bool CslpUdpReceiver::establish_session()
{
    invalidate_active_stream();
    reset_pending_frames();
    const uint32_t handshake_epoch =
        active_stream_epoch_.load(std::memory_order_acquire);
    device_boot_id_ = 0;
    const uint32_t initial_session_seed = session_id_ == 0 ? esp_random() : 0;
    session_id_ = receiver_policy::next_session_id(session_id_, initial_session_seed);
    control_sequence_ = esp_random();
    uint32_t negotiated_config_id = 0;
    if (!run_hello() || !run_config(&negotiated_config_id)
        || !run_enable_push()) {
        ESP_LOGW(kTag, "CSLP session 0x%08" PRIX32 " handshake failed", session_id_);
        return false;
    }

    if (!commit_active_stream(session_id_, negotiated_config_id,
                              handshake_epoch)) {
        ESP_LOGW(kTag,
                 "CSLP session 0x%08" PRIX32
                 " invalidated before activation",
                 session_id_);
        return false;
    }
    stats_.sessions_established.fetch_add(1, std::memory_order_relaxed);
    last_stream_message_us_ = now_us();
    ESP_LOGI(kTag, "CSLP session ready: session=0x%08" PRIX32
             " boot=%" PRIu32 " config=%" PRIu32,
             session_id_, device_boot_id_,
             active_config_id_.load(std::memory_order_acquire));
    return true;
}

size_t CslpUdpReceiver::prepare_request(cslp::MessageType type, uint32_t sequence,
                                        uint16_t payload_bytes)
{
    if (!cslp::encode_common_header(tx_buffer_.data(), tx_buffer_.size(), type,
                                    cslp::kCommonHeaderBytes, session_id_, sequence,
                                    now_us(), payload_bytes, 0)) {
        return 0;
    }
    return cslp::kCommonHeaderBytes + payload_bytes;
}

uint32_t CslpUdpReceiver::next_control_sequence()
{
    ++control_sequence_;
    if (control_sequence_ == 0) {
        ++control_sequence_;
    }
    return control_sequence_;
}

bool CslpUdpReceiver::run_hello()
{
    const uint32_t sequence = next_control_sequence();
    const size_t length = prepare_request(cslp::MessageType::Hello, sequence, 8);
    if (length == 0) {
        return false;
    }
    cslp::write_be16(tx_buffer_.data() + 32, kLocalPort);
    cslp::write_be16(tx_buffer_.data() + 34, cslp::kMaxUdpPayloadBytes);
    cslp::write_be32(tx_buffer_.data() + 36, cslp::kRequiredCapabilities);
    cslp::finalize_crc32(tx_buffer_.data(), length);

    cslp::CommonHeader response{};
    if (!transact(length, cslp::MessageType::HelloAck, sequence, &response)
        || response.payload_bytes != 16) {
        return false;
    }

    const uint8_t *payload = rx_buffer_.data() + cslp::kCommonHeaderBytes;
    const auto status = static_cast<cslp::StatusCode>(cslp::read_be16(payload));
    const uint8_t negotiated_version = payload[2];
    const uint8_t reserved = payload[3];
    const uint32_t capabilities = cslp::read_be32(payload + 4);
    const uint32_t max_frame_samples = cslp::read_be32(payload + 8);
    const uint32_t boot_id = cslp::read_be32(payload + 12);
    if (status != cslp::StatusCode::Ok || negotiated_version != cslp::kVersion
        || reserved != 0 || capabilities != cslp::kRequiredCapabilities
        || max_frame_samples < kFrameSampleCount || boot_id == 0) {
        ESP_LOGE(kTag, "HELLO_ACK rejected: status=%u caps=0x%08" PRIX32
                 " max=%" PRIu32 " boot=%" PRIu32,
                 static_cast<unsigned>(status), capabilities, max_frame_samples, boot_id);
        return false;
    }
    device_boot_id_ = boot_id;
    return true;
}

bool CslpUdpReceiver::run_config(uint32_t *negotiated_config_id)
{
    if (negotiated_config_id == nullptr) {
        return false;
    }
    *negotiated_config_id = 0;
    const uint32_t sequence = next_control_sequence();
    const size_t length = prepare_request(cslp::MessageType::ConfigSet, sequence, 20);
    if (length == 0) {
        return false;
    }
    cslp::write_be32(tx_buffer_.data() + 32, kSampleRateHz);
    cslp::write_be32(tx_buffer_.data() + 36, kFrameSampleCount);
    cslp::write_be32(tx_buffer_.data() + 40, kFramePeriodUs);
    tx_buffer_[44] = cslp::kSampleFormatS16Le;
    tx_buffer_[45] = 1;
    cslp::write_be16(tx_buffer_.data() + 46, kFilterProfile);
    cslp::write_be32(tx_buffer_.data() + 48, 0);
    cslp::finalize_crc32(tx_buffer_.data(), length);

    cslp::CommonHeader response{};
    if (!transact(length, cslp::MessageType::ConfigAck, sequence, &response)
        || response.payload_bytes != 28) {
        return false;
    }

    const uint8_t *payload = rx_buffer_.data() + cslp::kCommonHeaderBytes;
    const auto status = static_cast<cslp::StatusCode>(cslp::read_be16(payload));
    const uint16_t reserved = cslp::read_be16(payload + 2);
    const uint32_t config_id = cslp::read_be32(payload + 4);
    const uint32_t sample_rate_hz = cslp::read_be32(payload + 8);
    const uint32_t frame_sample_count = cslp::read_be32(payload + 12);
    const uint32_t frame_period_us = cslp::read_be32(payload + 16);
    const uint8_t sample_format = payload[20];
    const uint8_t channel_count = payload[21];
    const uint16_t filter_profile = cslp::read_be16(payload + 22);
    const uint32_t max_frame_samples = cslp::read_be32(payload + 24);
    if (status != cslp::StatusCode::Ok || reserved != 0 || config_id == 0
        || sample_rate_hz != kSampleRateHz || frame_sample_count != kFrameSampleCount
        || frame_period_us != kFramePeriodUs
        || sample_format != cslp::kSampleFormatS16Le || channel_count != 1
        || filter_profile != kFilterProfile || max_frame_samples < kFrameSampleCount) {
        ESP_LOGE(kTag, "CONFIG_ACK rejected: status=%u config=%" PRIu32
                 " Fs=%" PRIu32 " N=%" PRIu32 " period=%" PRIu32,
                 static_cast<unsigned>(status), config_id, sample_rate_hz,
                 frame_sample_count, frame_period_us);
        return false;
    }
    *negotiated_config_id = config_id;
    return true;
}

bool CslpUdpReceiver::run_enable_push()
{
    const uint32_t sequence = next_control_sequence();
    const size_t length = prepare_request(cslp::MessageType::EnablePush, sequence, 0);
    if (length == 0) {
        return false;
    }
    cslp::finalize_crc32(tx_buffer_.data(), length);

    cslp::CommonHeader response{};
    if (!transact(length, cslp::MessageType::EnablePushAck, sequence, &response)
        || response.payload_bytes != 4) {
        return false;
    }
    const uint8_t *payload = rx_buffer_.data() + cslp::kCommonHeaderBytes;
    const auto status = static_cast<cslp::StatusCode>(cslp::read_be16(payload));
    return status == cslp::StatusCode::Ok && cslp::read_be16(payload + 2) == 0;
}

bool CslpUdpReceiver::run_disable_push()
{
    const uint32_t sequence = next_control_sequence();
    const size_t length =
        prepare_request(cslp::MessageType::DisablePush, sequence, 0);
    if (length == 0) {
        return false;
    }
    cslp::finalize_crc32(tx_buffer_.data(), length);

    cslp::CommonHeader response{};
    if (!transact(length, cslp::MessageType::DisablePushAck, sequence,
                  &response)
        || response.payload_bytes != 4) {
        return false;
    }
    const uint8_t *payload = rx_buffer_.data() + cslp::kCommonHeaderBytes;
    const auto status =
        static_cast<cslp::StatusCode>(cslp::read_be16(payload));
    return status == cslp::StatusCode::Ok
           && cslp::read_be16(payload + 2) == 0;
}

#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
bool CslpUdpReceiver::run_disable_push_reconfigure_test()
{
    const uint32_t previous_config_id =
        active_config_id_.load(std::memory_order_acquire);
    disable_test_previous_config_id_.store(previous_config_id,
                                           std::memory_order_release);
    disable_test_reconfigured_.store(false, std::memory_order_release);
    disable_test_in_progress_.store(true, std::memory_order_release);
    ESP_LOGI(kTag,
             "Starting DISABLE/CONFIG/ENABLE lifecycle test: session=0x%08"
             PRIX32 " config=%" PRIu32,
             session_id_, previous_config_id);
    if (!run_disable_push()) {
        disable_test_in_progress_.store(false, std::memory_order_release);
        return false;
    }

    // Once DISABLE_PUSH_ACK is accepted, no old-config WAVE may enter the
    // reassembler. transact() still receives datagrams while CONFIG/ENABLE
    // are in flight; the active-session gate drops those WAVE packets before
    // decoding or statistics.
    invalidate_active_stream();
    reset_pending_frames();
    const uint32_t reconfigure_epoch =
        active_stream_epoch_.load(std::memory_order_acquire);
    uint32_t negotiated_config_id = 0;
    if (!run_config(&negotiated_config_id)
        || negotiated_config_id == previous_config_id || !run_enable_push()
        || !commit_active_stream(session_id_, negotiated_config_id,
                                 reconfigure_epoch)) {
        invalidate_active_stream();
        disable_test_in_progress_.store(false, std::memory_order_release);
        return false;
    }

    last_stream_message_us_ = now_us();
    disable_test_reconfigured_.store(true, std::memory_order_release);
    ESP_LOGI(kTag,
             "DISABLE/CONFIG/ENABLE lifecycle test PASS: session=0x%08"
             PRIX32 " config=%" PRIu32 "->%" PRIu32,
             session_id_, previous_config_id,
             active_config_id_.load(std::memory_order_acquire));
    return true;
}
#endif

bool CslpUdpReceiver::transact(size_t request_length, cslp::MessageType expected_response,
                               uint32_t request_sequence, cslp::CommonHeader *response)
{
    for (int attempt = 0; attempt <= kControlMaxRetries; ++attempt) {
        if ((xEventGroupGetBits(network_events_) & kIpReadyBit) == 0) {
            return false;
        }
        if (attempt > 0) {
            stats_.control_retries.fetch_add(1, std::memory_order_relaxed);
        }

        const ssize_t sent = sendto(socket_fd_, tx_buffer_.data(), request_length, 0,
                                    reinterpret_cast<const sockaddr *>(&fpga_address_),
                                    sizeof(fpga_address_));
        if (sent != static_cast<ssize_t>(request_length)) {
            ESP_LOGW(kTag, "Control send failed: errno=%d attempt=%d", errno, attempt + 1);
        }

        const uint64_t deadline = now_us() + kControlTimeoutUs;
        while (now_us() < deadline) {
            cslp::CommonHeader incoming{};
            size_t incoming_length = 0;
            const ReceiveResult result = receive_valid_datagram(&incoming, &incoming_length);
            if (result == ReceiveResult::Fatal) {
                return false;
            }
            if (result == ReceiveResult::Timeout) {
                continue;
            }
            if (incoming.message_type == expected_response
                && incoming.message_seq == request_sequence) {
                *response = incoming;
                return true;
            }
            dispatch_datagram(incoming, incoming_length);
        }
    }
    ESP_LOGW(kTag, "Control transaction 0x%02X seq=%" PRIu32 " timed out",
             static_cast<unsigned>(expected_response), request_sequence);
    return false;
}

CslpUdpReceiver::ReceiveResult
CslpUdpReceiver::receive_valid_datagram(cslp::CommonHeader *common, size_t *length)
{
    sockaddr_in source{};
    socklen_t source_length = sizeof(source);
    int receive_fd = socket_fd_;
#if CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST
    if (runtime_fault_test::consume_receiver_runtime_failpoint(
            runtime_fault_test::ReceiverRuntimeFailPoint::RecvfromFatalActive)) {
        // Exercise lwIP's real EBADF path without closing the live socket.
        // The normal teardown below remains the sole owner of the real fd.
        receive_fd = -1;
    }
#endif
    const ssize_t received = recvfrom(receive_fd, rx_buffer_.data(), rx_buffer_.size(), 0,
                                      reinterpret_cast<sockaddr *>(&source), &source_length);
    if (received < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            return ReceiveResult::Timeout;
        }
        stats_.recv_fatal_errors.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGE(kTag, "recvfrom failed: errno=%d", errno);
        return ReceiveResult::Fatal;
    }

    stats_.udp_packets_received.fetch_add(1, std::memory_order_relaxed);
    if (source.sin_family != AF_INET
        || source.sin_addr.s_addr != fpga_address_.sin_addr.s_addr
        || source.sin_port != fpga_address_.sin_port) {
        stats_.bad_source.fetch_add(1, std::memory_order_relaxed);
        return ReceiveResult::Timeout;
    }

    const size_t datagram_length = static_cast<size_t>(received);
    if (datagram_length > cslp::kMaxUdpPayloadBytes) {
        stats_.bad_length.fetch_add(1, std::memory_order_relaxed);
        return ReceiveResult::Timeout;
    }
    const cslp::ParseError parse_error =
        cslp::decode_common_header(rx_buffer_.data(), datagram_length, common);
    if (parse_error != cslp::ParseError::None) {
        record_parse_error(parse_error);
        return ReceiveResult::Timeout;
    }
    if (common->session_id != session_id_) {
        stats_.bad_session.fetch_add(1, std::memory_order_relaxed);
        return ReceiveResult::Timeout;
    }
    if (!cslp::verify_crc32(rx_buffer_.data(), datagram_length, *common)) {
        stats_.crc_failures.fetch_add(1, std::memory_order_relaxed);
        return ReceiveResult::Timeout;
    }

    *length = datagram_length;
    return ReceiveResult::Valid;
}

void CslpUdpReceiver::record_parse_error(cslp::ParseError error)
{
    switch (error) {
    case cslp::ParseError::BadMagic:
        stats_.bad_magic.fetch_add(1, std::memory_order_relaxed);
        break;
    case cslp::ParseError::BadVersion:
        stats_.bad_version.fetch_add(1, std::memory_order_relaxed);
        break;
    case cslp::ParseError::TooShort:
    case cslp::ParseError::BadLength:
    case cslp::ParseError::BadType:
    case cslp::ParseError::BadFlags:
        stats_.bad_length.fetch_add(1, std::memory_order_relaxed);
        break;
    case cslp::ParseError::None:
        break;
    }
}

void CslpUdpReceiver::dispatch_datagram(const cslp::CommonHeader &common, size_t length)
{
    switch (common.message_type) {
    case cslp::MessageType::Status:
        handle_status(common, length);
        break;
    case cslp::MessageType::WaveData:
        handle_wave(common, length);
        break;
    case cslp::MessageType::Error:
        handle_error(common, length);
        break;
    default:
        break;
    }
}

void CslpUdpReceiver::handle_status(const cslp::CommonHeader &common, size_t length)
{
    if (common.payload_bytes != 40 || length != cslp::kCommonHeaderBytes + 40) {
        stats_.bad_length.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    const uint8_t *payload = rx_buffer_.data() + cslp::kCommonHeaderBytes;
    const uint16_t device_state = cslp::read_be16(payload);
    const uint16_t last_error = cslp::read_be16(payload + 2);
    const uint32_t active_config_id = cslp::read_be32(payload + 4);
    const uint32_t reserved = cslp::read_be32(payload + 36);
    if (reserved != 0 || device_state > 3) {
        stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    const uint32_t expected_config_id =
        active_config_id_.load(std::memory_order_acquire);
    if (expected_config_id != 0 && active_config_id != expected_config_id) {
        stats_.config_mismatches.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGW(kTag, "STATUS config changed from %" PRIu32 " to %" PRIu32,
                 expected_config_id, active_config_id);
        invalidate_active_stream();
        return;
    }
    last_stream_message_us_ = now_us();
    const bool session_active = active_session_id_.load(std::memory_order_acquire) != 0;
    if (device_state != 2 || last_error != 0) {
        ESP_LOGW(kTag, "FPGA STATUS state=%u last_error=%u",
                 static_cast<unsigned>(device_state), static_cast<unsigned>(last_error));
    }
    if (session_active && device_state != 2) {
        ESP_LOGW(kTag, "FPGA left PUSH_ENABLED; starting a new session");
        invalidate_active_stream();
    }
}

void CslpUdpReceiver::handle_error(const cslp::CommonHeader &common, size_t length)
{
    if (common.payload_bytes != 12 || length != cslp::kCommonHeaderBytes + 12) {
        stats_.bad_length.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    const uint8_t *payload = rx_buffer_.data() + cslp::kCommonHeaderBytes;
    if (payload[3] != 0) {
        stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    ESP_LOGW(kTag, "FPGA ERROR code=%u type=0x%02X seq=%" PRIu32 " detail=%" PRIu32,
             static_cast<unsigned>(cslp::read_be16(payload)),
             static_cast<unsigned>(payload[2]), cslp::read_be32(payload + 4),
             cslp::read_be32(payload + 8));
}

bool CslpUdpReceiver::validate_wave(const cslp::CommonHeader &common,
                                    const cslp::WaveHeader &wave)
{
    if (wave.frame_id == 0 || wave.chunk_count != kChunkCount
        || wave.chunk_index >= wave.chunk_count
        || wave.sample_format != cslp::kSampleFormatS16Le || wave.channel_count != 1
        || wave.scale_uv_per_lsb == 0) {
        stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
        return false;
    }

    const size_t expected_samples =
        wave.chunk_index + 1U < kChunkCount
            ? kSamplesPerFullChunk
            : kFrameSampleCount - (kChunkCount - 1U) * kSamplesPerFullChunk;
    const uint32_t expected_offset =
        static_cast<uint32_t>(wave.chunk_index) * kSamplesPerFullChunk;
    const uint16_t expected_payload_bytes =
        static_cast<uint16_t>(expected_samples * sizeof(int16_t));
    const bool expected_first = wave.chunk_index == 0;
    const bool expected_last = wave.chunk_index + 1U == wave.chunk_count;
    const bool first_flag = (common.flags & cslp::kFlagFirstChunk) != 0;
    const bool last_flag = (common.flags & cslp::kFlagLastChunk) != 0;
    if (wave.samples_in_chunk != expected_samples || wave.sample_offset != expected_offset
        || common.payload_bytes != expected_payload_bytes
        || first_flag != expected_first || last_flag != expected_last) {
        stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
        return false;
    }

    const bool calibrated_flag = (common.flags & cslp::kFlagCalibrated) != 0;
    if (calibrated_flag != (wave.calibration_id != 0)) {
        stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    const uint32_t active_config_id =
        active_config_id_.load(std::memory_order_acquire);
    if (active_config_id == 0 || wave.config_id != active_config_id
        || wave.sample_rate_hz != kSampleRateHz
        || wave.frame_sample_count != kFrameSampleCount
        || wave.filter_profile != kFilterProfile
        || (common.flags & cslp::kFlagFiltered) == 0) {
        stats_.config_mismatches.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    return true;
}

void CslpUdpReceiver::handle_wave(const cslp::CommonHeader &common, size_t length)
{
    const uint32_t active_session_id =
        active_session_id_.load(std::memory_order_acquire);
    if (!receiver_policy::session_is_current(active_session_id, common.session_id)) {
        return;
    }

    cslp::WaveHeader wave{};
    if (!cslp::decode_wave_header(rx_buffer_.data(), length, common, &wave)) {
        stats_.bad_length.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    if (!validate_wave(common, wave)) {
        reject_frame(wave.frame_id);
        return;
    }

    if (wave.frame_id == rejected_frame_id_) {
        return;
    }

    if (!have_observed_frame_) {
        newest_observed_frame_id_ = wave.frame_id;
        have_observed_frame_ = true;
    } else if (wave.frame_id != newest_observed_frame_id_) {
        if (!cslp::sequence_is_newer(wave.frame_id, newest_observed_frame_id_)) {
            stats_.stale_chunks.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        newest_observed_frame_id_ = wave.frame_id;
        invalidate_assembly(true);
        rejected_frame_id_ = 0;
    }

    last_stream_message_us_ = now_us();
    const bool adc_overrange = (common.flags & cslp::kFlagAdcOverrange) != 0;
    const bool fifo_overflow = (common.flags & cslp::kFlagFifoOverflow) != 0;
    if (adc_overrange) {
        stats_.overrange_frames.fetch_add(1, std::memory_order_relaxed);
    }
    if (fifo_overflow) {
        stats_.fifo_overflow_frames.fetch_add(1, std::memory_order_relaxed);
    }
    if (adc_overrange || fifo_overflow) {
        reject_frame(wave.frame_id);
        return;
    }

    const int slot_index = ensure_assembly_slot(common, wave);
    if (slot_index < 0) {
        return;
    }
    FrameSlot &slot = slots_[slot_index];
    const uint16_t chunk_bit = static_cast<uint16_t>(1U << wave.chunk_index);
    if ((slot.chunk_bitmap & chunk_bit) != 0) {
        if (slot.chunk_crc[wave.chunk_index] == common.crc32) {
            stats_.duplicate_chunks.fetch_add(1, std::memory_order_relaxed);
        } else {
            stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
            reject_frame(wave.frame_id);
        }
        return;
    }

    const uint8_t *payload = rx_buffer_.data() + cslp::kWaveHeaderBytes;
    for (size_t sample = 0; sample < wave.samples_in_chunk; ++sample) {
        slot.samples[wave.sample_offset + sample] =
            cslp::read_le_i16(payload + sample * sizeof(int16_t));
    }
    slot.chunk_crc[wave.chunk_index] = common.crc32;
    slot.chunk_bitmap = static_cast<uint16_t>(slot.chunk_bitmap | chunk_bit);

    if (slot.chunk_bitmap == kAllChunksMask) {
        publish_assembly();
    }
}

int CslpUdpReceiver::ensure_assembly_slot(const cslp::CommonHeader &common,
                                          const cslp::WaveHeader &wave)
{
    if (assembling_index_ >= 0) {
        FrameSlot &current = slots_[assembling_index_];
        if (wave.frame_id == current.metadata.frame_id) {
            if (!shared_metadata_matches(current, common, wave)) {
                stats_.metadata_conflicts.fetch_add(1, std::memory_order_relaxed);
                reject_frame(wave.frame_id);
                return -1;
            }
            return assembling_index_;
        }
        if (!cslp::sequence_is_newer(wave.frame_id, current.metadata.frame_id)) {
            stats_.stale_chunks.fetch_add(1, std::memory_order_relaxed);
            return -1;
        }

        stats_.incomplete_frames.fetch_add(1, std::memory_order_relaxed);
        initialize_assembly(&current, common, wave);
        return assembling_index_;
    }

    if (have_completed_frame_
        && !cslp::sequence_is_newer(wave.frame_id, last_completed_frame_id_)) {
        stats_.stale_chunks.fetch_add(1, std::memory_order_relaxed);
        return -1;
    }

    int free_index = -1;
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    for (size_t index = 0; index < slots_.size(); ++index) {
        if (slots_[index].state == SlotState::Free) {
            slots_[index].state = SlotState::Assembling;
            free_index = static_cast<int>(index);
            break;
        }
    }
    xSemaphoreGive(slot_mutex_);
    if (free_index < 0) {
        stats_.dropped_busy.fetch_add(1, std::memory_order_relaxed);
        reject_frame(wave.frame_id);
        return -1;
    }

    assembling_index_ = free_index;
    initialize_assembly(&slots_[free_index], common, wave);
    return free_index;
}

void CslpUdpReceiver::initialize_assembly(FrameSlot *slot,
                                          const cslp::CommonHeader &common,
                                          const cslp::WaveHeader &wave)
{
    slot->metadata = {
        .session_id = common.session_id,
        .frame_id = wave.frame_id,
        .timestamp_us = common.timestamp_us,
        .sample_rate_hz = wave.sample_rate_hz,
        .sample_count = wave.frame_sample_count,
        .scale_uv_per_lsb = wave.scale_uv_per_lsb,
        .offset_uv = wave.offset_uv,
        .config_id = wave.config_id,
        .filter_profile = wave.filter_profile,
        .calibration_id = wave.calibration_id,
        .flags = static_cast<uint16_t>(common.flags
                                       & ~(cslp::kFlagFirstChunk | cslp::kFlagLastChunk)),
    };
    slot->chunk_crc.fill(0);
    slot->chunk_bitmap = 0;
    slot->assembly_started_us = now_us();
}

bool CslpUdpReceiver::shared_metadata_matches(const FrameSlot &slot,
                                              const cslp::CommonHeader &common,
                                              const cslp::WaveHeader &wave) const
{
    const uint16_t frame_flags =
        static_cast<uint16_t>(common.flags
                              & ~(cslp::kFlagFirstChunk | cslp::kFlagLastChunk));
    return slot.metadata.session_id == common.session_id
           && slot.metadata.frame_id == wave.frame_id
           && slot.metadata.timestamp_us == common.timestamp_us
           && slot.metadata.sample_rate_hz == wave.sample_rate_hz
           && slot.metadata.sample_count == wave.frame_sample_count
           && slot.metadata.scale_uv_per_lsb == wave.scale_uv_per_lsb
           && slot.metadata.offset_uv == wave.offset_uv
           && slot.metadata.config_id == wave.config_id
           && slot.metadata.filter_profile == wave.filter_profile
           && slot.metadata.calibration_id == wave.calibration_id
           && slot.metadata.flags == frame_flags;
}

void CslpUdpReceiver::publish_assembly()
{
    if (assembling_index_ < 0) {
        return;
    }

    const int completed_index = assembling_index_;
    const uint32_t completed_frame_id = slots_[completed_index].metadata.frame_id;
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    if (latest_index_ >= 0) {
        slots_[latest_index_].state = SlotState::Free;
        stats_.latest_overwrites.fetch_add(1, std::memory_order_relaxed);
    }
    slots_[completed_index].state = SlotState::Latest;
    latest_index_ = completed_index;
    assembling_index_ = -1;
    xSemaphoreGive(slot_mutex_);

    last_completed_frame_id_ = completed_frame_id;
    have_completed_frame_ = true;
    const uint32_t completed =
        stats_.frames_completed.fetch_add(1, std::memory_order_relaxed) + 1;
    if (completed == 1 || completed % 100 == 0) {
        ESP_LOGI(kTag, "Published frame=%" PRIu32 " completed=%" PRIu32,
                 completed_frame_id, completed);
    }
}

void CslpUdpReceiver::invalidate_assembly(bool count_incomplete)
{
    if (assembling_index_ < 0) {
        return;
    }
    if (count_incomplete && slots_[assembling_index_].chunk_bitmap != 0) {
        stats_.incomplete_frames.fetch_add(1, std::memory_order_relaxed);
    }
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    slots_[assembling_index_].state = SlotState::Free;
    xSemaphoreGive(slot_mutex_);
    assembling_index_ = -1;
}

void CslpUdpReceiver::expire_assembly(uint64_t current_time_us)
{
    if (assembling_index_ >= 0
        && current_time_us - slots_[assembling_index_].assembly_started_us
               > kFrameAssemblyTimeoutUs) {
        reject_frame(slots_[assembling_index_].metadata.frame_id);
    }
}

void CslpUdpReceiver::reset_pending_frames()
{
    if (slot_mutex_ == nullptr) {
        return;
    }
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    for (FrameSlot &slot : slots_) {
        if (slot.state == SlotState::Assembling || slot.state == SlotState::Latest) {
            slot.state = SlotState::Free;
        }
    }
    assembling_index_ = -1;
    latest_index_ = -1;
    have_completed_frame_ = false;
    last_completed_frame_id_ = 0;
    have_observed_frame_ = false;
    newest_observed_frame_id_ = 0;
    rejected_frame_id_ = 0;
    xSemaphoreGive(slot_mutex_);
}

void CslpUdpReceiver::invalidate_active_stream()
{
    if (slot_mutex_ != nullptr) {
        xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    }
    active_session_id_.store(0, std::memory_order_release);
    active_config_id_.store(0, std::memory_order_release);
    active_stream_epoch_.fetch_add(1, std::memory_order_acq_rel);
    if (slot_mutex_ != nullptr) {
        xSemaphoreGive(slot_mutex_);
    }
}

bool CslpUdpReceiver::commit_active_stream(
    uint32_t session_id, uint32_t config_id,
    uint32_t expected_stream_epoch)
{
    if (session_id == 0 || config_id == 0 || slot_mutex_ == nullptr
        || network_events_ == nullptr) {
        return false;
    }

    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    const bool ip_ready =
        (xEventGroupGetBits(network_events_) & kIpReadyBit) != 0;
    const bool unchanged =
        active_stream_epoch_.load(std::memory_order_acquire)
            == expected_stream_epoch
        && active_session_id_.load(std::memory_order_acquire) == 0
        && active_config_id_.load(std::memory_order_acquire) == 0;
    if (ip_ready && unchanged) {
        active_config_id_.store(config_id, std::memory_order_release);
        active_session_id_.store(session_id, std::memory_order_release);
    }
    xSemaphoreGive(slot_mutex_);
    return ip_ready && unchanged;
}

void CslpUdpReceiver::reject_frame(uint32_t frame_id)
{
    if (!receiver_policy::rejection_targets_observed(
            frame_id, have_observed_frame_, newest_observed_frame_id_)) {
        return;
    }
    rejected_frame_id_ = frame_id;
    if (assembling_index_ >= 0
        && slots_[assembling_index_].metadata.frame_id == frame_id) {
        invalidate_assembly(true);
    }
}

bool CslpUdpReceiver::acquire_latest(const FrameCursor &after, FrameView *view)
{
    if (view == nullptr || slot_mutex_ == nullptr) {
        return false;
    }

    bool acquired = false;
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    if (latest_index_ >= 0) {
        FrameSlot &slot = slots_[latest_index_];
        const uint32_t stream_epoch_before =
            active_stream_epoch_.load(std::memory_order_acquire);
        const uint32_t active_session_id =
            active_session_id_.load(std::memory_order_acquire);
        const uint32_t active_config_id =
            active_config_id_.load(std::memory_order_acquire);
        const FrameCursor candidate = {
            slot.metadata.session_id,
            slot.metadata.frame_id,
        };
        if (receiver_policy::session_is_current(active_session_id,
                                                slot.metadata.session_id)
            && active_config_id != 0
            && active_config_id == slot.metadata.config_id
            && stream_epoch_before
                   == active_stream_epoch_.load(std::memory_order_acquire)
            && receiver_policy::cursor_allows(candidate, after)) {
            slot.state = SlotState::InUse;
            ++slot.lease_generation;
            if (slot.lease_generation == 0) {
                ++slot.lease_generation;
            }
            view->samples = slot.samples.data();
            view->sample_count = slot.metadata.sample_count;
            view->metadata = slot.metadata;
            view->slot_index = static_cast<uint8_t>(latest_index_);
            view->lease_generation = slot.lease_generation;
            view->stream_epoch = stream_epoch_before;
            latest_index_ = -1;
            acquired = true;
        }
    }
    xSemaphoreGive(slot_mutex_);

    if (acquired) {
        stats_.frames_acquired.fetch_add(1, std::memory_order_relaxed);
    }
    return acquired;
}

bool CslpUdpReceiver::frame_is_current(const FrameView &view) const
{
    if (view.slot_index >= slots_.size() || slot_mutex_ == nullptr) {
        return false;
    }

    bool lease_is_current = false;
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    const FrameSlot &slot = slots_[view.slot_index];
    lease_is_current = slot.state == SlotState::InUse
                       && slot.lease_generation == view.lease_generation
                       && slot.metadata.session_id == view.metadata.session_id
                       && slot.metadata.frame_id == view.metadata.frame_id;
    xSemaphoreGive(slot_mutex_);

    return lease_is_current
           && stream_is_current(view.metadata.session_id,
                                view.metadata.config_id,
                                view.stream_epoch);
}

bool CslpUdpReceiver::stream_is_current(uint32_t session_id,
                                        uint32_t config_id,
                                        uint32_t stream_epoch) const
{
    const uint32_t epoch_before =
        active_stream_epoch_.load(std::memory_order_acquire);
    if (epoch_before != stream_epoch) {
        return false;
    }
    const uint32_t active_session_id =
        active_session_id_.load(std::memory_order_acquire);
    const uint32_t active_config_id =
        active_config_id_.load(std::memory_order_acquire);
    const uint32_t epoch_after =
        active_stream_epoch_.load(std::memory_order_acquire);
    return epoch_before == epoch_after
           && receiver_policy::stream_identity_is_current(
               active_session_id, active_config_id, epoch_after,
               session_id, config_id, stream_epoch);
}

#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
void CslpUdpReceiver::synchronize_disable_push_test(const FrameView &view)
{
    if (!disable_test_in_progress_.load(std::memory_order_acquire)
        || view.metadata.config_id
               != disable_test_previous_config_id_.load(
                   std::memory_order_acquire)) {
        return;
    }

    ESP_LOGW(kTag,
             "Holding old-config analysis before publish: frame=%" PRIu32
             " config=%" PRIu32 " epoch=%" PRIu32,
             view.metadata.frame_id, view.metadata.config_id,
             view.stream_epoch);
    const uint64_t deadline = now_us() + 2000000U;
    while (!disable_test_reconfigured_.load(std::memory_order_acquire)
           && disable_test_in_progress_.load(std::memory_order_acquire)
           && now_us() < deadline) {
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    const bool reconfigured =
        disable_test_reconfigured_.load(std::memory_order_acquire);
    ESP_LOGI(kTag,
             "Old-config publish latch released: frame=%" PRIu32
             " reconfigured=%u",
             view.metadata.frame_id, static_cast<unsigned>(reconfigured));
    disable_test_in_progress_.store(false, std::memory_order_release);
}
#endif

void CslpUdpReceiver::release(FrameView *view)
{
    if (view == nullptr || view->slot_index >= slots_.size() || slot_mutex_ == nullptr) {
        return;
    }

    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    FrameSlot &slot = slots_[view->slot_index];
    if (slot.state == SlotState::InUse
        && slot.lease_generation == view->lease_generation) {
        slot.state = SlotState::Free;
    } else {
        ESP_LOGW(kTag, "Ignoring stale frame release token");
    }
    xSemaphoreGive(slot_mutex_);
    *view = {};
}

CslpUdpReceiver::Stats CslpUdpReceiver::stats() const
{
    return {
        .udp_packets_received = relaxed_load(stats_.udp_packets_received),
        .bad_source = relaxed_load(stats_.bad_source),
        .bad_magic = relaxed_load(stats_.bad_magic),
        .bad_version = relaxed_load(stats_.bad_version),
        .bad_length = relaxed_load(stats_.bad_length),
        .bad_session = relaxed_load(stats_.bad_session),
        .crc_failures = relaxed_load(stats_.crc_failures),
        .config_mismatches = relaxed_load(stats_.config_mismatches),
        .metadata_conflicts = relaxed_load(stats_.metadata_conflicts),
        .duplicate_chunks = relaxed_load(stats_.duplicate_chunks),
        .stale_chunks = relaxed_load(stats_.stale_chunks),
        .incomplete_frames = relaxed_load(stats_.incomplete_frames),
        .overrange_frames = relaxed_load(stats_.overrange_frames),
        .fifo_overflow_frames = relaxed_load(stats_.fifo_overflow_frames),
        .frames_completed = relaxed_load(stats_.frames_completed),
        .latest_overwrites = relaxed_load(stats_.latest_overwrites),
        .dropped_busy = relaxed_load(stats_.dropped_busy),
        .frames_acquired = relaxed_load(stats_.frames_acquired),
        .control_retries = relaxed_load(stats_.control_retries),
        .reconnects = relaxed_load(stats_.reconnects),
        .socket_open_failures = relaxed_load(stats_.socket_open_failures),
        .recv_fatal_errors = relaxed_load(stats_.recv_fatal_errors),
        .socket_close_failures = relaxed_load(stats_.socket_close_failures),
        .sessions_established = relaxed_load(stats_.sessions_established),
    };
}

bool CslpUdpReceiver::session_ready() const
{
    return active_session_id_.load(std::memory_order_acquire) != 0;
}

bool CslpUdpReceiver::started() const
{
    return start_state_.load(std::memory_order_acquire) == StartState::Started;
}

void CslpUdpReceiver::log_health()
{
    const Stats snapshot = stats();
    ESP_LOGI(kTag, "health/rx: packets=%" PRIu32 " source=%" PRIu32
             " magic=%" PRIu32 " version=%" PRIu32 " length=%" PRIu32
             " session=%" PRIu32 " crc=%" PRIu32,
             snapshot.udp_packets_received, snapshot.bad_source,
             snapshot.bad_magic, snapshot.bad_version, snapshot.bad_length,
             snapshot.bad_session, snapshot.crc_failures);
    ESP_LOGI(kTag, "health/frame: completed=%" PRIu32 " acquired=%" PRIu32
             " overwrite=%" PRIu32 " incomplete=%" PRIu32
             " duplicate=%" PRIu32 " stale=%" PRIu32 " busy=%" PRIu32,
             snapshot.frames_completed, snapshot.frames_acquired,
             snapshot.latest_overwrites, snapshot.incomplete_frames,
             snapshot.duplicate_chunks, snapshot.stale_chunks,
             snapshot.dropped_busy);
    ESP_LOGI(kTag, "health/reject: config=%" PRIu32 " metadata=%" PRIu32
             " overrange=%" PRIu32 " fifo=%" PRIu32 " retries=%" PRIu32
             " reconnects=%" PRIu32,
             snapshot.config_mismatches, snapshot.metadata_conflicts,
             snapshot.overrange_frames, snapshot.fifo_overflow_frames,
             snapshot.control_retries, snapshot.reconnects);
    ESP_LOGI(kTag, "health/socket: open_fail=%" PRIu32
             " recv_fatal=%" PRIu32 " close_fail=%" PRIu32
             " sessions=%" PRIu32,
             snapshot.socket_open_failures, snapshot.recv_fatal_errors,
             snapshot.socket_close_failures, snapshot.sessions_established);
}

uint64_t CslpUdpReceiver::now_us()
{
    return static_cast<uint64_t>(esp_timer_get_time());
}

}  // namespace cyclescope
