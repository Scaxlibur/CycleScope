#include "cslp_control.h"
#include "cslp_frame_pool.h"
#include "cslp_mirror_policy.h"
#include "cslp_protocol.h"
#include "cslp_time.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(condition)                                                        \
    do {                                                                        \
        if (!(condition)) {                                                     \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, \
                    #condition);                                                \
            exit(EXIT_FAILURE);                                                 \
        }                                                                       \
    } while (0)

#define TEST_CPU_CLOCK_HZ 666666687ULL
#define TEST_COUNTS_PER_SECOND TEST_CPU_CLOCK_HZ / 2ULL

static const uint8_t golden_wave[82] = {
    0x43, 0x53, 0x4c, 0x50, 0x01, 0x20, 0x00, 0x48,
    0x11, 0x22, 0x33, 0x44, 0x01, 0x02, 0x03, 0x04,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x12, 0xd6, 0x87,
    0x00, 0x0a, 0x00, 0x0f, 0x69, 0xdb, 0x20, 0x4c,
    0x00, 0x00, 0x00, 0x2a, 0x00, 0x00, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x05, 0x01, 0x01,
    0x00, 0x3d, 0xfd, 0x24, 0x00, 0x00, 0x00, 0x05,
    0x00, 0x00, 0x01, 0xe8, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x07, 0x00, 0x01, 0x00, 0x03,
    0x00, 0x80, 0xff, 0xff, 0x00, 0x00, 0x01, 0x00,
    0xff, 0x7f
};

static void test_tick_conversion(void)
{
    const uint64_t counts_per_second =
        (uint64_t)(TEST_COUNTS_PER_SECOND);
    const uint64_t one_day_ticks = counts_per_second * 86400ULL;

    CHECK(counts_per_second == 333333343ULL);
    CHECK(cslp_ticks_to_us(counts_per_second, counts_per_second) ==
          1000000ULL);
    CHECK(one_day_ticks > UINT64_MAX / 1000000ULL);
    CHECK(cslp_ticks_to_us(one_day_ticks, counts_per_second) ==
          86400000000ULL);
    CHECK(cslp_ticks_to_us(counts_per_second + counts_per_second / 2ULL,
                           counts_per_second) == 1499999ULL);

    CHECK(cslp_adc_elapsed_ticks_to_us(0U) == 0U);
    CHECK(cslp_adc_elapsed_ticks_to_us(32U) == 0U);
    CHECK(cslp_adc_elapsed_ticks_to_us(33U) == 1U);
    CHECK(cslp_adc_elapsed_ticks_to_us(65U) == 1U);
    CHECK(cslp_adc_elapsed_ticks_to_us(3250000U) == 50000U);

    {
        uint64_t mapped = 0U;

        CHECK(cslp_adc_tick_to_monotonic_us(
            3251000U, 1000U, 7000000U, &mapped));
        CHECK(mapped == 7050000U);
        CHECK(cslp_adc_tick_to_monotonic_us(
            50U, UINT64_MAX - 99U, 123U, &mapped));
        CHECK(mapped == 125U);
        CHECK(!cslp_adc_tick_to_monotonic_us(
            99U, 100U, 0U, &mapped));
        CHECK(!cslp_adc_tick_to_monotonic_us(
            165U, 100U, UINT64_MAX, &mapped));
        CHECK(!cslp_adc_tick_to_monotonic_us(
            100U, 100U, 0U, NULL));
    }
}

