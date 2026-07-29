#include "cslp_udp_receiver.hpp"

#include <cerrno>
#include <cstring>
#include <inttypes.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "ethernet_init.h"
#include "lwip/inet.h"

namespace cyclescope {
namespace {

constexpr char kTag[] = "cslp_rx";
constexpr char kLocalIp[] = "192.168.10.3";
constexpr char kNetmask[] = "255.255.255.0";
constexpr char kFpgaIp[] = "192.168.10.2";
constexpr int kSocketReceiveBufferBytes = 64 * 1024;
constexpr int kSocketTimeoutMs = 20;
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
    if (started_.exchange(true, std::memory_order_acq_rel)) {
        return ESP_OK;
    }
    if (!cslp::protocol_self_test()) {
        ESP_LOGE(kTag, "CSLP golden packet self-test failed");
        return ESP_FAIL;
    }

    slot_mutex_ = xSemaphoreCreateMutex();
    network_events_ = xEventGroupCreate();
    if (slot_mutex_ == nullptr || network_events_ == nullptr) {
        ESP_LOGE(kTag, "Unable to allocate receiver synchronization primitives");
        return ESP_ERR_NO_MEM;
    }

    const esp_err_t network_error = initialize_ethernet();
    if (network_error != ESP_OK) {
        ESP_LOGE(kTag, "Ethernet initialization failed: %s", esp_err_to_name(network_error));
        return network_error;
    }

    if (xTaskCreatePinnedToCore(receiver_task, "cslp_udp_rx", kReceiverTaskStackBytes, this,
                                kReceiverTaskPriority, &receiver_task_handle_, kReceiverCore) != pdPASS) {
        ESP_LOGE(kTag, "Unable to create CSLP receiver task");
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(kTag, "CSLP v1 receiver ready on Core %d; golden packet PASS", kReceiverCore);
    return ESP_OK;
}

esp_err_t CslpUdpReceiver::initialize_ethernet()
{
    esp_err_t error = esp_netif_init();
    if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) {
        return error;
    }
    error = esp_event_loop_create_default();
    if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) {
        return error;
    }

    error = ethernet_init_all(&eth_handles_, &eth_handle_count_);
    if (error != ESP_OK) {
        return error;
    }
    if (eth_handle_count_ == 0 || eth_handles_ == nullptr) {
        return ESP_ERR_NOT_FOUND;
    }
    if (eth_handle_count_ > 1) {
        ESP_LOGW(kTag, "%u Ethernet devices detected; CSLP uses the first",
                 static_cast<unsigned>(eth_handle_count_));
    }

    esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
    eth_netif_ = esp_netif_new(&netif_config);
    if (eth_netif_ == nullptr) {
        return ESP_ERR_NO_MEM;
    }

    eth_glue_ = esp_eth_new_netif_glue(eth_handles_[0]);
    if (eth_glue_ == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    error = esp_netif_attach(eth_netif_, eth_glue_);
    if (error != ESP_OK) {
        return error;
    }

    error = esp_event_handler_instance_register(ETH_EVENT, ESP_EVENT_ANY_ID,
                                                network_event_handler, this,
                                                &eth_event_instance_);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_ETH_GOT_IP,
                                                network_event_handler, this,
                                                &ip_event_instance_);
    if (error != ESP_OK) {
        return error;
    }
    return esp_eth_start(eth_handles_[0]);
}

esp_err_t CslpUdpReceiver::configure_static_ip()
{
    const esp_err_t dhcp_error = esp_netif_dhcpc_stop(eth_netif_);
    if (dhcp_error != ESP_OK && dhcp_error != ESP_ERR_ESP_NETIF_DHCP_ALREADY_STOPPED) {
        return dhcp_error;
    }

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
        if (event_id == ETHERNET_EVENT_CONNECTED) {
            const esp_err_t error = receiver->configure_static_ip();
            if (error != ESP_OK) {
                ESP_LOGE(kTag, "Unable to configure static IPv4: %s", esp_err_to_name(error));
            }
        } else if (event_id == ETHERNET_EVENT_DISCONNECTED || event_id == ETHERNET_EVENT_STOP) {
            xEventGroupClearBits(receiver->network_events_, kIpReadyBit);
            receiver->session_ready_.store(false, std::memory_order_release);
            ESP_LOGW(kTag, "Ethernet link down");
        }
        return;
    }

    if (event_base == IP_EVENT && event_id == IP_EVENT_ETH_GOT_IP) {
        const auto *event = static_cast<const ip_event_got_ip_t *>(event_data);
        if (event->esp_netif != receiver->eth_netif_) {
            return;
        }
        xEventGroupSetBits(receiver->network_events_, kIpReadyBit);
        ESP_LOGI(kTag, "Ethernet static IPv4 ready: " IPSTR, IP2STR(&event->ip_info.ip));
    }
}

void CslpUdpReceiver::receiver_task(void *context)
{
    static_cast<CslpUdpReceiver *>(context)->task_main();
}

