#!/usr/bin/env python3
"""Inspect and replay real-FPGA CSLP samples from a CycleScope pcap archive.

The archived packets target the original host and contain stale session/config
identifiers, so raw tcpreplay is intentionally not used.  This fixture verifies
the archive, reconstructs complete real-ADC frames, performs a fresh CSLP
handshake with the P4, and regenerates only the transport/session headers.

By default the tool is offline.  Network transmission requires the explicit
--confirm-network-replay flag.  Binding the production FPGA address also
requires --allow-production-source-ip and must only be done on an isolated LAN.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import struct
import sys
import time
import zlib


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EMULATOR_PATH = PROJECT_ROOT / "ESP32-P4" / "tools" / "cslp_fpga_emulator.py"
PRODUCTION_FPGA_IP = "192.168.10.2"
DEFAULT_P4_IP = "192.168.10.3"
DEFAULT_P4_PORT = 50001
DEFAULT_SOURCE_PORT = 50000
ETHERNET_HEADER_BYTES = 14
IP_PROTOCOL_UDP = 17
PCAP_LINKTYPE_ETHERNET = 1
VLAN_ETHERTYPES = {0x8100, 0x88A8}
IPV4_ETHERTYPE = 0x0800
PCAP_MAGICS = {
    bytes.fromhex("d4c3b2a1"): ("<", 1_000),
    bytes.fromhex("a1b2c3d4"): (">", 1_000),
    bytes.fromhex("4d3cb2a1"): ("<", 1),
    bytes.fromhex("a1b23c4d"): (">", 1),
}


def load_emulator_module():
    spec = importlib.util.spec_from_file_location(
        "cyclescope_cslp_fpga_emulator",
        EMULATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load emulator module: {EMULATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cslp = load_emulator_module()


@dataclass(frozen=True)
class Endpoint:
    ip: str
    port: int


@dataclass(frozen=True)
class CapturedFrame:
    original_frame_id: int
    original_timestamp_us: int
    base_flags: int
    samples: tuple[int, ...]
    scale_uv_per_lsb: int
    offset_uv: int
    calibration_id: int


@dataclass
class PartialFrame:
    session_id: int
    config_id: int
    original_frame_id: int
    original_timestamp_us: int
    base_flags: int
    scale_uv_per_lsb: int
    offset_uv: int
    calibration_id: int
    chunks: dict[int, tuple[int, tuple[int, ...]]] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureArchive:
    directory: Path
    pcap_sha256: str
    source: Endpoint
    destination: Endpoint
    original_session_id: int
    original_config_id: int
    frames: tuple[CapturedFrame, ...]
    cslp_packets: int
    wave_packets: int


def fail(message: str) -> None:
    raise ValueError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_endpoint(text: str) -> Endpoint:
    ip_text, separator, port_text = text.rpartition(":")
    if not separator:
        fail(f"invalid endpoint: {text}")
    ip = str(ipaddress.IPv4Address(ip_text))
    port = int(port_text)
    if not 0 < port <= 0xFFFF:
        fail(f"invalid UDP port in endpoint: {text}")
    return Endpoint(ip, port)


def verify_manifest(directory: Path) -> tuple[dict, dict, dict]:
    manifest_path = directory / "manifest.json"
    report_path = directory / "lan-report.json"
    analysis_path = directory / "pcap-analysis.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))

    if manifest.get("format") != "CycleScope M11 replay source packet archive v1":
        fail(f"{directory}: unsupported archive format")
    if not manifest.get("pcap_analysis_pass"):
        fail(f"{directory}: manifest pcap analysis did not pass")
    if not report.get("pass") or report.get("source_mode") != "real-adc":
        fail(f"{directory}: LAN report is not a passing real-adc capture")
    if not analysis.get("pass"):
        fail(f"{directory}: pcap analysis did not pass")

    for name, expected in manifest.get("files", {}).items():
        path = directory / name
        if not path.is_file():
            fail(f"{directory}: missing manifest file {name}")
        if path.stat().st_size != expected.get("size"):
            fail(f"{directory}: size mismatch for {name}")
        if sha256_file(path) != expected.get("sha256"):
            fail(f"{directory}: SHA256 mismatch for {name}")

    pcap = directory / "wire.pcap"
    if sha256_file(pcap) != analysis.get("pcap_sha256"):
        fail(f"{directory}: wire.pcap disagrees with pcap-analysis.json")
    return manifest, report, analysis


def iter_pcap_frames(path: Path):
    with path.open("rb") as stream:
        magic = stream.read(4)
        parameters = PCAP_MAGICS.get(magic)
        if parameters is None:
            fail(f"{path}: unsupported pcap magic {magic.hex()}")
        endian, timestamp_unit_ns = parameters
        global_rest = stream.read(20)
        if len(global_rest) != 20:
            fail(f"{path}: truncated global header")
        _major, _minor, _zone, _sigfigs, _snaplen, linktype = struct.unpack(
            f"{endian}HHIIII",
            global_rest,
        )
        if linktype != PCAP_LINKTYPE_ETHERNET:
            fail(f"{path}: expected Ethernet linktype, got {linktype}")

        packet_header = struct.Struct(f"{endian}IIII")
        while True:
            raw_header = stream.read(packet_header.size)
            if not raw_header:
                break
            if len(raw_header) != packet_header.size:
                fail(f"{path}: truncated packet header")
            seconds, fraction, included_length, original_length = packet_header.unpack(
                raw_header
            )
            packet = stream.read(included_length)
            if len(packet) != included_length:
                fail(f"{path}: truncated packet data")
            if included_length != original_length:
                fail(f"{path}: packet was truncated by snaplen")
            timestamp_ns = seconds * 1_000_000_000 + fraction * timestamp_unit_ns
            yield timestamp_ns, packet


def parse_udp_ethernet(packet: bytes) -> tuple[Endpoint, Endpoint, bytes] | None:
    if len(packet) < ETHERNET_HEADER_BYTES:
        fail("truncated Ethernet frame")
    offset = ETHERNET_HEADER_BYTES
    ethertype = struct.unpack_from("!H", packet, 12)[0]
    while ethertype in VLAN_ETHERTYPES:
        if len(packet) < offset + 4:
            fail("truncated VLAN header")
        ethertype = struct.unpack_from("!H", packet, offset + 2)[0]
        offset += 4
    if ethertype != IPV4_ETHERTYPE:
        return None

    if len(packet) < offset + 20:
        fail("truncated IPv4 header")
    version_ihl = packet[offset]
    if version_ihl >> 4 != 4:
        fail("non-IPv4 packet under IPv4 ethertype")
    ip_header_bytes = (version_ihl & 0x0F) * 4
    if ip_header_bytes < 20 or len(packet) < offset + ip_header_bytes:
        fail("invalid IPv4 header length")
    total_length = struct.unpack_from("!H", packet, offset + 2)[0]
    if total_length < ip_header_bytes + 8 or len(packet) < offset + total_length:
        fail("invalid or truncated IPv4 total length")
    fragment = struct.unpack_from("!H", packet, offset + 6)[0]
    if fragment & 0x3FFF:
        fail("IPv4 fragmentation is not replayable")
    if packet[offset + 9] != IP_PROTOCOL_UDP:
        return None

    source_ip = socket.inet_ntoa(packet[offset + 12 : offset + 16])
    destination_ip = socket.inet_ntoa(packet[offset + 16 : offset + 20])
    udp_offset = offset + ip_header_bytes
    source_port, destination_port, udp_length, _checksum = struct.unpack_from(
        "!HHHH",
        packet,
        udp_offset,
    )
    if udp_length < 8 or udp_offset + udp_length > offset + total_length:
        fail("invalid UDP length")
    payload = packet[udp_offset + 8 : udp_offset + udp_length]
    return (
        Endpoint(source_ip, source_port),
        Endpoint(destination_ip, destination_port),
        payload,
    )


def decode_wave_datagram(payload: bytes):
    if len(payload) < cslp.COMMON_HEADER_BYTES:
        fail("truncated CSLP common header")
    common = cslp.COMMON_HEADER.unpack_from(payload)
    (
        magic,
        version,
        message_type,
        header_bytes,
        session_id,
        _message_seq,
        timestamp_us,
        payload_bytes,
        flags,
        packet_crc,
    ) = common
    if magic != cslp.MAGIC or version != cslp.VERSION:
        fail("bad CSLP magic/version")
    if message_type != cslp.WAVE_DATA:
        return None
    if len(payload) < cslp.WAVE_HEADER_BYTES:
        fail("truncated CSLP WAVE_DATA")
    if header_bytes != cslp.WAVE_HEADER_BYTES:
        fail(f"unexpected WAVE_DATA header size {header_bytes}")
    if header_bytes + payload_bytes != len(payload):
        fail("CSLP WAVE_DATA length mismatch")
    if cslp.packet_crc32(payload) != packet_crc:
        fail("CSLP WAVE_DATA CRC mismatch")
    if session_id == 0 or flags & ~0x007F:
        fail("invalid WAVE_DATA session or flags")

    wave = cslp.WAVE_HEADER.unpack_from(payload, cslp.COMMON_HEADER_BYTES)
    (
        frame_id,
        chunk_index,
        chunk_count,
        sample_offset,
        samples_in_chunk,
        sample_format,
        channel_count,
        sample_rate_hz,
        frame_sample_count,
        scale_uv_per_lsb,
        offset_uv,
        config_id,
        filter_profile,
        calibration_id,
    ) = wave
    if (
        frame_id == 0
        or config_id == 0
        or chunk_count != cslp.CHUNK_COUNT
        or chunk_index >= chunk_count
        or sample_format != cslp.SAMPLE_FORMAT_S16_LE
        or channel_count != cslp.CHANNEL_COUNT
        or sample_rate_hz != cslp.SAMPLE_RATE_HZ
        or frame_sample_count != cslp.FRAME_SAMPLE_COUNT
        or filter_profile != cslp.FILTER_PROFILE
        or scale_uv_per_lsb == 0
    ):
        fail("WAVE_DATA metadata is incompatible with frozen Profile 1")
    expected_offset = chunk_index * cslp.SAMPLES_PER_CHUNK
    expected_samples = min(
        cslp.SAMPLES_PER_CHUNK,
        cslp.FRAME_SAMPLE_COUNT - expected_offset,
    )
    if sample_offset != expected_offset or samples_in_chunk != expected_samples:
        fail("WAVE_DATA chunk geometry mismatch")
    if payload_bytes != samples_in_chunk * 2:
        fail("WAVE_DATA sample payload length mismatch")
    first_expected = chunk_index == 0
    last_expected = chunk_index + 1 == chunk_count
    if bool(flags & cslp.FLAG_FIRST_CHUNK) != first_expected:
        fail("WAVE_DATA FIRST_CHUNK flag mismatch")
    if bool(flags & cslp.FLAG_LAST_CHUNK) != last_expected:
        fail("WAVE_DATA LAST_CHUNK flag mismatch")
    if bool(flags & cslp.FLAG_CALIBRATED) != (calibration_id != 0):
        fail("WAVE_DATA calibration flag/id mismatch")

    samples = struct.unpack_from(f"<{samples_in_chunk}h", payload, header_bytes)
    base_flags = flags & ~(cslp.FLAG_FIRST_CHUNK | cslp.FLAG_LAST_CHUNK)
    return {
        "session_id": session_id,
        "config_id": config_id,
        "frame_id": frame_id,
        "timestamp_us": timestamp_us,
        "base_flags": base_flags,
        "chunk_index": chunk_index,
        "sample_offset": sample_offset,
        "samples": tuple(samples),
        "scale_uv_per_lsb": scale_uv_per_lsb,
        "offset_uv": offset_uv,
        "calibration_id": calibration_id,
    }


def reconstruct_frames(
    pcap: Path,
    expected_source: Endpoint,
    expected_destination: Endpoint,
) -> tuple[tuple[CapturedFrame, ...], int, int]:
    partials: dict[tuple[int, int, int], PartialFrame] = {}
    cslp_packets = 0
    wave_packets = 0

    for _timestamp_ns, ethernet in iter_pcap_frames(pcap):
        decoded_udp = parse_udp_ethernet(ethernet)
        if decoded_udp is None:
            continue
        source, destination, payload = decoded_udp
        if source != expected_source or destination != expected_destination:
            continue
        if len(payload) < 4 or payload[:4] != cslp.MAGIC:
            continue
        cslp_packets += 1
        wave = decode_wave_datagram(payload)
        if wave is None:
            continue
        wave_packets += 1
        key = (wave["session_id"], wave["config_id"], wave["frame_id"])
        partial = partials.get(key)
        if partial is None:
            partial = PartialFrame(
                session_id=wave["session_id"],
                config_id=wave["config_id"],
                original_frame_id=wave["frame_id"],
                original_timestamp_us=wave["timestamp_us"],
                base_flags=wave["base_flags"],
                scale_uv_per_lsb=wave["scale_uv_per_lsb"],
                offset_uv=wave["offset_uv"],
                calibration_id=wave["calibration_id"],
            )
            partials[key] = partial
        consistent = (
            partial.original_timestamp_us == wave["timestamp_us"]
            and partial.base_flags == wave["base_flags"]
            and partial.scale_uv_per_lsb == wave["scale_uv_per_lsb"]
            and partial.offset_uv == wave["offset_uv"]
            and partial.calibration_id == wave["calibration_id"]
        )
        if not consistent:
            fail(f"inconsistent metadata in captured frame {wave['frame_id']}")
        chunk_index = wave["chunk_index"]
        if chunk_index in partial.chunks:
            fail(f"duplicate chunk {chunk_index} in frame {wave['frame_id']}")
        partial.chunks[chunk_index] = (wave["sample_offset"], wave["samples"])

    if not partials:
        fail(f"{pcap}: no CSLP WAVE_DATA frames")
    sessions = {(frame.session_id, frame.config_id) for frame in partials.values()}
    if len(sessions) != 1:
        fail(f"{pcap}: capture contains multiple session/config pairs")

    complete: list[CapturedFrame] = []
    for partial in sorted(partials.values(), key=lambda item: item.original_frame_id):
        if len(partial.chunks) != cslp.CHUNK_COUNT:
            fail(
                f"incomplete captured frame {partial.original_frame_id}: "
                f"{len(partial.chunks)}/{cslp.CHUNK_COUNT} chunks"
            )
        samples: list[int | None] = [None] * cslp.FRAME_SAMPLE_COUNT
        for chunk_index in range(cslp.CHUNK_COUNT):
            sample_offset, chunk_samples = partial.chunks[chunk_index]
            end = sample_offset + len(chunk_samples)
            if any(value is not None for value in samples[sample_offset:end]):
                fail(f"overlapping chunks in frame {partial.original_frame_id}")
            samples[sample_offset:end] = chunk_samples
        if any(value is None for value in samples):
            fail(f"sample holes in frame {partial.original_frame_id}")
        complete.append(
            CapturedFrame(
                original_frame_id=partial.original_frame_id,
                original_timestamp_us=partial.original_timestamp_us,
                base_flags=partial.base_flags,
                samples=tuple(int(value) for value in samples),
                scale_uv_per_lsb=partial.scale_uv_per_lsb,
                offset_uv=partial.offset_uv,
                calibration_id=partial.calibration_id,
            )
        )

    frame_ids = [frame.original_frame_id for frame in complete]
    if frame_ids != list(range(frame_ids[0], frame_ids[0] + len(frame_ids))):
        fail(f"{pcap}: captured frame IDs are not contiguous")
    return tuple(complete), cslp_packets, wave_packets


def load_archive(directory: Path) -> CaptureArchive:
    directory = directory.resolve()
    manifest, report, analysis = verify_manifest(directory)
    source = parse_endpoint(analysis["source"])
    destination = parse_endpoint(analysis["destination"])
    frames, cslp_packets, wave_packets = reconstruct_frames(
        directory / "wire.pcap",
        source,
        destination,
    )
    expected_counts = manifest["packet_counts"]
    if cslp_packets != expected_counts["target_cslp_packets"]:
        fail(f"{directory}: CSLP packet count mismatch")
    if wave_packets != expected_counts["target_wave_packets"]:
        fail(f"{directory}: WAVE_DATA packet count mismatch")
    if len(frames) != report["counters"]["frames_completed"]:
        fail(f"{directory}: complete frame count disagrees with LAN report")
    if wave_packets != len(frames) * cslp.CHUNK_COUNT:
        fail(f"{directory}: WAVE_DATA count is not exactly 12 per frame")
    sessions = {
        (partial_session, partial_config)
        for partial_session, partial_config, _frame_id in (
            (
                cslp.COMMON_HEADER.unpack_from(payload)[4],
                cslp.WAVE_HEADER.unpack_from(payload, cslp.COMMON_HEADER_BYTES)[11],
                cslp.WAVE_HEADER.unpack_from(payload, cslp.COMMON_HEADER_BYTES)[0],
            )
            for _timestamp_ns, ethernet in iter_pcap_frames(directory / "wire.pcap")
            for decoded_udp in [parse_udp_ethernet(ethernet)]
            if decoded_udp is not None
            for packet_source, packet_destination, payload in [decoded_udp]
            if packet_source == source
            and packet_destination == destination
            and len(payload) >= cslp.WAVE_HEADER_BYTES
            and payload[:4] == cslp.MAGIC
            and payload[5] == cslp.WAVE_DATA
        )
    }
    if len(sessions) != 1:
        fail(f"{directory}: failed to identify one source session/config")
    original_session_id, original_config_id = next(iter(sessions))
    return CaptureArchive(
        directory=directory,
        pcap_sha256=analysis["pcap_sha256"],
        source=source,
        destination=destination,
        original_session_id=original_session_id,
        original_config_id=original_config_id,
        frames=frames,
        cslp_packets=cslp_packets,
        wave_packets=wave_packets,
    )


def archive_summary(archive: CaptureArchive) -> str:
    sample_min = min(min(frame.samples) for frame in archive.frames)
    sample_max = max(max(frame.samples) for frame in archive.frames)
    sample_crc = 0
    for frame in archive.frames:
        sample_crc = zlib.crc32(
            struct.pack(f"<{len(frame.samples)}h", *frame.samples),
            sample_crc,
        )
    metadata = {
        (
            frame.scale_uv_per_lsb,
            frame.offset_uv,
            frame.calibration_id,
            frame.base_flags,
        )
        for frame in archive.frames
    }
    return (
        f"archive={archive.directory.name} pcap_sha256={archive.pcap_sha256} "
        f"source={archive.source.ip}:{archive.source.port} "
        f"destination={archive.destination.ip}:{archive.destination.port} "
        f"original_session=0x{archive.original_session_id:08X} "
        f"original_config=0x{archive.original_config_id:08X} "
        f"frames={len(archive.frames)} wave_packets={archive.wave_packets} "
        f"frame_ids={archive.frames[0].original_frame_id}.."
        f"{archive.frames[-1].original_frame_id} "
        f"sample_range={sample_min}..{sample_max} "
        f"sample_crc32=0x{sample_crc & 0xFFFFFFFF:08X} metadata={sorted(metadata)}"
    )


def combine_real_adc_archives(
    archives: tuple[CaptureArchive, ...],
) -> CaptureArchive:
    if len(archives) < 2:
        fail("derived composite requires at least two archives")
    primary = archives[0]
    for archive in archives[1:]:
        if archive.source != primary.source or archive.destination != primary.destination:
            fail("derived composite archives use different network endpoints")
    frame_count = min(len(archive.frames) for archive in archives)
    combined_frames: list[CapturedFrame] = []
    digest = hashlib.sha256()
    for archive in archives:
        digest.update(bytes.fromhex(archive.pcap_sha256))

    for frame_index in range(frame_count):
        source_frames = tuple(
            archive.frames[frame_index] for archive in archives
        )
        reference = source_frames[0]
        metadata = {
            (
                frame.scale_uv_per_lsb,
                frame.offset_uv,
                frame.calibration_id,
                frame.base_flags,
            )
            for frame in source_frames
        }
        if len(metadata) != 1 or reference.offset_uv != 0:
            fail(
                "derived composite requires identical metadata and zero offset"
            )
        samples: list[int] = []
        for sample_index in range(cslp.FRAME_SAMPLE_COUNT):
            value = sum(
                frame.samples[sample_index] for frame in source_frames
            )
            if value < -2048 or value > 2047:
                fail(
                    f"derived composite exceeds realistic 12-bit range at "
                    f"frame {frame_index + 1}, sample {sample_index}: {value}"
                )
            samples.append(value)
        packed = struct.pack(f"<{len(samples)}h", *samples)
        digest.update(packed)
        combined_frames.append(
            CapturedFrame(
                original_frame_id=reference.original_frame_id,
                original_timestamp_us=reference.original_timestamp_us,
                base_flags=reference.base_flags,
                samples=tuple(samples),
                scale_uv_per_lsb=reference.scale_uv_per_lsb,
                offset_uv=reference.offset_uv,
                calibration_id=reference.calibration_id,
            )
        )
    return replace(
        primary,
        pcap_sha256=digest.hexdigest(),
        frames=tuple(combined_frames),
        cslp_packets=frame_count * cslp.CHUNK_COUNT,
        wave_packets=frame_count * cslp.CHUNK_COUNT,
    )


def build_replay_wave_packet(
    emulator,
    frame: CapturedFrame,
    frame_id: int,
    message_seq: int,
    frame_timestamp_us: int,
    chunk_index: int,
) -> bytes:
    sample_offset = chunk_index * cslp.SAMPLES_PER_CHUNK
    samples_in_chunk = min(
        cslp.SAMPLES_PER_CHUNK,
        cslp.FRAME_SAMPLE_COUNT - sample_offset,
    )
    samples = frame.samples[sample_offset : sample_offset + samples_in_chunk]
    sample_payload = struct.pack(f"<{samples_in_chunk}h", *samples)
    extension = cslp.WAVE_HEADER.pack(
        frame_id,
        chunk_index,
        cslp.CHUNK_COUNT,
        sample_offset,
        samples_in_chunk,
        cslp.SAMPLE_FORMAT_S16_LE,
        cslp.CHANNEL_COUNT,
        cslp.SAMPLE_RATE_HZ,
        cslp.FRAME_SAMPLE_COUNT,
        frame.scale_uv_per_lsb,
        frame.offset_uv,
        emulator.config_id,
        cslp.FILTER_PROFILE,
        frame.calibration_id,
    )
    flags = frame.base_flags
    if chunk_index == 0:
        flags |= cslp.FLAG_FIRST_CHUNK
    if chunk_index + 1 == cslp.CHUNK_COUNT:
        flags |= cslp.FLAG_LAST_CHUNK
    return cslp.build_message(
        cslp.WAVE_DATA,
        emulator.session_id,
        message_seq,
        frame_timestamp_us,
        sample_payload,
        flags=flags,
        extension=extension,
    )


def replay_archive(
    archive: CaptureArchive,
    bind_ip: str,
    bind_port: int,
    peer_ip: str,
    peer_port: int,
    frame_count: int,
    chunk_gap_us: int,
    pre_stream_delay_ms: int,
    hold_seconds: float,
    handshake_timeout: float,
) -> None:
    selected = tuple(
        archive.frames[index % len(archive.frames)] for index in range(frame_count)
    )
    first = selected[0]
    emulator = cslp.CslpFpgaEmulator(
        bind_ip,
        bind_port,
        peer_ip,
        peer_port,
        first.samples,
        first.scale_uv_per_lsb,
        first.offset_uv,
        first.calibration_id,
    )
    print(
        f"listening on {bind_ip}:{bind_port}; expecting {peer_ip}:{peer_port}; "
        f"real_capture_frames={len(archive.frames)} replay_frames={frame_count}",
        flush=True,
    )
    try:
        emulator.handshake(handshake_timeout)
        if pre_stream_delay_ms:
            time.sleep(pre_stream_delay_ms / 1_000)
        wave_sequence = secrets.randbits(32)
        status_sequence = secrets.randbits(32)
        packets_sent = 0
        next_status = time.monotonic()
        next_frame = time.monotonic()

        for frame_id, frame in enumerate(selected, start=1):
            now = time.monotonic()
            if now < next_frame:
                time.sleep(next_frame - now)
            frame_start = time.monotonic()
            frame_timestamp_us = cslp.monotonic_us(emulator.boot_start_ns)
            if frame_start >= next_status:
                status_sequence = (status_sequence + 1) & 0xFFFFFFFF
                emulator.send_status(
                    status_sequence,
                    frame_id - 1,
                    frame_id - 1,
                    packets_sent,
                )
                next_status = frame_start + 0.5

            for chunk_index in range(cslp.CHUNK_COUNT):
                wave_sequence = (wave_sequence + 1) & 0xFFFFFFFF
                packet = build_replay_wave_packet(
                    emulator,
                    frame,
                    frame_id,
                    wave_sequence,
                    frame_timestamp_us,
                    chunk_index,
                )
                emulator.socket.sendto(packet, emulator.peer_address)
                packets_sent += 1
                if chunk_index + 1 < cslp.CHUNK_COUNT and chunk_gap_us:
                    time.sleep(chunk_gap_us / 1_000_000)

            next_frame = frame_start + cslp.FRAME_PERIOD_US / 1_000_000
            if frame_id == 1 or frame_id % 25 == 0 or frame_id == frame_count:
                print(
                    f"replayed frame={frame_id} "
                    f"original_frame={frame.original_frame_id} "
                    f"packets={packets_sent}",
                    flush=True,
                )

        hold_deadline = time.monotonic() + hold_seconds
        while time.monotonic() < hold_deadline:
            status_sequence = (status_sequence + 1) & 0xFFFFFFFF
            emulator.send_status(
                status_sequence,
                frame_count,
                frame_count,
                packets_sent,
            )
            remaining = max(0.0, hold_deadline - time.monotonic())
            time.sleep(min(0.5, remaining))
        print(
            f"completed real-capture replay frames={frame_count} "
            f"wave_packets={packets_sent}",
            flush=True,
        )
    finally:
        emulator.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive",
        type=Path,
        help="one source_data_for_test measurement-point directory",
    )
    parser.add_argument(
        "--add-archive",
        action="append",
        type=Path,
        default=[],
        help=(
            "derive a multi-line frame by sample-wise addition of another "
            "verified real-ADC archive; does not represent simultaneous capture"
        ),
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="verify and summarize the archive without opening a socket",
    )
    parser.add_argument("--bind-ip", default="192.168.10.4")
    parser.add_argument("--port", type=int, default=DEFAULT_SOURCE_PORT)
    parser.add_argument("--peer-ip", default=DEFAULT_P4_IP)
    parser.add_argument("--peer-port", type=int, default=DEFAULT_P4_PORT)
    parser.add_argument("--frames", type=int, default=0)
    parser.add_argument("--chunk-gap-us", type=int, default=500)
    parser.add_argument(
        "--pre-stream-delay-ms",
        type=int,
        default=100,
        help="guard time after ENABLE_ACK before the first real captured frame",
    )
    parser.add_argument("--hold-seconds", type=float, default=3.0)
    parser.add_argument("--handshake-timeout", type=float, default=20.0)
    parser.add_argument(
        "--confirm-network-replay",
        action="store_true",
        help="required acknowledgement before this tool transmits UDP",
    )
    parser.add_argument(
        "--allow-production-source-ip",
        action="store_true",
        help="allow binding 192.168.10.2; only safe when the real FPGA is isolated",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_archives = (
        load_archive(args.archive),
        *(load_archive(path) for path in args.add_archive),
    )
    if args.add_archive:
        for source_archive in source_archives:
            print(f"composite_source {archive_summary(source_archive)}", flush=True)
        archive = combine_real_adc_archives(source_archives)
        print(
            "derivation=REAL_ADC_SAMPLEWISE_SUM "
            "boundary=NOT_SIMULTANEOUS_FPGA_CAPTURE",
            flush=True,
        )
    else:
        archive = source_archives[0]
    print(archive_summary(archive), flush=True)
    if args.inspect_only:
        print("archive inspection PASS; network_replay=NOT_RUN", flush=True)
        return 0
    if not args.confirm_network_replay:
        raise SystemExit("network replay requires --confirm-network-replay")
    if args.bind_ip == PRODUCTION_FPGA_IP and not args.allow_production_source_ip:
        raise SystemExit(
            "refusing production source IP while the real FPGA may be online; "
            "use an isolated LAN and --allow-production-source-ip"
        )
    if (
        args.frames < 0
        or args.chunk_gap_us < 0
        or args.pre_stream_delay_ms < 0
        or args.hold_seconds < 0
    ):
        raise SystemExit("frames and replay timing values must be non-negative")
    frame_count = args.frames or len(archive.frames)
    if frame_count <= 0:
        raise SystemExit("no frames selected")
    cslp.self_test()
    replay_archive(
        archive,
        str(ipaddress.IPv4Address(args.bind_ip)),
        args.port,
        str(ipaddress.IPv4Address(args.peer_ip)),
        args.peer_port,
        frame_count,
        args.chunk_gap_us,
        args.pre_stream_delay_ms,
        args.hold_seconds,
        args.handshake_timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