static void test_crc_and_golden_packet(void)
{
    static const uint8_t check_text[] = "123456789";
    const int16_t samples[] = {-32768, -1, 0, 1, 32767};
    cslp_wave_metadata_t metadata = {
        .frame_id = 42,
        .sample_rate_hz = CSLP_PROFILE_SAMPLE_RATE_HZ,
        .frame_sample_count = 5,
        .scale_uv_per_lsb = 488,
        .offset_uv = 0,
        .config_id = 7,
        .filter_profile = 1,
        .calibration_id = 3,
        .frame_flags = CSLP_FLAG_FILTERED | CSLP_FLAG_CALIBRATED,
        .sample_format = CSLP_PROFILE_SAMPLE_FORMAT,
        .channel_count = CSLP_PROFILE_CHANNEL_COUNT,
    };
    cslp_header_t header;
    uint8_t encoded[CSLP_MAX_UDP_PAYLOAD];
    size_t encoded_bytes;

    CHECK(cslp_crc32_iso_hdlc(check_text, sizeof(check_text) - 1U) ==
          0xcbf43926U);
    CHECK(cslp_crc32_datagram(golden_wave, sizeof(golden_wave)) ==
          0x69db204cU);
    CHECK(cslp_parse_datagram(golden_wave, sizeof(golden_wave), &header) ==
          CSLP_PARSE_OK);
    CHECK(header.message_type == CSLP_MSG_WAVE_DATA);
    CHECK(header.header_bytes == CSLP_WAVE_HEADER_BYTES);
    CHECK(header.payload_bytes == 10U);
    CHECK(header.timestamp_us == 1234567ULL);

    encoded_bytes = cslp_build_wave_chunk(
        encoded, sizeof(encoded), 0x11223344U, 0x01020304U, 1234567ULL,
        &metadata, samples, 0U);
    CHECK(encoded_bytes == sizeof(golden_wave));
    CHECK(memcmp(encoded, golden_wave, sizeof(golden_wave)) == 0);
    CHECK(encoded[72] == 0x00 && encoded[73] == 0x80);
    CHECK(encoded[80] == 0xff && encoded[81] == 0x7f);
}

static void test_fixed_fragmentation(void)
{
    int16_t *samples = malloc(CSLP_PROFILE_FRAME_SAMPLES * sizeof(*samples));
    cslp_wave_metadata_t metadata = {
        .frame_id = 1,
        .sample_rate_hz = CSLP_PROFILE_SAMPLE_RATE_HZ,
        .frame_sample_count = CSLP_PROFILE_FRAME_SAMPLES,
        .scale_uv_per_lsb = 488,
        .offset_uv = 0,
        .config_id = 9,
        .filter_profile = CSLP_PROFILE_FILTER,
        .calibration_id = 0,
        .frame_flags = CSLP_FLAG_FILTERED | CSLP_FLAG_TEST_PATTERN,
        .sample_format = CSLP_PROFILE_SAMPLE_FORMAT,
        .channel_count = CSLP_PROFILE_CHANNEL_COUNT,
    };
    uint8_t packet[CSLP_MAX_UDP_PAYLOAD];
    uint32_t covered = 0U;
    uint16_t chunk;

    CHECK(samples != NULL);
    for (uint32_t index = 0; index < CSLP_PROFILE_FRAME_SAMPLES; ++index)
        samples[index] = (int16_t)((index & 0x0fffU) - 2048);

    for (chunk = 0; chunk < CSLP_PROFILE_CHUNK_COUNT; ++chunk) {
        cslp_header_t header;
        uint16_t chunk_count;
        uint16_t samples_in_chunk;
        uint32_t sample_offset;
        size_t packet_bytes;

        CHECK(cslp_wave_chunk_layout(CSLP_PROFILE_FRAME_SAMPLES, chunk,
                                     &chunk_count, &sample_offset,
                                     &samples_in_chunk));
        CHECK(chunk_count == CSLP_PROFILE_CHUNK_COUNT);
        CHECK(sample_offset == (uint32_t)chunk * 700U);
        CHECK(samples_in_chunk == (chunk == 11U ? 492U : 700U));

        packet_bytes = cslp_build_wave_chunk(
            packet, sizeof(packet), 5U, 100U + chunk, 1000000ULL, &metadata,
            samples, chunk);
        CHECK(packet_bytes == CSLP_WAVE_HEADER_BYTES + samples_in_chunk * 2U);
        CHECK(packet_bytes == (chunk == 11U ? 1056U : 1472U));
        CHECK(cslp_parse_datagram(packet, packet_bytes, &header) ==
              CSLP_PARSE_OK);
        CHECK(((header.flags & CSLP_FLAG_FIRST_CHUNK) != 0U) == (chunk == 0U));
        CHECK(((header.flags & CSLP_FLAG_LAST_CHUNK) != 0U) == (chunk == 11U));
        CHECK((header.flags & CSLP_FLAG_CALIBRATED) == 0U);
        CHECK(cslp_read_u32be(packet + 40) == sample_offset);
        CHECK(cslp_read_u16be(packet + 44) == samples_in_chunk);
        covered += samples_in_chunk;
    }
    CHECK(covered == CSLP_PROFILE_FRAME_SAMPLES);
    free(samples);
}

