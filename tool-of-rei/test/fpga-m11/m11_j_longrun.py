#!/usr/bin/env python3
"""Fail-closed M11-J response-time and 10,001-frame long-run gates.

The live coordinator is intentionally added only after M11-I has a passing
seven-point summary.  Pure helpers in this module freeze the response, frame
count, long-run stimulus, progress-window, and gain-drift contracts used by
that coordinator.
"""

# ruff: noqa: E402 -- adjacent M11 modules establish the WaveBench source path.

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m11_arb_point as arb
import m11_calibration as calibration
import m11_repeat_arb_driver as repeat_arb
import m11_sine_point as sine
import m11_wavebench_safe as safety

from wavebench.logging import CommandLogger
from wavebench.services.power_service import PowerService
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService
from wavebench.services.source_service import SourceService


EVIDENCE_ROOT = safety.EVIDENCE_ROOT
H_SUMMARY = EVIDENCE_ROOT / "offline" / "combination-summary-v1" / "summary.json"
I_SUMMARY = EVIDENCE_ROOT / "offline" / "upper-frequency-summary-v1" / "summary.json"
J_FINAL_SUMMARY_DIR = EVIDENCE_ROOT / "offline" / "j-longrun-final-v1"
CALIBRATION_MANIFEST = (
    EVIDENCE_ROOT / "offline" / "calibration-v1" / "calibration-build-manifest.json"
)
J_LIVE_ACK = "M11_STAGE_J_RESPONSE_AND_10001_FRAME_LONGRUN"
REQUESTED_FRAMES = 10_000
EXPECTED_FRAMES = 10_001
PACKETS_PER_FRAME = 12
EXPECTED_WAVE_PACKETS = EXPECTED_FRAMES * PACKETS_PER_FRAME
RESPONSE_LIMIT_US = 250_000.0
GAIN_DRIFT_LIMIT_DB = 0.05
GAIN_BLOCK_FRAMES = 1_000
PROGRESS_CAPTURE_THRESHOLDS = (100, 5_000, 9_800)
PROGRESS_PATTERN = re.compile(r"\bPROGRESS frames=(\d+)\b")
PROGRESS_EVERY = 100
LONGRUN_TIMEOUT_S = 540.0
GEM_TCL = SCRIPT_DIR / "m11_gem_readonly.tcl"
XSDB = Path("/tools/Xilinx/2025.1/Vitis/bin/xsdb")
HW_SERVER = Path("/tools/Xilinx/2025.1/Vitis/bin/hw_server")
HW_SERVER_URL = "tcp:127.0.0.1:3121"
GEM_REGISTER_NAMES = {
    0xF8000140: "SLCR_GEM0_CLK_CTRL",
    0xE000B004: "GEM0_NWCFG",
    0xE000B108: "TX_FRAMES",
    0xE000B134: "TX_UNDERRUN",
    0xE000B138: "TX_SINGLE_COLLISION",
    0xE000B13C: "TX_MULTIPLE_COLLISION",
    0xE000B140: "TX_EXCESS_COLLISION",
    0xE000B144: "TX_LATE_COLLISION",
    0xE000B148: "TX_DEFERRED",
    0xE000B14C: "TX_CARRIER_SENSE",
    0xE000B184: "RX_UNDERSIZE",
    0xE000B188: "RX_OVERSIZE",
    0xE000B18C: "RX_JABBER",
    0xE000B190: "RX_FCS",
    0xE000B194: "RX_LENGTH",
    0xE000B198: "RX_SYMBOL",
    0xE000B19C: "RX_ALIGN",
    0xE000B1A0: "RX_RESOURCE",
    0xE000B1A4: "RX_OVERRUN",
    0xE000B1A8: "RX_IP_CHECKSUM",
    0xE000B1AC: "RX_TCP_CHECKSUM",
    0xE000B1B0: "RX_UDP_CHECKSUM",
}
GEM_ERROR_ADDRESSES = set(GEM_REGISTER_NAMES) - {
    0xF8000140,
    0xE000B004,
    0xE000B108,
}
GEM_LINE_PATTERN = re.compile(r"^\s*([0-9A-Fa-f]{8}):\s+([0-9A-Fa-f]{8})\s*$", re.MULTILINE)


class M11JError(RuntimeError):
    """M11-J evidence is incomplete, stale, ambiguous, or unsafe."""


