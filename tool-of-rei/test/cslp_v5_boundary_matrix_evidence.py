#!/usr/bin/env python3
"""Validate the normal-v5 four-boundary sender/UART evidence.

The fixture is deliberately read-only.  It reads four completed ``script``
sender transcripts and one UART transcript, opens no socket or serial port,
starts no subprocess, and writes no result file.  The formal ``final4``
profile includes all four peer-silent/LIVE-to-STALE edges.  The older
``final2`` compatibility profile ends at case four's frame-100 measurement and
therefore explicitly does *not* claim the fourth edge.

A PASS covers only PC-synthetic, already-filtered CSLP data received and
analyzed by the ESP32-P4.  It does not prove the physical BNC/ADC/FPGA path,
the >=1 MHz interference path, or panel/LVGL visual behavior.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import math
from pathlib import Path
import re

import cslp_g_acceptance_matrix as boundary


DEFAULT_PROFILE = "final4"
PROFILE_NAMES = ("final4", "final2")
PROFILE_SENDER_LOGS = {
    profile: tuple(
        Path(
            f"/tmp/cyclescope-p4-v5-boundary-{profile}-{index}-emulator.log"
        )
        for index in range(1, 5)
    )
    for profile in PROFILE_NAMES
}
PROFILE_SERIAL_LOGS = {
    profile: Path(
        f"/tmp/cyclescope-p4-v5-boundary-{profile}-4x100-serial.log"
    )
    for profile in PROFILE_NAMES
}
PROFILE_NORMALIZED_LOG_SHA256 = {
    "final4": (
        "ee55a71aa5491fcc3cbef0c169cf8024d0c432514c36fbc47e1965870e6f5b10",
        "d0dafd2905d182d4cc32fa92a10bcd28093d330b88edba3a6804693bc6ce4f8c",
        "3d42275b63fc24eb689ddca6b8f7a5e1db7ba3df299b383bbde46c486ee3ce23",
        "1acc4e8b81cf24ef2fd07e587f09ae560acd18f55c22c48e90bb0fc976938efa",
        "cf565928939841a485e44eefa53dfa7c4a8f126314addad5d9cbde2c351e3822",
    ),
    "final2": (
        "5712745b4df6419e02329b365079a01b22650099c7fbd0a224b05231a963dd57",
        "4e7ba2621645292371916b681fd5049f854f8ef56880174eb3cd1b17e164987f",
        "55b6d7098cb42ff24b743c0ec9ed98ff5382a84f7d02afd48af5828b29f182a9",
        "e1e07e706aec428351b594cda7890397faad8d0faaa7408d88155158701105b6",
        "4b3dae24c834b3d1023e5c1b71be28ecc87a662435e82039937b7240f9b2fefd",
    ),
}
DEFAULT_SENDER_LOGS = PROFILE_SENDER_LOGS[DEFAULT_PROFILE]
DEFAULT_SERIAL_LOG = PROFILE_SERIAL_LOGS[DEFAULT_PROFILE]

EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA_PREFIX = "6d15ace8d"
EXPECTED_MATRIX_SHA256 = (
    "e2a06ffc7b42d7a8c722df7864cb76e77b67054b36797761c107bc49f2dac566"
)
EXPECTED_EMULATOR_SHA256 = (
    "0e07aa86a63ea9f0f7f9249c6a01759a5e4c27775f84cca4b5efbc2386a0032c"
)

FREQUENCY_TOLERANCE_HZ = 1_000.0
VOLTAGE_TOLERANCE_MV = 5.0
MINIMUM_SENDER_DURATION_SECONDS = 5.0
MAXIMUM_SENDER_DURATION_SECONDS = 30.0
EXPECTED_SCALE_MAX_MV = (100.0, 100.0, 50.0, 100.0)
EXPECTED_COMPLETED = (100, 200, 300, 400)
PROFILE_EPOCHS = {
    "final4": (2, 4, 6, 8),
    "final2": (132, 134, 136, 138),
}
PROFILE_LIVE_FRAMES = {
    "final4": (1, 1, 2, 2),
    "final2": (1, 2, 2, 2),
}
PROFILE_SENDER_IDENTITIES = {
    "final4": (
        ("BA29D2A6", 0xF6A0FE13, 0x7D079E94),
        ("BA29D2A7", 0xDA7EB45D, 0x0C9EE0FA),
        ("BA29D2A8", 0x84B694CD, 0x93BC3A98),
        ("BA29D2A9", 0x7D56B4BA, 0xECDCFE01),
    ),
    "final2": (
        ("62CBA839", 0x38AB469E, 0x697B22C1),
        ("62CBA83A", 0xEB01E9C6, 0x7E087657),
        ("62CBA83B", 0xB19121D1, 0x55C9420A),
        ("62CBA83C", 0x5AA801B7, 0x34AB8D44),
    ),
}

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SCRIPT_START_PATTERN = re.compile(
    r'^Script started on (?P<timestamp>[^\[]+) '
    r'\[COMMAND="(?P<command>[^"]+)" <not executed on terminal>\]$'
)
SCRIPT_DONE_PATTERN = re.compile(
    r'^Script done on (?P<timestamp>[^\[]+) '
    r'\[COMMAND_EXIT_CODE="(?P<exit_code>[0-9]+)"\]$'
)
HELLO_PATTERN = re.compile(
    r"^HELLO session=0x(?P<session>[0-9A-Fa-f]{8}) "
    r"seq=(?P<seq>[0-9]+) port=50001 mtu=1472 "
    r"caps=0x0000001F status=(?P<status>[0-9]+)$"
)
CONFIG_PATTERN = re.compile(
    r"^CONFIG_SET seq=(?P<seq>[0-9]+) status=(?P<status>[0-9]+) "
    r"config_id=0x(?P<config>[0-9A-Fa-f]{8}) "
    r"values=\(4062500, 8192, 50000, 1, 1, 1, 0\)$"
)
ENABLE_PATTERN = re.compile(
    r"^ENABLE_PUSH seq=(?P<seq>[0-9]+) status=(?P<status>[0-9]+)$"
)
SENDER_READY_PATTERN = re.compile(
    r"^session ready: session=0x(?P<session>[0-9A-Fa-f]{8}) "
    r"boot_id=0x(?P<boot>[0-9A-Fa-f]{8}) "
    r"config_id=0x(?P<config>[0-9A-Fa-f]{8})$"
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
    r"frame=(?P<frame>[0-9]+) "
    r"previous=(?P<previous>[0-9]+\.[0-9]+)mVpk "
    r"Amax=(?P<amax>[0-9]+\.[0-9]+)mVpk "
    r"reason=(?P<reason>NEW_STREAM|UPSHIFT|DOWNSHIFT)$",
    re.MULTILINE,
)
MEASUREMENT_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_pipe: measurement: "
    r"session=(?P<session>[0-9A-Fa-f]{8}) "
    r"config=(?P<config>[0-9A-Fa-f]{8}) epoch=(?P<epoch>[0-9]+) "
    r"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    r"F0=(?P<f0>[0-9]+\.[0-9]+)Hz "
    r"Vpp=(?P<vpp>[0-9]+\.[0-9]+)mV "
    r"RMS=(?P<rms>[0-9]+\.[0-9]+)mV "
    r"peaks=(?P<peaks>[0-9]+) "
    r"P1=(?P<p1f>[0-9]+\.[0-9]+)Hz/"
    r"(?P<p1a>[0-9]+\.[0-9]+)mVpk "
    r"P2=(?P<p2f>[0-9]+\.[0-9]+)Hz/"
    r"(?P<p2a>[0-9]+\.[0-9]+)mVpk "
    r"P3=(?P<p3f>[0-9]+\.[0-9]+)Hz/"
    r"(?P<p3a>[0-9]+\.[0-9]+)mVpk "
    r"cal=(?P<calibration>[0-9]+) test=(?P<test>[0-9]+)$",
    re.MULTILINE,
)
PUBLICATION_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: "
    r"Published frame=(?P<frame>[0-9]+) "
    r"completed=(?P<completed>[0-9]+)$",
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
SPECTRUM_UI_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    r"Spectrum UI bridge on Core 0: session=(?P<session>[0-9A-Fa-f]{8}) "
    r"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    r"A/B=(?P<sentinel>[0-9]+) columns=(?P<columns>[0-9]+) "
    r"peaks=(?P<peaks>[0-9]+) "
    r"Fs=(?P<sample_rate>[0-9]+\.[0-9]+)MHz "
    r"axis=(?P<axis>[0-9]+\.[0-9]+)MHz "
    r"Amax=(?P<amax>[0-9]+\.[0-9]+)mVpk$",
    re.MULTILINE,
)

RX_HEALTH_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: health/rx: "
    r"packets=(?P<packets>[0-9]+) source=(?P<source>[0-9]+) "
    r"magic=(?P<magic>[0-9]+) version=(?P<version>[0-9]+) "
    r"length=(?P<length>[0-9]+) session=(?P<session>[0-9]+) "
    r"crc=(?P<crc>[0-9]+)$",
    re.MULTILINE,
)
FRAME_HEALTH_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: health/frame: "
    r"completed=(?P<completed>[0-9]+) acquired=(?P<acquired>[0-9]+) "
    r"overwrite=(?P<overwrite>[0-9]+) "
    r"incomplete=(?P<incomplete>[0-9]+) "
    r"duplicate=(?P<duplicate>[0-9]+) stale=(?P<stale>[0-9]+) "
    r"busy=(?P<busy>[0-9]+)$",
    re.MULTILINE,
)
REJECT_HEALTH_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: health/reject: "
    r"config=(?P<config>[0-9]+) metadata=(?P<metadata>[0-9]+) "
    r"overrange=(?P<overrange>[0-9]+) fifo=(?P<fifo>[0-9]+) "
    r"retries=(?P<retries>[0-9]+) reconnects=(?P<reconnects>[0-9]+)$",
    re.MULTILINE,
)
SOCKET_HEALTH_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: health/socket: "
    r"open_fail=(?P<open_fail>[0-9]+) "
    r"recv_fatal=(?P<recv_fatal>[0-9]+) "
    r"close_fail=(?P<close_fail>[0-9]+) "
    r"sessions=(?P<sessions>[0-9]+)$",
    re.MULTILINE,
)
PIPELINE_HEALTH_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) "
    r"(?P<tag>cyclescope_fft|cyclescope_pipe): health: "
    r"acquired=(?P<acquired>[0-9]+) "
    r"analyzed=(?P<analyzed>[0-9]+) "
    r"published=(?P<published>[0-9]+) ui=(?P<ui>[0-9]+) "
    r"stale=(?P<stale>[0-9]+) invalid=(?P<invalid>[0-9]+) "
    r"(?:failures|fft_fail)=(?P<failures>[0-9]+) "
    r"ui_overwrite=(?P<ui_overwrite>[0-9]+) "
    r"fft_us\(last/avg/max\)=(?P<last_us>[0-9]+)/"
    r"(?P<average_us>[0-9]+)/(?P<maximum_us>[0-9]+) "
    r"selftest=(?P<selftest>PASS|FAIL) "
    r"max_ui_gap=(?P<max_ui_gap>[0-9]+)ms free=(?P<free>[0-9]+)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SenderIdentity:
    session_id: str
    boot_id: int
    config_id: int


@dataclass(frozen=True)
class SenderEvidence:
    identity: SenderIdentity
    started: datetime
    completed: datetime


@dataclass(frozen=True)
class Measurement:
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
class EvidenceSummary:
    profile: str
    identities: tuple[SenderIdentity, ...]
    max_f0_error_hz: float
    max_vpp_error_mv: float
    max_rms_error_mv: float
    max_line_frequency_error_hz: float
    max_line_amplitude_error_mv: float
    receiver_health_sets: int
    pipeline_health_snapshots: int


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
        raise RuntimeError(f"{label} is not strict ASCII") from error
    text = text.replace("\r\n", "\n")
    if (
        "\r" in text
        or "\x00" in text
        or ANSI_ESCAPE_PATTERN.search(text)
        or not text.endswith("\n")
    ):
        raise RuntimeError(f"{label} has an invalid control/trailer shape")
    return text


def _normalized_log_sha256(path: Path, label: str) -> str:
    normalized = _read_ascii(path, label)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _parse_wall_time(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise RuntimeError(f"{label} has an invalid wall timestamp") from error
    if result.tzinfo is None:
        raise RuntimeError(f"{label} wall timestamp has no timezone")
    return result


def _expected_sender_lines(case: boundary.MatrixCase) -> tuple[str, ...]:
    result = boundary.metrics(case)
    lines = [
        "synthetic multitone: "
        f"F0={case.fundamental_hz:.6f}Hz "
        f"Vpp={float(result['ideal_vpp_mv']):.6f}mV "
        f"RMS={float(result['ideal_rms_mv']):.6f}mV "
        f"scale={boundary.SCALE_UV_PER_LSB}uV/LSB "
        f"offset={boundary.OFFSET_UV}uV "
        f"calibration_id={boundary.CALIBRATION_ID}"
    ]
    lines.extend(
        f"  H{harmonic}: f={harmonic * case.fundamental_hz:.6f}Hz "
        f"A={amplitude * 1000.0:.6f}mVpk phase={phase:.6f}rad"
        for harmonic, amplitude, phase in case.tones
    )
    lines.append(
        "listening on 192.168.10.5:50000; "
        "expecting 192.168.10.3:50001"
    )
    return tuple(lines)


def validate_definitions() -> None:
    boundary.validate_matrix()
    commands = tuple(boundary.replay_command(case) for case in boundary.CASES)
    if (
        _sha256(Path(boundary.__file__)) != EXPECTED_MATRIX_SHA256
        or _sha256(Path(boundary.emulator.__file__))
        != EXPECTED_EMULATOR_SHA256
        or len(boundary.CASES) != 4
        or len(commands) != 4
        or any(
            "--bind-ip 192.168.10.5 --port 50000 "
            "--peer-ip 192.168.10.3 --peer-port 50001" not in command
            or "--frames 100 --chunk-gap-us 250 --hold-seconds 2 "
            "--handshake-timeout 15" not in command
            or "192.168.10.2" in command
            or "192.168.10.4" in command
            for command in commands
        )
        or EXPECTED_BOARD_APP_VERSION != "94dab8f-dirty"
        or EXPECTED_BOARD_ELF_SHA_PREFIX != "6d15ace8d"
        or DEFAULT_PROFILE != "final4"
        or PROFILE_NAMES != ("final4", "final2")
        or PROFILE_EPOCHS
        != {"final4": (2, 4, 6, 8), "final2": (132, 134, 136, 138)}
        or PROFILE_LIVE_FRAMES
        != {"final4": (1, 1, 2, 2), "final2": (1, 2, 2, 2)}
        or set(PROFILE_NORMALIZED_LOG_SHA256) != set(PROFILE_NAMES)
        or any(
            len(hashes) != 5
            or any(
                len(value) != 64
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in hashes
            )
            for hashes in PROFILE_NORMALIZED_LOG_SHA256.values()
        )
        or EXPECTED_SCALE_MAX_MV != (100.0, 100.0, 50.0, 100.0)
        or EXPECTED_COMPLETED != (100, 200, 300, 400)
        or PROFILE_SENDER_IDENTITIES
        != {
            "final4": (
                ("BA29D2A6", 0xF6A0FE13, 0x7D079E94),
                ("BA29D2A7", 0xDA7EB45D, 0x0C9EE0FA),
                ("BA29D2A8", 0x84B694CD, 0x93BC3A98),
                ("BA29D2A9", 0x7D56B4BA, 0xECDCFE01),
            ),
            "final2": (
                ("62CBA839", 0x38AB469E, 0x697B22C1),
                ("62CBA83A", 0xEB01E9C6, 0x7E087657),
                ("62CBA83B", 0xB19121D1, 0x55C9420A),
                ("62CBA83C", 0x5AA801B7, 0x34AB8D44),
            ),
        }
        or FREQUENCY_TOLERANCE_HZ != 1_000.0
        or VOLTAGE_TOLERANCE_MV != 5.0
        or MINIMUM_SENDER_DURATION_SECONDS != 5.0
        or MAXIMUM_SENDER_DURATION_SECONDS != 30.0
    ):
        raise RuntimeError("normal-v5 boundary evidence definition changed")


def validate_sender_log(
    case: boundary.MatrixCase,
    expected_identity: tuple[str, int, int],
    path: Path,
) -> SenderEvidence:
    text = _read_ascii(path, f"{case.case_id} sender log")
    lines = text.splitlines()
    expected_semantic = _expected_sender_lines(case)
    expected_command = boundary.replay_command(case)
    expected_line_count = 15 + len(case.tones)
    if (
        len(lines) != expected_line_count
        or lines[-2] != ""
        or any(not line for line in lines[:-2])
        or "192.168.10.2" in text
        or "192.168.10.4" in text
    ):
        raise RuntimeError(f"{case.case_id}: sender transcript shape changed")

    start = SCRIPT_START_PATTERN.fullmatch(lines[0])
    done = SCRIPT_DONE_PATTERN.fullmatch(lines[-1])
    if (
        start is None
        or done is None
        or start.group("command") != expected_command
        or done.group("exit_code") != "0"
    ):
        raise RuntimeError(f"{case.case_id}: complete command/exit changed")

    semantic_start = 1
    semantic_end = semantic_start + len(expected_semantic)
    if tuple(lines[semantic_start:semantic_end]) != expected_semantic:
        raise RuntimeError(f"{case.case_id}: synthetic truth/tone lines changed")

    hello_index = semantic_end
    config_index = hello_index + 1
    enable_index = hello_index + 2
    ready_index = hello_index + 3
    hello = HELLO_PATTERN.fullmatch(lines[hello_index])
    config = CONFIG_PATTERN.fullmatch(lines[config_index])
    enable = ENABLE_PATTERN.fullmatch(lines[enable_index])
    ready = SENDER_READY_PATTERN.fullmatch(lines[ready_index])
    if None in (hello, config, enable, ready):
        raise RuntimeError(f"{case.case_id}: sender control grammar changed")
    assert hello is not None
    assert config is not None
    assert enable is not None
    assert ready is not None

    expected = SenderIdentity(*expected_identity)
    actual = SenderIdentity(
        ready.group("session").upper(),
        int(ready.group("boot"), 16),
        int(ready.group("config"), 16),
    )
    hello_sequence = int(hello.group("seq"))
    if (
        actual != expected
        or hello.group("session").upper() != expected.session_id
        or config.group("config").upper() != f"{expected.config_id:08X}"
        or (hello.group("status"), config.group("status"), enable.group("status"))
        != ("0", "0", "0")
        or int(config.group("seq")) != (hello_sequence + 1) & 0xFFFFFFFF
        or int(enable.group("seq")) != (hello_sequence + 2) & 0xFFFFFFFF
    ):
        raise RuntimeError(f"{case.case_id}: sender session/boot/config changed")

    expected_progress = (
        "sent frame=1 packets=12",
        "sent frame=25 packets=300",
        "sent frame=50 packets=600",
        "sent frame=75 packets=900",
        "sent frame=100 packets=1200",
        "completed frames=100 wave_packets=1200",
    )
    progress_start = ready_index + 1
    progress_end = progress_start + len(expected_progress)
    if tuple(lines[progress_start:progress_end]) != expected_progress:
        raise RuntimeError(f"{case.case_id}: sender frame/packet counts changed")
    if progress_end != len(lines) - 2:
        raise RuntimeError(f"{case.case_id}: sender contains unbound extra lines")

    started = _parse_wall_time(start.group("timestamp"), case.case_id)
    completed = _parse_wall_time(done.group("timestamp"), case.case_id)
    duration = (completed - started).total_seconds()
    if not (
        MINIMUM_SENDER_DURATION_SECONDS
        <= duration
        <= MAXIMUM_SENDER_DURATION_SECONDS
    ):
        raise RuntimeError(f"{case.case_id}: sender wrapper duration changed")
    return SenderEvidence(actual, started, completed)


def validate_sender_logs(
    paths: tuple[Path, ...], profile: str = DEFAULT_PROFILE
) -> tuple[SenderEvidence, ...]:
    if profile not in PROFILE_NAMES:
        raise RuntimeError(f"unknown boundary evidence profile: {profile}")
    if len(paths) != 4:
        raise RuntimeError(f"expected four sender logs, got {len(paths)}")
    evidence = tuple(
        validate_sender_log(case, identity, path)
        for case, identity, path in zip(
            boundary.CASES,
            PROFILE_SENDER_IDENTITIES[profile],
            paths,
            strict=True,
        )
    )
    if any(
        current.started != previous.completed
        for previous, current in zip(evidence[:-1], evidence[1:], strict=True)
    ):
        raise RuntimeError("four sender wrappers are not one contiguous matrix run")
    return evidence


def _parse_measurement(match: re.Match[str]) -> Measurement:
    return Measurement(
        session_id=match.group("session").upper(),
        config_id=int(match.group("config"), 16),
        epoch=int(match.group("epoch")),
        frame_id=int(match.group("frame")),
        generation=int(match.group("generation")),
        fundamental_hz=float(match.group("f0")),
        vpp_mv=float(match.group("vpp")),
        rms_mv=float(match.group("rms")),
        peak_count=int(match.group("peaks")),
        tones=tuple(
            (
                float(match.group(f"p{index}f")),
                float(match.group(f"p{index}a")),
            )
            for index in range(1, 4)
        ),
        calibration_id=int(match.group("calibration")),
        test_pattern=int(match.group("test")),
    )


def _measurement_signature(measurement: Measurement) -> tuple[object, ...]:
    return (
        measurement.session_id,
        measurement.config_id,
        measurement.epoch,
        measurement.fundamental_hz,
        measurement.vpp_mv,
        measurement.rms_mv,
        measurement.peak_count,
        measurement.tones,
        measurement.calibration_id,
        measurement.test_pattern,
    )


def _validate_measurement(
    case: boundary.MatrixCase,
    sender: SenderIdentity,
    epoch: int,
    measurement: Measurement,
    expected_frame: int,
    expected_generation: int,
) -> tuple[float, float, float, float, float]:
    if (
        measurement.session_id != sender.session_id
        or measurement.config_id != sender.config_id
        or measurement.epoch != epoch
        or measurement.frame_id != expected_frame
        or measurement.generation != expected_generation
        or measurement.calibration_id != boundary.CALIBRATION_ID
        or measurement.test_pattern != 1
    ):
        raise RuntimeError(
            f"{case.case_id}: measurement identity/frame/generation changed"
        )

    expected_metrics = boundary.metrics(case)
    f0_error_hz = abs(measurement.fundamental_hz - case.fundamental_hz)
    vpp_error_mv = abs(
        measurement.vpp_mv - float(expected_metrics["ideal_vpp_mv"])
    )
    rms_error_mv = abs(
        measurement.rms_mv - float(expected_metrics["ideal_rms_mv"])
    )
    if (
        f0_error_hz > FREQUENCY_TOLERANCE_HZ
        or vpp_error_mv > VOLTAGE_TOLERANCE_MV
        or rms_error_mv > VOLTAGE_TOLERANCE_MV
    ):
        raise RuntimeError(
            f"{case.case_id}: F0/Vpp/RMS exceeds full.md tolerance: "
            f"{f0_error_hz:.3f}Hz/{vpp_error_mv:.3f}mV/"
            f"{rms_error_mv:.3f}mV"
        )

    expected_tones = tuple(
        sorted(
            (
                harmonic * case.fundamental_hz,
                amplitude * 1000.0,
            )
            for harmonic, amplitude, _ in case.tones
        )
    )
    if measurement.peak_count != len(expected_tones):
        raise RuntimeError(f"{case.case_id}: visible spectral-line count changed")
    observed_tones = tuple(sorted(measurement.tones[: measurement.peak_count]))
    if any(
        frequency != 0.0 or amplitude != 0.0
        for frequency, amplitude in measurement.tones[measurement.peak_count :]
    ):
        raise RuntimeError(f"{case.case_id}: unused spectral-line slot is nonzero")

    line_frequency_error_hz = 0.0
    line_amplitude_error_mv = 0.0
    for expected, observed in zip(expected_tones, observed_tones, strict=True):
        frequency_error = abs(observed[0] - expected[0])
        amplitude_error = abs(observed[1] - expected[1])
        line_frequency_error_hz = max(line_frequency_error_hz, frequency_error)
        line_amplitude_error_mv = max(line_amplitude_error_mv, amplitude_error)
        if (
            frequency_error > FREQUENCY_TOLERANCE_HZ
            or amplitude_error > VOLTAGE_TOLERANCE_MV
        ):
            raise RuntimeError(
                f"{case.case_id}: spectral line exceeds full.md tolerance: "
                f"{frequency_error:.3f}Hz/{amplitude_error:.3f}mV"
            )
    return (
        f0_error_hz,
        vpp_error_mv,
        rms_error_mv,
        line_frequency_error_hz,
        line_amplitude_error_mv,
    )


def _groups(
    matches: tuple[re.Match[str], ...], names: tuple[str, ...]
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(
            match.group(name)
            if name in ("tag", "selftest")
            else int(match.group(name))
            for name in names
        )
        for match in matches
    )


def _validate_health(
    text: str,
    profile: str,
) -> tuple[
    tuple[re.Match[str], ...],
    tuple[re.Match[str], ...],
    tuple[re.Match[str], ...],
    tuple[re.Match[str], ...],
    tuple[re.Match[str], ...],
]:
    rx = tuple(RX_HEALTH_PATTERN.finditer(text))
    frame = tuple(FRAME_HEALTH_PATTERN.finditer(text))
    reject = tuple(REJECT_HEALTH_PATTERN.finditer(text))
    socket = tuple(SOCKET_HEALTH_PATTERN.finditer(text))
    pipeline = tuple(PIPELINE_HEALTH_PATTERN.finditer(text))
    expected_rx = {
        "final4": ((3921, 0, 0, 0, 0, 0, 0),),
        "final2": (
            (4, 0, 0, 0, 0, 0, 0),
            (4756, 0, 0, 0, 0, 0, 0),
        ),
    }[profile]
    expected_frame = {
        "final4": ((322, 322, 0, 0, 0, 0, 0),),
        "final2": (
            (0, 0, 0, 0, 0, 0, 0),
            (391, 391, 0, 0, 0, 0, 0),
        ),
    }[profile]
    expected_reject = {
        "final4": ((0, 0, 0, 0, 3, 3),),
        "final2": (
            (0, 0, 0, 0, 195, 65),
            (0, 0, 0, 0, 195, 68),
        ),
    }[profile]
    expected_socket = {
        "final4": ((0, 0, 0, 4),),
        "final2": ((0, 0, 0, 1), (0, 0, 0, 4)),
    }[profile]
    expected_pipeline = {
        "final4": (
            (
                "cyclescope_fft",
                328,
                327,
                327,
                69,
                0,
                0,
                0,
                258,
                16172,
                15072,
                24628,
                "PASS",
                255,
                28126880,
            ),
        ),
        "final2": (
            (
                "cyclescope_fft",
                2,
                2,
                2,
                1,
                0,
                0,
                0,
                0,
                14352,
                16180,
                18008,
                "PASS",
                0,
                28125284,
            ),
            (
                "cyclescope_fft",
                393,
                392,
                392,
                82,
                0,
                0,
                0,
                310,
                16189,
                15320,
                23898,
                "PASS",
                260,
                28126884,
            ),
        ),
    }[profile]
    expected_count = len(expected_rx)
    if (
        tuple(
            text.count(marker)
            for marker in (
                "health/rx:",
                "health/frame:",
                "health/reject:",
                "health/socket:",
            )
        )
        != (expected_count,) * 4
        or tuple(map(len, (rx, frame, reject, socket)))
        != (expected_count,) * 4
        or text.count("cyclescope_fft: health:") != expected_count
        or text.count("cyclescope_pipe: health:") != 0
        or len(pipeline) != expected_count
    ):
        raise RuntimeError("visible receiver/pipeline health cardinality changed")

    if _groups(
        rx,
        ("packets", "source", "magic", "version", "length", "session", "crc"),
    ) != expected_rx:
        raise RuntimeError("receiver/rx counts or visible errors changed")
    if _groups(
        frame,
        (
            "completed",
            "acquired",
            "overwrite",
            "incomplete",
            "duplicate",
            "stale",
            "busy",
        ),
    ) != expected_frame:
        raise RuntimeError("receiver/frame counts or visible errors changed")
    if _groups(
        reject,
        ("config", "metadata", "overrange", "fifo", "retries", "reconnects"),
    ) != expected_reject:
        raise RuntimeError("receiver/reject counts or visible errors changed")
    if _groups(
        socket,
        ("open_fail", "recv_fatal", "close_fail", "sessions"),
    ) != expected_socket:
        raise RuntimeError("receiver/socket counts or visible errors changed")
    if _groups(
        pipeline,
        (
            "tag",
            "acquired",
            "analyzed",
            "published",
            "ui",
            "stale",
            "invalid",
            "failures",
            "ui_overwrite",
            "last_us",
            "average_us",
            "maximum_us",
            "selftest",
            "max_ui_gap",
            "free",
        ),
    ) != expected_pipeline:
        raise RuntimeError("pipeline counts, timings, or visible errors changed")
    return rx, frame, reject, socket, pipeline


def _validate_failure_word_lines(text: str, profile: str) -> None:
    failure_word = re.compile(
        r"\b(?:fail|failed|failure|fatal)\b", re.IGNORECASE
    )
    allowed = re.compile(
        r"^W \([0-9]+\) cslp_rx: CSLP session "
        r"0x(?P<session>[0-9A-Fa-f]{8}) handshake failed$"
    )
    lines = tuple(line for line in text.splitlines() if failure_word.search(line))
    expected_start, expected_count = {
        "final4": (int("BA29D2AA", 16), 27),
        "final2": (int("62CBA7F8", 16), 65),
    }[profile]
    if len(lines) != expected_count:
        raise RuntimeError(
            f"UART {profile} failure-word line cardinality changed: {len(lines)}"
        )
    for index, line in enumerate(lines):
        match = allowed.fullmatch(line)
        if (
            match is None
            or int(match.group("session"), 16) != expected_start + index
        ):
            raise RuntimeError(
                "UART contains a non-whitelisted failure/fatal word line"
            )


def _reject_global_fatal(text: str) -> None:
    fatal_patterns = (
        r"^E \(",
        r"\bFAIL\b",
        r"\bFATAL\b",
        r"Guru Meditation",
        r"\bpanic(?:ked|'ed)?\b",
        r"\babort(?:ed)?\b",
        r"\bassert(?:ion)?\b",
        r"(?:Task|Interrupt) watchdog",
        r"\bWDT\b",
        r"brownout",
        r"Stack canary",
        r"heap corruption",
        r"Backtrace",
        r"Measurement rejected",
        r"FFT (?:failed|failure)",
        r"Discarded stale",
        r"Unable to publish",
        r"selftest=FAIL",
    )
    for pattern in fatal_patterns:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(f"UART contains a global fatal marker: {pattern}")


def _validate_startup_sequence(text: str) -> None:
    specifications = (
        (
            "primary FFT startup self-test",
            "FFT startup self-test:",
            re.compile(
                r"^I \([0-9]+\) cyclescope_pipe: "
                r"FFT startup self-test:[^\n]*\bPASS$",
                re.MULTILINE,
            ),
        ),
        (
            "exact-weak FFT startup self-test",
            "FFT exact weak startup self-test:",
            re.compile(
                r"^I \([0-9]+\) cyclescope_pipe: "
                r"FFT exact weak startup self-test:[^\n]*\bPASS$",
                re.MULTILINE,
            ),
        ),
        (
            "formal pipeline preparation",
            "Formal CSLP FFT pipeline prepared;",
            re.compile(
                r"^I \([0-9]+\) cyclescope_pipe: "
                r"Formal CSLP FFT pipeline prepared; result=15744 bytes$",
                re.MULTILINE,
            ),
        ),
        (
            "analysis preparation",
            "Instrument analysis preparation:",
            re.compile(
                r"^I \([0-9]+\) cyclescope_ui: "
                r"Instrument analysis preparation: READY$",
                re.MULTILINE,
            ),
        ),
        (
            "receiver golden-ready gate",
            "CSLP v1 receiver ready on Core 1;",
            re.compile(
                r"^I \([0-9]+\) cslp_rx: CSLP v1 receiver ready on "
                r"Core 1; golden packet PASS$",
                re.MULTILINE,
            ),
        ),
        (
            "UI running gate",
            "Instrument UI started; formal CSLP FFT8192:",
            re.compile(
                r"^I \([0-9]+\) cyclescope_ui: Instrument UI started; "
                r"formal CSLP FFT8192: RUNNING$",
                re.MULTILINE,
            ),
        ),
    )
    markers: list[re.Match[str]] = []
    for label, raw_marker, pattern in specifications:
        matches = tuple(pattern.finditer(text))
        if text.count(raw_marker) != 1 or len(matches) != 1:
            raise RuntimeError(f"UART {label} uniqueness/PASS gate changed")
        markers.append(matches[0])
    first_board_ready = BOARD_READY_PATTERN.search(text)
    if first_board_ready is None:
        raise RuntimeError("UART has no first active board session")
    ordered = (*markers, first_board_ready)
    if any(
        right.start() <= left.end()
        for left, right in zip(ordered[:-1], ordered[1:], strict=True)
    ):
        raise RuntimeError(
            "UART startup gate order must precede the first active session"
        )


def _validate_image(text: str) -> None:
    _validate_startup_sequence(text)
    app_versions = re.findall(r"app_init: App version:\s+([^\n]+)", text)
    elf_hashes = re.findall(
        r"app_init: ELF file SHA256:\s+([0-9A-Fa-f]+)\.\.\.", text
    )
    route_matches = re.findall(
        r"^I \([0-9]+\) cslp_rx: UDP ([^\n]+)$", text, re.MULTILINE
    )
    if (
        text.count("ESP-ROM:esp32p4-eco2-20240710") != 1
        or text.count("rst:0x1 (POWERON)") != 1
        or text.count("app_init: Project name:     CycleScopeP4") != 1
        or app_versions != [EXPECTED_BOARD_APP_VERSION]
        or [value.lower() for value in elf_hashes]
        != [EXPECTED_BOARD_ELF_SHA_PREFIX]
        or text.count("CSLP v1 receiver ready on Core 1; golden packet PASS")
        != 1
        or not route_matches
        or set(route_matches)
        != {"192.168.10.3:50001 -> 192.168.10.5:50000"}
        or "192.168.10.2" in text
        or "192.168.10.4" in text
    ):
        raise RuntimeError("UART does not identify the isolated normal-v5 image")


def _validate_final4_post_stale_tail(
    text: str, final_stale: re.Match[str]
) -> None:
    tail = text[final_stale.end() :]
    if not tail.startswith("\n") or not tail.endswith("\n"):
        raise RuntimeError("final4 post-STALE observer tail shape changed")
    lines = tail[1:].splitlines()
    if len(lines) != 82:
        raise RuntimeError(
            f"final4 post-STALE observer tail line count changed: {len(lines)}"
        )
    route_pattern = re.compile(
        r"^I \([0-9]+\) cslp_rx: UDP "
        r"192\.168\.10\.3:50001 -> 192\.168\.10\.5:50000$"
    )
    timeout_pattern = re.compile(
        r"^W \([0-9]+\) cslp_rx: Control transaction 0x81 "
        r"seq=[0-9]+ timed out$"
    )
    failure_pattern = re.compile(
        r"^W \([0-9]+\) cslp_rx: CSLP session "
        r"0x(?P<session>[0-9A-Fa-f]{8}) handshake failed$"
    )
    if route_pattern.fullmatch(lines[0]) is None:
        raise RuntimeError("final4 post-STALE observer tail lost its first route")
    expected_session = int("BA29D2AA", 16)
    for retry_index in range(27):
        route = lines[retry_index * 3]
        timeout = lines[retry_index * 3 + 1]
        failure = lines[retry_index * 3 + 2]
        if (
            route_pattern.fullmatch(route) is None
            or timeout_pattern.fullmatch(timeout) is None
        ):
            raise RuntimeError("final4 post-STALE receiver retry grammar changed")
        match = failure_pattern.fullmatch(failure)
        if (
            match is None
            or int(match.group("session"), 16)
            != expected_session + retry_index
        ):
            raise RuntimeError("final4 post-STALE retry session sequence changed")
    if route_pattern.fullmatch(lines[-1]) is None:
        raise RuntimeError("final4 post-STALE observer tail lost its final route")


def validate_serial_log(
    path: Path,
    senders: tuple[SenderEvidence, ...],
    profile: str = DEFAULT_PROFILE,
) -> EvidenceSummary:
    if profile not in PROFILE_NAMES:
        raise RuntimeError(f"unknown boundary evidence profile: {profile}")
    epochs = PROFILE_EPOCHS[profile]
    live_frames = PROFILE_LIVE_FRAMES[profile]
    text = _read_ascii(path, "UART log")
    _validate_failure_word_lines(text, profile)
    _reject_global_fatal(text)
    _validate_image(text)
    timestamps = tuple(
        int(match.group(1))
        for match in re.finditer(r"^[IWE] \(([0-9]+)\)", text, re.MULTILINE)
    )
    if not timestamps or any(
        current < previous
        for previous, current in zip(
            timestamps[:-1], timestamps[1:], strict=True
        )
    ):
        raise RuntimeError("UART ESP-IDF timestamps are not monotonic")

    identities = tuple(sender.identity for sender in senders)
    ready = tuple(BOARD_READY_PATTERN.finditer(text))
    board_identities = tuple(
        SenderIdentity(
            match.group("session").upper(),
            int(match.group("boot")),
            int(match.group("config")),
        )
        for match in ready
    )
    if text.count("CSLP session ready:") != 4 or board_identities != identities:
        raise RuntimeError("UART session/boot/config does not match four senders")

    scales = tuple(SCALE_PATTERN.finditer(text))
    scale_values = tuple(
        (
            match.group("session").upper(),
            int(match.group("config"), 16),
            int(match.group("epoch")),
            int(match.group("frame")),
            float(match.group("previous")),
            float(match.group("amax")),
            match.group("reason"),
        )
        for match in scales
    )
    expected_scales = tuple(
        (
            identity.session_id,
            identity.config_id,
            epoch,
            1,
            0.0,
            amplitude_max,
            "NEW_STREAM",
        )
        for identity, epoch, amplitude_max in zip(
            identities,
            epochs,
            EXPECTED_SCALE_MAX_MV,
            strict=True,
        )
    )
    if (
        text.count("Spectrum scale committed:") != 4
        or scale_values != expected_scales
    ):
        raise RuntimeError("four NEW_STREAM scale commits changed")

    measurement_matches = tuple(MEASUREMENT_PATTERN.finditer(text))
    if (
        text.count("cyclescope_pipe: measurement:") != 8
        or len(measurement_matches) != 8
    ):
        raise RuntimeError("UART must contain exactly frame 1/100 measurements")
    measurements = tuple(_parse_measurement(match) for match in measurement_matches)
    expected_measurement_identity = tuple(
        (
            identity.session_id,
            identity.config_id,
            epoch,
            frame,
            case_index * 100 + frame,
        )
        for case_index, (identity, epoch) in enumerate(
            zip(identities, epochs, strict=True)
        )
        for frame in (1, 100)
    )
    actual_measurement_identity = tuple(
        (
            item.session_id,
            item.config_id,
            item.epoch,
            item.frame_id,
            item.generation,
        )
        for item in measurements
    )
    if actual_measurement_identity != expected_measurement_identity:
        raise RuntimeError("measurement session/frame/generation sequence changed")

    maximum_errors = [0.0] * 5
    for case_index, (case, identity, epoch) in enumerate(
        zip(boundary.CASES, identities, epochs, strict=True)
    ):
        first = measurements[2 * case_index]
        last = measurements[2 * case_index + 1]
        if _measurement_signature(first) != _measurement_signature(last):
            raise RuntimeError(f"{case.case_id}: frame 1/100 measurement drifted")
        for measurement, frame in zip((first, last), (1, 100), strict=True):
            errors = _validate_measurement(
                case,
                identity,
                epoch,
                measurement,
                frame,
                case_index * 100 + frame,
            )
            maximum_errors = [
                max(previous, current)
                for previous, current in zip(maximum_errors, errors, strict=True)
            ]

    publications = tuple(PUBLICATION_PATTERN.finditer(text))
    publication_values = tuple(
        (int(match.group("frame")), int(match.group("completed")))
        for match in publications
    )
    if text.count("cslp_rx: Published frame=") != 5 or publication_values != (
        (1, 1),
        (100, 100),
        (100, 200),
        (100, 300),
        (100, 400),
    ):
        raise RuntimeError("frame publication/cumulative completion changed")

    peer_silent = tuple(PEER_SILENT_PATTERN.finditer(text))
    states = tuple(UI_STATE_PATTERN.finditer(text))
    state_values = tuple(
        (
            match.group("level"),
            match.group("before"),
            match.group("after"),
            bool(match.group("last")),
            match.group("session").upper(),
            int(match.group("frame")),
            bool(match.group("retained")),
        )
        for match in states
    )
    expected_states_list = [
        (
            "I",
            "WAITING",
            "LIVE",
            False,
            identities[0].session_id,
            live_frames[0],
            False,
        )
    ]
    for case_index in range(3):
        expected_states_list.extend(
            (
                (
                    "W",
                    "LIVE",
                    "STALE",
                    True,
                    identities[case_index].session_id,
                    100,
                    True,
                ),
                (
                    "I",
                    "STALE",
                    "LIVE",
                    False,
                    identities[case_index + 1].session_id,
                    live_frames[case_index + 1],
                    False,
                ),
            )
        )
    if profile == "final4":
        expected_states_list.append(
            (
                "W",
                "LIVE",
                "STALE",
                True,
                identities[3].session_id,
                100,
                True,
            )
        )
    expected_states = tuple(expected_states_list)
    expected_peer_silent = 4 if profile == "final4" else 3
    if (
        text.count("CSLP peer silent for more than 1500 ms")
        != expected_peer_silent
        or len(peer_silent) != expected_peer_silent
        or text.count("CSLP UI stream state:") != len(expected_states)
        or state_values != expected_states
    ):
        raise RuntimeError(
            f"UART {profile} LIVE/STALE edge profile changed"
        )

    spectrum_ui = tuple(SPECTRUM_UI_PATTERN.finditer(text))
    if (
        text.count("Spectrum UI bridge") != 1
        or len(spectrum_ui) != 1
    ):
        raise RuntimeError("UART Spectrum UI bridge cardinality changed")
    ui = spectrum_ui[0]
    if (
        ui.group("session").upper() != identities[0].session_id
        or tuple(
            int(ui.group(name))
            for name in ("frame", "generation", "sentinel", "columns", "peaks")
        )
        != (1, 1, 255, 640, 2)
        or not math.isclose(
            float(ui.group("sample_rate")), 4.0625, abs_tol=0.000001
        )
        or not math.isclose(float(ui.group("axis")), 0.5, abs_tol=0.000001)
        or not math.isclose(float(ui.group("amax")), 100.0, abs_tol=0.000001)
        or not measurement_matches[0].end() < ui.start() < states[0].start()
    ):
        raise RuntimeError("first live Spectrum UI bridge changed")

    rx, frame_health, reject, socket, pipeline = _validate_health(text, profile)
    if profile == "final4":
        if not (
            states[6].end()
            < rx[0].start()
            < frame_health[0].start()
            < reject[0].start()
            < socket[0].start()
            < pipeline[0].start()
            < publications[4].start()
        ):
            raise RuntimeError("final4 receiver/pipeline health ordering changed")
    else:
        for matches in (rx, frame_health, reject, socket):
            if not (
                ready[0].end() < matches[0].start() < publications[0].start()
                and states[6].end()
                < matches[1].start()
                < publications[4].start()
            ):
                raise RuntimeError(
                    "final2 receiver health set moved outside its live interval"
                )
        if not (
            rx[0].start()
            < frame_health[0].start()
            < reject[0].start()
            < socket[0].start()
            and rx[1].start()
            < frame_health[1].start()
            < reject[1].start()
            < socket[1].start()
            and states[0].end() < pipeline[0].start() < publications[1].start()
            and socket[1].end() < pipeline[1].start() < publications[4].start()
        ):
            raise RuntimeError("final2 receiver/pipeline health ordering changed")

    for case_index in range(4):
        first_measurement = measurement_matches[2 * case_index]
        last_measurement = measurement_matches[2 * case_index + 1]
        live_state = states[0 if case_index == 0 else 2 * case_index]
        terminal_publication = publications[case_index + 1]
        if not (
            ready[case_index].end()
            < scales[case_index].start()
            < first_measurement.start()
            < live_state.start()
            < terminal_publication.start()
            < last_measurement.start()
        ):
            raise RuntimeError(
                f"case {case_index + 1}: ready/scale/live/frame100 order changed"
            )
        if case_index == 0 and not (
            ready[0].end() < publications[0].start() < scales[0].start()
        ):
            raise RuntimeError("case 1 frame-1 publication ordering changed")
        if case_index < 3 and not (
            last_measurement.end()
            < peer_silent[case_index].start()
            < states[2 * case_index + 1].start()
            < ready[case_index + 1].start()
        ):
            raise RuntimeError(
                f"case {case_index + 1}: inter-case stale boundary changed"
            )

    final_measurement = measurement_matches[-1]
    if profile == "final4":
        final_peer_silent = peer_silent[3]
        final_stale = states[7]
        if not (
            final_measurement.end()
            < final_peer_silent.start()
            < final_stale.start()
        ):
            raise RuntimeError("final4 terminal frame100/peer-silent/STALE order changed")
        _validate_final4_post_stale_tail(text, final_stale)
        formal_end = final_stale.end()
    else:
        if text[final_measurement.end() :] != "\n":
            raise RuntimeError(
                "final2 capture must end immediately after case-4 frame-100; "
                "final STALE is not part of this compatibility evidence"
            )
        formal_end = final_measurement.end()
    formal_window = text[ready[0].start() : formal_end]
    forbidden = (
        r"^E \(",
        r"Guru Meditation",
        r"\bpanic(?:ked|'ed)?\b",
        r"\babort(?:ed)?\b",
        r"\bassert(?:ion)?\b",
        r"(?:Task|Interrupt) watchdog",
        r"\bWDT\b",
        r"brownout",
        r"Stack canary",
        r"heap corruption",
        r"Backtrace",
        r"Measurement rejected",
        r"FFT (?:failed|failure)",
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
            raise RuntimeError(f"formal matrix window contains failure: {pattern}")

    return EvidenceSummary(
        profile=profile,
        identities=identities,
        max_f0_error_hz=maximum_errors[0],
        max_vpp_error_mv=maximum_errors[1],
        max_rms_error_mv=maximum_errors[2],
        max_line_frequency_error_hz=maximum_errors[3],
        max_line_amplitude_error_mv=maximum_errors[4],
        receiver_health_sets=len(rx),
        pipeline_health_snapshots=len(pipeline),
    )


def validate_evidence(
    sender_paths: tuple[Path, ...],
    serial_path: Path,
    profile: str = DEFAULT_PROFILE,
) -> EvidenceSummary:
    validate_definitions()
    senders = validate_sender_logs(sender_paths, profile)
    summary = validate_serial_log(serial_path, senders, profile)
    normalized_hashes = tuple(
        _normalized_log_sha256(path, f"{profile} sender #{index}")
        for index, path in enumerate(sender_paths, start=1)
    ) + (_normalized_log_sha256(serial_path, f"{profile} UART"),)
    if normalized_hashes != PROFILE_NORMALIZED_LOG_SHA256[profile]:
        raise RuntimeError(
            f"{profile} normalized sender/UART log SHA256 identity changed"
        )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument(
        "--profile", choices=PROFILE_NAMES, default=DEFAULT_PROFILE
    )
    parser.add_argument(
        "--sender-log",
        action="append",
        type=Path,
        help="repeat exactly four times in acceptance-matrix order",
    )
    parser.add_argument("--serial-log", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_definitions()
    if args.self_test_only:
        if args.sender_log is not None or args.serial_log is not None:
            raise SystemExit(
                "--self-test-only cannot be combined with log path arguments"
            )
        print("CycleScope normal-v5 boundary evidence definitions passed")
        return 0
    sender_paths = tuple(args.sender_log) if args.sender_log is not None else (
        PROFILE_SENDER_LOGS[args.profile]
    )
    serial_path = (
        args.serial_log
        if args.serial_log is not None
        else PROFILE_SERIAL_LOGS[args.profile]
    )
    summary = validate_evidence(sender_paths, serial_path, args.profile)
    sessions = ",".join(identity.session_id for identity in summary.identities)
    if summary.profile == "final4":
        terminal = (
            "terminal=case4-frame100/gen400->peer-silent->STALE; "
            "post_terminal_wait_retries=27(outside_matrix_window)"
        )
    else:
        terminal = (
            "terminal=case4-frame100/gen400-CAPTURE_END; "
            "final_peer_silent/STALE=NOT_CAPTURED/NOT_CLAIMED"
        )
    print(
        "CycleScope normal-v5 four-boundary evidence PASS: "
        f"profile={summary.profile}; "
        f"sessions={sessions}; sender=4x100frames/4800packets; "
        "measurements=8 frame/gen="
        "1/1,100/100;1/101,100/200;1/201,100/300;1/301,100/400 "
        "completed=100,200,300,400; "
        "scale_NEW_STREAM=100/100/50/100mVpk; "
        "inter_case=3x(LIVE->STALE->LIVE); "
        "visible_health_error_fields=0 "
        f"(receiver_sets={summary.receiver_health_sets},"
        f"pipeline={summary.pipeline_health_snapshots}); "
        "canonical_logs=SHA256_PASS; "
        "max_error(F0/Vpp/RMS/line_f/line_A)="
        f"{summary.max_f0_error_hz:.3f}Hz/"
        f"{summary.max_vpp_error_mv:.3f}mV/"
        f"{summary.max_rms_error_mv:.3f}mV/"
        f"{summary.max_line_frequency_error_hz:.3f}Hz/"
        f"{summary.max_line_amplitude_error_mv:.3f}mV; "
        f"{terminal}; "
        "path=.5:50000->.3:50001; physical_input/FPGA/panel_flush=UNPROVEN"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
