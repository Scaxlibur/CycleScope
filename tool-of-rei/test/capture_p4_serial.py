#!/usr/bin/env python3
"""Hard-reset ESP32-P4 and capture a bounded UART log."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

import serial


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0.0:
        parser.error("--seconds must be positive")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with serial.Serial(args.port, args.baud, timeout=0.1) as uart:
        uart.dtr = False
        uart.rts = False
        time.sleep(0.05)
        uart.reset_input_buffer()

        # ESP32-P4 CP210x hard reset: RTS asserted drives EN low.
        uart.rts = True
        time.sleep(0.1)
        uart.reset_input_buffer()
        uart.rts = False

        deadline = time.monotonic() + args.seconds
        with args.output.open("wb") as log:
            while time.monotonic() < deadline:
                data = uart.read(uart.in_waiting or 1)
                if not data:
                    continue
                log.write(data)
                log.flush()
                sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