static size_t make_request(uint8_t *output,
                           uint8_t message_type,
                           uint32_t session_id,
                           uint32_t sequence,
                           const uint8_t *payload,
                           uint16_t payload_bytes)
{
    return cslp_build_message(output, CSLP_MAX_UDP_PAYLOAD, message_type,
                              session_id, sequence, 123U, 0U, payload,
                              payload_bytes);
}

static uint16_t response_status(const uint8_t *response, size_t response_bytes)
{
    cslp_header_t header;
    CHECK(cslp_parse_datagram(response, response_bytes, &header) ==
          CSLP_PARSE_OK);
    return cslp_read_u16be(response + header.header_bytes);
}

static void test_control_state_and_idempotency(void)
{
    const uint32_t session = 0xaabbccddU;
    cslp_control_t control;
    uint8_t request[CSLP_MAX_UDP_PAYLOAD];
    uint8_t response[CSLP_CONTROL_MAX_RESPONSE_BYTES];
    uint8_t replay[CSLP_CONTROL_MAX_RESPONSE_BYTES];
    uint8_t hello[8] = {0};
    uint8_t config[20] = {0};
    uint8_t conflict[20] = {0};
    size_t request_bytes;
    size_t response_bytes;
    size_t replay_bytes;
    uint32_t first_config_id;
    cslp_control_result_t result;

    cslp_control_init(&control, 0x10203040U);
    cslp_write_u16be(hello + 0, 50001U);
    cslp_write_u16be(hello + 2, CSLP_MAX_UDP_PAYLOAD);
    cslp_write_u32be(hello + 4, CSLP_REQUIRED_CAPS);
    request_bytes = make_request(request, CSLP_MSG_HELLO, session, 1U,
                                 hello, sizeof(hello));
    result = cslp_control_handle(&control, request, request_bytes, 50001U,
                                 1000U, false, response, sizeof(response),
                                 &response_bytes);
    CHECK(result == CSLP_CONTROL_RESPONSE);
    CHECK(response_status(response, response_bytes) == CSLP_STATUS_OK);
    CHECK(control.session_id == session);
    CHECK(control.state == CSLP_DEVICE_IDLE);

    result = cslp_control_handle(&control, request, request_bytes, 50001U,
                                 500000U, false, replay, sizeof(replay),
                                 &replay_bytes);
    CHECK(result == CSLP_CONTROL_RESPONSE);
    CHECK(replay_bytes == response_bytes);
    CHECK(memcmp(replay, response, response_bytes) == 0);
    CHECK(control.stats.cache_hits == 1U);

    cslp_write_u32be(config + 0, CSLP_PROFILE_SAMPLE_RATE_HZ);
    cslp_write_u32be(config + 4, CSLP_PROFILE_FRAME_SAMPLES);
    cslp_write_u32be(config + 8, CSLP_PROFILE_FRAME_PERIOD_US);
    config[12] = CSLP_PROFILE_SAMPLE_FORMAT;
    config[13] = CSLP_PROFILE_CHANNEL_COUNT;
    cslp_write_u16be(config + 14, CSLP_PROFILE_FILTER);
    request_bytes = make_request(request, CSLP_MSG_CONFIG_SET, session, 2U,
                                 config, sizeof(config));
    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              600000U, false, response, sizeof(response),
                              &response_bytes) == CSLP_CONTROL_RESPONSE);
    CHECK(response_status(response, response_bytes) == CSLP_STATUS_OK);
    first_config_id = control.active_config_id;
    CHECK(first_config_id != 0U);
    CHECK(control.state == CSLP_DEVICE_READY);

    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              700000U, false, replay, sizeof(replay),
                              &replay_bytes) == CSLP_CONTROL_RESPONSE);
    CHECK(control.active_config_id == first_config_id);
    CHECK(replay_bytes == response_bytes);
    CHECK(memcmp(replay, response, response_bytes) == 0);

    memcpy(conflict, config, sizeof(conflict));
    cslp_write_u32be(conflict + 8, 40000U);
    request_bytes = make_request(request, CSLP_MSG_CONFIG_SET, session, 2U,
                                 conflict, sizeof(conflict));
    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              800000U, false, response, sizeof(response),
                              &response_bytes) == CSLP_CONTROL_RESPONSE);
    CHECK(response_status(response, response_bytes) ==
          CSLP_STATUS_SEQ_CONFLICT);
    CHECK(control.active_config_id == first_config_id);

    request_bytes = make_request(request, CSLP_MSG_ENABLE_PUSH, session, 3U,
                                 NULL, 0U);
    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              900000U, false, response, sizeof(response),
                              &response_bytes) == CSLP_CONTROL_RESPONSE);
    CHECK(response_status(response, response_bytes) == CSLP_STATUS_OK);
    CHECK(cslp_control_push_enabled(&control));

    request_bytes = make_request(request, CSLP_MSG_DISABLE_PUSH, session, 4U,
                                 NULL, 0U);
    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              1000000U, true, response, sizeof(response),
                              &response_bytes) == CSLP_CONTROL_DEFERRED);
    CHECK(!cslp_control_push_enabled(&control));
    CHECK(control.disable_pending);
    CHECK(cslp_control_poll(&control, 1100000U, true, response,
                            sizeof(response), &response_bytes) ==
          CSLP_CONTROL_DROP);
    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              3100000U, true, replay, sizeof(replay),
                              &replay_bytes) == CSLP_CONTROL_DEFERRED);
    CHECK(control.disable_pending);
    CHECK(control.pending_disable_cache_index >= 0);
    CHECK(cslp_control_poll(&control, 3200000U, false, response,
                            sizeof(response), &response_bytes) ==
          CSLP_CONTROL_RESPONSE);
    CHECK(response_status(response, response_bytes) == CSLP_STATUS_OK);
    CHECK(control.state == CSLP_DEVICE_READY);
    CHECK(!control.disable_pending);

    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              3300000U, false, replay, sizeof(replay),
                              &replay_bytes) == CSLP_CONTROL_RESPONSE);
    CHECK(replay_bytes == response_bytes);
    CHECK(memcmp(replay, response, response_bytes) == 0);

    request[16] ^= 1U;
    CHECK(cslp_control_handle(&control, request, request_bytes, 50001U,
                              3400000U, false, response, sizeof(response),
                              &response_bytes) == CSLP_CONTROL_DROP);
}

