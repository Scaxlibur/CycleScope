#!/usr/bin/env python3
"""Adversarial mutation tests for both normal-v5 boundary profiles."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.dont_write_bytecode = True
import cslp_v5_boundary_matrix_evidence as evidence


@dataclass(frozen=True)
class Mutation:
    category: str
    label: str
    sender_texts: tuple[str, ...]
    serial_text: str
    profile: str = "final4"


def read_normalized(path: Path) -> str:
    return path.read_bytes().decode("ascii").replace("\r\n", "\n")


def replace_exact(
    text: str,
    old: str,
    new: str,
    label: str,
    expected_count: int = 1,
) -> str:
    count = text.count(old)
    if count != expected_count:
        raise RuntimeError(
            f"{label}: expected {expected_count} mutation targets, got {count}"
        )
    return text.replace(old, new, expected_count)


def line_with(text: str, needle: str, label: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    if len(matches) != 1:
        raise RuntimeError(f"{label}: expected one matching line, got {len(matches)}")
    return matches[0]


def remove_line_with(text: str, needle: str, label: str) -> str:
    line = line_with(text, needle, label)
    return replace_exact(text, line + "\n", "", label)


def insert_after_line(
    text: str, needle: str, inserted_line: str, label: str
) -> str:
    line = line_with(text, needle, label)
    return replace_exact(
        text,
        line + "\n",
        line + "\n" + inserted_line + "\n",
        label,
    )


def replace_on_lines(
    text: str,
    line_needle: str,
    old: str,
    new: str,
    label: str,
    expected_lines: int,
) -> str:
    result: list[str] = []
    changed = 0
    for line in text.splitlines(keepends=True):
        if line_needle in line:
            if line.count(old) != 1:
                raise RuntimeError(f"{label}: target is not unique within line")
            line = line.replace(old, new, 1)
            changed += 1
        result.append(line)
    if changed != expected_lines:
        raise RuntimeError(
            f"{label}: expected {expected_lines} matching lines, got {changed}"
        )
    return "".join(result)


def mutate_sender(
    sender_texts: tuple[str, ...], index: int, mutated: str
) -> tuple[str, ...]:
    result = list(sender_texts)
    result[index] = mutated
    return tuple(result)


def expect_reject(mutation: Mutation) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cyclescope-v5-boundary-adversarial-", dir="/tmp"
    ) as directory:
        root = Path(directory)
        sender_paths = tuple(root / f"sender-{index}.log" for index in range(4))
        serial_path = root / "serial.log"
        for path, text in zip(
            sender_paths, mutation.sender_texts, strict=True
        ):
            path.write_text(text, encoding="ascii")
        serial_path.write_text(mutation.serial_text, encoding="ascii")
        try:
            evidence.validate_evidence(
                sender_paths, serial_path, mutation.profile
            )
        except Exception as error:
            print(
                f"{mutation.category}/{mutation.label}[{mutation.profile}]: REJECT "
                f"({type(error).__name__}: {error})"
            )
            return
    raise RuntimeError(
        f"{mutation.category}/{mutation.label}: falsified evidence passed"
    )


def expect_cli_conflict_reject() -> None:
    argv = [
        "cslp_v5_boundary_matrix_evidence.py",
        "--self-test-only",
        "--serial-log",
        "/does/not/exist",
    ]
    try:
        with patch.object(sys, "argv", argv):
            evidence.main()
    except SystemExit as error:
        if error.code not in (None, 0):
            print(
                "cli/self_test_with_serial_log: REJECT "
                f"(SystemExit: {error.code})"
            )
            return
    raise RuntimeError("CLI self-test/log conflict unexpectedly passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sender-log",
        action="append",
        type=Path,
        help="repeat exactly four times in final4 matrix order",
    )
    parser.add_argument("--serial-log", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sender_paths = (
        tuple(args.sender_log)
        if args.sender_log is not None
        else evidence.PROFILE_SENDER_LOGS["final4"]
    )
    serial_path = (
        args.serial_log
        if args.serial_log is not None
        else evidence.PROFILE_SERIAL_LOGS["final4"]
    )
    evidence.validate_evidence(sender_paths, serial_path, "final4")
    sender_texts = tuple(read_normalized(path) for path in sender_paths)
    serial_text = read_normalized(serial_path)
    final2_sender_paths = evidence.PROFILE_SENDER_LOGS["final2"]
    final2_serial_path = evidence.PROFILE_SERIAL_LOGS["final2"]
    evidence.validate_evidence(
        final2_sender_paths, final2_serial_path, "final2"
    )
    final2_sender_texts = tuple(
        read_normalized(path) for path in final2_sender_paths
    )
    final2_serial_text = read_normalized(final2_serial_path)

    mutations: list[Mutation] = []

    def add_sender(
        category: str, label: str, index: int, mutated_text: str
    ) -> None:
        mutations.append(
            Mutation(
                category,
                label,
                mutate_sender(sender_texts, index, mutated_text),
                serial_text,
            )
        )

    def add_serial(category: str, label: str, mutated_text: str) -> None:
        mutations.append(
            Mutation(category, label, sender_texts, mutated_text)
        )

    # Regressions found by independent parser audit: each used to pass.
    add_serial(
        "regression_bypass",
        "fatal_before_first_ready",
        insert_after_line(
            serial_text,
            "main_task: Returned from app_main()",
            "E (4250) app_init: catastrophic initialization failure",
            "pre-ready fatal",
        ),
    )
    add_serial(
        "regression_bypass",
        "generic_fatal_before_first_ready",
        insert_after_line(
            serial_text,
            "main_task: Returned from app_main()",
            "FATAL: catastrophic initialization failure",
            "generic pre-ready FATAL",
        ),
    )
    add_serial(
        "regression_bypass",
        "warning_fatal_before_first_ready",
        insert_after_line(
            serial_text,
            "main_task: Returned from app_main()",
            "W (4250) app_init: FATAL initialization fault",
            "warning pre-ready FATAL",
        ),
    )
    add_serial(
        "regression_bypass",
        "boot_failed_before_first_ready",
        insert_after_line(
            serial_text,
            "main_task: Returned from app_main()",
            "W (4250) app_init: boot failed after initialization",
            "pre-ready boot failed",
        ),
    )
    add_serial(
        "regression_bypass",
        "extra_malformed_ui_bridge",
        insert_after_line(
            serial_text,
            "Spectrum UI bridge on Core 0: session=BA29D2A6",
            "I (4601) cyclescope_ui: Spectrum UI bridge malformed",
            "malformed UI bridge",
        ),
    )
    ui_line = line_with(
        serial_text,
        "Spectrum UI bridge on Core 0: session=BA29D2A6",
        "movable UI bridge",
    )
    ui_before_measurement = replace_exact(
        serial_text,
        ui_line + "\n",
        "",
        "remove UI bridge for move",
    )
    moved_ui_line = replace_exact(
        ui_line,
        "I (4600)",
        "I (4590)",
        "adjust moved UI timestamp",
    )
    ui_before_measurement = insert_after_line(
        ui_before_measurement,
        "Spectrum scale committed: session=BA29D2A6",
        moved_ui_line,
        "move UI before producer measurement",
    )
    add_serial(
        "regression_bypass",
        "ui_bridge_before_measurement",
        ui_before_measurement,
    )
    exact_weak_line = line_with(
        serial_text,
        "FFT exact weak startup self-test:",
        "exact-weak startup PASS",
    )
    add_serial(
        "regression_bypass",
        "duplicate_exact_weak_startup_pass",
        insert_after_line(
            serial_text,
            "FFT exact weak startup self-test:",
            exact_weak_line,
            "duplicate exact-weak PASS",
        ),
    )
    primary_startup_line = line_with(
        serial_text,
        "FFT startup self-test:",
        "primary startup PASS",
    )
    add_serial(
        "regression_bypass",
        "primary_startup_fail",
        replace_exact(
            serial_text,
            primary_startup_line,
            replace_exact(
                primary_startup_line,
                " PASS",
                " FAIL",
                "primary startup result",
            ),
            "primary startup line",
        ),
    )
    add_serial(
        "regression_bypass",
        "primary_fail_then_pass",
        replace_exact(
            serial_text,
            "elapsed=17544us PASS",
            "elapsed=17544us FAIL PASS",
            "primary FAIL PASS",
        ),
    )
    add_serial(
        "regression_bypass",
        "exact_heap_fail_then_pass",
        replace_exact(
            serial_text,
            "elapsed=13907us heap=PASS PASS",
            "elapsed=13907us heap=FAIL PASS",
            "exact heap FAIL PASS",
        ),
    )
    add_serial(
        "regression_bypass",
        "duplicate_primary_startup_pass",
        insert_after_line(
            serial_text,
            "FFT startup self-test:",
            primary_startup_line,
            "duplicate primary PASS",
        ),
    )
    add_serial(
        "regression_bypass",
        "missing_formal_pipeline_prepared",
        remove_line_with(
            serial_text,
            "Formal CSLP FFT pipeline prepared;",
            "formal pipeline prepared",
        ),
    )
    add_serial(
        "regression_bypass",
        "analysis_not_ready",
        replace_exact(
            serial_text,
            "Instrument analysis preparation: READY",
            "Instrument analysis preparation: NOT_READY",
            "analysis READY gate",
        ),
    )
    receiver_ready_line = line_with(
        serial_text,
        "CSLP v1 receiver ready on Core 1; golden packet PASS",
        "receiver golden-ready",
    )
    receiver_after_session = replace_exact(
        serial_text,
        receiver_ready_line + "\n",
        "",
        "remove receiver ready for move",
    )
    moved_receiver_line = replace_exact(
        receiver_ready_line,
        "I (4214)",
        "I (4570)",
        "adjust moved receiver-ready timestamp",
    )
    receiver_after_session = insert_after_line(
        receiver_after_session,
        "CSLP session ready: session=0xBA29D2A6",
        moved_receiver_line,
        "move receiver ready after active session",
    )
    add_serial(
        "regression_bypass",
        "receiver_ready_after_active_session",
        receiver_after_session,
    )
    ui_running_line = line_with(
        serial_text,
        "Instrument UI started; formal CSLP FFT8192: RUNNING",
        "UI RUNNING gate",
    )
    add_serial(
        "regression_bypass",
        "duplicate_ui_running",
        insert_after_line(
            serial_text,
            "Instrument UI started; formal CSLP FFT8192: RUNNING",
            ui_running_line,
            "duplicate UI RUNNING",
        ),
    )
    add_sender(
        "regression_bypass",
        "one_second_sender_wrapper",
        0,
        replace_exact(
            sender_texts[0],
            "Script done on 2026-07-30 21:43:29+08:00",
            "Script done on 2026-07-30 21:43:23+08:00",
            "one-second sender wrapper",
        ),
    )
    add_serial(
        "canonical_hash",
        "otherwise_unbound_harmless_line",
        insert_after_line(
            serial_text,
            "main_task: Returned from app_main()",
            "I (4250) evidence_audit: otherwise unbound harmless marker",
            "canonical hash terminal gate",
        ),
    )

    # Identity linkage: sender identity, board identity, and the v5 image.
    add_sender(
        "identity",
        "sender_session",
        0,
        replace_exact(
            sender_texts[0],
            "session=0xBA29D2A6",
            "session=0xBA29D2A5",
            "sender session",
            2,
        ),
    )
    add_sender(
        "identity",
        "sender_boot",
        0,
        replace_exact(
            sender_texts[0],
            "boot_id=0xF6A0FE13",
            "boot_id=0xF6A0FE12",
            "sender boot",
        ),
    )
    add_sender(
        "identity",
        "sender_config",
        1,
        replace_exact(
            sender_texts[1],
            "config_id=0x0C9EE0FA",
            "config_id=0x0C9EE0FB",
            "sender config",
            2,
        ),
    )
    add_serial(
        "identity",
        "wrong_v5_elf",
        replace_exact(
            serial_text,
            "ELF file SHA256:  6d15ace8d...",
            "ELF file SHA256:  0c36dc583...",
            "v5 ELF",
        ),
    )
    add_serial(
        "identity",
        "board_boot",
        replace_exact(
            serial_text,
            "boot=4137745939 config=2097651348",
            "boot=4137745938 config=2097651348",
            "board boot",
        ),
    )
    add_serial(
        "identity",
        "board_config",
        replace_exact(
            serial_text,
            "boot=3665736797 config=211738874",
            "boot=3665736797 config=211738875",
            "board config",
        ),
    )

    # The entire replay command and successful process/control status are bound.
    add_sender(
        "command",
        "wrong_bind_ip",
        0,
        replace_exact(
            sender_texts[0],
            "--bind-ip 192.168.10.5 --port 50000",
            "--bind-ip 192.168.10.4 --port 50000",
            "bind IP",
        ),
    )
    add_sender(
        "command",
        "wrong_peer_ip",
        1,
        replace_exact(
            sender_texts[1],
            "--peer-ip 192.168.10.3 --peer-port 50001",
            "--peer-ip 192.168.10.2 --peer-port 50001",
            "peer IP",
        ),
    )
    add_sender(
        "command",
        "wrong_frame_argument",
        2,
        replace_exact(
            sender_texts[2],
            "--waveform multitone --frames 100 --chunk-gap-us 250",
            "--waveform multitone --frames 99 --chunk-gap-us 250",
            "frame argument",
        ),
    )
    add_sender(
        "command",
        "wrong_chunk_gap",
        3,
        replace_exact(
            sender_texts[3],
            "--chunk-gap-us 250 --hold-seconds 2",
            "--chunk-gap-us 251 --hold-seconds 2",
            "chunk gap",
        ),
    )
    add_sender(
        "command",
        "nonzero_exit",
        0,
        replace_exact(
            sender_texts[0],
            'COMMAND_EXIT_CODE="0"',
            'COMMAND_EXIT_CODE="1"',
            "sender exit",
        ),
    )
    add_sender(
        "command",
        "nonzero_control_status",
        3,
        replace_exact(
            sender_texts[3],
            "caps=0x0000001F status=0",
            "caps=0x0000001F status=5",
            "HELLO status",
        ),
    )

    # Sender and UART frame/packet/publication/health counts are not inferred.
    add_sender(
        "counts",
        "sender_completion_packets",
        0,
        replace_exact(
            sender_texts[0],
            "completed frames=100 wave_packets=1200",
            "completed frames=100 wave_packets=1199",
            "sender completion",
        ),
    )
    add_sender(
        "counts",
        "missing_sender_checkpoint",
        1,
        remove_line_with(
            sender_texts[1], "sent frame=75 packets=900", "sender checkpoint"
        ),
    )
    add_sender(
        "counts",
        "duplicate_sender_completion",
        2,
        insert_after_line(
            sender_texts[2],
            "completed frames=100 wave_packets=1200",
            "completed frames=100 wave_packets=1200",
            "duplicate sender completion",
        ),
    )
    add_serial(
        "counts",
        "terminal_completed_399",
        replace_exact(
            serial_text,
            "Published frame=100 completed=400",
            "Published frame=100 completed=399",
            "terminal completed",
        ),
    )
    add_serial(
        "counts",
        "missing_frame100_measurement",
        remove_line_with(
            serial_text,
            "measurement: session=BA29D2A6 config=7D079E94 epoch=2 frame=100",
            "frame100 measurement",
        ),
    )
    final_publication = line_with(
        serial_text, "Published frame=100 completed=400", "final publication"
    )
    add_serial(
        "counts",
        "duplicate_publication",
        insert_after_line(
            serial_text,
            "Published frame=100 completed=400",
            final_publication,
            "duplicate publication",
        ),
    )
    add_serial(
        "counts",
        "receiver_packet_count",
        replace_exact(
            serial_text,
            "health/rx: packets=3921",
            "health/rx: packets=3920",
            "receiver packet count",
        ),
    )

    # Measurement tolerances and exact frame/generation/calibration metadata.
    add_serial(
        "numeric",
        "f0_over_tolerance",
        replace_exact(
            serial_text,
            "F0=10000.06Hz",
            "F0=11001.00Hz",
            "F0 tolerance",
            2,
        ),
    )
    add_serial(
        "numeric",
        "vpp_over_tolerance",
        replace_exact(
            serial_text,
            "Vpp=100.004mV",
            "Vpp=106.000mV",
            "Vpp tolerance",
            2,
        ),
    )
    add_serial(
        "numeric",
        "rms_over_tolerance",
        replace_exact(
            serial_text,
            "RMS=35.137mV",
            "RMS=41.000mV",
            "RMS tolerance",
            2,
        ),
    )
    add_serial(
        "numeric",
        "terminal_generation",
        replace_exact(
            serial_text,
            "frame=100 gen=400",
            "frame=100 gen=399",
            "terminal generation",
        ),
    )
    add_serial(
        "numeric",
        "calibration_id",
        replace_on_lines(
            serial_text,
            "measurement: session=BA29D2A9",
            "cal=1 test=1",
            "cal=2 test=1",
            "calibration ID",
            2,
        ),
    )

    # Spectral line count, frequency, amplitude, and unused slots are checked.
    add_serial(
        "spectral_lines",
        "peak_count",
        replace_exact(
            serial_text,
            "RMS=35.137mV peaks=2",
            "RMS=35.137mV peaks=3",
            "peak count",
            2,
        ),
    )
    add_serial(
        "spectral_lines",
        "line_frequency",
        replace_exact(
            serial_text,
            "P1=10000.06Hz/44.444mVpk",
            "P1=11001.00Hz/44.444mVpk",
            "line frequency",
            2,
        ),
    )
    add_serial(
        "spectral_lines",
        "line_amplitude",
        replace_exact(
            serial_text,
            "P1=10000.06Hz/44.444mVpk",
            "P1=10000.06Hz/50.001mVpk",
            "line amplitude",
            2,
        ),
    )
    add_serial(
        "spectral_lines",
        "unused_peak_slot",
        replace_exact(
            serial_text,
            "P2=20000.11Hz/22.224mVpk P3=0.00Hz/0.000mVpk",
            "P2=20000.11Hz/22.224mVpk P3=30000.00Hz/1.000mVpk",
            "unused peak slot",
            2,
        ),
    )

    # Every case must commit exactly one session-bound NEW_STREAM scale.
    add_serial(
        "scale",
        "wrong_amax",
        replace_exact(
            serial_text,
            "session=BA29D2A8 config=93BC3A98 epoch=6 frame=1 "
            "previous=0.0mVpk Amax=50.0mVpk reason=NEW_STREAM",
            "session=BA29D2A8 config=93BC3A98 epoch=6 frame=1 "
            "previous=0.0mVpk Amax=100.0mVpk reason=NEW_STREAM",
            "scale Amax",
        ),
    )
    add_serial(
        "scale",
        "wrong_reason",
        replace_exact(
            serial_text,
            "session=BA29D2A9 config=ECDCFE01 epoch=8 frame=1 "
            "previous=0.0mVpk Amax=100.0mVpk reason=NEW_STREAM",
            "session=BA29D2A9 config=ECDCFE01 epoch=8 frame=1 "
            "previous=0.0mVpk Amax=100.0mVpk reason=UPSHIFT",
            "scale reason",
        ),
    )
    add_serial(
        "scale",
        "wrong_scale_frame",
        replace_exact(
            serial_text,
            "session=BA29D2A7 config=0C9EE0FA epoch=4 frame=1 "
            "previous=0.0mVpk",
            "session=BA29D2A7 config=0C9EE0FA epoch=4 frame=2 "
            "previous=0.0mVpk",
            "scale frame",
        ),
    )
    first_scale = line_with(
        serial_text,
        "Spectrum scale committed: session=BA29D2A6",
        "first scale",
    )
    add_serial(
        "scale",
        "extra_scale_commit",
        insert_after_line(
            serial_text,
            "Spectrum scale committed: session=BA29D2A6",
            first_scale,
            "extra scale",
        ),
    )
    add_serial(
        "scale",
        "missing_scale_commit",
        remove_line_with(
            serial_text,
            "Spectrum scale committed: session=BA29D2A8",
            "missing scale",
        ),
    )

    # Lifecycle proof includes three recoveries plus final4's terminal STALE.
    add_serial(
        "state",
        "missing_final_stale",
        remove_line_with(
            serial_text,
            "last session=BA29D2A9 frame=100; retaining",
            "final stale",
        ),
    )
    add_serial(
        "state",
        "wrong_intercase_stale_frame",
        replace_exact(
            serial_text,
            "last session=BA29D2A7 frame=100; retaining",
            "last session=BA29D2A7 frame=99; retaining",
            "inter-case stale frame",
        ),
    )
    add_serial(
        "state",
        "wrong_recovery_edge",
        replace_exact(
            serial_text,
            "CSLP UI stream state: STALE -> LIVE; session=BA29D2A7 frame=1",
            "CSLP UI stream state: WAITING -> LIVE; session=BA29D2A7 frame=1",
            "recovery edge",
        ),
    )
    final_stale = line_with(
        serial_text,
        "last session=BA29D2A9 frame=100; retaining",
        "terminal stale line",
    )
    add_serial(
        "state",
        "duplicate_final_stale",
        insert_after_line(
            serial_text,
            "last session=BA29D2A9 frame=100; retaining",
            final_stale,
            "duplicate terminal stale",
        ),
    )
    add_serial(
        "state",
        "missing_terminal_peer_silent",
        remove_line_with(
            serial_text,
            "W (38036) cslp_rx: CSLP peer silent",
            "terminal peer silent",
        ),
    )

    # All visible error fields and explicit formal-window failures are fatal.
    add_serial(
        "error_injection",
        "rx_crc",
        replace_exact(serial_text, "session=0 crc=0", "session=0 crc=1", "rx CRC"),
    )
    add_serial(
        "error_injection",
        "frame_incomplete",
        replace_exact(
            serial_text,
            "overwrite=0 incomplete=0 duplicate=0",
            "overwrite=0 incomplete=1 duplicate=0",
            "frame incomplete",
        ),
    )
    add_serial(
        "error_injection",
        "reject_metadata",
        replace_exact(
            serial_text,
            "config=0 metadata=0 overrange=0",
            "config=0 metadata=1 overrange=0",
            "reject metadata",
        ),
    )
    add_serial(
        "error_injection",
        "socket_recv_fatal",
        replace_exact(
            serial_text,
            "open_fail=0 recv_fatal=0 close_fail=0",
            "open_fail=0 recv_fatal=1 close_fail=0",
            "socket fatal",
        ),
    )
    add_serial(
        "error_injection",
        "pipeline_failure",
        replace_exact(
            serial_text,
            "stale=0 invalid=0 failures=0",
            "stale=0 invalid=0 failures=1",
            "pipeline failure",
        ),
    )
    add_serial(
        "error_injection",
        "pipeline_selftest_fail",
        replace_exact(
            serial_text,
            "selftest=PASS max_ui_gap=255ms",
            "selftest=FAIL max_ui_gap=255ms",
            "pipeline self-test",
        ),
    )
    add_serial(
        "error_injection",
        "formal_esp_error",
        insert_after_line(
            serial_text,
            "CSLP session ready: session=0xBA29D2A7",
            "E (13061) cyclescope_pipe: injected formal error",
            "formal ESP error",
        ),
    )
    add_serial(
        "error_injection",
        "measurement_rejected",
        insert_after_line(
            serial_text,
            "measurement: session=BA29D2A8 config=93BC3A98 epoch=6 "
            "frame=1 gen=201",
            "W (21575) cyclescope_pipe: Measurement rejected: injected",
            "measurement rejection",
        ),
    )

    # Whole-log concatenation and valid-log position splicing must not pass.
    add_sender(
        "log_concatenation",
        "sender_self_concatenation",
        0,
        sender_texts[0] + sender_texts[0],
    )
    add_serial(
        "log_concatenation",
        "uart_self_concatenation",
        serial_text + serial_text,
    )
    swapped = list(sender_texts)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    mutations.append(
        Mutation(
            "log_concatenation",
            "valid_sender_position_splice",
            tuple(swapped),
            serial_text,
        )
    )

    # A valid transcript from one frozen profile cannot satisfy the other.
    mutations.extend(
        (
            Mutation(
                "profile_mix",
                "final2_senders_with_final4_uart",
                final2_sender_texts,
                serial_text,
                "final4",
            ),
            Mutation(
                "profile_mix",
                "final4_senders_with_final2_uart",
                sender_texts,
                final2_serial_text,
                "final4",
            ),
            Mutation(
                "profile_mix",
                "final4_logs_claimed_as_final2",
                sender_texts,
                serial_text,
                "final2",
            ),
        )
    )
    false_final2_terminal = final2_serial_text + (
        "W (101380) cslp_rx: CSLP peer silent for more than 1500 ms; "
        "starting a new session\n"
        "W (101480) cyclescope_ui: CSLP UI stream state: LIVE -> STALE; "
        "last session=62CBA83C frame=100; retaining waveform and "
        "measurements\n"
    )
    mutations.append(
        Mutation(
            "final2_regression",
            "fabricated_uncaptured_terminal_stale",
            final2_sender_texts,
            false_final2_terminal,
            "final2",
        )
    )

    expected_categories = {
        "identity",
        "command",
        "counts",
        "numeric",
        "spectral_lines",
        "scale",
        "state",
        "error_injection",
        "log_concatenation",
        "regression_bypass",
        "profile_mix",
        "final2_regression",
        "canonical_hash",
    }
    category_counts = Counter(mutation.category for mutation in mutations)
    if set(category_counts) != expected_categories or any(
        count == 0 for count in category_counts.values()
    ):
        raise RuntimeError(f"adversarial category coverage changed: {category_counts}")
    for mutation in mutations:
        expect_reject(mutation)
    expect_cli_conflict_reject()

    counts = ",".join(
        f"{category}={category_counts[category]}"
        for category in sorted(category_counts)
    )
    print(
        "CycleScope normal-v5 boundary adversarial PASS: "
        "baseline=final4+final2; "
        f"rejected={len(mutations)}+cli1; {counts}; temp=/tmp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
