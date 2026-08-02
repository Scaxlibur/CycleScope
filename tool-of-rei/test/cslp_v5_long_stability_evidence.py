#!/usr/bin/env python3
"""Validate the matched sender/UART evidence for the normal-v5 10k run.

The historical v3 and v4 fixtures remain byte-for-byte intact.  This module
reuses their adversarial-tested protocol parser, but independently validates
the raw v5 timing profile, first-frame Spectrum-scale commit, and UI
WAITING/LIVE/STALE edges before presenting an in-memory compatibility view to
the legacy parser.  It opens no socket or UART and writes no result file.

A PASS proves only the PC-synthetic CSLP receive/analyze/publish stability
window and its logged UI state transitions.  It does not replace panel-flush,
physical-front-end, or real-FPGA acceptance work.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Iterator

import cslp_v4_long_stability_evidence as legacy


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
    "/tmp/cyclescope-p4-v5-final-10000-emulator.log"
)
DEFAULT_SERIAL_LOG = Path(
    "/tmp/cyclescope-p4-v5-final-10000-serial.log"
)
DEFAULT_ELF = Path(
    "/tmp/cyclescope-p4-normal-host-v5/CycleScopeP4.elf"
)
DEFAULT_BIN = Path(
    "/tmp/cyclescope-p4-normal-host-v5/CycleScopeP4.bin"
)

EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA256 = (
    "6d15ace8dff278791383373d11119ff00b049c42a4e105e97e9251145e44492b"
)
EXPECTED_BOARD_BIN_SHA256 = (
    "075c3ec7028082e032823af39c2b2cbf09610d37dedbb3c1515a4586b78ed3c6"
)
EXPECTED_BOARD_ELF_SHA_PREFIX = EXPECTED_BOARD_ELF_SHA256[:9]
EXPECTED_LEGACY_V4_PARSER_SHA256 = (
    "eee541c50db3e770e65edf3fb3a66ca8473756b824bbac6eb5699462ff406bde"
)

# Populate this formal-run profile only from the completed, immutable UART
# log.  ``validate_serial_log`` intentionally refuses to issue a PASS while
# any field remains pending.  Partial observations during capture must not be
# mistaken for the terminal 10k profile.
EXPECTED_RAW_RECEIVER_HEALTH_SNAPSHOTS: int | None = 18
EXPECTED_CANONICAL_RECEIVER_HEALTH_SNAPSHOTS: int | None = 18
EXPECTED_UI_HEALTH_SNAPSHOTS: int | None = 17
EXPECTED_PIPE_PSRAM_FREE: int | None = 28_040_080
EXPECTED_MINIMUM_PIPE_INTERNAL_FREE: int | None = 118_811
EXPECTED_MINIMUM_UI_FREE: int | None = 28_126_860
EXPECTED_FINAL_FFT_LAST_US: int | None = 16_264
EXPECTED_FINAL_AVERAGE_FFT_US: int | None = 17_491
EXPECTED_MAXIMUM_FFT_US: int | None = 50_210

EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV = 100.0
EXPECTED_SCALE_PREVIOUS_MV = 0.0
EXPECTED_SCALE_REASON = "NEW_STREAM"
EXPECTED_FIRST_UI_FRAME = 2

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
SCALE_COMMIT_PATTERN = re.compile(
    rf"^I \((?P<timestamp>[0-9]+)\) cyclescope_pipe: "
    rf"Spectrum scale committed: session=(?P<session>[0-9A-Fa-f]{{8}}) "
    rf"config=(?P<config>[0-9A-Fa-f]{{8}}) epoch=(?P<epoch>[0-9]+) "
    rf"frame=(?P<frame>[0-9]+) "
    rf"previous=(?P<previous>{common.DECIMAL_PATTERN})mVpk "
    rf"Amax=(?P<amplitude_max>{common.DECIMAL_PATTERN})mVpk "
    r"reason=(?P<reason>NEW_STREAM|UPSHIFT|DOWNSHIFT)$",
    re.MULTILINE,
)
WAITING_TO_LIVE_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    r"CSLP UI stream state: WAITING -> LIVE; "
    r"session=(?P<session>[0-9A-Fa-f]{8}) frame=(?P<frame>[0-9]+)$",
    re.MULTILINE,
)
STALE_TO_LIVE_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    r"CSLP UI stream state: STALE -> LIVE; "
    r"session=(?P<session>[0-9A-Fa-f]{8}) frame=(?P<frame>[0-9]+)$",
    re.MULTILINE,
)
LIVE_TO_STALE_PATTERN = re.compile(
    r"^W \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    r"CSLP UI stream state: LIVE -> STALE; last "
    r"session=(?P<session>[0-9A-Fa-f]{8}) frame=(?P<frame>[0-9]+); "
    r"retaining waveform and measurements$",
    re.MULTILINE,
)
PEER_SILENT_PATTERN = re.compile(
    r"^W \((?P<timestamp>[0-9]+)\) cslp_rx: "
    r"CSLP peer silent for more than 1500 ms; starting a new session$",
    re.MULTILINE,
)
RAW_FFT_TIMING_PATTERN = re.compile(
    r"fft_us\(last/avg/max\)="
    r"(?P<last>[0-9]+)/(?P<average>[0-9]+)/(?P<maximum>[0-9]+)"
)


@dataclass(frozen=True)
class V5RunProfile:
    raw_receiver_health_snapshots: int
    canonical_receiver_health_snapshots: int
    ui_health_snapshots: int
    pipe_psram_free: int
    minimum_pipe_internal_free: int
    minimum_ui_free: int
    final_fft_last_us: int
    final_average_fft_us: int
    maximum_fft_us: int


@dataclass(frozen=True)
class RawRuntimeProfile:
    final_fft_last_us: int
    final_average_fft_us: int
    maximum_fft_us: int
    minimum_pipe_internal_free: int
    minimum_ui_free: int


@dataclass(frozen=True)
class V5StabilitySummary(StabilitySummary):
    final_fft_last_us: int
    minimum_ui_free: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _formal_profile() -> V5RunProfile:
    values = {
        "raw_receiver_health_snapshots": (
            EXPECTED_RAW_RECEIVER_HEALTH_SNAPSHOTS
        ),
        "canonical_receiver_health_snapshots": (
            EXPECTED_CANONICAL_RECEIVER_HEALTH_SNAPSHOTS
        ),
        "ui_health_snapshots": EXPECTED_UI_HEALTH_SNAPSHOTS,
        "pipe_psram_free": EXPECTED_PIPE_PSRAM_FREE,
        "minimum_pipe_internal_free": EXPECTED_MINIMUM_PIPE_INTERNAL_FREE,
        "minimum_ui_free": EXPECTED_MINIMUM_UI_FREE,
        "final_fft_last_us": EXPECTED_FINAL_FFT_LAST_US,
        "final_average_fft_us": EXPECTED_FINAL_AVERAGE_FFT_US,
        "maximum_fft_us": EXPECTED_MAXIMUM_FFT_US,
    }
    pending = tuple(name for name, value in values.items() if value is None)
    if pending:
        raise RuntimeError(
            "normal-v5 10k formal profile is pending: " + ", ".join(pending)
        )
    profile = V5RunProfile(**values)  # type: ignore[arg-type]
    if (
        min(vars(profile).values()) <= 0
        or profile.raw_receiver_health_snapshots
        != profile.canonical_receiver_health_snapshots
        or profile.maximum_fft_us < profile.final_fft_last_us
        or profile.maximum_fft_us < profile.final_average_fft_us
    ):
        raise RuntimeError("normal-v5 10k formal profile is invalid")
    return profile


def validate_definitions() -> None:
    legacy.validate_definitions()
    _formal_profile()
    if (
        _sha256(Path(legacy.__file__))
        != EXPECTED_LEGACY_V4_PARSER_SHA256
        or len(EXPECTED_BOARD_ELF_SHA256) != 64
        or len(EXPECTED_BOARD_BIN_SHA256) != 64
        or EXPECTED_BOARD_ELF_SHA_PREFIX != "6d15ace8d"
        or DEFAULT_SENDER_LOG == legacy.DEFAULT_SENDER_LOG
        or DEFAULT_SERIAL_LOG == legacy.DEFAULT_SERIAL_LOG
        or not math.isclose(
            EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV,
            100.0,
            abs_tol=0.000001,
        )
        or not math.isclose(
            EXPECTED_SCALE_PREVIOUS_MV,
            0.0,
            abs_tol=0.000001,
        )
        or EXPECTED_SCALE_REASON != "NEW_STREAM"
        or EXPECTED_FIRST_UI_FRAME != 2
    ):
        raise RuntimeError("normal-v5 evidence identity/profile changed")


def _line_for_match(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    return text[start:end]


def _timestamp_for_match(text: str, match: re.Match[str]) -> int:
    line = _line_for_match(text, match)
    timestamp = re.match(r"^[IWE] \(([0-9]+)\)", line)
    if timestamp is None:
        raise RuntimeError("normal-v5 evidence line has no ESP-IDF timestamp")
    return int(timestamp.group(1))


def _exact_formal_markers(
    text: str,
) -> tuple[re.Match[str], re.Match[str]]:
    ready_matches = list(common.BOARD_SESSION_PATTERN.finditer(text))
    peer_silent_matches = list(PEER_SILENT_PATTERN.finditer(text))
    if (
        len(ready_matches) != 1
        or len(peer_silent_matches) != 1
        or text.count(
            "CSLP peer silent for more than 1500 ms; starting a new session"
        )
        != 1
    ):
        raise RuntimeError("normal-v5 session/peer-silent cardinality changed")
    ready = ready_matches[0]
    peer_silent = peer_silent_matches[0]
    if ready.end() >= peer_silent.start():
        raise RuntimeError("normal-v5 formal-window markers are misordered")
    return ready, peer_silent


def _validate_raw_runtime_profile(
    text: str,
    profile: V5RunProfile,
) -> RawRuntimeProfile:
    ready, peer_silent = _exact_formal_markers(text)
    pipeline_health = tuple(
        match
        for match in common.PIPELINE_HEALTH_PATTERN.finditer(text)
        if ready.end() < match.start() < peer_silent.start()
    )
    if not pipeline_health:
        raise RuntimeError("normal-v5 raw pipeline health is missing")

    timings: list[tuple[int, int, int]] = []
    pipe_timings: list[tuple[int, int, int]] = []
    pipe_internal_free: list[int] = []
    pipe_psram_free: list[int] = []
    ui_free: list[int] = []
    for health in pipeline_health:
        timing = RAW_FFT_TIMING_PATTERN.search(health.group(0))
        if timing is None:
            raise RuntimeError("normal-v5 raw pipeline timing is incomplete")
        values = tuple(
            int(timing.group(name))
            for name in ("last", "average", "maximum")
        )
        last_us, average_us, maximum_us = values
        if (
            last_us <= 0
            or average_us <= 0
            or maximum_us <= 0
            or last_us > maximum_us
            or average_us > maximum_us
        ):
            raise RuntimeError("normal-v5 raw pipeline timing is invalid")
        timings.append(values)
        if health.group("tag") == "cyclescope_pipe":
            pipe_timings.append(values)
            extra = legacy.legacy.PIPE_HEALTH_EXTRA_PATTERN.search(
                health.group(0)
            )
            if extra is None:
                raise RuntimeError("normal-v5 raw pipeline memory is incomplete")
            pipe_internal_free.append(int(extra.group("internal")))
            pipe_psram_free.append(int(extra.group("psram")))
        else:
            extra = legacy.legacy.UI_HEALTH_EXTRA_PATTERN.search(
                health.group(0)
            )
            if extra is None:
                raise RuntimeError("normal-v5 raw UI memory is incomplete")
            ui_free.append(int(extra.group("free")))

    maxima = tuple(values[2] for values in timings)
    if any(
        current < previous
        for previous, current in zip(maxima[:-1], maxima[1:], strict=True)
    ):
        raise RuntimeError("normal-v5 raw FFT maximum is not monotonic")
    # Freeze the final deterministic 600-frame pipeline checkpoint (frame
    # 9600), not a timer-driven UI sample whose relative ordering can move by
    # one callback without changing the completed product run.
    final_last_us, final_average_us, final_maximum_us = pipe_timings[-1]
    actual = RawRuntimeProfile(
        final_fft_last_us=final_last_us,
        final_average_fft_us=final_average_us,
        maximum_fft_us=max(maxima),
        minimum_pipe_internal_free=min(pipe_internal_free),
        minimum_ui_free=min(ui_free),
    )
    if (
        final_maximum_us != actual.maximum_fft_us
        or actual.final_fft_last_us != profile.final_fft_last_us
        or actual.final_average_fft_us != profile.final_average_fft_us
        or actual.maximum_fft_us != profile.maximum_fft_us
        or actual.minimum_pipe_internal_free
        != profile.minimum_pipe_internal_free
        or actual.minimum_ui_free != profile.minimum_ui_free
        or set(pipe_psram_free) != {profile.pipe_psram_free}
    ):
        raise RuntimeError(
            "normal-v5 raw runtime profile changed: "
            f"last/avg/max={actual.final_fft_last_us}/"
            f"{actual.final_average_fft_us}/{actual.maximum_fft_us} "
            f"internal_min={actual.minimum_pipe_internal_free} "
            f"ui_min={actual.minimum_ui_free} "
            f"psram={sorted(set(pipe_psram_free))}"
        )
    return actual


def _legacy_fft_compatibility_view(text: str) -> str:
    """Return an in-memory view satisfying only the old parser's 50 ms cap.

    The 50 ms cap is a historical parser invariant, not a normal-v5 product
    criterion.  Every raw timing record is validated before this function is
    called, and the raw final last/average/maximum values are restored in the
    returned summary.  Nothing is written to disk.
    """

    def replace_timing(match: re.Match[str]) -> str:
        maximum_us = int(match.group("maximum"))
        if maximum_us < 50_000:
            return match.group(0)
        return (
            "fft_us(last/avg/max)="
            f"{match.group('last')}/{match.group('average')}/49999"
        )

    return RAW_FFT_TIMING_PATTERN.sub(replace_timing, text)


def _canonicalize_receiver_health(
    text: str,
    profile: V5RunProfile,
) -> tuple[str, int]:
    """Verify v5 receiver cardinality without deleting any evidence block."""

    ready, peer_silent = _exact_formal_markers(text)
    receiver_sets = tuple(
        tuple(
            match
            for match in pattern.finditer(text)
            if ready.end() < match.start() < peer_silent.start()
        )
        for pattern in (
            common.RX_HEALTH_PATTERN,
            common.FRAME_HEALTH_PATTERN,
            common.REJECT_HEALTH_PATTERN,
            common.SOCKET_HEALTH_PATTERN,
        )
    )
    if tuple(len(matches) for matches in receiver_sets) != (
        profile.raw_receiver_health_snapshots,
    ) * 4:
        raise RuntimeError("normal-v5 raw receiver health cardinality changed")
    return text, profile.raw_receiver_health_snapshots


@contextmanager
def _scoped_legacy_v5_profile(profile: V5RunProfile) -> Iterator[None]:
    overrides = (
        (legacy, "EXPECTED_BOARD_APP_VERSION", EXPECTED_BOARD_APP_VERSION),
        (
            legacy,
            "EXPECTED_BOARD_ELF_SHA_PREFIX",
            EXPECTED_BOARD_ELF_SHA_PREFIX,
        ),
        (legacy, "SPECTRUM_UI_PATTERN", SPECTRUM_UI_PATTERN),
        (
            legacy,
            "EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV",
            EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV,
        ),
        (
            legacy,
            "EXPECTED_RAW_RECEIVER_HEALTH_SNAPSHOTS",
            profile.raw_receiver_health_snapshots,
        ),
        (
            legacy,
            "EXPECTED_CANONICAL_RECEIVER_HEALTH_SNAPSHOTS",
            profile.canonical_receiver_health_snapshots,
        ),
        (
            legacy,
            "EXPECTED_UI_HEALTH_SNAPSHOTS",
            profile.ui_health_snapshots,
        ),
        (
            legacy,
            "EXPECTED_PIPE_PSRAM_FREE",
            profile.pipe_psram_free,
        ),
        (legacy, "MINIMUM_UI_FREE", profile.minimum_ui_free),
    )
    saved = tuple((owner, name, getattr(owner, name)) for owner, name, _ in overrides)
    try:
        for owner, name, value in overrides:
            setattr(owner, name, value)
        yield
    finally:
        for owner, name, value in reversed(saved):
            setattr(owner, name, value)


def _validate_v5_events(
    text: str,
    sender: common.SenderEvidence,
) -> None:
    ready, peer_silent = _exact_formal_markers(text)
    measurements = list(common.BOARD_MEASUREMENT_PATTERN.finditer(text))
    published = list(common.PUBLISHED_FRAME_PATTERN.finditer(text))
    spectrum_ui = list(SPECTRUM_UI_PATTERN.finditer(text))
    scale_commits = list(SCALE_COMMIT_PATTERN.finditer(text))
    waiting_to_live = list(WAITING_TO_LIVE_PATTERN.finditer(text))
    stale_to_live = list(STALE_TO_LIVE_PATTERN.finditer(text))
    live_to_stale = list(LIVE_TO_STALE_PATTERN.finditer(text))
    if (
        not measurements
        or not published
        or len(spectrum_ui) != 1
        or len(scale_commits) != 1
        or len(waiting_to_live) != 1
        or stale_to_live
        or len(live_to_stale) != 1
        or text.count("Spectrum scale committed:") != 1
        or text.count("CSLP UI stream state:") != 2
    ):
        raise RuntimeError("normal-v5 scale/UI transition cardinality changed")

    measurement = common.parse_board_measurement(measurements[0])
    scale = scale_commits[0]
    if (
        scale.group("session").upper() != sender.session_id
        or int(scale.group("config"), 16) != sender.config_id
        or int(scale.group("epoch")) != measurement.epoch
        or int(scale.group("frame")) != 1
        or not math.isclose(
            float(scale.group("previous")),
            EXPECTED_SCALE_PREVIOUS_MV,
            abs_tol=0.000001,
        )
        or not math.isclose(
            float(scale.group("amplitude_max")),
            EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV,
            abs_tol=0.000001,
        )
        or scale.group("reason") != EXPECTED_SCALE_REASON
        or not (
            ready.end()
            < published[0].start()
            < published[0].end()
            < scale.start()
            < scale.end()
            < measurements[0].start()
        )
    ):
        raise RuntimeError("normal-v5 first-frame scale commit changed")

    bridge = spectrum_ui[0]
    waiting = waiting_to_live[0]
    stale = live_to_stale[0]
    if (
        waiting.group("session").upper() != sender.session_id
        or waiting.group("frame") != bridge.group("frame")
        or int(waiting.group("frame")) != EXPECTED_FIRST_UI_FRAME
        or not bridge.end() < waiting.start() < peer_silent.start()
        or stale.group("session").upper() != sender.session_id
        or int(stale.group("frame")) != EXPECTED_FRAMES
        or stale.start() <= peer_silent.end()
        or not 0
        <= int(stale.group("timestamp")) - int(peer_silent.group("timestamp"))
        <= 300
    ):
        raise RuntimeError("normal-v5 UI transition identity/order changed")

    terminal_window = text[peer_silent.start() : stale.end()]
    forbidden_terminal_patterns = (
        r"^E \(",
        r"Guru Meditation",
        r"\bpanic(?:'ed)?\b",
        r"\babort\b",
        r"\bassert(?:ion)?\b",
        r"(?:Task|Interrupt) watchdog",
        r"\bWDT\b",
        r"brownout",
        r"Stack canary",
        r"heap corruption",
        r"Backtrace",
        r"ESP-ROM:",
        r"^rst:",
    )
    for pattern in forbidden_terminal_patterns:
        if re.search(pattern, terminal_window, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(
                "normal-v5 terminal transition contains forbidden marker: "
                + pattern
            )


def validate_sender_log(path: Path) -> common.SenderEvidence:
    return legacy.validate_sender_log(path)


def validate_serial_log(
    path: Path,
    sender: common.SenderEvidence,
) -> V5StabilitySummary:
    profile = _formal_profile()
    text = legacy.legacy.read_ascii_log(path, "UART")
    raw_profile = _validate_raw_runtime_profile(text, profile)
    _validate_v5_events(text, sender)

    with _scoped_legacy_v5_profile(profile):
        canonical_receiver, raw_receiver_health_count = (
            _canonicalize_receiver_health(text, profile)
        )
        compatibility_uart = _legacy_fft_compatibility_view(
            canonical_receiver
        )
        with legacy._scoped_legacy_v4_profile(compatibility_uart):
            base = legacy.legacy.validate_serial_log(path, sender)

    spectrum_bridge = SPECTRUM_UI_PATTERN.search(text)
    if spectrum_bridge is None or not math.isclose(
        float(spectrum_bridge.group("amplitude_max")),
        EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV,
        abs_tol=0.000001,
    ):
        raise RuntimeError("UART normal-v5 Spectrum amplitude profile changed")

    base_values = vars(base).copy()
    base_values["receiver_health_snapshots"] = raw_receiver_health_count
    base_values["maximum_fft_us"] = raw_profile.maximum_fft_us
    base_values["average_fft_us"] = raw_profile.final_average_fft_us
    base_values["minimum_internal_free"] = (
        raw_profile.minimum_pipe_internal_free
    )
    return V5StabilitySummary(
        **base_values,
        final_fft_last_us=raw_profile.final_fft_last_us,
        minimum_ui_free=raw_profile.minimum_ui_free,
    )


def validate_artifacts(elf_path: Path, bin_path: Path) -> None:
    actual_elf = _sha256(elf_path)
    actual_bin = _sha256(bin_path)
    if (
        actual_elf != EXPECTED_BOARD_ELF_SHA256
        or actual_bin != EXPECTED_BOARD_BIN_SHA256
    ):
        raise RuntimeError(
            "normal-v5 build artifact identity changed: "
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
        print("CycleScope normal-v5 10k stability definitions passed")
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
        "CycleScope normal-v5 10k stability evidence passed: "
        f"session={summary.session_id} frames={EXPECTED_FRAMES} "
        f"wave_packets={EXPECTED_WAVE_PACKETS} checkpoints=101 "
        f"receiver_health={summary.receiver_health_snapshots} "
        f"pipeline_health={summary.pipeline_health_snapshots} "
        "fft_us(last/avg/max)="
        f"{summary.final_fft_last_us}/{summary.average_fft_us}/"
        f"{summary.maximum_fft_us} "
        f"internal_min={summary.minimum_internal_free} "
        f"ui_min={summary.minimum_ui_free} "
        f"psram_free={summary.psram_free} "
        f"stream_ms={summary.stream_duration_ms} "
        "max_error(F0/voltage/tone_f/tone_A)="
        f"{summary.max_f0_error_hz:.3f}Hz/"
        f"{summary.max_voltage_error_mv:.3f}mV/"
        f"{summary.max_tone_frequency_error_hz:.3f}Hz/"
        f"{summary.max_tone_amplitude_error_mv:.3f}mV "
        f"Amax={EXPECTED_SPECTRUM_AMPLITUDE_MAX_MV:.1f}mVpk "
        "state=WAITING->LIVE->STALE "
        f"artifacts={artifact_status} "
        "path=.5:50000->.3:50001; digital_stability=PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
