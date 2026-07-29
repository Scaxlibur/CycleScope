#pragma once

#include <cstddef>
#include <cstdint>

namespace cyclescope::cslp {

constexpr size_t kCommonHeaderBytes = 32;
constexpr size_t kWaveHeaderBytes = 72;
constexpr size_t kMaxUdpPayloadBytes = 1472;
constexpr size_t kCrcOffset = 28;
constexpr uint8_t kVersion = 1;
constexpr uint8_t kSampleFormatS16Le = 1;

constexpr uint16_t kFlagFirstChunk = 0x0001;
constexpr uint16_t kFlagLastChunk = 0x0002;
constexpr uint16_t kFlagFiltered = 0x0004;
constexpr uint16_t kFlagCalibrated = 0x0008;
constexpr uint16_t kFlagAdcOverrange = 0x0010;
constexpr uint16_t kFlagFifoOverflow = 0x0020;
constexpr uint16_t kFlagTestPattern = 0x0040;
constexpr uint16_t kWaveFlagMask = 0x007F;

constexpr uint32_t kCapabilityLatestFrame = 1U << 0;
constexpr uint32_t kCapabilityS16Le = 1U << 1;
constexpr uint32_t kCapabilityAppCrc32 = 1U << 2;
constexpr uint32_t kCapabilityFilteredData = 1U << 3;
constexpr uint32_t kCapabilityConfigId = 1U << 4;
constexpr uint32_t kRequiredCapabilities = kCapabilityLatestFrame
                                           | kCapabilityS16Le
                                           | kCapabilityAppCrc32
                                           | kCapabilityFilteredData
                                           | kCapabilityConfigId;

enum class MessageType : uint8_t {
    Hello = 0x01,
    ConfigSet = 0x02,
    EnablePush = 0x03,
    DisablePush = 0x04,
    Status = 0x10,
    WaveData = 0x20,
    Error = 0x7F,
    HelloAck = 0x81,
    ConfigAck = 0x82,
    EnablePushAck = 0x83,
    DisablePushAck = 0x84,
};

enum class StatusCode : uint16_t {
    Ok = 0,
    BadVersion = 1,
    BadLength = 2,
    BadConfig = 3,
    Unsupported = 4,
    BadState = 5,
    Busy = 6,
    InternalError = 7,
    SequenceConflict = 8,
};

enum class ParseError {
    None,
    TooShort,
    BadMagic,
    BadVersion,
    BadLength,
    BadType,
    BadFlags,
};

struct CommonHeader {
    MessageType message_type;
    uint16_t header_bytes;
    uint32_t session_id;
    uint32_t message_seq;
    uint64_t timestamp_us;
    uint16_t payload_bytes;
    uint16_t flags;
    uint32_t crc32;
};

struct WaveHeader {
    uint32_t frame_id;
    uint16_t chunk_index;
    uint16_t chunk_count;
    uint32_t sample_offset;
    uint16_t samples_in_chunk;
    uint8_t sample_format;
    uint8_t channel_count;
    uint32_t sample_rate_hz;
    uint32_t frame_sample_count;
    uint32_t scale_uv_per_lsb;
    int32_t offset_uv;
    uint32_t config_id;
    uint16_t filter_profile;
    uint16_t calibration_id;
};

uint16_t read_be16(const uint8_t *data);
uint32_t read_be32(const uint8_t *data);
uint64_t read_be64(const uint8_t *data);
int32_t read_be_i32(const uint8_t *data);
int16_t read_le_i16(const uint8_t *data);

void write_be16(uint8_t *data, uint16_t value);
void write_be32(uint8_t *data, uint32_t value);
void write_be64(uint8_t *data, uint64_t value);

ParseError decode_common_header(const uint8_t *data, size_t length, CommonHeader *header);
bool decode_wave_header(const uint8_t *data, size_t length, const CommonHeader &common, WaveHeader *wave);

uint32_t calculate_crc32(const uint8_t *data, size_t length);
bool verify_crc32(const uint8_t *data, size_t length, const CommonHeader &header);
bool encode_common_header(uint8_t *data, size_t capacity, MessageType message_type,
                          uint16_t header_bytes, uint32_t session_id, uint32_t message_seq,
                          uint64_t timestamp_us, uint16_t payload_bytes, uint16_t flags);
void finalize_crc32(uint8_t *data, size_t length);

bool sequence_is_newer(uint32_t candidate, uint32_t reference);
bool protocol_self_test();

}  // namespace cyclescope::cslp
