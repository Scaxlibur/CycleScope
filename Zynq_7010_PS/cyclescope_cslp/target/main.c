#include "cslp_control.h"
#include "cslp_dma_zynq.h"
#include "cslp_protocol.h"
#include "platform.h"
#include "platform_config.h"

#include "lwip/init.h"
#include "lwip/inet.h"
#include "lwip/ip_addr.h"
#include "lwip/pbuf.h"
#include "lwip/priv/tcp_priv.h"
#include "lwip/udp.h"
#include "netif/xadapter.h"
#include "xil_printf.h"
#include "xstatus.h"
#include "xiltimer.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define CSLP_LOCAL_PORT 50000U
#define CSLP_REMOTE_PORT 50001U
#define CSLP_CHUNK_SPACING_US 150ULL
#define CSLP_STATUS_PERIOD_US 500000ULL
#define CSLP_PL_STATUS_OTR 0x00000004U
#define CSLP_PL_STATUS_OVERFLOW 0x00000008U

typedef struct {
    bool active;
    cslp_dma_frame_view_t frame;
    cslp_wave_metadata_t metadata;
    uint16_t next_chunk;
    uint64_t next_chunk_us;
} cslp_wave_tx_t;

typedef struct {
    struct udp_pcb *udp;
    cslp_control_t control;
    ip_addr_t remote_address;
    bool remote_valid;
    bool test_pattern;
    cslp_wave_tx_t wave;
    uint32_t wave_sequence;
    uint32_t status_sequence;
    uint64_t next_frame_us;
    uint64_t next_status_us;
    uint32_t frames_sent;
    uint32_t packets_sent;
    uint32_t frames_dropped;
    uint32_t adc_overrange_frames;
    uint32_t fifo_overflow_frames;
    uint32_t last_frame_id;
} cslp_app_t;

static struct netif server_netif;
static cslp_app_t app;

extern volatile int TcpFastTmrFlag;
extern volatile int TcpSlowTmrFlag;

static uint64_t now_us(void)
{
    XTime ticks;
    XTime_GetTime(&ticks);
    return ((uint64_t)ticks * 1000000ULL) / (uint64_t)COUNTS_PER_SECOND;
}

static uint32_t next_sequence(uint32_t *sequence)
{
    uint32_t current = *sequence;
    *sequence = current + 1U;
    return current;
}

static bool source_is_expected(const ip_addr_t *address)
{
    ip_addr_t expected;
    IP4_ADDR(ip_2_ip4(&expected), 192, 168, 10, 3);
    IP_SET_TYPE_VAL(expected, IPADDR_TYPE_V4);
    return ip_addr_cmp(address, &expected);
}

static bool send_datagram(const ip_addr_t *address,
                          uint16_t port,
                          const uint8_t *bytes,
                          size_t length)
{
    struct pbuf *packet;
    err_t error;

    if (length == 0U || length > CSLP_MAX_UDP_PAYLOAD)
        return false;
    packet = pbuf_alloc(PBUF_TRANSPORT, (u16_t)length, PBUF_RAM);
    if (packet == NULL)
        return false;
    error = pbuf_take(packet, bytes, (u16_t)length);
    if (error == ERR_OK)
        error = udp_sendto(app.udp, packet, address, port);
    pbuf_free(packet);
    return error == ERR_OK;
}

static void abort_wave(void)
{
    if (app.wave.active)
        cslp_dma_zynq_release_frame(app.wave.frame.slot);
    memset(&app.wave, 0, sizeof(app.wave));
}

static void apply_capture_state(bool was_enabled)
{
    bool enabled = cslp_control_push_enabled(&app.control);

    if (enabled == was_enabled)
        return;
    if (enabled) {
        cslp_dma_zynq_set_enabled(true);
        cslp_dma_zynq_set_capture(true);
        app.next_frame_us = now_us();
    } else {
        /* Stop new PL freezes first; an already-started UDP frame may finish. */
        cslp_dma_zynq_set_capture(false);
        cslp_dma_zynq_set_enabled(false);
    }
}

