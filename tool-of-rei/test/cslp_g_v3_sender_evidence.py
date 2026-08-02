#!/usr/bin/env python3
"""Validate the current-v3 G matrix, PC sender logs, and optional UART evidence.

The fixture combines the four in-band boundary vectors with one downstream
filtered-residual vector.  It never opens a socket, starts a subprocess, or
writes a result file.  Successful sender logs remain UNPROVEN unless a matched
UART capture supplies the P4 measurements, health counters, and UI-axis proof.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import re
import struct
import zlib

import cslp_g_acceptance_matrix as boundary


@dataclass(frozen=True)
class UnifiedCase:
    case_id: str
    requirement: str
    fundamental_hz: float
    transmitted_tones: tuple[tuple[int, float, float], ...]
    expected_p4_tones: tuple[tuple[int, float, float], ...]
    log_name: str
    note: str


@dataclass(frozen=True)
class SenderEvidence:
    session_id: str
    boot_id: int
    config_id: int


@dataclass(frozen=True)
class BoardMeasurement:
    session_id: str
    config_id: int
    epoch: int
    frame_id: int
    generation: int
    fundamental_hz: float
    vpp_mv: float
    rms_mv: float
    peak_count: int
    tones: tuple[tuple[float, float], ...]
    calibration_id: int
    test_pattern: int


@dataclass(frozen=True)
class BoardEvidenceSummary:
    max_f0_error_hz: float
    max_vpp_error_mv: float
    max_rms_error_mv: float
    max_tone_frequency_error_hz: float
    max_tone_amplitude_error_mv: float


BOUNDARY_CASES = tuple(
    UnifiedCase(
        case.case_id,
        case.requirement,
        case.fundamental_hz,
        case.tones,
        case.tones,
        f"cyclescope-p4-v3-final-g-case{index}-v2-emulator.log",
        case.note,
    )
    for index, case in enumerate(boundary.CASES, start=1)
)

FILTERED_RESIDUAL_CASE = UnifiedCase(
    "ub_filtered_1mhz_residual",
    "u_b + filtered u_J",
    20_000.0,
    (
        (1, 0.030, 0.17),
        (3, 0.025, 0.92),
        (5, 0.015, -0.51),
        (50, 0.000316, 0.4),
    ),
    (
        (1, 0.030, 0.17),
        (3, 0.025, 0.92),
        (5, 0.015, -0.51),
    ),
    "cyclescope-p4-v3-final-g-filtered-residual-v2-emulator.log",
    "200 mVpp interferer after a nominal 50 dB stop-band leaves 0.316 mVpk",
)

CASES = (*BOUNDARY_CASES, FILTERED_RESIDUAL_CASE)
DEFAULT_LOG_DIRECTORY = Path("/tmp")
FILTERED_RESIDUAL_SAMPLE_BYTES = 16_384
FILTERED_RESIDUAL_SAMPLE_CRC32 = 0xA818AF89
FILTERED_RESIDUAL_CODE_RANGE = (-529, 523)
EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA_PREFIX = "22f02f11b"
FREQUENCY_TOLERANCE_HZ = 1_000.0
VOLTAGE_TOLERANCE_MV = 5.0
EXPECTED_SPECTRUM_AXIS_MHZ = 0.5
EXPECTED_SPECTRUM_COLUMNS = 640
EXPECTED_SAMPLE_RATE_MHZ = 4.0625
DECIMAL_PATTERN = r"[0-9]+(?:\.[0-9]+)?"

SYNTHETIC_PATTERN = re.compile(
    r"synthetic multitone: F0=([0-9.]+)Hz Vpp=([0-9.]+)mV "
    r"RMS=([0-9.]+)mV scale=([0-9]+)uV/LSB offset=(-?[0-9]+)uV "
    r"calibration_id=([0-9]+)"
)
TONE_PATTERN = re.compile(
    r"^\s*H([0-9]+): f=([0-9.]+)Hz A=([0-9.]+)mVpk "
    r"phase=(-?[0-9.]+)rad$",
    re.MULTILINE,
)
SENDER_SESSION_PATTERN = re.compile(
    r"session ready: session=0x([0-9A-Fa-f]{8}) "
    r"boot_id=0x([0-9A-Fa-f]{8}) config_id=0x([0-9A-Fa-f]{8})"
)
BOARD_SESSION_PATTERN = re.compile(
    r"CSLP session ready: session=0x([0-9A-Fa-f]{8}) "
    r"boot=([0-9]+) config=([0-9]+)"
)
BOARD_MEASUREMENT_PATTERN = re.compile(
    rf"measurement: session=(?P<session>[0-9A-Fa-f]{{8}}) "
    rf"config=(?P<config>[0-9A-Fa-f]{{8}}) epoch=(?P<epoch>[0-9]+) "
    rf"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    rf"F0=(?P<f0>{DECIMAL_PATTERN})Hz "
    rf"Vpp=(?P<vpp>{DECIMAL_PATTERN})mV "
    rf"RMS=(?P<rms>{DECIMAL_PATTERN})mV peaks=(?P<peak_count>[0-9]+) "
    rf"P1=(?P<p1_f>{DECIMAL_PATTERN})Hz/(?P<p1_a>{DECIMAL_PATTERN})mVpk "
    rf"P2=(?P<p2_f>{DECIMAL_PATTERN})Hz/(?P<p2_a>{DECIMAL_PATTERN})mVpk "
    rf"P3=(?P<p3_f>{DECIMAL_PATTERN})Hz/(?P<p3_a>{DECIMAL_PATTERN})mVpk "
    r"cal=(?P<calibration>[0-9]+) test=(?P<test>[0-9]+)"
)
PUBLISHED_FRAME_PATTERN = re.compile(
    r"Published frame=([0-9]+) completed=([0-9]+)"
)
RX_HEALTH_PATTERN = re.compile(
    r"health/rx: packets=(?P<packets>[0-9]+) "
    r"source=(?P<source>[0-9]+) magic=(?P<magic>[0-9]+) "
    r"version=(?P<version>[0-9]+) length=(?P<length>[0-9]+) "
    r"session=(?P<session>[0-9]+) crc=(?P<crc>[0-9]+)"
)
FRAME_HEALTH_PATTERN = re.compile(
    r"health/frame: completed=(?P<completed>[0-9]+) "
    r"acquired=(?P<acquired>[0-9]+) overwrite=(?P<overwrite>[0-9]+) "
    r"incomplete=(?P<incomplete>[0-9]+) "
    r"duplicate=(?P<duplicate>[0-9]+) stale=(?P<stale>[0-9]+) "
    r"busy=(?P<busy>[0-9]+)"
)
REJECT_HEALTH_PATTERN = re.compile(
    r"health/reject: config=(?P<config>[0-9]+) "
    r"metadata=(?P<metadata>[0-9]+) overrange=(?P<overrange>[0-9]+) "
    r"fifo=(?P<fifo>[0-9]+) retries=(?P<retries>[0-9]+) "
    r"reconnects=(?P<reconnects>[0-9]+)"
)
SOCKET_HEALTH_PATTERN = re.compile(
    r"health/socket: open_fail=(?P<open_fail>[0-9]+) "
    r"recv_fatal=(?P<recv_fatal>[0-9]+) "
    r"close_fail=(?P<close_fail>[0-9]+) sessions=(?P<sessions>[0-9]+)"
)
PIPELINE_HEALTH_PATTERN = re.compile(
    r"(?P<tag>cyclescope_fft|cyclescope_pipe): health: "
    r"acquired=(?P<acquired>[0-9]+) analyzed=(?P<analyzed>[0-9]+) "
    r"published=(?P<published>[0-9]+)(?: ui=(?P<ui>[0-9]+))? "
    r"stale=(?P<stale>[0-9]+) invalid=(?P<invalid>[0-9]+) "
    r"(?:failures|fft_fail)=(?P<fft_fail>[0-9]+)[^\r\n]*"
)
SPECTRUM_UI_PATTERN = re.compile(
    rf"Spectrum UI bridge on Core 0: session=(?P<session>[0-9A-Fa-f]{{8}}) "
    rf"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    rf"A/B=(?P<buffer_sentinel>[0-9]+) "
    rf"columns=(?P<columns>[0-9]+) peaks=(?P<peak_count>[0-9]+) "
    rf"Fs=(?P<sample_rate>{DECIMAL_PATTERN})MHz "
    rf"axis=(?P<axis>{DECIMAL_PATTERN})MHz"
)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def expected_metrics(
    tones: tuple[tuple[int, float, float], ...],
) -> tuple[float, float]:
    vpp_volts, rms_volts = boundary.emulator.expected_multitone_metrics(tones)
    return vpp_volts * 1000.0, rms_volts * 1000.0


def as_matrix_case(case: UnifiedCase) -> boundary.MatrixCase:
    target_vpp_mv, _ = expected_metrics(case.transmitted_tones)
    return boundary.MatrixCase(
        case.case_id,
        case.requirement,
        target_vpp_mv,
        case.fundamental_hz,
        max(
            harmonic * case.fundamental_hz
            for harmonic, _, _ in case.transmitted_tones
        ),
        case.transmitted_tones,
        case.note,
    )


def validate_definitions() -> None:
    boundary.validate_matrix()
    if len(CASES) != 5 or len({case.case_id for case in CASES}) != len(CASES):
        raise RuntimeError("unified matrix must contain five unique cases")
    for source, unified in zip(boundary.CASES, BOUNDARY_CASES, strict=True):
        if (
            unified.case_id != source.case_id
            or unified.requirement != source.requirement
            or unified.fundamental_hz != source.fundamental_hz
            or unified.transmitted_tones != source.tones
            or unified.expected_p4_tones != source.tones
        ):
            raise RuntimeError("unified boundary definition diverged from its source")

    for case in CASES:
        boundary.emulator.synthesize_multitone(
            case.fundamental_hz,
            case.transmitted_tones,
            boundary.SCALE_UV_PER_LSB,
            boundary.OFFSET_UV,
        )
        if not case.expected_p4_tones:
            raise RuntimeError(f"{case.case_id}: missing P4 target tones")
        for tone in case.transmitted_tones:
            if boundary.emulator.parse_tone(boundary.tone_argument(tone)) != tone:
                raise RuntimeError(f"{case.case_id}: CLI tone round-trip failed")

    residual = FILTERED_RESIDUAL_CASE
    residual_tone = residual.transmitted_tones[-1]
    harmonic, amplitude_volts_peak, _ = residual_tone
    residual_frequency_hz = harmonic * residual.fundamental_hz
    attenuation_db = -20.0 * math.log10(amplitude_volts_peak / 0.100)
    if (
        residual_frequency_hz < 1_000_000.0
        or not math.isclose(amplitude_volts_peak, 0.000316, abs_tol=1.0e-12)
        or not math.isclose(attenuation_db, 50.006011, abs_tol=0.001)
    ):
        raise RuntimeError("filtered residual no longer represents the 1 MHz/50 dB case")
    if residual.transmitted_tones[:-1] != residual.expected_p4_tones:
        raise RuntimeError("P4 residual target must exclude only the out-of-band tone")
    if any(
        harmonic * residual.fundamental_hz > 500_000.0
        for harmonic, _, _ in residual.expected_p4_tones
    ):
        raise RuntimeError("P4 residual target contains an out-of-band tone")

    p4_vpp_mv, _ = expected_metrics(residual.expected_p4_tones)
    if not 50.0 <= p4_vpp_mv <= 250.0:
        raise RuntimeError("filtered residual's in-band u_b leaves the G-problem range")
    residual_samples = boundary.emulator.synthesize_multitone(
        residual.fundamental_hz,
        residual.transmitted_tones,
        boundary.SCALE_UV_PER_LSB,
        boundary.OFFSET_UV,
    )
    residual_payload = struct.pack(f"<{len(residual_samples)}h", *residual_samples)
    if (
        len(residual_payload) != FILTERED_RESIDUAL_SAMPLE_BYTES
        or (min(residual_samples), max(residual_samples))
        != FILTERED_RESIDUAL_CODE_RANGE
        or zlib.crc32(residual_payload) & 0xFFFFFFFF
        != FILTERED_RESIDUAL_SAMPLE_CRC32
    ):
        raise RuntimeError("filtered residual S16_LE vector changed")

    for case in CASES:
        command = boundary.replay_command(as_matrix_case(case))
        if (
            "--bind-ip 192.168.10.5" not in command
            or "--peer-ip 192.168.10.3" not in command
            or "--chunk-gap-us 250" not in command
            or "192.168.10.2" in command
            or "192.168.10.4" in command
        ):
            raise RuntimeError(f"{case.case_id}: replay command violates IP/timing isolation")


def validate_sender_log(case: UnifiedCase, path: Path) -> SenderEvidence:
    try:
        text = path.read_bytes().decode("ascii").replace("\r", "")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{case.case_id}: sender log is not ASCII") from error
    if any(
        marker in text
        for marker in (
            "Traceback",
            "timed out waiting",
            "COMMAND_EXIT_CODE=\"1\"",
            "status=3",
            "status=5",
            "status=8",
        )
    ):
        raise RuntimeError(f"{case.case_id}: sender log contains a failure marker")

    synthetic = SYNTHETIC_PATTERN.search(text)
    if synthetic is None:
        raise RuntimeError(f"{case.case_id}: synthetic truth header is missing")
    expected_vpp_mv, expected_rms_mv = expected_metrics(case.transmitted_tones)
    actual_f0_hz = float(synthetic.group(1))
    actual_vpp_mv = float(synthetic.group(2))
    actual_rms_mv = float(synthetic.group(3))
    if (
        not math.isclose(actual_f0_hz, case.fundamental_hz, abs_tol=0.000001)
        or not math.isclose(actual_vpp_mv, expected_vpp_mv, abs_tol=0.000001)
        or not math.isclose(actual_rms_mv, expected_rms_mv, abs_tol=0.000001)
        or int(synthetic.group(4)) != boundary.SCALE_UV_PER_LSB
        or int(synthetic.group(5)) != boundary.OFFSET_UV
        or int(synthetic.group(6)) != boundary.CALIBRATION_ID
    ):
        raise RuntimeError(f"{case.case_id}: synthetic truth header changed")

    parsed_tones = tuple(
        (
            int(match.group(1)),
            float(match.group(3)) / 1000.0,
            float(match.group(4)),
            float(match.group(2)),
        )
        for match in TONE_PATTERN.finditer(text)
    )
    if len(parsed_tones) != len(case.transmitted_tones):
        raise RuntimeError(f"{case.case_id}: sender tone count changed")
    for expected, actual in zip(case.transmitted_tones, parsed_tones, strict=True):
        harmonic, amplitude, phase = expected
        actual_harmonic, actual_amplitude, actual_phase, actual_frequency = actual
        if (
            actual_harmonic != harmonic
            or not math.isclose(actual_amplitude, amplitude, abs_tol=0.0000005)
            or not math.isclose(actual_phase, phase, abs_tol=0.0000005)
            or not math.isclose(
                actual_frequency,
                harmonic * case.fundamental_hz,
                abs_tol=0.000001,
            )
        ):
            raise RuntimeError(f"{case.case_id}: sender tone truth changed")

    required_fragments = (
        "--frames 100",
        "--chunk-gap-us 250",
        "--scale-uv-per-lsb 100",
        "--offset-uv 500",
        "--calibration-id 1",
        "listening on 192.168.10.5:50000; expecting 192.168.10.3:50001",
        "values=(4062500, 8192, 50000, 1, 1, 1, 0)",
        "sent frame=1 packets=12",
        "sent frame=25 packets=300",
        "sent frame=50 packets=600",
        "sent frame=75 packets=900",
        "sent frame=100 packets=1200",
        "completed frames=100 wave_packets=1200",
        "COMMAND_EXIT_CODE=\"0\"",
    )
    missing = tuple(fragment for fragment in required_fragments if fragment not in text)
    if missing:
        raise RuntimeError(f"{case.case_id}: sender log is incomplete: {missing}")
    if "192.168.10.2" in text or "192.168.10.4" in text:
        raise RuntimeError(f"{case.case_id}: sender log touched an excluded FPGA address")
    for transaction in ("HELLO", "CONFIG_SET", "ENABLE_PUSH"):
        if re.search(rf"^{transaction} .*status=0(?: |$)", text, re.MULTILINE) is None:
            raise RuntimeError(f"{case.case_id}: {transaction} did not complete cleanly")

    sessions = SENDER_SESSION_PATTERN.findall(text)
    if len(sessions) != 1:
        raise RuntimeError(f"{case.case_id}: expected exactly one ready session")
    session_id, boot_id, config_id = sessions[0]
    return SenderEvidence(
        session_id.upper(),
        int(boot_id, 16),
        int(config_id, 16),
    )


def validate_sender_logs(logs: dict[str, Path]) -> tuple[SenderEvidence, ...]:
    expected_ids = {case.case_id for case in CASES}
    if set(logs) != expected_ids:
        missing = sorted(expected_ids - set(logs))
        extra = sorted(set(logs) - expected_ids)
        raise RuntimeError(f"sender log set mismatch: missing={missing} extra={extra}")

    evidence = tuple(validate_sender_log(case, logs[case.case_id]) for case in CASES)
    if len({item.session_id for item in evidence}) != len(evidence):
        raise RuntimeError("unified sender matrix reused a ready session")
    if len({item.config_id for item in evidence}) != len(evidence):
        raise RuntimeError("unified sender matrix reused a config ID")
    return evidence


def parse_board_measurement(match: re.Match[str]) -> BoardMeasurement:
    tones = tuple(
        (
            float(match.group(f"p{index}_f")),
            float(match.group(f"p{index}_a")),
        )
        for index in range(1, 4)
    )
    return BoardMeasurement(
        session_id=match.group("session").upper(),
        config_id=int(match.group("config"), 16),
        epoch=int(match.group("epoch")),
        frame_id=int(match.group("frame")),
        generation=int(match.group("generation")),
        fundamental_hz=float(match.group("f0")),
        vpp_mv=float(match.group("vpp")),
        rms_mv=float(match.group("rms")),
        peak_count=int(match.group("peak_count")),
        tones=tones,
        calibration_id=int(match.group("calibration")),
        test_pattern=int(match.group("test")),
    )


def require_zero_health(
    matches: list[re.Match[str]],
    zero_fields: tuple[str, ...],
    label: str,
) -> None:
    if not matches:
        raise RuntimeError(f"UART matrix is missing {label} health evidence")
    for index, match in enumerate(matches, start=1):
        nonzero = {
            field: int(match.group(field))
            for field in zero_fields
            if int(match.group(field)) != 0
        }
        if nonzero:
            raise RuntimeError(
                f"UART {label} health #{index} has nonzero errors: {nonzero}"
            )


def validate_board_measurement(
    case: UnifiedCase,
    sender: SenderEvidence,
    measurement: BoardMeasurement,
    expected_frame: int,
    expected_generation: int,
) -> tuple[float, float, float, float, float]:
    if (
        measurement.session_id != sender.session_id
        or measurement.config_id != sender.config_id
        or measurement.epoch <= 0
        or measurement.frame_id != expected_frame
        or measurement.generation != expected_generation
        or measurement.calibration_id != boundary.CALIBRATION_ID
        or measurement.test_pattern != 1
    ):
        raise RuntimeError(
            f"{case.case_id}: board identity/metadata mismatch at "
            f"frame {expected_frame}"
        )

    expected_vpp_mv, expected_rms_mv = expected_metrics(case.expected_p4_tones)
    f0_error_hz = abs(measurement.fundamental_hz - case.fundamental_hz)
    vpp_error_mv = abs(measurement.vpp_mv - expected_vpp_mv)
    rms_error_mv = abs(measurement.rms_mv - expected_rms_mv)
    if f0_error_hz > FREQUENCY_TOLERANCE_HZ:
        raise RuntimeError(
            f"{case.case_id}: F0 error {f0_error_hz:.3f} Hz exceeds "
            f"{FREQUENCY_TOLERANCE_HZ:.0f} Hz"
        )
    if vpp_error_mv > VOLTAGE_TOLERANCE_MV or rms_error_mv > VOLTAGE_TOLERANCE_MV:
        raise RuntimeError(
            f"{case.case_id}: Vpp/RMS errors "
            f"{vpp_error_mv:.3f}/{rms_error_mv:.3f} mV exceed "
            f"{VOLTAGE_TOLERANCE_MV:.0f} mV"
        )

    expected_tones = tuple(
        sorted(
            (
                harmonic * case.fundamental_hz,
                amplitude_volts_peak * 1000.0,
            )
            for harmonic, amplitude_volts_peak, _ in case.expected_p4_tones
        )
    )
    if measurement.peak_count != len(expected_tones):
        raise RuntimeError(
            f"{case.case_id}: expected {len(expected_tones)} P4 peaks, "
            f"got {measurement.peak_count}"
        )
    observed_tones = tuple(sorted(measurement.tones[: measurement.peak_count]))
    unused_tones = measurement.tones[measurement.peak_count :]
    if any(
        frequency_hz != 0.0 or amplitude_mv != 0.0
        for frequency_hz, amplitude_mv in unused_tones
    ):
        raise RuntimeError(f"{case.case_id}: unused P4 peak slots are not zero")

    max_tone_frequency_error_hz = 0.0
    max_tone_amplitude_error_mv = 0.0
    for expected, observed in zip(expected_tones, observed_tones, strict=True):
        frequency_error_hz = abs(observed[0] - expected[0])
        amplitude_error_mv = abs(observed[1] - expected[1])
        max_tone_frequency_error_hz = max(
            max_tone_frequency_error_hz, frequency_error_hz
        )
        max_tone_amplitude_error_mv = max(
            max_tone_amplitude_error_mv, amplitude_error_mv
        )
        if frequency_error_hz > FREQUENCY_TOLERANCE_HZ:
            raise RuntimeError(
                f"{case.case_id}: tone frequency error "
                f"{frequency_error_hz:.3f} Hz exceeds "
                f"{FREQUENCY_TOLERANCE_HZ:.0f} Hz"
            )
        if amplitude_error_mv > VOLTAGE_TOLERANCE_MV:
            raise RuntimeError(
                f"{case.case_id}: tone amplitude error "
                f"{amplitude_error_mv:.3f} mV exceeds "
                f"{VOLTAGE_TOLERANCE_MV:.0f} mV"
            )

    if case.case_id == FILTERED_RESIDUAL_CASE.case_id and any(
        frequency_hz > 500_000.0 + FREQUENCY_TOLERANCE_HZ
        for frequency_hz, _ in observed_tones
    ):
        raise RuntimeError("filtered residual leaked its 1 MHz H50 into P4 peaks")

    return (
        f0_error_hz,
        vpp_error_mv,
        rms_error_mv,
        max_tone_frequency_error_hz,
        max_tone_amplitude_error_mv,
    )


def validate_board_serial(
    path: Path,
    sender_evidence: tuple[SenderEvidence, ...],
) -> BoardEvidenceSummary:
    try:
        text = path.read_bytes().decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("UART log is not strict ASCII") from error
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text or ANSI_ESCAPE_PATTERN.search(text):
        raise RuntimeError("UART log contains unexpected control bytes")

    app_version = re.search(r"app_init: App version:\s+([^\n]+)", text)
    elf_hash = re.search(r"app_init: ELF file SHA256:\s+([0-9A-Fa-f]+)\.\.\.", text)
    if (
        "app_init: Project name:     CycleScopeP4" not in text
        or app_version is None
        or app_version.group(1).strip() != EXPECTED_BOARD_APP_VERSION
        or elf_hash is None
        or elf_hash.group(1).lower() != EXPECTED_BOARD_ELF_SHA_PREFIX
        or text.count("rst:0x1 (POWERON)") != 1
        or "CSLP v1 receiver ready on Core 1; golden packet PASS" not in text
        or "UDP 192.168.10.3:50001 -> 192.168.10.5:50000" not in text
        or re.search(
            r"FFT exact weak startup self-test:[^\n]*\bPASS\b", text
        )
        is None
    ):
        raise RuntimeError("UART log does not identify the expected current-v3 image")
    if "192.168.10.2" in text or "192.168.10.4" in text:
        raise RuntimeError("UART log touched an excluded FPGA address")

    ready_matches = list(BOARD_SESSION_PATTERN.finditer(text))
    board_evidence = tuple(
        SenderEvidence(
            match.group(1).upper(),
            int(match.group(2)),
            int(match.group(3)),
        )
        for match in ready_matches
    )
    if board_evidence != sender_evidence:
        raise RuntimeError(
            "UART session/boot/config sequence does not match the sender logs: "
            f"sender={sender_evidence} board={board_evidence}"
        )

    parsed_measurements = tuple(
        (match, parse_board_measurement(match))
        for match in BOARD_MEASUREMENT_PATTERN.finditer(text)
    )
    expected_measurement_order = tuple(
        (sender.session_id, frame_id)
        for sender in sender_evidence
        for frame_id in (1, 100)
    )
    actual_measurement_order = tuple(
        (measurement.session_id, measurement.frame_id)
        for _, measurement in parsed_measurements
    )
    if actual_measurement_order != expected_measurement_order:
        raise RuntimeError(
            "UART measurement sequence must be exactly frame 1/100 for each "
            f"sender session: {actual_measurement_order}"
        )

    final_measurement_match, _ = parsed_measurements[-1]
    matrix_start = ready_matches[0].start()
    matrix_end = final_measurement_match.end()
    matrix_window = text[matrix_start:matrix_end]
    if re.search(r"^E \(", matrix_window, re.MULTILINE):
        raise RuntimeError("UART matrix window contains an ESP-IDF error log")
    forbidden_patterns = (
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
    for pattern in forbidden_patterns:
        if re.search(pattern, matrix_window, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(
                f"UART matrix window contains forbidden marker: {pattern}"
            )

    completion_matches = tuple(
        match
        for match in PUBLISHED_FRAME_PATTERN.finditer(text)
        if matrix_start <= match.start() < matrix_end
        and int(match.group(1)) == 100
    )
    completed = tuple(int(match.group(2)) for match in completion_matches)
    expected_completed = tuple(100 * index for index in range(1, len(CASES) + 1))
    if completed != expected_completed:
        raise RuntimeError(
            f"UART frame-100 completion sequence changed: {completed}"
        )

    measurement_positions = {
        (measurement.session_id, measurement.frame_id): match
        for match, measurement in parsed_measurements
    }
    for index, sender in enumerate(sender_evidence):
        ready = ready_matches[index]
        section_end = (
            ready_matches[index + 1].start()
            if index + 1 < len(ready_matches)
            else matrix_end
        )
        frame_1 = measurement_positions[(sender.session_id, 1)]
        frame_100 = measurement_positions[(sender.session_id, 100)]
        section_completions = tuple(
            match
            for match in completion_matches
            if ready.end() < match.start() < section_end
        )
        if len(section_completions) != 1:
            raise RuntimeError(
                f"UART session {sender.session_id} does not contain exactly one "
                "frame-100 completion"
            )
        completion = section_completions[0]
        if int(completion.group(2)) != (index + 1) * 100:
            raise RuntimeError(
                f"UART session {sender.session_id} has the wrong cumulative "
                "completion"
            )
        if not (
            ready.end()
            < frame_1.start()
            < completion.start()
            < frame_100.start()
            and frame_100.end() <= section_end
        ):
            raise RuntimeError(
                f"UART session {sender.session_id} violates "
                "ready -> frame1 -> completed -> frame100 ordering"
            )

    peer_silent_positions = tuple(
        match.start()
        for match in re.finditer(
            r"CSLP peer silent for more than 1500 ms; starting a new session",
            matrix_window,
        )
    )
    if len(peer_silent_positions) != len(CASES) - 1:
        raise RuntimeError("UART matrix must contain four inter-case peer-silent events")
    for index, peer_position in enumerate(peer_silent_positions):
        frame_100_end = measurement_positions[
            (sender_evidence[index].session_id, 100)
        ].end()
        next_ready_start = ready_matches[index + 1].start()
        absolute_peer_position = matrix_start + peer_position
        if not frame_100_end < absolute_peer_position < next_ready_start:
            raise RuntimeError("UART peer-silent event occurred inside an active case")

    rx_health = list(RX_HEALTH_PATTERN.finditer(matrix_window))
    require_zero_health(
        rx_health,
        ("source", "magic", "version", "length", "session", "crc"),
        "receiver/rx",
    )
    frame_health = list(FRAME_HEALTH_PATTERN.finditer(matrix_window))
    require_zero_health(
        frame_health,
        ("overwrite", "incomplete", "duplicate", "stale", "busy"),
        "receiver/frame",
    )
    if any(
        int(match.group("completed")) != int(match.group("acquired"))
        for match in frame_health
    ):
        raise RuntimeError("UART receiver health has completed/acquired divergence")

    reject_health = list(REJECT_HEALTH_PATTERN.finditer(matrix_window))
    require_zero_health(
        reject_health,
        ("config", "metadata", "overrange", "fifo"),
        "receiver/reject",
    )
    socket_health = list(SOCKET_HEALTH_PATTERN.finditer(matrix_window))
    require_zero_health(
        socket_health,
        ("open_fail", "recv_fatal", "close_fail"),
        "receiver/socket",
    )
    if tuple(
        len(matches)
        for matches in (rx_health, frame_health, reject_health, socket_health)
    ) != (2, 2, 2, 2):
        raise RuntimeError("UART matrix must contain two complete receiver health sets")
    receiver_health_intervals = (
        (
            measurement_positions[(sender_evidence[0].session_id, 100)].end(),
            matrix_start + peer_silent_positions[0],
        ),
        (
            measurement_positions[(sender_evidence[-1].session_id, 1)].end(),
            completion_matches[-1].start(),
        ),
    )
    for index, (start, end) in enumerate(receiver_health_intervals):
        positions = tuple(
            matrix_start + matches[index].start()
            for matches in (rx_health, frame_health, reject_health, socket_health)
        )
        if not start < positions[0] < positions[1] < positions[2] < positions[3] < end:
            raise RuntimeError(
                f"UART receiver health set #{index + 1} is outside its live interval"
            )
    receiver_completed = tuple(
        int(match.group("completed")) for match in frame_health
    )
    if (
        receiver_completed[0] != 100
        or not 400 < receiver_completed[1] < len(CASES) * 100
    ):
        raise RuntimeError(
            f"UART receiver health completion bounds changed: {receiver_completed}"
        )
    socket_sessions = tuple(int(match.group("sessions")) for match in socket_health)
    if socket_sessions != (1, len(CASES)):
        raise RuntimeError(
            f"UART socket health session sequence changed: {socket_sessions}"
        )

    pipeline_health = list(PIPELINE_HEALTH_PATTERN.finditer(matrix_window))
    require_zero_health(
        pipeline_health,
        ("stale", "invalid", "fft_fail"),
        "pipeline",
    )
    if len(pipeline_health) != 2:
        raise RuntimeError("UART matrix must contain two pipeline health snapshots")
    pipeline_intervals = (
        (
            measurement_positions[(sender_evidence[1].session_id, 1)].end(),
            completion_matches[1].start(),
        ),
        (
            measurement_positions[(sender_evidence[-1].session_id, 1)].end(),
            completion_matches[-1].start(),
        ),
    )
    for index, (start, end) in enumerate(pipeline_intervals):
        position = matrix_start + pipeline_health[index].start()
        if not start < position < end:
            raise RuntimeError(
                f"UART pipeline health #{index + 1} is outside its live interval"
            )
    maximum_pipeline_published = 0
    maximum_ui_frames = 0
    for match in pipeline_health:
        acquired = int(match.group("acquired"))
        analyzed = int(match.group("analyzed"))
        published = int(match.group("published"))
        maximum_pipeline_published = max(maximum_pipeline_published, published)
        if match.group("ui") is not None:
            maximum_ui_frames = max(maximum_ui_frames, int(match.group("ui")))
        if (
            not 0 <= published <= len(CASES) * 100
            or acquired < analyzed
            or acquired - analyzed > 1
            or analyzed != published
        ):
            raise RuntimeError("UART pipeline health ordering is inconsistent")
        maximum_us_match = re.search(
            r"fft_us\(last/avg/max\)=[0-9]+/[0-9]+/([0-9]+)",
            match.group(0),
        )
        if maximum_us_match is None or int(maximum_us_match.group(1)) >= 50_000:
            raise RuntimeError("UART pipeline health exceeds the 50 ms FFT budget")
        if (
            match.group("tag") != "cyclescope_fft"
            or "selftest=PASS" not in match.group(0)
        ):
            raise RuntimeError("UART pipeline self-test is not PASS")
    pipeline_published = tuple(
        int(match.group("published")) for match in pipeline_health
    )
    if (
        not 100 < pipeline_published[0] <= 200
        or not 400 < pipeline_published[1] < len(CASES) * 100
        or maximum_pipeline_published <= 400
        or maximum_ui_frames == 0
    ):
        raise RuntimeError("UART pipeline health never reached the fifth/UI case")

    ui_matches = list(SPECTRUM_UI_PATTERN.finditer(matrix_window))
    if len(ui_matches) != 1:
        raise RuntimeError("UART matrix must contain exactly one Spectrum UI bridge")
    ui_match = ui_matches[0]
    ui_absolute_position = matrix_start + ui_match.start()
    first_frame_1 = measurement_positions[(sender_evidence[0].session_id, 1)]
    first_completion = completion_matches[0]
    if not first_frame_1.end() < ui_absolute_position < first_completion.start():
        raise RuntimeError("UART Spectrum UI bridge is outside the first live case")
    if (
        ui_match.group("session").upper() != sender_evidence[0].session_id
        or int(ui_match.group("frame")) != 2
        or int(ui_match.group("generation")) != 2
        or int(ui_match.group("buffer_sentinel")) != 255
        or int(ui_match.group("columns")) != EXPECTED_SPECTRUM_COLUMNS
        or int(ui_match.group("peak_count"))
        != len(CASES[0].expected_p4_tones)
        or not math.isclose(
            float(ui_match.group("sample_rate")),
            EXPECTED_SAMPLE_RATE_MHZ,
            abs_tol=0.000001,
        )
        or not math.isclose(
            float(ui_match.group("axis")),
            EXPECTED_SPECTRUM_AXIS_MHZ,
            abs_tol=0.000001,
        )
    ):
        raise RuntimeError("UART Spectrum UI bridge identity/profile changed")

    maximum_errors = [0.0] * 5
    for case_index, (case, sender) in enumerate(
        zip(CASES, sender_evidence, strict=True)
    ):
        case_measurements = tuple(
            measurement
            for _, measurement in parsed_measurements
            if measurement.session_id == sender.session_id
        )
        first, last = case_measurements
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
            raise RuntimeError(f"{case.case_id}: frame 1/100 measurements drifted")
        for measurement_index, measurement in enumerate(case_measurements):
            expected_frame = (1, 100)[measurement_index]
            expected_generation = case_index * 100 + expected_frame
            errors = validate_board_measurement(
                case,
                sender,
                measurement,
                expected_frame,
                expected_generation,
            )
            maximum_errors = [
                max(previous, current)
                for previous, current in zip(maximum_errors, errors, strict=True)
            ]

    return BoardEvidenceSummary(*maximum_errors)


def print_sender_result(
    sender_evidence: tuple[SenderEvidence, ...],
    board_summary: BoardEvidenceSummary | None,
) -> None:
    sessions = ",".join(item.session_id for item in sender_evidence)
    if board_summary is None:
        board_result = "board_result=UNPROVEN"
    else:
        board_result = (
            "completed=100,200,300,400,500 axis=0.50000MHz "
            "max_error(F0/Vpp/RMS/tone_f/tone_A)="
            f"{board_summary.max_f0_error_hz:.3f}Hz/"
            f"{board_summary.max_vpp_error_mv:.3f}mV/"
            f"{board_summary.max_rms_error_mv:.3f}mV/"
            f"{board_summary.max_tone_frequency_error_hz:.3f}Hz/"
            f"{board_summary.max_tone_amplitude_error_mv:.3f}mV; "
            "board_result=PASS"
        )
    print(
        "G current-v3 sender evidence passed: "
        f"cases={len(CASES)} frames={len(CASES) * 100} "
        f"wave_packets={len(CASES) * 1200} sessions={sessions} "
        f"path=.5:50000->.3:50001; {board_result}"
    )


def parse_log_argument(value: str) -> tuple[str, Path]:
    case_id, separator, path_text = value.partition("=")
    if not separator or not case_id or not path_text:
        raise argparse.ArgumentTypeError("log must be CASE_ID=PATH")
    return case_id, Path(path_text)


def current_logs() -> dict[str, Path]:
    return {case.case_id: DEFAULT_LOG_DIRECTORY / case.log_name for case in CASES}


def print_matrix() -> None:
    print("# CycleScope 当前 v3 G 题统一发送矩阵")
    for case in CASES:
        tx_vpp_mv, tx_rms_mv = expected_metrics(case.transmitted_tones)
        p4_vpp_mv, p4_rms_mv = expected_metrics(case.expected_p4_tones)
        print()
        print(f"## {case.case_id}")
        print(
            f"发送真值 Vpp/RMS={tx_vpp_mv:.6f}/{tx_rms_mv:.6f} mV；"
            f"P4 带内目标={p4_vpp_mv:.6f}/{p4_rms_mv:.6f} mV"
        )
        print("```bash")
        print(boundary.replay_command(as_matrix_case(case)))
        print("```")
    print()
    print(
        f"> 滤后残余 S16_LE={FILTERED_RESIDUAL_SAMPLE_BYTES} bytes，"
        f"CRC32=0x{FILTERED_RESIDUAL_SAMPLE_CRC32:08X}，"
        f"码程={FILTERED_RESIDUAL_CODE_RANGE[0]}…{FILTERED_RESIDUAL_CODE_RANGE[1]}。"
    )
    print()
    print(
        "> 发送完成只证明电脑端波形和 CSLP 会话；没有 UART measurement/health "
        "证据时，板端结果必须保持 UNPROVEN。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument(
        "--current-logs",
        action="store_true",
        help="validate the five fixed /tmp sender logs from the current-v3 run",
    )
    parser.add_argument(
        "--log",
        action="append",
        type=parse_log_argument,
        help="repeat CASE_ID=PATH exactly once for every unified case",
    )
    parser.add_argument(
        "--serial-log",
        type=Path,
        help="validate a UART capture matched to the complete sender log set",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_definitions()
    if (
        args.self_test_only
        and not args.current_logs
        and not args.log
        and args.serial_log is None
    ):
        print(f"G current-v3 unified matrix self-test passed ({len(CASES)} cases)")
        return 0

    logs: dict[str, Path] = current_logs() if args.current_logs else {}
    for case_id, path in args.log or ():
        if case_id in logs:
            raise SystemExit(f"duplicate sender log for {case_id}")
        logs[case_id] = path
    if logs:
        sender_evidence = validate_sender_logs(logs)
        board_summary = (
            validate_board_serial(args.serial_log, sender_evidence)
            if args.serial_log is not None
            else None
        )
        print_sender_result(sender_evidence, board_summary)
        return 0
    if args.serial_log is not None:
        raise SystemExit("--serial-log requires the complete sender log set")

    print_matrix()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
