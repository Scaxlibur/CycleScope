#!/usr/bin/env python3
"""Capture ESP32-P4 UART without serial writes, control-line changes, or reset."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import termios
import time


def configure_reader(fd: int, baud: int) -> list[object]:
    """Set raw read parameters while preserving modem-control state."""
    speed = getattr(termios, f"B{baud}", None)
    if speed is None:
        raise ValueError(f"unsupported baud rate: {baud}")
    original = termios.tcgetattr(fd)
    configured = termios.tcgetattr(fd)
    configured[0] = 0
    configured[1] = 0
    configured[2] |= termios.CLOCAL | termios.CREAD
    configured[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
    configured[2] |= termios.CS8
    configured[3] = 0
    configured[4] = speed
    configured[5] = speed
    configured[6][termios.VMIN] = 0
    configured[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, configured)
    return original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=Path, default=Path("/dev/ttyUSB1"))
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0.0:
        parser.error("--seconds must be positive")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    original: list[object] | None = None
    captured_bytes = 0
    started = time.monotonic()
    try:
        original = configure_reader(fd, args.baud)
        with args.output.open("wb") as log:
            deadline = started + args.seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                readable, _, _ = select.select([fd], [], [], min(remaining, 0.2))
                if not readable:
                    continue
                data = os.read(fd, 4096)
                if data:
                    log.write(data)
                    captured_bytes += len(data)
    finally:
        if original is not None:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        os.close(fd)

    print(
        "passive UART capture: "
        f"port={args.port} baud={args.baud} duration_s={time.monotonic() - started:.3f} "
        f"bytes={captured_bytes} serial_writes=0 reset_actions=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
