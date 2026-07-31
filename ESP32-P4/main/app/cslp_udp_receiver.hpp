#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>

#include "sdkconfig.h"

#include "esp_err.h"
#include "esp_eth.h"
#include "esp_eth_netif_glue.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include "cslp_protocol.hpp"
#include "cslp_receiver_policy.hpp"

namespace cyclescope {

class CslpUdpReceiver;
namespace startup_fault_test {
bool run_receiver_startup_fault_matrix(CslpUdpReceiver &receiver);
}
namespace runtime_fault_test {
bool run_receiver_runtime_fault_matrix(CslpUdpReceiver &receiver);
}

class CslpUdpReceiver {
public:
    static constexpr uint32_t kSampleRateHz = 4062500;
    static constexpr size_t kFrameSampleCount = 8192;
    static constexpr uint32_t kFramePeriodUs = 50000;
    static constexpr uint16_t kFilterProfile = 1;
    static constexpr size_t kSamplesPerFullChunk = 700;
    static constexpr size_t kChunkCount = 12;

    using FrameCursor = receiver_policy::FrameCursor;

    struct FrameMetadata {
        uint32_t session_id;
        uint32_t frame_id;
        uint64_t timestamp_us;
        uint32_t sample_rate_hz;
        uint32_t sample_count;
        uint32_t scale_uv_per_lsb;
        int32_t offset_uv;
        uint32_t config_id;
        uint16_t filter_profile;
        uint16_t calibration_id;
        uint16_t flags;
    };

    struct FrameView {
        const int16_t *samples = nullptr;
        size_t sample_count = 0;
        FrameMetadata metadata{};
        uint8_t slot_index = 0xFF;
        uint32_t lease_generation = 0;
        uint32_t stream_epoch = 0;

        FrameCursor cursor() const
        {
            return {metadata.session_id, metadata.frame_id};
        }
    };

    struct Stats {
        uint32_t udp_packets_received;
        uint32_t bad_source;
        uint32_t bad_magic;
        uint32_t bad_version;
        uint32_t bad_length;
        uint32_t bad_session;
        uint32_t crc_failures;
        uint32_t config_mismatches;
        uint32_t metadata_conflicts;
        uint32_t duplicate_chunks;
        uint32_t stale_chunks;
        uint32_t incomplete_frames;
        uint32_t overrange_frames;
        uint32_t fifo_overflow_frames;
        uint32_t frames_completed;
        uint32_t latest_overwrites;
        uint32_t dropped_busy;
        uint32_t frames_acquired;
        uint32_t control_retries;
        uint32_t reconnects;
        uint32_t socket_open_failures;
        uint32_t recv_fatal_errors;
        uint32_t socket_close_failures;
        uint32_t sessions_established;
    };

    esp_err_t start();
    bool acquire_latest(const FrameCursor &after, FrameView *view);
    // Check immediately before publishing results derived from an acquired frame.
    bool frame_is_current(const FrameView &view) const;
    bool stream_is_current(uint32_t session_id, uint32_t config_id,
                           uint32_t stream_epoch) const;
#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
    void synchronize_disable_push_test(const FrameView &view);
#endif
    void release(FrameView *view);
    Stats stats() const;
    bool session_ready() const;
    bool started() const;

private:
    friend bool startup_fault_test::run_receiver_startup_fault_matrix(
        CslpUdpReceiver &receiver);
    friend bool runtime_fault_test::run_receiver_runtime_fault_matrix(
        CslpUdpReceiver &receiver);

    enum class SlotState : uint8_t {
        Free,
        Assembling,
        Latest,
        InUse,
    };

    enum class ReceiveResult {
        Timeout,
        Valid,
        Fatal,
    };

    enum class StartState : uint8_t {
        Stopped,
        Starting,
        Started,
        Failed,
    };

    struct FrameSlot {
        std::array<int16_t, kFrameSampleCount> samples{};
        FrameMetadata metadata{};
        std::array<uint32_t, kChunkCount> chunk_crc{};
        uint16_t chunk_bitmap = 0;
        uint64_t assembly_started_us = 0;
        uint32_t lease_generation = 0;
        SlotState state = SlotState::Free;
    };

    struct AtomicStats {
        std::atomic<uint32_t> udp_packets_received{0};
        std::atomic<uint32_t> bad_source{0};
        std::atomic<uint32_t> bad_magic{0};
        std::atomic<uint32_t> bad_version{0};
        std::atomic<uint32_t> bad_length{0};
        std::atomic<uint32_t> bad_session{0};
        std::atomic<uint32_t> crc_failures{0};
        std::atomic<uint32_t> config_mismatches{0};
        std::atomic<uint32_t> metadata_conflicts{0};
        std::atomic<uint32_t> duplicate_chunks{0};
        std::atomic<uint32_t> stale_chunks{0};
        std::atomic<uint32_t> incomplete_frames{0};
        std::atomic<uint32_t> overrange_frames{0};
        std::atomic<uint32_t> fifo_overflow_frames{0};
        std::atomic<uint32_t> frames_completed{0};
        std::atomic<uint32_t> latest_overwrites{0};
        std::atomic<uint32_t> dropped_busy{0};
        std::atomic<uint32_t> frames_acquired{0};
        std::atomic<uint32_t> control_retries{0};
        std::atomic<uint32_t> reconnects{0};
        std::atomic<uint32_t> socket_open_failures{0};
        std::atomic<uint32_t> recv_fatal_errors{0};
        std::atomic<uint32_t> socket_close_failures{0};
        std::atomic<uint32_t> sessions_established{0};
    };

