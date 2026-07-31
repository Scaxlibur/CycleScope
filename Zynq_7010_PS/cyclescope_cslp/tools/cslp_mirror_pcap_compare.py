#!/usr/bin/env python3
"""Compare primary and passive-mirror CSLP payloads from one Ethernet pcap."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import ipaddress
import json
from pathlib import Path
import struct
from typing import Any

import cslp_lan_stress as stress
import cslp_pcap_analyze as pcap


IP_PROTOCOL_UDP = 17


class MirrorCompareError(RuntimeError):
    """The pcap cannot prove byte-identical primary and mirror streams."""


def _payload_digest(payloads: list[bytes]) -> str:
    digest = hashlib.sha256()
    for payload in payloads:
        digest.update(struct.pack(">I", len(payload)))
        digest.update(payload)
    return digest.hexdigest()


def _extract_payloads(
    pcap_path: Path,
    source_ip: ipaddress.IPv4Address,
    destination_ip: ipaddress.IPv4Address,
    source_port: int,
    destination_port: int,
) -> list[bytes]:
    payloads: list[bytes] = []
    for packet_index, _timestamp_ns, frame in pcap.pcap_packets(pcap_path):
        parsed = pcap.ethernet_ipv4(frame)
        if parsed is None:
            continue
        _ip_offset, packet = parsed
        if len(packet) < 20:
            continue
        header_bytes = (packet[0] & 0x0F) * 4
        total_bytes = struct.unpack_from(">H", packet, 2)[0]
        if (
            packet[0] >> 4 != 4
            or header_bytes < 20
            or total_bytes < header_bytes + 8
            or len(packet) < total_bytes
            or packet[12:16] != source_ip.packed
            or packet[16:20] != destination_ip.packed
            or packet[9] != IP_PROTOCOL_UDP
        ):
            continue
        if struct.unpack_from(">H", packet, 6)[0] & 0x3FFF:
            raise MirrorCompareError(
                f"packet {packet_index} is a fragmented target UDP datagram"
            )
        udp = packet[header_bytes:total_bytes]
        udp_source, udp_destination, udp_bytes = struct.unpack_from(">HHH", udp)
        if udp_source != source_port or udp_destination != destination_port:
            continue
        if udp_bytes != len(udp) or udp_bytes < 8:
            raise MirrorCompareError(
                f"packet {packet_index} has an invalid UDP length"
            )
        payload = bytes(udp[8:])
        if not 1 <= len(payload) <= stress.MAX_UDP_PAYLOAD:
            raise MirrorCompareError(
                f"packet {packet_index} has an invalid CSLP payload length"
            )
        stress.parse_datagram(payload)
        payloads.append(payload)
    return payloads


def compare_payload_sequences(
    primary: list[bytes], mirror: list[bytes]
) -> dict[str, Any]:
    failures: list[str] = []
    if not primary:
        failures.append("primary CSLP payload sequence is empty")
    if not mirror:
        failures.append("mirror CSLP payload sequence is empty")
    if len(primary) != len(mirror):
        failures.append(
            f"payload count mismatch: primary={len(primary)} mirror={len(mirror)}"
        )
    first_mismatch = None
    for index, (primary_payload, mirror_payload) in enumerate(
        zip(primary, mirror, strict=False)
    ):
        if primary_payload != mirror_payload:
            first_mismatch = index
            failures.append(f"CSLP payload bytes differ at stream index {index}")
            break

    def types(payloads: list[bytes]) -> dict[str, int]:
        return dict(
            sorted(
                Counter(
                    f"0x{stress.parse_datagram(payload).header.message_type:02x}"
                    for payload in payloads
                ).items()
            )
        )

    return {
        "format": "CycleScope CSLP primary/mirror payload comparison v1",
        "primary_payloads": len(primary),
        "mirror_payloads": len(mirror),
        "primary_sha256": _payload_digest(primary),
        "mirror_sha256": _payload_digest(mirror),
        "primary_message_types": types(primary),
        "mirror_message_types": types(mirror),
        "first_mismatch_index": first_mismatch,
        "failures": failures,
        "pass": not failures,
    }


def analyze(
    pcap_path: Path,
    source_ip: ipaddress.IPv4Address,
    primary_ip: ipaddress.IPv4Address,
    mirror_ip: ipaddress.IPv4Address,
    source_port: int,
    primary_port: int,
    mirror_port: int,
) -> dict[str, Any]:
    primary = _extract_payloads(
        pcap_path, source_ip, primary_ip, source_port, primary_port
    )
    mirror = _extract_payloads(
        pcap_path, source_ip, mirror_ip, source_port, mirror_port
    )
    result = compare_payload_sequences(primary, mirror)
    result.update(
        {
            "pcap": str(pcap_path.resolve()),
            "source": f"{source_ip}:{source_port}",
            "primary": f"{primary_ip}:{primary_port}",
            "mirror": f"{mirror_ip}:{mirror_port}",
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--source-ip", default="192.168.10.2")
    parser.add_argument("--primary-ip", default="192.168.10.3")
    parser.add_argument("--mirror-ip", default="192.168.10.4")
    parser.add_argument("--source-port", type=int, default=50000)
    parser.add_argument("--primary-port", type=int, default=50001)
    parser.add_argument("--mirror-port", type=int, default=50002)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.pcap.is_file():
        parser.error(f"pcap is not a file: {args.pcap}")
    try:
        args.source_ip = ipaddress.IPv4Address(args.source_ip)
        args.primary_ip = ipaddress.IPv4Address(args.primary_ip)
        args.mirror_ip = ipaddress.IPv4Address(args.mirror_ip)
    except ipaddress.AddressValueError as error:
        parser.error(str(error))
    if len({args.source_ip, args.primary_ip, args.mirror_ip}) != 3:
        parser.error("source, primary, and mirror IPv4 addresses must differ")
    for name in ("source_port", "primary_port", "mirror_port"):
        if not 1 <= getattr(args, name) <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be in 1..65535")
    if args.report is not None and args.report.exists():
        parser.error(f"--report refuses to overwrite existing path: {args.report}")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = analyze(
            args.pcap,
            args.source_ip,
            args.primary_ip,
            args.mirror_ip,
            args.source_port,
            args.primary_port,
            args.mirror_port,
        )
    except Exception as error:
        result = {
            "format": "CycleScope CSLP primary/mirror payload comparison v1",
            "pcap": str(args.pcap.resolve()),
            "failures": [f"{type(error).__name__}: {error}"],
            "pass": False,
        }
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print("CSLP_MIRROR_COMPARE_PASS" if result["pass"] else "CSLP_MIRROR_COMPARE_FAIL")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
