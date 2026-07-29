#!/usr/bin/env python3
"""Minimal CSLP v0.1 Zynq-side emulator for ESP32-P4 LAN bring-up."""

from __future__ import annotations

import argparse
import contextlib
import io
import secrets
import socket
import struct
import time
import zlib
from dataclasses import dataclass


MAGIC = b"CSLP"
VERSION = 1
COMMON_HEADER_BYTES = 32
WAVE_HEADER_BYTES = 72
MAX_UDP_PAYLOAD_BYTES = 1472
CRC_OFFSET = 28
FLAGS_OFFSET = 26

HELLO = 0x01
CONFIG_SET = 0x02
ENABLE_PUSH = 0x03
STATUS = 0x10
WAVE_DATA = 0x20
HELLO_ACK = 0x81
CONFIG_ACK = 0x82
ENABLE_PUSH_ACK = 0x83

ACK_FOR_REQUEST = {
    HELLO: HELLO_ACK,
    CONFIG_SET: CONFIG_ACK,
    ENABLE_PUSH: ENABLE_PUSH_ACK,
}

STATUS_OK = 0
STATUS_BAD_CONFIG = 3
STATUS_BAD_STATE = 5
STATUS_SEQ_CONFLICT = 8

FLAG_FIRST_CHUNK = 0x0001
FLAG_LAST_CHUNK = 0x0002
FLAG_FILTERED = 0x0004
FLAG_TEST_PATTERN = 0x0040

CAPABILITIES = 0x0000001F
SAMPLE_RATE_HZ = 4_062_500
FRAME_SAMPLE_COUNT = 8_192
FRAME_PERIOD_US = 50_000
SAMPLE_FORMAT_S16_LE = 1
CHANNEL_COUNT = 1
FILTER_PROFILE = 1
SAMPLES_PER_CHUNK = 700
CHUNK_COUNT = 12

COMMON_HEADER = struct.Struct("!4sBBHIIQHHI")
WAVE_HEADER = struct.Struct("!IHHIHBBIIIiIHH")


@dataclass(frozen=True)
class Message:
    message_type: int
    session_id: int
    message_seq: int
    timestamp_us: int
    payload_bytes: int
    flags: int
    payload: bytes


def monotonic_us(start_ns: int) -> int:
    return (time.monotonic_ns() - start_ns) // 1_000


def packet_crc32(packet: bytes) -> int:
    crc_input = bytearray(packet)
    crc_input[CRC_OFFSET : CRC_OFFSET + 4] = b"\0\0\0\0"
    return zlib.crc32(crc_input) & 0xFFFFFFFF


def build_message(
    message_type: int,
    session_id: int,
    message_seq: int,
    timestamp_us: int,
    payload: bytes = b"",
    *,
    flags: int = 0,
    extension: bytes = b"",
) -> bytes:
    header_bytes = COMMON_HEADER_BYTES + len(extension)
    packet = COMMON_HEADER.pack(
        MAGIC,
        VERSION,
        message_type,
        header_bytes,
        session_id,
        message_seq,
        timestamp_us,
        len(payload),
        flags,
        0,
    ) + extension + payload
    if len(packet) > MAX_UDP_PAYLOAD_BYTES:
        raise ValueError(f"CSLP datagram exceeds {MAX_UDP_PAYLOAD_BYTES} bytes")
    crc = packet_crc32(packet)
    return packet[:CRC_OFFSET] + struct.pack("!I", crc) + packet[CRC_OFFSET + 4 :]


def parse_message(packet: bytes) -> Message:
    if len(packet) < COMMON_HEADER_BYTES:
        raise ValueError("datagram is shorter than the common header")
    (
        magic,
        version,
        message_type,
        header_bytes,
        session_id,
        message_seq,
        timestamp_us,
        payload_bytes,
        flags,
        crc,
    ) = COMMON_HEADER.unpack_from(packet)
    if magic != MAGIC or version != VERSION:
        raise ValueError("bad CSLP magic or version")
    if header_bytes != COMMON_HEADER_BYTES:
        raise ValueError("control request has an unexpected header size")
    if header_bytes + payload_bytes != len(packet):
        raise ValueError("CSLP length fields do not match the UDP datagram")
    if flags != 0:
        raise ValueError("control request has non-zero flags")
    if session_id == 0:
        raise ValueError("session_id must be non-zero")
    if packet_crc32(packet) != crc:
        raise ValueError("CSLP CRC mismatch")
    return Message(
        message_type=message_type,
        session_id=session_id,
        message_seq=message_seq,
        timestamp_us=timestamp_us,
        payload_bytes=payload_bytes,
        flags=flags,
        payload=packet[header_bytes:],
    )


