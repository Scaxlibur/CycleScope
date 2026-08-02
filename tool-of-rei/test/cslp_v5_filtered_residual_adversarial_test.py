#!/usr/bin/env python3
"""Mutation tests for the normal-v5 filtered-residual evidence parser."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

import cslp_v5_filtered_residual_evidence as evidence


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
        prefix="cyclescope-v5-residual-adversarial-", dir="/tmp"
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
            "sender_wrapper_terminal_shape",
            replace_one(
                sender_text,
                "<not executed on terminal>",
                "<not executed on tty>",
                "script terminal marker",
            ),
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
        (
            "sender_session_identity",
            replace_all(
                sender_text,
                "325A36C4",
                "325A36C5",
                2,
                "sender session",
            ),
        ),
        (
            "sender_boot_identity",
            replace_one(
                sender_text,
                "boot_id=0x579F0D45",
                "boot_id=0x579F0D46",
                "sender boot",
            ),
        ),
        (
            "sender_config_identity",
            replace_all(
                sender_text,
                "2F362DEF",
                "2F362DF0",
                2,
                "sender config",
            ),
        ),
    )
    for label, mutated_sender in sender_mutations:
        expect_reject(label, mutated_sender, serial_text)

    scale_line = (
        "I (17902) cyclescope_pipe: Spectrum scale committed: "
        "session=325A36C4 config=2F362DEF epoch=30 frame=1 "
        "previous=0.0mVpk Amax=50.0mVpk reason=NEW_STREAM"
    )
    peer_line = (
        "W (25866) cslp_rx: CSLP peer silent for more than 1500 ms; "
        "starting a new session"
    )
    stale_line = (
        "W (25995) cyclescope_ui: CSLP UI stream state: LIVE -> STALE; "
        "last session=325A36C4 frame=100; retaining waveform and measurements"
    )
    uart_mutations = (
        (
            "uart_wrong_v5_elf",
            replace_one(
                serial_text,
                "ELF file SHA256:  6d15ace8d...",
                "ELF file SHA256:  0c36dc583...",
                "ELF identity",
            ),
        ),
        (
            "uart_session_mismatch",
            replace_one(
                serial_text,
                "CSLP session ready: session=0x325A36C4",
                "CSLP session ready: session=0x325A36C5",
                "board session",
            ),
        ),
        (
            "uart_boot_mismatch",
            replace_one(
                serial_text,
                "boot=1470041413",
                "boot=1470041414",
                "board boot",
            ),
        ),
        (
            "uart_config_mismatch",
            replace_one(
                serial_text,
                "config=792079855",
                "config=792079856",
                "board config",
            ),
        ),
        (
            "uart_epoch_mismatch",
            replace_all(serial_text, "epoch=30", "epoch=31", 3, "epoch"),
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
            "uart_duplicate_scale_commit",
            replace_one(
                serial_text,
                scale_line,
                scale_line + "\n" + scale_line,
                "scale duplication",
            ),
        ),
        (
            "uart_scale_not_first_frame",
            replace_one(
                serial_text,
                scale_line,
                scale_line.replace("frame=1", "frame=2"),
                "scale line",
            ),
        ),
        (
            "uart_wrong_dynamic_scale",
            replace_one(
                serial_text,
                scale_line,
                scale_line.replace("Amax=50.0mVpk", "Amax=100.0mVpk"),
                "scale Amax",
            ),
        ),
        (
            "uart_missing_waiting_edge",
            replace_one(
                serial_text,
                "CSLP UI stream state: WAITING -> LIVE",
                "CSLP UI stream state: STALE -> LIVE",
                "WAITING edge",
            ),
        ),
        (
            "uart_wrong_stale_last_frame",
            replace_one(
                serial_text,
                "last session=325A36C4 frame=100",
                "last session=325A36C4 frame=99",
                "STALE last frame",
            ),
        ),
        (
            "uart_stale_before_peer_silent",
            replace_one(
                serial_text,
                peer_line + "\n" + stale_line,
                stale_line.replace("(25995)", "(25866)")
                + "\n"
                + peer_line.replace("(25866)", "(25995)"),
                "peer/STALE ordering",
            ),
        ),
        (
            "uart_periodic_health_injected",
            replace_one(
                serial_text,
                peer_line,
                "I (24000) cslp_rx: health/rx: packets=1203 source=0 magic=0 "
                "version=0 length=0 session=0 crc=0\n" + peer_line,
                "health insertion",
            ),
        ),
        (
            "uart_injected_reject",
            replace_one(
                serial_text,
                "I (18004) cyclescope_ui:",
                "E (17950) cyclescope_pipe: Measurement rejected\n"
                "I (18004) cyclescope_ui:",
                "formal-window insertion",
            ),
        ),
    )
    for label, mutated_serial in uart_mutations:
        expect_reject(label, sender_text, mutated_serial)

    print(
        "normal-v5 filtered-residual adversarial PASS: "
        f"rejected={len(sender_mutations) + len(uart_mutations)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
