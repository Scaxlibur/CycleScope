#!/usr/bin/env python3
"""Capture FPGA's CSLP diagnostic mirror without sending any network traffic."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import socket
import struct
import time
import zlib

import numpy as np


COMMON = struct.Struct("!4sBBHIIQHHI")
WAVE = struct.Struct("!IHHIHBBIIIiIHH")
RECORD = struct.Struct("!QHI")
MAGIC = b"CSLP"
WAVE_TYPE = 0x20
COMMON_BYTES = 32
WAVE_BYTES = 72


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def crc_matches(payload: bytes, expected: int) -> bool:
    if len(payload) < COMMON_BYTES:
        return False
    content = bytearray(payload)
    content[28:32] = b"\0\0\0\0"
    return (zlib.crc32(content) & 0xFFFFFFFF) == expected


def decode(payload: bytes) -> dict[str, int] | None:
    if len(payload) < COMMON_BYTES:
        return None
    magic, version, message_type, header_bytes, session, sequence, timestamp, payload_bytes, flags, crc = COMMON.unpack_from(payload)
    if magic != MAGIC or version != 1 or header_bytes + payload_bytes != len(payload):
        return None
    result = {
        "message_type": message_type,
        "header_bytes": header_bytes,
        "session_id": session,
        "message_seq": sequence,
        "timestamp_us": timestamp,
        "payload_bytes": payload_bytes,
        "flags": flags,
        "crc32": crc,
        "crc_ok": int(crc_matches(payload, crc)),
    }
    if message_type != WAVE_TYPE or header_bytes != WAVE_BYTES or len(payload) < WAVE_BYTES:
        return result
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
    ) = WAVE.unpack_from(payload, COMMON_BYTES)
    result.update(
        frame_id=frame_id,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        sample_offset=sample_offset,
        samples_in_chunk=samples_in_chunk,
        sample_format=sample_format,
        channel_count=channel_count,
        sample_rate_hz=sample_rate_hz,
        frame_sample_count=frame_sample_count,
        scale_uv_per_lsb=scale_uv_per_lsb,
        offset_uv=offset_uv,
        config_id=config_id,
        filter_profile=filter_profile,
        calibration_id=calibration_id,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="192.168.10.4")
    parser.add_argument("--port", type=int, default=50002)
    parser.add_argument("--expected-source", default="192.168.10.2")
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--keep-complete-frames", type=int, default=128)
    args = parser.parse_args()
    if args.seconds <= 0 or args.keep_complete_frames < 1:
        parser.error("duration and keep frame count must be positive")

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    raw_path = output / "udp-payloads.bin"
    events_path = output / "packets.jsonl"
    frames: dict[tuple[int, int, int], dict[int, bytes]] = {}
    completed: list[tuple[tuple[int, int, int], np.ndarray, dict[str, int]]] = []
    counts: Counter[str] = Counter()
    message_counts: Counter[str] = Counter()
    socket_opened_ns = time.time_ns()

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.bind, args.port))
    listener.settimeout(0.2)
    deadline = time.monotonic() + args.seconds
    with raw_path.open("wb") as raw, events_path.open("w", encoding="utf-8") as events:
        while time.monotonic() < deadline:
            try:
                payload, sender = listener.recvfrom(65535)
            except TimeoutError:
                continue
            received_ns = time.time_ns()
            sender_ip, sender_port = sender
            if sender_ip != args.expected_source:
                counts["unexpected_source"] += 1
                continue
            record = decode(payload)
            if record is None:
                counts["invalid_cslp"] += 1
                continue
            counts["accepted_datagrams"] += 1
            message_counts[f"0x{record['message_type']:02X}"] += 1
            if not record["crc_ok"]:
                counts["bad_cslp_crc"] += 1
            raw.write(RECORD.pack(received_ns, sender_port, len(payload)))
            raw.write(payload)
            event = {"received_unix_ns": received_ns, "source_ip": sender_ip, "source_port": sender_port, **record}
            events.write(json.dumps(event, sort_keys=True) + "\n")

            if record["message_type"] != WAVE_TYPE or not record["crc_ok"]:
                continue
            if record.get("chunk_count") != 12 or record.get("frame_sample_count") != 8192:
                counts["invalid_wave_geometry"] += 1
                continue
            expected_payload = int(record["samples_in_chunk"]) * 2
            if int(record["payload_bytes"]) != expected_payload:
                counts["invalid_wave_payload_bytes"] += 1
                continue
            key = (int(record["session_id"]), int(record["config_id"]), int(record["frame_id"]))
            partial = frames.setdefault(key, {})
            partial[int(record["chunk_index"])] = payload[WAVE_BYTES:]
            if len(partial) != 12:
                continue
            ordered = b"".join(partial[index] for index in range(12))
            if len(ordered) != 8192 * 2:
                counts["bad_reassembled_length"] += 1
                del frames[key]
                continue
            samples = np.frombuffer(ordered, dtype="<i2").copy()
            completed.append((key, samples, record))
            del frames[key]

    listener.close()
    kept = completed[-args.keep_complete_frames :]
    if kept:
        np.save(output / "complete-frames-s16le.npy", np.stack([item[1] for item in kept]), allow_pickle=False)
        frame_records = [
            {
                "session_id": item[0][0],
                "config_id": item[0][1],
                "frame_id": item[0][2],
                "sample_rate_hz": item[2]["sample_rate_hz"],
                "scale_uv_per_lsb": item[2]["scale_uv_per_lsb"],
                "offset_uv": item[2]["offset_uv"],
                "calibration_id": item[2]["calibration_id"],
            }
            for item in kept
        ]
    else:
        frame_records = []
    summary = {
        "format": "CycleScope M12 passive FPGA mirror capture v1",
        "network_writes": 0,
        "bind": f"{args.bind}:{args.port}",
        "expected_source": args.expected_source,
        "duration_s": args.seconds,
        "socket_opened_unix_ns": socket_opened_ns,
        "counts": dict(sorted(counts.items())),
        "message_counts": dict(sorted(message_counts.items())),
        "complete_frames": len(completed),
        "kept_complete_frames": len(kept),
        "incomplete_frame_keys_at_end": len(frames),
        "frames": frame_records,
        "files": {"udp_payloads": raw_path.name, "packets": events_path.name},
    }
    for path in sorted(output.iterdir()):
        if path.is_file():
            summary.setdefault("sha256", {})[path.name] = sha256(path)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
