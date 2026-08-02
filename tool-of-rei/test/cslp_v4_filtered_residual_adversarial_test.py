#!/usr/bin/env python3
"""Key mutation tests for the normal-v4 filtered-residual evidence parser."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

import cslp_v4_filtered_residual_evidence as evidence


def read_normalized(path: Path) -> str:
    return path.read_bytes().decode("ascii").replace("\r\n", "\n")


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one target, got {count}")
    return text.replace(old, new, 1)


def replace_all(text: str, old: str, new: str, count: int, label: str) -> str:
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(f"{label}: expected {count} targets, got {actual}")
    return text.replace(old, new)


def expect_reject(label: str, sender_text: str, serial_text: str) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cyclescope-v4-residual-adversarial-", dir="/tmp"
    ) as directory:
        sender_path = Path(directory) / "sender.log"
        serial_path = Path(directory) / "serial.log"
        sender_path.write_text(sender_text, encoding="ascii")
        serial_path.write_text(serial_text, encoding="ascii")
        try:
            sender = evidence.validate_sender_log(sender_path)
            evidence.validate_serial_log(serial_path, sender)
        except Exception as error:
            print(f"{label}: REJECT ({type(error).__name__}: {error})")
            return
    raise RuntimeError(f"{label}: falsified evidence unexpectedly passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender-log", type=Path, default=evidence.DEFAULT_SENDER_LOG)
    parser.add_argument("--serial-log", type=Path, default=evidence.DEFAULT_SERIAL_LOG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence.validate_definitions()
    sender_text = read_normalized(args.sender_log)
    serial_text = read_normalized(args.serial_log)
    sender = evidence.validate_sender_log(args.sender_log)
    evidence.validate_serial_log(args.serial_log, sender)

    sender_mutations = (
        (
            "sender_command_frame_count",
            replace_one(sender_text, "--frames 100", "--frames 99", "command frames"),
        ),
        (
            "sender_completion_packet_count",
            replace_one(
                sender_text,
                "completed frames=100 wave_packets=1200",
                "completed frames=100 wave_packets=1199",
                "completion packets",
            ),
        ),
        (
            "sender_nonzero_exit",
            replace_one(
                sender_text,
                'COMMAND_EXIT_CODE="0"',
                'COMMAND_EXIT_CODE="1"',
                "script exit",
            ),
        ),
    )
    for label, mutated_sender in sender_mutations:
        expect_reject(label, mutated_sender, serial_text)

    uart_mutations = (
        (
            "uart_wrong_v4_elf",
            replace_one(
                serial_text,
                "ELF file SHA256:  0c36dc583...",
                "ELF file SHA256:  22f02f11b...",
                "ELF identity",
            ),
        ),
        (
            "uart_session_mismatch",
            replace_one(
                serial_text,
                "CSLP session ready: session=0x57483CFC",
                "CSLP session ready: session=0x57483CFD",
                "board session",
            ),
        ),
        (
            "uart_terminal_generation",
            replace_one(serial_text, "frame=100 gen=100", "frame=100 gen=99", "gen100"),
        ),
        (
            "uart_h50_leakage",
            replace_all(
                serial_text,
                "P3=99999.98Hz/15.000mVpk",
                "P3=1000000.00Hz/0.316mVpk",
                2,
                "P3 H50",
            ),
        ),
        (
            "uart_wrong_dynamic_scale",
            replace_one(serial_text, "Amax=50.0mVpk", "Amax=100.0mVpk", "Amax"),
        ),
        (
            "uart_injected_reject",
            replace_one(
                serial_text,
                "I (7492) cyclescope_ui:",
                "E (7450) cyclescope_pipe: Measurement rejected\n"
                "I (7492) cyclescope_ui:",
                "formal-window insertion",
            ),
        ),
    )
    for label, mutated_serial in uart_mutations:
        expect_reject(label, sender_text, mutated_serial)

    print(
        f"normal-v4 filtered-residual adversarial PASS: "
        f"rejected={len(sender_mutations) + len(uart_mutations)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
