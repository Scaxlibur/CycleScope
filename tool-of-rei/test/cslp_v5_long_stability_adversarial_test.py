#!/usr/bin/env python3
"""Adversarial regression test for the normal-v5 10k evidence parser.

The fixture validates the fixed formal logs, writes mutated copies only to a
temporary directory under ``/tmp``, and requires every falsified evidence pair
to be rejected.  It retains the historical sender/receiver/pipeline attacks
and adds raw FFT timing, Spectrum-scale commit, and UI state-edge attacks.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tempfile

import cslp_v5_long_stability_evidence as evidence


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


def mutate_timing(line: str, field: str) -> str:
    timing = evidence.RAW_FFT_TIMING_PATTERN.search(line)
    if timing is None:
        raise RuntimeError("timing mutation target is missing")
    values = {
        name: int(timing.group(name))
        for name in ("last", "average", "maximum")
    }
    values[field] += 1
    replacement = (
        "fft_us(last/avg/max)="
        f"{values['last']}/{values['average']}/{values['maximum']}"
    )
    return line[: timing.start()] + replacement + line[timing.end() :]


def expect_reject(
    label: str,
    sender_text: str,
    serial_text: str,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cyclescope-v5-long-evidence-adversarial-", dir="/tmp"
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
    evidence.validate_definitions()
    sender_text = read_normalized(args.sender_log)
    serial_text = read_normalized(args.serial_log)

    sender = evidence.validate_sender_log(args.sender_log)
    evidence.validate_serial_log(args.serial_log, sender)
    mutations = 0

    def reject(label: str, mutated_serial: str) -> None:
        nonlocal mutations
        expect_reject(label, sender_text, mutated_serial)
        mutations += 1

    def reject_sender(label: str, mutated_sender: str) -> None:
        nonlocal mutations
        expect_reject(label, mutated_sender, serial_text)
        mutations += 1

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
        reject_sender(label, mutated_sender)

    reject(
        "uart_wrong_v5_elf_identity",
        replace_one(
            serial_text,
            f"ELF file SHA256:  {evidence.EXPECTED_BOARD_ELF_SHA_PREFIX}...",
            "ELF file SHA256:  0c36dc583...",
            "v5 ELF prefix",
        ),
    )

    all_one = re.sub(
        r"^([IWE]) \([0-9]+\)", r"\1 (1)", serial_text, flags=re.MULTILINE
    )
    reject("uart_all_timestamps_one", all_one)

    measurements = list(
        evidence.common.BOARD_MEASUREMENT_PATTERN.finditer(serial_text)
    )
    if not measurements:
        raise RuntimeError("formal UART measurements are missing")
    last_measurement_line = line_for_match(serial_text, measurements[-1])
    backward_line = re.sub(
        r"^[IWE] \([0-9]+\)", "I (1)", last_measurement_line, count=1
    )
    reject(
        "uart_timestamp_nonmonotonic",
        replace_one(
            serial_text,
            last_measurement_line,
            backward_line,
            "last measurement timestamp",
        ),
    )

    ready = evidence.common.BOARD_SESSION_PATTERN.search(serial_text)
    peer = evidence.PEER_SILENT_PATTERN.search(serial_text)
    if ready is None or peer is None:
        raise RuntimeError("formal UART window markers are missing")
    receiver_sets = tuple(
        tuple(
            match
            for match in pattern.finditer(serial_text)
            if ready.end() < match.start() < peer.start()
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

    receiver_final = receiver_sets[1][-1]
    receiver_final_line = line_for_match(serial_text, receiver_final)
    reject(
        "receiver_final_counter_9999",
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
    reject(
        "receiver_all_counts_one",
        re.sub(
            r"health/frame: completed=[0-9]+ acquired=[0-9]+",
            "health/frame: completed=1 acquired=1",
            serial_text,
        ),
    )

    ready_line = line_for_match(serial_text, ready)
    ready_timestamp = timestamp_for_line(ready_line)
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
    reject(
        "receiver_health_moved_before_frame1",
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
    reject(
        "final_health_moved_before_frame10000",
        replace_one(
            without_final_health,
            published_10000_line,
            f"{final_health_at_publish}\n{published_10000_line}",
            "frame 10000 publish line",
        ),
    )

    reject(
        "pipeline_all_memory_one",
        re.sub(
            r"internal_free=[0-9]+ psram_free=[0-9]+",
            "internal_free=1 psram_free=1",
            serial_text,
        ),
    )
    reject(
        "pipeline_wrong_psram_profile",
        replace_all(
            serial_text,
            f"psram_free={evidence.EXPECTED_PIPE_PSRAM_FREE}",
            "psram_free=1",
            "v5 PSRAM profile",
        ),
    )
    reject(
        "pipeline_internal_min_changed",
        replace_all(
            serial_text,
            f"internal_free={evidence.EXPECTED_MINIMUM_PIPE_INTERNAL_FREE}",
            f"internal_free={evidence.EXPECTED_MINIMUM_PIPE_INTERNAL_FREE + 1}",
            "v5 internal minimum",
        ),
    )
    reject(
        "ui_all_memory_one",
        re.sub(
            r"(cyclescope_fft: health:[^\n]* free=)[0-9]+",
            r"\g<1>1",
            serial_text,
        ),
    )
    reject(
        "ui_minimum_changed",
        replace_all(
            serial_text,
            f"free={evidence.EXPECTED_MINIMUM_UI_FREE}",
            f"free={evidence.EXPECTED_MINIMUM_UI_FREE + 1}",
            "v5 UI minimum",
        ),
    )

    pipe_health = tuple(
        match
        for match in evidence.common.PIPELINE_HEALTH_PATTERN.finditer(serial_text)
        if (
            ready.end() < match.start() < peer.start()
            and match.group("tag") == "cyclescope_pipe"
        )
    )
    if not pipe_health:
        raise RuntimeError("formal pipe health is missing")
    final_pipe_line = line_for_match(serial_text, pipe_health[-1])
    for field in ("last", "average", "maximum"):
        reject(
            f"raw_final_fft_{field}_changed",
            replace_one(
                serial_text,
                final_pipe_line,
                mutate_timing(final_pipe_line, field),
                f"final raw FFT {field}",
            ),
        )
    normalized_raw_max = re.sub(
        r"(fft_us\(last/avg/max\)=[0-9]+/[0-9]+)/50210\b",
        r"\g<1>/49999",
        serial_text,
    )
    if normalized_raw_max == serial_text:
        raise RuntimeError("raw 50.210 ms maximum mutation target is missing")
    reject("raw_fft_maximum_forged_as_legacy_49999", normalized_raw_max)

    last_pipe_line = line_for_match(serial_text, pipe_health[-1])
    reject(
        "pipeline_stale_nonzero",
        replace_one(
            serial_text,
            last_pipe_line,
            replace_one(
                last_pipe_line, "stale=0", "stale=1", "pipeline stale"
            ),
            "last pipeline health",
        ),
    )

    peer_line = line_for_match(serial_text, peer)
    peer_timestamp = timestamp_for_line(peer_line)
    tail_timestamp = peer_timestamp - 1
    tail_health = (
        f"I ({tail_timestamp}) cyclescope_pipe: health: acquired=10000 "
        "analyzed=10000 published=10000 stale=1 invalid=0 fft_fail=0 "
        "ui_overwrite=0 "
        f"fft_us(last/avg/max)={evidence.EXPECTED_FINAL_FFT_LAST_US}/"
        f"{evidence.EXPECTED_FINAL_AVERAGE_FFT_US}/"
        f"{evidence.EXPECTED_MAXIMUM_FFT_US} "
        f"internal_free={evidence.EXPECTED_MINIMUM_PIPE_INTERNAL_FREE} "
        f"psram_free={evidence.EXPECTED_PIPE_PSRAM_FREE}"
    )
    reject(
        "nonzero_pipeline_health_in_hold_tail",
        replace_one(
            serial_text,
            peer_line,
            f"{tail_health}\n{peer_line}",
            "peer-silent tail",
        ),
    )
    tail_bad_memory = tail_health.replace("stale=1", "stale=0").replace(
        f"internal_free={evidence.EXPECTED_MINIMUM_PIPE_INTERNAL_FREE} "
        f"psram_free={evidence.EXPECTED_PIPE_PSRAM_FREE}",
        "internal_free=1 psram_free=1",
    )
    reject(
        "bad_memory_pipeline_health_in_hold_tail",
        replace_one(
            serial_text,
            peer_line,
            f"{tail_bad_memory}\n{peer_line}",
            "peer-silent tail",
        ),
    )

    spectrum_bridge = evidence.SPECTRUM_UI_PATTERN.search(serial_text)
    if spectrum_bridge is None:
        raise RuntimeError("formal normal-v5 Spectrum UI bridge is missing")
    spectrum_bridge_line = line_for_match(serial_text, spectrum_bridge)
    reject(
        "spectrum_amplitude_scale_changed",
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
    reject(
        "duplicate_spectrum_bridge_in_hold_tail",
        replace_one(
            serial_text,
            peer_line,
            f"{duplicated_bridge}\n{peer_line}",
            "peer-silent tail",
        ),
    )
    reject(
        "crash_after_final_health_before_peer_silent",
        replace_one(
            serial_text,
            peer_line,
            f"E ({tail_timestamp}) panic: injected hold-tail crash\n{peer_line}",
            "peer-silent tail",
        ),
    )

    scale = evidence.SCALE_COMMIT_PATTERN.search(serial_text)
    if scale is None:
        raise RuntimeError("formal v5 Spectrum scale commit is missing")
    scale_line = line_for_match(serial_text, scale)
    scale_mutations = (
        ("scale_commit_missing", ""),
        ("scale_commit_duplicated", f"{scale_line}\n{scale_line}"),
        (
            "scale_commit_previous_changed",
            scale_line.replace("previous=0.0mVpk", "previous=20.0mVpk"),
        ),
        (
            "scale_commit_amplitude_changed",
            scale_line.replace("Amax=100.0mVpk", "Amax=200.0mVpk"),
        ),
        (
            "scale_commit_reason_changed",
            scale_line.replace("reason=NEW_STREAM", "reason=UPSHIFT"),
        ),
        (
            "scale_commit_frame_changed",
            scale_line.replace(" frame=1 ", " frame=2 "),
        ),
        (
            "scale_commit_config_changed",
            scale_line.replace(
                f"config={scale.group('config')}", "config=00000001"
            ),
        ),
    )
    for label, replacement in scale_mutations:
        reject(
            label,
            replace_one(serial_text, scale_line, replacement, "scale commit"),
        )

    waiting = evidence.WAITING_TO_LIVE_PATTERN.search(serial_text)
    stale = evidence.LIVE_TO_STALE_PATTERN.search(serial_text)
    if waiting is None or stale is None:
        raise RuntimeError("formal v5 UI state edges are missing")
    waiting_line = line_for_match(serial_text, waiting)
    stale_line = line_for_match(serial_text, stale)
    reject(
        "waiting_to_live_missing",
        replace_one(serial_text, waiting_line, "", "WAITING -> LIVE"),
    )
    reject(
        "waiting_to_live_duplicated",
        replace_one(
            serial_text,
            waiting_line,
            f"{waiting_line}\n{waiting_line}",
            "WAITING -> LIVE",
        ),
    )
    reject(
        "waiting_to_live_frame_changed",
        replace_one(
            serial_text,
            waiting_line,
            waiting_line.replace(
                f"frame={waiting.group('frame')}",
                f"frame={int(waiting.group('frame')) + 1}",
            ),
            "WAITING -> LIVE",
        ),
    )
    changed_ui_frame = str(evidence.EXPECTED_FIRST_UI_FRAME + 1)
    bridge_and_wait_changed = replace_one(
        serial_text,
        spectrum_bridge_line,
        spectrum_bridge_line.replace(
            f"frame={evidence.EXPECTED_FIRST_UI_FRAME} "
            f"gen={evidence.EXPECTED_FIRST_UI_FRAME}",
            f"frame={changed_ui_frame} gen={changed_ui_frame}",
        ),
        "Spectrum bridge frame",
    )
    bridge_and_wait_changed = replace_one(
        bridge_and_wait_changed,
        waiting_line,
        waiting_line.replace(
            f"frame={evidence.EXPECTED_FIRST_UI_FRAME}",
            f"frame={changed_ui_frame}",
        ),
        "WAITING -> LIVE frame",
    )
    reject("first_ui_frame_changed_consistently", bridge_and_wait_changed)
    reject(
        "live_to_stale_missing",
        replace_one(serial_text, stale_line, "", "LIVE -> STALE"),
    )
    reject(
        "live_to_stale_last_frame_changed",
        replace_one(
            serial_text,
            stale_line,
            stale_line.replace("frame=10000", "frame=9999"),
            "LIVE -> STALE",
        ),
    )

    stale_before_peer = re.sub(
        r"^[IWE] \([0-9]+\)",
        f"W ({peer_timestamp - 1})",
        stale_line,
        count=1,
    )
    without_stale = replace_one(
        serial_text, stale_line, "", "LIVE -> STALE"
    )
    reject(
        "live_to_stale_moved_before_peer_silent",
        replace_one(
            without_stale,
            peer_line,
            f"{stale_before_peer}\n{peer_line}",
            "peer-silent line",
        ),
    )

    stale_timestamp = int(stale.group("timestamp"))
    false_recovery = (
        f"I ({stale_timestamp}) cyclescope_ui: CSLP UI stream state: "
        f"STALE -> LIVE; session={stale.group('session')} frame=10001"
    )
    reject(
        "unexpected_stale_to_live_recovery",
        replace_one(
            serial_text,
            stale_line,
            f"{stale_line}\n{false_recovery}",
            "LIVE -> STALE",
        ),
    )
    reject(
        "crash_between_peer_silent_and_stale",
        replace_one(
            serial_text,
            peer_line,
            f"{peer_line}\nE ({peer_timestamp}) panic: injected terminal crash",
            "peer-silent line",
        ),
    )

    expected_mutations = 44
    if mutations != expected_mutations:
        raise RuntimeError(
            f"adversarial mutation inventory changed: {mutations}"
        )
    print(
        "CycleScope normal-v5 10k adversarial evidence test passed: "
        f"mutations={mutations} all_rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
