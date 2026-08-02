#!/usr/bin/env python3
"""Validate CycleScope wire-level IPv4/UDP evidence from a classic pcap file."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import struct
from typing import Any


PCAP_GLOBAL_BYTES = 24
PCAP_PACKET_BYTES = 16
LINKTYPE_ETHERNET = 1
ETHERTYPE_IPV4 = 0x0800
VLAN_ETHERTYPES = {0x8100, 0x88A8, 0x9100}
IP_PROTOCOL_UDP = 17
CSLP_MAGIC = b"CSLP"
CSLP_WAVE_DATA = 0x20

PCAP_FORMATS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1),
    b"\xa1\xb2\x3c\x4d": (">", 1),
}


class PcapError(RuntimeError):
    """The capture cannot be parsed without weakening an evidence gate."""


def checksum_sum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\x00"
    total = 0
    for offset in range(0, len(data), 2):
        total += (data[offset] << 8) | data[offset + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (total & 0xFFFF) + (total >> 16)


def checksum_valid(data: bytes) -> bool:
    return checksum_sum(data) == 0xFFFF


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def pcap_packets(path: Path):
    with path.open("rb") as stream:
        global_header = stream.read(PCAP_GLOBAL_BYTES)
        if len(global_header) != PCAP_GLOBAL_BYTES:
            raise PcapError("pcap global header is truncated")
        format_info = PCAP_FORMATS.get(global_header[:4])
        if format_info is None:
            raise PcapError("unsupported pcap magic; pcapng is not accepted")
        endian, fraction_to_ns = format_info
        _magic, major, minor, _zone, _accuracy, snaplen, linktype = struct.unpack(
            f"{endian}IHHIIII", global_header
        )
        if (major, minor) != (2, 4):
            raise PcapError(f"unsupported pcap version {major}.{minor}")
        if linktype != LINKTYPE_ETHERNET:
            raise PcapError(f"expected Ethernet linktype 1, got {linktype}")
        if snaplen < 64:
            raise PcapError(f"pcap snaplen is too small: {snaplen}")

        packet_index = 0
        while True:
            record_header = stream.read(PCAP_PACKET_BYTES)
            if not record_header:
                break
            if len(record_header) != PCAP_PACKET_BYTES:
                raise PcapError("pcap packet header is truncated")
            seconds, fraction, captured, original = struct.unpack(
                f"{endian}IIII", record_header
            )
            if captured > snaplen or captured > original:
                raise PcapError(
                    f"packet {packet_index} has invalid captured/original lengths"
                )
            frame = stream.read(captured)
            if len(frame) != captured:
                raise PcapError(f"packet {packet_index} data is truncated")
            if captured != original:
                raise PcapError(
                    f"packet {packet_index} was truncated by snaplen: "
                    f"captured={captured} original={original}"
                )
            timestamp_ns = seconds * 1_000_000_000 + fraction * fraction_to_ns
            yield packet_index, timestamp_ns, frame
            packet_index += 1


def ethernet_ipv4(frame: bytes) -> tuple[int, bytes] | None:
    if len(frame) < 14:
        raise PcapError("Ethernet frame is shorter than 14 bytes")
    ethertype = struct.unpack_from(">H", frame, 12)[0]
    offset = 14
    while ethertype in VLAN_ETHERTYPES:
        if len(frame) < offset + 4:
            raise PcapError("VLAN Ethernet header is truncated")
        ethertype = struct.unpack_from(">H", frame, offset + 2)[0]
        offset += 4
    if ethertype != ETHERTYPE_IPV4:
        return None
    return offset, frame[offset:]


def parse_tcpdump_log(path: Path | None) -> dict[str, int | None]:
    result: dict[str, int | None] = {
        "packets_captured": None,
        "packets_received_by_filter": None,
        "packets_dropped_by_kernel": None,
    }
    if path is None:
        return result
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "packets_captured": r"(?m)^(\d+) packets captured$",
        "packets_received_by_filter": r"(?m)^(\d+) packets received by filter$",
        "packets_dropped_by_kernel": r"(?m)^(\d+) packets dropped by kernel$",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is not None:
            result[name] = int(match.group(1))
    return result


def load_expected_wave_packets(path: Path | None) -> tuple[bool | None, int | None]:
    if path is None:
        return None, None
    report = json.loads(path.read_text(encoding="utf-8"))
    passed = report.get("pass")
    counters = report.get("counters")
    wave_packets = counters.get("wave_packets") if isinstance(counters, dict) else None
    if type(passed) is not bool or type(wave_packets) is not int:
        raise PcapError("LAN report lacks boolean pass or integer counters.wave_packets")
    return passed, wave_packets


def analyze(
    pcap_path: Path,
    source_ip: ipaddress.IPv4Address,
    destination_ip: ipaddress.IPv4Address,
    source_port: int,
    destination_port: int,
    lan_report_path: Path | None = None,
    tcpdump_log_path: Path | None = None,
) -> dict[str, Any]:
    source_bytes = source_ip.packed
    destination_bytes = destination_ip.packed
    counts = {
        "pcap_packets": 0,
        "source_ipv4_packets": 0,
        "source_udp_fragments": 0,
        "source_ipv4_checksum_bad": 0,
        "target_udp_packets": 0,
        "target_udp_malformed": 0,
        "target_udp_checksum_zero": 0,
        "target_udp_checksum_bad": 0,
        "target_udp_checksum_valid": 0,
        "target_cslp_packets": 0,
        "target_wave_packets": 0,
    }
    message_types: dict[str, int] = {}
    wave_timestamps_ns: list[int] = []
    first_timestamp_ns: int | None = None
    last_timestamp_ns: int | None = None

    for packet_index, timestamp_ns, frame in pcap_packets(pcap_path):
        counts["pcap_packets"] += 1
        if first_timestamp_ns is None:
            first_timestamp_ns = timestamp_ns
        last_timestamp_ns = timestamp_ns
        parsed_ethernet = ethernet_ipv4(frame)
        if parsed_ethernet is None:
            continue
        _ip_offset, packet = parsed_ethernet
        if len(packet) < 20:
            raise PcapError(f"packet {packet_index} IPv4 header is truncated")
        version = packet[0] >> 4
        header_bytes = (packet[0] & 0x0F) * 4
        if version != 4 or header_bytes < 20 or len(packet) < header_bytes:
            raise PcapError(f"packet {packet_index} has an invalid IPv4 header")
        total_bytes = struct.unpack_from(">H", packet, 2)[0]
        if total_bytes < header_bytes or len(packet) < total_bytes:
            raise PcapError(f"packet {packet_index} IPv4 payload is truncated")
        source = packet[12:16]
        destination = packet[16:20]
        if source != source_bytes or destination != destination_bytes:
            continue
        counts["source_ipv4_packets"] += 1
        if not checksum_valid(packet[:header_bytes]):
            counts["source_ipv4_checksum_bad"] += 1

        protocol = packet[9]
        fragment_field = struct.unpack_from(">H", packet, 6)[0]
        fragmented = (fragment_field & 0x3FFF) != 0
        if protocol == IP_PROTOCOL_UDP and fragmented:
            counts["source_udp_fragments"] += 1
            continue
        if protocol != IP_PROTOCOL_UDP:
            continue

        udp = packet[header_bytes:total_bytes]
        if len(udp) < 8:
            counts["target_udp_malformed"] += 1
            continue
        udp_source, udp_destination, udp_bytes, udp_checksum = struct.unpack_from(
            ">HHHH", udp, 0
        )
        if udp_source != source_port or udp_destination != destination_port:
            continue
        counts["target_udp_packets"] += 1
        if udp_bytes < 8 or udp_bytes != len(udp):
            counts["target_udp_malformed"] += 1
            continue
        if udp_checksum == 0:
            counts["target_udp_checksum_zero"] += 1
        else:
            pseudo_header = (
                source
                + destination
                + bytes((0, IP_PROTOCOL_UDP))
                + struct.pack(">H", udp_bytes)
            )
            if checksum_valid(pseudo_header + udp):
                counts["target_udp_checksum_valid"] += 1
            else:
                counts["target_udp_checksum_bad"] += 1

        payload = udp[8:]
        if len(payload) >= 6 and payload[:4] == CSLP_MAGIC:
            counts["target_cslp_packets"] += 1
            message_type = payload[5]
            message_key = f"0x{message_type:02x}"
            message_types[message_key] = message_types.get(message_key, 0) + 1
            if message_type == CSLP_WAVE_DATA:
                counts["target_wave_packets"] += 1
                wave_timestamps_ns.append(timestamp_ns)

    expected_lan_pass, expected_wave_packets = load_expected_wave_packets(lan_report_path)
    tcpdump = parse_tcpdump_log(tcpdump_log_path)
    failures: list[str] = []
    if counts["target_udp_packets"] == 0:
        failures.append("pcap contains no target UDP packets")
    for name in (
        "source_udp_fragments",
        "source_ipv4_checksum_bad",
        "target_udp_malformed",
        "target_udp_checksum_zero",
        "target_udp_checksum_bad",
    ):
        if counts[name] != 0:
            failures.append(f"{name}={counts[name]}")
    if counts["target_udp_checksum_valid"] != counts["target_udp_packets"]:
        failures.append("not every target UDP packet has a valid nonzero checksum")
    if expected_lan_pass is False:
        failures.append("LAN report did not pass")
    if (
        expected_wave_packets is not None
        and counts["target_wave_packets"] != expected_wave_packets
    ):
        failures.append(
            "pcap/LAN WAVE packet mismatch: "
            f"pcap={counts['target_wave_packets']} LAN={expected_wave_packets}"
        )
    dropped = tcpdump["packets_dropped_by_kernel"]
    if tcpdump_log_path is not None and dropped is None:
        failures.append("tcpdump log lacks kernel-drop statistics")
    elif dropped not in (None, 0):
        failures.append(f"tcpdump packets_dropped_by_kernel={dropped}")

    wave_gaps_us = [
        (current - previous) / 1000.0
        for previous, current in zip(wave_timestamps_ns, wave_timestamps_ns[1:])
    ]
    capture_duration_s = None
    if first_timestamp_ns is not None and last_timestamp_ns is not None:
        capture_duration_s = (last_timestamp_ns - first_timestamp_ns) / 1_000_000_000.0
    return {
        "pass": not failures,
        "failures": failures,
        "pcap": str(pcap_path.resolve()),
        "pcap_bytes": pcap_path.stat().st_size,
        "pcap_sha256": file_sha256(pcap_path),
        "source": f"{source_ip}:{source_port}",
        "destination": f"{destination_ip}:{destination_port}",
        "capture_duration_s": capture_duration_s,
        "counts": counts,
        "cslp_message_types": dict(sorted(message_types.items())),
        "wave_gap_us": {
            "min": min(wave_gaps_us, default=None),
            "p50": percentile(wave_gaps_us, 50),
            "p99": percentile(wave_gaps_us, 99),
            "max": max(wave_gaps_us, default=None),
        },
        "lan_report": {
            "path": str(lan_report_path.resolve()) if lan_report_path else None,
            "pass": expected_lan_pass,
            "wave_packets": expected_wave_packets,
        },
        "tcpdump": {
            "log": str(tcpdump_log_path.resolve()) if tcpdump_log_path else None,
            **tcpdump,
        },
        "timing_note": (
            "Host software timestamps may be coalesced by NAPI; timing is reported "
            "but is not a wire-spacing acceptance gate."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--source-ip", default="192.168.10.2")
    parser.add_argument("--destination-ip", default="192.168.10.4")
    parser.add_argument("--source-port", type=int, default=50000)
    parser.add_argument("--destination-port", type=int, default=50001)
    parser.add_argument("--lan-report", type=Path)
    parser.add_argument("--tcpdump-log", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        args.source_ip = ipaddress.IPv4Address(args.source_ip)
        args.destination_ip = ipaddress.IPv4Address(args.destination_ip)
    except ipaddress.AddressValueError as error:
        parser.error(str(error))
    for name in ("source_port", "destination_port"):
        if not 1 <= getattr(args, name) <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be in 1..65535")
    for name in ("pcap", "lan_report", "tcpdump_log"):
        path = getattr(args, name)
        if path is not None and not path.is_file():
            parser.error(f"--{name.replace('_', '-')} is not a file: {path}")
    if args.report is not None and args.report.exists():
        parser.error(f"--report refuses to overwrite existing path: {args.report}")
    return args


def main() -> int:
    args = parse_args()
    try:
        report = analyze(
            args.pcap,
            args.source_ip,
            args.destination_ip,
            args.source_port,
            args.destination_port,
            args.lan_report,
            args.tcpdump_log,
        )
    except Exception as error:
        report = {
            "pass": False,
            "failures": [f"{type(error).__name__}: {error}"],
            "pcap": str(args.pcap.resolve()),
        }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
        print(f"CSLP_PCAP_REPORT={args.report}")
    print("CSLP_PCAP_PASS" if report["pass"] else "CSLP_PCAP_FAIL")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
