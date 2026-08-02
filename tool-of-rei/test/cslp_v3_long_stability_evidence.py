#!/usr/bin/env python3
"""Validate the matched sender/UART evidence for the current-v3 10k run.

The fixture is read-only.  It does not open a socket or UART and does not
write a result file.  A PASS binds the PC truth log to one P4 session and
proves the digital receive/analyze/publish stability window only; it does not
replace the pending panel, physical-front-end, or real-FPGA acceptance work.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import re
import shlex

import cslp_g_v3_sender_evidence as common


EXPECTED_FRAMES = 10_000
EXPECTED_WAVE_PACKETS = EXPECTED_FRAMES * 12
EXPECTED_CHUNK_GAP_US = 500
EXPECTED_HOLD_SECONDS = 40
EXPECTED_HANDSHAKE_TIMEOUT_SECONDS = 15
EXPECTED_TONES = (
    (1, 0.025, 0.17),
    (3, 0.070, 0.92),
    (4, 0.025, -0.51),
)
EXPECTED_FUNDAMENTAL_HZ = 40_750.0
EXPECTED_CHECKPOINTS = (1, *range(100, EXPECTED_FRAMES + 1, 100))
EXPECTED_PIPELINE_CHECKPOINTS = tuple(range(600, 9_601, 600))
EXPECTED_RECEIVER_HEALTH_SNAPSHOTS = 18
EXPECTED_UI_HEALTH_SNAPSHOTS = 17
EXPECTED_PIPE_PSRAM_FREE = 28_044_452
MINIMUM_PIPE_INTERNAL_FREE = 118_000
MINIMUM_UI_FREE = 28_130_000
FRAME_PERIOD_MS = 50
DEFAULT_SENDER_LOG = Path(
    "/tmp/cyclescope-p4-v3-final-10000-v3-emulator.log"
)
DEFAULT_SERIAL_LOG = Path(
    "/tmp/cyclescope-p4-v3-final-10000-v3-serial.log"
)

HELLO_PATTERN = re.compile(
    r"^HELLO session=0x(?P<session>[0-9A-Fa-f]{8}) "
    r"seq=(?P<sequence>[0-9]+) port=50001 mtu=1472 "
    r"caps=0x0000001F status=0$",
    re.MULTILINE,
)
CONFIG_PATTERN = re.compile(
    r"^CONFIG_SET seq=(?P<sequence>[0-9]+) status=0 "
    r"config_id=0x(?P<config>[0-9A-Fa-f]{8}) "
    r"values=\(4062500, 8192, 50000, 1, 1, 1, 0\)$",
    re.MULTILINE,
)
ENABLE_PATTERN = re.compile(
    r"^ENABLE_PUSH seq=(?P<sequence>[0-9]+) status=0$",
    re.MULTILINE,
)
SENT_PATTERN = re.compile(
    r"^sent frame=(?P<frame>[0-9]+) packets=(?P<packets>[0-9]+)$",
    re.MULTILINE,
)
FFT_TIMING_PATTERN = re.compile(
    r"fft_us\(last/avg/max\)=([0-9]+)/([0-9]+)/([0-9]+)"
)
UI_HEALTH_EXTRA_PATTERN = re.compile(
    r"ui_overwrite=(?P<overwrite>[0-9]+).*"
    r"selftest=PASS max_ui_gap=(?P<gap>[0-9]+)ms "
    r"free=(?P<free>[0-9]+)"
)
PIPE_HEALTH_EXTRA_PATTERN = re.compile(
    r"internal_free=(?P<internal>[0-9]+) "
    r"psram_free=(?P<psram>[0-9]+)"
)
SCRIPT_START_PATTERN = re.compile(
    r'^Script started on (?P<timestamp>[^\[]+) '
    r'\[COMMAND="(?P<command>[^"]+)" [^\]]+\]$',
    re.MULTILINE,
)
SCRIPT_DONE_PATTERN = re.compile(
    r'^Script done on (?P<timestamp>[^\[]+) '
    r'\[COMMAND_EXIT_CODE="(?P<exit>[0-9]+)"\]$',
    re.MULTILINE,
)
ESP_LOG_TIMESTAMP_PATTERN = re.compile(
    r"^[IWE] \((?P<milliseconds>[0-9]+)\)", re.MULTILINE
)


@dataclass(frozen=True)
class StabilitySummary:
    session_id: str
    receiver_health_snapshots: int
    pipeline_health_snapshots: int
    maximum_fft_us: int
    average_fft_us: int
    minimum_internal_free: int
    psram_free: int
    max_f0_error_hz: float
    max_voltage_error_mv: float
    max_tone_frequency_error_hz: float
    max_tone_amplitude_error_mv: float
    stream_duration_ms: int


def read_ascii_log(path: Path, label: str) -> str:
    try:
        text = path.read_bytes().decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{label} log is not strict ASCII") from error
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text or common.ANSI_ESCAPE_PATTERN.search(text):
        raise RuntimeError(f"{label} log contains unexpected control bytes")
    return text


def validate_definitions() -> None:
    common.boundary.emulator.self_test()
    if (
        EXPECTED_CHECKPOINTS[0] != 1
        or EXPECTED_CHECKPOINTS[-1] != EXPECTED_FRAMES
        or len(EXPECTED_CHECKPOINTS) != 101
        or EXPECTED_PIPELINE_CHECKPOINTS
        != tuple(range(600, EXPECTED_FRAMES, 600))
    ):
        raise RuntimeError("long-run checkpoint definitions changed")
    samples = common.boundary.emulator.synthesize_multitone(
        EXPECTED_FUNDAMENTAL_HZ,
        EXPECTED_TONES,
        common.boundary.SCALE_UV_PER_LSB,
        common.boundary.OFFSET_UV,
    )
    if len(samples) != 8192:
        raise RuntimeError("long-run waveform is not one FFT frame")
    expected_vpp_mv, expected_rms_mv = common.expected_metrics(EXPECTED_TONES)
    if (
        not math.isclose(expected_vpp_mv, 181.421109, abs_tol=0.000001)
        or not math.isclose(expected_rms_mv, 55.452683, abs_tol=0.000001)
    ):
        raise RuntimeError("long-run analytic truth changed")


def validate_sender_log(path: Path) -> common.SenderEvidence:
    text = read_ascii_log(path, "sender")
    forbidden = (
        "Traceback",
        "KeyboardInterrupt",
        "timed out waiting",
        "unexpected source",
        'COMMAND_EXIT_CODE="1"',
    )
    if any(marker in text for marker in forbidden):
        raise RuntimeError("sender log contains a failure marker")
    starts = list(SCRIPT_START_PATTERN.finditer(text))
    completions = list(SCRIPT_DONE_PATTERN.finditer(text))
    if (
        len(starts) != 1
        or len(completions) != 1
        or starts[0].start() != 0
        or completions[0].group("exit") != "0"
        or text.count("Script started on ") != 1
        or text.count("Script done on ") != 1
        or text.count("COMMAND_EXIT_CODE=") != 1
        or text.rstrip().rsplit("\n", 1)[-1] != completions[0].group(0)
    ):
        raise RuntimeError("sender script header/footer is not one clean exit")
    try:
        start_time = datetime.fromisoformat(
            starts[0].group("timestamp").strip()
        )
        completion_time = datetime.fromisoformat(
            completions[0].group("timestamp").strip()
        )
    except ValueError as error:
        raise RuntimeError("sender script timestamp is invalid") from error
    sender_duration_seconds = (completion_time - start_time).total_seconds()
    nominal_active_seconds = (
        (EXPECTED_FRAMES - 1) * FRAME_PERIOD_MS / 1000.0
        + EXPECTED_HOLD_SECONDS
    )
    if not (
        nominal_active_seconds - 2.0
        <= sender_duration_seconds
        <= nominal_active_seconds
        + EXPECTED_HANDSHAKE_TIMEOUT_SECONDS
        + 5.0
    ):
        raise RuntimeError(
            f"sender duration does not cover the 10k run: "
            f"{sender_duration_seconds:.3f} s"
        )
    expected_command = (
        "python3",
        "ESP32-P4/tools/cslp_fpga_emulator.py",
        "--bind-ip",
        "192.168.10.5",
        "--port",
        "50000",
        "--peer-ip",
        "192.168.10.3",
        "--peer-port",
        "50001",
        "--frames",
        str(EXPECTED_FRAMES),
        "--chunk-gap-us",
        str(EXPECTED_CHUNK_GAP_US),
        "--hold-seconds",
        str(EXPECTED_HOLD_SECONDS),
        "--handshake-timeout",
        str(EXPECTED_HANDSHAKE_TIMEOUT_SECONDS),
        "--scenario",
        "normal",
    )
    output_required = (
        "listening on 192.168.10.5:50000; "
        "expecting 192.168.10.3:50001",
        f"sent frame={EXPECTED_FRAMES} packets={EXPECTED_WAVE_PACKETS}",
        f"completed frames={EXPECTED_FRAMES} "
        f"wave_packets={EXPECTED_WAVE_PACKETS}",
    )
    actual_command = tuple(shlex.split(starts[0].group("command")))
    output_lines = tuple(text.splitlines())
    output_cardinality = tuple(
        (fragment, output_lines.count(fragment)) for fragment in output_required
    )
    invalid_output = tuple(
        (fragment, count)
        for fragment, count in output_cardinality
        if count != 1
    )
    semantic_completion_lines = tuple(
        line for line in output_lines if line.startswith("completed frames=")
    )
    if (
        actual_command != expected_command
        or invalid_output
        or semantic_completion_lines != (output_required[-1],)
    ):
        raise RuntimeError(
            "sender log is incomplete: "
            f"command_match={actual_command == expected_command} "
            f"output_cardinality={invalid_output} "
            f"completion_lines={semantic_completion_lines}"
        )
    if "192.168.10.2" in text or "192.168.10.4" in text:
        raise RuntimeError("sender log touched an excluded FPGA address")

    synthetic = common.SYNTHETIC_PATTERN.search(text)
    if synthetic is None:
        raise RuntimeError("sender synthetic truth is missing")
    expected_vpp_mv, expected_rms_mv = common.expected_metrics(EXPECTED_TONES)
    if (
        not math.isclose(
            float(synthetic.group(1)),
            EXPECTED_FUNDAMENTAL_HZ,
            abs_tol=0.000001,
        )
        or not math.isclose(
            float(synthetic.group(2)), expected_vpp_mv, abs_tol=0.000001
        )
        or not math.isclose(
            float(synthetic.group(3)), expected_rms_mv, abs_tol=0.000001
        )
        or int(synthetic.group(4)) != common.boundary.SCALE_UV_PER_LSB
        or int(synthetic.group(5)) != common.boundary.OFFSET_UV
        or int(synthetic.group(6)) != common.boundary.CALIBRATION_ID
    ):
        raise RuntimeError("sender synthetic truth changed")

    parsed_tones = tuple(
        (
            int(match.group(1)),
            float(match.group(3)) / 1000.0,
            float(match.group(4)),
            float(match.group(2)),
        )
        for match in common.TONE_PATTERN.finditer(text)
    )
    if len(parsed_tones) != len(EXPECTED_TONES):
        raise RuntimeError("sender tone count changed")
    for expected, actual in zip(EXPECTED_TONES, parsed_tones, strict=True):
        harmonic, amplitude, phase = expected
        actual_harmonic, actual_amplitude, actual_phase, actual_frequency = actual
        if (
            actual_harmonic != harmonic
            or not math.isclose(actual_amplitude, amplitude, abs_tol=0.0000005)
            or not math.isclose(actual_phase, phase, abs_tol=0.0000005)
            or not math.isclose(
                actual_frequency,
                harmonic * EXPECTED_FUNDAMENTAL_HZ,
                abs_tol=0.000001,
            )
        ):
            raise RuntimeError("sender tone truth changed")

    hello = HELLO_PATTERN.findall(text)
    config = CONFIG_PATTERN.findall(text)
    enable = ENABLE_PATTERN.findall(text)
    ready = common.SENDER_SESSION_PATTERN.findall(text)
    control_statuses = tuple(
        int(match.group(1))
        for match in re.finditer(
            r"^(?:HELLO|CONFIG_SET|ENABLE_PUSH)[^\n]* status=([0-9]+)(?: |$)",
            text,
            re.MULTILINE,
        )
    )
    if (
        len(hello) != 1
        or len(config) != 1
        or len(enable) != 1
        or len(ready) != 1
        or control_statuses != (0, 0, 0)
    ):
        raise RuntimeError("sender control transaction cardinality changed")
    hello_session, hello_sequence = hello[0]
    config_sequence, config_id = config[0]
    enable_sequence = enable[0]
    ready_session, boot_id, ready_config = ready[0]
    if (
        ready_session.upper() != hello_session.upper()
        or ready_config.upper() != config_id.upper()
        or int(config_sequence) != (int(hello_sequence) + 1) & 0xFFFFFFFF
        or int(enable_sequence) != (int(hello_sequence) + 2) & 0xFFFFFFFF
    ):
        raise RuntimeError("sender control sequence/session/config mismatch")

    progress = tuple(
        (int(match.group("frame")), int(match.group("packets")))
        for match in SENT_PATTERN.finditer(text)
    )
    expected_progress_frames = (1, *range(25, EXPECTED_FRAMES + 1, 25))
    if tuple(frame for frame, _ in progress) != expected_progress_frames:
        raise RuntimeError("sender progress sequence changed")
    if any(packets != frame * 12 for frame, packets in progress):
        raise RuntimeError("sender progress packet count changed")

    return common.SenderEvidence(
        ready_session.upper(),
        int(boot_id, 16),
        int(ready_config, 16),
    )


def measurement_signature(
    measurement: common.BoardMeasurement,
) -> tuple[float | int | tuple[tuple[float, float], ...], ...]:
    return (
        measurement.fundamental_hz,
        measurement.vpp_mv,
        measurement.rms_mv,
        measurement.peak_count,
        measurement.tones,
        measurement.calibration_id,
        measurement.test_pattern,
    )


def validate_serial_log(
    path: Path,
    sender: common.SenderEvidence,
) -> StabilitySummary:
    text = read_ascii_log(path, "UART")
    app_version = re.search(r"app_init: App version:\s+([^\n]+)", text)
    elf_hash = re.search(
        r"app_init: ELF file SHA256:\s+([0-9A-Fa-f]+)\.\.\.", text
    )
    exact_self_test = re.search(
        r"FFT exact weak startup self-test: crc=0x4ECFD324 "
        r"F0=10000\.04Hz Vpp=50\.002mV RMS=16\.141mV lines=2 "
        r"P1=5\.537mVpk P2=22\.145mVpk elapsed=([0-9]+)us "
        r"heap=PASS PASS",
        text,
    )
    if (
        text.count("ESP-ROM:esp32p4-eco2-20240710") != 1
        or text.count("rst:0x1 (POWERON)") != 1
        or "app_init: Project name:     CycleScopeP4" not in text
        or app_version is None
        or app_version.group(1).strip() != common.EXPECTED_BOARD_APP_VERSION
        or elf_hash is None
        or elf_hash.group(1).lower() != common.EXPECTED_BOARD_ELF_SHA_PREFIX
        or exact_self_test is None
        or int(exact_self_test.group(1)) >= 50_000
        or "CSLP v1 receiver ready on Core 1; golden packet PASS" not in text
        or "UDP 192.168.10.3:50001 -> 192.168.10.5:50000" not in text
    ):
        raise RuntimeError("UART log does not identify the expected current-v3 image")
    if "192.168.10.2" in text or "192.168.10.4" in text:
        raise RuntimeError("UART log touched an excluded FPGA address")
    esp_timestamps = tuple(
        int(match.group("milliseconds"))
        for match in ESP_LOG_TIMESTAMP_PATTERN.finditer(text)
    )
    if len(esp_timestamps) < 500 or any(
        current < previous
        for previous, current in zip(
            esp_timestamps[:-1], esp_timestamps[1:], strict=True
        )
    ):
        raise RuntimeError("UART ESP-IDF timestamps are missing or non-monotonic")

    def match_timestamp(match: re.Match[str]) -> int:
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        timestamp = re.match(
            r"^[IWE] \(([0-9]+)\)", text[line_start:line_end]
        )
        if timestamp is None:
            raise RuntimeError("UART evidence line has no ESP-IDF timestamp")
        return int(timestamp.group(1))

    ready_matches = list(common.BOARD_SESSION_PATTERN.finditer(text))
    board_sessions = tuple(
        common.SenderEvidence(
            match.group(1).upper(),
            int(match.group(2)),
            int(match.group(3)),
        )
        for match in ready_matches
    )
    if board_sessions != (sender,):
        raise RuntimeError(
            "UART session/boot/config does not match the sender: "
            f"sender={sender} board={board_sessions}"
        )
    ready = ready_matches[0]

    published = list(common.PUBLISHED_FRAME_PATTERN.finditer(text))
    published_values = tuple(
        (int(match.group(1)), int(match.group(2))) for match in published
    )
    if published_values != tuple((value, value) for value in EXPECTED_CHECKPOINTS):
        raise RuntimeError("UART published checkpoint sequence changed")

    parsed_measurements = tuple(
        (match, common.parse_board_measurement(match))
        for match in common.BOARD_MEASUREMENT_PATTERN.finditer(text)
    )
    measurement_frames = tuple(
        measurement.frame_id for _, measurement in parsed_measurements
    )
    if measurement_frames != EXPECTED_CHECKPOINTS:
        raise RuntimeError("UART measurement checkpoint sequence changed")
    for index, ((published_match, _), (measurement_match, _)) in enumerate(
        zip(published_values, parsed_measurements, strict=True)
    ):
        next_published_start = (
            published[index + 1].start()
            if index + 1 < len(published)
            else len(text)
        )
        if not (
            published[index].end()
            < measurement_match.start()
            < next_published_start
        ):
            raise RuntimeError("UART publish/measurement ordering changed")
    if ready.end() >= published[0].start():
        raise RuntimeError("UART first frame precedes session ready")
    published_by_frame = {
        frame: match
        for match, (frame, _) in zip(published, published_values, strict=True)
    }
    measurements_by_frame = {
        measurement.frame_id: match
        for match, measurement in parsed_measurements
    }

    expected_vpp_mv, expected_rms_mv = common.expected_metrics(EXPECTED_TONES)
    expected_tones = tuple(
        sorted(
            (
                harmonic * EXPECTED_FUNDAMENTAL_HZ,
                amplitude_volts_peak * 1000.0,
            )
            for harmonic, amplitude_volts_peak, _ in EXPECTED_TONES
        )
    )
    reference_signature = measurement_signature(parsed_measurements[0][1])
    max_f0_error_hz = 0.0
    max_voltage_error_mv = 0.0
    max_tone_frequency_error_hz = 0.0
    max_tone_amplitude_error_mv = 0.0
    epoch = parsed_measurements[0][1].epoch
    for _, measurement in parsed_measurements:
        if (
            measurement.session_id != sender.session_id
            or measurement.config_id != sender.config_id
            or measurement.epoch != epoch
            or measurement.epoch <= 0
            or measurement.generation != measurement.frame_id
            or measurement.peak_count != len(expected_tones)
            or measurement.calibration_id != common.boundary.CALIBRATION_ID
            or measurement.test_pattern != 1
            or measurement_signature(measurement) != reference_signature
        ):
            raise RuntimeError(
                f"UART measurement identity/drift at frame {measurement.frame_id}"
            )
        f0_error_hz = abs(
            measurement.fundamental_hz - EXPECTED_FUNDAMENTAL_HZ
        )
        voltage_error_mv = max(
            abs(measurement.vpp_mv - expected_vpp_mv),
            abs(measurement.rms_mv - expected_rms_mv),
        )
        observed_tones = tuple(sorted(measurement.tones))
        tone_frequency_error_hz = max(
            abs(observed[0] - expected[0])
            for expected, observed in zip(
                expected_tones, observed_tones, strict=True
            )
        )
        tone_amplitude_error_mv = max(
            abs(observed[1] - expected[1])
            for expected, observed in zip(
                expected_tones, observed_tones, strict=True
            )
        )
        max_f0_error_hz = max(max_f0_error_hz, f0_error_hz)
        max_voltage_error_mv = max(max_voltage_error_mv, voltage_error_mv)
        max_tone_frequency_error_hz = max(
            max_tone_frequency_error_hz, tone_frequency_error_hz
        )
        max_tone_amplitude_error_mv = max(
            max_tone_amplitude_error_mv, tone_amplitude_error_mv
        )
    if (
        max_f0_error_hz > common.FREQUENCY_TOLERANCE_HZ
        or max_voltage_error_mv > common.VOLTAGE_TOLERANCE_MV
        or max_tone_frequency_error_hz > common.FREQUENCY_TOLERANCE_HZ
        or max_tone_amplitude_error_mv > common.VOLTAGE_TOLERANCE_MV
    ):
        raise RuntimeError("UART measurement exceeds the G-problem tolerance")

    rx_health = [
        match
        for match in common.RX_HEALTH_PATTERN.finditer(text)
        if match.start() > ready.end()
    ]
    frame_health = [
        match
        for match in common.FRAME_HEALTH_PATTERN.finditer(text)
        if match.start() > ready.end()
    ]
    reject_health = [
        match
        for match in common.REJECT_HEALTH_PATTERN.finditer(text)
        if match.start() > ready.end()
    ]
    socket_health = [
        match
        for match in common.SOCKET_HEALTH_PATTERN.finditer(text)
        if match.start() > ready.end()
    ]
    health_count = len(frame_health)
    if (
        health_count != EXPECTED_RECEIVER_HEALTH_SNAPSHOTS
        or tuple(
            len(matches)
            for matches in (rx_health, frame_health, reject_health, socket_health)
        )
        != (health_count,) * 4
    ):
        raise RuntimeError("UART receiver health-set cardinality changed")
    peer_silent_matches = list(
        re.finditer(
            r"CSLP peer silent for more than 1500 ms; starting a new session",
            text,
        )
    )
    if (
        not peer_silent_matches
        or peer_silent_matches[0].start() <= socket_health[-1].end()
    ):
        raise RuntimeError("UART hold tail does not end with peer-silent")
    formal_end = peer_silent_matches[0].start()
    ready_timestamp_ms = match_timestamp(ready)
    final_measurement_timestamp_ms = match_timestamp(
        parsed_measurements[-1][0]
    )
    peer_silent_timestamp_ms = match_timestamp(peer_silent_matches[0])
    stream_duration_ms = final_measurement_timestamp_ms - ready_timestamp_ms
    nominal_stream_ms = (EXPECTED_FRAMES - 1) * FRAME_PERIOD_MS
    if not (
        nominal_stream_ms - 5_000
        <= stream_duration_ms
        <= nominal_stream_ms + 15_000
        and 35_000
        <= peer_silent_timestamp_ms - final_measurement_timestamp_ms
        <= 50_000
    ):
        raise RuntimeError(
            "UART timestamps do not cover the 10k stream and hold tail"
        )

    def counter_interval(counter: int) -> tuple[int, int]:
        lower_candidates = tuple(
            checkpoint
            for checkpoint in EXPECTED_CHECKPOINTS
            if checkpoint <= counter
        )
        if not lower_candidates or counter > EXPECTED_FRAMES:
            raise RuntimeError(f"UART health counter is out of range: {counter}")
        lower = lower_candidates[-1]
        lower_end = measurements_by_frame[lower].end()
        lower_index = EXPECTED_CHECKPOINTS.index(lower)
        upper_start = (
            published_by_frame[EXPECTED_CHECKPOINTS[lower_index + 1]].start()
            if lower_index + 1 < len(EXPECTED_CHECKPOINTS)
            else formal_end
        )
        return lower_end, upper_start

    for index in range(health_count):
        positions = tuple(
            matches[index].start()
            for matches in (rx_health, frame_health, reject_health, socket_health)
        )
        completed = int(frame_health[index].group("completed"))
        interval_start, interval_end = counter_interval(completed)
        if not (
            interval_start
            < positions[0]
            < positions[1]
            < positions[2]
            < positions[3]
            and socket_health[index].end() < interval_end
        ):
            raise RuntimeError("UART receiver health-set ordering changed")
    common.require_zero_health(
        rx_health,
        ("source", "magic", "version", "length", "session", "crc"),
        "receiver/rx",
    )
    common.require_zero_health(
        frame_health,
        ("overwrite", "incomplete", "duplicate", "stale", "busy"),
        "receiver/frame",
    )
    common.require_zero_health(
        reject_health,
        ("config", "metadata", "overrange", "fifo"),
        "receiver/reject",
    )
    common.require_zero_health(
        socket_health,
        ("open_fail", "recv_fatal", "close_fail"),
        "receiver/socket",
    )
    receiver_completed = tuple(
        int(match.group("completed")) for match in frame_health
    )
    receiver_acquired = tuple(
        int(match.group("acquired")) for match in frame_health
    )
    if (
        any(
            current <= previous
            for previous, current in zip(
                receiver_completed[:-1],
                receiver_completed[1:],
                strict=True,
            )
        )
        or any(
            not 0 <= completed - acquired <= 1
            for completed, acquired in zip(
                receiver_completed, receiver_acquired, strict=True
            )
        )
        or receiver_completed[-1] != EXPECTED_FRAMES
        or receiver_acquired[-1] != EXPECTED_FRAMES
    ):
        raise RuntimeError("UART receiver completed/acquired sequence changed")
    receiver_packets = tuple(int(match.group("packets")) for match in rx_health)
    if (
        any(
            current <= previous
            for previous, current in zip(
                receiver_packets[:-1], receiver_packets[1:], strict=True
            )
        )
        or receiver_packets[-1] < EXPECTED_WAVE_PACKETS
    ):
        raise RuntimeError("UART receiver packet sequence is incomplete")
    retry_pairs = tuple(
        (int(match.group("retries")), int(match.group("reconnects")))
        for match in reject_health
    )
    socket_sessions = tuple(
        int(match.group("sessions")) for match in socket_health
    )
    if len(set(retry_pairs)) != 1 or set(socket_sessions) != {1}:
        raise RuntimeError("UART retry/reconnect/session counters grew in-window")

    final_health_end = socket_health[-1].end()
    if parsed_measurements[-1][0].end() >= final_health_end:
        raise RuntimeError("UART final health does not follow frame 10000")
    formal_window = text[ready.start():formal_end]
    forbidden_patterns = (
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
        r"Measurement rejected",
        r"FFT failed",
        r"Discarded stale",
        r"Unable to publish",
        r"Control transaction[^\n]*timed out",
        r"handshake failed",
        r"ESP-ROM:",
        r"^rst:",
    )
    for pattern in forbidden_patterns:
        if re.search(pattern, formal_window, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(
                f"UART formal window contains forbidden marker: {pattern}"
            )

    pipeline_health = [
        match
        for match in common.PIPELINE_HEALTH_PATTERN.finditer(text)
        if ready.end() < match.start() < formal_end
    ]
    common.require_zero_health(
        pipeline_health,
        ("stale", "invalid", "fft_fail"),
        "pipeline",
    )
    maximum_fft_us = 0
    last_average_fft_us = 0
    ui_free = []
    pipe_internal_free = []
    pipe_psram_free = []
    pipe_checkpoints = []
    ui_health_count = 0
    previous_published = 0
    for match in pipeline_health:
        acquired = int(match.group("acquired"))
        analyzed = int(match.group("analyzed"))
        published_count = int(match.group("published"))
        if (
            published_count < previous_published
            or not 0 <= acquired - analyzed <= 1
            or analyzed != published_count
            or published_count > EXPECTED_FRAMES
        ):
            raise RuntimeError("UART pipeline health ordering/count changed")
        previous_published = published_count
        interval_start, interval_end = counter_interval(published_count)
        if not (
            interval_start < match.start() and match.end() < interval_end
        ):
            raise RuntimeError("UART pipeline health is outside its frame interval")
        timing = FFT_TIMING_PATTERN.search(match.group(0))
        if timing is None or int(timing.group(3)) >= 50_000:
            raise RuntimeError("UART pipeline FFT timing budget failed")
        last_average_fft_us = int(timing.group(2))
        maximum_fft_us = max(maximum_fft_us, int(timing.group(3)))
        if match.group("tag") == "cyclescope_fft":
            ui_health_count += 1
            extra = UI_HEALTH_EXTRA_PATTERN.search(match.group(0))
            if extra is None or match.group("ui") is None:
                raise RuntimeError("UART UI health is incomplete")
            ui_frames = int(match.group("ui"))
            overwrite = int(extra.group("overwrite"))
            if published_count - ui_frames - overwrite not in (0, 1):
                raise RuntimeError("UART latest-UI queue accounting changed")
            if int(extra.group("gap")) > 300:
                raise RuntimeError("UART UI gap exceeds 300 ms")
            ui_free.append(int(extra.group("free")))
        else:
            extra = PIPE_HEALTH_EXTRA_PATTERN.search(match.group(0))
            if extra is None or not (
                acquired == analyzed == published_count
            ):
                raise RuntimeError("UART exact pipeline health is incomplete")
            pipe_checkpoints.append(published_count)
            pipe_internal_free.append(int(extra.group("internal")))
            pipe_psram_free.append(int(extra.group("psram")))
    if (
        tuple(pipe_checkpoints) != EXPECTED_PIPELINE_CHECKPOINTS
        or ui_health_count != EXPECTED_UI_HEALTH_SNAPSHOTS
        or len(pipeline_health)
        != len(EXPECTED_PIPELINE_CHECKPOINTS)
        + EXPECTED_UI_HEALTH_SNAPSHOTS
    ):
        raise RuntimeError("UART exact pipeline checkpoint sequence changed")
    if (
        not ui_free
        or ui_free[-1] < ui_free[0] - 64
        or min(ui_free) < MINIMUM_UI_FREE
        or not pipe_internal_free
        or pipe_internal_free[-1] < pipe_internal_free[0] - 64
        or max(pipe_internal_free) - pipe_internal_free[-1] > 64
        or min(pipe_internal_free) < pipe_internal_free[-1] - 1024
        or min(pipe_internal_free) < MINIMUM_PIPE_INTERNAL_FREE
        or set(pipe_psram_free) != {EXPECTED_PIPE_PSRAM_FREE}
    ):
        raise RuntimeError("UART long-run memory trend changed")

    ui_matches = [
        match
        for match in common.SPECTRUM_UI_PATTERN.finditer(text)
        if ready.end() < match.start() < formal_end
    ]
    if len(ui_matches) != 1:
        raise RuntimeError("UART long run must contain one Spectrum UI bridge")
    ui_match = ui_matches[0]
    if (
        ui_match.group("session").upper() != sender.session_id
        or int(ui_match.group("frame")) != int(ui_match.group("generation"))
        or not 1 <= int(ui_match.group("frame")) <= 5
        or int(ui_match.group("buffer_sentinel")) != 255
        or int(ui_match.group("columns")) != common.EXPECTED_SPECTRUM_COLUMNS
        or int(ui_match.group("peak_count")) != len(EXPECTED_TONES)
        or not math.isclose(
            float(ui_match.group("sample_rate")),
            common.EXPECTED_SAMPLE_RATE_MHZ,
            abs_tol=0.000001,
        )
        or not math.isclose(
            float(ui_match.group("axis")),
            common.EXPECTED_SPECTRUM_AXIS_MHZ,
            abs_tol=0.000001,
        )
        or not (
            parsed_measurements[0][0].end()
            < ui_match.start()
            < published[1].start()
        )
    ):
        raise RuntimeError("UART Spectrum UI bridge identity/profile changed")

    return StabilitySummary(
        session_id=sender.session_id,
        receiver_health_snapshots=health_count,
        pipeline_health_snapshots=len(pipeline_health),
        maximum_fft_us=maximum_fft_us,
        average_fft_us=last_average_fft_us,
        minimum_internal_free=min(pipe_internal_free),
        psram_free=pipe_psram_free[-1],
        max_f0_error_hz=max_f0_error_hz,
        max_voltage_error_mv=max_voltage_error_mv,
        max_tone_frequency_error_hz=max_tone_frequency_error_hz,
        max_tone_amplitude_error_mv=max_tone_amplitude_error_mv,
        stream_duration_ms=stream_duration_ms,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument("--current-logs", action="store_true")
    parser.add_argument("--sender-log", type=Path)
    parser.add_argument("--serial-log", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_definitions()
    if args.self_test_only and not (
        args.current_logs or args.sender_log or args.serial_log
    ):
        print("CycleScope current-v3 10k stability definitions passed")
        return 0
    sender_log = DEFAULT_SENDER_LOG if args.current_logs else args.sender_log
    serial_log = DEFAULT_SERIAL_LOG if args.current_logs else args.serial_log
    if sender_log is None or serial_log is None:
        raise SystemExit("provide --current-logs or both --sender-log/--serial-log")
    sender = validate_sender_log(sender_log)
    summary = validate_serial_log(serial_log, sender)
    print(
        "CycleScope current-v3 10k stability evidence passed: "
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
        "path=.5:50000->.3:50001; digital_stability=PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
