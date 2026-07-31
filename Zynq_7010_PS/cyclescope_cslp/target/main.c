#include "cslp_control.h"
#include "cslp_dma_zynq.h"
#include "cslp_protocol.h"
#include "cslp_time.h"
#include "platform.h"
#include "platform_config.h"

#include "lwip/init.h"
#include "lwip/inet.h"
#include "lwip/ip_addr.h"
#include "lwip/pbuf.h"
#include "lwip/priv/tcp_priv.h"
#include "lwip/udp.h"
#include "netif/xadapter.h"
#include "xemacps_hw.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "xstatus.h"
#include "xiltimer.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define CSLP_LOCAL_PORT 50000U
#define CSLP_REMOTE_PORT 50001U
#define CSLP_CHUNK_SPACING_US 500ULL
#define CSLP_STATUS_PERIOD_US 500000ULL
#define CSLP_IPV4_HEADER_BYTES 20U
#define CSLP_UDP_HEADER_BYTES 8U
#define CSLP_REQUIRED_NETIF_MTU \
    (CSLP_MAX_UDP_PAYLOAD + CSLP_IPV4_HEADER_BYTES + CSLP_UDP_HEADER_BYTES)
#define CSLP_PL_STATUS_OTR 0x00000004U
#define CSLP_PL_STATUS_OVERFLOW 0x00000008U
#define CSLP_PL_STATUS_FRAMES_DROPPED_MASK 0x0000fff0U
#define CSLP_PL_STATUS_FRAMES_DROPPED_SHIFT 4U
#define CSLP_SLCR_GEM0_CLK_CTRL 0xF8000140U
#define CSLP_GEM_CLK_SOURCE_SHIFT 4U
#define CSLP_GEM_CLK_DIVISOR0_SHIFT 8U
#define CSLP_GEM_CLK_DIVISOR1_SHIFT 20U
#define CSLP_GEM_CLK_SOURCE_MASK 0x7U
#define CSLP_GEM_CLK_DIVISOR_MASK 0x3FU
#define CSLP_GEM_CLK_IO_PLL 0U
#define CSLP_GEM_CLK_DIVISOR0_100M 8U
#define CSLP_GEM_CLK_DIVISOR1_100M 5U
#define CSLP_GEM_CLK_ACTIVE_MASK 0x1U

#ifndef CSLP_DEFAULT_TEST_PATTERN
#define CSLP_DEFAULT_TEST_PATTERN 0
#endif

#ifndef CSLP_DEFAULT_TEST_MODE
#define CSLP_DEFAULT_TEST_MODE 0
#endif

#ifndef CSLP_DEFAULT_TEST_AMPLITUDE
#define CSLP_DEFAULT_TEST_AMPLITUDE 2047
#endif

#ifndef CSLP_DEFAULT_TEST_PHASE_INCREMENT
#define CSLP_DEFAULT_TEST_PHASE_INCREMENT 8388608U
#endif

#ifndef CSLP_DEFAULT_TEST_FAULTS
#define CSLP_DEFAULT_TEST_FAULTS 0U
#endif

#ifndef CSLP_PEER_IPV4_LAST_OCTET
#define CSLP_PEER_IPV4_LAST_OCTET 3
#endif

#ifndef CSLP_WAVE_SCALE_UV_PER_LSB
#define CSLP_WAVE_SCALE_UV_PER_LSB 488U
#endif

#ifndef CSLP_WAVE_OFFSET_UV
#define CSLP_WAVE_OFFSET_UV 0
#endif

#ifndef CSLP_WAVE_CALIBRATION_ID
#define CSLP_WAVE_CALIBRATION_ID 0U
#endif

#if CSLP_DEFAULT_TEST_PATTERN != 0 && CSLP_DEFAULT_TEST_PATTERN != 1
#error "CSLP_DEFAULT_TEST_PATTERN must be 0 or 1"
#endif