def build_wave_packet(
    session_id: int,
    message_seq: int,
    frame_id: int,
    frame_timestamp_us: int,
    chunk_index: int,
    config_id: int,
) -> bytes:
    sample_offset = chunk_index * SAMPLES_PER_CHUNK
    samples_in_chunk = min(SAMPLES_PER_CHUNK, FRAME_SAMPLE_COUNT - sample_offset)
    samples = tuple(((sample_offset + index) % 4096) - 2048 for index in range(samples_in_chunk))
    payload = struct.pack(f"<{samples_in_chunk}h", *samples)
    extension = WAVE_HEADER.pack(
        frame_id,
        chunk_index,
        CHUNK_COUNT,
        sample_offset,
        samples_in_chunk,
        SAMPLE_FORMAT_S16_LE,
        CHANNEL_COUNT,
        SAMPLE_RATE_HZ,
        FRAME_SAMPLE_COUNT,
        488,
        0,
        config_id,
        FILTER_PROFILE,
        0,
    )
    flags = FLAG_FILTERED | FLAG_TEST_PATTERN
    if chunk_index == 0:
        flags |= FLAG_FIRST_CHUNK
    if chunk_index + 1 == CHUNK_COUNT:
        flags |= FLAG_LAST_CHUNK
    return build_message(
        WAVE_DATA,
        session_id,
        message_seq,
        frame_timestamp_us,
        payload,
        flags=flags,
        extension=extension,
    )


def replace_wave_flags(packet: bytes, flags: int) -> bytes:
    """Return a valid-CRC copy with different frame-level flags."""
    rewritten = bytearray(packet)
    struct.pack_into("!H", rewritten, FLAGS_OFFSET, flags)
    rewritten[CRC_OFFSET : CRC_OFFSET + 4] = b"\0\0\0\0"
    struct.pack_into("!I", rewritten, CRC_OFFSET, zlib.crc32(rewritten) & 0xFFFFFFFF)
    return bytes(rewritten)


def corrupt_wave_payload(packet: bytes) -> bytes:
    """Flip one payload bit without updating CRC, as an on-wire corruption."""
    corrupted = bytearray(packet)
    corrupted[WAVE_HEADER_BYTES] ^= 0x01
    return bytes(corrupted)


