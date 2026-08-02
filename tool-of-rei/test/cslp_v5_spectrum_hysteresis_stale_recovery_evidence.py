#!/usr/bin/env python3
"""Validate the matched normal-v5 hysteresis/stale-recovery evidence.

This fixture is deliberately read-only: it reads two sender transcripts and
one UART transcript, opens no serial port or socket, starts no subprocess, and
writes no result file.  A PASS proves the fixed PC-synthetic 80-frame spectrum
scale schedule followed by a 20-frame new-session recovery.  The short runs
contain no periodic health snapshots, so PASS explicitly makes no health-
counter claim.  Physical input, FPGA, ADC, BNC, and panel-flush behavior remain
outside this evidence boundary.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re


DEFAULT_DYNAMIC_SENDER_LOG = Path(
    "/tmp/cyclescope-p4-v5-hysteresis-emulator.log"
)
DEFAULT_RECOVERY_SENDER_LOG = Path(
    "/tmp/cyclescope-p4-v5-hysteresis-recovery-emulator.log"
)
DEFAULT_SERIAL_LOG = Path(
    "/tmp/cyclescope-p4-v5-hysteresis-stale-recovery-serial.log"
)

EXPECTED_BOARD_APP_VERSION = "94dab8f-dirty"
EXPECTED_BOARD_ELF_SHA_PREFIX = "6d15ace8d"

EXPECTED_DYNAMIC_COMMAND = " ".join(
    (
        "python3",
        "tool-of-rei/test/cslp_spectrum_hysteresis_sender.py",
        "--chunk-gap-us 250",
        "--handshake-timeout 15",
    )
)
EXPECTED_RECOVERY_COMMAND = " ".join(
    (
        "python3",
        "ESP32-P4/tools/cslp_fpga_emulator.py",
        "--bind-ip 192.168.10.5",
        "--port 50000",
        "--peer-ip 192.168.10.3",
        "--peer-port 50001",
        "--frames 20",
        "--chunk-gap-us 250",
        "--hold-seconds 2",
        "--handshake-timeout 15",
        "--scenario normal",
    )
)

DYNAMIC_IDENTITY = ("E26255F1", 0xBA7090E9, 0xD4E8EAB6)
RECOVERY_IDENTITY = ("E26255F4", 0x6407AF8E, 0x77803D15)

DYNAMIC_STATIC_LINES = (
    "fixed route 192.168.10.5:50000 -> 192.168.10.3:50001; "
    "F0=40750.000Hz H1=25.0mVpk@0.17rad H4=10.0mVpk@-0.51rad",
    "stage=threshold-alternation frames=1-40 expected_Amax=100mVpk "
    "truth: H3=83.2mVpk@0.92rad Vpp=198.625452mV RMS=61.835427mV | "
    "H3=83.5mVpk@0.92rad Vpp=199.220596mV RMS=62.037287mV",
    "stage=upshift frames=41-50 expected_Amax=200mVpk truth: "
    "H3=90.0mVpk@0.92rad Vpp=212.122341mV RMS=66.426651mV",
    "stage=upper-hold frames=51-60 expected_Amax=200mVpk truth: "
    "H3=83.5mVpk@0.92rad Vpp=199.220596mV RMS=62.037287mV",
    "stage=downshift frames=61-80 expected_Amax=100mVpk truth: "
    "H3=79.0mVpk@0.92rad Vpp=190.296747mV RMS=59.016947mV",
    "listening on 192.168.10.5:50000; expecting 192.168.10.3:50001; "
    "frames=80 chunk_gap_us=250 hold_seconds=2.0",
    "sent stage=threshold-alternation through frame=40 wave_packets=480",
    "sent stage=upshift through frame=50 wave_packets=600",
    "sent stage=upper-hold through frame=60 wave_packets=720",
    "sent stage=downshift through frame=80 wave_packets=960",
    "completed frames=80 wave_packets=960 expected_scale=100->200->100 "
    "stateless_first40=100/200-jitter",
)

RECOVERY_STATIC_LINES = (
    "synthetic multitone: F0=40750.000000Hz Vpp=181.421109mV "
    "RMS=55.452683mV scale=100uV/LSB offset=500uV calibration_id=1",
    "  H1: f=40750.000000Hz A=25.000000mVpk phase=0.170000rad",
    "  H3: f=122250.000000Hz A=70.000000mVpk phase=0.920000rad",
    "  H4: f=163000.000000Hz A=25.000000mVpk phase=-0.510000rad",
    "listening on 192.168.10.5:50000; expecting 192.168.10.3:50001",
    "sent frame=1 packets=12",
    "sent frame=20 packets=240",
    "completed frames=20 wave_packets=240",
)

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
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
MEASUREMENT_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_pipe: measurement: "
    r"session=(?P<session>[0-9A-Fa-f]{8}) "
    r"config=(?P<config>[0-9A-Fa-f]{8}) epoch=(?P<epoch>[0-9]+) "
    r"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    r"F0=(?P<f0>[0-9]+\.[0-9]+)Hz Vpp=(?P<vpp>[0-9]+\.[0-9]+)mV "
    r"RMS=(?P<rms>[0-9]+\.[0-9]+)mV peaks=(?P<peaks>[0-9]+) "
    r"P1=(?P<p1f>[0-9]+\.[0-9]+)Hz/(?P<p1a>[0-9]+\.[0-9]+)mVpk "
    r"P2=(?P<p2f>[0-9]+\.[0-9]+)Hz/(?P<p2a>[0-9]+\.[0-9]+)mVpk "
    r"P3=(?P<p3f>[0-9]+\.[0-9]+)Hz/(?P<p3a>[0-9]+\.[0-9]+)mVpk "
    r"cal=(?P<calibration>[0-9]+) test=(?P<test>[0-9]+)$",
    re.MULTILINE,
)
PUBLICATION_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cslp_rx: "
    r"Published frame=(?P<frame>[0-9]+) completed=(?P<completed>[0-9]+)$",
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
SPECTRUM_UI_PATTERN = re.compile(
    r"^I \((?P<timestamp>[0-9]+)\) cyclescope_ui: "
    r"Spectrum UI bridge on Core 0: session=(?P<session>[0-9A-Fa-f]{8}) "
    r"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    r"A/B=(?P<sentinel>[0-9]+) columns=(?P<columns>[0-9]+) "
    r"peaks=(?P<peaks>[0-9]+) Fs=(?P<sample_rate>[0-9]+\.[0-9]+)MHz "
    r"axis=(?P<axis>[0-9]+\.[0-9]+)MHz "
    r"Amax=(?P<amax>[0-9]+\.[0-9]+)mVpk$",
    re.MULTILINE,
)
PEER_SILENT_PATTERN = re.compile(
    r"^W \((?P<timestamp>[0-9]+)\) cslp_rx: "
    r"CSLP peer silent for more than 1500 ms; starting a new session$",
    re.MULTILINE,
)
PERIODIC_HEALTH_PATTERN = re.compile(
    r"\bhealth(?:/[A-Za-z_]+)?:", re.IGNORECASE
)


@dataclass(frozen=True)
class SenderIdentity:
    session_id: str
    boot_id: int
    config_id: int


@dataclass(frozen=True)
class EvidenceSummary:
    dynamic: SenderIdentity
    recovery: SenderIdentity
    scale_commits: int
    ui_edges: int
    measurements: int
    periodic_health_records: int


def _read_ascii(path: Path, label: str) -> str:
    try:
        text = path.read_bytes().decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{label} log is not strict ASCII") from error
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text or ANSI_ESCAPE_PATTERN.search(text):
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


def _validate_wrapper(
    text: str, command: str, expected_nonempty_lines: int, label: str
) -> tuple[re.Match[str], re.Match[str]]:
    start = _exactly_one(SCRIPT_START_PATTERN, text, f"{label} script start")
    done = _exactly_one(SCRIPT_DONE_PATTERN, text, f"{label} script completion")
    lines = text.splitlines()
    if (
        start.start() != 0
        or start.group("command") != command
        or done.group("exit_code") != "0"
        or not start.end() < done.start()
        or not text.rstrip().endswith(done.group(0))
        or len([line for line in lines if line]) != expected_nonempty_lines
        or len(lines) < 2
        or lines[-2] != ""
        or any(not line for line in lines[:-2])
    ):
        raise RuntimeError(f"{label} wrapper command/exit/shape changed")
    return start, done


def _validate_control(
    text: str,
    expected: tuple[str, int, int],
    before: re.Match[str],
    after: re.Match[str],
    label: str,
) -> tuple[SenderIdentity, tuple[re.Match[str], ...]]:
    hello = _exactly_one(HELLO_PATTERN, text, f"{label} HELLO")
    config = _exactly_one(CONFIG_PATTERN, text, f"{label} CONFIG_SET")
    enable = _exactly_one(ENABLE_PATTERN, text, f"{label} ENABLE_PUSH")
    ready = _exactly_one(SENDER_READY_PATTERN, text, f"{label} session-ready")
    identity = SenderIdentity(
        ready.group("session").upper(),
        int(ready.group("boot"), 16),
        int(ready.group("config"), 16),
    )
    hello_sequence = int(hello.group("seq"))
    if (
        identity != SenderIdentity(*expected)
        or hello.group("session").upper() != identity.session_id
        or config.group("config").upper() != f"{identity.config_id:08X}"
        or (hello.group("status"), config.group("status"), enable.group("status"))
        != ("0", "0", "0")
        or int(config.group("seq")) != (hello_sequence + 1) & 0xFFFFFFFF
        or int(enable.group("seq")) != (hello_sequence + 2) & 0xFFFFFFFF
        or not (
            before.end()
            < hello.start()
            < config.start()
            < enable.start()
            < ready.start()
            < after.start()
        )
    ):
        raise RuntimeError(f"{label} control/session identity or ordering changed")
    return identity, (hello, config, enable, ready)


def validate_definitions() -> None:
    if (
        EXPECTED_BOARD_APP_VERSION != "94dab8f-dirty"
        or EXPECTED_BOARD_ELF_SHA_PREFIX != "6d15ace8d"
        or DYNAMIC_IDENTITY != ("E26255F1", 0xBA7090E9, 0xD4E8EAB6)
        or RECOVERY_IDENTITY != ("E26255F4", 0x6407AF8E, 0x77803D15)
        or len(DYNAMIC_STATIC_LINES) != 11
        or len(RECOVERY_STATIC_LINES) != 8
    ):
        raise RuntimeError("normal-v5 evidence identity/profile changed")


def validate_dynamic_sender_log(path: Path) -> SenderIdentity:
    text = _read_ascii(path, "dynamic sender")
    start, done = _validate_wrapper(
        text, EXPECTED_DYNAMIC_COMMAND, 17, "dynamic sender"
    )
    static = tuple(
        _exact_line(text, line, f"dynamic sender semantic line {index}")
        for index, line in enumerate(DYNAMIC_STATIC_LINES, start=1)
    )
    if any(right.start() <= left.start() for left, right in zip(static, static[1:])):
        raise RuntimeError("dynamic sender truth/stage lines are out of order")
    identity, control = _validate_control(
        text, DYNAMIC_IDENTITY, static[5], static[6], "dynamic sender"
    )
    if not (
        start.end() < static[0].start()
        and static[5].end() < control[0].start()
        and control[-1].end() < static[6].start()
        and static[-1].end() < done.start()
    ):
        raise RuntimeError("dynamic sender wrapper/truth/completion order changed")
    return identity


def validate_recovery_sender_log(path: Path) -> SenderIdentity:
    text = _read_ascii(path, "recovery sender")
    start, done = _validate_wrapper(
        text, EXPECTED_RECOVERY_COMMAND, 14, "recovery sender"
    )
    static = tuple(
        _exact_line(text, line, f"recovery sender semantic line {index}")
        for index, line in enumerate(RECOVERY_STATIC_LINES, start=1)
    )
    if any(right.start() <= left.start() for left, right in zip(static, static[1:])):
        raise RuntimeError("recovery sender truth/frame lines are out of order")
    identity, control = _validate_control(
        text, RECOVERY_IDENTITY, static[4], static[5], "recovery sender"
    )
    if not (
        start.end() < static[0].start()
        and static[4].end() < control[0].start()
        and control[-1].end() < static[5].start()
        and static[-1].end() < done.start()
    ):
        raise RuntimeError("recovery sender wrapper/truth/completion order changed")
    return identity


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
        or text.count("UDP 192.168.10.3:50001 -> 192.168.10.5:50000") < 1
        or "192.168.10.2" in text
        or "192.168.10.4" in text
        or re.search(r"FFT exact weak startup self-test:[^\n]*\bPASS\b", text)
        is None
    ):
        raise RuntimeError("UART does not identify the expected normal-v5 image")


def _measurement_tuple(match: re.Match[str]) -> tuple[str, ...]:
    names = (
        "session",
        "config",
        "epoch",
        "frame",
        "generation",
        "f0",
        "vpp",
        "rms",
        "peaks",
        "p1f",
        "p1a",
        "p2f",
        "p2a",
        "p3f",
        "p3a",
        "calibration",
        "test",
    )
    values = tuple(match.group(name) for name in names)
    return (values[0].upper(), values[1].upper(), *values[2:])


def _reject_forbidden_uart(text: str) -> None:
    forbidden = (
        r"^E \(",
        r"Measurement rejected",
        r"\bFFT\s+(?:fail|failed|failure)\b",
        r"\bfft_fail=[1-9][0-9]*\b",
        r"(?:Task|Interrupt) watchdog",
        r"\bWDT\b",
        r"Guru Meditation",
        r"\bpanic(?:ked|'ed)?\b",
        r"\bassert(?:ion)?\b",
        r"\babort(?:ed)?\b",
        r"Backtrace",
        r"Stack canary",
        r"brownout",
        r"\bheap\b[^\n]{0,48}\b(?:error|fail(?:ed|ure)?|corrupt(?:ed|ion)?|poison(?:ed|ing)?)\b",
        r"\b(?:error|fail(?:ed|ure)?|corrupt(?:ed|ion)?)\b[^\n]{0,48}\bheap\b",
    )
    for pattern in forbidden:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            raise RuntimeError(f"UART contains forbidden failure marker: {pattern}")


def validate_serial_log(
    path: Path, dynamic: SenderIdentity, recovery: SenderIdentity
) -> EvidenceSummary:
    text = _read_ascii(path, "UART")
    _validate_image_identity(text)
    _reject_forbidden_uart(text)

    timestamps = tuple(
        int(match.group(1))
        for match in re.finditer(r"^[IWE] \(([0-9]+)\)", text, re.MULTILINE)
    )
    if not timestamps or any(
        current < previous
        for previous, current in zip(timestamps, timestamps[1:])
    ):
        raise RuntimeError("UART timestamps are not monotonic")

    ready = tuple(BOARD_READY_PATTERN.finditer(text))
    if text.count("CSLP session ready:") != 2 or len(ready) != 2:
        raise RuntimeError("UART must contain exactly two board session-ready lines")
    board_identities = tuple(
        SenderIdentity(
            match.group("session").upper(),
            int(match.group("boot")),
            int(match.group("config")),
        )
        for match in ready
    )
    if board_identities != (dynamic, recovery):
        raise RuntimeError(
            "UART session/boot/config identities do not match both sender logs"
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
    expected_scales = (
        ("E26255F1", "D4E8EAB6", "18", "1", "0.0", "100.0", "NEW_STREAM"),
        ("E26255F1", "D4E8EAB6", "18", "41", "100.0", "200.0", "UPSHIFT"),
        ("E26255F1", "D4E8EAB6", "18", "61", "200.0", "100.0", "DOWNSHIFT"),
        ("E26255F4", "77803D15", "24", "1", "0.0", "100.0", "NEW_STREAM"),
    )
    if (
        text.count("Spectrum scale committed:") != 4
        or scale_values != expected_scales
    ):
        raise RuntimeError(
            "scale commits are not exactly NEW@1, UP@41, DOWN@61, NEW@1; "
            "the first 40 frames must contain no extra 100/200 jitter"
        )

    measurements = tuple(MEASUREMENT_PATTERN.finditer(text))
    expected_measurements = (
        (
            "E26255F1", "D4E8EAB6", "18", "1", "1", "40750.00",
            "198.621", "61.835", "3", "40750.00", "25.000",
            "122249.98", "83.199", "162999.98", "10.000", "1", "1",
        ),
        (
            "E26255F4", "77803D15", "24", "1", "81", "40750.00",
            "181.418", "55.452", "3", "40750.00", "25.000",
            "122249.98", "70.000", "162999.98", "24.999", "1", "1",
        ),
        (
            "E26255F4", "77803D15", "24", "20", "100", "40750.00",
            "181.418", "55.452", "3", "40750.00", "25.000",
            "122249.98", "70.000", "162999.98", "24.999", "1", "1",
        ),
    )
    if (
        text.count("cyclescope_pipe: measurement:") != 3
        or tuple(_measurement_tuple(match) for match in measurements)
        != expected_measurements
    ):
        raise RuntimeError("UART measurement identity/generation/values changed")

    publications = tuple(PUBLICATION_PATTERN.finditer(text))
    publication_values = tuple(
        (match.group("frame"), match.group("completed"))
        for match in publications
    )
    if (
        text.count("cslp_rx: Published frame=") != 2
        or publication_values != (("1", "1"), ("20", "100"))
    ):
        raise RuntimeError(
            "UART publication checkpoints must be frame1/completed1 and "
            "frame20/completed100"
        )

    ui_edges = tuple(UI_STATE_PATTERN.finditer(text))
    edge_values = tuple(
        (
            match.group("level"),
            match.group("before"),
            match.group("after"),
            bool(match.group("last")),
            match.group("session").upper(),
            match.group("frame"),
            bool(match.group("retained")),
        )
        for match in ui_edges
    )
    expected_edges = (
        ("I", "WAITING", "LIVE", False, "E26255F1", "4", False),
        ("W", "LIVE", "STALE", True, "E26255F1", "80", True),
        ("I", "STALE", "LIVE", False, "E26255F4", "1", False),
        ("W", "LIVE", "STALE", True, "E26255F4", "20", True),
    )
    if text.count("CSLP UI stream state:") != 4 or edge_values != expected_edges:
        raise RuntimeError("UART WAITING/LIVE/STALE edge contract changed")

    ui = _exactly_one(SPECTRUM_UI_PATTERN, text, "Spectrum UI bridge")
    ui_values = (
        ui.group("session").upper(),
        ui.group("frame"),
        ui.group("generation"),
        ui.group("sentinel"),
        ui.group("columns"),
        ui.group("peaks"),
        ui.group("sample_rate"),
        ui.group("axis"),
        ui.group("amax"),
    )
    if ui_values != (
        "E26255F1", "4", "4", "255", "640", "3", "4.0625",
        "0.50000", "100.0",
    ):
        raise RuntimeError("UART first-session Spectrum UI bridge changed")

    peer_silent = tuple(PEER_SILENT_PATTERN.finditer(text))
    if len(peer_silent) != 2:
        raise RuntimeError("UART must contain exactly two peer-silent boundaries")

    ordered_markers = (
        ready[0],
        publications[0],
        scales[0],
        measurements[0],
        ui,
        ui_edges[0],
        scales[1],
        scales[2],
        peer_silent[0],
        ui_edges[1],
        ready[1],
        scales[3],
        measurements[1],
        ui_edges[2],
        publications[1],
        measurements[2],
        peer_silent[1],
        ui_edges[3],
    )
    if any(
        right.start() <= left.start()
        for left, right in zip(ordered_markers, ordered_markers[1:])
    ):
        raise RuntimeError(
            "UART lifecycle order changed or STALE recovered directly on session-ready"
        )

    health_count = len(tuple(PERIODIC_HEALTH_PATTERN.finditer(text)))
    if health_count != 0:
        raise RuntimeError(
            "short v5 capture changed its no-periodic-health profile: "
            f"count={health_count}"
        )

    return EvidenceSummary(
        dynamic,
        recovery,
        len(scales),
        len(ui_edges),
        len(measurements),
        health_count,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dynamic-sender-log", type=Path, default=DEFAULT_DYNAMIC_SENDER_LOG
    )
    parser.add_argument(
        "--recovery-sender-log", type=Path, default=DEFAULT_RECOVERY_SENDER_LOG
    )
    parser.add_argument("--serial-log", type=Path, default=DEFAULT_SERIAL_LOG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_definitions()
    dynamic = validate_dynamic_sender_log(args.dynamic_sender_log)
    recovery = validate_recovery_sender_log(args.recovery_sender_log)
    summary = validate_serial_log(args.serial_log, dynamic, recovery)
    print(
        "normal-v5 spectrum-hysteresis/stale-recovery evidence PASS: "
        f"dynamic={summary.dynamic.session_id}/boot=0x{summary.dynamic.boot_id:08X}/"
        f"config=0x{summary.dynamic.config_id:08X}/80frames/960packets; "
        f"recovery={summary.recovery.session_id}/boot=0x{summary.recovery.boot_id:08X}/"
        f"config=0x{summary.recovery.config_id:08X}/20frames/240packets; "
        "scale=100@1->200@41->100@61,new-session=100@1; "
        "ui=WAITING->LIVE->STALE->LIVE->STALE; "
        "terminal=frame20/gen100/completed100; "
        f"periodic_health=NOT_CLAIMED(count={summary.periodic_health_records}); "
        "physical_input/FPGA/ADC/BNC/panel_flush=UNPROVEN"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