    static void receiver_task(void *context);
    static void network_event_handler(void *context, esp_event_base_t event_base,
                                      int32_t event_id, void *event_data);

    esp_err_t initialize_ethernet();
    esp_err_t configure_static_ip();
    bool rollback_start();
    void task_main();
    bool open_socket();
    void close_socket();
    bool establish_session();
    bool run_hello();
    bool run_config(uint32_t *negotiated_config_id);
    bool run_enable_push();
    bool run_disable_push();
    bool run_disable_push_reconfigure_test();
    bool transact(size_t request_length, cslp::MessageType expected_response,
                  uint32_t request_sequence, cslp::CommonHeader *response);
    ReceiveResult receive_valid_datagram(cslp::CommonHeader *common, size_t *length);
    void dispatch_datagram(const cslp::CommonHeader &common, size_t length);
    void handle_status(const cslp::CommonHeader &common, size_t length);
    void handle_error(const cslp::CommonHeader &common, size_t length);
    void handle_wave(const cslp::CommonHeader &common, size_t length);

    bool validate_wave(const cslp::CommonHeader &common, const cslp::WaveHeader &wave);
    int ensure_assembly_slot(const cslp::CommonHeader &common, const cslp::WaveHeader &wave);
    void initialize_assembly(FrameSlot *slot, const cslp::CommonHeader &common,
                             const cslp::WaveHeader &wave);
    bool shared_metadata_matches(const FrameSlot &slot, const cslp::CommonHeader &common,
                                 const cslp::WaveHeader &wave) const;
    void publish_assembly();
    void invalidate_assembly(bool count_incomplete);
    void expire_assembly(uint64_t now_us);
    void reset_pending_frames();
    void invalidate_active_stream();
    bool commit_active_stream(uint32_t session_id, uint32_t config_id,
                              uint32_t expected_stream_epoch);
    void reject_frame(uint32_t frame_id);
    void record_parse_error(cslp::ParseError error);
    void log_health();

    size_t prepare_request(cslp::MessageType type, uint32_t sequence, uint16_t payload_bytes);
    uint32_t next_control_sequence();
    static uint64_t now_us();

    static constexpr EventBits_t kIpReadyBit = BIT0;
    static constexpr EventBits_t kEthernetStoppedBit = BIT1;
    static constexpr uint16_t kLocalPort = 50001;
    static constexpr uint16_t kFpgaPort = 50000;
    static constexpr uint16_t kAllChunksMask = (1U << kChunkCount) - 1U;

    std::array<FrameSlot, 3> slots_{};
    // One sentinel byte exposes datagrams that violate the v0.1 MTU limit.
    std::array<uint8_t, cslp::kMaxUdpPayloadBytes + 1> rx_buffer_{};
    std::array<uint8_t, 64> tx_buffer_{};
    AtomicStats stats_{};

    SemaphoreHandle_t slot_mutex_ = nullptr;
    EventGroupHandle_t network_events_ = nullptr;
    TaskHandle_t receiver_task_handle_ = nullptr;
    esp_eth_handle_t *eth_handles_ = nullptr;
    uint8_t eth_handle_count_ = 0;
    esp_netif_t *eth_netif_ = nullptr;
    esp_eth_netif_glue_handle_t eth_glue_ = nullptr;
    esp_event_handler_instance_t eth_event_instance_ = nullptr;
    esp_event_handler_instance_t ip_event_instance_ = nullptr;
    sockaddr_in fpga_address_{};
    int socket_fd_ = -1;

    std::atomic<StartState> start_state_{StartState::Stopped};
    std::atomic<uint32_t> active_session_id_{0};
    std::atomic<uint32_t> active_config_id_{0};
    std::atomic<uint32_t> active_stream_epoch_{1};
    std::atomic<bool> network_callbacks_enabled_{false};
    bool ethernet_start_attempted_ = false;
    int assembling_index_ = -1;
    int latest_index_ = -1;
    uint32_t last_completed_frame_id_ = 0;
    bool have_completed_frame_ = false;
    uint32_t newest_observed_frame_id_ = 0;
    bool have_observed_frame_ = false;
    uint32_t rejected_frame_id_ = 0;
    uint32_t session_id_ = 0;
    uint32_t control_sequence_ = 0;
    uint32_t device_boot_id_ = 0;
    uint64_t last_stream_message_us_ = 0;
    uint64_t last_health_log_us_ = 0;
#if CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST
    std::atomic<bool> disable_test_in_progress_{false};
    std::atomic<bool> disable_test_reconfigured_{false};
    std::atomic<uint32_t> disable_test_previous_config_id_{0};
#endif
};

CslpUdpReceiver &cslp_udp_receiver();

}  // namespace cyclescope