void CslpUdpReceiver::task_main()
{
    bool attempted_session = false;
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
            session_ready_.store(false, std::memory_order_release);
            reset_pending_frames();
            close_socket();
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

        while ((xEventGroupGetBits(network_events_) & kIpReadyBit) != 0
               && session_ready_.load(std::memory_order_acquire)) {
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

        session_ready_.store(false, std::memory_order_release);
        reset_pending_frames();
        close_socket();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

bool CslpUdpReceiver::open_socket()
{
    close_socket();
    socket_fd_ = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (socket_fd_ < 0) {
        ESP_LOGE(kTag, "socket() failed: errno=%d", errno);
        return false;
    }

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
    if (setsockopt(socket_fd_, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) {
        ESP_LOGE(kTag, "SO_RCVTIMEO failed: errno=%d", errno);
        close_socket();
        return false;
    }

    sockaddr_in local_address{};
    local_address.sin_family = AF_INET;
    local_address.sin_port = htons(kLocalPort);
    local_address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(socket_fd_, reinterpret_cast<sockaddr *>(&local_address),
             sizeof(local_address)) != 0) {
        ESP_LOGE(kTag, "bind(%u) failed: errno=%d",
                 static_cast<unsigned>(kLocalPort), errno);
        close_socket();
        return false;
    }

    std::memset(&fpga_address_, 0, sizeof(fpga_address_));
    fpga_address_.sin_family = AF_INET;
    fpga_address_.sin_port = htons(kFpgaPort);
    if (inet_pton(AF_INET, kFpgaIp, &fpga_address_.sin_addr) != 1) {
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
        shutdown(socket_fd_, SHUT_RDWR);
        close(socket_fd_);
        socket_fd_ = -1;
    }
}

bool CslpUdpReceiver::establish_session()
{
    session_ready_.store(false, std::memory_order_release);
    reset_pending_frames();
    active_config_id_ = 0;
    device_boot_id_ = 0;
    session_id_ = esp_random();
    if (session_id_ == 0) {
        session_id_ = 1;
    }
    control_sequence_ = esp_random();
    if (!run_hello() || !run_config() || !run_enable_push()) {
        ESP_LOGW(kTag, "CSLP session 0x%08" PRIX32 " handshake failed", session_id_);
        return false;
    }

    last_stream_message_us_ = now_us();
    session_ready_.store(true, std::memory_order_release);
    ESP_LOGI(kTag, "CSLP session ready: session=0x%08" PRIX32
             " boot=%" PRIu32 " config=%" PRIu32,
             session_id_, device_boot_id_, active_config_id_);
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

bool CslpUdpReceiver::run_config()
{
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
    active_config_id_ = config_id;
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
    const ssize_t received = recvfrom(socket_fd_, rx_buffer_.data(), rx_buffer_.size(), 0,
                                      reinterpret_cast<sockaddr *>(&source), &source_length);
    if (received < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            return ReceiveResult::Timeout;
        }
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
    if (active_config_id_ != 0 && active_config_id != active_config_id_) {
        stats_.config_mismatches.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGW(kTag, "STATUS config changed from %" PRIu32 " to %" PRIu32,
                 active_config_id_, active_config_id);
        session_ready_.store(false, std::memory_order_release);
        return;
    }
    last_stream_message_us_ = now_us();
    const bool session_active = session_ready_.load(std::memory_order_acquire);
    if (device_state != 2 || last_error != 0) {
        ESP_LOGW(kTag, "FPGA STATUS state=%u last_error=%u",
                 static_cast<unsigned>(device_state), static_cast<unsigned>(last_error));
    }
    if (session_active && device_state != 2) {
        ESP_LOGW(kTag, "FPGA left PUSH_ENABLED; starting a new session");
        session_ready_.store(false, std::memory_order_release);
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
    if (active_config_id_ == 0 || wave.config_id != active_config_id_
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
    cslp::WaveHeader wave{};
    if (!cslp::decode_wave_header(rx_buffer_.data(), length, common, &wave)) {
        stats_.bad_length.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    if (!validate_wave(common, wave)) {
        reject_frame(wave.frame_id);
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
        if (rejected_frame_id_ != 0
            && cslp::sequence_is_newer(wave.frame_id, rejected_frame_id_)) {
            rejected_frame_id_ = 0;
        }
    }
    if (wave.frame_id == rejected_frame_id_) {
        return;
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
        if (rejected_frame_id_ != 0
            && cslp::sequence_is_newer(wave.frame_id, rejected_frame_id_)) {
            rejected_frame_id_ = 0;
        }
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
    if (rejected_frame_id_ != 0
        && cslp::sequence_is_newer(wave.frame_id, rejected_frame_id_)) {
        rejected_frame_id_ = 0;
    }
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
        rejected_frame_id_ = slots_[assembling_index_].metadata.frame_id;
        invalidate_assembly(true);
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

void CslpUdpReceiver::reject_frame(uint32_t frame_id)
{
    if (frame_id == 0 || frame_id == rejected_frame_id_) {
        return;
    }
    rejected_frame_id_ = frame_id;
    if (assembling_index_ >= 0
        && slots_[assembling_index_].metadata.frame_id == frame_id) {
        invalidate_assembly(true);
    }
}

bool CslpUdpReceiver::acquire_latest(uint32_t after_frame_id, FrameView *view)
{
    if (view == nullptr || slot_mutex_ == nullptr) {
        return false;
    }

    bool acquired = false;
    xSemaphoreTake(slot_mutex_, portMAX_DELAY);
    if (latest_index_ >= 0) {
        FrameSlot &slot = slots_[latest_index_];
        if (after_frame_id == 0
            || cslp::sequence_is_newer(slot.metadata.frame_id, after_frame_id)) {
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
    };
}

bool CslpUdpReceiver::session_ready() const
{
    return session_ready_.load(std::memory_order_acquire);
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
}

uint64_t CslpUdpReceiver::now_us()
{
    return static_cast<uint64_t>(esp_timer_get_time());
}

}  // namespace cyclescope
