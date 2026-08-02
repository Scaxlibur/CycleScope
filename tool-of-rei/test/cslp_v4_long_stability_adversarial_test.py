#!/usr/bin/env python3
"""Adversarial regression test for the normal-v4 10k evidence parser.

The fixture validates the fixed formal logs, writes mutated copies only to a
temporary directory under ``/tmp``, and requires every falsified evidence pair
to be rejected.  It preserves the 19 historical v3 mutations and adds v4
image, dynamic-spectrum-scale, and PSRAM-profile identity attacks.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tempfile

import cslp_v4_long_stability_evidence as evidence


def read_normalized(path: Path) -> str:
    return path.read_bytes().decode("ascii").replace("\r\n", "\n")


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one mutation target, got {count}")
    return text.replace(old, new, 1)


def replace_all(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        raise RuntimeError(f"{label}: mutation target is missing")
    return text.replace(old, new)


def line_for_match(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    return text[start:end]


def timestamp_for_line(line: str) -> int:
    match = re.match(r"^[IWE] \(([0-9]+)\)", line)
    if match is None:
        raise RuntimeError("mutation target has no ESP-IDF timestamp")
    return int(match.group(1))


def expect_reject(
    label: str,
    sender_text: str,
    serial_text: str,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cyclescope-v4-long-evidence-adversarial-", dir="/tmp"
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
    parser.add_argument(
        "--sender-log", type=Path, default=evidence.DEFAULT_SENDER_LOG
    )
    parser.add_argument(
        "--serial-log", type=Path, default=evidence.DEFAULT_SERIAL_LOG
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sender_text = read_normalized(args.sender_log)
    serial_text = read_normalized(args.serial_log)

    sender = evidence.validate_sender_log(args.sender_log)
    evidence.validate_serial_log(args.serial_log, sender)

    start = evidence.SCRIPT_START_PATTERN.search(sender_text)
    done = evidence.SCRIPT_DONE_PATTERN.search(sender_text)
    if start is None or done is None:
        raise RuntimeError("formal sender wrapper markers are missing")
    done_line = done.group(0)
    zero_duration_done = (
        f"Script done on {start.group('timestamp').strip()} "
        '[COMMAND_EXIT_CODE="0"]'
    )
    completion = (
        f"completed frames={evidence.EXPECTED_FRAMES} "
        f"wave_packets={evidence.EXPECTED_WAVE_PACKETS}"
    )

    sender_mutations = (
        (
            "sender_duplicate_script_footer",
            replace_one(
                sender_text, done_line, f"{done_line}\n{done_line}", "done footer"
            ),
        ),
        (
            "sender_failed_script_footer",
            replace_one(
                sender_text,
                'COMMAND_EXIT_CODE="0"',
                'COMMAND_EXIT_CODE="1"',
                "sender exit",
            ),
        ),
        (
            "sender_zero_runtime",
            replace_one(sender_text, done_line, zero_duration_done, "done time"),
        ),
        (
            "sender_duplicate_semantic_completion",
            replace_one(
                sender_text,
                completion,
                f"{completion}\n{completion}",
                "semantic completion",
            ),
        ),
        (
            "sender_conflicting_semantic_completion",
            replace_one(
                sender_text,
                completion,
                f"completed frames=9999 wave_packets=119988\n{completion}",
                "semantic completion",
            ),
        ),
        (
            "sender_semantic_completion_suffix",
            replace_one(
                sender_text,
                completion,
                f"{completion} status=FAILED",
                "semantic completion",
            ),
        ),
    )
    for label, mutated_sender in sender_mutations:
        expect_reject(label, mutated_sender, serial_text)

    wrong_elf_prefix = "22f02f11b"
    expect_reject(
        "uart_wrong_v4_elf_identity",
        sender_text,
        replace_one(
            serial_text,
            f"ELF file SHA256:  {evidence.EXPECTED_BOARD_ELF_SHA_PREFIX}...",
            f"ELF file SHA256:  {wrong_elf_prefix}...",
            "v4 ELF prefix",
        ),
    )

    all_one = re.sub(
        r"^([IWE]) \([0-9]+\)", r"\1 (1)", serial_text, flags=re.MULTILINE
    )
    expect_reject("uart_all_timestamps_one", sender_text, all_one)

    measurements = list(evidence.common.BOARD_MEASUREMENT_PATTERN.finditer(serial_text))
    if not measurements:
        raise RuntimeError("formal UART measurements are missing")
    last_measurement_line = line_for_match(serial_text, measurements[-1])
    backward_line = re.sub(
        r"^[IWE] \([0-9]+\)", "I (1)", last_measurement_line, count=1
    )
    expect_reject(
        "uart_timestamp_nonmonotonic",
        sender_text,
        replace_one(
            serial_text,
            last_measurement_line,
            backward_line,
            "last measurement timestamp",
        ),
    )

    receiver_final = list(evidence.common.FRAME_HEALTH_PATTERN.finditer(serial_text))[-1]
    receiver_final_line = line_for_match(serial_text, receiver_final)
    expect_reject(
        "receiver_final_counter_9999",
        sender_text,
        replace_one(
            serial_text,
            receiver_final_line,
            receiver_final_line.replace(
                "completed=10000 acquired=10000",
                "completed=9999 acquired=9999",
            ),
            "receiver final counter",
        ),
    )

    receiver_all_one = re.sub(
        r"health/frame: completed=[0-9]+ acquired=[0-9]+",
        "health/frame: completed=1 acquired=1",
        serial_text,
    )
    expect_reject("receiver_all_counts_one", sender_text, receiver_all_one)

    ready = evidence.common.BOARD_SESSION_PATTERN.search(serial_text)
    if ready is None:
        raise RuntimeError("formal UART session-ready marker is missing")
    ready_line = line_for_match(serial_text, ready)
    ready_timestamp = timestamp_for_line(ready_line)
    receiver_sets = tuple(
        tuple(
            match
            for match in pattern.finditer(serial_text)
            if match.start() > ready.end()
        )
        for pattern in (
            evidence.common.RX_HEALTH_PATTERN,
            evidence.common.FRAME_HEALTH_PATTERN,
            evidence.common.REJECT_HEALTH_PATTERN,
            evidence.common.SOCKET_HEALTH_PATTERN,
        )
    )
    if len({len(matches) for matches in receiver_sets}) != 1:
        raise RuntimeError("formal UART receiver health sets are incomplete")

    first_health = "\n".join(
        line_for_match(serial_text, matches[0]) for matches in receiver_sets
    )
    first_health_at_ready = re.sub(
        r"^[IWE] \([0-9]+\)",
        f"I ({ready_timestamp})",
        first_health,
        flags=re.MULTILINE,
    )
    without_first_health = replace_one(
        serial_text, first_health, "", "first receiver health set"
    )
    expect_reject(
        "receiver_health_moved_before_frame1",
        sender_text,
        replace_one(
            without_first_health,
            ready_line,
            f"{ready_line}\n{first_health_at_ready}",
            "session-ready line",
        ),
    )

    final_health = "\n".join(
        line_for_match(serial_text, matches[-1]) for matches in receiver_sets
    )
    published_10000 = next(
        match
        for match in evidence.common.PUBLISHED_FRAME_PATTERN.finditer(serial_text)
        if int(match.group(1)) == evidence.EXPECTED_FRAMES
    )
    published_10000_line = line_for_match(serial_text, published_10000)
    published_10000_timestamp = timestamp_for_line(published_10000_line)
    final_health_at_publish = re.sub(
        r"^[IWE] \([0-9]+\)",
        f"I ({published_10000_timestamp})",
        final_health,
        flags=re.MULTILINE,
    )
    without_final_health = replace_one(
        serial_text, final_health, "", "final receiver health set"
    )
    expect_reject(
        "final_health_moved_before_frame10000",
        sender_text,
        replace_one(
            without_final_health,
            published_10000_line,
            f"{final_health_at_publish}\n{published_10000_line}",
            "frame 10000 publish line",
        ),
    )

    pipeline_memory_one = re.sub(
        r"internal_free=[0-9]+ psram_free=[0-9]+",
        "internal_free=1 psram_free=1",
        serial_text,
    )
    expect_reject("pipeline_all_memory_one", sender_text, pipeline_memory_one)

    expected_psram_free, _ = evidence._memory_profile()
    wrong_psram_free = (
        28_044_452 if expected_psram_free != 28_044_452 else 1
    )
    expect_reject(
        "pipeline_old_image_psram_profile",
        sender_text,
        replace_all(
            serial_text,
            f"psram_free={expected_psram_free}",
            f"psram_free={wrong_psram_free}",
            "v4 PSRAM profile",
        ),
    )

    ui_memory_one = re.sub(
        r"(cyclescope_fft: health:[^\n]* free=)[0-9]+",
        r"\g<1>1",
        serial_text,
    )
    expect_reject("ui_all_memory_one", sender_text, ui_memory_one)

    expect_reject(
        "pipeline_stale_nonzero",
        sender_text,
        replace_one(
            serial_text,
            "published=9600 stale=0 invalid=0",
            "published=9600 stale=1 invalid=0",
            "pipeline stale",
        ),
    )

    peer = re.search(
        r"CSLP peer silent for more than 1500 ms; starting a new session",
        serial_text,
    )
    if peer is None:
        raise RuntimeError("formal peer-silent marker is missing")
    peer_line = line_for_match(serial_text, peer)
    peer_timestamp = timestamp_for_line(peer_line)
    tail_timestamp = peer_timestamp - 1
    tail_health = (
        f"I ({tail_timestamp}) cyclescope_pipe: health: acquired=10000 "
        "analyzed=10000 published=10000 stale=1 invalid=0 fft_fail=0 "
        "fft_us(last/avg/max)=16000/16888/24733 "
        f"internal_free=118843 psram_free={expected_psram_free}"
    )
    expect_reject(
        "nonzero_pipeline_health_in_hold_tail",
        sender_text,
        replace_one(
            serial_text,
            peer_line,
            f"{tail_health}\n{peer_line}",
            "peer-silent tail",
        ),
    )
    tail_bad_memory = (
        f"I ({tail_timestamp}) cyclescope_pipe: health: acquired=10000 "
        "analyzed=10000 published=10000 stale=0 invalid=0 fft_fail=0 "
        "fft_us(last/avg/max)=16000/16888/24733 "
        "internal_free=1 psram_free=1"
    )
    expect_reject(
        "bad_memory_pipeline_health_in_hold_tail",
        sender_text,
        replace_one(
            serial_text,
            peer_line,
            f"{tail_bad_memory}\n{peer_line}",
            "peer-silent tail",
        ),
    )

    spectrum_bridge = evidence.SPECTRUM_UI_PATTERN.search(serial_text)
    if spectrum_bridge is None:
        raise RuntimeError("formal normal-v4 Spectrum UI bridge is missing")
    spectrum_bridge_line = line_for_match(serial_text, spectrum_bridge)
    expect_reject(
        "spectrum_amplitude_scale_changed",
        sender_text,
        replace_one(
            serial_text,
            spectrum_bridge_line,
            spectrum_bridge_line.replace(
                f"Amax={evidence.EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV:.1f}mVpk",
                "Amax=500.0mVpk",
            ),
            "Spectrum Amax",
        ),
    )
    duplicated_bridge = re.sub(
        r"^[IWE] \([0-9]+\)",
        f"I ({tail_timestamp})",
        spectrum_bridge_line,
        count=1,
    )
    expect_reject(
        "duplicate_spectrum_bridge_in_hold_tail",
        sender_text,
        replace_one(
            serial_text,
            peer_line,
            f"{duplicated_bridge}\n{peer_line}",
            "peer-silent tail",
        ),
    )
    expect_reject(
        "crash_after_final_health_before_peer_silent",
        sender_text,
        replace_one(
            serial_text,
            peer_line,
            f"E ({tail_timestamp}) panic: injected hold-tail crash\n{peer_line}",
            "peer-silent tail",
        ),
    )

    print(
        "CycleScope normal-v4 10k adversarial evidence test passed: "
        "mutations=22 all_rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