EXPECTED_PREOUTPUT_FAILURES = {
    "LAN acquisition did not pass",
    "response did not pass",
    "exact long-run did not pass",
    "ADC recovery did not pass",
    "gain drift did not pass",
    "GEM delta did not pass",
    "three progress-bound scope windows are incomplete",
    "complete pcap source_data archive did not pass",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise M11JError(f"cannot read JSON evidence: {path}") from error
    if not isinstance(value, dict):
        raise M11JError(f"JSON object required: {path}")
    return value


def upper_frequency_gate(path: Path = I_SUMMARY) -> dict[str, Any]:
    if not path.is_file():
        raise M11JError("M11-I seven-point summary is missing")
    summary = load_json(path)
    failures: list[str] = []
    if summary.get("pass") is not True:
        failures.append("M11-I hard acceptance did not pass")
    if summary.get("target_pass") is not True:
        failures.append("M11-I 3 mV target did not pass")
    if int(summary.get("point_count", -1)) != 7:
        failures.append("M11-I summary must contain exactly seven points")
    if int(summary.get("sine_point_count", -1)) != 5:
        failures.append("M11-I summary must contain five sine points")
    if int(summary.get("arb_point_count", -1)) != 2:
        failures.append("M11-I summary must contain two combination points")
    minimum_attenuation = summary.get("minimum_attenuation_lower_bound_db")
    if (
        not isinstance(minimum_attenuation, (int, float))
        or not math.isfinite(float(minimum_attenuation))
        or float(minimum_attenuation) < 50.0
    ):
        failures.append("M11-I minimum attenuation is below 50 dB or missing")
    if failures:
        raise M11JError("; ".join(failures))
    return {
        "path": str(path.resolve()),
        "sha256": safety.sha256_file(path),
        "point_count": 7,
        "minimum_attenuation_lower_bound_db": float(minimum_attenuation),
        "pass": True,
    }


def select_longrun_case(path: Path = H_SUMMARY) -> dict[str, Any]:
    summary = load_json(path)
    if summary.get("pass") is not True or summary.get("target_pass") is not True:
        raise M11JError("M11-H summary is not fully passing")
    records = summary.get("records")
    if not isinstance(records, list):
        raise M11JError("M11-H summary records are missing")
    candidates = [
        item
        for item in records
        if isinstance(item, dict)
        and str(item.get("case_id", "")).startswith("h-b-edge-j-")
        and item.get("target_pass") is True
    ]
    if len(candidates) != 5:
        raise M11JError("M11-H must contain five passing B-edge combinations")
    candidate_frequencies = {float(item["u_j_frequency_hz"]) for item in candidates}
    if candidate_frequencies != {1e6, 1.5e6, 2e6, 2.5e6, 3e6}:
        raise M11JError("M11-H B-edge frequency grid changed")
    selected_summary = max(
        candidates,
        key=lambda item: (
            float(item["source_vpp_v"]),
            float(item.get("intermod_residual_peak_v_p95", 0.0)),
        ),
    )
    case_id = str(selected_summary["case_id"])
    record = arb.load_arb_case(case_id)
    if (
        record.get("stage") != "H"
        or record.get("u_b_source_case") != "g-b-edge-high-crest"
        or not 0.0 < float(record["source_vpp_v"]) <= 0.45
        or float(record.get("u_j_frequency_hz", 0.0)) != 3e6
    ):
        raise M11JError("selected long-run ARB is not the frozen safe B-edge case")
    return {
        "selection_policy": (
            "among the five passing H B-edge high-crest combinations, choose the "
            "largest measured total source Vpp; tie-break on residual intermod"
        ),
        "summary_path": str(path.resolve()),
        "summary_sha256": safety.sha256_file(path),
        "case_id": case_id,
        "source_vpp_v": float(record["source_vpp_v"]),
        "u_j_frequency_hz": float(record["u_j_frequency_hz"]),
        "waveform_path": record["waveform_path"],
        "waveform_sha256": record["sha256"],
        "record": record,
        "pass": True,
    }


def validate_preoutput_resume(
    point_dir: Path,
    selection: dict[str, Any],
    current_preflight: dict[str, Any],
) -> dict[str, Any]:
    """Bind a resume to the one J attempt that failed before source output ON."""

    point_dir = point_dir.resolve()
    try:
        point_dir.relative_to((EVIDENCE_ROOT / "points").resolve())
    except ValueError as error:
        raise M11JError("J pre-output resume point escapes the M11 points root") from error
    if not point_dir.name.endswith("_j-response-longrun"):
        raise M11JError("J pre-output resume point has the wrong directory suffix")
    verification = sine._verify_point_sha256sums(point_dir)
    point_path = point_dir / "point.json"
    payload = load_json(point_path)
    source_window = payload.get("source_window", {})
    failures = list(payload.get("failures", []))
    gem_failures = [
        item
        for item in failures
        if str(item).startswith("M11JError: GEM snapshot is missing:")
    ]
    if (
        payload.get("pass") is not False
        or len(gem_failures) != 1
        or set(failures) - set(gem_failures) != EXPECTED_PREOUTPUT_FAILURES
        or source_window.get("on_status") is not None
        or source_window.get("on_monotonic_ns") is not None
        or source_window.get("off_status") is not None
        or payload.get("lan") is not None
        or payload.get("scope_windows") != []
        or payload.get("gem_before") is not None
        or payload.get("gem_after") is not None
        or payload.get("dp832_writes") is not False
        or payload.get("scope_impedance_writes") is not False
    ):
        raise M11JError("J resume source is not the frozen pre-output GEM-only failure")

    configuration = payload.get("configuration")
    if not isinstance(configuration, dict) or configuration.get("pass") is not True:
        raise M11JError("J resume source lacks a passing ARB configuration")
    if configuration.get("mode") != "hash-bound-wavebench-repeat-user-to-user":
        raise M11JError("J resume source was not configured through repeat USER upload")
    archive = Path(str(configuration.get("archive", ""))).resolve()
    try:
        archive.relative_to(point_dir)
    except ValueError as error:
        raise M11JError("J resume ARB archive escapes the prior point") from error
    run = load_json(archive)
    result = run.get("result", {})
    audit = result.get("audit", {})
    after_status = result.get("after", {}).get("status", {})
    record = selection["record"]
    if (
        run.get("pass") is not True
        or run.get("case_id") != selection["case_id"]
        or result.get("waveform_sha256") != selection["waveform_sha256"]
        or result.get("output_on") is not False
        or audit.get("distribution") != repeat_arb.EXPECTED_DISTRIBUTION
        or audit.get("version") != repeat_arb.EXPECTED_VERSION
        or audit.get("driver_source_sha256") != repeat_arb.EXPECTED_DRIVER_SHA256
        or after_status.get("function") != "USER"
        or after_status.get("output") != "OFF"
        or not math.isclose(
            float(after_status.get("frequency_hz", math.nan)),
            float(record["playback_frequency_hz"]),
            rel_tol=0.0,
            abs_tol=0.01,
        )
        or not math.isclose(
            float(after_status.get("amplitude", math.nan)),
            float(record["source_vpp_v"]),
            rel_tol=0.0,
            abs_tol=1e-6,
        )
    ):
        raise M11JError("J resume ARB upload/readback binding failed")

    current_profile = current_preflight.get("source", {}).get("profile", {})
    current_status = current_profile.get("status", {})
    if (
        current_preflight.get("pass") is not True
        or current_status.get("function") != "USER"
        or current_status.get("output") != "OFF"
        or current_profile.get("load_ohm") != 50.0
        or not math.isclose(
            float(current_status.get("frequency_hz", math.nan)),
            float(record["playback_frequency_hz"]),
            rel_tol=0.0,
            abs_tol=0.01,
        )
        or not math.isclose(
            float(current_status.get("amplitude", math.nan)),
            float(record["source_vpp_v"]),
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        or not math.isclose(
            float(current_status.get("offset_v", math.nan)),
            0.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        raise M11JError("current DG state no longer matches the configured J ARB")
    current_path = Path(str(current_preflight.get("evidence_path", ""))).resolve()
    if not current_path.is_file():
        raise M11JError("current J resume preflight evidence is missing")
    return {
        "format": "CycleScope M11-J pre-output resume binding v1",
        "prior_point": str(point_dir),
        "prior_point_json_sha256": safety.sha256_file(point_path),
        "prior_point_sha256sums": verification,
        "prior_configuration_archive": str(archive),
        "prior_configuration_archive_sha256": safety.sha256_file(archive),
        "waveform_sha256": selection["waveform_sha256"],
        "prior_source_output_on": False,
        "prior_lan_started": False,
        "prior_scope_capture_started": False,
        "current_preflight": str(current_path),
        "current_preflight_sha256": safety.sha256_file(current_path),
        "repeat_arb_upload_performed_during_resume": False,
        "resume_scope": "continue after fixed read-only GEM logging defect only",
        "pass": True,
    }


def response_gate(report: dict[str, Any]) -> dict[str, Any]:
    value = report.get("first_complete_frame_latency_us")
    failures: list[str] = []
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        failures.append("first complete frame latency is missing or non-finite")
    elif float(value) < 0.0:
        failures.append("first complete frame latency is negative")
    elif float(value) > RESPONSE_LIMIT_US:
        failures.append(
            f"first complete frame latency {float(value):g} us exceeds {RESPONSE_LIMIT_US:g} us"
        )
    return {
        "metric": "ENABLE_PUSH request start to first validated complete frame",
        "first_complete_frame_latency_us": value,
        "limit_us": RESPONSE_LIMIT_US,
        "failures": failures,
        "pass": not failures,
    }


def exact_longrun_gate(report: dict[str, Any]) -> dict[str, Any]:
    counters = report.get("counters")
    capture = report.get("capture")
    status_delta = report.get("status_delta")
    failures: list[str] = []
    if report.get("pass") is not True:
        failures.append("CSLP long-run report did not pass")
    if not isinstance(counters, dict):
        failures.append("CSLP counters are missing")
        counters = {}
    if not isinstance(capture, dict):
        failures.append("CSLP complete-frame capture is missing")
        capture = {}
    if not isinstance(status_delta, dict):
        failures.append("CSLP STATUS delta is missing")
        status_delta = {}
    expectations = (
        ("counters.frames_completed", counters.get("frames_completed"), EXPECTED_FRAMES),
        ("counters.wave_packets", counters.get("wave_packets"), EXPECTED_WAVE_PACKETS),
        ("capture.frame_count", capture.get("frame_count"), EXPECTED_FRAMES),
        (
            "expected_frames_after_deferred_disable",
            report.get("expected_frames_after_deferred_disable"),
            EXPECTED_FRAMES,
        ),
        ("status_delta.frames_sent", status_delta.get("frames_sent"), EXPECTED_FRAMES),
        (
            "status_delta.packets_sent",
            status_delta.get("packets_sent"),
            EXPECTED_WAVE_PACKETS,
        ),
    )
    for name, actual, expected in expectations:
        if actual != expected:
            failures.append(f"{name}={actual!r}, expected {expected}")
    return {
        "requested_frames": REQUESTED_FRAMES,
        "deferred_terminal_frames": 1,
        "expected_frames": EXPECTED_FRAMES,
        "expected_wave_packets": EXPECTED_WAVE_PACKETS,
        "checks": [
            {"name": name, "actual": actual, "expected": expected}
            for name, actual, expected in expectations
        ],
        "failures": failures,
        "pass": not failures,
    }


def progress_frame_count(log_text: str) -> int | None:
    matches = [int(value) for value in PROGRESS_PATTERN.findall(log_text)]
    return None if not matches else max(matches)


def require_live_acknowledgement(value: str) -> None:
    if value != J_LIVE_ACK:
        raise M11JError(f"M11-J live run requires --acknowledge {J_LIVE_ACK!r}")


def parse_gem_snapshot(text: str) -> dict[str, Any]:
    if "M11_GEM_READONLY_BEGIN" not in text or "M11_GEM_READONLY_END" not in text:
        raise M11JError("GEM snapshot markers are missing")
    values: dict[int, int] = {}
    for address_text, value_text in GEM_LINE_PATTERN.findall(text):
        address = int(address_text, 16)
        if address not in GEM_REGISTER_NAMES:
            continue
        if address in values:
            raise M11JError(f"duplicate GEM register 0x{address:08X}")
        values[address] = int(value_text, 16)
    missing = set(GEM_REGISTER_NAMES) - set(values)
    if missing:
        raise M11JError(
            "GEM snapshot is missing: "
            + ", ".join(f"0x{address:08X}" for address in sorted(missing))
        )
    failures: list[str] = []
    if values[0xF8000140] != 0x00500801:
        failures.append("GEM0 clock is not the frozen 25 MHz /8/5 configuration")
    nwcfg = values[0xE000B004]
    if nwcfg & 0x3 != 0x3 or nwcfg & (1 << 10):
        failures.append("GEM0 NWCFG does not report 100M full duplex with gigabit disabled")
    nonzero_errors = {
        GEM_REGISTER_NAMES[address]: values[address]
        for address in sorted(GEM_ERROR_ADDRESSES)
        if values[address] != 0
    }
    if nonzero_errors:
        failures.append(f"GEM error counters are nonzero: {nonzero_errors}")
    return {
        "registers": {
            GEM_REGISTER_NAMES[address]: {
                "address": f"0x{address:08X}",
                "value": values[address],
                "value_hex": f"0x{values[address]:08X}",
            }
            for address in sorted(values)
        },
        "error_counters_nonzero": nonzero_errors,
        "failures": failures,
        "pass": not failures,
    }


def _hw_server_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 3121), timeout=0.2):
            return True
    except OSError:
        return False


def start_hw_server(directory: Path) -> tuple[subprocess.Popen[str] | None, Any | None]:
    if _hw_server_ready():
        return None, None
    for executable in (HW_SERVER, XSDB):
        if not executable.is_file():
            raise M11JError(f"Xilinx executable is missing: {executable}")
    log_path = directory / "hw_server.log"
    log = log_path.open("x", encoding="utf-8")
    process = subprocess.Popen(
        [str(HW_SERVER), "-s", "tcp::3121"],
        cwd=safety.FPGA_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _hw_server_ready():
            return process, log
        returncode = process.poll()
        if returncode is not None:
            log.close()
            raise M11JError(f"hw_server exited early with code {returncode}")
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=3)
    log.close()
    raise M11JError("hw_server did not become ready within 10 seconds")


def stop_hw_server(process: subprocess.Popen[str] | None, log: Any | None) -> None:
    try:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    finally:
        if log is not None:
            log.close()


def capture_gem_snapshot(directory: Path, label: str) -> dict[str, Any]:
    if not GEM_TCL.is_file() or not XSDB.is_file():
        raise M11JError("read-only GEM script or XSDB is missing")
    log_path = directory / f"gem-{label}.log"
    with log_path.open("x", encoding="utf-8") as log:
        result = subprocess.run(
            [str(XSDB), "-no-ini", str(GEM_TCL), "--hw-url", HW_SERVER_URL],
            cwd=safety.FPGA_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode != 0:
        raise M11JError(f"read-only GEM snapshot exited with code {result.returncode}")
    parsed = parse_gem_snapshot(log_path.read_text(encoding="utf-8"))
    payload = {
        "format": "CycleScope M11-J read-only GEM snapshot v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "label": label,
        "instrument_writes": False,
        "target_reset_or_stop": False,
        "log": str(log_path.resolve()),
        "log_sha256": safety.sha256_file(log_path),
        **parsed,
    }
    path = directory / f"gem-{label}.json"
    safety.write_json_exclusive(path, payload)
    payload["evidence_path"] = str(path.resolve())
    return payload


def gem_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_tx = int(before["registers"]["TX_FRAMES"]["value"])
    after_tx = int(after["registers"]["TX_FRAMES"]["value"])
    transmitted = (after_tx - before_tx) & 0xFFFFFFFF
    failures: list[str] = []
    if before.get("pass") is not True or after.get("pass") is not True:
        failures.append("one GEM snapshot did not pass")
    if transmitted < EXPECTED_WAVE_PACKETS:
        failures.append(
            f"GEM TX frame delta {transmitted} is below {EXPECTED_WAVE_PACKETS} WAVE packets"
        )
    return {
        "tx_frames_before": before_tx,
        "tx_frames_after": after_tx,
        "tx_frames_delta": transmitted,
        "minimum_expected_wave_packets": EXPECTED_WAVE_PACKETS,
        "failures": failures,
        "pass": not failures,
    }


def capture_power_snapshot(directory: Path, label: str, config: Any) -> dict[str, Any]:
    """Read DP832 identity, operating point, and protection without any setter."""

    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"power-{label}-commands.log"
    logger = CommandLogger(log_path)
    service = PowerService(config=config, logger=logger)
    idn = service.idn()
    status = service.status(safety.POWER_CHANNEL)
    measurement = service.measurement(safety.POWER_CHANNEL)
    protection = service.protection_status(safety.POWER_CHANNEL)
    failures: list[str] = []
    if status.output != "ON":
        failures.append(f"DP832 CH1 output changed from ON: {status.output!r}")
    if (
        status.measured_voltage_v is None
        or not safety.EXPECTED_SUPPLY_MIN_V
        <= status.measured_voltage_v
        <= safety.EXPECTED_SUPPLY_MAX_V
    ):
        failures.append(
            f"DP832 measured voltage is outside the frozen 5 V gate: "
            f"{status.measured_voltage_v!r}"
        )
    if protection.ovp_tripped != "NO" or protection.ocp_tripped != "NO":
        failures.append(
            f"DP832 protection trip changed: OVP={protection.ovp_tripped!r}, "
            f"OCP={protection.ocp_tripped!r}"
        )
    payload = {
        "format": "CycleScope M11-J DP832 read-only window snapshot v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "label": label,
        "idn": idn,
        "status": status.as_dict(),
        "measurement": {
            "channel": measurement.channel,
            "measured_voltage_v": measurement.measured_voltage_v,
            "measured_current_a": measurement.measured_current_a,
            "measured_power_w": measurement.measured_power_w,
        },
        "protection": protection.as_dict(),
        "commands_log": str(log_path.resolve()),
        "commands_log_sha256": safety.sha256_file(log_path),
        "read_only": True,
        "instrument_writes": False,
        "failures": failures,
        "pass": not failures,
    }
    path = directory / f"power-{label}.json"
    safety.write_json_exclusive(path, payload)
    payload["evidence_path"] = str(path.resolve())
    return payload


def fresh_source_output_off(config: Any, logger: CommandLogger) -> dict[str, Any]:
    """Discard any long-lived transport and perform at most one fresh OFF write."""

    base = SourceService(config=config, logger=logger)
    session = base.open_session()
    try:
        source = SourceService(config=config, logger=logger, session=session)
        before = source.status(1)
        write_performed = before.output != "OFF"
        after = source.set_output(1, False) if write_performed else before
        confirmed = source.status(1)
        if after.output != "OFF" or confirmed.output != "OFF":
            raise M11JError("fresh-session DG OFF readback mismatch")
        return {
            "before": before.as_dict(),
            "set_status": after.as_dict(),
            "confirmed": confirmed.as_dict(),
            "off_write_performed": write_performed,
            "write_count_maximum": 1,
            "pass": True,
        }
    finally:
        session.close()


def configure_longrun_arb(
    *,
    point_dir: Path,
    record: dict[str, Any],
    before: dict[str, Any],
    config: Any,
) -> dict[str, Any]:
    """Upload the frozen H stimulus while DG is OFF and archive the transaction."""

    plan_path = point_dir / "source-arb-config-plan.toml"
    plan_path.write_text(arb.plan_text(record), encoding="utf-8")
    checked = arb.validate_configuration_plan(plan_path, record, config)
    plan = load_run_plan(plan_path)
    service = RunService(config=config, logger=CommandLogger())
    verify = service.verify(plan)
    initial_function = str(before["source"]["profile"]["status"]["function"]).upper()
    if initial_function == "SIN":
        runs_before = safety._run_directories()
        result = service.run(plan)
        runs_after = safety._run_directories()
        new_runs = runs_after - runs_before
        if result.run_dir.resolve() not in new_runs:
            raise M11JError("WaveBench ARB run directory was not uniquely created")
        archive = safety.archive_run(result.run_dir, point_dir / "wavebench" / "run")
        run_payload = load_json(result.run_json_path)
        if run_payload.get("status") != "ok":
            raise M11JError("WaveBench ARB configuration did not pass")
        mode = "wavebench-run-service-basic-to-user"
    elif initial_function == "USER":
        repeat_dir = point_dir / "wavebench" / "run" / "repeat-arb-upload"
        repeat_dir.mkdir(parents=True, exist_ok=False)
        repeat_logger = CommandLogger(repeat_dir / "commands.log")
        result = repeat_arb.upload_repeated_arb(
            config=config,
            logger=repeat_logger,
            waveform=Path(record["waveform_path"]),
            playback_frequency_hz=float(record["playback_frequency_hz"]),
            amplitude_vpp=float(record["source_vpp_v"]),
            points=int(record["points"]),
        )
        archive = repeat_dir / "run.json"
        safety.write_json_exclusive(
            archive,
            {
                "format": "CycleScope M11-J hash-bound repeated ARB upload v1",
                "created_at": datetime.now().astimezone().isoformat(),
                "case_id": record["case_id"],
                "checked_plan": checked,
                "result": result,
                "pass": True,
            },
        )
        safety._write_sha256sums(repeat_dir)
        mode = "hash-bound-wavebench-repeat-user-to-user"
    else:
        raise M11JError(
            f"M11-J configuration requires SIN/OFF or USER/OFF, got {initial_function!r}"
        )
    return {
        "checked_plan": checked,
        "verify": [
            {
                "instrument": item.instrument,
                "idn": item.idn,
                "resource_sha256": safety.sha256_text(item.resource),
            }
            for item in verify
        ],
        "mode": mode,
        "archive": str(archive.resolve()),
        "pass": True,
    }


def _read_progress(log_path: Path) -> int | None:
    try:
        return progress_frame_count(log_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def capture_progress_window(
    *,
    point_dir: Path,
    record: dict[str, Any],
    config: Any,
    threshold: int,
    console_log: Path,
) -> dict[str, Any]:
    """Capture one dual-channel WaveBench package inside a running LAN session."""

    label = f"j-frame-{threshold:05d}"
    window_dir = point_dir / "scope-windows"
    window_dir.mkdir(parents=True, exist_ok=True)
    progress_before = _read_progress(console_log)
    if progress_before is None or progress_before < threshold:
        raise M11JError(
            f"scope window {threshold} started without matching LAN progress evidence"
        )
    power = capture_power_snapshot(window_dir, label, config)
    raw_before = safety._raw_directories()
    scope = arb._scope_capture(config, record, f"cyclescope_m11_{label}")
    raw_after = safety._raw_directories()
    packages = safety._select_new_scope_raw_packages(
        scope_result=scope,
        raw_before=raw_before,
        raw_after=raw_after,
    )
    archives = safety.archive_raw_packages(packages, point_dir / "wavebench" / "raw")
    if len(archives) != 1:
        raise M11JError(f"scope window {threshold} did not archive exactly one raw package")
    archived_package = Path(archives[0]["destination"])
    analysis = arb._scope_analysis(record, archived_package)
    analysis_path = window_dir / f"scope-{label}-analysis.json"
    safety.write_json_exclusive(analysis_path, analysis)
    progress_after = _read_progress(console_log)
    overlap_frames = None
    if progress_after is not None:
        overlap_frames = {
            "first_observed_progress": progress_before,
            "last_observed_progress": progress_after,
        }
    failures: list[str] = []
    if power.get("pass") is not True:
        failures.extend(f"power: {item}" for item in power.get("failures", []))
    if scope.get("pass") is not True:
        failures.extend(f"scope: {item}" for item in scope.get("failures", []))
    if analysis.get("pass") is not True:
        failures.extend(f"scope analysis: {item}" for item in analysis.get("failures", []))
    payload = {
        "format": "CycleScope M11-J progress-bound scope window v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "threshold_frame": threshold,
        "progress_binding": overlap_frames,
        "power_snapshot": power.get("evidence_path"),
        "scope": scope,
        "wavebench_raw_archives": archives,
        "scope_analysis": str(analysis_path.resolve()),
        "screenshots_used_for_numeric_results": False,
        "failures": failures,
        "pass": not failures,
    }
    path = window_dir / f"window-{label}.json"
    safety.write_json_exclusive(path, payload)
    payload["evidence_path"] = str(path.resolve())
    return payload


def analyze_longrun_adc(record: dict[str, Any], capture_dir: Path) -> dict[str, Any]:
    """Reuse the frozen H recovery model over every captured long-run frame."""

    fit, _verification = calibration.load_frozen_fit(arb.CALIBRATION_FIT_DIR)
    result = arb._adc_analysis(record, capture_dir, fit["model"])
    if int(result.get("frame_count", -1)) != EXPECTED_FRAMES:
        result["failures"].append(
            f"long-run ADC analysis used {result.get('frame_count')!r} frames, "
            f"expected {EXPECTED_FRAMES}"
        )
        result["pass"] = False
    return result


def gain_drift_summary(adc_recovery: dict[str, Any]) -> dict[str, Any]:
    frames = adc_recovery.get("frames")
    if not isinstance(frames, list) or len(frames) != EXPECTED_FRAMES:
        raise M11JError(
            f"long-run ADC analysis must contain {EXPECTED_FRAMES} frames"
        )
    block_records: list[dict[str, Any]] = []
    first_median = None
    for block_index in range(REQUESTED_FRAMES // GAIN_BLOCK_FRAMES):
        start = block_index * GAIN_BLOCK_FRAMES
        finish = start + GAIN_BLOCK_FRAMES
        values = np.asarray(
            [float(item["recovered_band_vpp_v"]) for item in frames[start:finish]],
            dtype=np.float64,
        )
        if values.size != GAIN_BLOCK_FRAMES or not np.all(np.isfinite(values)):
            raise M11JError("gain-drift block contains invalid recovered Vpp values")
        median_vpp = float(np.median(values))
        if median_vpp <= 0.0:
            raise M11JError("gain-drift block has non-positive recovered Vpp")
        if first_median is None:
            first_median = median_vpp
        gain_db = 20.0 * math.log10(median_vpp / first_median)
        block_records.append(
            {
                "block_index": block_index,
                "frame_index_start": start,
                "frame_index_finish_exclusive": finish,
                "median_recovered_band_vpp_v": median_vpp,
                "relative_to_first_block_db": gain_db,
            }
        )
    gains = [float(item["relative_to_first_block_db"]) for item in block_records]
    span_db = max(gains) - min(gains)
    return {
        "policy": (
            "ten non-overlapping 1,000-frame blocks from the requested 10,000 frames; "
            "the deferred terminal frame is protocol evidence only"
        ),
        "blocks": block_records,
        "gain_span_db": span_db,
        "limit_db": GAIN_DRIFT_LIMIT_DB,
        "terminal_frame_excluded_from_drift": True,
        "failures": [] if span_db <= GAIN_DRIFT_LIMIT_DB else [
            f"gain drift {span_db:g} dB exceeds {GAIN_DRIFT_LIMIT_DB:g} dB"
        ],
        "pass": span_db <= GAIN_DRIFT_LIMIT_DB,
    }


def run_live(
    acknowledgement: str,
    resume_preoutput_point: Path | None = None,
) -> dict[str, Any]:
    """Execute the one authorized J response/long-run session fail closed."""

    require_live_acknowledgement(acknowledgement)
    upper_gate = upper_frequency_gate()
    static = safety.validate_static(write_evidence=True)
    physical = sine.physical_gate()
    if physical.get("pass") is not True:
        raise M11JError(
            "formal physical gate is incomplete: "
            + "; ".join(physical.get("failures", []))
        )
    identity = arb.require_nonzero_calibration(CALIBRATION_MANIFEST)
    driver = repeat_arb.verify_installed_driver()
    selection = select_longrun_case()
    record = selection["record"]

    before = arb.arb_readonly_preflight()
    if before.get("pass") is not True:
        raise M11JError(
            "read-only ARB preflight failed: " + "; ".join(before.get("failures", []))
        )
    preoutput_resume = None
    if resume_preoutput_point is not None:
        preoutput_resume = validate_preoutput_resume(
            resume_preoutput_point,
            selection,
            before,
        )
    lan_smoke = safety.lan_preflight(
        safety.LIVE_ACK,
        instrument_preflight=before,
        expected_calibration_id=int(identity["calibration_id"]),
        expected_scale_uv_per_lsb=int(identity["scale_uv_per_lsb"]),
        expected_offset_uv=int(identity["offset_uv"]),
    )
    if lan_smoke.get("pass") is not True:
        raise M11JError("calibrated LAN preflight failed")

    stamp = safety.now_stamp()
    point_dir = EVIDENCE_ROOT / "points" / f"{stamp}_j-response-longrun"
    point_dir.mkdir(parents=True, exist_ok=False)
    config = safety.derived_config()
    if preoutput_resume is None:
        configuration = configure_longrun_arb(
            point_dir=point_dir,
            record=record,
            before=before,
            config=config,
        )
    else:
        configuration = {
            "format": "CycleScope M11-J reused verified pre-output ARB configuration v1",
            "mode": "resume-verified-preoutput-configuration",
            "prior_configuration_archive": preoutput_resume[
                "prior_configuration_archive"
            ],
            "waveform_sha256": preoutput_resume["waveform_sha256"],
            "repeat_arb_upload_performed": False,
            "pass": True,
        }
    configured = arb.arb_readonly_preflight()
    if configured.get("pass") is not True:
        raise M11JError(
            "configured ARB preflight failed: "
            + "; ".join(configured.get("failures", []))
        )

    operation_errors: list[str] = []
    windows: list[dict[str, Any]] = []
    lan_result: dict[str, Any] | None = None
    output_on_status: dict[str, Any] | None = None
    output_off_status: dict[str, Any] | None = None
    output_off_recovery: dict[str, Any] | None = None
    on_ns: int | None = None
    off_ns: int | None = None
    gem_before: dict[str, Any] | None = None
    gem_after: dict[str, Any] | None = None
    hw_process: subprocess.Popen[str] | None = None
    hw_log: Any | None = None
    source_session = None
    source_logger: CommandLogger | None = None
    executor: ThreadPoolExecutor | None = None
    lan_future = None
    console_log = point_dir / "lan" / "console.log"

    try:
        hw_process, hw_log = start_hw_server(point_dir)
        gem_before = capture_gem_snapshot(point_dir, "before")
        if gem_before.get("pass") is not True:
            raise M11JError("initial GEM snapshot did not pass")

        source_logger = CommandLogger(point_dir / "source-window-commands.log")
        source_base = SourceService(config=config, logger=source_logger)
        source_session = source_base.open_session()
        source = SourceService(
            config=config,
            logger=source_logger,
            session=source_session,
        )
        profile_failures = arb._profile_matches_arb(source.channel_profile(1), record)
        if profile_failures:
            raise M11JError("; ".join(profile_failures))
        output_on_status = source.set_output(1, True).as_dict()
        on_ns = time.monotonic_ns()
        if output_on_status.get("output") != "ON" or output_on_status.get("function") != "USER":
            raise M11JError("DG did not read back USER/ON for the J stimulus")
        time.sleep(1.0)

        executor = ThreadPoolExecutor(max_workers=1)
        lan_future = executor.submit(
            safety._capture_zero_lan,
            point_dir,
            REQUESTED_FRAMES,
            activity_policy="require",
            expected_calibration_id=int(identity["calibration_id"]),
            expected_scale_uv_per_lsb=int(identity["scale_uv_per_lsb"]),
            expected_offset_uv=int(identity["offset_uv"]),
            archive_packets=True,
            run_timeout_s=LONGRUN_TIMEOUT_S,
            progress_every=PROGRESS_EVERY,
        )

        threshold_index = 0
        last_announced = 0
        while threshold_index < len(PROGRESS_CAPTURE_THRESHOLDS):
            progress = _read_progress(console_log)
            if progress is not None and progress // 1_000 > last_announced // 1_000:
                last_announced = progress
                print(f"M11_J_PROGRESS frames={progress}", flush=True)
            threshold = PROGRESS_CAPTURE_THRESHOLDS[threshold_index]
            if progress is not None and progress >= threshold:
                print(f"M11_J_SCOPE_WINDOW_START threshold={threshold}", flush=True)
                window = capture_progress_window(
                    point_dir=point_dir,
                    record=record,
                    config=config,
                    threshold=threshold,
                    console_log=console_log,
                )
                windows.append(window)
                print(
                    f"M11_J_SCOPE_WINDOW_DONE threshold={threshold} "
                    f"pass={str(window['pass']).lower()}",
                    flush=True,
                )
                if window.get("pass") is not True:
                    raise M11JError(f"scope/power window {threshold} did not pass")
                threshold_index += 1
                continue
            if lan_future.done():
                break
            time.sleep(0.1)
        if len(windows) != len(PROGRESS_CAPTURE_THRESHOLDS):
            raise M11JError(
                f"captured {len(windows)} progress windows, expected "
                f"{len(PROGRESS_CAPTURE_THRESHOLDS)}"
            )
        lan_result = lan_future.result(timeout=LONGRUN_TIMEOUT_S + 30.0)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        operation_errors.append(message)
        print(f"M11_J_LIVE_ERROR={message}", file=sys.stderr, flush=True)
    finally:
        if source_session is not None:
            try:
                source_session.close()
            except Exception as error:
                operation_errors.append(
                    f"close long-lived DG session: {type(error).__name__}: {error}"
                )
            finally:
                source_session = None
        if output_on_status is not None and output_on_status.get("output") == "ON":
            try:
                output_off_recovery = fresh_source_output_off(
                    config,
                    CommandLogger(point_dir / "source-final-off-commands.log"),
                )
                output_off_status = output_off_recovery["confirmed"]
                off_ns = time.monotonic_ns()
                if (
                    output_off_status.get("output") != "OFF"
                    or output_off_status.get("function") != "USER"
                ):
                    operation_errors.append("DG did not read back USER/OFF after J")
            except Exception as error:
                message = f"DG USER/OFF recovery: {type(error).__name__}: {error}"
                operation_errors.append(message)
                print(f"M11_J_RECOVERY_ERROR={message}", file=sys.stderr, flush=True)

    if lan_future is not None and lan_result is None:
        try:
            print("M11_J_WAITING_FOR_LAN_DISABLE", flush=True)
            lan_result = lan_future.result(timeout=LONGRUN_TIMEOUT_S + 30.0)
        except Exception as error:
            operation_errors.append(f"LAN collector: {type(error).__name__}: {error}")
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)

    try:
        if gem_before is not None:
            gem_after = capture_gem_snapshot(point_dir, "after")
    except Exception as error:
        operation_errors.append(f"final GEM snapshot: {type(error).__name__}: {error}")
    finally:
        stop_hw_server(hw_process, hw_log)

    try:
        after = arb.arb_readonly_preflight()
    except Exception as error:
        operation_errors.append(f"final read-only preflight: {type(error).__name__}: {error}")
        after = {
            "pass": False,
            "failures": [operation_errors[-1]],
            "evidence_path": None,
        }

    response: dict[str, Any] | None = None
    exact: dict[str, Any] | None = None
    adc_analysis: dict[str, Any] | None = None
    drift: dict[str, Any] | None = None
    gem_counts: dict[str, Any] | None = None
    lan_report: dict[str, Any] | None = None
    if lan_result is not None:
        report_path = Path(str(lan_result["report"]))
        if report_path.is_file():
            lan_report = load_json(report_path)
            response = response_gate(lan_report)
            exact = exact_longrun_gate(lan_report)
            try:
                print("M11_J_ADC_ANALYSIS_START", flush=True)
                adc_analysis = analyze_longrun_adc(
                    record,
                    Path(str(lan_result["capture_dir"])),
                )
                adc_path = point_dir / "adc-analysis.json"
                safety.write_json_exclusive(adc_path, adc_analysis)
                drift = gain_drift_summary(adc_analysis)
                safety.write_json_exclusive(point_dir / "gain-drift.json", drift)
                print("M11_J_ADC_ANALYSIS_DONE", flush=True)
            except Exception as error:
                operation_errors.append(f"ADC analysis: {type(error).__name__}: {error}")
        else:
            operation_errors.append("LAN report path is missing")
    if gem_before is not None and gem_after is not None:
        gem_counts = gem_delta(gem_before, gem_after)
        safety.write_json_exclusive(point_dir / "gem-delta.json", gem_counts)

    failures = list(operation_errors)
    for name, result in (
        ("configured preflight", configured),
        ("LAN acquisition", lan_result),
        ("response", response),
        ("exact long-run", exact),
        ("ADC recovery", adc_analysis),
        ("gain drift", drift),
        ("GEM delta", gem_counts),
        ("final preflight", after),
    ):
        if not isinstance(result, dict) or result.get("pass") is not True:
            failures.append(f"{name} did not pass")
    if len(windows) != len(PROGRESS_CAPTURE_THRESHOLDS):
        failures.append("three progress-bound scope windows are incomplete")
    for window in windows:
        if window.get("pass") is not True:
            failures.append(
                f"scope window {window.get('threshold_frame')!r} did not pass"
            )
    packet_archive = None if lan_result is None else lan_result.get("packet_archive")
    if not isinstance(packet_archive, dict) or packet_archive.get("pass") is not True:
        failures.append("complete pcap source_data archive did not pass")

    overlaps: list[dict[str, Any]] = []
    if lan_result is not None:
        for window in windows:
            scope = window["scope"]
            overlap_ns = min(
                int(scope["finished_monotonic_ns"]),
                int(lan_result["finished_monotonic_ns"]),
            ) - max(
                int(scope["started_monotonic_ns"]),
                int(lan_result["started_monotonic_ns"]),
            )
            overlaps.append(
                {
                    "threshold_frame": window["threshold_frame"],
                    "scope_lan_overlap_ns": overlap_ns,
                    "pass": overlap_ns > 0,
                }
            )
            if overlap_ns <= 0:
                failures.append(
                    f"scope window {window['threshold_frame']} did not overlap LAN"
                )

    payload = {
        "format": "CycleScope M11-J response and 10,001-frame long-run v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "acknowledgement": acknowledgement,
        "static": static,
        "physical_gate": physical,
        "upper_frequency_gate": upper_gate,
        "calibration_identity": identity,
        "repeat_arb_driver": driver,
        "longrun_selection": selection,
        "preflight_evidence": before.get("evidence_path"),
        "lan_preflight_evidence": lan_smoke.get("evidence_path"),
        "configuration": configuration,
        "preoutput_resume": preoutput_resume,
        "configured_preflight_evidence": configured.get("evidence_path"),
        "source_window": {
            "on_monotonic_ns": on_ns,
            "off_monotonic_ns": off_ns,
            "on_status": output_on_status,
            "off_status": output_off_status,
            "off_recovery": output_off_recovery,
            "restoration_boundary": (
                "DG is forced to USER/OFF. The hash-bound user waiver accepts USER/OFF "
                "as the final state and does not require restoration to fixed SIN."
            ),
        },
        "lan": lan_result,
        "lan_report": lan_report,
        "response_gate": response,
        "exact_longrun_gate": exact,
        "scope_windows": [item.get("evidence_path") for item in windows],
        "scope_lan_overlaps": overlaps,
        "adc_analysis": (
            None
            if adc_analysis is None
            else str((point_dir / "adc-analysis.json").resolve())
        ),
        "gain_drift": drift,
        "gem_before": None if gem_before is None else gem_before.get("evidence_path"),
        "gem_after": None if gem_after is None else gem_after.get("evidence_path"),
        "gem_delta": gem_counts,
        "final_preflight_evidence": after.get("evidence_path"),
        "packet_archive": packet_archive,
        "dp832_writes": False,
        "scope_impedance_writes": False,
        "screenshots_used_for_numeric_results": False,
        "user_restoration_waiver": sine.load_user_restoration_waiver(),
        "final_fixed_sine_restoration_required": False,
        "failures": failures,
        "pass": not failures,
    }
    point_path = point_dir / "point.json"
    safety.write_json_exclusive(point_path, payload)
    sums = safety._write_sha256sums(point_dir)
    payload["evidence_path"] = str(point_path.resolve())
    payload["sha256sums"] = str(sums.resolve())
    return payload


def build_recovered_final_summary(
    point_dir: Path,
    recovery_dir: Path,
) -> dict[str, Any]:
    """Accept completed J measurements while preserving the delayed OFF failure."""

    point_dir = point_dir.resolve()
    recovery_dir = recovery_dir.resolve()
    point_verification = sine._verify_point_sha256sums(point_dir)
    point_path = point_dir / "point.json"
    point = load_json(point_path)
    failures = list(point.get("failures", []))
    recovery_failures = [
        item
        for item in failures
        if str(item).startswith("DG USER/OFF recovery: InstrumentError:")
    ]
    if (
        point.get("pass") is not False
        or len(recovery_failures) != 1
        or set(failures) - set(recovery_failures) != {"final preflight did not pass"}
        or point.get("dp832_writes") is not False
        or point.get("scope_impedance_writes") is not False
        or point.get("source_window", {}).get("on_status", {}).get("output") != "ON"
        or point.get("source_window", {}).get("off_status") is not None
    ):
        raise M11JError("J point is not the frozen completed-run/delayed-OFF case")

    lan_report = point.get("lan_report")
    if not isinstance(lan_report, dict):
        raise M11JError("J point lacks the complete LAN report")
    response = response_gate(lan_report)
    exact = exact_longrun_gate(lan_report)
    if (
        response.get("pass") is not True
        or exact.get("pass") is not True
        or calibration.canonical_sha256(response)
        != calibration.canonical_sha256(point.get("response_gate"))
        or calibration.canonical_sha256(exact)
        != calibration.canonical_sha256(point.get("exact_longrun_gate"))
    ):
        raise M11JError("J response/exact long-run gates are not reproducible")

    adc_path = Path(str(point.get("adc_analysis", ""))).resolve()
    try:
        adc_path.relative_to(point_dir)
    except ValueError as error:
        raise M11JError("J ADC analysis escapes the point directory") from error
    adc = load_json(adc_path)
    drift = gain_drift_summary(adc)
    if (
        adc.get("pass") is not True
        or int(adc.get("frame_count", -1)) != EXPECTED_FRAMES
        or drift.get("pass") is not True
        or calibration.canonical_sha256(drift)
        != calibration.canonical_sha256(point.get("gain_drift"))
    ):
        raise M11JError("J ADC recovery/gain-drift evidence failed")

    windows = point.get("scope_windows")
    overlaps = point.get("scope_lan_overlaps")
    if not isinstance(windows, list) or len(windows) != 3:
        raise M11JError("J does not contain exactly three scope windows")
    window_records: list[dict[str, Any]] = []
    for expected_threshold, value in zip(PROGRESS_CAPTURE_THRESHOLDS, windows, strict=True):
        path = Path(str(value)).resolve()
        try:
            path.relative_to(point_dir)
        except ValueError as error:
            raise M11JError("J scope-window evidence escapes the point") from error
        record = load_json(path)
        if record.get("pass") is not True or record.get("threshold_frame") != expected_threshold:
            raise M11JError(f"J scope window {expected_threshold} did not pass")
        window_records.append(
            {
                "threshold_frame": expected_threshold,
                "path": str(path),
                "sha256": safety.sha256_file(path),
                "power_snapshot": record.get("power_snapshot"),
                "scope_analysis": record.get("scope_analysis"),
                "pass": True,
            }
        )
    if (
        not isinstance(overlaps, list)
        or [item.get("threshold_frame") for item in overlaps]
        != list(PROGRESS_CAPTURE_THRESHOLDS)
        or not all(item.get("pass") is True for item in overlaps)
    ):
        raise M11JError("J scope/LAN overlap evidence failed")

    gem = point.get("gem_delta")
    if (
        not isinstance(gem, dict)
        or gem.get("pass") is not True
        or int(gem.get("tx_frames_delta", -1)) < EXPECTED_WAVE_PACKETS
    ):
        raise M11JError("J GEM delta evidence failed")
    source_archive = calibration._source_archive_evidence(
        point_dir,
        point,
        safety.SOURCE_DATA_ROOT,
    )
    if int(source_archive.get("wave_packets", -1)) != EXPECTED_WAVE_PACKETS:
        raise M11JError("J source_data archive WAVE packet count changed")

    recovery_verification = calibration.verify_sha256sums(recovery_dir)
    recovery_path = recovery_dir / "recovery.json"
    recovery = load_json(recovery_path)
    recovered = recovery.get("postflight_state", {})
    if (
        recovery.get("pass") is not True
        or recovery.get("prior_j_point") != str(point_dir)
        or recovery.get("prior_j_point_json_sha256") != safety.sha256_file(point_path)
        or recovery.get("authorized_write")
        != "DG4202 CH1 output OFF exactly once through WaveBench run plan"
        or recovery.get("function_frequency_amplitude_writes") is not False
        or recovery.get("scope_writes") is not False
        or recovery.get("dp832_writes") is not False
        or recovery.get("retry_count") != 0
        or recovered.get("function") != "USER"
        or recovered.get("output") != "OFF"
        or not math.isclose(float(recovered.get("offset_v", math.nan)), 0.0, abs_tol=1e-9)
    ):
        raise M11JError("J fresh-session OFF recovery evidence failed")
    archive_manifests = recovery.get("archive_manifests")
    if not isinstance(archive_manifests, list) or len(archive_manifests) != 1:
        raise M11JError("J OFF recovery must archive exactly one WaveBench run")
    archive_manifest = Path(str(archive_manifests[0])).resolve()
    try:
        archive_manifest.relative_to(recovery_dir)
    except ValueError as error:
        raise M11JError("J OFF recovery run archive escapes recovery evidence") from error
    archive = load_json(archive_manifest)
    run_path = Path(str(archive.get("destination_run", ""))) / "run.json"
    run = load_json(run_path)
    if (
        run.get("status") != "ok"
        or [step.get("kind") for step in run.get("steps", [])]
        != ["source.status", "power.status", "source.output", "source.status", "power.status"]
        or run.get("steps", [])[2].get("artifact", {}).get("source_status", {}).get("output")
        != "OFF"
    ):
        raise M11JError("J OFF recovery WaveBench run did not pass exactly once")

    return {
        "format": "CycleScope M11-J final acceptance with fresh-session OFF recovery v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "measurement_point": str(point_dir),
        "measurement_point_json_sha256": safety.sha256_file(point_path),
        "measurement_point_sha256sums": {
            "manifest": point_verification["manifest"],
            "manifest_sha256": point_verification["manifest_sha256"],
            "files_verified": point_verification["files_verified"],
        },
        "original_point_pass": False,
        "original_same_session_off_pass": False,
        "measurement_gates": {
            "response": response,
            "exact_longrun": exact,
            "adc_recovery_pass": True,
            "gain_drift": drift,
            "gem_delta": gem,
            "scope_windows": window_records,
            "scope_lan_overlaps": overlaps,
            "pcap": source_archive,
            "pass": True,
        },
        "fresh_session_off_recovery": {
            "directory": str(recovery_dir),
            "recovery_json": str(recovery_path),
            "recovery_json_sha256": safety.sha256_file(recovery_path),
            "sha256sums": recovery_verification,
            "final_source_status": recovered,
            "pass": True,
        },
        "longrun_repeated": False,
        "raw_samples_modified": False,
        "dp832_writes": False,
        "final_fixed_sine_restoration_required": False,
        "warning": (
            "the original long-lived DG session failed to turn output OFF; a separately "
            "checked fresh-session OFF action succeeded and is preserved as explicit recovery"
        ),
        "pass": True,
    }


def write_recovered_final_summary(
    point_dir: Path,
    recovery_dir: Path,
    output_dir: Path = J_FINAL_SUMMARY_DIR,
) -> dict[str, Any]:
    if output_dir.exists():
        raise M11JError(f"J final summary already exists: {output_dir}")
    payload = build_recovered_final_summary(point_dir, recovery_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    path = output_dir / "summary.json"
    safety.write_json_exclusive(path, payload)
    sums = safety._write_sha256sums(output_dir)
    return {
        "summary": str(path.resolve()),
        "summary_sha256": safety.sha256_file(path),
        "sha256sums": str(sums.resolve()),
        "pass": True,
    }


def offline_check() -> dict[str, Any]:
    static = safety.validate_static(write_evidence=True)
    physical = sine.physical_gate()
    identity = arb.require_nonzero_calibration(CALIBRATION_MANIFEST)
    driver = repeat_arb.verify_installed_driver()
    selection = select_longrun_case()
    restoration_waiver = sine.load_user_restoration_waiver()
    failures: list[str] = []
    i_summary = None
    try:
        i_summary = upper_frequency_gate()
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
    if physical.get("pass") is not True:
        failures.extend(f"physical: {item}" for item in physical.get("failures", []))
    return {
        "format": "CycleScope M11-J offline readiness v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "instrument_io": False,
        "live_writes": False,
        "static": static,
        "physical_gate": physical,
        "calibration_identity": identity,
        "repeat_arb_driver": driver,
        "longrun_selection": selection,
        "user_restoration_waiver": restoration_waiver,
        "i_summary": i_summary,
        "response_limit_us": RESPONSE_LIMIT_US,
        "requested_frames": REQUESTED_FRAMES,
        "expected_frames": EXPECTED_FRAMES,
        "expected_wave_packets": EXPECTED_WAVE_PACKETS,
        "gain_drift_limit_db": GAIN_DRIFT_LIMIT_DB,
        "dp800_writes_forbidden": True,
        "failures": failures,
        "live_ready": not failures,
        "pass": not failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    response = commands.add_parser("response-check")
    response.add_argument("--lan-report", type=Path, required=True)
    exact = commands.add_parser("longrun-check")
    exact.add_argument("--lan-report", type=Path, required=True)
    live = commands.add_parser("j-live")
    live.add_argument("--acknowledge", required=True)
    live.add_argument("--resume-preoutput-point", type=Path)
    finalize = commands.add_parser("finalize-recovered")
    finalize.add_argument("--point-dir", type=Path, required=True)
    finalize.add_argument("--recovery-dir", type=Path, required=True)
    finalize.add_argument("--output-dir", type=Path, default=J_FINAL_SUMMARY_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "check":
            result = offline_check()
        elif args.command == "response-check":
            result = response_gate(load_json(args.lan_report.resolve()))
        elif args.command == "longrun-check":
            result = exact_longrun_gate(load_json(args.lan_report.resolve()))
        elif args.command == "j-live":
            result = run_live(args.acknowledge, args.resume_preoutput_point)
        else:
            result = write_recovered_final_summary(
                args.point_dir,
                args.recovery_dir,
                args.output_dir,
            )
    except Exception as error:
        print(f"M11_J_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
