#!/usr/bin/env python3
"""Key mutation tests for the normal-v5 hysteresis/recovery evidence parser."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

import cslp_v5_spectrum_hysteresis_stale_recovery_evidence as evidence


def read_normalized(path: Path) -> str:
    return path.read_bytes().decode("ascii").replace("\r\n", "\n")


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one target, got {count}")
    return text.replace(old, new, 1)


def line_with(text: str, needle: str, label: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    if len(matches) != 1:
        raise RuntimeError(f"{label}: expected one matching line, got {len(matches)}")
    return matches[0]


def remove_line_with(text: str, needle: str, label: str) -> str:
    line = line_with(text, needle, label)
    return replace_one(text, line + "\n", "", label)


def move_line_after(
    text: str, moving_needle: str, anchor_needle: str, label: str
) -> str:
    moving = line_with(text, moving_needle, f"{label} moving")
    anchor = line_with(text, anchor_needle, f"{label} anchor")
    without = replace_one(text, moving + "\n", "", f"{label} remove")
    return replace_one(
        without,
        anchor + "\n",
        anchor + "\n" + moving + "\n",
        f"{label} insert",
    )


def expect_reject(
    label: str, dynamic_text: str, recovery_text: str, serial_text: str
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cyclescope-v5-hysteresis-adversarial-", dir="/tmp"
    ) as directory:
        root = Path(directory)
        dynamic_path = root / "dynamic.log"
        recovery_path = root / "recovery.log"
        serial_path = root / "serial.log"
        dynamic_path.write_text(dynamic_text, encoding="ascii")
        recovery_path.write_text(recovery_text, encoding="ascii")
        serial_path.write_text(serial_text, encoding="ascii")
        try:
            dynamic = evidence.validate_dynamic_sender_log(dynamic_path)
            recovery = evidence.validate_recovery_sender_log(recovery_path)
            evidence.validate_serial_log(serial_path, dynamic, recovery)
        except Exception as error:
            print(f"{label}: REJECT ({type(error).__name__}: {error})")
            return
    raise RuntimeError(f"{label}: falsified evidence unexpectedly passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dynamic-sender-log",
        type=Path,
        default=evidence.DEFAULT_DYNAMIC_SENDER_LOG,
    )
    parser.add_argument(
        "--recovery-sender-log",
        type=Path,
        default=evidence.DEFAULT_RECOVERY_SENDER_LOG,
    )
    parser.add_argument(
        "--serial-log", type=Path, default=evidence.DEFAULT_SERIAL_LOG
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence.validate_definitions()
    dynamic_text = read_normalized(args.dynamic_sender_log)
    recovery_text = read_normalized(args.recovery_sender_log)
    serial_text = read_normalized(args.serial_log)
    dynamic = evidence.validate_dynamic_sender_log(args.dynamic_sender_log)
    recovery = evidence.validate_recovery_sender_log(args.recovery_sender_log)
    evidence.validate_serial_log(args.serial_log, dynamic, recovery)

    sender_mutations = (
        (
            "dynamic_sender_completion",
            replace_one(
                dynamic_text,
                "completed frames=80 wave_packets=960",
                "completed frames=80 wave_packets=959",
                "dynamic completion",
            ),
            recovery_text,
        ),
        (
            "recovery_sender_completion",
            dynamic_text,
            replace_one(
                recovery_text,
                "completed frames=20 wave_packets=240",
                "completed frames=20 wave_packets=239",
                "recovery completion",
            ),
        ),
        (
            "recovery_sender_nonzero_exit",
            dynamic_text,
            replace_one(
                recovery_text,
                'COMMAND_EXIT_CODE="0"',
                'COMMAND_EXIT_CODE="1"',
                "recovery exit",
            ),
        ),
    )
    for label, dynamic_mutation, recovery_mutation in sender_mutations:
        expect_reject(label, dynamic_mutation, recovery_mutation, serial_text)

    first_scale = line_with(
        serial_text,
        "session=E26255F1 config=D4E8EAB6 epoch=18 frame=1 "
        "previous=0.0mVpk Amax=100.0mVpk reason=NEW_STREAM",
        "first scale",
    )
    jitter = (
        "I (12164) cyclescope_pipe: Spectrum scale committed: "
        "session=E26255F1 config=D4E8EAB6 epoch=18 frame=2 "
        "previous=100.0mVpk Amax=200.0mVpk reason=UPSHIFT\n"
        "I (12164) cyclescope_pipe: Spectrum scale committed: "
        "session=E26255F1 config=D4E8EAB6 epoch=18 frame=3 "
        "previous=200.0mVpk Amax=100.0mVpk reason=DOWNSHIFT\n"
    )
    extra_jitter = replace_one(
        serial_text,
        first_scale + "\n",
        first_scale + "\n" + jitter,
        "extra 100/200 jitter",
    )
    direct_recovery = move_line_after(
        serial_text,
        "CSLP UI stream state: STALE -> LIVE",
        "CSLP session ready: session=0xE26255F4",
        "direct recovery",
    )
    direct_recovery = replace_one(
        direct_recovery,
        "I (21603) cyclescope_ui: CSLP UI stream state: STALE -> LIVE",
        "I (21551) cyclescope_ui: CSLP UI stream state: STALE -> LIVE",
        "direct recovery timestamp",
    )

    uart_mutations = (
        (
            "extra_first40_100_200_jitter",
            extra_jitter,
        ),
        (
            "missing_upshift",
            remove_line_with(serial_text, "frame=41 previous=100.0mVpk", "upshift"),
        ),
        (
            "missing_downshift",
            remove_line_with(serial_text, "frame=61 previous=200.0mVpk", "downshift"),
        ),
        (
            "wrong_scale_frame",
            replace_one(serial_text, "epoch=18 frame=41", "epoch=18 frame=40", "scale frame"),
        ),
        (
            "wrong_scale_value",
            replace_one(
                serial_text,
                "previous=100.0mVpk Amax=200.0mVpk",
                "previous=100.0mVpk Amax=300.0mVpk",
                "scale value",
            ),
        ),
        (
            "wrong_scale_reason",
            replace_one(serial_text, "reason=UPSHIFT", "reason=DOWNSHIFT", "scale reason"),
        ),
        (
            "early_first_stale",
            replace_one(
                serial_text,
                "last session=E26255F1 frame=80",
                "last session=E26255F1 frame=60",
                "first stale frame",
            ),
        ),
        (
            "session_ready_direct_recovery",
            direct_recovery,
        ),
        (
            "missing_second_stale",
            remove_line_with(
                serial_text,
                "last session=E26255F4 frame=20; retaining",
                "second stale",
            ),
        ),
        (
            "wrong_v5_elf",
            replace_one(
                serial_text,
                "ELF file SHA256:  6d15ace8d...",
                "ELF file SHA256:  0c36dc583...",
                "ELF identity",
            ),
        ),
        (
            "wrong_recovery_generation",
            replace_one(serial_text, "frame=1 gen=81", "frame=1 gen=80", "generation"),
        ),
        (
            "wrong_dynamic_measurement_value",
            replace_one(serial_text, "Vpp=198.621mV", "Vpp=198.620mV", "measurement value"),
        ),
        (
            "wrong_terminal_completed_count",
            replace_one(
                serial_text,
                "Published frame=20 completed=100",
                "Published frame=20 completed=99",
                "terminal publication",
            ),
        ),
    )
    for label, serial_mutation in uart_mutations:
        expect_reject(label, dynamic_text, recovery_text, serial_mutation)

    rejected = len(sender_mutations) + len(uart_mutations)
    print(
        "normal-v5 spectrum-hysteresis/stale-recovery adversarial PASS: "
        f"rejected={rejected}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
