#include "cslp_protocol.hpp"

#include <cstring>

namespace cyclescope::cslp {
namespace {

constexpr uint8_t kMagic[4] = {'C', 'S', 'L', 'P'};
constexpr uint32_t kCrcPolynomialReflected = 0xEDB88320U;

bool is_known_message_type(MessageType type)
{
    switch (type) {
    case MessageType::Hello:
    case MessageType::ConfigSet:
    case MessageType::EnablePush:
    case MessageType::DisablePush:
    case MessageType::Status:
    case MessageType::WaveData:
    case MessageType::Error:
    case MessageType::HelloAck:
    case MessageType::ConfigAck:
    case MessageType::EnablePushAck:
    case MessageType::DisablePushAck:
        return true;
    }
    return false;
}

}  // namespace

uint16_t read_be16(const uint8_t *data)
{
    return static_cast<uint16_t>((static_cast<uint16_t>(data[0]) << 8)
                                 | static_cast<uint16_t>(data[1]));
}

uint32_t read_be32(const uint8_t *data)
{
    return (static_cast<uint32_t>(data[0]) << 24)
           | (static_cast<uint32_t>(data[1]) << 16)
           | (static_cast<uint32_t>(data[2]) << 8)
           | static_cast<uint32_t>(data[3]);
}

uint64_t read_be64(const uint8_t *data)
{
    return (static_cast<uint64_t>(read_be32(data)) << 32)
           | static_cast<uint64_t>(read_be32(data + 4));
}

int32_t read_be_i32(const uint8_t *data)
{
    return static_cast<int32_t>(read_be32(data));
}

int16_t read_le_i16(const uint8_t *data)
{
    const uint16_t value = static_cast<uint16_t>(data[0])
                           | (static_cast<uint16_t>(data[1]) << 8);
    return static_cast<int16_t>(value);
}

void write_be16(uint8_t *data, uint16_t value)
{
    data[0] = static_cast<uint8_t>(value >> 8);
    data[1] = static_cast<uint8_t>(value);
}

void write_be32(uint8_t *data, uint32_t value)
{
    data[0] = static_cast<uint8_t>(value >> 24);
    data[1] = static_cast<uint8_t>(value >> 16);
    data[2] = static_cast<uint8_t>(value >> 8);
    data[3] = static_cast<uint8_t>(value);
}

void write_be64(uint8_t *data, uint64_t value)
{
    write_be32(data, static_cast<uint32_t>(value >> 32));
    write_be32(data + 4, static_cast<uint32_t>(value));
}

ParseError decode_common_header(const uint8_t *data, size_t length, CommonHeader *header)
{
    if (data == nullptr || header == nullptr || length < kCommonHeaderBytes) {
        return ParseError::TooShort;
    }
    if (std::memcmp(data, kMagic, sizeof(kMagic)) != 0) {
        return ParseError::BadMagic;
    }
    if (data[4] != kVersion) {
        return ParseError::BadVersion;
    }

    const auto message_type = static_cast<MessageType>(data[5]);
    if (!is_known_message_type(message_type)) {
        return ParseError::BadType;
    }

    const uint16_t header_bytes = read_be16(data + 6);
    const uint16_t payload_bytes = read_be16(data + 24);
    if (header_bytes < kCommonHeaderBytes
        || static_cast<size_t>(header_bytes) + payload_bytes != length) {
        return ParseError::BadLength;
    }
    const uint16_t expected_header_bytes =
        message_type == MessageType::WaveData ? kWaveHeaderBytes : kCommonHeaderBytes;
    if (header_bytes != expected_header_bytes) {
        return ParseError::BadLength;
    }

    const uint16_t flags = read_be16(data + 26);
    if ((message_type == MessageType::WaveData && (flags & ~kWaveFlagMask) != 0)
        || (message_type != MessageType::WaveData && flags != 0)) {
        return ParseError::BadFlags;
    }

    *header = {
        .message_type = message_type,
        .header_bytes = header_bytes,
        .session_id = read_be32(data + 8),
        .message_seq = read_be32(data + 12),
        .timestamp_us = read_be64(data + 16),
        .payload_bytes = payload_bytes,
        .flags = flags,
        .crc32 = read_be32(data + kCrcOffset),
    };
    return ParseError::None;
}

bool decode_wave_header(const uint8_t *data, size_t length, const CommonHeader &common,
                        WaveHeader *wave)
{
    if (data == nullptr || wave == nullptr
        || common.message_type != MessageType::WaveData
        || common.header_bytes != kWaveHeaderBytes
        || length < kWaveHeaderBytes) {
        return false;
    }

    *wave = {
        .frame_id = read_be32(data + 32),
        .chunk_index = read_be16(data + 36),
        .chunk_count = read_be16(data + 38),
        .sample_offset = read_be32(data + 40),
        .samples_in_chunk = read_be16(data + 44),
        .sample_format = data[46],
        .channel_count = data[47],
        .sample_rate_hz = read_be32(data + 48),
        .frame_sample_count = read_be32(data + 52),
        .scale_uv_per_lsb = read_be32(data + 56),
        .offset_uv = read_be_i32(data + 60),
        .config_id = read_be32(data + 64),
        .filter_profile = read_be16(data + 68),
        .calibration_id = read_be16(data + 70),
    };
    return true;
}

uint32_t calculate_crc32(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t index = 0; index < length; ++index) {
        const uint8_t byte = index >= kCrcOffset && index < kCrcOffset + sizeof(uint32_t)
                                 ? 0
                                 : data[index];
        crc ^= byte;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1U) != 0 ? kCrcPolynomialReflected : 0U);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