static void test_frame_ownership(void)
{
    cslp_frame_pool_t pool;
    int dma0;
    int dma1;
    int tx;

    cslp_frame_pool_init(&pool);
    dma0 = cslp_frame_pool_acquire_dma(&pool);
    dma1 = cslp_frame_pool_acquire_dma(&pool);
    CHECK(dma0 == 0 && dma1 == 1);
    CHECK(cslp_frame_pool_acquire_dma(&pool) < 0);
    CHECK(cslp_frame_pool_complete_dma(&pool, (unsigned int)dma0, 10U, 0U,
                                       100U));
    CHECK(cslp_frame_pool_complete_dma(&pool, (unsigned int)dma1, 11U, 4U,
                                       200U));
    tx = cslp_frame_pool_acquire_latest_tx(&pool);
    CHECK(tx == dma1);
    CHECK(pool.slots[(unsigned int)dma0].owner == CSLP_FRAME_FREE);
    CHECK(pool.dropped_ready == 1U);
    CHECK(cslp_frame_pool_release_tx(&pool, (unsigned int)tx));
    dma0 = cslp_frame_pool_acquire_dma(&pool);
    CHECK(dma0 >= 0);
    CHECK(cslp_frame_pool_cancel_dma(&pool, (unsigned int)dma0));
    CHECK(pool.slots[(unsigned int)dma0].owner == CSLP_FRAME_FREE);
    CHECK(cslp_frame_pool_acquire_latest_tx(&pool) < 0);
}

