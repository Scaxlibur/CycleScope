#!/usr/bin/env python3
"""Validate matched normal-v5 filtered-residual sender/UART evidence.

This read-only fixture proves a 100-frame PC-synthetic CSLP run in which a
nominally filtered 1 MHz interferer is represented by a 0.316 mVpk H50
residual.  It also freezes the normal-v5 first-frame spectrum-scale commit
and the WAITING -> LIVE -> STALE UI lifecycle after peer silence.  It does
not prove the physical 200 mVpp interferer, analog filter, BNC path, ADC,
FPGA, or panel flush.  The short capture contains no periodic health
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
    "/tmp/cyclescope-p4-v5-filtered-residual-emulator.log"
)
DEFAULT_SERIAL_LOG = Path(
    "/tmp/cyclescope-p4-v5-filtered-residual-serial.log"
)

CASE = common.FILTERED_RESIDUAL_CASE
EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA_PREFIX = "6d15ace8d"
EXPECTED_SENDER_IDENTITY = common.SenderEvidence(
    "325A36C4", 0x579F0D45, 0x2F362DEF
)
EXPECTED_EPOCH = 30
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

EXPECTED_SENDER_STATIC_LINES = (
    "synthetic multitone: F0=20000.000000Hz Vpp=105.249288mV "
    "RMS=29.581243mV scale=100uV/LSB offset=500uV calibration_id=1",
    "  H1: f=20000.000000Hz A=30.000000mVpk phase=0.170000rad",
    "  H3: f=60000.000000Hz A=25.000000mVpk phase=0.920000rad",
    "  H5: f=100000.000000Hz A=15.000000mVpk phase=-0.510000rad",
    "  H50: f=1000000.000000Hz A=0.316000mVpk phase=0.400000rad",
    "listening on 192.168.10.5:50000; expecting 192.168.10.3:50001",
    "sent frame=1 packets=12",
    "sent frame=25 packets=300",
    "sent frame=50 packets=600",
    "sent frame=75 packets=900",
    "sent frame=100 packets=1200",
    "completed frames=100 wave_packets=1200",
)
EXPECTED_SENDER_NONEMPTY_LINES = 18

SCRIPT_START_PATTERN = re.compile(
    r'^Script started on (?P<timestamp>[^\[\r\n]+) '
    r'\[COMMAND="(?P<command>[^"\r\n]+)" '
    r'(?P<terminal>TERM="dumb" TTY="[^"\r\n]+" '
    r'COLUMNS="80" LINES="24"|<not executed on terminal>)\]$',
    re.MULTILINE,
)
SCRIPT_DONE_PATTERN = re.compile(
    r'^Script done on (?P<timestamp>[^\[\r\n]+) '
    r'\[COMMAND_EXIT_CODE="(?P<exit_code>[0-9]+)"\]$',
    re.MULTILINE,
)
HELLO_PATTERN = re.compile(
    r"^HELLO session=0x(?P<session>[0-9A-Fa-f]{8}) "
    r"seq=(?P<seq>[0-9]+) port=50001 mtu=1472 "
    r"caps=0x0000001F status=(?P<status>[0-9]+)$",
    re.MULTILINE,
)
CONFIG_PATTERN = re.compile(
    r"^CONFIG_SET seq=(?P<seq>[0-9]+) status=(?P<status>[0-9]+) "
    r"config_id=0x(?P<config>[0-9A-Fa-f]{8}) "
    r"values=\(4062500, 8192, 50000, 1, 1, 1, 0\)$",
    re.MULTILINE,
)
ENABLE_PATTERN = re.compile(
    r"^ENABLE_PUSH seq=(?P<seq>[0-9]+) status=(?P<status>[0-9]+)$",
    re.MULTILINE,
)
SENDER_READY_PATTERN = re.compile(
    r"^session ready: session=0x(?P<session>[0-9A-Fa-f]{8}) "
    r"boot_id=0x(?P<boot>[0-9A-Fa-f]{8}) "
    r"config_id=0x(?P<config>[0-9A-Fa-f]{8})$",
    re.MULTILINE,
)
BOARD_READY_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: CSLP session ready: "
    r"session=0x(?P<session>[0-9A-Fa-f]{8}) "
    r"boot=(?P<boot>[0-9]+) config=(?P<config>[0-9]+)$",
    re.MULTILINE,
)
SCALE_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_pipe: "
    r"Spectrum scale committed: session=(?P<session>[0-9A-Fa-f]{8}) "
    r"config=(?P<config>[0-9A-Fa-f]{8}) epoch=(?P<epoch>[0-9]+) "
    r"frame=(?P<frame>[0-9]+) previous=(?P<previous>[0-9]+\.[0-9]+)mVpk "
    r"Amax=(?P<amax>[0-9]+\.[0-9]+)mVpk "
    r"reason=(?P<reason>[A-Z_]+)$",
    re.MULTILINE,
)
SPECTRUM_UI_PATTERN = re.compile(
    rf"^I \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    rf"Spectrum UI bridge on Core 0: "
    rf"session=(?P<session>[0-9A-Fa-f]{{8}}) "
    rf"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    rf"A/B=(?P<buffer_sentinel>[0-9]+) "
    rf"columns=(?P<columns>[0-9]+) peaks=(?P<peak_count>[0-9]+) "
    rf"Fs=(?P<sample_rate>{common.DECIMAL_PATTERN})MHz "
    rf"axis=(?P<axis>{common.DECIMAL_PATTERN})MHz "
    rf"Amax=(?P<amplitude_max>{common.DECIMAL_PATTERN})mVpk$",
    re.MULTILINE,
)
UI_STATE_PATTERN = re.compile(
    r"^(?P<level>[IW]) \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    r"CSLP UI stream state: (?P<before>[A-Z]+) -> (?P<after>[A-Z]+); "
    r"(?P<last>last )?session=(?P<session>[0-9A-Fa-f]{8}) "
    r"frame=(?P<frame>[0-9]+)"
    r"(?P<retained>; retaining waveform and measurements)?$",
    re.MULTILINE,
)
PEER_SILENT_PATTERN = re.compile(
    r"^W \((?P<timestamp>[0-9]+)\) cslp_rx: "
    r"CSLP peer silent for more than 1500 ms; starting a new session$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ResidualSummary:
    session_id: str
    boot_id: int
    config_id: int
    epoch: int
    max_f0_error_hz: float
    max_vpp_error_mv: float
    max_rms_error_mv: float
    max_tone_frequency_error_hz: float
    max_tone_amplitude_error_mv: float
    periodic_health_records: int


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


def _exactly_one(
    pattern: re.Pattern[str], text: str, label: str
) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {label}, got {len(matches)}")
    return matches[0]


def _exact_line(text: str, line: str, label: str) -> re.Match[str]:
    return _exactly_one(
        re.compile(r"^" + re.escape(line) + r"$", re.MULTILINE), text, label
    )


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
        EXPECTED_BOARD_APP_VERSION != "94dab8f-dirty"
        or EXPECTED_BOARD_ELF_SHA_PREFIX != "6d15ace8d"
        or EXPECTED_SENDER_IDENTITY
        != common.SenderEvidence("325A36C4", 0x579F0D45, 0x2F362DEF)
        or EXPECTED_EPOCH != 30
        or EXPECTED_SENDER_NONEMPTY_LINES != 18
        or len(EXPECTED_SENDER_STATIC_LINES) != 12
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
        raise RuntimeError("normal-v5 filtered-residual profile changed")


def validate_sender_log(path: Path) -> common.SenderEvidence:
    text = _read_ascii(path, "sender")
    start = _exactly_one(SCRIPT_START_PATTERN, text, "script start")
    done = _exactly_one(SCRIPT_DONE_PATTERN, text, "script completion")
    lines = text.splitlines()
    if (
        start.start() != 0
        or start.group("command") != EXPECTED_COMMAND
        or done.group("exit_code") != "0"
        or not start.end() < done.start()
        or not text.rstrip().endswith(done.group(0))
        or len([line for line in lines if line])
        != EXPECTED_SENDER_NONEMPTY_LINES
        or len(lines) < 2
        or lines[-2] != ""
        or any(not line for line in lines[:-2])
    ):
        raise RuntimeError("sender wrapper command/exit/shape changed")

    static = tuple(
        _exact_line(text, line, f"sender semantic line {index}")
        for index, line in enumerate(EXPECTED_SENDER_STATIC_LINES, start=1)
    )
    if any(right.start() <= left.start() for left, right in zip(static, static[1:])):
        raise RuntimeError("sender truth/frame/completion lines are out of order")

    sender = common.validate_sender_log(CASE, path)
    hello = _exactly_one(HELLO_PATTERN, text, "HELLO result")
    config = _exactly_one(CONFIG_PATTERN, text, "CONFIG_SET result")
    enable = _exactly_one(ENABLE_PATTERN, text, "ENABLE_PUSH result")
    ready = _exactly_one(SENDER_READY_PATTERN, text, "sender session-ready")
    local_identity = common.SenderEvidence(
        ready.group("session").upper(),
        int(ready.group("boot"), 16),
        int(ready.group("config"), 16),
    )
    hello_sequence = int(hello.group("seq"))
    if (
        sender != EXPECTED_SENDER_IDENTITY
        or local_identity != EXPECTED_SENDER_IDENTITY
        or hello.group("session").upper() != EXPECTED_SENDER_IDENTITY.session_id
        or config.group("config").upper()
        != f"{EXPECTED_SENDER_IDENTITY.config_id:08X}"
        or (hello.group("status"), config.group("status"), enable.group("status"))
        != ("0", "0", "0")
        or int(config.group("seq")) != (hello_sequence + 1) & 0xFFFFFFFF
        or int(enable.group("seq")) != (hello_sequence + 2) & 0xFFFFFFFF
        or not (
            start.end()
            < static[0].start()
            < static[5].start()
            < hello.start()
            < config.start()
            < enable.start()
            < ready.start()
            < static[6].start()
            < static[-1].start()
            < done.start()
        )
    ):
        raise RuntimeError("sender control/session identity or ordering changed")
    return sender


def _validate_image_identity(text: str) -> None:
    app_versions = re.findall(r"app_init: App version:\s+([^\n]+)", text)
    elf_hashes = re.findall(
        r"app_init: ELF file SHA256:\s+([0-9A-Fa-f]+)\.\.\.", text
    )
    if (
        text.count("rst:0x1 (POWERON)") != 1
        or text.count("app_init: Project name:     CycleScopeP4") != 1
        or app_versions != [EXPECTED_BOARD_APP_VERSION]
        or [value.lower() for value in elf_hashes]
        != [EXPECTED_BOARD_ELF_SHA_PREFIX]
        or text.count("CSLP v1 receiver ready on Core 1; golden packet PASS") != 1
        or "UDP 192.168.10.3:50001 -> 192.168.10.5:50000" not in text
        or re.search(r"FFT exact weak startup self-test:[^\n]*\bPASS\b", text)
        is None
        or "192.168.10.2" in text
        or "192.168.10.4" in text
    ):
        raise RuntimeError("UART log does not identify the expected normal-v5 image")


def _require_no_health_claim(text: str) -> int:
    health_patterns = (
        common.RX_HEALTH_PATTERN,
        common.FRAME_HEALTH_PATTERN,
        common.REJECT_HEALTH_PATTERN,
        common.SOCKET_HEALTH_PATTERN,
        common.PIPELINE_HEALTH_PATTERN,
    )
    counts = tuple(len(tuple(pattern.finditer(text))) for pattern in health_patterns)
    if counts != (0, 0, 0, 0, 0):
        raise RuntimeError(
            "short filtered-residual capture changed its no-periodic-health profile: "
            f"{counts}"
        )
    return sum(counts)


def _reject_forbidden_uart(text: str, formal_window: str) -> None:
    globally_forbidden = (
        r"^E \(",
        r"Guru Meditation",
        r"\bpanic(?:ked|'ed)?\b",
        r"(?:Task|Interrupt) watchdog",
        r"\bWDT\b",
        r"\bassert(?:ion)?\b",
        r"\babort(?:ed)?\b",
        r"Backtrace",
        r"Stack canary",
        r"brownout",
        r"heap corruption",
    )
    formal_forbidden = (
        *globally_forbidden,
        r"\bfatal\b",
        r"\berror(?:s)?\b",
        r"\breject(?:ed|ion|ions)?\b",
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
    for pattern in globally_forbidden:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(f"UART capture contains forbidden marker: {pattern}")
    for pattern in formal_forbidden:
        if re.search(pattern, formal_window, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(f"UART formal window contains forbidden marker: {pattern}")


def validate_serial_log(
    path: Path, sender: common.SenderEvidence
) -> ResidualSummary:
    text = _read_ascii(path, "UART")
    _validate_image_identity(text)
    health_count = _require_no_health_claim(text)

    timestamps = tuple(
        int(match.group(1))
        for match in re.finditer(r"^[IWE] \(([0-9]+)\)", text, re.MULTILINE)
    )
    if not timestamps or any(
        current < previous
        for previous, current in zip(timestamps, timestamps[1:])
    ):
        raise RuntimeError("UART timestamps are not monotonic")

    ready = _exactly_one(BOARD_READY_PATTERN, text, "board session-ready")
    board_identity = common.SenderEvidence(
        ready.group("session").upper(),
        int(ready.group("boot")),
        int(ready.group("config")),
    )
    if (
        text.count("CSLP session ready:") != 1
        or sender != EXPECTED_SENDER_IDENTITY
        or board_identity != EXPECTED_SENDER_IDENTITY
    ):
        raise RuntimeError(
            "UART session/boot/config does not match the frozen sender identity"
        )

    scales = tuple(SCALE_PATTERN.finditer(text))
    scale_values = tuple(
        (
            match.group("session").upper(),
            match.group("config").upper(),
            match.group("epoch"),
            match.group("frame"),
            match.group("previous"),
            match.group("amax"),
            match.group("reason"),
        )
        for match in scales
    )
    expected_scale = (
        EXPECTED_SENDER_IDENTITY.session_id,
        f"{EXPECTED_SENDER_IDENTITY.config_id:08X}",
        str(EXPECTED_EPOCH),
        "1",
        "0.0",
        "50.0",
        "NEW_STREAM",
    )
    if (
        text.count("Spectrum scale committed:") != 1
        or scale_values != (expected_scale,)
    ):
        raise RuntimeError(
            "UART must contain exactly one first-frame 0->50mVpk NEW_STREAM commit"
        )

    measurements = tuple(
        (match, common.parse_board_measurement(match))
        for match in common.BOARD_MEASUREMENT_PATTERN.finditer(text)
    )
    actual_frame_generation = tuple(
        (measurement.frame_id, measurement.generation)
        for _, measurement in measurements
    )
    if (
        text.count("cyclescope_pipe: measurement:") != 2
        or actual_frame_generation != ((1, 1), (100, 100))
    ):
        raise RuntimeError(
            "UART measurements must be exactly frame/gen 1/1 and 100/100: "
            f"{actual_frame_generation}"
        )

    maximum_errors = [0.0] * 5
    for index, (_, measurement) in enumerate(measurements):
        expected_frame = (1, 100)[index]
        if measurement.epoch != EXPECTED_EPOCH:
            raise RuntimeError("UART measurement epoch changed")
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
    if (
        text.count("cslp_rx: Published frame=") != 2
        or tuple(item[1:] for item in publications) != ((1, 1), (100, 100))
    ):
        raise RuntimeError("UART publication sequence is not exactly 1/1,100/100")

    ui = _exactly_one(SPECTRUM_UI_PATTERN, text, "Spectrum UI bridge")
    if (
        text.count("Spectrum UI bridge on Core 0:") != 1
        or ui.group("session").upper() != sender.session_id
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
        raise RuntimeError("Spectrum UI v5 filtered-residual profile changed")

    ui_states = tuple(UI_STATE_PATTERN.finditer(text))
    state_values = tuple(
        (
            match.group("level"),
            match.group("before"),
            match.group("after"),
            bool(match.group("last")),
            match.group("session").upper(),
            match.group("frame"),
            bool(match.group("retained")),
        )
        for match in ui_states
    )
    expected_states = (
        ("I", "WAITING", "LIVE", False, sender.session_id, "2", False),
        ("W", "LIVE", "STALE", True, sender.session_id, "100", True),
    )
    if text.count("CSLP UI stream state:") != 2 or state_values != expected_states:
        raise RuntimeError("UART WAITING->LIVE and LIVE->STALE contract changed")

    peer_silent = _exactly_one(PEER_SILENT_PATTERN, text, "peer-silent boundary")
    first_measurement_match, _ = measurements[0]
    final_measurement_match, _ = measurements[1]
    ordered_markers = (
        ready,
        publications[0][0],
        scales[0],
        first_measurement_match,
        ui,
        ui_states[0],
        publications[1][0],
        final_measurement_match,
        peer_silent,
        ui_states[1],
    )
    if any(
        right.start() <= left.start()
        for left, right in zip(ordered_markers, ordered_markers[1:])
    ):
        raise RuntimeError(
            "UART ready/frame/scale/UI/peer-silent/STALE ordering changed"
        )

    formal_window = text[ready.start() : ui_states[1].end()]
    _reject_forbidden_uart(text, formal_window)

    return ResidualSummary(
        sender.session_id,
        sender.boot_id,
        sender.config_id,
        EXPECTED_EPOCH,
        *maximum_errors,
        health_count,
    )


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
        "normal-v5 filtered-residual evidence PASS: "
        f"session={summary.session_id} boot=0x{summary.boot_id:08X} "
        f"config=0x{summary.config_id:08X} epoch={summary.epoch} "
        "frames/gen=1/1,100/100 lines=20/60/100kHz H50@1MHz=excluded "
        "scale=0->50mVpk@frame1/NEW_STREAM "
        "ui=WAITING->LIVE@frame2,peer-silent->LIVE->STALE@frame100 "
        "sender=100frames/1200packets/exit0 "
        "max_error(F0/Vpp/RMS/tone_f/tone_A)="
        f"{summary.max_f0_error_hz:.3f}Hz/"
        f"{summary.max_vpp_error_mv:.3f}mV/"
        f"{summary.max_rms_error_mv:.3f}mV/"
        f"{summary.max_tone_frequency_error_hz:.3f}Hz/"
        f"{summary.max_tone_amplitude_error_mv:.3f}mV; "
        f"periodic_health=NOT_CLAIMED(count={summary.periodic_health_records}); "
        "physical_200mVpp_1MHz_chain/panel_flush=UNPROVEN"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
