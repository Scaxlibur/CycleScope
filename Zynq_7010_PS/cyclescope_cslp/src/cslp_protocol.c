#include "cslp_protocol.h"

#include <string.h>

#define CSLP_CRC_FIELD_OFFSET 28U
#define CSLP_WAVE_ALLOWED_FRAME_FLAGS                                             \
    (CSLP_FLAG_FILTERED | CSLP_FLAG_CALIBRATED | CSLP_FLAG_ADC_OVERRANGE |       \
     CSLP_FLAG_FIFO_OVERFLOW | CSLP_FLAG_TEST_PATTERN)

uint16_t cslp_read_u16be(const uint8_t *bytes)
{
    return (uint16_t)(((uint16_t)bytes[0] << 8) | bytes[1]);
}
uint32_t cslp_read_u32be(const uint8_t *bytes)
{
    return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) | (uint32_t)bytes[3];
}

uint64_t cslp_read_u64be(const uint8_t *bytes)
{
    return ((uint64_t)cslp_read_u32be(bytes) << 32) |
           cslp_read_u32be(bytes + 4);
}

void cslp_write_u16be(uint8_t *bytes, uint16_t value)
{
    bytes[0] = (uint8_t)(value >> 8);
    bytes[1] = (uint8_t)value;
}

void cslp_write_u32be(uint8_t *bytes, uint32_t value)
{
    bytes[0] = (uint8_t)(value >> 24);
    bytes[1] = (uint8_t)(value >> 16);
    bytes[2] = (uint8_t)(value >> 8);
    bytes[3] = (uint8_t)value;
}

void cslp_write_u64be(uint8_t *bytes, uint64_t value)
{
    cslp_write_u32be(bytes, (uint32_t)(value >> 32));
    cslp_write_u32be(bytes + 4, (uint32_t)value);
}

static uint32_t crc32_update(uint32_t crc, uint8_t byte)
{
    unsigned int bit;

    crc ^= byte;
    for (bit = 0; bit < 8U; ++bit)
        crc = (crc >> 1) ^ ((crc & 1U) ? 0xedb88320U : 0U);
    return crc;
}

uint32_t cslp_crc32_iso_hdlc(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xffffffffU;
    size_t index;

    if (data == NULL && length != 0U)
        return 0U;
    for (index = 0; index < length; ++index)
        crc = crc32_update(crc, data[index]);
    return crc ^ 0xffffffffU;
}

uint32_t cslp_crc32_datagram(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xffffffffU;
    size_t index;

    if (data == NULL || length < CSLP_COMMON_HEADER_BYTES)
        return 0U;

    for (index = 0; index < length; ++index) {
        uint8_t byte = data[index];
        if (index >= CSLP_CRC_FIELD_OFFSET &&
            index < CSLP_CRC_FIELD_OFFSET + 4U)
            byte = 0U;
        crc = crc32_update(crc, byte);
    }
    return crc ^ 0xffffffffU;
}

cslp_parse_result_t cslp_parse_datagram(const uint8_t *data,
                                        size_t length,
                                        cslp_header_t *header)
{
    uint16_t header_bytes;
    uint16_t payload_bytes;

    if (data == NULL || header == NULL || length < CSLP_COMMON_HEADER_BYTES)
        return CSLP_PARSE_TOO_SHORT;
    if (data[0] != 'C' || data[1] != 'S' || data[2] != 'L' || data[3] != 'P')
        return CSLP_PARSE_BAD_MAGIC;

    header_bytes = cslp_read_u16be(data + 6);
    payload_bytes = cslp_read_u16be(data + 24);
    if (header_bytes < CSLP_COMMON_HEADER_BYTES || header_bytes > length)
        return CSLP_PARSE_BAD_HEADER_LENGTH;
    if ((size_t)header_bytes + payload_bytes != length)
        return CSLP_PARSE_BAD_TOTAL_LENGTH;
    if (cslp_crc32_datagram(data, length) != cslp_read_u32be(data + 28))
        return CSLP_PARSE_BAD_CRC;

    header->version = data[4];
    header->message_type = data[5];
    header->header_bytes = header_bytes;
    header->session_id = cslp_read_u32be(data + 8);
    header->message_seq = cslp_read_u32be(data + 12);
    header->timestamp_us = cslp_read_u64be(data + 16);
    header->payload_bytes = payload_bytes;
    header->flags = cslp_read_u16be(data + 26);
    header->crc32 = cslp_read_u32be(data + 28);
    return CSLP_PARSE_OK;
}