static void receive_control(void *argument,
                            struct udp_pcb *pcb,
                            struct pbuf *packet,
                            const ip_addr_t *address,
                            u16_t port)
{
    uint8_t request[CSLP_MAX_UDP_PAYLOAD];
    uint8_t response[CSLP_CONTROL_MAX_RESPONSE_BYTES];
    cslp_header_t header;
    cslp_control_result_t result;
    size_t response_bytes = 0U;
    size_t request_bytes;
    uint32_t old_session;
    bool was_enabled;

    (void)argument;
    (void)pcb;
    if (packet == NULL)
        return;
    request_bytes = packet->tot_len;
    if (request_bytes > sizeof(request) ||
        pbuf_copy_partial(packet, request, (u16_t)request_bytes, 0U) !=
            request_bytes) {
        pbuf_free(packet);
        return;
    }
    pbuf_free(packet);

    if (cslp_parse_datagram(request, request_bytes, &header) !=
            CSLP_PARSE_OK ||
        !source_is_expected(address))
        return;
    if (header.message_type != CSLP_MSG_HELLO &&
        (!app.remote_valid || port != app.control.data_port ||
         !ip_addr_cmp(address, &app.remote_address)))
        return;

    old_session = app.control.session_id;
    was_enabled = cslp_control_push_enabled(&app.control);
    result = cslp_control_handle(
        &app.control, request, request_bytes, port, now_us(),
        app.wave.active, response, sizeof(response), &response_bytes);

    if (app.control.session_id != old_session &&
        app.control.session_id == header.session_id) {
        abort_wave();
        cslp_dma_zynq_set_capture(false);
        cslp_dma_zynq_set_enabled(false);
        cslp_dma_zynq_discard_ready();
        app.remote_address = *address;
        app.remote_valid = true;
        app.next_status_us = now_us() + CSLP_STATUS_PERIOD_US;
    }
    apply_capture_state(was_enabled);
    if (result == CSLP_CONTROL_RESPONSE)
        (void)send_datagram(address, port, response, response_bytes);
}

static void begin_wave_if_ready(uint64_t current_us)
{
    uint16_t flags = CSLP_FLAG_FILTERED;

    if (app.wave.active || !cslp_control_push_enabled(&app.control) ||
        current_us < app.next_frame_us)
        return;
    if (cslp_dma_zynq_acquire_frame(&app.wave.frame) != XST_SUCCESS)
        return;

    if ((app.wave.frame.status_word & CSLP_PL_STATUS_OTR) != 0U) {
        flags |= CSLP_FLAG_ADC_OVERRANGE;
        ++app.adc_overrange_frames;
    }
    if ((app.wave.frame.status_word & CSLP_PL_STATUS_OVERFLOW) != 0U) {
        flags |= CSLP_FLAG_FIFO_OVERFLOW;
        ++app.fifo_overflow_frames;
    }
    if (app.test_pattern)
        flags |= CSLP_FLAG_TEST_PATTERN;

    app.wave.metadata.frame_id = app.wave.frame.frame_id;
    app.wave.metadata.sample_rate_hz = app.control.config.sample_rate_hz;
    app.wave.metadata.frame_sample_count =
        app.control.config.frame_sample_count;
    app.wave.metadata.scale_uv_per_lsb = 488U;
    app.wave.metadata.offset_uv = 0;
    app.wave.metadata.config_id = app.control.active_config_id;
    app.wave.metadata.filter_profile = app.control.config.filter_profile;
    app.wave.metadata.calibration_id = 0U;
    app.wave.metadata.frame_flags = flags;
    app.wave.metadata.sample_format = app.control.config.sample_format;
    app.wave.metadata.channel_count = app.control.config.channel_count;
    app.wave.next_chunk = 0U;
    app.wave.next_chunk_us = current_us;
    app.wave.active = true;
    app.last_frame_id = app.wave.frame.frame_id;
    app.next_frame_us = current_us + CSLP_PROFILE_FRAME_PERIOD_US;
}

static void service_wave(uint64_t current_us)
{
    uint8_t datagram[CSLP_MAX_UDP_PAYLOAD];
    size_t datagram_bytes;

    begin_wave_if_ready(current_us);
    if (!app.wave.active || current_us < app.wave.next_chunk_us)
        return;

    datagram_bytes = cslp_build_wave_chunk(
        datagram, sizeof(datagram), app.control.session_id,
        next_sequence(&app.wave_sequence), app.wave.frame.timestamp_us,
        &app.wave.metadata, app.wave.frame.samples, app.wave.next_chunk);
    if (datagram_bytes == 0U ||
        !send_datagram(&app.remote_address, app.control.data_port, datagram,
                       datagram_bytes)) {
        ++app.frames_dropped;
        abort_wave();
        return;
    }

    ++app.packets_sent;
    ++app.wave.next_chunk;
    if (app.wave.next_chunk == CSLP_PROFILE_CHUNK_COUNT) {
        ++app.frames_sent;
        cslp_dma_zynq_release_frame(app.wave.frame.slot);
        memset(&app.wave, 0, sizeof(app.wave));
    } else {
        app.wave.next_chunk_us = current_us + CSLP_CHUNK_SPACING_US;
    }
}

static void service_disable_ack(uint64_t current_us)
{
    uint8_t response[CSLP_CONTROL_MAX_RESPONSE_BYTES];
    size_t response_bytes;

    if (cslp_control_poll(&app.control, current_us, app.wave.active, response,
                          sizeof(response), &response_bytes) ==
        CSLP_CONTROL_RESPONSE) {
        cslp_dma_zynq_discard_ready();
        (void)send_datagram(&app.remote_address, app.control.data_port, response,
                            response_bytes);
    }
}