#if CSLP_DEFAULT_TEST_MODE < 0 || CSLP_DEFAULT_TEST_MODE > 2
#error "CSLP_DEFAULT_TEST_MODE must be 0..2"
#endif

#if CSLP_DEFAULT_TEST_AMPLITUDE < 0 || CSLP_DEFAULT_TEST_AMPLITUDE > 2047
#error "CSLP_DEFAULT_TEST_AMPLITUDE must be 0..2047"
#endif

#if (CSLP_DEFAULT_TEST_FAULTS & ~CSLP_TEST_FAULT_ALL) != 0
#error "CSLP_DEFAULT_TEST_FAULTS contains an unknown bit"
#endif

#if CSLP_PEER_IPV4_LAST_OCTET < 1 || CSLP_PEER_IPV4_LAST_OCTET > 254
#error "CSLP_PEER_IPV4_LAST_OCTET must be in 1..254"
#endif

#if CSLP_WAVE_SCALE_UV_PER_LSB == 0U
#error "CSLP_WAVE_SCALE_UV_PER_LSB must be nonzero"
#endif

#if CSLP_WAVE_CALIBRATION_ID > 65535U
#error "CSLP_WAVE_CALIBRATION_ID must fit u16"
#endif

#if CSLP_DEFAULT_TEST_PATTERN != 0 && CSLP_WAVE_CALIBRATION_ID != 0U
#error "test-pattern firmware must not advertise a real-ADC calibration"
#endif

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
    uint32_t pending_test_faults;
} cslp_app_t;

static struct netif server_netif;
static cslp_app_t app;

extern volatile int TcpFastTmrFlag;
extern volatile int TcpSlowTmrFlag;
extern int cyclescope_rtl8211f_link_is_ready(void);

static uint64_t now_us(void)
{
    XTime ticks;

    XTime_GetTime(&ticks);
    return cslp_ticks_to_us((uint64_t)ticks,
                            (uint64_t)(COUNTS_PER_SECOND));
}