static bool write_common_header(uint8_t *output,
                                size_t capacity,
                                uint8_t message_type,
                                uint16_t header_bytes,
                                uint32_t session_id,
                                uint32_t message_seq,
                                uint64_t timestamp_us,
                                uint16_t payload_bytes,
                                uint16_t flags)
{
    size_t total_bytes = (size_t)header_bytes + payload_bytes;

    if (output == NULL || capacity < total_bytes ||
        header_bytes < CSLP_COMMON_HEADER_BYTES)
        return false;

    memset(output, 0, total_bytes);
    output[0] = 'C';
    output[1] = 'S';
    output[2] = 'L';
    output[3] = 'P';
    output[4] = CSLP_VERSION;
    output[5] = message_type;
    cslp_write_u16be(output + 6, header_bytes);
    cslp_write_u32be(output + 8, session_id);
    cslp_write_u32be(output + 12, message_seq);
    cslp_write_u64be(output + 16, timestamp_us);
    cslp_write_u16be(output + 24, payload_bytes);
    cslp_write_u16be(output + 26, flags);
    return true;
}

static void finalize_datagram(uint8_t *output, size_t total_bytes)
{
    cslp_write_u32be(output + CSLP_CRC_FIELD_OFFSET, 0U);
    cslp_write_u32be(output + CSLP_CRC_FIELD_OFFSET,
                     cslp_crc32_datagram(output, total_bytes));
}

size_t cslp_build_message(uint8_t *output,
                          size_t capacity,
                          uint8_t message_type,
                          uint32_t session_id,
                          uint32_t message_seq,
                          uint64_t timestamp_us,
                          uint16_t flags,
                          const uint8_t *payload,
                          uint16_t payload_bytes)
{
    size_t total_bytes = CSLP_COMMON_HEADER_BYTES + (size_t)payload_bytes;

    if (payload_bytes != 0U && payload == NULL)
        return 0U;
    if (!write_common_header(output, capacity, message_type,
                             CSLP_COMMON_HEADER_BYTES, session_id, message_seq,
                             timestamp_us, payload_bytes, flags))
        return 0U;
    if (payload_bytes != 0U)
        memcpy(output + CSLP_COMMON_HEADER_BYTES, payload, payload_bytes);
    finalize_datagram(output, total_bytes);
    return total_bytes;
}

bool cslp_wave_chunk_layout(uint32_t frame_sample_count,
                            uint16_t chunk_index,
                            uint16_t *chunk_count,
                            uint32_t *sample_offset,
                            uint16_t *samples_in_chunk)
{
    uint32_t count;
    uint32_t offset;
    uint32_t remaining;

    if (frame_sample_count == 0U || frame_sample_count > 45874500U ||
        chunk_count == NULL || sample_offset == NULL ||
        samples_in_chunk == NULL)
        return false;

    count = (frame_sample_count + CSLP_WAVE_SAMPLES_PER_FULL_CHUNK - 1U) /
            CSLP_WAVE_SAMPLES_PER_FULL_CHUNK;
    if (count == 0U || count > UINT16_MAX || chunk_index >= count)
        return false;

    offset = (uint32_t)chunk_index * CSLP_WAVE_SAMPLES_PER_FULL_CHUNK;
    remaining = frame_sample_count - offset;
    *chunk_count = (uint16_t)count;
    *sample_offset = offset;
    *samples_in_chunk = (uint16_t)(
        remaining > CSLP_WAVE_SAMPLES_PER_FULL_CHUNK
            ? CSLP_WAVE_SAMPLES_PER_FULL_CHUNK
            : remaining);
    return true;
}