class CslpFpgaEmulator:
    def __init__(self, bind_ip: str, port: int, peer_ip: str, peer_port: int) -> None:
        self.bind_address = (bind_ip, port)
        self.expected_peer = (peer_ip, peer_port)
        self.boot_start_ns = time.monotonic_ns()
        self.boot_id = secrets.randbits(32) or 1
        self.config_id = secrets.randbits(32) or 1
        self.session_id = 0
        self.peer_address = self.expected_peer
        self.response_cache: dict[tuple[int, int, int], tuple[bytes, bytes]] = {}
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.bind_address)
        self.socket.settimeout(1.0)

    def close(self) -> None:
        self.socket.close()

    def send_response(self, request: Message, response_type: int, payload: bytes) -> None:
        key = (request.session_id, request.message_type, request.message_seq)
        cached = self.response_cache.get(key)
        if cached is not None:
            cached_payload, cached_response = cached
            if cached_payload == request.payload:
                self.socket.sendto(cached_response, self.peer_address)
                print(f"replayed response type=0x{response_type:02X} seq={request.message_seq}", flush=True)
                return
            conflict = build_message(
                response_type,
                request.session_id,
                request.message_seq,
                monotonic_us(self.boot_start_ns),
                self.conflict_payload(response_type),
            )
            self.socket.sendto(conflict, self.peer_address)
            print(f"rejected sequence conflict type=0x{response_type:02X} seq={request.message_seq}", flush=True)
            return

        response = build_message(
            response_type,
            request.session_id,
            request.message_seq,
            monotonic_us(self.boot_start_ns),
            payload,
        )
        self.response_cache[key] = (request.payload, response)
        self.socket.sendto(response, self.peer_address)

    def handle_cached_request(self, request: Message) -> bool:
        key = (request.session_id, request.message_type, request.message_seq)
        if key not in self.response_cache:
            return False
        response_type = ACK_FOR_REQUEST.get(request.message_type)
        if response_type is None:
            raise RuntimeError(f"cached unsupported control type 0x{request.message_type:02X}")
        self.send_response(request, response_type, b"")
        return True

    @staticmethod
    def conflict_payload(response_type: int) -> bytes:
        if response_type == HELLO_ACK:
            return struct.pack("!HBBIII", STATUS_SEQ_CONFLICT, 0, 0, 0, 0, 0)
        if response_type == CONFIG_ACK:
            return struct.pack("!HHIIIIBBHI", STATUS_SEQ_CONFLICT, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        return struct.pack("!HH", STATUS_SEQ_CONFLICT, 0)

    def receive_request(self, deadline: float) -> tuple[bytes, Message, tuple[str, int]]:
        while time.monotonic() < deadline:
            try:
                packet, address = self.socket.recvfrom(MAX_UDP_PAYLOAD_BYTES + 1)
            except socket.timeout:
                continue
            if address != self.expected_peer:
                print(f"ignored unexpected source {address[0]}:{address[1]}", flush=True)
                continue
            try:
                message = parse_message(packet)
            except ValueError as error:
                print(f"ignored invalid CSLP request: {error}", flush=True)
                continue
            return packet, message, address
        raise TimeoutError("timed out waiting for an ESP32-P4 control request")

    def handshake(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        state = HELLO
        while state != WAVE_DATA:
            _packet, request, address = self.receive_request(deadline)
            self.peer_address = address

            if self.handle_cached_request(request):
                continue

            if request.message_type == HELLO:
                if request.payload_bytes != 8:
                    continue
                data_port, max_udp_payload, capabilities = struct.unpack("!HHI", request.payload)
                status = STATUS_OK
                if (
                    data_port != self.expected_peer[1]
                    or max_udp_payload != MAX_UDP_PAYLOAD_BYTES
                    or capabilities != CAPABILITIES
                ):
                    status = STATUS_BAD_CONFIG
                self.session_id = request.session_id
                self.config_id = secrets.randbits(32) or 1
                self.response_cache.clear()
                ack_payload = (
                    struct.pack(
                        "!HBBIII",
                        STATUS_OK,
                        VERSION,
                        0,
                        CAPABILITIES,
                        FRAME_SAMPLE_COUNT,
                        self.boot_id,
                    )
                    if status == STATUS_OK
                    else struct.pack("!HBBIII", status, 0, 0, 0, 0, 0)
                )
                self.send_response(request, HELLO_ACK, ack_payload)
                print(
                    f"HELLO session=0x{self.session_id:08X} seq={request.message_seq} "
                    f"port={data_port} mtu={max_udp_payload} caps=0x{capabilities:08X}",
                    flush=True,
                )
                state = CONFIG_SET if status == STATUS_OK else HELLO
                continue

            if request.session_id != self.session_id:
                continue
            if request.message_type == CONFIG_SET:
                if request.payload_bytes != 20:
                    continue
                config = struct.unpack("!IIIBBHI", request.payload)
                expected = (
                    SAMPLE_RATE_HZ,
                    FRAME_SAMPLE_COUNT,
                    FRAME_PERIOD_US,
                    SAMPLE_FORMAT_S16_LE,
                    CHANNEL_COUNT,
                    FILTER_PROFILE,
                    0,
                )
                status = STATUS_OK if state == CONFIG_SET and config == expected else STATUS_BAD_CONFIG
                ack_payload = (
                    struct.pack(
                        "!HHIIIIBBHI",
                        STATUS_OK,
                        0,
                        self.config_id,
                        SAMPLE_RATE_HZ,
                        FRAME_SAMPLE_COUNT,
                        FRAME_PERIOD_US,
                        SAMPLE_FORMAT_S16_LE,
                        CHANNEL_COUNT,
                        FILTER_PROFILE,
                        FRAME_SAMPLE_COUNT,
                    )
                    if status == STATUS_OK
                    else struct.pack("!HHIIIIBBHI", status, 0, 0, 0, 0, 0, 0, 0, 0, 0)
                )
                self.send_response(request, CONFIG_ACK, ack_payload)
                print(f"CONFIG_SET seq={request.message_seq} values={config}", flush=True)
                state = ENABLE_PUSH if status == STATUS_OK else state
                continue

            if request.message_type == ENABLE_PUSH:
                status = STATUS_OK if state == ENABLE_PUSH and request.payload_bytes == 0 else STATUS_BAD_STATE
                self.send_response(
                    request,
                    ENABLE_PUSH_ACK,
                    struct.pack("!HH", status, 0),
                )
                print(f"ENABLE_PUSH seq={request.message_seq} status={status}", flush=True)
                state = WAVE_DATA if status == STATUS_OK else state

        print(
            f"session ready: session=0x{self.session_id:08X} "
            f"boot_id=0x{self.boot_id:08X} config_id=0x{self.config_id:08X}",
            flush=True,
        )

    def send_status(self, sequence: int, last_frame_id: int, frames_sent: int, packets_sent: int) -> None:
        uptime_ms = monotonic_us(self.boot_start_ns) // 1_000
        payload = struct.pack(
            "!HHIIIIIIIII",
            2,
            0,
            self.config_id,
            last_frame_id,
            frames_sent,
            packets_sent,
            0,
            0,
            0,
            uptime_ms & 0xFFFFFFFF,
            0,
        )
        packet = build_message(
            STATUS,
            self.session_id,
            sequence,
            monotonic_us(self.boot_start_ns),
            payload,
        )
        self.socket.sendto(packet, self.peer_address)

    def send_frames(self, frame_count: int, chunk_gap_us: int, hold_seconds: float) -> None:
        wave_sequence = secrets.randbits(32)
        status_sequence = secrets.randbits(32)
        frames_sent = 0
        packets_sent = 0
        next_status = time.monotonic()
        next_frame = time.monotonic()

        for frame_id in range(1, frame_count + 1):
            now = time.monotonic()
            if now < next_frame:
                time.sleep(next_frame - now)
            frame_start = time.monotonic()
            frame_timestamp_us = monotonic_us(self.boot_start_ns)

            if frame_start >= next_status:
                status_sequence = (status_sequence + 1) & 0xFFFFFFFF
                self.send_status(status_sequence, frames_sent, frames_sent, packets_sent)
                next_status = frame_start + 0.5

            for chunk_index in range(CHUNK_COUNT):
                wave_sequence = (wave_sequence + 1) & 0xFFFFFFFF
                packet = build_wave_packet(
                    self.session_id,
                    wave_sequence,
                    frame_id,
                    frame_timestamp_us,
                    chunk_index,
                    self.config_id,
                )
                self.socket.sendto(packet, self.peer_address)
                packets_sent += 1
                if chunk_index + 1 < CHUNK_COUNT:
                    time.sleep(chunk_gap_us / 1_000_000)

            frames_sent += 1
            next_frame = frame_start + FRAME_PERIOD_US / 1_000_000
            if frame_id == 1 or frame_id % 25 == 0 or frame_id == frame_count:
                print(f"sent frame={frame_id} packets={packets_sent}", flush=True)

        hold_deadline = time.monotonic() + hold_seconds
        while time.monotonic() < hold_deadline:
            status_sequence = (status_sequence + 1) & 0xFFFFFFFF
            self.send_status(status_sequence, frames_sent, frames_sent, packets_sent)
            time.sleep(min(0.5, max(0.0, hold_deadline - time.monotonic())))

        print(f"completed frames={frames_sent} wave_packets={packets_sent}", flush=True)

    def send_fault_suite(self, chunk_gap_us: int, hold_seconds: float) -> None:
        """Inject the deterministic frame faults required by CSLP v0.1 section 21.5."""
        wave_sequence = secrets.randbits(32)
        status_sequence = secrets.randbits(32)
        packets_sent = 0
        completed_frames_sent = 0

        status_sequence = (status_sequence + 1) & 0xFFFFFFFF
        self.send_status(status_sequence, 0, 0, 0)

        def send_frame(
            frame_id: int,
            *,
            order: tuple[int, ...] = tuple(range(CHUNK_COUNT)),
            skip_chunk: int | None = None,
            duplicate_chunk: int | None = None,
            corrupt_chunk: int | None = None,
            conflict_flags_chunk: int | None = None,
            config_id: int | None = None,
        ) -> None:
            nonlocal wave_sequence, packets_sent
            frame_timestamp_us = monotonic_us(self.boot_start_ns)
            selected_config_id = self.config_id if config_id is None else config_id
            for chunk_index in order:
                if chunk_index == skip_chunk:
                    continue
                wave_sequence = (wave_sequence + 1) & 0xFFFFFFFF
                packet = build_wave_packet(
                    self.session_id,
                    wave_sequence,
                    frame_id,
                    frame_timestamp_us,
                    chunk_index,
                    selected_config_id,
                )
                if chunk_index == conflict_flags_chunk:
                    flags = struct.unpack_from("!H", packet, FLAGS_OFFSET)[0]
                    packet = replace_wave_flags(packet, flags ^ FLAG_TEST_PATTERN)
                if chunk_index == corrupt_chunk:
                    packet = corrupt_wave_payload(packet)
                self.socket.sendto(packet, self.peer_address)
                packets_sent += 1
                if chunk_index == duplicate_chunk:
                    self.socket.sendto(packet, self.peer_address)
                    packets_sent += 1
                time.sleep(chunk_gap_us / 1_000_000)

        # Missing chunk: frame 1 must be abandoned when frame 2 arrives.
        send_frame(1, skip_chunk=5)
        send_frame(2)
        completed_frames_sent += 1

        # Swap chunks 1 and 2: frame 3 must still complete.
        out_of_order = (0, 2, 1, *range(3, CHUNK_COUNT))
        send_frame(3, order=out_of_order)
        completed_frames_sent += 1

        # Identical duplicate: count it once without rejecting frame 4.
        send_frame(4, duplicate_chunk=5)
        completed_frames_sent += 1

        # Bad CRC leaves frame 5 incomplete; frame 6 must replace it and complete.
        send_frame(5, corrupt_chunk=5)
        send_frame(6)
        completed_frames_sent += 1

        # Valid CRC but conflicting shared flags invalidates frame 7.
        send_frame(7, conflict_flags_chunk=1)
        send_frame(8)
        completed_frames_sent += 1

        # One old-config chunk must be rejected without contaminating frame 10.
        old_config_id = (self.config_id - 1) & 0xFFFFFFFF
        if old_config_id == 0:
            old_config_id = 0xFFFFFFFF
        send_frame(9, order=(0,), config_id=old_config_id)
        send_frame(10)
        completed_frames_sent += 1

        # A late chunk from an already superseded frame must be stale.
        send_frame(8, order=(0,))

        print(
            "fault suite sent: expected completed=6 incomplete=3 duplicate=1 "
            "stale=1 crc=1 config=1 metadata=1",
            flush=True,
        )

        hold_deadline = time.monotonic() + hold_seconds
        while time.monotonic() < hold_deadline:
            status_sequence = (status_sequence + 1) & 0xFFFFFFFF
            self.send_status(
                status_sequence,
                10,
                completed_frames_sent,
                packets_sent,
            )
            time.sleep(min(0.5, max(0.0, hold_deadline - time.monotonic())))

        print(
            f"completed fault suite valid_frames={completed_frames_sent} "
            f"wave_packets={packets_sent}",
            flush=True,
        )


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, packet: bytes, address: tuple[str, int]) -> None:
        self.sent.append((packet, address))


def make_offline_emulator() -> CslpFpgaEmulator:
    emulator = object.__new__(CslpFpgaEmulator)
    emulator.expected_peer = ("192.0.2.3", 50001)
    emulator.peer_address = emulator.expected_peer
    emulator.boot_start_ns = time.monotonic_ns()
    emulator.boot_id = 0x12345678
    emulator.config_id = 0x87654321
    emulator.session_id = 0
    emulator.response_cache = {}
    emulator.socket = FakeSocket()
    return emulator


def response_status(packet: bytes) -> int:
    response = parse_message(packet)
    return struct.unpack_from("!H", response.payload)[0]


def test_idempotency_cache() -> None:
    emulator = make_offline_emulator()
    payload_a = struct.pack("!HHI", 50001, MAX_UDP_PAYLOAD_BYTES, CAPABILITIES)
    payload_b = struct.pack("!HHI", 50001, MAX_UDP_PAYLOAD_BYTES, CAPABILITIES ^ 1)
    request_a = parse_message(build_message(HELLO, 1, 7, 10, payload_a))
    request_a_retry = parse_message(build_message(HELLO, 1, 7, 20, payload_a))
    request_b = parse_message(build_message(HELLO, 1, 7, 30, payload_b))
    ok_payload = struct.pack(
        "!HBBIII",
        STATUS_OK,
        VERSION,
        0,
        CAPABILITIES,
        FRAME_SAMPLE_COUNT,
        emulator.boot_id,
    )

    with contextlib.redirect_stdout(io.StringIO()):
        emulator.send_response(request_a, HELLO_ACK, ok_payload)
        emulator.send_response(request_a_retry, HELLO_ACK, ok_payload)
        emulator.send_response(request_b, HELLO_ACK, ok_payload)
        emulator.send_response(request_a_retry, HELLO_ACK, ok_payload)

    responses = [packet for packet, _address in emulator.socket.sent]
    if [response_status(packet) for packet in responses] != [0, 0, 8, 0]:
        raise RuntimeError("idempotency cache returned incorrect statuses")
    if responses[0] != responses[1] or responses[0] != responses[3]:
        raise RuntimeError("idempotency cache did not preserve the first response")
    cached_payload, cached_response = emulator.response_cache[(1, HELLO, 7)]
    if cached_payload != payload_a or cached_response != responses[0]:
        raise RuntimeError("sequence conflict replaced the first cache entry")


def test_handshake_conflict_state() -> None:
    emulator = make_offline_emulator()
    rejected_session_id = 0x10203040
    session_id = 0x10203041
    address = emulator.expected_peer
    bad_hello = build_message(
        HELLO,
        rejected_session_id,
        1,
        1,
        struct.pack("!HHI", 50001, MAX_UDP_PAYLOAD_BYTES, CAPABILITIES ^ 1),
    )
    conflicting_hello = build_message(
        HELLO,
        rejected_session_id,
        1,
        2,
        struct.pack("!HHI", 50001, MAX_UDP_PAYLOAD_BYTES, CAPABILITIES),
    )
    accepted_hello = build_message(
        HELLO,
        session_id,
        2,
        3,
        struct.pack("!HHI", 50001, MAX_UDP_PAYLOAD_BYTES, CAPABILITIES),
    )
    expected_config = (
        SAMPLE_RATE_HZ,
        FRAME_SAMPLE_COUNT,
        FRAME_PERIOD_US,
        SAMPLE_FORMAT_S16_LE,
        CHANNEL_COUNT,
        FILTER_PROFILE,
        0,
    )
    bad_config = build_message(
        CONFIG_SET,
        session_id,
        3,
        4,
        struct.pack("!IIIBBHI", SAMPLE_RATE_HZ + 1, *expected_config[1:]),
    )
    conflicting_config = build_message(
        CONFIG_SET,
        session_id,
        3,
        5,
        struct.pack("!IIIBBHI", *expected_config),
    )
    accepted_config = build_message(
        CONFIG_SET,
        session_id,
        4,
        6,
        struct.pack("!IIIBBHI", *expected_config),
    )
    bad_enable = build_message(ENABLE_PUSH, session_id, 5, 7, b"\0")
    conflicting_enable = build_message(ENABLE_PUSH, session_id, 5, 8)
    accepted_enable = build_message(ENABLE_PUSH, session_id, 6, 9)
    requests = iter(
        (packet, parse_message(packet), address)
        for packet in (
            bad_hello,
            conflicting_hello,
            accepted_hello,
            bad_config,
            conflicting_config,
            accepted_config,
            bad_enable,
            conflicting_enable,
            accepted_enable,
        )
    )
    emulator.receive_request = lambda _deadline: next(requests)

    with contextlib.redirect_stdout(io.StringIO()):
        emulator.handshake(1.0)

    statuses_by_type = {
        response_type: [
            response_status(packet)
            for packet, _address in emulator.socket.sent
            if parse_message(packet).message_type == response_type
        ]
        for response_type in (HELLO_ACK, CONFIG_ACK, ENABLE_PUSH_ACK)
    }
    if statuses_by_type[HELLO_ACK] != [
        STATUS_BAD_CONFIG,
        STATUS_SEQ_CONFLICT,
        STATUS_OK,
    ]:
        raise RuntimeError(
            f"conflicting HELLO changed handshake state: {statuses_by_type[HELLO_ACK]}"
        )
    config_statuses = statuses_by_type[CONFIG_ACK]
    if config_statuses != [STATUS_BAD_CONFIG, STATUS_SEQ_CONFLICT, STATUS_OK]:
        raise RuntimeError(
            f"conflicting CONFIG_SET changed handshake state: {config_statuses}"
        )
    if statuses_by_type[ENABLE_PUSH_ACK] != [
        STATUS_BAD_STATE,
        STATUS_SEQ_CONFLICT,
        STATUS_OK,
    ]:
        raise RuntimeError(
            "conflicting ENABLE_PUSH changed handshake state: "
            f"{statuses_by_type[ENABLE_PUSH_ACK]}"
        )

    cached_payload, cached_response = emulator.response_cache[
        (session_id, CONFIG_SET, 3)
    ]
    if cached_payload != parse_message(bad_config).payload:
        raise RuntimeError("CONFIG_SET conflict replaced the first request payload")
    if response_status(cached_response) != STATUS_BAD_CONFIG:
        raise RuntimeError("CONFIG_SET conflict replaced the first response")


def self_test() -> None:
    if COMMON_HEADER.size != COMMON_HEADER_BYTES or WAVE_HEADER.size != 40:
        raise RuntimeError("CSLP struct sizes do not match v0.1")
    if (zlib.crc32(b"123456789") & 0xFFFFFFFF) != 0xCBF43926:
        raise RuntimeError("host CRC-32 implementation is incompatible")
    first = build_wave_packet(1, 1, 1, 1, 0, 1)
    last = build_wave_packet(1, 12, 1, 1, CHUNK_COUNT - 1, 1)
    if len(first) != 1472 or len(last) != 1056:
        raise RuntimeError("CSLP WAVE_DATA packet sizes do not match the frozen profile")
    changed_flags = replace_wave_flags(first, FLAG_FILTERED)
    if packet_crc32(changed_flags) != struct.unpack_from("!I", changed_flags, CRC_OFFSET)[0]:
        raise RuntimeError("fault-injection flag rewrite produced an invalid CRC")
    corrupted = corrupt_wave_payload(first)
    if packet_crc32(corrupted) == struct.unpack_from("!I", corrupted, CRC_OFFSET)[0]:
        raise RuntimeError("fault-injection payload corruption did not break CRC")
    test_idempotency_cache()
    test_handshake_conflict_state()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-ip", default="192.168.10.2")
    parser.add_argument("--port", type=int, default=50000)
    parser.add_argument("--peer-ip", default="192.168.10.3")
    parser.add_argument("--peer-port", type=int, default=50001)
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--chunk-gap-us", type=int, default=150)
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    parser.add_argument("--handshake-timeout", type=float, default=15.0)
    parser.add_argument("--scenario", choices=("normal", "faults"), default="normal")
    parser.add_argument(
        "--self-test-only",
        action="store_true",
        help="run offline protocol/idempotency tests without creating a UDP socket",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    self_test()
    if args.self_test_only:
        print("CSLP emulator self-test passed", flush=True)
        return 0
    if args.frames <= 0 or args.chunk_gap_us < 0 or args.hold_seconds < 0:
        raise SystemExit("frames must be positive; timing arguments must be non-negative")
    emulator = CslpFpgaEmulator(args.bind_ip, args.port, args.peer_ip, args.peer_port)
    print(
        f"listening on {args.bind_ip}:{args.port}; expecting {args.peer_ip}:{args.peer_port}",
        flush=True,
    )
    try:
        emulator.handshake(args.handshake_timeout)
        if args.scenario == "faults":
            emulator.send_fault_suite(args.chunk_gap_us, args.hold_seconds)
        else:
            emulator.send_frames(args.frames, args.chunk_gap_us, args.hold_seconds)
    finally:
        emulator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