bool verify_crc32(const uint8_t *data, size_t length, const CommonHeader &header)
{
    return data != nullptr && length >= kCommonHeaderBytes
           && calculate_crc32(data, length) == header.crc32;
}

bool encode_common_header(uint8_t *data, size_t capacity, MessageType message_type,
                          uint16_t header_bytes, uint32_t session_id, uint32_t message_seq,
                          uint64_t timestamp_us, uint16_t payload_bytes, uint16_t flags)
{
    const size_t total_bytes = static_cast<size_t>(header_bytes) + payload_bytes;
    if (data == nullptr || capacity < total_bytes || header_bytes < kCommonHeaderBytes
        || total_bytes > kMaxUdpPayloadBytes || session_id == 0) {
        return false;
    }

    std::memset(data, 0, total_bytes);
    std::memcpy(data, kMagic, sizeof(kMagic));
    data[4] = kVersion;
    data[5] = static_cast<uint8_t>(message_type);
    write_be16(data + 6, header_bytes);
    write_be32(data + 8, session_id);
    write_be32(data + 12, message_seq);
    write_be64(data + 16, timestamp_us);
    write_be16(data + 24, payload_bytes);
    write_be16(data + 26, flags);
    return true;
}

void finalize_crc32(uint8_t *data, size_t length)
{
    write_be32(data + kCrcOffset, 0);
    write_be32(data + kCrcOffset, calculate_crc32(data, length));
}

bool sequence_is_newer(uint32_t candidate, uint32_t reference)
{
    return static_cast<int32_t>(candidate - reference) > 0;
}

bool protocol_self_test()
{
    static constexpr uint8_t kCrcCheck[] = {
        '1', '2', '3', '4', '5', '6', '7', '8', '9',
    };
    static constexpr uint8_t kGoldenWave[] = {
        0x43, 0x53, 0x4C, 0x50, 0x01, 0x20, 0x00, 0x48,
        0x11, 0x22, 0x33, 0x44, 0x01, 0x02, 0x03, 0x04,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x12, 0xD6, 0x87,
        0x00, 0x0A, 0x00, 0x0F, 0x69, 0xDB, 0x20, 0x4C,
        0x00, 0x00, 0x00, 0x2A, 0x00, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x05, 0x01, 0x01,
        0x00, 0x3D, 0xFD, 0x24, 0x00, 0x00, 0x00, 0x05,
        0x00, 0x00, 0x01, 0xE8, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x07, 0x00, 0x01, 0x00, 0x03,
        0x00, 0x80, 0xFF, 0xFF, 0x00, 0x00, 0x01, 0x00,
        0xFF, 0x7F,
    };

    CommonHeader common{};
    WaveHeader wave{};
    return calculate_crc32(kCrcCheck, sizeof(kCrcCheck)) == 0xCBF43926U
           && sizeof(kGoldenWave) == 82
           && decode_common_header(kGoldenWave, sizeof(kGoldenWave), &common) == ParseError::None
           && verify_crc32(kGoldenWave, sizeof(kGoldenWave), common)
           && common.session_id == 0x11223344U
           && common.message_seq == 0x01020304U
           && common.timestamp_us == 1234567U
           && common.payload_bytes == 10
           && decode_wave_header(kGoldenWave, sizeof(kGoldenWave), common, &wave)
           && wave.frame_id == 42
           && wave.chunk_index == 0
           && wave.chunk_count == 1
           && wave.samples_in_chunk == 5
           && wave.sample_rate_hz == 4062500U
           && wave.config_id == 7
           && wave.filter_profile == 1
           && wave.calibration_id == 3
           && read_le_i16(kGoldenWave + kWaveHeaderBytes) == -32768
           && read_le_i16(kGoldenWave + kWaveHeaderBytes + 2) == -1
           && read_le_i16(kGoldenWave + kWaveHeaderBytes + 4) == 0
           && read_le_i16(kGoldenWave + kWaveHeaderBytes + 6) == 1
           && read_le_i16(kGoldenWave + kWaveHeaderBytes + 8) == 32767
           && sequence_is_newer(1U, 0xFFFFFFFFU)
           && !sequence_is_newer(0xFFFFFFFFU, 1U)
           && !sequence_is_newer(42U, 42U);
}

}  // namespace cyclescope::cslp