size_t cslp_build_wave_chunk(uint8_t *output,
                             size_t capacity,
                             uint32_t session_id,
                             uint32_t message_seq,
                             uint64_t timestamp_us,
                             const cslp_wave_metadata_t *metadata,
                             const int16_t *frame_samples,
                             uint16_t chunk_index)
{
    uint16_t chunk_count;
    uint32_t sample_offset;
    uint16_t samples_in_chunk;
    uint16_t payload_bytes;
    uint16_t flags;
    size_t total_bytes;
    size_t index;

    if (metadata == NULL || frame_samples == NULL || metadata->frame_id == 0U ||
        metadata->config_id == 0U || metadata->scale_uv_per_lsb == 0U ||
        metadata->sample_format != CSLP_PROFILE_SAMPLE_FORMAT ||
        metadata->channel_count != CSLP_PROFILE_CHANNEL_COUNT ||
        (metadata->frame_flags & ~CSLP_WAVE_ALLOWED_FRAME_FLAGS) != 0U ||
        (((metadata->frame_flags & CSLP_FLAG_CALIBRATED) != 0U) !=
         (metadata->calibration_id != 0U)))
        return 0U;

    if (!cslp_wave_chunk_layout(metadata->frame_sample_count, chunk_index,
                                &chunk_count, &sample_offset,
                                &samples_in_chunk))
        return 0U;

    payload_bytes = (uint16_t)(samples_in_chunk * 2U);
    total_bytes = CSLP_WAVE_HEADER_BYTES + (size_t)payload_bytes;
    if (total_bytes > CSLP_MAX_UDP_PAYLOAD || capacity < total_bytes)
        return 0U;

    flags = metadata->frame_flags;
    if (chunk_index == 0U)
        flags |= CSLP_FLAG_FIRST_CHUNK;
    if ((uint16_t)(chunk_index + 1U) == chunk_count)
        flags |= CSLP_FLAG_LAST_CHUNK;

    if (!write_common_header(output, capacity, CSLP_MSG_WAVE_DATA,
                             CSLP_WAVE_HEADER_BYTES, session_id, message_seq,
                             timestamp_us, payload_bytes, flags))
        return 0U;

    cslp_write_u32be(output + 32, metadata->frame_id);
    cslp_write_u16be(output + 36, chunk_index);
    cslp_write_u16be(output + 38, chunk_count);
    cslp_write_u32be(output + 40, sample_offset);
    cslp_write_u16be(output + 44, samples_in_chunk);
    output[46] = metadata->sample_format;
    output[47] = metadata->channel_count;
    cslp_write_u32be(output + 48, metadata->sample_rate_hz);
    cslp_write_u32be(output + 52, metadata->frame_sample_count);
    cslp_write_u32be(output + 56, metadata->scale_uv_per_lsb);
    cslp_write_u32be(output + 60, (uint32_t)metadata->offset_uv);
    cslp_write_u32be(output + 64, metadata->config_id);
    cslp_write_u16be(output + 68, metadata->filter_profile);
    cslp_write_u16be(output + 70, metadata->calibration_id);

    for (index = 0; index < samples_in_chunk; ++index) {
        uint16_t sample = (uint16_t)frame_samples[sample_offset + index];
        output[CSLP_WAVE_HEADER_BYTES + index * 2U] = (uint8_t)sample;
        output[CSLP_WAVE_HEADER_BYTES + index * 2U + 1U] =
            (uint8_t)(sample >> 8);
    }

    finalize_datagram(output, total_bytes);
    return total_bytes;
}

size_t cslp_build_status(uint8_t *output,
                         size_t capacity,
                         uint32_t session_id,
                         uint32_t message_seq,
                         uint64_t timestamp_us,
                         const cslp_status_snapshot_t *snapshot)
{
    uint8_t payload[40] = {0};

    if (snapshot == NULL)
        return 0U;
    cslp_write_u16be(payload + 0, snapshot->device_state);
    cslp_write_u16be(payload + 2, snapshot->last_error);
    cslp_write_u32be(payload + 4, snapshot->active_config_id);
    cslp_write_u32be(payload + 8, snapshot->last_frame_id);
    cslp_write_u32be(payload + 12, snapshot->frames_sent);
    cslp_write_u32be(payload + 16, snapshot->packets_sent);
    cslp_write_u32be(payload + 20, snapshot->adc_overrange_frames);
    cslp_write_u32be(payload + 24, snapshot->fifo_overflow_frames);
    cslp_write_u32be(payload + 28, snapshot->frames_dropped);
    cslp_write_u32be(payload + 32, snapshot->uptime_ms);
    return cslp_build_message(output, capacity, CSLP_MSG_STATUS, session_id,
                              message_seq, timestamp_us, 0U, payload,
                              (uint16_t)sizeof(payload));
}
