#!/usr/bin/env python3
"""Validate the matched normal-v4 filtered-residual sender/UART evidence.

This read-only fixture proves a 100-frame PC-synthetic CSLP run in which a
nominally filtered 1 MHz interferer is represented by a 0.316 mVpk H50
residual.  It does not prove the physical 200 mVpp interferer, analog filter,
BNC path, ADC, or FPGA.  The short capture contains no periodic health
snapshots, so a PASS deliberately makes no health-counter claim.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re

import cslp_g_v3_sender_evidence as common


DEFAULT_SENDER_LOG = Path(
    "/tmp/cyclescope-p4-v4-filtered-residual-emulator.log"
)
DEFAULT_SERIAL_LOG = Path(
    "/tmp/cyclescope-p4-v4-filtered-residual-serial.log"
)

CASE = common.FILTERED_RESIDUAL_CASE
EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA_PREFIX = "0c36dc583"
EXPECTED_COMMON_SHA256 = (
    "abb42513ca05c290980ac9805a364410e5b625abf9eb8452f2ecd79aad919896"
)
EXPECTED_MATRIX_SHA256 = (
    "e2a06ffc7b42d7a8c722df7864cb76e77b67054b36797761c107bc49f2dac566"
)
EXPECTED_EMULATOR_SHA256 = (
    "0e07aa86a63ea9f0f7f9249c6a01759a5e4c27775f84cca4b5efbc2386a0032c"
)

EXPECTED_COMMAND = " ".join(
    (
        "python3",
        "ESP32-P4/tools/cslp_fpga_emulator.py",
        "--bind-ip 192.168.10.5",
        "--port 50000",
        "--peer-ip 192.168.10.3",
        "--peer-port 50001",
        "--scenario normal",
        "--waveform multitone",
        "--frames 100",
        "--chunk-gap-us 250",
        "--hold-seconds 2",
        "--handshake-timeout 15",
        "--scale-uv-per-lsb 100",
        "--offset-uv 500",
        "--calibration-id 1",
        "--fundamental-hz 20000.0",
        "--tone 1:0.03:0.17",
        "--tone 3:0.025:0.92",
        "--tone 5:0.015:-0.51",
        "--tone 50:0.000316:0.4",
    )
)

SCRIPT_START_PATTERN = re.compile(
    r'^Script started on (?P<timestamp>[^\[]+) '
    r'\[COMMAND="(?P<command>[^"]+)" TERM="dumb" TTY="[^"]+" '
    r'COLUMNS="80" LINES="24"\]$',
    re.MULTILINE,
)
SCRIPT_DONE_PATTERN = re.compile(
    r'^Script done on (?P<timestamp>[^\[]+) '
    r'\[COMMAND_EXIT_CODE="(?P<exit_code>[0-9]+)"\]$',
    re.MULTILINE,
)
HELLO_PATTERN = re.compile(
    r"^HELLO session=0x(?P<session>[0-9A-Fa-f]{8}) seq=[0-9]+ "
    r"port=50001 mtu=1472 caps=0x0000001F status=(?P<status>[0-9]+)$",
    re.MULTILINE,
)
CONFIG_PATTERN = re.compile(
    r"^CONFIG_SET seq=[0-9]+ status=(?P<status>[0-9]+) "
    r"config_id=0x(?P<config>[0-9A-Fa-f]{8}) "
    r"values=\(4062500, 8192, 50000, 1, 1, 1, 0\)$",
    re.MULTILINE,
)
ENABLE_PATTERN = re.compile(
    r"^ENABLE_PUSH seq=[0-9]+ status=(?P<status>[0-9]+)$",
    re.MULTILINE,
)
SENT_FRAME_PATTERN = re.compile(
    r"^sent frame=(?P<frame>[0-9]+) packets=(?P<packets>[0-9]+)$",
    re.MULTILINE,
)
COMPLETION_PATTERN = re.compile(
    r"^completed frames=(?P<frames>[0-9]+) "
    r"wave_packets=(?P<packets>[0-9]+)$",
    re.MULTILINE,
)
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
PEER_SILENT_PATTERN = re.compile(
    r"^W \([0-9]+\) cslp_rx: CSLP peer silent for more than 1500 ms; "
    r"starting a new session$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ResidualSummary:
    session_id: str
    boot_id: int
    config_id: int
    max_f0_error_hz: float
    max_vpp_error_mv: float
    max_rms_error_mv: float
    max_tone_frequency_error_hz: float
    max_tone_amplitude_error_mv: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_ascii(path: Path, label: str) -> str:
    try:
        text = path.read_bytes().decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{label} log is not strict ASCII") from error
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text or common.ANSI_ESCAPE_PATTERN.search(text):
        raise RuntimeError(f"{label} log contains unexpected control bytes")
    return text


def _exactly_one(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {label}, got {len(matches)}")
    return matches[0]


def validate_definitions() -> None:
    common.validate_definitions()
    dependency_hashes = (
        (_sha256(Path(common.__file__)), EXPECTED_COMMON_SHA256),
        (_sha256(Path(common.boundary.__file__)), EXPECTED_MATRIX_SHA256),
        (
            _sha256(Path(common.boundary.emulator.__file__)),
            EXPECTED_EMULATOR_SHA256,
        ),
    )
    if any(actual != expected for actual, expected in dependency_hashes):
        raise RuntimeError("frozen filtered-residual helper identity changed")
    if (
        EXPECTED_BOARD_ELF_SHA_PREFIX != "0c36dc583"
        or CASE.fundamental_hz != 20_000.0
        or CASE.transmitted_tones
        != (
            (1, 0.030, 0.17),
            (3, 0.025, 0.92),
            (5, 0.015, -0.51),
            (50, 0.000316, 0.4),
        )
        or CASE.expected_p4_tones != CASE.transmitted_tones[:3]
    ):
        raise RuntimeError("normal-v4 filtered-residual profile changed")


def validate_sender_log(path: Path) -> common.SenderEvidence:
    text = _read_ascii(path, "sender")
    start = _exactly_one(SCRIPT_START_PATTERN, text, "script start")
    done = _exactly_one(SCRIPT_DONE_PATTERN, text, "script completion")
    if (
        start.group("command") != EXPECTED_COMMAND
        or done.group("exit_code") != "0"
        or start.start() != 0
        or not text.rstrip().endswith(done.group(0))
        or not start.end() < done.start()
    ):
        raise RuntimeError("sender wrapper command/exit identity changed")

    sender = common.validate_sender_log(CASE, path)
    hello = _exactly_one(HELLO_PATTERN, text, "HELLO result")
    config = _exactly_one(CONFIG_PATTERN, text, "CONFIG_SET result")
    enable = _exactly_one(ENABLE_PATTERN, text, "ENABLE_PUSH result")
    ready = _exactly_one(common.SENDER_SESSION_PATTERN, text, "sender session-ready")
    if (
        hello.group("status") != "0"
        or config.group("status") != "0"
        or enable.group("status") != "0"
        or hello.group("session").upper() != sender.session_id
        or config.group("config").upper() != f"{sender.config_id:08X}"
        or ready.group(1).upper() != sender.session_id
        or int(ready.group(2), 16) != sender.boot_id
        or int(ready.group(3), 16) != sender.config_id
        or not hello.start() < config.start() < enable.start() < ready.start()
    ):
        raise RuntimeError("sender control/session identity or ordering changed")

    checkpoints = tuple(
        (int(match.group("frame")), int(match.group("packets")))
        for match in SENT_FRAME_PATTERN.finditer(text)
    )
    completion = _exactly_one(COMPLETION_PATTERN, text, "semantic completion")
    if (
        checkpoints != ((1, 12), (25, 300), (50, 600), (75, 900), (100, 1200))
        or (int(completion.group("frames")), int(completion.group("packets")))
        != (100, 1200)
        or not ready.end() < completion.start() < done.start()
    ):
        raise RuntimeError("sender 100-frame/1200-packet evidence is incomplete")
    return sender


def _validate_image_identity(text: str) -> None:
    app_versions = re.findall(r"app_init: App version:\s+([^\n]+)", text)
    elf_hashes = re.findall(
        r"app_init: ELF file SHA256:\s+([0-9A-Fa-f]+)\.\.\.", text
    )
    if (
        text.count("app_init: Project name:     CycleScopeP4") != 1
        or app_versions != [EXPECTED_BOARD_APP_VERSION]
        or [value.lower() for value in elf_hashes]
        != [EXPECTED_BOARD_ELF_SHA_PREFIX]
        or text.count("rst:0x1 (POWERON)") != 1
        or text.count("CSLP v1 receiver ready on Core 1; golden packet PASS") != 1
        or "UDP 192.168.10.3:50001 -> 192.168.10.5:50000" not in text
        or re.search(r"FFT exact weak startup self-test:[^\n]*\bPASS\b", text)
        is None
        or "192.168.10.2" in text
        or "192.168.10.4" in text
    ):
        raise RuntimeError("UART log does not identify the expected normal-v4 image")


def _require_no_health_claim(text: str) -> None:
    health_patterns = (
        common.RX_HEALTH_PATTERN,
        common.FRAME_HEALTH_PATTERN,
        common.REJECT_HEALTH_PATTERN,
        common.SOCKET_HEALTH_PATTERN,
        common.PIPELINE_HEALTH_PATTERN,
    )
    counts = tuple(len(list(pattern.finditer(text))) for pattern in health_patterns)
    if counts != (0, 0, 0, 0, 0):
        raise RuntimeError(
            "short filtered-residual capture changed its no-periodic-health profile: "
            f"{counts}"
        )


def validate_serial_log(
    path: Path, sender: common.SenderEvidence
) -> ResidualSummary:
    text = _read_ascii(path, "UART")
    _validate_image_identity(text)
    _require_no_health_claim(text)

    timestamps = tuple(
        int(match.group(1))
        for match in re.finditer(r"^[IWE] \(([0-9]+)\)", text, re.MULTILINE)
    )
    if not timestamps or any(
        current < previous
        for previous, current in zip(timestamps, timestamps[1:])
    ):
        raise RuntimeError("UART timestamps are not monotonic")

    ready = _exactly_one(common.BOARD_SESSION_PATTERN, text, "board session-ready")
    board_identity = common.SenderEvidence(
        ready.group(1).upper(), int(ready.group(2)), int(ready.group(3))
    )
    if board_identity != sender:
        raise RuntimeError(
            f"UART session/boot/config does not match sender: {board_identity}"
        )

    measurements = tuple(
        (match, common.parse_board_measurement(match))
        for match in common.BOARD_MEASUREMENT_PATTERN.finditer(text)
    )
    actual_frame_generation = tuple(
        (measurement.frame_id, measurement.generation)
        for _, measurement in measurements
    )
    if actual_frame_generation != ((1, 1), (100, 100)):
        raise RuntimeError(
            "UART measurements must be exactly frame/gen 1/1 and 100/100: "
            f"{actual_frame_generation}"
        )

    maximum_errors = [0.0] * 5
    for index, (_, measurement) in enumerate(measurements):
        expected_frame = (1, 100)[index]
        errors = common.validate_board_measurement(
            CASE, sender, measurement, expected_frame, expected_frame
        )
        maximum_errors = [
            max(previous, current)
            for previous, current in zip(maximum_errors, errors, strict=True)
        ]
    first = measurements[0][1]
    last = measurements[1][1]
    if (
        first.config_id,
        first.epoch,
        first.fundamental_hz,
        first.vpp_mv,
        first.rms_mv,
        first.peak_count,
        first.tones,
        first.calibration_id,
        first.test_pattern,
    ) != (
        last.config_id,
        last.epoch,
        last.fundamental_hz,
        last.vpp_mv,
        last.rms_mv,
        last.peak_count,
        last.tones,
        last.calibration_id,
        last.test_pattern,
    ):
        raise RuntimeError("frame 1/100 filtered-residual measurements drifted")
    if any(frequency_hz > 500_000.0 for frequency_hz, _ in first.tones):
        raise RuntimeError("1 MHz H50 leaked into the reported P4 line spectrum")

    publications = tuple(
        (match, int(match.group(1)), int(match.group(2)))
        for match in common.PUBLISHED_FRAME_PATTERN.finditer(text)
    )
    if tuple(item[1:] for item in publications) != ((1, 1), (100, 100)):
        raise RuntimeError("UART publication sequence is not exactly 1/1,100/100")

    ui = _exactly_one(SPECTRUM_UI_PATTERN, text, "Spectrum UI bridge")
    if (
        ui.group("session").upper() != sender.session_id
        or int(ui.group("frame")) != 2
        or int(ui.group("generation")) != 2
        or int(ui.group("buffer_sentinel")) != 255
        or int(ui.group("columns")) != common.EXPECTED_SPECTRUM_COLUMNS
        or int(ui.group("peak_count")) != 3
        or not math.isclose(
            float(ui.group("sample_rate")),
            common.EXPECTED_SAMPLE_RATE_MHZ,
            abs_tol=0.000001,
        )
        or not math.isclose(
            float(ui.group("axis")),
            common.EXPECTED_SPECTRUM_AXIS_MHZ,
            abs_tol=0.000001,
        )
        or not math.isclose(
            float(ui.group("amplitude_max")), 50.0, abs_tol=0.000001
        )
    ):
        raise RuntimeError("Spectrum UI v4 filtered-residual profile changed")

    peer_silent = _exactly_one(PEER_SILENT_PATTERN, text, "peer-silent boundary")
    first_measurement_match, _ = measurements[0]
    final_measurement_match, _ = measurements[1]
    if not (
        ready.end()
        < publications[0][0].start()
        < first_measurement_match.start()
        < ui.start()
        < publications[1][0].start()
        < final_measurement_match.start()
        < peer_silent.start()
    ):
        raise RuntimeError("UART ready/frame/UI/terminal ordering changed")

    formal_window = text[ready.start() : peer_silent.end()]
    forbidden = (
        r"^E \(",
        r"\bfatal\b",
        r"\berror(?:s)?\b",
        r"\breject(?:ed|ion|ions)?\b",
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
        r"Measurement rejected",
        r"FFT failed",
        r"Discarded stale",
        r"Unable to publish",
        r"Control transaction[^\n]*timed out",
        r"handshake failed",
        r"selftest=FAIL",
        r"ESP-ROM:",
        r"^rst:",
    )
    for pattern in forbidden:
        if re.search(pattern, formal_window, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(f"UART formal window contains forbidden marker: {pattern}")
    if re.search(r"^E \(", text, re.MULTILINE) or re.search(
        r"\b(?:fatal|errors?|reject(?:ed|ion|ions)?)\b", text, re.IGNORECASE
    ):
        raise RuntimeError("UART capture contains fatal/error/reject evidence")

    return ResidualSummary(sender.session_id, sender.boot_id, sender.config_id, *maximum_errors)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender-log", type=Path, default=DEFAULT_SENDER_LOG)
    parser.add_argument("--serial-log", type=Path, default=DEFAULT_SERIAL_LOG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_definitions()
    sender = validate_sender_log(args.sender_log)
    summary = validate_serial_log(args.serial_log, sender)
    print(
        "normal-v4 filtered-residual evidence PASS: "
        f"session={summary.session_id} boot=0x{summary.boot_id:08X} "
        f"config=0x{summary.config_id:08X} frames/gen=1/1,100/100 "
        "lines=20/60/100kHz H50@1MHz=excluded Amax=50.0mVpk "
        "sender=100frames/1200packets/exit0 "
        "max_error(F0/Vpp/RMS/tone_f/tone_A)="
        f"{summary.max_f0_error_hz:.3f}Hz/"
        f"{summary.max_vpp_error_mv:.3f}mV/"
        f"{summary.max_rms_error_mv:.3f}mV/"
        f"{summary.max_tone_frequency_error_hz:.3f}Hz/"
        f"{summary.max_tone_amplitude_error_mv:.3f}mV; "
        "periodic_health=NOT_CLAIMED(count=0); "
        "physical_200mVpp_1MHz_chain=UNPROVEN"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