static void service_status(uint64_t current_us)
{
    uint8_t datagram[CSLP_COMMON_HEADER_BYTES + 40U];
    cslp_status_snapshot_t snapshot;
    cslp_dma_stats_t dma_stats;
    size_t datagram_bytes;

    if (!cslp_control_has_session(&app.control) || !app.remote_valid ||
        current_us < app.next_status_us)
        return;
    app.next_status_us = current_us + CSLP_STATUS_PERIOD_US;
    cslp_dma_zynq_get_stats(&dma_stats);
    memset(&snapshot, 0, sizeof(snapshot));
    snapshot.device_state = (uint16_t)app.control.state;
    snapshot.last_error = (uint16_t)app.control.last_error;
    snapshot.active_config_id = app.control.active_config_id;
    snapshot.last_frame_id = app.last_frame_id;
    snapshot.frames_sent = app.frames_sent;
    snapshot.packets_sent = app.packets_sent;
    snapshot.adc_overrange_frames = app.adc_overrange_frames;
    snapshot.fifo_overflow_frames = app.fifo_overflow_frames + dma_stats.errors;
    snapshot.frames_dropped = app.frames_dropped + dma_stats.dropped_ready +
                              dma_stats.submit_failures +
                              dma_stats.metadata_failures;
    snapshot.uptime_ms = (uint32_t)(current_us / 1000ULL);
    datagram_bytes = cslp_build_status(
        datagram, sizeof(datagram), app.control.session_id,
        next_sequence(&app.status_sequence), current_us, &snapshot);
    (void)send_datagram(&app.remote_address, app.control.data_port, datagram,
                        datagram_bytes);
}

static int start_application(void)
{
    err_t error;
    uint32_t boot_id;

    memset(&app, 0, sizeof(app));
    app.wave_sequence = 1U;
    app.status_sequence = 1U;
    boot_id = (uint32_t)now_us() ^ 0x43534c50U;
    cslp_control_init(&app.control, boot_id);
    if (cslp_dma_zynq_init() != XST_SUCCESS)
        return XST_FAILURE;
    cslp_dma_zynq_set_test_pattern(false);
    cslp_dma_zynq_clear_pl_stats();

    app.udp = udp_new_ip_type(IPADDR_TYPE_V4);
    if (app.udp == NULL)
        return XST_FAILURE;
    error = udp_bind(app.udp, IP_ANY_TYPE, CSLP_LOCAL_PORT);
    if (error != ERR_OK) {
        udp_remove(app.udp);
        app.udp = NULL;
        return XST_FAILURE;
    }
    udp_recv(app.udp, receive_control, NULL);
    xil_printf("CSLP control UDP %u, peer 192.168.10.3:%u\r\n",
               CSLP_LOCAL_PORT, CSLP_REMOTE_PORT);
    return XST_SUCCESS;
}

int main(void)
{
    unsigned char mac_address[] = {0x02, 0x43, 0x53, 0x4c, 0x50, 0x01};
    ip_addr_t local_address;
    ip_addr_t netmask;
    ip_addr_t gateway;

    init_platform();
    lwip_init();
    IP4_ADDR(ip_2_ip4(&local_address), 192, 168, 10, 2);
    IP4_ADDR(ip_2_ip4(&netmask), 255, 255, 255, 0);
    IP4_ADDR(ip_2_ip4(&gateway), 0, 0, 0, 0);
    IP_SET_TYPE_VAL(local_address, IPADDR_TYPE_V4);
    IP_SET_TYPE_VAL(netmask, IPADDR_TYPE_V4);
    IP_SET_TYPE_VAL(gateway, IPADDR_TYPE_V4);

    if (!xemac_add(&server_netif, &local_address, &netmask, &gateway,
                   mac_address, PLATFORM_EMAC_BASEADDR)) {
        xil_printf("CSLP: xemac_add failed\r\n");
        return XST_FAILURE;
    }
    netif_set_default(&server_netif);
#ifndef SDT
    platform_enable_interrupts();
#endif
    netif_set_up(&server_netif);
    if (start_application() != XST_SUCCESS) {
        xil_printf("CSLP: initialization failed\r\n");
        return XST_FAILURE;
    }

    xil_printf("CycleScope CSLP 192.168.10.2/24 ready\r\n");
    while (1) {
        uint64_t current_us;
        if (TcpFastTmrFlag) {
            tcp_fasttmr();
            TcpFastTmrFlag = 0;
        }
        if (TcpSlowTmrFlag) {
            tcp_slowtmr();
            TcpSlowTmrFlag = 0;
        }
        xemacif_input(&server_netif);
        current_us = now_us();
        service_wave(current_us);
        service_disable_ack(current_us);
        service_status(current_us);
    }
}