static bool gem0_is_configured_for_100_full(void)
{
    uint32_t clock_control = Xil_In32(CSLP_SLCR_GEM0_CLK_CTRL);
    uint32_t network_config = Xil_In32(
        PLATFORM_EMAC_BASEADDR + XEMACPS_NWCFG_OFFSET);
    uint32_t clock_source =
        (clock_control >> CSLP_GEM_CLK_SOURCE_SHIFT) &
        CSLP_GEM_CLK_SOURCE_MASK;
    uint32_t divisor0 =
        (clock_control >> CSLP_GEM_CLK_DIVISOR0_SHIFT) &
        CSLP_GEM_CLK_DIVISOR_MASK;
    uint32_t divisor1 =
        (clock_control >> CSLP_GEM_CLK_DIVISOR1_SHIFT) &
        CSLP_GEM_CLK_DIVISOR_MASK;
    bool valid =
        (clock_control & CSLP_GEM_CLK_ACTIVE_MASK) != 0U &&
        clock_source == CSLP_GEM_CLK_IO_PLL &&
        divisor0 == CSLP_GEM_CLK_DIVISOR0_100M &&
        divisor1 == CSLP_GEM_CLK_DIVISOR1_100M &&
        (network_config & XEMACPS_NWCFG_100_MASK) != 0U &&
        (network_config & XEMACPS_NWCFG_FDEN_MASK) != 0U &&
        (network_config & XEMACPS_NWCFG_1000_MASK) == 0U;

    xil_printf("CYCLESCOPE_GEM0_100_FULL_%s SLCR=0x%08lx "
               "NWCFG=0x%08lx SRC=%lu DIV0=%lu DIV1=%lu\r\n",
               valid ? "PASS" : "FAIL",
               (unsigned long)clock_control,
               (unsigned long)network_config,
               (unsigned long)clock_source,
               (unsigned long)divisor0,
               (unsigned long)divisor1);
    return valid;
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
    IP4_ADDR(ip_2_ip4(&expected), 192, 168, 10,
             CSLP_PEER_IPV4_LAST_OCTET);
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
    if (CSLP_WAVE_CALIBRATION_ID != 0U)
        flags |= CSLP_FLAG_CALIBRATED;

    app.wave.metadata.frame_id = app.wave.frame.frame_id;
    app.wave.metadata.sample_rate_hz = app.control.config.sample_rate_hz;
    app.wave.metadata.frame_sample_count =
        app.control.config.frame_sample_count;
    app.wave.metadata.scale_uv_per_lsb =
        (uint32_t)CSLP_WAVE_SCALE_UV_PER_LSB;
    app.wave.metadata.offset_uv = (int32_t)CSLP_WAVE_OFFSET_UV;
    app.wave.metadata.config_id = app.control.active_config_id;
    app.wave.metadata.filter_profile = app.control.config.filter_profile;
    app.wave.metadata.calibration_id =
        (uint16_t)CSLP_WAVE_CALIBRATION_ID;
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
        if (app.pending_test_faults != 0U) {
            (void)cslp_dma_zynq_inject_test_faults(app.pending_test_faults);
            app.pending_test_faults = 0U;
        }
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
                              dma_stats.metadata_failures +
                              ((dma_stats.last_status_word &
                                CSLP_PL_STATUS_FRAMES_DROPPED_MASK) >>
                               CSLP_PL_STATUS_FRAMES_DROPPED_SHIFT);
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
    app.test_pattern = CSLP_DEFAULT_TEST_PATTERN != 0;
    if (!cslp_dma_zynq_configure_test_source(
            app.test_pattern, (cslp_test_mode_t)CSLP_DEFAULT_TEST_MODE,
            (uint16_t)CSLP_DEFAULT_TEST_AMPLITUDE,
            (uint32_t)CSLP_DEFAULT_TEST_PHASE_INCREMENT))
        return XST_FAILURE;
    cslp_dma_zynq_clear_pl_stats();
    app.pending_test_faults = CSLP_DEFAULT_TEST_FAULTS;

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
    xil_printf("CSLP control UDP %u, peer 192.168.10.%u:%u, "
               "test=%u mode=%u amplitude=%u phase_inc=%lu faults=0x%02x "
               "calibration_id=%u scale_uv_per_lsb=%lu offset_uv=%ld\r\n",
               CSLP_LOCAL_PORT, CSLP_PEER_IPV4_LAST_OCTET,
               CSLP_REMOTE_PORT, app.test_pattern ? 1U : 0U,
               CSLP_DEFAULT_TEST_MODE, CSLP_DEFAULT_TEST_AMPLITUDE,
               (unsigned long)CSLP_DEFAULT_TEST_PHASE_INCREMENT,
               CSLP_DEFAULT_TEST_FAULTS, CSLP_WAVE_CALIBRATION_ID,
               (unsigned long)CSLP_WAVE_SCALE_UV_PER_LSB,
               (long)CSLP_WAVE_OFFSET_UV);
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
    /*
     * AMD lwIP 2.2.0's xemacpsif subtracts the 14-byte Ethernet header from
     * XEMACPS_MTU even though lwIP's netif.mtu is the IP MTU.  That leaves
     * 1486 bytes and fragments a valid 1472-byte UDP payload.  GEM accepts
     * the standard 1500-byte IP MTU, so restore the value explicitly.
     */
    server_netif.mtu = (u16_t)CSLP_REQUIRED_NETIF_MTU;
    xil_printf("CYCLESCOPE_NETIF_MTU_PASS MTU=%u MAX_UDP=%u\r\n",
               (unsigned int)server_netif.mtu,
               (unsigned int)CSLP_MAX_UDP_PAYLOAD);
    if (!cyclescope_rtl8211f_link_is_ready()) {
        xil_printf("CSLP: RTL8211F 100M full-duplex link is not ready\r\n");
        return XST_FAILURE;
    }
    if (!gem0_is_configured_for_100_full()) {
        xil_printf("CSLP: GEM0 is not configured for 100M full duplex\r\n");
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
