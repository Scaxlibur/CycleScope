#!/usr/bin/env python3
"""Exercise a live CycleScope CSLP sender with strict frame validation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import time
from typing import Any
import zlib


MAGIC = b"CSLP"
VERSION = 1
COMMON_HEADER_BYTES = 32
WAVE_HEADER_BYTES = 72
MAX_UDP_PAYLOAD = 1472
FRAME_SAMPLES = 8192
FULL_CHUNK_SAMPLES = 700
CHUNK_COUNT = 12
SAMPLE_RATE_HZ = 4_062_500
FRAME_PERIOD_US = 50_000
SAMPLE_FORMAT_S16_LE = 1
CHANNEL_COUNT = 1
FILTER_PROFILE = 1
REQUIRED_CAPS = 0x1F
TEST_FAULT_OTR = 0x01
TEST_FAULT_OVERFLOW = 0x02
TEST_FAULT_FRAME_DROP = 0x04
TEST_FAULT_ALL = 0x07

MSG_HELLO = 0x01
MSG_CONFIG_SET = 0x02
MSG_ENABLE_PUSH = 0x03
MSG_DISABLE_PUSH = 0x04
MSG_STATUS = 0x10
MSG_WAVE_DATA = 0x20
MSG_ERROR = 0x7F
MSG_HELLO_ACK = 0x81
MSG_CONFIG_ACK = 0x82
MSG_ENABLE_PUSH_ACK = 0x83
MSG_DISABLE_PUSH_ACK = 0x84

STATUS_OK = 0
STATUS_SEQ_CONFLICT = 8
DEVICE_IDLE = 0
DEVICE_READY = 1

FLAG_FIRST_CHUNK = 0x0001
FLAG_LAST_CHUNK = 0x0002
FLAG_FILTERED = 0x0004
FLAG_CALIBRATED = 0x0008
FLAG_ADC_OVERRANGE = 0x0010
FLAG_FIFO_OVERFLOW = 0x0020
FLAG_TEST_PATTERN = 0x0040
WAVE_ALLOWED_FLAGS = (
    FLAG_FIRST_CHUNK
    | FLAG_LAST_CHUNK
    | FLAG_FILTERED
    | FLAG_CALIBRATED
    | FLAG_ADC_OVERRANGE
    | FLAG_FIFO_OVERFLOW
    | FLAG_TEST_PATTERN
)

ACK_PAYLOAD_BYTES = {
    MSG_HELLO_ACK: 16,
    MSG_CONFIG_ACK: 28,
    MSG_ENABLE_PUSH_ACK: 4,
    MSG_DISABLE_PUSH_ACK: 4,
}

COMMON_HEADER = struct.Struct(">4sBBHIIQHHI")
WAVE_EXTENSION = struct.Struct(">IHHIHBBIIIiIHH")
STATUS_PAYLOAD = struct.Struct(">HHIIIIIIIII")


class ProtocolError(RuntimeError):
    """A received datagram violates CSLP v0.1."""


class ChunkConflictError(ProtocolError):
    """A repeated chunk index carries different sample bytes."""


class WaveFlagError(ProtocolError):
    """WAVE_DATA flags violate the protocol or active test profile."""


class TimestampOrderError(ProtocolError):
    """Frame timestamps did not increase strictly across frames."""


@dataclass(frozen=True)
class Header:
    version: int
    message_type: int
    header_bytes: int
    session_id: int
    message_seq: int
    timestamp_us: int
    payload_bytes: int
    flags: int
    crc32: int


@dataclass(frozen=True)
class ParsedPacket:
    header: Header
    extension: bytes
    payload: bytes
    raw: bytes


@dataclass(frozen=True)
class WaveChunk:
    header: Header
    frame_id: int
    chunk_index: int
    chunk_count: int
    sample_offset: int
    samples_in_chunk: int
    sample_format: int
    channel_count: int
    sample_rate_hz: int
    frame_sample_count: int
    scale_uv_per_lsb: int
    offset_uv: int
    config_id: int
    filter_profile: int
    calibration_id: int
    samples: bytes
    raw_bytes: int

    @property
    def frame_flags(self) -> int:
        return self.header.flags & ~(FLAG_FIRST_CHUNK | FLAG_LAST_CHUNK)

    @property
    def shared_metadata(self) -> tuple[int, ...]:
        return (
            self.header.timestamp_us,
            self.chunk_count,
            self.sample_format,
            self.channel_count,
            self.sample_rate_hz,
            self.frame_sample_count,
            self.scale_uv_per_lsb,
            self.offset_uv,
            self.config_id,
            self.filter_profile,
            self.calibration_id,
            self.frame_flags,
        )


@dataclass
class FrameAssembly:
    frame_id: int
    shared_metadata: tuple[int, ...]
    first_arrival_ns: int
    last_arrival_ns: int
    chunks: dict[int, bytes] = field(default_factory=dict)

    def add(self, chunk: WaveChunk, arrival_ns: int) -> bool:
        if chunk.frame_id != self.frame_id:
            raise ProtocolError("frame id changed inside assembly")
        if chunk.shared_metadata != self.shared_metadata:
            raise ProtocolError(f"frame {self.frame_id} shared metadata conflict")
        previous = self.chunks.get(chunk.chunk_index)
        if previous is not None:
            if previous != chunk.samples:
                raise ChunkConflictError(
                    f"frame {self.frame_id} duplicate chunk conflict"
                )
            return False
        self.chunks[chunk.chunk_index] = chunk.samples
        self.last_arrival_ns = arrival_ns
        return len(self.chunks) == chunk.chunk_count

    def samples_bytes(self) -> bytes:
        return b"".join(self.chunks[index] for index in range(CHUNK_COUNT))


@dataclass
class Counters:
    datagrams_received: int = 0
    datagram_bytes: int = 0
    wave_packets: int = 0
    wave_bytes: int = 0
    status_packets: int = 0
    frames_completed: int = 0
    crc_errors: int = 0
    parse_errors: int = 0
    source_errors: int = 0
    wave_sequence_gaps: int = 0
    wave_sequence_duplicates: int = 0
    wave_sequence_reordered: int = 0
    status_sequence_gaps: int = 0
    status_sequence_duplicates: int = 0
    status_sequence_reordered: int = 0
    status_format_errors: int = 0
    frame_id_gaps: int = 0
    frame_id_reordered: int = 0
    frame_id_duplicates: int = 0
    frame_interleaves: int = 0
    timestamp_order_errors: int = 0
    chunk_duplicates: int = 0
    chunk_conflicts: int = 0
    incomplete_frames: int = 0
    metadata_errors: int = 0
    flag_errors: int = 0
    packet_size_errors: int = 0
    sample_shape_errors: int = 0
    adc_overrange_wave_frames: int = 0
    fifo_overflow_wave_frames: int = 0
    socket_overflow: int = 0
    control_retries: int = 0
    control_replays: int = 0
    control_seq_conflicts: int = 0
    post_disable_wave_packets: int = 0


def crc32_datagram(data: bytes) -> int:
    if len(data) < COMMON_HEADER_BYTES:
        raise ProtocolError("datagram is shorter than common header")
    normalized = data[:28] + b"\x00\x00\x00\x00" + data[32:]
    return zlib.crc32(normalized) & 0xFFFFFFFF


def build_message(
    message_type: int,
    session_id: int,
    message_seq: int,
    payload: bytes = b"",
    timestamp_us: int | None = None,
) -> bytes:
    if timestamp_us is None:
        timestamp_us = time.monotonic_ns() // 1000
    header = COMMON_HEADER.pack(
        MAGIC,
        VERSION,
        message_type,
        COMMON_HEADER_BYTES,
        session_id,
        message_seq,
        timestamp_us,
        len(payload),
        0,
        0,
    )
    message = bytearray(header + payload)
    struct.pack_into(">I", message, 28, crc32_datagram(message))
    return bytes(message)


def parse_datagram(data: bytes) -> ParsedPacket:
    if len(data) < COMMON_HEADER_BYTES:
        raise ProtocolError("short datagram")
    fields = COMMON_HEADER.unpack_from(data)
    if fields[0] != MAGIC:
        raise ProtocolError("bad magic")
    header = Header(*fields[1:])
    if header.header_bytes < COMMON_HEADER_BYTES:
        raise ProtocolError("header is shorter than common header")
    if header.header_bytes + header.payload_bytes != len(data):
        raise ProtocolError("header/payload length mismatch")
    if crc32_datagram(data) != header.crc32:
        raise ProtocolError("CRC mismatch")
    return ParsedPacket(
        header=header,
        extension=data[COMMON_HEADER_BYTES : header.header_bytes],
        payload=data[header.header_bytes :],
        raw=data,
    )


def validate_control_ack(
    packet: ParsedPacket, expected_type: int, expected_sequence: int
) -> int:
    expected_payload_bytes = ACK_PAYLOAD_BYTES.get(expected_type)
    header = packet.header
    if expected_payload_bytes is None:
        raise ValueError(f"unknown control ACK type 0x{expected_type:02x}")
    if header.message_type != expected_type or header.message_seq != expected_sequence:
        raise ProtocolError("control ACK type/sequence mismatch")
    if (
        header.header_bytes != COMMON_HEADER_BYTES
        or packet.extension
        or header.flags != 0
        or len(packet.payload) != expected_payload_bytes
    ):
        raise ProtocolError("control ACK header/flags/length mismatch")
    if expected_type == MSG_HELLO_ACK:
        reserved = packet.payload[3]
    else:
        reserved = int.from_bytes(packet.payload[2:4], "big")
    if reserved != 0:
        raise ProtocolError("control ACK reserved field is nonzero")
    status = struct.unpack_from(">H", packet.payload)[0]
    if status > 8:
        raise ProtocolError(f"control ACK has unknown status {status}")
    if status != STATUS_OK and any(packet.payload[2:]):
        raise ProtocolError("failed control ACK has nonzero result fields")
    return status


def observe_sequence(last: int | None, current: int) -> tuple[int, int, int, int]:
    """Return new high-water mark and gap/duplicate/reorder increments."""
    if last is None:
        return current, 0, 0, 0
    delta = (current - last) & 0xFFFFFFFF
    if delta == 0:
        return last, 0, 1, 0
    if delta == 1:
        return current, 0, 0, 0
    if delta < 0x80000000:
        return current, delta - 1, 0, 0
    return last, 0, 0, 1


def frame_id_forward_distance(previous: int, current: int) -> int:
    """Return distance in the cyclic nonzero-u32 frame_id sequence."""
    if not 1 <= previous <= 0xFFFFFFFF or not 1 <= current <= 0xFFFFFFFF:
        raise ValueError("frame_id must be a nonzero u32")
    if current >= previous:
        return current - previous
    return (0xFFFFFFFF - previous) + current


def parse_wave(packet: ParsedPacket) -> WaveChunk:
    header = packet.header
    if header.message_type != MSG_WAVE_DATA or header.header_bytes != WAVE_HEADER_BYTES:
        raise ProtocolError("not a 72-byte WAVE_DATA packet")
    if len(packet.extension) != WAVE_EXTENSION.size:
        raise ProtocolError("bad WAVE_DATA extension length")
    if header.flags & ~WAVE_ALLOWED_FLAGS:
        raise WaveFlagError("WAVE_DATA reserved flag is nonzero")
    values = WAVE_EXTENSION.unpack(packet.extension)
    chunk = WaveChunk(header, *values, packet.payload, len(packet.raw))
    if chunk.frame_id == 0:
        raise ProtocolError("WAVE_DATA frame_id must be nonzero")
    if chunk.scale_uv_per_lsb == 0:
        raise ProtocolError("WAVE_DATA scale_uV_per_lsb must be nonzero")
    expected_chunks = (
        chunk.frame_sample_count + FULL_CHUNK_SAMPLES - 1
    ) // FULL_CHUNK_SAMPLES
    if chunk.chunk_count != expected_chunks:
        raise ProtocolError("unexpected chunk count")
    if chunk.chunk_index >= chunk.chunk_count:
        raise ProtocolError("chunk index out of range")
    expected_offset = chunk.chunk_index * FULL_CHUNK_SAMPLES
    expected_samples = min(
        FULL_CHUNK_SAMPLES, chunk.frame_sample_count - expected_offset
    )
    if (
        chunk.sample_offset != expected_offset
        or chunk.samples_in_chunk != expected_samples
    ):
        raise ProtocolError("chunk layout mismatch")
    if len(chunk.samples) != chunk.samples_in_chunk * chunk.channel_count * 2:
        raise ProtocolError("sample payload length mismatch")
    if bool(header.flags & FLAG_FIRST_CHUNK) != (chunk.chunk_index == 0):
        raise ProtocolError("FIRST_CHUNK mismatch")
    if bool(header.flags & FLAG_LAST_CHUNK) != (
        chunk.chunk_index + 1 == chunk.chunk_count
    ):
        raise ProtocolError("LAST_CHUNK mismatch")
    return chunk


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def monotonic_latency_us(start_ns: int | None, finish_ns: int | None) -> float | None:
    if start_ns is None or finish_ns is None:
        return None
    if finish_ns < start_ns:
        raise ProtocolError("monotonic response timestamp moved backwards")
    return (finish_ns - start_ns) / 1000.0


def read_nic_stats(interface: str) -> dict[str, int]:
    root = Path("/sys/class/net") / interface / "statistics"
    names = ("rx_packets", "rx_bytes", "rx_dropped", "rx_errors")
    try:
        return {name: int((root / name).read_text().strip()) for name in names}
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"cannot read NIC statistics for {interface}") from error


def read_interface_ipv4(interface: str) -> list[tuple[ipaddress.IPv4Address, int]]:
    try:
        socket.if_nametoindex(interface)
    except OSError as error:
        raise RuntimeError(f"network interface does not exist: {interface}") from error
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "address", "show", "dev", interface],
            check=True,
            capture_output=True,
            text=True,
        )
        records = json.loads(result.stdout)
        return [
            (ipaddress.IPv4Address(info["local"]), int(info["prefixlen"]))
            for record in records
            for info in record.get("addr_info", [])
            if info.get("family") == "inet"
        ]
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ) as error:
        raise RuntimeError(f"cannot inspect IPv4 addresses on {interface}") from error


def validate_network_configuration(args: argparse.Namespace) -> None:
    local = ipaddress.IPv4Address(args.local_ip)
    remote = ipaddress.IPv4Address(args.remote_ip)
    configured = read_interface_ipv4(args.interface)
    matching_networks = [
        ipaddress.IPv4Network((address, prefix), strict=False)
        for address, prefix in configured
        if address == local
    ]
    if not matching_networks:
        assigned = ", ".join(str(address) for address, _prefix in configured) or "none"
        raise RuntimeError(
            f"local IP {local} is not assigned to {args.interface}; "
            f"assigned IPv4 addresses: {assigned}"
        )
    if not any(remote in network for network in matching_networks):
        raise RuntimeError(
            f"remote IP {remote} is not on-link with {local} on {args.interface}"
        )
    if any(
        remote in (network.network_address, network.broadcast_address)
        for network in matching_networks
    ):
        raise RuntimeError(f"remote IP {remote} is a network/broadcast address")


def validation_scope() -> dict[str, list[str]]:
    return {
        "validated_here": [
            "CSLP control and CRC semantics",
            "WAVE_DATA framing, ordering, metadata, and counters",
            "host UDP delivery and host NIC receive-drop counters",
        ],
        "requires_external_evidence": [
            "wire UDP checksum is nonzero and valid (pcap)",
            "IPv4 fragmentation is absent (pcap)",
            "wire packet-start spacing is at least 150 us (pcap)",
            "Zynq PHY/GEM speed and hardware counters (board logs/registers)",
        ],
    }


def deferred_disable_count_failures(
    target_frames: int,
    frames_completed: int,
    wave_packets: int,
) -> list[str]:
    expected_frames = target_frames + 1
    failures: list[str] = []
    if frames_completed != expected_frames:
        failures.append(
            f"deferred DISABLE expected {expected_frames} frames, got {frames_completed}"
        )
    if wave_packets != frames_completed * CHUNK_COUNT:
        failures.append("wave packet/frame ratio is not 12")
    return failures


def overrange_policy_failures(
    policy: str,
    wave_frames: int,
    device_frames: int | None,
) -> list[str]:
    failures: list[str] = []
    if policy == "reject":
        if wave_frames:
            failures.append(f"adc_overrange_wave_frames={wave_frames}")
        if device_frames not in (None, 0):
            failures.append(f"device adc_overrange_frames increased by {device_frames}")
        return failures
    if policy == "require" and wave_frames == 0:
        failures.append("ADC overrange was required but no WAVE frame asserted it")
    if device_frames is not None and device_frames != wave_frames:
        failures.append(
            "device/WAVE ADC overrange frame count mismatch: "
            f"device={device_frames} wave={wave_frames}"
        )
    return failures


def test_fault_policy_failures(
    fault_mask: int,
    frames_completed: int,
    wave_overrange: int,
    wave_overflow: int,
    device_overrange: int | None,
    device_overflow: int | None,
    device_drops: int | None,
    timestamp_intervals_ms: list[float],
) -> list[str]:
    """Validate the deterministic one-shot diagnostics armed after frame one."""
    failures: list[str] = []
    if fault_mask & TEST_FAULT_OTR:
        if wave_overrange != 1:
            failures.append(
                f"expected exactly one injected OTR WAVE frame, got {wave_overrange}"
            )
        if device_overrange != 1:
            failures.append(
                f"expected device adc_overrange_frames delta 1, got {device_overrange}"
            )

    expected_overflow_frames = (
        max(frames_completed - 1, 0)
        if fault_mask & TEST_FAULT_OVERFLOW
        else 0
    )
    if wave_overflow != expected_overflow_frames:
        failures.append(
            "injected overflow WAVE count mismatch: "
            f"expected={expected_overflow_frames} actual={wave_overflow}"
        )
    if device_overflow is not None and device_overflow != expected_overflow_frames:
        failures.append(
            "device injected overflow count mismatch: "
            f"expected={expected_overflow_frames} actual={device_overflow}"
        )

    expected_drops = 1 if fault_mask & TEST_FAULT_FRAME_DROP else 0
    if device_drops is not None and device_drops != expected_drops:
        failures.append(
            "device injected frame-drop count mismatch: "
            f"expected={expected_drops} actual={device_drops}"
        )
    if expected_drops:
        long_gaps = [value for value in timestamp_intervals_ms if 99.0 <= value <= 101.0]
        invalid_gaps = [
            value
            for value in timestamp_intervals_ms
            if not (49.0 <= value <= 51.0 or 99.0 <= value <= 101.0)
        ]
        if len(long_gaps) != 1:
            failures.append(
                "injected frame drop expected one 100 ms timestamp gap, got "
                f"{len(long_gaps)}"
            )
        if invalid_gaps:
            failures.append(
                f"injected frame drop produced invalid timestamp gaps: {invalid_gaps}"
            )
    return failures


class StressClient:
    def __init__(self, args: argparse.Namespace) -> None:
        validate_network_configuration(args)
        self.args = args
        self.session_id = secrets.randbits(32) or 1
        self.control_seq = 1
        self.config_id = 0
        self.device_boot_id = 0
        self.enabled = False
        self.disable_ack_ns: int | None = None
        self.counters = Counters()
        self.assemblies: dict[int, FrameAssembly] = {}
        self.active_frame_id: int | None = None
        self.completed_frame_ids: set[int] = set()
        self.last_wave_seq: int | None = None
        self.last_status_seq: int | None = None
        self.last_frame_id: int | None = None
        self.last_frame_timestamp_us: int | None = None
        self.enable_request_started_ns: int | None = None
        self.enable_ack_ns: int | None = None
        self.first_wave_ns: int | None = None
        self.first_complete_ns: int | None = None
        self.last_complete_ns: int | None = None
        self.last_wave_arrival_ns: int | None = None
        self.last_status_arrival_ns: int | None = None
        self.wave_intervals_us: list[float] = []
        self.frame_intervals_ms: list[float] = []
        self.frame_timestamp_intervals_ms: list[float] = []
        self.status_intervals_ms: list[float] = []
        self.status_snapshots: list[dict[str, int]] = []
        self.baseline_status: dict[str, int] | None = None
        self.sample_min = 32767
        self.sample_max = -32768
        self.sample_values: set[int] = set()
        self.wave_metadata_identity: tuple[int, int, int] | None = None
        self.packet_sizes: dict[int, int] = {}
        self.run_started_ns = 0
        self.run_finished_ns = 0
        self.handshake_complete = False
        self.runtime_failures: list[str] = []
        self.disable_ack_latency_us: float | None = None
        self.frames_completed_at_disable_ack: int | None = None
        self.disable_trigger_frame_id: int | None = None
        self.nic_before = read_nic_stats(args.interface)
        self.nic_after = dict(self.nic_before)
        self.capture_dir: Path | None = args.capture_dir
        self.capture_frames: list[dict[str, int | str]] = []
        if self.capture_dir is not None:
            try:
                self.capture_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError as error:
                raise RuntimeError(
                    f"capture directory already exists: {self.capture_dir}"
                ) from error

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.receive_buffer)
        self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_RXQ_OVFL", 40), 1)
        self.socket.bind((args.local_ip, args.local_port))
        self.remote = (args.remote_ip, args.remote_port)
        self.actual_receive_buffer = self.socket.getsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF
        )

    def close(self) -> None:
        self.socket.close()

    def record_capture_frame(
        self, assembly: FrameAssembly, samples_bytes: bytes
    ) -> None:
        capture_dir = getattr(self, "capture_dir", None)
        if capture_dir is None:
            return
        frame_index = len(self.capture_frames)
        file_name = f"frame_{frame_index:05d}_{assembly.frame_id:08x}.s16le"
        frame_path = capture_dir / file_name
        frame_path.write_bytes(samples_bytes)
        metadata = assembly.shared_metadata
        record: dict[str, int | str] = {
            "frame_index": frame_index,
            "frame_id": assembly.frame_id,
            "timestamp_us": metadata[0],
            "file": file_name,
            "frame_bytes": len(samples_bytes),
            "sample_count": metadata[5],
            "sample_rate_hz": metadata[4],
            "scale_uv_per_lsb": metadata[6],
            "offset_uv": metadata[7],
            "config_id": metadata[8],
            "filter_profile": metadata[9],
            "calibration_id": metadata[10],
            "frame_flags": metadata[11],
            "sha256": hashlib.sha256(samples_bytes).hexdigest(),
        }
        self.capture_frames.append(record)

    def next_control_seq(self) -> int:
        sequence = self.control_seq
        self.control_seq = (self.control_seq + 1) & 0xFFFFFFFF
        return sequence

    def receive(self, timeout: float) -> tuple[ParsedPacket, int]:
        self.socket.settimeout(timeout)
        data, ancillary, _flags, source = self.socket.recvmsg(2048, 256)
        arrival_ns = time.monotonic_ns()
        if source != self.remote:
            self.counters.source_errors += 1
            raise ProtocolError(f"unexpected source {source}")
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == getattr(
                socket, "SO_RXQ_OVFL", 40
            ):
                self.counters.socket_overflow = max(
                    self.counters.socket_overflow, struct.unpack("I", value[:4])[0]
                )
        self.counters.datagrams_received += 1
        self.counters.datagram_bytes += len(data)
        try:
            packet = parse_datagram(data)
        except ProtocolError as error:
            if "CRC" in str(error):
                self.counters.crc_errors += 1
            else:
                self.counters.parse_errors += 1
            raise
        if packet.header.version != VERSION:
            raise ProtocolError("unexpected protocol version")
        if packet.header.session_id != self.session_id:
            raise ProtocolError("unexpected session id")
        return packet, arrival_ns

    def exchange(self, request: bytes, ack_type: int, sequence: int) -> ParsedPacket:
        for attempt in range(self.args.control_retries):
            if attempt:
                self.counters.control_retries += 1
            self.socket.sendto(request, self.remote)
            deadline = time.monotonic() + self.args.control_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    packet, arrival_ns = self.receive(remaining)
                except socket.timeout:
                    break
                if (
                    packet.header.message_type == ack_type
                    and packet.header.message_seq == sequence
                ):
                    validate_control_ack(packet, ack_type, sequence)
                    return packet
                if packet.header.message_type == MSG_ERROR:
                    raise ProtocolError("device returned ERROR")
                self.consume(packet, arrival_ns)
        raise TimeoutError(f"control ACK 0x{ack_type:02x} timed out")

    def transact(
        self, message_type: int, ack_type: int, payload: bytes
    ) -> ParsedPacket:
        sequence = self.next_control_seq()
        request = build_message(message_type, self.session_id, sequence, payload)
        return self.exchange(request, ack_type, sequence)

    @staticmethod
    def ack_status(packet: ParsedPacket) -> int:
        return validate_control_ack(
            packet, packet.header.message_type, packet.header.message_seq
        )

    def handshake(self) -> None:
        hello_payload = struct.pack(
            ">HHI", self.args.local_port, MAX_UDP_PAYLOAD, REQUIRED_CAPS
        )
        hello = self.transact(MSG_HELLO, MSG_HELLO_ACK, hello_payload)
        if self.ack_status(hello) != STATUS_OK or len(hello.payload) != 16:
            raise ProtocolError("HELLO failed")
        selected_version = hello.payload[2]
        caps, max_samples, self.device_boot_id = struct.unpack_from(
            ">III", hello.payload, 4
        )
        if (
            selected_version != VERSION
            or caps != REQUIRED_CAPS
            or max_samples != FRAME_SAMPLES
            or self.device_boot_id == 0
        ):
            raise ProtocolError("HELLO_ACK capability mismatch")

        self.wait_for_idle_status()

        config_payload = struct.pack(
            ">IIIBBHI",
            SAMPLE_RATE_HZ,
            FRAME_SAMPLES,
            FRAME_PERIOD_US,
            SAMPLE_FORMAT_S16_LE,
            CHANNEL_COUNT,
            FILTER_PROFILE,
            0,
        )
        sequence = self.next_control_seq()
        request = build_message(
            MSG_CONFIG_SET, self.session_id, sequence, config_payload
        )
        config = self.exchange(request, MSG_CONFIG_ACK, sequence)
        if self.ack_status(config) != STATUS_OK or len(config.payload) != 28:
            raise ProtocolError("CONFIG_SET failed")
        self.config_id, rate, samples, period = struct.unpack_from(
            ">IIII", config.payload, 4
        )
        sample_format, channels, filter_profile = struct.unpack_from(
            ">BBH", config.payload, 20
        )
        max_frame_samples = struct.unpack_from(">I", config.payload, 24)[0]
        if (
            self.config_id == 0
            or rate != SAMPLE_RATE_HZ
            or samples != FRAME_SAMPLES
            or period != FRAME_PERIOD_US
            or sample_format != SAMPLE_FORMAT_S16_LE
            or channels != CHANNEL_COUNT
            or filter_profile != FILTER_PROFILE
            or max_frame_samples != FRAME_SAMPLES
        ):
            raise ProtocolError("CONFIG_ACK profile mismatch")

        replay = self.exchange(request, MSG_CONFIG_ACK, sequence)
        if replay.raw != config.raw:
            raise ProtocolError("CONFIG_SET idempotent replay changed response")
        self.counters.control_replays += 1

        # Reuse the same transaction key with another otherwise valid payload.
        # The protocol requires SEQ_CONFLICT and forbids applying the request.
        conflict_payload = struct.pack(
            ">IIIBBHI",
            0,
            FRAME_SAMPLES,
            FRAME_PERIOD_US,
            SAMPLE_FORMAT_S16_LE,
            CHANNEL_COUNT,
            FILTER_PROFILE,
            0,
        )
        conflict_request = build_message(
            MSG_CONFIG_SET, self.session_id, sequence, conflict_payload
        )
        conflict = self.exchange(conflict_request, MSG_CONFIG_ACK, sequence)
        if self.ack_status(conflict) != STATUS_SEQ_CONFLICT:
            raise ProtocolError("CONFIG_SET conflicting replay was not rejected")
        self.counters.control_seq_conflicts += 1

        sequence = self.next_control_seq()
        request = build_message(MSG_ENABLE_PUSH, self.session_id, sequence, b"")
        self.enable_request_started_ns = time.monotonic_ns()
        enable = self.exchange(request, MSG_ENABLE_PUSH_ACK, sequence)
        self.enable_ack_ns = time.monotonic_ns()
        if self.ack_status(enable) != STATUS_OK:
            raise ProtocolError("ENABLE_PUSH failed")
        self.enabled = True

    def consume(self, packet: ParsedPacket, arrival_ns: int) -> WaveChunk | None:
        message_type = packet.header.message_type
        if message_type == MSG_WAVE_DATA:
            return self.consume_wave(packet, arrival_ns)
        if message_type == MSG_STATUS:
            self.consume_status(packet, arrival_ns)
            return None
        if message_type in (
            MSG_HELLO_ACK,
            MSG_CONFIG_ACK,
            MSG_ENABLE_PUSH_ACK,
            MSG_DISABLE_PUSH_ACK,
        ):
            return None
        raise ProtocolError(f"unexpected message type 0x{message_type:02x}")

    def consume_wave(self, packet: ParsedPacket, arrival_ns: int) -> WaveChunk | None:
        if self.first_wave_ns is None:
            self.first_wave_ns = arrival_ns
        self.counters.wave_packets += 1
        self.counters.wave_bytes += len(packet.raw)
        self.packet_sizes[len(packet.raw)] = (
            self.packet_sizes.get(len(packet.raw), 0) + 1
        )
        if len(packet.raw) not in (1472, 1056):
            self.counters.packet_size_errors += 1

        sequence = packet.header.message_seq
        self.last_wave_seq, gaps, duplicates, reordered = observe_sequence(
            self.last_wave_seq, sequence
        )
        self.counters.wave_sequence_gaps += gaps
        self.counters.wave_sequence_duplicates += duplicates
        self.counters.wave_sequence_reordered += reordered

        if self.last_wave_arrival_ns is not None:
            self.wave_intervals_us.append(
                (arrival_ns - self.last_wave_arrival_ns) / 1000.0
            )
        self.last_wave_arrival_ns = arrival_ns

        try:
            chunk = parse_wave(packet)
            if (
                self.disable_ack_ns is not None
                and arrival_ns > self.disable_ack_ns
                and chunk.frame_id != self.disable_trigger_frame_id
            ):
                self.counters.post_disable_wave_packets += 1
            if self.args.source_mode == "test-pattern":
                required_flags = FLAG_FILTERED | FLAG_TEST_PATTERN
                forbidden_flags = FLAG_CALIBRATED
                if not self.args.expected_test_faults & TEST_FAULT_OTR:
                    forbidden_flags |= FLAG_ADC_OVERRANGE
                if not self.args.expected_test_faults & TEST_FAULT_OVERFLOW:
                    forbidden_flags |= FLAG_FIFO_OVERFLOW
                mode_description = "synthetic test mode"
            else:
                required_flags = FLAG_FILTERED
                forbidden_flags = FLAG_TEST_PATTERN
                if self.args.expected_calibration_id:
                    required_flags |= FLAG_CALIBRATED
                else:
                    forbidden_flags |= FLAG_CALIBRATED
                mode_description = "real ADC mode"
            if (
                chunk.frame_flags & required_flags != required_flags
                or chunk.frame_flags & forbidden_flags
            ):
                raise WaveFlagError(f"WAVE flags do not match {mode_description}")
            metadata_identity = (
                chunk.calibration_id,
                chunk.scale_uv_per_lsb,
                chunk.offset_uv,
            )
            if self.wave_metadata_identity is None:
                self.wave_metadata_identity = metadata_identity
            elif self.wave_metadata_identity != metadata_identity:
                raise ProtocolError("WAVE calibration metadata changed during run")
            if (
                chunk.config_id != self.config_id
                or chunk.chunk_count != CHUNK_COUNT
                or chunk.calibration_id != self.args.expected_calibration_id
                or (
                    self.args.expected_scale_uv_per_lsb is not None
                    and chunk.scale_uv_per_lsb
                    != self.args.expected_scale_uv_per_lsb
                )
                or (
                    self.args.expected_offset_uv is not None
                    and chunk.offset_uv != self.args.expected_offset_uv
                )
                or chunk.sample_rate_hz != SAMPLE_RATE_HZ
                or chunk.frame_sample_count != FRAME_SAMPLES
                or chunk.sample_format != SAMPLE_FORMAT_S16_LE
                or chunk.channel_count != CHANNEL_COUNT
                or chunk.filter_profile != FILTER_PROFILE
            ):
                raise ProtocolError("WAVE metadata mismatch")

            if (
                self.active_frame_id is not None
                and chunk.frame_id != self.active_frame_id
            ):
                distance = frame_id_forward_distance(
                    self.active_frame_id, chunk.frame_id
                )
                if distance >= 0x80000000:
                    # A late chunk from an older frame cannot contribute to the
                    # active assembly. Drop it without disturbing the new frame.
                    if chunk.frame_id == self.last_frame_id:
                        self.counters.frame_id_duplicates += 1
                    else:
                        self.counters.frame_id_reordered += 1
                    return None
                # Per CSLP section 15, a newer frame supersedes an incomplete
                # assembly. Keep running so the new frame can still be checked,
                # while retaining both counters as a final stress-test failure.
                self.assemblies.pop(self.active_frame_id, None)
                self.counters.incomplete_frames += 1
                self.counters.frame_interleaves += 1
                self.active_frame_id = None
            if self.active_frame_id is None and self.last_frame_id is not None:
                distance = frame_id_forward_distance(self.last_frame_id, chunk.frame_id)
                if distance == 0:
                    self.counters.frame_id_duplicates += 1
                    return None
                if distance >= 0x80000000:
                    self.counters.frame_id_reordered += 1
                    return None
            assembly = self.assemblies.get(chunk.frame_id)
            if assembly is None:
                if (
                    self.last_frame_timestamp_us is not None
                    and chunk.header.timestamp_us <= self.last_frame_timestamp_us
                ):
                    raise TimestampOrderError(
                        f"frame {chunk.frame_id} timestamp "
                        f"{chunk.header.timestamp_us} did not advance past "
                        f"{self.last_frame_timestamp_us}"
                    )
                if self.last_frame_timestamp_us is not None:
                    self.frame_timestamp_intervals_ms.append(
                        (chunk.header.timestamp_us - self.last_frame_timestamp_us)
                        / 1000.0
                    )
                assembly = FrameAssembly(
                    frame_id=chunk.frame_id,
                    shared_metadata=chunk.shared_metadata,
                    first_arrival_ns=arrival_ns,
                    last_arrival_ns=arrival_ns,
                )
                self.assemblies[chunk.frame_id] = assembly
                self.active_frame_id = chunk.frame_id
                self.last_frame_timestamp_us = chunk.header.timestamp_us
            was_present = chunk.chunk_index in assembly.chunks
            complete = assembly.add(chunk, arrival_ns)
            if was_present:
                self.counters.chunk_duplicates += 1
            if complete:
                self.complete_frame(assembly, arrival_ns)
                self.completed_frame_ids.add(chunk.frame_id)
                del self.assemblies[chunk.frame_id]
                self.active_frame_id = None
        except ChunkConflictError:
            self.counters.chunk_conflicts += 1
            raise
        except TimestampOrderError:
            self.counters.timestamp_order_errors += 1
            raise
        except WaveFlagError:
            self.counters.flag_errors += 1
            raise
        except ProtocolError:
            self.counters.metadata_errors += 1
            raise
        return chunk

    def complete_frame(self, assembly: FrameAssembly, arrival_ns: int) -> None:
        samples_bytes = assembly.samples_bytes()
        if len(samples_bytes) != FRAME_SAMPLES * 2:
            raise ProtocolError("completed frame byte count mismatch")
        samples = struct.unpack(f"<{FRAME_SAMPLES}h", samples_bytes)
        frame_min = min(samples)
        frame_max = max(samples)
        self.sample_min = min(self.sample_min, frame_min)
        self.sample_max = max(self.sample_max, frame_max)
        self.sample_values.update(samples)
        frame_flags = assembly.shared_metadata[-1]
        self.record_capture_frame(assembly, samples_bytes)
        if frame_flags & FLAG_ADC_OVERRANGE:
            self.counters.adc_overrange_wave_frames += 1
        if frame_flags & FLAG_FIFO_OVERFLOW:
            self.counters.fifo_overflow_wave_frames += 1
        if self.args.source_mode == "test-pattern" and (
            frame_min >= 0 or frame_max <= 0 or len(set(samples[:512])) < 32
        ):
            self.counters.sample_shape_errors += 1

        if self.last_frame_id is not None:
            distance = frame_id_forward_distance(self.last_frame_id, assembly.frame_id)
            if distance == 0:
                self.counters.frame_id_duplicates += 1
            elif distance < 0x80000000:
                self.counters.frame_id_gaps += distance - 1
            else:
                self.counters.frame_id_reordered += 1
        self.last_frame_id = assembly.frame_id
        self.counters.frames_completed += 1
        if self.first_complete_ns is None:
            self.first_complete_ns = arrival_ns
        if self.last_complete_ns is not None:
            self.frame_intervals_ms.append(
                (arrival_ns - self.last_complete_ns) / 1_000_000.0
            )
        self.last_complete_ns = arrival_ns
        if self.counters.frames_completed % self.args.progress_every == 0:
            elapsed = (arrival_ns - self.run_started_ns) / 1_000_000_000.0
            print(
                f"PROGRESS frames={self.counters.frames_completed} "
                f"packets={self.counters.wave_packets} elapsed_s={elapsed:.1f}",
                flush=True,
            )

    def consume_status(self, packet: ParsedPacket, arrival_ns: int) -> None:
        if (
            packet.header.message_type != MSG_STATUS
            or packet.header.header_bytes != COMMON_HEADER_BYTES
            or packet.extension
            or packet.header.flags != 0
            or len(packet.payload) != 40
        ):
            self.counters.status_format_errors += 1
            raise ProtocolError("STATUS header/flags/length mismatch")
        values = STATUS_PAYLOAD.unpack(packet.payload)
        if values[-1] != 0:
            self.counters.status_format_errors += 1
            raise ProtocolError("STATUS reserved field is nonzero")
        sequence = packet.header.message_seq
        self.last_status_seq, gaps, duplicates, reordered = observe_sequence(
            self.last_status_seq, sequence
        )
        self.counters.status_sequence_gaps += gaps
        self.counters.status_sequence_duplicates += duplicates
        self.counters.status_sequence_reordered += reordered
        names = (
            "device_state",
            "last_error",
            "active_config_id",
            "last_frame_id",
            "frames_sent",
            "packets_sent",
            "adc_overrange_frames",
            "fifo_overflow_frames",
            "frames_dropped",
            "uptime_ms",
            "reserved",
        )
        snapshot = dict(zip(names, values, strict=True))
        snapshot["arrival_ns"] = arrival_ns
        snapshot["message_seq"] = packet.header.message_seq
        self.status_snapshots.append(snapshot)
        self.counters.status_packets += 1
        if self.last_status_arrival_ns is not None:
            self.status_intervals_ms.append(
                (arrival_ns - self.last_status_arrival_ns) / 1_000_000.0
            )
        self.last_status_arrival_ns = arrival_ns

    def wait_for_idle_status(self) -> None:
        deadline = time.monotonic() + self.args.baseline_status_timeout
        while time.monotonic() < deadline:
            try:
                packet, arrival_ns = self.receive(deadline - time.monotonic())
            except socket.timeout:
                break
            self.consume(packet, arrival_ns)
            if packet.header.message_type != MSG_STATUS:
                continue
            snapshot = self.status_snapshots[-1]
            if (
                snapshot["device_state"] == DEVICE_IDLE
                and snapshot["active_config_id"] == 0
            ):
                self.baseline_status = snapshot
                return
        raise TimeoutError("initial IDLE STATUS timed out")

    def disable(self) -> None:
        if not self.enabled:
            return
        sequence = self.next_control_seq()
        request = build_message(MSG_DISABLE_PUSH, self.session_id, sequence)
        started_ns = time.monotonic_ns()
        response = self.exchange(request, MSG_DISABLE_PUSH_ACK, sequence)
        ack_ns = time.monotonic_ns()
        if self.ack_status(response) != STATUS_OK:
            raise ProtocolError("DISABLE_PUSH failed")
        self.disable_ack_latency_us = (ack_ns - started_ns) / 1000.0
        self.disable_ack_ns = ack_ns
        self.frames_completed_at_disable_ack = self.counters.frames_completed
        self.enabled = False

    def wait_for_final_status(self) -> None:
        deadline = time.monotonic() + self.args.final_status_timeout
        while time.monotonic() < deadline:
            try:
                packet, arrival_ns = self.receive(deadline - time.monotonic())
            except socket.timeout:
                break
            self.consume(packet, arrival_ns)
            if (
                packet.header.message_type == MSG_STATUS
                and self.status_snapshots[-1]["device_state"] == DEVICE_READY
                and self.status_snapshots[-1]["active_config_id"] == self.config_id
            ):
                return
        raise TimeoutError("final READY STATUS timed out")

    def observe_post_disable(self) -> None:
        deadline = time.monotonic() + self.args.post_disable_observe
        while time.monotonic() < deadline:
            try:
                packet, arrival_ns = self.receive(deadline - time.monotonic())
            except socket.timeout:
                return
            self.consume(packet, arrival_ns)

    def run(self) -> dict[str, Any]:
        attempt_started_ns = time.monotonic_ns()
        print(
            f"CSLP_STRESS local={self.args.local_ip}:{self.args.local_port} "
            f"remote={self.args.remote_ip}:{self.args.remote_port} "
            f"frames={self.args.frames} source={self.args.source_mode} "
            f"rcvbuf={self.actual_receive_buffer}",
            flush=True,
        )
        print(
            "CSLP_STRESS_SCOPE=application-layer; pcap is required for "
            "wire checksum, fragmentation, and packet-spacing evidence",
            flush=True,
        )
        try:
            self.handshake()
            self.handshake_complete = True
            print(
                f"HANDSHAKE_PASS session=0x{self.session_id:08x} "
                f"boot=0x{self.device_boot_id:08x} config={self.config_id}",
                flush=True,
            )
            self.run_started_ns = time.monotonic_ns()
            deadline = time.monotonic() + self.args.run_timeout
            disable_started = False
            while time.monotonic() < deadline:
                packet, arrival_ns = self.receive(min(1.5, deadline - time.monotonic()))
                chunk = self.consume(packet, arrival_ns)
                if (
                    chunk is not None
                    and chunk.chunk_index == 0
                    and self.counters.frames_completed >= self.args.frames
                ):
                    disable_started = True
                    self.disable_trigger_frame_id = chunk.frame_id
                    self.disable()
                    break
            if not disable_started:
                raise TimeoutError("frame target was not reached before run timeout")
            self.wait_for_final_status()
            self.observe_post_disable()
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            self.runtime_failures.append(failure)
            print(f"CSLP_STRESS_RUNTIME_ERROR={failure}", file=sys.stderr)
        finally:
            if self.run_started_ns == 0:
                self.run_started_ns = attempt_started_ns
            if self.enabled:
                try:
                    self.disable()
                except Exception as error:  # best-effort cleanup
                    failure = f"cleanup disable: {type(error).__name__}: {error}"
                    self.runtime_failures.append(failure)
                    print(f"CLEANUP_DISABLE_FAILED {error}", file=sys.stderr)
            self.run_finished_ns = time.monotonic_ns()
            try:
                self.nic_after = read_nic_stats(self.args.interface)
            except Exception as error:
                failure = f"NIC final stats: {type(error).__name__}: {error}"
                self.runtime_failures.append(failure)
                print(f"NIC_FINAL_STATS_FAILED {error}", file=sys.stderr)
        return self.report()

    def report(self) -> dict[str, Any]:
        if self.assemblies:
            self.counters.incomplete_frames += len(self.assemblies)
        duration_s = (self.run_finished_ns - self.run_started_ns) / 1_000_000_000.0
        steady_duration_s = None
        frame_rate = None
        opportunity_rate = None
        if self.first_complete_ns is not None and self.last_complete_ns is not None:
            steady_duration_s = (
                self.last_complete_ns - self.first_complete_ns
            ) / 1_000_000_000.0
            if steady_duration_s > 0 and self.counters.frames_completed > 1:
                frame_rate = (self.counters.frames_completed - 1) / steady_duration_s
                expected_drops = (
                    1
                    if self.args.expected_test_faults & TEST_FAULT_FRAME_DROP
                    else 0
                )
                opportunity_rate = (
                    self.counters.frames_completed - 1 + expected_drops
                ) / steady_duration_s

        enable_ack_latency_us = monotonic_latency_us(
            self.enable_request_started_ns, self.enable_ack_ns
        )
        first_wave_latency_us = monotonic_latency_us(
            self.enable_request_started_ns, self.first_wave_ns
        )
        first_complete_frame_latency_us = monotonic_latency_us(
            self.enable_request_started_ns, self.first_complete_ns
        )

        nic_delta = {
            key: self.nic_after.get(key, value) - value
            for key, value in self.nic_before.items()
        }
        first_status = self.baseline_status
        last_status = self.status_snapshots[-1] if self.status_snapshots else None
        status_delta: dict[str, int] = {}
        if first_status is not None and last_status is not None:
            for key in (
                "frames_sent",
                "packets_sent",
                "adc_overrange_frames",
                "fifo_overflow_frames",
                "frames_dropped",
            ):
                status_delta[key] = (last_status[key] - first_status[key]) & 0xFFFFFFFF

        failures = list(self.runtime_failures)
        zero_fields = (
            "crc_errors",
            "parse_errors",
            "source_errors",
            "wave_sequence_gaps",
            "wave_sequence_duplicates",
            "wave_sequence_reordered",
            "status_sequence_gaps",
            "status_sequence_duplicates",
            "status_sequence_reordered",
            "status_format_errors",
            "frame_id_gaps",
            "frame_id_reordered",
            "frame_id_duplicates",
            "frame_interleaves",
            "timestamp_order_errors",
            "chunk_duplicates",
            "chunk_conflicts",
            "incomplete_frames",
            "metadata_errors",
            "flag_errors",
            "packet_size_errors",
            "sample_shape_errors",
            "socket_overflow",
            "post_disable_wave_packets",
        )
        counter_values = asdict(self.counters)
        for field_name in zero_fields:
            if counter_values[field_name] != 0:
                failures.append(f"{field_name}={counter_values[field_name]}")
        if self.handshake_complete:
            failures.extend(
                deferred_disable_count_failures(
                    self.args.frames,
                    self.counters.frames_completed,
                    self.counters.wave_packets,
                )
            )
            if opportunity_rate is None or not 19.5 <= opportunity_rate <= 20.5:
                failures.append(
                    f"steady delivery-opportunity rate out of range: {opportunity_rate}"
                )
            if self.counters.control_replays != 1:
                failures.append(
                    f"expected one idempotent control replay, got "
                    f"{self.counters.control_replays}"
                )
            if self.counters.control_seq_conflicts != 1:
                failures.append(
                    f"expected one SEQ_CONFLICT, got "
                    f"{self.counters.control_seq_conflicts}"
                )
        if nic_delta["rx_dropped"] != 0 or nic_delta["rx_errors"] != 0:
            failures.append(f"NIC receive errors: {nic_delta}")
        if (
            self.args.source_mode == "real-adc"
            and self.args.activity_policy == "require"
            and self.counters.frames_completed > 0
            and len(self.sample_values) < 2
        ):
            failures.append(
                "real ADC samples are constant; no sample activity was observed"
            )
        if self.status_intervals_ms and max(self.status_intervals_ms) >= 1500.0:
            failures.append("STATUS silence exceeded 1.5 seconds")
        if status_delta:
            if status_delta["frames_sent"] != self.counters.frames_completed:
                failures.append("device/host frame count mismatch")
            if status_delta["packets_sent"] != self.counters.wave_packets:
                failures.append("device/host packet count mismatch")
        wave_overrange = self.counters.adc_overrange_wave_frames
        device_overrange = status_delta.get("adc_overrange_frames")
        if self.args.expected_test_faults & TEST_FAULT_OTR:
            pass
        else:
            failures.extend(
                overrange_policy_failures(
                    self.args.overrange_policy, wave_overrange, device_overrange
                )
            )
        failures.extend(
            test_fault_policy_failures(
                self.args.expected_test_faults,
                self.counters.frames_completed,
                wave_overrange,
                self.counters.fifo_overflow_wave_frames,
                device_overrange,
                status_delta.get("fifo_overflow_frames"),
                status_delta.get("frames_dropped"),
                self.frame_timestamp_intervals_ms,
            )
        )

        capture = None
        if self.capture_dir is not None:
            manifest = {
                "format": "CycleScope CSLP independent complete frames v1",
                "sample_encoding": "S16_LE",
                "frame_count": len(self.capture_frames),
                "frame_samples": FRAME_SAMPLES,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "source_mode": self.args.source_mode,
                "activity_policy": self.args.activity_policy,
                "overrange_policy": self.args.overrange_policy,
                "expected_test_faults": self.args.expected_test_faults,
                "session_id": self.session_id,
                "device_boot_id": self.device_boot_id,
                "config_id": self.config_id,
                "calibration_id": (
                    self.wave_metadata_identity[0]
                    if self.wave_metadata_identity is not None
                    else None
                ),
                "scale_uv_per_lsb": (
                    self.wave_metadata_identity[1]
                    if self.wave_metadata_identity is not None
                    else None
                ),
                "offset_uv": (
                    self.wave_metadata_identity[2]
                    if self.wave_metadata_identity is not None
                    else None
                ),
                "frames": self.capture_frames,
                "partial": bool(failures),
            }
            manifest_path = self.capture_dir / "capture.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            capture = {
                "directory": str(self.capture_dir.resolve()),
                "manifest": str(manifest_path.resolve()),
                "frame_count": len(self.capture_frames),
                "partial": bool(failures),
            }

        report = {
            "pass": not failures,
            "failures": failures,
            "session_id": self.session_id,
            "device_boot_id": self.device_boot_id,
            "config_id": self.config_id,
            "calibration_id": (
                self.wave_metadata_identity[0]
                if self.wave_metadata_identity is not None
                else None
            ),
            "scale_uv_per_lsb": (
                self.wave_metadata_identity[1]
                if self.wave_metadata_identity is not None
                else None
            ),
            "offset_uv": (
                self.wave_metadata_identity[2]
                if self.wave_metadata_identity is not None
                else None
            ),
            "expected_wave_metadata": {
                "calibration_id": self.args.expected_calibration_id,
                "scale_uv_per_lsb": self.args.expected_scale_uv_per_lsb,
                "offset_uv": self.args.expected_offset_uv,
            },
            "source_mode": self.args.source_mode,
            "activity_policy": self.args.activity_policy,
            "overrange_policy": self.args.overrange_policy,
            "expected_test_faults": self.args.expected_test_faults,
            "handshake_complete": self.handshake_complete,
            "runtime_failures": list(self.runtime_failures),
            "requested_receive_buffer": self.args.receive_buffer,
            "actual_receive_buffer": self.actual_receive_buffer,
            "duration_s": duration_s,
            "steady_duration_s": steady_duration_s,
            "steady_frame_rate_hz": frame_rate,
            "steady_delivery_opportunity_rate_hz": opportunity_rate,
            "application_mbit_s": (
                self.counters.wave_bytes * 8.0 / duration_s / 1_000_000.0
                if duration_s > 0
                else None
            ),
            "enable_request_started_monotonic_ns": self.enable_request_started_ns,
            "enable_ack_monotonic_ns": self.enable_ack_ns,
            "first_wave_monotonic_ns": self.first_wave_ns,
            "first_complete_frame_monotonic_ns": self.first_complete_ns,
            "enable_ack_latency_us": enable_ack_latency_us,
            "first_wave_latency_us": first_wave_latency_us,
            "first_complete_frame_latency_us": first_complete_frame_latency_us,
            "disable_ack_latency_us": self.disable_ack_latency_us,
            "disable_trigger_frame_id": self.disable_trigger_frame_id,
            "frames_completed_at_disable_ack": self.frames_completed_at_disable_ack,
            "expected_frames_after_deferred_disable": self.args.frames + 1,
            "sample_min": (
                self.sample_min if self.counters.frames_completed > 0 else None
            ),
            "sample_max": (
                self.sample_max if self.counters.frames_completed > 0 else None
            ),
            "sample_span": (
                self.sample_max - self.sample_min
                if self.counters.frames_completed > 0
                else None
            ),
            "sample_unique_values": len(self.sample_values),
            "packet_sizes": self.packet_sizes,
            "counters": counter_values,
            "nic_delta": nic_delta,
            "status_delta": status_delta,
            "baseline_status": first_status,
            "timing": {
                "wave_gap_us_p50": percentile(self.wave_intervals_us, 50),
                "wave_gap_us_p99": percentile(self.wave_intervals_us, 99),
                "wave_gap_us_max": max(self.wave_intervals_us, default=None),
                "frame_period_ms_p50": percentile(self.frame_intervals_ms, 50),
                "frame_period_ms_p99": percentile(self.frame_intervals_ms, 99),
                "frame_period_ms_max": max(self.frame_intervals_ms, default=None),
                "frame_timestamp_period_ms": self.frame_timestamp_intervals_ms,
                "frame_timestamp_period_ms_p50": percentile(
                    self.frame_timestamp_intervals_ms, 50
                ),
                "frame_timestamp_period_ms_max": max(
                    self.frame_timestamp_intervals_ms, default=None
                ),
                "status_period_ms_max": max(self.status_intervals_ms, default=None),
            },
            "capture": capture,
            "last_status": last_status,
            "scope": validation_scope(),
        }
        return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-ip", default="192.168.10.4")
    parser.add_argument("--local-port", type=int, default=50001)
    parser.add_argument("--remote-ip", default="192.168.10.2")
    parser.add_argument("--remote-port", type=int, default=50000)
    parser.add_argument("--interface", default="enp2s0")
    parser.add_argument(
        "--source-mode",
        choices=("test-pattern", "real-adc"),
        default="test-pattern",
        help="expected PL source and WAVE flag profile",
    )
    parser.add_argument(
        "--activity-policy",
        choices=("require", "allow"),
        default="require",
        help="whether a real-ADC capture must contain at least two sample codes",
    )
    parser.add_argument(
        "--overrange-policy",
        choices=("reject", "require", "allow"),
        default="reject",
        help="expected ADC_OVERRANGE behavior for this run",
    )
    parser.add_argument(
        "--expected-test-faults",
        type=lambda value: int(value, 0),
        default=0,
        help="expected diagnostic fault mask: bit0 OTR, bit1 overflow, bit2 drop",
    )
    parser.add_argument(
        "--expected-calibration-id",
        type=lambda value: int(value, 0),
        default=0,
        help="required WAVE calibration_id; nonzero also requires CALIBRATED",
    )
    parser.add_argument(
        "--expected-scale-uv-per-lsb",
        type=lambda value: int(value, 0),
        help="optional exact WAVE scale_uV_per_lsb expectation",
    )
    parser.add_argument(
        "--expected-offset-uv",
        type=lambda value: int(value, 0),
        help="optional exact WAVE offset_uV expectation",
    )
    parser.add_argument("--frames", type=int, default=1200)
    parser.add_argument("--run-timeout", type=float, default=90.0)
    parser.add_argument("--control-timeout", type=float, default=0.1)
    parser.add_argument("--control-retries", type=int, default=3)
    parser.add_argument("--baseline-status-timeout", type=float, default=1.6)
    parser.add_argument("--final-status-timeout", type=float, default=1.6)
    parser.add_argument("--post-disable-observe", type=float, default=0.6)
    parser.add_argument("--receive-buffer", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--capture-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        local_address = ipaddress.IPv4Address(args.local_ip)
        remote_address = ipaddress.IPv4Address(args.remote_ip)
    except ipaddress.AddressValueError as error:
        parser.error(str(error))
    for label, address in (("local", local_address), ("remote", remote_address)):
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            parser.error(f"--{label}-ip must be a usable unicast IPv4 address")
    args.local_ip = str(local_address)
    args.remote_ip = str(remote_address)
    if args.frames < 2:
        parser.error("--frames must be at least 2")
    if not 0 <= args.expected_test_faults <= TEST_FAULT_ALL:
        parser.error("--expected-test-faults must use only bits 0..2")
    if args.source_mode != "test-pattern" and args.expected_test_faults:
        parser.error("--expected-test-faults requires --source-mode test-pattern")
    if not 0 <= args.expected_calibration_id <= 0xFFFF:
        parser.error("--expected-calibration-id must fit u16")
    if args.source_mode != "real-adc" and args.expected_calibration_id:
        parser.error("nonzero --expected-calibration-id requires --source-mode real-adc")
    if args.expected_calibration_id and (
        args.expected_scale_uv_per_lsb is None or args.expected_offset_uv is None
    ):
        parser.error(
            "nonzero --expected-calibration-id requires expected scale and offset"
        )
    if args.expected_scale_uv_per_lsb is not None and not (
        1 <= args.expected_scale_uv_per_lsb <= 0xFFFFFFFF
    ):
        parser.error("--expected-scale-uv-per-lsb must be in 1..0xffffffff")
    if args.expected_offset_uv is not None and not (
        -0x80000000 <= args.expected_offset_uv <= 0x7FFFFFFF
    ):
        parser.error("--expected-offset-uv must fit i32")
    if not args.interface:
        parser.error("--interface must not be empty")
    for name in ("local_port", "remote_port"):
        if not 1 <= getattr(args, name) <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be in 1..65535")
    for name in (
        "run_timeout",
        "control_timeout",
        "baseline_status_timeout",
        "final_status_timeout",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.post_disable_observe < 0:
        parser.error("--post-disable-observe must not be negative")
    if args.control_retries < 1:
        parser.error("--control-retries must be at least 1")
    if args.receive_buffer < MAX_UDP_PAYLOAD:
        parser.error(f"--receive-buffer must be at least {MAX_UDP_PAYLOAD}")
    if args.progress_every < 1:
        parser.error("--progress-every must be at least 1")
    if args.local_ip == args.remote_ip:
        parser.error("local and remote IP addresses must differ")
    if args.report is not None and args.report.exists():
        parser.error(f"--report refuses to overwrite existing path: {args.report}")
    if args.capture_dir is not None:
        if args.capture_dir.exists():
            parser.error(
                f"--capture-dir refuses to overwrite existing path: {args.capture_dir}"
            )
        if args.report is not None:
            try:
                args.report.resolve().relative_to(args.capture_dir.resolve())
            except ValueError:
                pass
            else:
                parser.error("--report must not be inside --capture-dir")
    return args


def partial_failure_report(
    args: argparse.Namespace,
    error: Exception,
    phase: str,
    actual_receive_buffer: int | None = None,
) -> dict[str, Any]:
    failure = f"{phase}: {type(error).__name__}: {error}"
    return {
        "pass": False,
        "failures": [failure],
        "runtime_failures": [failure],
        "phase": phase,
        "handshake_complete": False,
        "local": f"{args.local_ip}:{args.local_port}",
        "remote": f"{args.remote_ip}:{args.remote_port}",
        "interface": args.interface,
        "source_mode": args.source_mode,
        "activity_policy": args.activity_policy,
        "overrange_policy": args.overrange_policy,
        "expected_test_faults": args.expected_test_faults,
        "expected_wave_metadata": {
            "calibration_id": args.expected_calibration_id,
            "scale_uv_per_lsb": args.expected_scale_uv_per_lsb,
            "offset_uv": args.expected_offset_uv,
        },
        "calibration_id": None,
        "scale_uv_per_lsb": None,
        "offset_uv": None,
        "requested_receive_buffer": args.receive_buffer,
        "actual_receive_buffer": actual_receive_buffer,
        "enable_request_started_monotonic_ns": None,
        "enable_ack_monotonic_ns": None,
        "first_wave_monotonic_ns": None,
        "first_complete_frame_monotonic_ns": None,
        "enable_ack_latency_us": None,
        "first_wave_latency_us": None,
        "first_complete_frame_latency_us": None,
        "counters": asdict(Counters()),
        "scope": validation_scope(),
    }


def main() -> int:
    args = parse_args()
    client: StressClient | None = None
    report: dict[str, Any] | None = None
    try:
        client = StressClient(args)
        report = client.run()
    except Exception as error:
        actual_receive_buffer = (
            client.actual_receive_buffer if client is not None else None
        )
        report = partial_failure_report(
            args, error, "preflight/runtime", actual_receive_buffer
        )
        print(report["failures"][0], file=sys.stderr)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:
                failure = f"socket close: {type(error).__name__}: {error}"
                if report is None:
                    report = partial_failure_report(
                        args, error, "socket close", client.actual_receive_buffer
                    )
                else:
                    report["pass"] = False
                    report["failures"].append(failure)
                    report["runtime_failures"].append(failure)
    if report is None:
        report = partial_failure_report(
            args, RuntimeError("no report was produced"), "internal"
        )
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
        print(f"CSLP_STRESS_REPORT={args.report}")
    print("CSLP_LAN_STRESS_PASS" if report["pass"] else "CSLP_LAN_STRESS_FAIL")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