typedef struct {
    cslp_fanout_destination_t calls[2];
    const uint8_t *bytes[2];
    size_t lengths[2];
    unsigned int call_count;
    bool primary_result;
    bool mirror_result;
} fake_fanout_t;

static bool fake_fanout_send(void *context,
                             cslp_fanout_destination_t destination,
                             const uint8_t *bytes,
                             size_t length)
{
    fake_fanout_t *fake = (fake_fanout_t *)context;
    unsigned int index = fake->call_count++;

    CHECK(index < 2U);
    fake->calls[index] = destination;
    fake->bytes[index] = bytes;
    fake->lengths[index] = length;
    return destination == CSLP_FANOUT_PRIMARY ? fake->primary_result
                                               : fake->mirror_result;
}

static fake_fanout_t fresh_fake(void)
{
    fake_fanout_t fake;

    memset(&fake, 0, sizeof(fake));
    fake.primary_result = true;
    fake.mirror_result = true;
    return fake;
}

static void test_primary_then_mirror_policy(void)
{
    static const uint8_t datagram[] = {0x43, 0x53, 0x4c, 0x50};
    cslp_mirror_stats_t mirror = {.enabled = true};
    fake_fanout_t fake = fresh_fake();

    CHECK(cslp_send_primary_then_mirror(
        &mirror, true, datagram, sizeof(datagram), fake_fanout_send, &fake));
    CHECK(fake.call_count == 2U);
    CHECK(fake.calls[0] == CSLP_FANOUT_PRIMARY);
    CHECK(fake.calls[1] == CSLP_FANOUT_MIRROR);
    CHECK(fake.bytes[0] == datagram && fake.bytes[1] == datagram);
    CHECK(fake.lengths[0] == sizeof(datagram));
    CHECK(fake.lengths[1] == sizeof(datagram));
    CHECK(mirror.datagrams_attempted == 1U);
    CHECK(mirror.datagrams_queued == 1U);
    CHECK(mirror.send_failures == 0U);
    CHECK(mirror.arp_unresolved == 0U);

    mirror = (cslp_mirror_stats_t){.enabled = true};
    fake = fresh_fake();
    fake.primary_result = false;
    CHECK(!cslp_send_primary_then_mirror(
        &mirror, true, datagram, sizeof(datagram), fake_fanout_send, &fake));
    CHECK(fake.call_count == 1U);
    CHECK(fake.calls[0] == CSLP_FANOUT_PRIMARY);
    CHECK(mirror.datagrams_attempted == 0U);
    CHECK(mirror.datagrams_queued == 0U);
    CHECK(mirror.send_failures == 0U);

    mirror = (cslp_mirror_stats_t){.enabled = true};
    fake = fresh_fake();
    fake.mirror_result = false;
    CHECK(cslp_send_primary_then_mirror(
        &mirror, true, datagram, sizeof(datagram), fake_fanout_send, &fake));
    CHECK(fake.call_count == 2U);
    CHECK(mirror.datagrams_attempted == 1U);
    CHECK(mirror.datagrams_queued == 0U);
    CHECK(mirror.send_failures == 1U);

    mirror = (cslp_mirror_stats_t){.enabled = true};
    fake = fresh_fake();
    CHECK(cslp_send_primary_then_mirror(
        &mirror, false, datagram, sizeof(datagram), fake_fanout_send, &fake));
    CHECK(fake.call_count == 1U);
    CHECK(mirror.arp_unresolved == 1U);
    CHECK(mirror.datagrams_attempted == 0U);

    mirror = (cslp_mirror_stats_t){.enabled = false};
    fake = fresh_fake();
    CHECK(cslp_send_primary_then_mirror(
        &mirror, true, datagram, sizeof(datagram), fake_fanout_send, &fake));
    CHECK(fake.call_count == 1U);
    CHECK(mirror.datagrams_attempted == 0U);
}

int main(void)
{
    test_tick_conversion();
    test_crc_and_golden_packet();
    test_fixed_fragmentation();
    test_control_state_and_idempotency();
    test_frame_ownership();
    test_primary_then_mirror_policy();
    puts("ALL_CSLP_HOST_TESTS_PASS");
    return EXIT_SUCCESS;
}
