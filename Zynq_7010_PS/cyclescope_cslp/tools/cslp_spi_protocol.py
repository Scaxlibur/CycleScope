#!/usr/bin/env python3
"""Build and validate CycleScope SPI diagnostic transactions offline."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import struct
from typing import Any


SPI_GET_INFO = 0xA0
SPI_READ_SAMPLES = 0xA1
SPI_INFO_BYTES = 10
SPI_VERSION = 1
FRAME_SAMPLES = 8192
FRAME_BYTES = FRAME_SAMPLES * 2
INVALID_MEASUREMENT_STATUS_MASK = 0xFC


class SpiProtocolError(RuntimeError):
    """SPI evidence violates the frozen diagnostic contract."""


@dataclass(frozen=True)
class SpiInfo:
    magic: str
    version: int
    status: int
    frame_id: int
    frame_samples: int


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def parse_info_payload(payload: bytes) -> SpiInfo:
    if len(payload) != SPI_INFO_BYTES:
        raise SpiProtocolError(
            f"GET_INFO payload must be {SPI_INFO_BYTES} bytes, got {len(payload)}"
        )
    info = SpiInfo(
        magic=payload[:2].decode("ascii", errors="replace"),
        version=payload[2],
        status=payload[3],
        frame_id=int.from_bytes(payload[4:8], "big"),
        frame_samples=int.from_bytes(payload[8:10], "big"),
    )
    failures = info_failures(info)
    if failures:
        raise SpiProtocolError("; ".join(failures))
    return info


def parse_get_info_exchange(received: bytes) -> SpiInfo:
    expected = 1 + SPI_INFO_BYTES
    if len(received) != expected:
        raise SpiProtocolError(
            f"GET_INFO exchange must return {expected} bytes, got {len(received)}"
        )
    return parse_info_payload(received[1:])


def info_failures(info: SpiInfo) -> list[str]:
    failures: list[str] = []
    if info.magic != "CS":
        failures.append(f"bad SPI magic {info.magic!r}")
    if info.version != SPI_VERSION:
        failures.append(f"unsupported SPI version {info.version}")
    if info.frame_id == 0:
        failures.append("SPI frame_id is zero")
    if info.frame_samples != FRAME_SAMPLES:
        failures.append(
            f"SPI frame_samples={info.frame_samples}, expected {FRAME_SAMPLES}"
        )
    return failures


def build_get_info_transfer() -> bytes:
    return bytes((SPI_GET_INFO,)) + bytes(SPI_INFO_BYTES)


def build_read_samples_transfer(start: int = 0, count: int = FRAME_SAMPLES) -> bytes:
    if not 0 <= start < FRAME_SAMPLES:
        raise ValueError(f"start must be in 0..{FRAME_SAMPLES - 1}")
    if not 1 <= count <= FRAME_SAMPLES:
        raise ValueError(f"count must be in 1..{FRAME_SAMPLES}")
    if start + count > FRAME_SAMPLES:
        raise ValueError("read must not wrap the diagnostic frame")
    return bytes((SPI_READ_SAMPLES, start >> 8, start & 0xFF, 0)) + bytes(count * 2)


def parse_read_samples_exchange(received: bytes, count: int) -> bytes:
    expected = 4 + count * 2
    if len(received) != expected:
        raise SpiProtocolError(
            f"READ_SAMPLES exchange must return {expected} bytes, got {len(received)}"
        )
    return received[4:]


def decode_s16le_samples(raw: bytes) -> tuple[int, ...]:
    if len(raw) & 1:
        raise SpiProtocolError("S16_LE sample payload has an odd byte count")
    return struct.unpack(f"<{len(raw) // 2}h", raw)


def require_same_generation(before: SpiInfo, after: SpiInfo) -> None:
    if before.frame_id != after.frame_id:
        raise SpiProtocolError(
            f"SPI generation changed during capture: {before.frame_id} -> {after.frame_id}"
        )
    for label, info in (("before", before), ("after", after)):
        invalid = info.status & INVALID_MEASUREMENT_STATUS_MASK
        if invalid:
            raise SpiProtocolError(
                f"SPI {label} status has OTR/overflow/drop bits: 0x{info.status:02x}"
            )


def load_udp_frame(capture_manifest: Path, frame_id: int) -> tuple[Path, bytes, dict[str, Any]]:
    manifest = json.loads(capture_manifest.read_text(encoding="utf-8"))
    if manifest.get("format") != "CycleScope CSLP independent complete frames v1":
        raise SpiProtocolError("unsupported UDP capture manifest format")
    if manifest.get("frame_samples") != FRAME_SAMPLES:
        raise SpiProtocolError("UDP capture frame size is not 8192 samples")
    records = manifest.get("frames")
    if not isinstance(records, list):
        raise SpiProtocolError("UDP capture manifest lacks a frame list")
    matches = [record for record in records if record.get("frame_id") == frame_id]
    if len(matches) != 1:
        raise SpiProtocolError(
            f"UDP capture has {len(matches)} records for frame_id {frame_id}"
        )
    record = matches[0]
    frame_path = capture_manifest.parent / str(record.get("file", ""))
    raw = frame_path.read_bytes()
    if len(raw) != FRAME_BYTES or record.get("frame_bytes") != FRAME_BYTES:
        raise SpiProtocolError("UDP frame byte count is not 16384")
    declared_hash = record.get("sha256")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if declared_hash != actual_hash:
        raise SpiProtocolError("UDP frame SHA-256 does not match its manifest")
    return frame_path, raw, record


def compare_spi_to_udp(
    before: SpiInfo,
    after: SpiInfo,
    spi_samples: Path,
    capture_manifest: Path,
) -> dict[str, Any]:
    failures: list[str] = []
    try:
        require_same_generation(before, after)
        spi_raw = spi_samples.read_bytes()
        if len(spi_raw) != FRAME_BYTES:
            raise SpiProtocolError(
                f"SPI sample file is {len(spi_raw)} bytes, expected {FRAME_BYTES}"
            )
        udp_path, udp_raw, udp_record = load_udp_frame(
            capture_manifest, before.frame_id
        )
        if spi_raw != udp_raw:
            first_mismatch = next(
                index
                for index, (spi_byte, udp_byte) in enumerate(zip(spi_raw, udp_raw))
                if spi_byte != udp_byte
            )
            raise SpiProtocolError(
                f"SPI/UDP payload mismatch at byte {first_mismatch}"
            )
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
        spi_raw = spi_samples.read_bytes() if spi_samples.is_file() else b""
        udp_path = None
        udp_raw = b""
        udp_record = None
    return {
        "pass": not failures,
        "failures": failures,
        "info_before": asdict(before),
        "info_after": asdict(after),
        "spi_samples": str(spi_samples.resolve()),
        "spi_bytes": len(spi_raw),
        "spi_sha256": hashlib.sha256(spi_raw).hexdigest(),
        "udp_manifest": str(capture_manifest.resolve()),
        "udp_frame": str(udp_path.resolve()) if udp_path is not None else None,
        "udp_bytes": len(udp_raw),
        "udp_sha256": hashlib.sha256(udp_raw).hexdigest() if udp_raw else None,
        "udp_record": udp_record,
        "integrity_note": (
            "SPI v0.1 has no wire CRC; acceptance requires unchanged GET_INFO "
            "generation and byte-identical UDP data for the same frame_id."
        ),
    }


def parse_info_hex(value: str) -> SpiInfo:
    try:
        payload = bytes.fromhex(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    try:
        return parse_info_payload(payload)
    except SpiProtocolError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    decode = subparsers.add_parser("decode-info")
    decode.add_argument("payload", type=parse_info_hex)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--info-before", required=True, type=parse_info_hex)
    compare.add_argument("--info-after", required=True, type=parse_info_hex)
    compare.add_argument("--spi-samples", required=True, type=Path)
    compare.add_argument("--udp-capture", required=True, type=Path)
    compare.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.command == "compare":
        for name in ("spi_samples", "udp_capture"):
            path = getattr(args, name)
            if not path.is_file():
                parser.error(f"--{name.replace('_', '-')} is not a file: {path}")
        if args.report is not None and args.report.exists():
            parser.error(f"--report refuses to overwrite existing path: {args.report}")
    return args


def main() -> int:
    args = parse_args()
    if args.command == "decode-info":
        print(json.dumps(asdict(args.payload), indent=2, sort_keys=True))
        return 0
    report = compare_spi_to_udp(
        args.info_before,
        args.info_after,
        args.spi_samples,
        args.udp_capture,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
        print(f"CSLP_SPI_REPORT={args.report}")
    print("CSLP_SPI_PASS" if report["pass"] else "CSLP_SPI_FAIL")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
