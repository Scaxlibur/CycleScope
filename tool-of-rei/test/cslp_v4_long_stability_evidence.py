#!/usr/bin/env python3
"""Validate the matched sender/UART evidence for the normal-v4 10k run.

The historical current-v3 fixture is intentionally left byte-for-byte intact.
This module reuses its already adversarial-tested protocol/log parser while
applying a scoped v4 image, memory, and Spectrum UI profile.  It is read-only:
it opens no socket or UART and writes no result file.

A PASS proves only the PC-synthetic CSLP receive/analyze/publish stability
window.  It does not replace panel-flush, physical-front-end, or real-FPGA
acceptance work.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import re
from typing import Iterator

import cslp_v3_long_stability_evidence as legacy


# Re-export the frozen protocol/log vocabulary used by the corresponding v4
# adversarial test.  No assignment below mutates the v3 source file.
common = legacy.common
StabilitySummary = legacy.StabilitySummary
EXPECTED_FRAMES = legacy.EXPECTED_FRAMES
EXPECTED_WAVE_PACKETS = legacy.EXPECTED_WAVE_PACKETS
EXPECTED_CHUNK_GAP_US = legacy.EXPECTED_CHUNK_GAP_US
EXPECTED_HOLD_SECONDS = legacy.EXPECTED_HOLD_SECONDS
EXPECTED_HANDSHAKE_TIMEOUT_SECONDS = legacy.EXPECTED_HANDSHAKE_TIMEOUT_SECONDS
EXPECTED_TONES = legacy.EXPECTED_TONES
EXPECTED_FUNDAMENTAL_HZ = legacy.EXPECTED_FUNDAMENTAL_HZ
SCRIPT_START_PATTERN = legacy.SCRIPT_START_PATTERN
SCRIPT_DONE_PATTERN = legacy.SCRIPT_DONE_PATTERN

DEFAULT_SENDER_LOG = Path(
    "/tmp/cyclescope-p4-v4-final-10000-emulator.log"
)
DEFAULT_SERIAL_LOG = Path(
    "/tmp/cyclescope-p4-v4-final-10000-serial.log"
)
DEFAULT_ELF = Path(
    "/tmp/cyclescope-p4-normal-host-v4/CycleScopeP4.elf"
)
DEFAULT_BIN = Path(
    "/tmp/cyclescope-p4-normal-host-v4/CycleScopeP4.bin"
)

EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA256 = (
    "0c36dc5833d666c2e467ca07887779b5b3bc19344f7033aa62dddbad22fe1ee0"
)
EXPECTED_BOARD_BIN_SHA256 = (
    "b4793a9f65befbb418ffd159cd42cc185cbbec5afeea5c3a2f61013a5cd934f7"
)
EXPECTED_BOARD_ELF_SHA_PREFIX = EXPECTED_BOARD_ELF_SHA256[:9]
EXPECTED_LEGACY_PARSER_SHA256 = (
    "7ec50b210ac41a85cb81d1db564751c03561c89fe999fd458e6fea946f57fca6"
)
EXPECTED_COMMON_PARSER_SHA256 = (
    "abb42513ca05c290980ac9805a364410e5b625abf9eb8452f2ecd79aad919896"
)
EXPECTED_SENDER_SOURCE_SHA256 = (
    "0e07aa86a63ea9f0f7f9249c6a01759a5e4c27775f84cca4b5efbc2386a0032c"
)

# Frozen from all health records in the completed formal v4 10k window.  The
# run contains 16 timer-driven UI snapshots (its stream ends before a possible
# seventeenth callback) plus the fixed 600..9600 pipeline checkpoints.
EXPECTED_UI_HEALTH_SNAPSHOTS = 16
EXPECTED_RAW_RECEIVER_HEALTH_SNAPSHOTS = 18
EXPECTED_CANONICAL_RECEIVER_HEALTH_SNAPSHOTS = 17
EXPECTED_PIPE_PSRAM_FREE: int | None = 28_041_452
MINIMUM_UI_FREE: int | None = 28_128_244

EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV = 100.0
SPECTRUM_UI_PATTERN = re.compile(
    rf"^I \([0-9]+\) cyclescope_ui: Spectrum UI bridge on Core 0: "
    rf"session=(?P<session>[0-9A-Fa-f]{{8}}) "
    rf"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    rf"A/B=(?P<buffer_sentinel>[0-9]+) "
    rf"columns=(?P<columns>[0-9]+) peaks=(?P<peak_count>[0-9]+) "
    rf"Fs=(?P<sample_rate>{common.DECIMAL_PATTERN})MHz "
    rf"axis=(?P<axis>{common.DECIMAL_PATTERN})MHz "
    rf"Amax=(?P<amplitude_max>{common.DECIMAL_PATTERN})mVpk$",
    re.MULTILINE,
)


def validate_definitions() -> None:
    legacy.validate_definitions()
    dependency_hashes = (
        (_sha256(Path(legacy.__file__)), EXPECTED_LEGACY_PARSER_SHA256),
        (_sha256(Path(common.__file__)), EXPECTED_COMMON_PARSER_SHA256),
        (
            _sha256(Path(common.boundary.emulator.__file__)),
            EXPECTED_SENDER_SOURCE_SHA256,
        ),
    )
    if (
        len(EXPECTED_BOARD_ELF_SHA256) != 64
        or len(EXPECTED_BOARD_BIN_SHA256) != 64
        or EXPECTED_BOARD_ELF_SHA_PREFIX != "0c36dc583"
        or any(actual != expected for actual, expected in dependency_hashes)
        or DEFAULT_SENDER_LOG == legacy.DEFAULT_SENDER_LOG
        or DEFAULT_SERIAL_LOG == legacy.DEFAULT_SERIAL_LOG
        or not math.isclose(
            EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV,
            100.0,
            abs_tol=0.000001,
        )
    ):
        raise RuntimeError("normal-v4 evidence identity/profile changed")


def _memory_profile() -> tuple[int, int]:
    if EXPECTED_PIPE_PSRAM_FREE is None or MINIMUM_UI_FREE is None:
        raise RuntimeError(
            "normal-v4 10k memory profile is pending the completed formal UART log"
        )
    if EXPECTED_PIPE_PSRAM_FREE <= 0 or MINIMUM_UI_FREE <= 0:
        raise RuntimeError("normal-v4 10k memory profile is invalid")
    return EXPECTED_PIPE_PSRAM_FREE, MINIMUM_UI_FREE


def _line_for_match(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    return text[start:end]


def _canonicalize_terminal_receiver_health(text: str) -> tuple[str, int]:
    ready_matches = list(common.BOARD_SESSION_PATTERN.finditer(text))
    peer_silent = re.search(
        r"CSLP peer silent for more than 1500 ms; starting a new session",
        text,
    )
    if len(ready_matches) != 1 or peer_silent is None:
        raise RuntimeError("normal-v4 receiver formal-window markers changed")
    ready = ready_matches[0]
    formal_end = peer_silent.start()
    receiver_sets = tuple(
        tuple(
            match
            for match in pattern.finditer(text)
            if ready.end() < match.start() < formal_end
        )
        for pattern in (
            common.RX_HEALTH_PATTERN,
            common.FRAME_HEALTH_PATTERN,
            common.REJECT_HEALTH_PATTERN,
            common.SOCKET_HEALTH_PATTERN,
        )
    )
    if tuple(len(matches) for matches in receiver_sets) != (
        EXPECTED_RAW_RECEIVER_HEALTH_SNAPSHOTS,
    ) * 4:
        raise RuntimeError("normal-v4 raw receiver health cardinality changed")

    rx_health, frame_health, reject_health, socket_health = receiver_sets
    common.require_zero_health(
        rx_health,
        ("source", "magic", "version", "length", "session", "crc"),
        "normal-v4 raw receiver/rx",
    )
    common.require_zero_health(
        frame_health,
        ("overwrite", "incomplete", "duplicate", "stale", "busy"),
        "normal-v4 raw receiver/frame",
    )
    common.require_zero_health(
        reject_health,
        ("config", "metadata", "overrange", "fifo"),
        "normal-v4 raw receiver/reject",
    )
    common.require_zero_health(
        socket_health,
        ("open_fail", "recv_fatal", "close_fail"),
        "normal-v4 raw receiver/socket",
    )
    for index in range(EXPECTED_RAW_RECEIVER_HEALTH_SNAPSHOTS):
        positions = tuple(matches[index].start() for matches in receiver_sets)
        if not positions[0] < positions[1] < positions[2] < positions[3]:
            raise RuntimeError("normal-v4 raw receiver health ordering changed")

    completed = tuple(int(match.group("completed")) for match in frame_health)
    acquired = tuple(int(match.group("acquired")) for match in frame_health)
    packets = tuple(int(match.group("packets")) for match in rx_health)
    retry_pairs = tuple(
        (int(match.group("retries")), int(match.group("reconnects")))
        for match in reject_health
    )
    socket_sessions = tuple(
        int(match.group("sessions")) for match in socket_health
    )
    if (
        completed[-2:] != (EXPECTED_FRAMES, EXPECTED_FRAMES)
        or acquired != completed
        or any(
            current <= previous
            for previous, current in zip(
                completed[:-2], completed[1:-1], strict=True
            )
        )
        or any(
            current <= previous
            for previous, current in zip(packets[:-1], packets[1:], strict=True)
        )
        or packets[-1] < EXPECTED_WAVE_PACKETS
        or len(set(retry_pairs)) != 1
        or set(socket_sessions) != {1}
    ):
        raise RuntimeError("normal-v4 raw receiver terminal hold profile changed")

    # The second completed=10000 set is a valid 30-second hold-tail snapshot.
    # Validate it above, then omit only that redundant set from the canonical
    # view consumed by the historical strictly-increasing parser.
    redundant_block = "\n".join(
        _line_for_match(text, matches[-1]) for matches in receiver_sets
    )
    terminated_block = redundant_block + "\n"
    if text.count(terminated_block) != 1:
        raise RuntimeError("normal-v4 terminal receiver health block changed")
    canonical_text = text.replace(terminated_block, "", 1)
    return canonical_text, len(frame_health)


@contextmanager
def _scoped_legacy_v4_profile(canonical_uart: str) -> Iterator[None]:
    expected_psram_free, minimum_ui_free = _memory_profile()
    original_read_ascii_log = legacy.read_ascii_log

    def read_profiled_log(path: Path, label: str) -> str:
        if label == "UART":
            return canonical_uart
        return original_read_ascii_log(path, label)

    overrides = (
        (
            common,
            "EXPECTED_BOARD_APP_VERSION",
            EXPECTED_BOARD_APP_VERSION,
        ),
        (
            common,
            "EXPECTED_BOARD_ELF_SHA_PREFIX",
            EXPECTED_BOARD_ELF_SHA_PREFIX,
        ),
        (common, "SPECTRUM_UI_PATTERN", SPECTRUM_UI_PATTERN),
        (
            legacy,
            "EXPECTED_RECEIVER_HEALTH_SNAPSHOTS",
            EXPECTED_CANONICAL_RECEIVER_HEALTH_SNAPSHOTS,
        ),
        (
            legacy,
            "EXPECTED_UI_HEALTH_SNAPSHOTS",
            EXPECTED_UI_HEALTH_SNAPSHOTS,
        ),
        (legacy, "EXPECTED_PIPE_PSRAM_FREE", expected_psram_free),
        (legacy, "MINIMUM_UI_FREE", minimum_ui_free),
        (legacy, "read_ascii_log", read_profiled_log),
    )
    saved = tuple((owner, name, getattr(owner, name)) for owner, name, _ in overrides)
    try:
        for owner, name, value in overrides:
            setattr(owner, name, value)
        yield
    finally:
        for owner, name, value in reversed(saved):
            setattr(owner, name, value)


def validate_sender_log(path: Path) -> common.SenderEvidence:
    # The formal v4 run deliberately keeps the v3 long-run waveform and exact
    # .5:50000 -> .3:50001 sender command for direct stability comparison.
    return legacy.validate_sender_log(path)


def validate_serial_log(
    path: Path,
    sender: common.SenderEvidence,
) -> StabilitySummary:
    text = legacy.read_ascii_log(path, "UART")
    canonical_text, raw_receiver_health_count = (
        _canonicalize_terminal_receiver_health(text)
    )
    with _scoped_legacy_v4_profile(canonical_text):
        summary = legacy.validate_serial_log(path, sender)

    ui_matches = list(SPECTRUM_UI_PATTERN.finditer(text))
    if len(ui_matches) != 1 or not math.isclose(
        float(ui_matches[0].group("amplitude_max")),
        EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV,
        abs_tol=0.000001,
    ):
        raise RuntimeError("UART normal-v4 Spectrum amplitude profile changed")
    return replace(
        summary,
        receiver_health_snapshots=raw_receiver_health_count,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_artifacts(elf_path: Path, bin_path: Path) -> None:
    actual_elf = _sha256(elf_path)
    actual_bin = _sha256(bin_path)
    if (
        actual_elf != EXPECTED_BOARD_ELF_SHA256
        or actual_bin != EXPECTED_BOARD_BIN_SHA256
    ):
        raise RuntimeError(
            "normal-v4 build artifact identity changed: "
            f"elf={actual_elf} bin={actual_bin}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument("--current-logs", action="store_true")
    parser.add_argument("--sender-log", type=Path)
    parser.add_argument("--serial-log", type=Path)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--bin", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_definitions()
    if args.self_test_only and not (
        args.current_logs
        or args.sender_log
        or args.serial_log
        or args.elf
        or args.bin
    ):
        print("CycleScope normal-v4 10k stability definitions passed")
        return 0

    sender_log = DEFAULT_SENDER_LOG if args.current_logs else args.sender_log
    serial_log = DEFAULT_SERIAL_LOG if args.current_logs else args.serial_log
    if sender_log is None or serial_log is None:
        raise SystemExit("provide --current-logs or both --sender-log/--serial-log")

    if args.current_logs:
        elf_path = args.elf if args.elf is not None else DEFAULT_ELF
        bin_path = args.bin if args.bin is not None else DEFAULT_BIN
    else:
        if (args.elf is None) != (args.bin is None):
            raise SystemExit("provide both --elf and --bin, or neither")
        elf_path = args.elf
        bin_path = args.bin
    artifact_status = "UART_PREFIX_ONLY"
    if elf_path is not None and bin_path is not None:
        validate_artifacts(elf_path, bin_path)
        artifact_status = "FULL_SHA_PASS"

    sender = validate_sender_log(sender_log)
    summary = validate_serial_log(serial_log, sender)
    print(
        "CycleScope normal-v4 10k stability evidence passed: "
        f"session={summary.session_id} frames={EXPECTED_FRAMES} "
        f"wave_packets={EXPECTED_WAVE_PACKETS} checkpoints=101 "
        f"receiver_health={summary.receiver_health_snapshots} "
        f"pipeline_health={summary.pipeline_health_snapshots} "
        f"fft_us(avg/max)={summary.average_fft_us}/"
        f"{summary.maximum_fft_us} internal_min={summary.minimum_internal_free} "
        f"psram_free={summary.psram_free} "
        f"stream_ms={summary.stream_duration_ms} "
        "max_error(F0/voltage/tone_f/tone_A)="
        f"{summary.max_f0_error_hz:.3f}Hz/"
        f"{summary.max_voltage_error_mv:.3f}mV/"
        f"{summary.max_tone_frequency_error_hz:.3f}Hz/"
        f"{summary.max_tone_amplitude_error_mv:.3f}mV "
        f"Amax={EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV:.1f}mVpk "
        f"artifacts={artifact_status} "
        "path=.5:50000->.3:50001; digital_stability=PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
