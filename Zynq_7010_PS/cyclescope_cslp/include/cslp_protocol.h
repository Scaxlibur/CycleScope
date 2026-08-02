#ifndef CSLP_PROTOCOL_H
#define CSLP_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif
#define CSLP_VERSION                         1U
#define CSLP_COMMON_HEADER_BYTES            32U
#define CSLP_WAVE_HEADER_BYTES              72U
#define CSLP_MAX_UDP_PAYLOAD              1472U
#define CSLP_WAVE_SAMPLES_PER_FULL_CHUNK   700U
#define CSLP_PROFILE_SAMPLE_RATE_HZ    4062500U
#define CSLP_PROFILE_FRAME_SAMPLES        8192U
#define CSLP_PROFILE_FRAME_PERIOD_US     50000U
#define CSLP_PROFILE_SAMPLE_FORMAT           1U
#define CSLP_PROFILE_CHANNEL_COUNT           1U
#define CSLP_PROFILE_FILTER                  1U
#define CSLP_PROFILE_CHUNK_COUNT            12U
#define CSLP_REQUIRED_CAPS                 0x1fU

typedef enum {
    CSLP_MSG_HELLO = 0x01,
    CSLP_MSG_CONFIG_SET = 0x02,
    CSLP_MSG_ENABLE_PUSH = 0x03,
    CSLP_MSG_DISABLE_PUSH = 0x04,
    CSLP_MSG_STATUS = 0x10,
    CSLP_MSG_WAVE_DATA = 0x20,
    CSLP_MSG_ERROR = 0x7f,
    CSLP_MSG_HELLO_ACK = 0x81,
    CSLP_MSG_CONFIG_ACK = 0x82,
    CSLP_MSG_ENABLE_PUSH_ACK = 0x83,
    CSLP_MSG_DISABLE_PUSH_ACK = 0x84
} cslp_message_type_t;

typedef enum {
    CSLP_STATUS_OK = 0,
    CSLP_STATUS_BAD_VERSION = 1,
    CSLP_STATUS_BAD_LENGTH = 2,
    CSLP_STATUS_BAD_CONFIG = 3,
    CSLP_STATUS_UNSUPPORTED = 4,
    CSLP_STATUS_BAD_STATE = 5,
    CSLP_STATUS_BUSY = 6,
    CSLP_STATUS_INTERNAL_ERROR = 7,
    CSLP_STATUS_SEQ_CONFLICT = 8
} cslp_status_code_t;

typedef enum {
    CSLP_DEVICE_IDLE = 0,
    CSLP_DEVICE_READY = 1,
    CSLP_DEVICE_PUSH_ENABLED = 2,
    CSLP_DEVICE_FAULT = 3
} cslp_device_state_t;

enum {
    CSLP_FLAG_FIRST_CHUNK = 0x0001,
    CSLP_FLAG_LAST_CHUNK = 0x0002,
    CSLP_FLAG_FILTERED = 0x0004,
    CSLP_FLAG_CALIBRATED = 0x0008,
    CSLP_FLAG_ADC_OVERRANGE = 0x0010,
    CSLP_FLAG_FIFO_OVERFLOW = 0x0020,
    CSLP_FLAG_TEST_PATTERN = 0x0040
};

typedef struct {
    uint8_t version;
    uint8_t message_type;
    uint16_t header_bytes;
    uint32_t session_id;
    uint32_t message_seq;
    uint64_t timestamp_us;
    uint16_t payload_bytes;
    uint16_t flags;
    uint32_t crc32;
} cslp_header_t;

typedef enum {
    CSLP_PARSE_OK = 0,
    CSLP_PARSE_TOO_SHORT,
    CSLP_PARSE_BAD_MAGIC,
    CSLP_PARSE_BAD_HEADER_LENGTH,
    CSLP_PARSE_BAD_TOTAL_LENGTH,
    CSLP_PARSE_BAD_CRC
} cslp_parse_result_t;

typedef struct {
    uint32_t frame_id;
    uint32_t sample_rate_hz;
    uint32_t frame_sample_count;
    uint32_t scale_uv_per_lsb;
    int32_t offset_uv;
    uint32_t config_id;
    uint16_t filter_profile;
    uint16_t calibration_id;
    uint16_t frame_flags;
    uint8_t sample_format;
    uint8_t channel_count;
} cslp_wave_metadata_t;

typedef struct {
    uint16_t device_state;
    uint16_t last_error;
    uint32_t active_config_id;
    uint32_t last_frame_id;
    uint32_t frames_sent;
    uint32_t packets_sent;
    uint32_t adc_overrange_frames;
    uint32_t fifo_overflow_frames;
    uint32_t frames_dropped;
    uint32_t uptime_ms;
} cslp_status_snapshot_t;

uint16_t cslp_read_u16be(const uint8_t *bytes);
uint32_t cslp_read_u32be(const uint8_t *bytes);
uint64_t cslp_read_u64be(const uint8_t *bytes);
void cslp_write_u16be(uint8_t *bytes, uint16_t value);
void cslp_write_u32be(uint8_t *bytes, uint32_t value);
void cslp_write_u64be(uint8_t *bytes, uint64_t value);

uint32_t cslp_crc32_iso_hdlc(const uint8_t *data, size_t length);
uint32_t cslp_crc32_datagram(const uint8_t *data, size_t length);
cslp_parse_result_t cslp_parse_datagram(const uint8_t *data,
                                        size_t length,
                                        cslp_header_t *header);

size_t cslp_build_message(uint8_t *output,
                          size_t capacity,
                          uint8_t message_type,
                          uint32_t session_id,
                          uint32_t message_seq,
                          uint64_t timestamp_us,
                          uint16_t flags,
                          const uint8_t *payload,
                          uint16_t payload_bytes);

bool cslp_wave_chunk_layout(uint32_t frame_sample_count,
                            uint16_t chunk_index,
                            uint16_t *chunk_count,
                            uint32_t *sample_offset,
                            uint16_t *samples_in_chunk);

size_t cslp_build_wave_chunk(uint8_t *output,
                             size_t capacity,
                             uint32_t session_id,
                             uint32_t message_seq,
                             uint64_t timestamp_us,
                             const cslp_wave_metadata_t *metadata,
                             const int16_t *frame_samples,
                             uint16_t chunk_index);

size_t cslp_build_status(uint8_t *output,
                         size_t capacity,
                         uint32_t session_id,
                         uint32_t message_seq,
                         uint64_t timestamp_us,
                         const cslp_status_snapshot_t *snapshot);

#ifdef __cplusplus
}
#endif

#endif
