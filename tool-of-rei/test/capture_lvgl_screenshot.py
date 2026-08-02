#!/usr/bin/env python3
"""Capture the local-only ESP32-P4 LVGL screenshot debug stream.

The P4 service is enabled only by ``lvgl_screenshot_debug.cmake``.  It accepts
one ``SHOT\\n`` request on TCP 50002 from this workstation (192.168.10.4), then
returns a fixed 48-byte big-endian header and a full RGB565 little-endian
snapshot.  This fixture converts it to PNG and retains raw bytes + metadata
under tool-of-rei/screenshots for later visual/debug traceability.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import socket
import struct
import sys
import zlib
from pathlib import Path


MAGIC = b"CSCP"
PROTOCOL_VERSION = 1
HEADER_FORMAT = "!4sHHIIIHHIIIQI"
HEADER_BYTES = struct.calcsize(HEADER_FORMAT)
REQUEST = b"SHOT\n"
COLOR_FORMAT_RGB565_LE = 1
STATUS_NAMES = {
    0: "ok",
    1: "bad_request",
    2: "lvgl_lock_failed",
    3: "snapshot_failed",
    4: "invalid_snapshot",
}


def receive_exact(sock: socket.socket, length: int) -> bytes:
    """Receive exactly *length* bytes or raise a descriptive error."""
    parts: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError(
                f"peer closed screenshot connection with {remaining} bytes missing"
            )
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def decode_header(data: bytes) -> dict[str, int | bytes]:
    if len(data) != HEADER_BYTES:
        raise RuntimeError(f"wrong screenshot header size: {len(data)}")
    (
        magic,
        version,
        header_bytes,
        status,
        sequence,
        color_format,
        width,
        height,
        stride,
        pixel_bytes,
        lvgl_tick_ms,
        captured_at_us,
        duration_us,
    ) = struct.unpack(HEADER_FORMAT, data)
    if magic != MAGIC:
        raise RuntimeError(f"wrong screenshot magic: {magic!r}")
    if version != PROTOCOL_VERSION:
        raise RuntimeError(f"unsupported screenshot protocol version: {version}")
    if header_bytes != HEADER_BYTES:
        raise RuntimeError(
            f"unexpected screenshot header_bytes={header_bytes}, expected {HEADER_BYTES}"
        )
    return {
        "status": status,
        "sequence": sequence,
        "color_format": color_format,
        "width": width,
        "height": height,
        "stride": stride,
        "pixel_bytes": pixel_bytes,
        "lvgl_tick_ms": lvgl_tick_ms,
        "captured_at_us": captured_at_us,
        "duration_us": duration_us,
    }


def rgb565le_to_rgb888(
    pixels: bytes, width: int, height: int, stride: int
) -> bytes:
    expected_bytes = stride * height
    if len(pixels) != expected_bytes:
        raise RuntimeError(
            f"pixel size mismatch: got {len(pixels)}, expected {expected_bytes}"
        )
    if stride < width * 2:
        raise RuntimeError(f"RGB565 stride {stride} is narrower than {width} pixels")

    rgb = bytearray(width * height * 3)
    destination = 0
    for row in range(height):
        source = row * stride
        for column in range(width):
            offset = source + column * 2
            value = pixels[offset] | (pixels[offset + 1] << 8)
            red = (value >> 11) & 0x1F
            green = (value >> 5) & 0x3F
            blue = value & 0x1F
            rgb[destination] = (red * 255 + 15) // 31
            rgb[destination + 1] = (green * 255 + 31) // 63
            rgb[destination + 2] = (blue * 255 + 15) // 31
            destination += 3
    return bytes(rgb)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack("!I", len(payload))
        + kind
        + payload
        + struct.pack("!I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_png(rgb: bytes, width: int, height: int) -> bytes:
    row_bytes = width * 3
    if len(rgb) != row_bytes * height:
        raise RuntimeError("RGB888 buffer dimensions are inconsistent")
    scanlines = bytearray((row_bytes + 1) * height)
    for row in range(height):
        source_start = row * row_bytes
        destination_start = row * (row_bytes + 1)
        scanlines[destination_start] = 0  # PNG filter: None
        scanlines[destination_start + 1 : destination_start + 1 + row_bytes] = rgb[
            source_start : source_start + row_bytes
        ]
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            png_chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9)),
            png_chunk(b"IEND", b""),
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.10.3", help="P4 IPv4 address")
    parser.add_argument("--port", type=int, default=50002, help="debug TCP port")
    parser.add_argument("--timeout", type=float, default=10.0, help="socket timeout seconds")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "screenshots",
        help="directory containing timestamped screenshot evidence",
    )
    parser.add_argument(
        "--label",
        default="",
        help="optional ASCII/filename-safe context suffix, e.g. live-time",
    )
    return parser.parse_args()


def output_directory(root: Path, sequence: int, label: str) -> Path:
    timestamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in label
    ).strip("-")
    name = f"{timestamp}-seq{sequence:08d}"
    if safe_label:
        name += f"-{safe_label}"
    destination = root / name
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite existing screenshot: {destination}")
    destination.mkdir(parents=True)
    return destination


def main() -> int:
    arguments = parse_args()
    if HEADER_BYTES != 48:
        raise AssertionError(f"protocol header unexpectedly changed to {HEADER_BYTES} bytes")

    with socket.create_connection((arguments.host, arguments.port), arguments.timeout) as sock:
        sock.settimeout(arguments.timeout)
        sock.sendall(REQUEST)
        header = decode_header(receive_exact(sock, HEADER_BYTES))
        status = int(header["status"])
        if status != 0:
            raise RuntimeError(
                f"P4 rejected screenshot: status={status} ({STATUS_NAMES.get(status, 'unknown')})"
            )
        if header["color_format"] != COLOR_FORMAT_RGB565_LE:
            raise RuntimeError(f"unsupported P4 color format: {header['color_format']}")
        pixel_bytes = int(header["pixel_bytes"])
        stride = int(header["stride"])
        height = int(header["height"])
        if pixel_bytes != stride * height:
            raise RuntimeError(
                f"header pixel_bytes={pixel_bytes} does not equal stride*height={stride * height}"
            )
        pixels = receive_exact(sock, pixel_bytes)

    width = int(header["width"])
    rgb = rgb565le_to_rgb888(pixels, width, height, stride)
    png = make_png(rgb, width, height)
    directory = output_directory(arguments.output_root, int(header["sequence"]), arguments.label)
    raw_path = directory / "screen.rgb565le"
    png_path = directory / "screen.png"
    metadata_path = directory / "metadata.json"
    raw_path.write_bytes(pixels)
    png_path.write_bytes(png)
    metadata = {
        "protocol": "CycleScope LVGL screenshot v1",
        "host_capture_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "source": {"host": arguments.host, "port": arguments.port},
        "request": REQUEST.decode("ascii").rstrip(),
        "status": STATUS_NAMES[0],
        "header": header,
        "raw_file": raw_path.name,
        "png_file": png_path.name,
        "rgb565_byte_order": "little-endian",
        "raw_sha256": hashlib.sha256(pixels).hexdigest(),
        "png_sha256": hashlib.sha256(png).hexdigest(),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"screenshot={png_path}")
    print(f"raw={raw_path}")
    print(f"metadata={metadata_path}")
    print(
        "capture="
        f"{width}x{height} stride={stride} seq={header['sequence']} "
        f"lvgl_tick_ms={header['lvgl_tick_ms']} duration_us={header['duration_us']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"capture failed: {error}", file=sys.stderr)
        raise SystemExit(1)
