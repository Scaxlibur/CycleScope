#!/usr/bin/env python3
"""Fail-closed WaveBench campaign for CycleScope final calibration M2..M6.

The hardware path deliberately has no PowerService and loads a TOML file with
no [power] section.  The only DP800 operation is a separate exact
``wavebench power status`` subprocess in preflight.  FPGA traffic is received
through the existing passive mirror; this program never sends CSLP traffic.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
import json
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
from typing import Any

import numpy as np

import calibration_core as core


PROJECT_ROOT = core.PROJECT_ROOT
WORKSPACE_ROOT = PROJECT_ROOT.parent
WAVEBENCH_ROOT = core.WAVEBENCH_ROOT
WAVEBENCH_PYTHON = WAVEBENCH_ROOT / ".venv" / "bin" / "python"
WAVEBENCH_CLI = WAVEBENCH_ROOT / ".venv" / "bin" / "wavebench"
WAVEBENCH_CONFIG = PROJECT_ROOT / "tool-of-rei" / "m12-wavebench-safe.toml"
POWER_READONLY_CONFIG = WAVEBENCH_ROOT / "wavebench.toml"
MIRROR_SCRIPT = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_passive_mirror_capture.py"
EVIDENCE_PARENT = PROJECT_ROOT / "tool-of-rei" / "evidence"
MIRROR_BIND = "192.168.10.4"
MIRROR_PORT = 50002
FPGA_IP = "192.168.10.2"
SOURCE_CHANNEL = 1
SCOPE_CHANNELS = (1, 2)

if str(core.WAVEBENCH_SRC) not in sys.path:
    sys.path.insert(0, str(core.WAVEBENCH_SRC))

from wavebench.config import WaveBenchConfig, load_config
from wavebench.logging import CommandLogger
from wavebench.services.scope_service import ScopeService
from wavebench.services.source_service import SourceService


class CampaignError(RuntimeError):
    """A live point cannot proceed without violating a frozen invariant."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def require_evidence_root(path: Path, *, may_create: bool = False) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(EVIDENCE_PARENT.resolve())
    except ValueError as error:
        raise CampaignError("evidence root must remain under tool-of-rei/evidence") from error
    if may_create:
        if resolved.exists():
            raise CampaignError(f"evidence root already exists: {resolved}")
    elif not resolved.is_dir():
        raise CampaignError(f"evidence root does not exist: {resolved}")
    return resolved


def checked_config() -> WaveBenchConfig:
    config = load_config(WAVEBENCH_CONFIG)
    if config.power is not None:
        raise CampaignError("final calibration config must not contain a [power] section")
    if config.source is None or not config.source.resource:
        raise CampaignError("DG4202 source is not configured")
    if config.safety_limits.max_source_vpp != 0.5:
        raise CampaignError("source safety ceiling must be exactly 0.5 Vpp")
    if config.connection.resource != "TCPIP::192.168.1.115::INSTR":
        raise CampaignError("RTM2032 resource changed")
    if config.source.resource != "TCPIP::192.168.1.127::INSTR":
        raise CampaignError("DG4202 resource changed")
    return config


def run_logged(command: list[str | Path], output: Path, *, timeout_s: float = 60.0) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        result = subprocess.run(
            [str(part) for part in command],
            cwd=PROJECT_ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    if result.returncode != 0:
        raise CampaignError(f"command failed with exit {result.returncode}: {command}")


def git_snapshot() -> dict[str, Any]:
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if branch != "main":
        raise CampaignError(f"final calibration requires main, found {branch!r}")
    status = subprocess.run(
        ["git", "status", "--short", "--branch"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"branch": branch, "head": head, "status": status}


def initialize(root: Path) -> dict[str, Any]:
    root = require_evidence_root(root, may_create=True)
    root.mkdir(parents=True)
    payload = {
        "format": "CycleScope final calibration campaign v2",
        "created_at": now_iso(),
        "status": "initialized",
        "git": git_snapshot(),
        "approved_decisions": {
            "formal_reference": "DG4202 CH1 50-ohm setting",
            "main_sweep_vpp": 0.1,
            "supported_source_max_vpp": core.SUPPORTED_SOURCE_MAX_VPP_V,
            "exclude_compressed_points": True,
            "training_anchors": 12,
            "independent_holdouts": 7,
            "amplitude_dependence_stop_percent": 1.0,
            "phase_compensation": False,
            "p4_dual_calibration_identity": True,
            "fpga_changes": False,
            "engineering_target_v": 0.003,
            "hard_limit_v": 0.005,
        },
        "electrical_invariants": {
            "dg_load_ohm": 50.0,
            "rtm_ch1_high_impedance": True,
            "rtm_ch2_high_impedance": True,
            "dp800_writes_forbidden": True,
            "configured_source_safety_ceiling_vpp": 0.5,
            "formal_source_max_vpp": core.SUPPORTED_SOURCE_MAX_VPP_V,
            "ch2_engineering_limit_vpp": 2.35,
            "ch2_immediate_stop_vpp": 2.5,
        },
        "case_catalog_sha256": core.canonical_sha256(core.catalog_payload()),
    }
    core.write_json(root / "campaign.json", payload, exclusive=True)
    core.write_json(root / "case-catalog.json", core.catalog_payload(), exclusive=True)
    core.write_sha256s(root)
    return payload


def assert_source_profile(
    profile: Any,
    *,
    expected_frequency_hz: float | None = None,
    expected_vpp: float | None = None,
    expected_function: str | None = None,
) -> None:
    status = profile.status
    failures: list[str] = []
    if status.output != "OFF":
        failures.append("DG CH1 output is not OFF")
    if profile.load_ohm is None or not math.isclose(profile.load_ohm, 50.0, abs_tol=1e-12):
        failures.append("DG CH1 load is not exactly 50 ohm")
    if status.offset_v is None or not math.isclose(status.offset_v, 0.0, abs_tol=1e-12):
        failures.append("DG CH1 offset is not 0 V")
    if status.amplitude_unit != "VPP" or status.amplitude is None:
        failures.append("DG CH1 amplitude is not readable VPP")
    elif not 0.0 < status.amplitude <= 0.5:
        failures.append("DG CH1 amplitude is outside 0..0.5 Vpp")
    if status.frequency_mode != "FIX" or status.sweep_enabled != "OFF":
        failures.append("DG CH1 is not fixed-frequency with sweep OFF")
    if profile.polarity != "NORMAL":
        failures.append("DG CH1 polarity is not NORMAL")
    if profile.noise_enabled or profile.burst_enabled or profile.modulation_enabled:
        failures.append("DG CH1 noise/burst/modulation is enabled")
    if expected_function is not None and status.function != expected_function:
        failures.append(f"DG function {status.function!r} != {expected_function!r}")
    if expected_frequency_hz is not None and (
        status.frequency_hz is None
        or not math.isclose(status.frequency_hz, expected_frequency_hz, abs_tol=0.1)
    ):
        failures.append("DG frequency readback mismatch")
    if expected_vpp is not None and (
        status.amplitude is None
        or not math.isclose(status.amplitude, expected_vpp, abs_tol=1e-9)
    ):
        failures.append("DG amplitude readback mismatch")
    if failures:
        raise CampaignError("; ".join(failures))


def start_mirror(output_dir: Path, *, duration_s: float, keep_frames: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = output_dir.parent / "mirror-capture.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("wb")
    command = [
        WAVEBENCH_PYTHON,
        MIRROR_SCRIPT,
        "--bind",
        MIRROR_BIND,
        "--port",
        str(MIRROR_PORT),
        "--expected-source",
        FPGA_IP,
        "--seconds",
        f"{duration_s:.6g}",
        "--output-dir",
        output_dir,
        "--keep-complete-frames",
        str(keep_frames),
    ]
    process = subprocess.Popen(
        [str(part) for part in command],
        cwd=PROJECT_ROOT,
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    return process, stream


def finish_mirror(process: subprocess.Popen[bytes], stream: Any, *, timeout_s: float) -> None:
    try:
        result = process.wait(timeout=timeout_s)
    finally:
        stream.close()
    if result != 0:
        raise CampaignError(f"passive mirror exited with {result}")


def terminate_mirror(process: subprocess.Popen[bytes] | None, stream: Any) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
    if stream is not None and not stream.closed:
        stream.close()


def mirror_frame_records(mirror_dir: Path) -> tuple[list[dict[str, Any]], np.ndarray]:
    summary = core.read_json(mirror_dir / "summary.json")
    if summary.get("network_writes") != 0:
        raise CampaignError("mirror capture reported a network write")
    counts = summary.get("counts", {})
    if not isinstance(counts, dict):
        raise CampaignError("mirror counts are malformed")
    forbidden = (
        "unexpected_source",
        "invalid_cslp",
        "bad_cslp_crc",
        "invalid_wave_geometry",
        "invalid_wave_payload_bytes",
        "bad_reassembled_length",
    )
    if any(int(counts.get(key, 0)) != 0 for key in forbidden):
        raise CampaignError("mirror protocol counters contain errors")
    kept = summary.get("frames")
    if not isinstance(kept, list) or not kept:
        raise CampaignError("mirror summary contains no kept frames")
    arrays = np.load(mirror_dir / "complete-frames-s16le.npy", allow_pickle=False)
    if arrays.ndim != 2 or arrays.shape != (len(kept), core.FRAME_SAMPLES):
        raise CampaignError("mirror frame array does not match summary")

    groups: dict[tuple[int, int, int], dict[str, Any]] = {}
    with (mirror_dir / "packets.jsonl").open("r", encoding="utf-8") as stream:
        for line in stream:
            packet = json.loads(line)
            if int(packet.get("message_type", -1)) != 0x20 or int(packet.get("crc_ok", 0)) != 1:
                continue
            key = (
                int(packet["session_id"]),
                int(packet["config_id"]),
                int(packet["frame_id"]),
            )
            group = groups.setdefault(
                key,
                {
                    "chunk_indices": set(),
                    "first_received_unix_ns": int(packet["received_unix_ns"]),
                    "last_received_unix_ns": int(packet["received_unix_ns"]),
                    "core_flags": set(),
                    "metadata": packet,
                },
            )
            group["chunk_indices"].add(int(packet["chunk_index"]))
            group["first_received_unix_ns"] = min(
                int(group["first_received_unix_ns"]), int(packet["received_unix_ns"])
            )
            group["last_received_unix_ns"] = max(
                int(group["last_received_unix_ns"]), int(packet["received_unix_ns"])
            )
            group["core_flags"].add(int(packet["flags"]) & ~0x0003)

    records: list[dict[str, Any]] = []
    for index, item in enumerate(kept):
        key = (int(item["session_id"]), int(item["config_id"]), int(item["frame_id"]))
        group = groups.get(key)
        if group is None or group["chunk_indices"] != set(range(12)):
            raise CampaignError(f"kept mirror frame is not exactly complete: {key}")
        if len(group["core_flags"]) != 1:
            raise CampaignError(f"frame core flags changed between chunks: {key}")
        metadata = group["metadata"]
        records.append(
            {
                "array_index": index,
                "session_id": key[0],
                "config_id": key[1],
                "frame_id": key[2],
                "first_received_unix_ns": int(group["first_received_unix_ns"]),
                "last_received_unix_ns": int(group["last_received_unix_ns"]),
                "core_flags": next(iter(group["core_flags"])),
                "calibration_id": int(metadata["calibration_id"]),
                "scale_uv_per_lsb": int(metadata["scale_uv_per_lsb"]),
                "offset_uv": int(metadata["offset_uv"]),
                "filter_profile": int(metadata["filter_profile"]),
                "sample_rate_hz": int(metadata["sample_rate_hz"]),
                "frame_sample_count": int(metadata["frame_sample_count"]),
            }
        )
    return records, arrays


def select_mirror_frames(
    mirror_dir: Path,
    output_dir: Path,
    *,
    count: int,
    after_unix_ns: int | None = None,
    before_unix_ns: int | None = None,
) -> tuple[list[dict[str, Any]], Path]:
    records, arrays = mirror_frame_records(mirror_dir)
    eligible = [
        record
        for record in records
        if (after_unix_ns is None or record["first_received_unix_ns"] >= after_unix_ns)
        and (before_unix_ns is None or record["last_received_unix_ns"] <= before_unix_ns)
    ]
    if len(eligible) < count:
        raise CampaignError(f"only {len(eligible)} eligible mirror frames, need {count}")
    selected = eligible[-count:]
    core.validate_mirror_metadata(selected)
    selected_arrays = np.stack([arrays[int(record["array_index"])] for record in selected])
    path = output_dir / "selected-frames-s16le.npy"
    np.save(path, selected_arrays, allow_pickle=False)
    core.write_json(output_dir / "selected-frames.json", {"frames": selected})
    return selected, path


def scope_window(frequency_hz: float) -> float:
    return 0.002 if frequency_hz < 50_000.0 else 0.0002


def choose_vertical_scale(source_vpp_v: float) -> float:
    # M2 measured Ksrc≈2 and Gamp≈4.6, so the DG-setting-to-CH2 gain is
    # currently about 9.2 V/V.  Budget 10 V/V to keep both traces visible;
    # this changes only RTM vertical scale, never either channel termination.
    expected_ch2_vpp = source_vpp_v * 10.0
    required = max(expected_ch2_vpp / 5.0, 0.002)
    for exponent in range(-4, 2):
        for base in (1.0, 2.0, 5.0):
            candidate = base * (10.0**exponent)
            if candidate >= required:
                return candidate
    raise CampaignError("required RTM vertical scale is unsupported")


def predicted_ch2_gate(
    root: Path,
    case: core.CalibrationCase,
    *,
    active_stop_vpp: float = 2.5,
    authorization: str | None = None,
) -> dict[str, Any]:
    """Refuse energizing a high point when M2 predicts unsafe CH2 voltage."""

    ratios: list[float] = []
    evidence: list[dict[str, Any]] = []
    for candidate in core.CASES:
        if candidate.role != "safety":
            continue
        point = root / "points" / candidate.case_id
        if not point.is_dir():
            continue
        core.exact_point_analysis(root, candidate.case_id)
        point_record = core.read_json(point / "point.json")
        ch2_vpp = float(point_record["scope"]["ch2_raw_vpp_v"])
        ratio = ch2_vpp / candidate.source_vpp_v
        ratios.append(ratio)
        evidence.append(
            {"case_id": candidate.case_id, "ch2_vpp_v": ch2_vpp, "ratio_v_per_v": ratio}
        )
    if case.source_vpp_v > 0.1 and len(ratios) != 3:
        raise CampaignError("all three passing M2 low-amplitude points are required first")
    maximum_ratio = max(ratios) if ratios else 10.0
    prediction_ratio = float(np.median(ratios)) if ratios else 10.0
    predicted_vpp = prediction_ratio * case.source_vpp_v
    predicted_with_margin = predicted_vpp * 1.01
    pre_energize_limit = (
        2.35 if active_stop_vpp <= 2.5 else active_stop_vpp
    )
    result = {
        "basis": evidence,
        "maximum_observed_dg_setting_to_ch2_gain": maximum_ratio,
        "median_dg_setting_to_ch2_gain_used": prediction_ratio,
        "source_vpp_v": case.source_vpp_v,
        "predicted_ch2_vpp_v": predicted_vpp,
        "margin_factor": 1.01,
        "predicted_with_margin_vpp_v": predicted_with_margin,
        "engineering_limit_vpp_v": 2.35,
        "legacy_immediate_stop_vpp_v": 2.5,
        "active_immediate_stop_vpp_v": active_stop_vpp,
        "authorization": authorization,
        "pre_energize_limit_vpp_v": pre_energize_limit,
        "pass": predicted_with_margin <= pre_energize_limit,
    }
    if not result["pass"]:
        raise CampaignError(
            f"pre-energize CH2 prediction {predicted_with_margin:.6f} Vpp "
            f"exceeds active {pre_energize_limit:.6f} Vpp gate; DG remains OFF"
        )
    return result


def archive_scope_package(
    package_dir: Path, point_dir: Path, channels: tuple[int, ...]
) -> Path:
    destination = point_dir / "wavebench" / "raw" / package_dir.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_dir, destination)
    if any(not (destination / f"ch{channel}.npy").is_file() for channel in channels):
        raise CampaignError("archived WaveBench package is missing requested NPY files")
    return destination


def capture_scope(
    config: WaveBenchConfig,
    point_dir: Path,
    *,
    channels: tuple[int, ...] = SCOPE_CHANNELS,
    label: str,
    time_range_s: float,
    vertical_scale_v_per_div: float,
    expected_frequency_hz: float | None,
) -> dict[str, Any]:
    capture_config = replace(
        config,
        waveform=replace(
            config.waveform,
            points="DEF",
            time_range_s=time_range_s,
            expected_frequency_hz=expected_frequency_hz,
            target_cycles=(
                None if expected_frequency_hz is None else expected_frequency_hz * time_range_s
            ),
            window_frequency_hz=expected_frequency_hz,
            vertical_scale_v_per_div=vertical_scale_v_per_div,
            target_vpp=None,
        ),
        output=replace(
            config.output,
            save_csv=True,
            save_npy=True,
            save_json=True,
            save_commands_log=True,
            save_screenshot=True,
        ),
    )
    logger = CommandLogger()
    base = ScopeService(config=capture_config, logger=logger)
    session = base.open_session()
    started_ns = time.time_ns()
    try:
        service = ScopeService(config=capture_config, logger=logger, session=session)
        before = {channel: service.require_high_impedance(channel) for channel in channels}
        result = service.capture_waveforms(channels=list(channels), label=label)
        after = {channel: service.require_high_impedance(channel) for channel in channels}
    finally:
        session.close()
    finished_ns = time.time_ns()
    archived = archive_scope_package(result.package_dir, point_dir, channels)
    metadata = core.read_json(archived / "metadata.json")
    operation = metadata.get("operation", {})
    if (
        operation.get("channels") != list(channels)
        or operation.get("trigger_mode") != "single_acquisition"
    ):
        raise CampaignError("WaveBench metadata does not prove the requested acquisition")
    if result.screenshot_path is None or not (archived / "screenshot.png").is_file():
        raise CampaignError("qualitative RTM screenshot is missing")
    return {
        "started_unix_ns": started_ns,
        "finished_unix_ns": finished_ns,
        "package": str(archived),
        "original_wavebench_package": str(result.package_dir),
        "couplings_before": {str(key): value for key, value in before.items()},
        "couplings_after": {str(key): value for key, value in after.items()},
        "time_range_s": time_range_s,
        "vertical_scale_v_per_div": vertical_scale_v_per_div,
        "screenshot_numeric_use": False,
        "ch2_raw_vpp_v": float(np.ptp(result.waveforms[2].voltages_v)),
    }


def readonly_preflight(root: Path) -> dict[str, Any]:
    root = require_evidence_root(root)
    directory = root / "preflight"
    if directory.exists():
        raise CampaignError("formal preflight evidence already exists")
    directory.mkdir(parents=True)
    config = checked_config()

    source_logger = CommandLogger(directory / "source-readonly-commands.log")
    source_base = SourceService(config=config, logger=source_logger)
    source_session = source_base.open_session()
    try:
        source = SourceService(config=config, logger=source_logger, session=source_session)
        source_profile = source.channel_profile(SOURCE_CHANNEL)
        assert_source_profile(source_profile)
    finally:
        source_session.close()

    scope_logger = CommandLogger(directory / "scope-readonly-commands.log")
    scope_base = ScopeService(config=config, logger=scope_logger)
    scope_session = scope_base.open_session()
    try:
        scope = ScopeService(config=config, logger=scope_logger, session=scope_session)
        couplings = {
            str(channel): scope.require_high_impedance(channel) for channel in SCOPE_CHANNELS
        }
        scope_idn = scope.idn()
    finally:
        scope_session.close()

    run_logged(
        [WAVEBENCH_CLI, "power", "status", "--config", POWER_READONLY_CONFIG],
        directory / "dp800-readonly-status.txt",
        timeout_s=30.0,
    )
    mirror_process: subprocess.Popen[bytes] | None = None
    mirror_stream: Any = None
    try:
        mirror_process, mirror_stream = start_mirror(
            directory / "mirror", duration_s=4.0, keep_frames=96
        )
        finish_mirror(mirror_process, mirror_stream, timeout_s=12.0)
        mirror_stream = None
    finally:
        terminate_mirror(mirror_process, mirror_stream)
    records, _arrays = mirror_frame_records(directory / "mirror")
    selected = records[-64:]
    mirror_identity = core.validate_mirror_metadata(selected)
    payload = {
        "format": "CycleScope final calibration readonly preflight v1",
        "created_at": now_iso(),
        "git": git_snapshot(),
        "source_profile": source_profile.as_dict(),
        "scope_idn": scope_idn,
        "scope_couplings": couplings,
        "dp800_operation": "wavebench power status only",
        "dp800_writes": False,
        "mirror": mirror_identity,
        "mirror_network_writes": 0,
        "fpga_changes": False,
        "pass": True,
    }
    core.write_json(directory / "preflight.json", payload)
    core.write_sha256s(directory)
    return payload


def set_source_sine_while_off(source: SourceService) -> dict[str, Any]:
    """Leave HARM safely while output is OFF, then restore 50-ohm semantics."""

    before = source.channel_profile(SOURCE_CHANNEL)
    assert_source_profile(before)
    if before.status.function not in {"HARM", "HARMONIC"}:
        source.set_function(SOURCE_CHANNEL, "SIN")
        return {"method": "normal", "before": before.as_dict()}

    driver = source.session
    transport = getattr(driver, "transport", None)
    if transport is None:
        raise CampaignError("HARM-to-SIN transition requires the active DG session")
    transport.write(f":OUTP{SOURCE_CHANNEL}:LOAD INF")
    try:
        temporary = source.channel_profile(SOURCE_CHANNEL)
        if temporary.status.output != "OFF" or temporary.load_ohm is not None:
            raise CampaignError("DG did not enter output-OFF high-impedance semantics")
        source.set_function(SOURCE_CHANNEL, "SIN")
    finally:
        transport.write(f":OUTP{SOURCE_CHANNEL}:LOAD 50")
    after = source.channel_profile(SOURCE_CHANNEL)
    assert_source_profile(after, expected_function="SIN")
    return {
        "method": "output-off temporary high-impedance HARM exit",
        "before": before.as_dict(),
        "after": after.as_dict(),
    }


def prepare_source(
    source: SourceService,
    case: core.CalibrationCase,
) -> dict[str, Any]:
    if case.frequency_hz is None or case.source_vpp_v <= 0.0:
        raise CampaignError("cannot configure source for a zero case")
    before = source.channel_profile(SOURCE_CHANNEL)
    assert_source_profile(before)
    function_transition = set_source_sine_while_off(source)
    source.set_amplitude_vpp(SOURCE_CHANNEL, case.source_vpp_v)
    source.set_frequency(SOURCE_CHANNEL, float(case.frequency_hz))
    configured = source.channel_profile(SOURCE_CHANNEL)
    assert_source_profile(
        configured,
        expected_frequency_hz=float(case.frequency_hz),
        expected_vpp=case.source_vpp_v,
        expected_function="SIN",
    )
    return {
        "before": before.as_dict(),
        "function_transition": function_transition,
        "configured": configured.as_dict(),
    }


def capture_zero(root: Path) -> dict[str, Any]:
    root = require_evidence_root(root)
    case = core.CASES_BY_ID["m2-zero"]
    point_dir = root / "points" / case.case_id
    if point_dir.exists():
        raise CampaignError(f"point already exists: {point_dir}")
    point_dir.mkdir(parents=True)
    config = checked_config()
    source_logger = CommandLogger(point_dir / "source-commands.log")
    source_base = SourceService(config=config, logger=source_logger)
    source_session = source_base.open_session()
    mirror_process: subprocess.Popen[bytes] | None = None
    mirror_stream: Any = None
    try:
        source = SourceService(config=config, logger=source_logger, session=source_session)
        profile = source.channel_profile(SOURCE_CHANNEL)
        assert_source_profile(profile)
        mirror_process, mirror_stream = start_mirror(
            point_dir / "mirror", duration_s=8.0, keep_frames=192
        )
        time.sleep(0.5)
        scope = capture_scope(
            config,
            point_dir,
            label="final_cal_m2_zero",
            time_range_s=0.005,
            vertical_scale_v_per_div=0.02,
            expected_frequency_hz=None,
        )
        finish_mirror(mirror_process, mirror_stream, timeout_s=16.0)
        mirror_stream = None
        final_profile = source.channel_profile(SOURCE_CHANNEL)
        assert_source_profile(final_profile)
    except Exception:
        try:
            source = SourceService(config=config, logger=source_logger, session=source_session)
            source.set_output(SOURCE_CHANNEL, False)
        except Exception:
            pass
        raise
    finally:
        terminate_mirror(mirror_process, mirror_stream)
        source_session.close()

    selected, frames_path = select_mirror_frames(
        point_dir / "mirror", point_dir, count=128
    )
    analysis = core.analyze_zero(Path(scope["package"]), frames_path)
    analysis["case"] = asdict(case)
    analysis["mirror_identity"] = core.validate_mirror_metadata(selected)
    analysis["pass"] = True
    core.write_json(point_dir / "analysis.json", analysis)
    point = {
        "format": "CycleScope final calibration point v1",
        "created_at": now_iso(),
        "case": asdict(case),
        "source_profile": profile.as_dict(),
        "source_output_during_capture": "OFF",
        "scope": scope,
        "selected_frames": len(selected),
        "fpga_changes": False,
        "dp800_writes": False,
        "pass": True,
    }
    core.write_json(point_dir / "point.json", point)
    core.write_sha256s(point_dir)
    return point


def capture_tone(
    root: Path,
    case: core.CalibrationCase,
) -> dict[str, Any]:
    root = require_evidence_root(root)
    if core.CASES_BY_ID.get(case.case_id) != case:
        raise CampaignError("tone point is outside the active v2 calibration catalog")
    if case.frequency_hz is None or case.source_vpp_v <= 0.0:
        raise CampaignError("tone point requires nonzero frequency and amplitude")
    active_stop_vpp = 2.5
    authorization = None
    scope_channels = (2,) if case.milestone == "M6" else SCOPE_CHANNELS
    safety_prediction = predicted_ch2_gate(
        root,
        case,
        active_stop_vpp=active_stop_vpp,
        authorization=authorization,
    )
    point_dir = root / "points" / case.case_id
    if point_dir.exists():
        raise CampaignError(f"point already exists: {point_dir}")
    point_dir.mkdir(parents=True)
    config = checked_config()
    source_logger = CommandLogger(point_dir / "source-commands.log")
    source_base = SourceService(config=config, logger=source_logger)
    source_session = source_base.open_session()
    mirror_process: subprocess.Popen[bytes] | None = None
    mirror_stream: Any = None
    source_enabled = False
    on_unix_ns: int | None = None
    stable_unix_ns: int | None = None
    off_unix_ns: int | None = None
    source_states: dict[str, Any] = {}
    scope: dict[str, Any] | None = None
    try:
        source = SourceService(config=config, logger=source_logger, session=source_session)
        source_states.update(prepare_source(source, case))
        mirror_process, mirror_stream = start_mirror(
            point_dir / "mirror", duration_s=7.0, keep_frames=192
        )
        time.sleep(0.5)
        on_status = source.set_output(SOURCE_CHANNEL, True)
        source_enabled = True
        on_unix_ns = time.time_ns()
        source_states["on"] = on_status.as_dict()
        time.sleep(1.0)
        stable_unix_ns = time.time_ns()
        scope = capture_scope(
            config,
            point_dir,
            channels=scope_channels,
            label=f"final_cal_{case.case_id}",
            time_range_s=scope_window(float(case.frequency_hz)),
            vertical_scale_v_per_div=choose_vertical_scale(case.source_vpp_v),
            expected_frequency_hz=float(case.frequency_hz),
        )
        if float(scope["ch2_raw_vpp_v"]) > active_stop_vpp:
            off_unix_ns = time.time_ns()
            source_states["immediate_stop"] = source.set_output(SOURCE_CHANNEL, False).as_dict()
            source_enabled = False
            raise CampaignError(
                f"RTM CH2 exceeded active {active_stop_vpp:.3f} Vpp limit; "
                "source forced OFF"
            )
        finish_mirror(mirror_process, mirror_stream, timeout_s=16.0)
        mirror_stream = None
        off_unix_ns = time.time_ns()
        source_states["off"] = source.set_output(SOURCE_CHANNEL, False).as_dict()
        source_enabled = False
        final_profile = source.channel_profile(SOURCE_CHANNEL)
        assert_source_profile(
            final_profile,
            expected_frequency_hz=float(case.frequency_hz),
            expected_vpp=case.source_vpp_v,
            expected_function="SIN",
        )
        source_states["final"] = final_profile.as_dict()
    except Exception as error:
        if source_enabled:
            try:
                source = SourceService(config=config, logger=source_logger, session=source_session)
                off_unix_ns = time.time_ns()
                source_states["failsafe_off"] = source.set_output(
                    SOURCE_CHANNEL, False
                ).as_dict()
            except Exception as off_error:
                core.write_json(
                    point_dir / "failsafe-off-failure.json",
                    {"error_type": type(off_error).__name__, "error": str(off_error)},
                )
        core.write_json(
            point_dir / "failure.json",
            {
                "case": asdict(case),
                "error_type": type(error).__name__,
                "error": str(error),
                "source_states": source_states,
            },
        )
        core.write_sha256s(point_dir)
        raise
    finally:
        terminate_mirror(mirror_process, mirror_stream)
        source_session.close()

    if None in (on_unix_ns, stable_unix_ns, off_unix_ns) or scope is None:
        raise CampaignError("source/scope time window is incomplete")
    selected, frames_path = select_mirror_frames(
        point_dir / "mirror",
        point_dir,
        count=64,
        after_unix_ns=int(stable_unix_ns),
        before_unix_ns=int(off_unix_ns),
    )
    analysis = core.analyze_tone(
        case,
        Path(scope["package"]),
        frames_path,
        scope_channels=scope_channels,
        ch2_immediate_stop_vpp=active_stop_vpp,
        ch2_limit_authorization=authorization,
    )
    if case.milestone == "M4":
        frequency = int(case.frequency_hz)
        up = core.exact_point_analysis(
            root, f"m3-train-up-{frequency}Hz"
        )["ratios"]
        down = core.exact_point_analysis(
            root, f"m3-train-down-{frequency}Hz"
        )["ratios"]
        baseline = statistics.mean(
            (
                float(up["ke2e_code_per_v"]),
                float(down["ke2e_code_per_v"]),
            )
        )
        measured = float(analysis["ratios"]["ke2e_code_per_v"])
        deviation_percent = (measured / baseline - 1.0) * 100.0
        amplitude_gate = {
            "baseline_code_per_v": baseline,
            "measured_code_per_v": measured,
            "deviation_percent": deviation_percent,
            "hard_limit_percent": 1.0,
            "pass": abs(deviation_percent) <= 1.0,
        }
        analysis["m4_amplitude_dependence_gate"] = amplitude_gate
        if not amplitude_gate["pass"]:
            analysis["failures"].append(
                "M4 amplitude dependence exceeds the 1% hard stop"
            )
            analysis["pass"] = False
    analysis["mirror_identity"] = core.validate_mirror_metadata(selected)
    core.write_json(point_dir / "analysis.json", analysis)
    point = {
        "format": "CycleScope final calibration point v1",
        "created_at": now_iso(),
        "case": asdict(case),
        "source_states": source_states,
        "source_window": {
            "on_unix_ns": on_unix_ns,
            "stable_unix_ns": stable_unix_ns,
            "off_unix_ns": off_unix_ns,
        },
        "scope": scope,
        "scope_channels": list(scope_channels),
        "formal_reference": "DG4202 CH1 50-ohm setting",
        "rtm_ch1_connected": case.milestone != "M6",
        "pre_energize_ch2_prediction": safety_prediction,
        "ch2_limit_authorization": authorization,
        "selected_frames": len(selected),
        "fpga_changes": False,
        "dp800_writes": False,
        "pass": analysis["pass"],
    }
    core.write_json(point_dir / "point.json", point)
    core.write_sha256s(point_dir)
    if not analysis["pass"]:
        raise CampaignError("tone point analysis failed: " + "; ".join(analysis["failures"]))
    return point


def reanalyze_point(root: Path, case: core.CalibrationCase) -> dict[str, Any]:
    """Re-run pure analysis while preserving the previous report and SHA manifest."""

    root = require_evidence_root(root)
    if case.frequency_hz is None or case.source_vpp_v <= 0.0:
        raise CampaignError("offline tone reanalysis requires a tone case")
    point_dir = root / "points" / case.case_id
    core.verify_sha256s(point_dir)
    point_path = point_dir / "point.json"
    analysis_path = point_dir / "analysis.json"
    if not point_path.is_file() or not analysis_path.is_file():
        raise CampaignError("point lacks the original point/analysis pair")
    for source, destination in (
        (point_path, point_dir / "point-v1-before-reanalysis.json"),
        (analysis_path, point_dir / "analysis-v1-before-reanalysis.json"),
        (point_dir / "SHA256SUMS", point_dir / "SHA256SUMS-v1-before-reanalysis"),
    ):
        if destination.exists():
            raise CampaignError(f"reanalysis archive already exists: {destination}")
        shutil.copy2(source, destination)
    point = core.read_json(point_path)
    scope_package = Path(str(point["scope"]["package"]))
    frames_path = point_dir / "selected-frames-s16le.npy"
    selected_payload = core.read_json(point_dir / "selected-frames.json")
    selected = selected_payload.get("frames")
    if not isinstance(selected, list):
        raise CampaignError("selected frame record is malformed")
    analysis = core.analyze_tone(case, scope_package, frames_path)
    analysis["mirror_identity"] = core.validate_mirror_metadata(selected)
    analysis["offline_reanalysis"] = {
        "performed_at": now_iso(),
        "raw_capture_reused": True,
        "instrument_writes": False,
        "reason": "2.35 Vpp is an engineering target; 2.5 Vpp remains the hard stop",
        "previous_analysis": "analysis-v1-before-reanalysis.json",
    }
    point["pass"] = analysis["pass"]
    point["offline_reanalysis"] = analysis["offline_reanalysis"]
    core.write_json(analysis_path, analysis)
    core.write_json(point_path, point)
    core.write_sha256s(point_dir)
    return {"case_id": case.case_id, "pass": analysis["pass"], "warnings": analysis["warnings"]}


def group_case_ids(group: str) -> tuple[str, ...]:
    if group == "m2-low":
        return tuple(case.case_id for case in core.CASES if case.role == "safety")
    if group == "m3-up":
        return tuple(case.case_id for case in core.CASES if case.direction == "up")
    if group == "m3-down":
        return tuple(case.case_id for case in core.CASES if case.direction == "down")
    if group == "m4-minline":
        return tuple(case.case_id for case in core.CASES if "m4-minline" in case.case_id)
    if group == "m4-cross-low":
        return tuple(
            case.case_id
            for case in core.CASES
            if "m4-cross" in case.case_id and case.source_vpp_v in {0.05, 0.25}
        )
    if group == "m6-holdout":
        return core.HOLDOUT_CASE_IDS
    raise CampaignError(f"unsupported group: {group}")


def capture_group(
    root: Path,
    group: str,
    *,
    resume: bool,
) -> dict[str, Any]:
    root = require_evidence_root(root)
    records: list[dict[str, Any]] = []
    failure: dict[str, str] | None = None
    for case_id in group_case_ids(group):
        try:
            point_dir = root / "points" / case_id
            if point_dir.exists() and resume:
                core.exact_point_analysis(root, case_id)
                records.append(
                    {"case_id": case_id, "status": "already-passing"}
                )
                continue
            case = core.CASES_BY_ID[case_id]
            capture_tone(root, case)
            records.append({"case_id": case_id, "status": "captured-pass"})
        except Exception as error:
            failure = {
                "case_id": case_id,
                "error_type": type(error).__name__,
                "error": str(error),
            }
            records.append(
                {
                    "case_id": case_id,
                    "status": "stopped-fail",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
            break
    report = {
        "format": "CycleScope final calibration group result v2",
        "group": group,
        "records": records,
        "failure": failure,
        "remaining_case_ids_not_run": [
            case_id
            for case_id in group_case_ids(group)
            if case_id not in {record["case_id"] for record in records}
        ],
        "pass": failure is None,
    }
    destination = root / "groups" / f"{group}.json"
    core.write_json(destination, report)
    if failure is not None:
        raise CampaignError(
            f"group {group} stopped at {failure['case_id']}: "
            f"{failure['error']}"
        )
    return report


def archive_precondition_failure(root: Path, case_id: str) -> dict[str, Any]:
    """Move a proven pre-energize failure aside without deleting its evidence."""

    root = require_evidence_root(root)
    if case_id not in core.CASES_BY_ID:
        raise CampaignError("precondition archive case is outside the active catalog")
    point_dir = root / "points" / case_id
    group_path = root / "groups" / "m6-holdout.json"
    core.verify_sha256s(point_dir)
    failure = core.read_json(point_dir / "failure.json")
    group = core.read_json(group_path)
    point_files = {path.name for path in point_dir.iterdir() if path.is_file()}
    if point_files != {"SHA256SUMS", "failure.json", "source-commands.log"}:
        raise CampaignError("precondition failure contains energized/capture artifacts")
    if (
        failure.get("case", {}).get("case_id") != case_id
        or failure.get("error") != "DG CH1 output is not OFF"
        or failure.get("source_states") != {}
        or group.get("group") != "m6-holdout"
        or group.get("failure", {}).get("case_id") != case_id
        or group.get("pass") is not False
    ):
        raise CampaignError("precondition failure evidence is not the exact safe-stop case")

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S%z")
    destination = root / "failed-attempts" / f"{timestamp}-{case_id}"
    destination.mkdir(parents=True, exist_ok=False)
    point_manifest_sha = core.sha256_file(point_dir / "SHA256SUMS")
    group_sha = core.sha256_file(group_path)
    shutil.move(str(point_dir), str(destination / "point-attempt"))
    shutil.move(str(group_path), str(destination / "group-result.json"))
    archive = {
        "format": "CycleScope pre-energize calibration failure archive v1",
        "created_at": now_iso(),
        "case_id": case_id,
        "reason": "DG CH1 was already ON before the fixture attempted any source write",
        "source_energized_by_attempt": False,
        "scope_capture_created": False,
        "fpga_frames_selected": False,
        "point_manifest_sha256": point_manifest_sha,
        "group_result_sha256": group_sha,
        "source_restored_before_archive": True,
        "dp800_writes": False,
        "fpga_changes": False,
    }
    core.write_json(destination / "archive.json", archive)
    core.write_sha256s(destination)
    return {**archive, "archive_path": str(destination)}


def archive_scope_capture_failure(
    root: Path, case_id: str, failed_package: Path
) -> dict[str, Any]:
    """Preserve the obsolete two-channel attempt after CH1 was disconnected."""

    root = require_evidence_root(root)
    if case_id not in core.HOLDOUT_CASE_IDS:
        raise CampaignError("scope failure archive is restricted to M6 holdouts")
    point_dir = root / "points" / case_id
    group_path = root / "groups" / "m6-holdout.json"
    core.verify_sha256s(point_dir)
    failure = core.read_json(point_dir / "failure.json")
    group = core.read_json(group_path)
    if (point_dir / "point.json").exists() or (point_dir / "analysis.json").exists():
        raise CampaignError("cannot archive a completed calibration point as a retry")
    if (
        failure.get("case", {}).get("case_id") != case_id
        or failure.get("error") != "invalid waveform point count: 0"
        or group.get("group") != "m6-holdout"
        or group.get("failure", {}).get("case_id") != case_id
        or group.get("pass") is not False
    ):
        raise CampaignError("scope failure evidence is not the exact zero-point case")
    source_log = (point_dir / "source-commands.log").read_text(encoding="utf-8")
    if "\twrite\t:OUTP1 OFF" not in source_log or "\tresponse\tOFF" not in source_log:
        raise CampaignError("source command log does not prove failsafe OFF")
    mirror = core.read_json(point_dir / "mirror" / "summary.json")
    if int(mirror.get("complete_frames", 0)) <= 0:
        raise CampaignError("failed attempt does not contain a valid passive mirror capture")

    failed_package = failed_package.resolve()
    raw_root = Path(checked_config().output.directory).resolve()
    try:
        failed_package.relative_to(raw_root)
    except ValueError as error:
        raise CampaignError("failed WaveBench package is outside the configured raw root") from error
    metadata = core.read_json(failed_package / "metadata.partial.json")
    if (
        metadata.get("operation", {}).get("label") != f"final_cal_{case_id}"
        or metadata.get("error", {}).get("message") != "invalid waveform point count: 0"
    ):
        raise CampaignError("failed WaveBench package identity mismatch")

    restore_records = sorted(
        (root / "restores").glob("restore-*/restore.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not restore_records:
        raise CampaignError("source restore evidence is missing")
    latest_restore = core.read_json(restore_records[-1])
    if (
        latest_restore.get("pass") is not True
        or latest_restore.get("profile", {}).get("status", {}).get("output") != "OFF"
    ):
        raise CampaignError("latest source restore does not prove OFF")

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S%z")
    destination = root / "failed-attempts" / f"{timestamp}-{case_id}-rtm-zero-points"
    destination.mkdir(parents=True, exist_ok=False)
    point_manifest_sha = core.sha256_file(point_dir / "SHA256SUMS")
    group_sha = core.sha256_file(group_path)
    shutil.copytree(failed_package, destination / "wavebench-failed")
    shutil.move(str(point_dir), str(destination / "point-attempt"))
    shutil.move(str(group_path), str(destination / "group-result.json"))
    archive = {
        "format": "CycleScope obsolete M6 two-channel attempt archive v1",
        "created_at": now_iso(),
        "case_id": case_id,
        "reason": (
            "the old fixture required RTM CH1, but CH1 is intentionally not connected; "
            "formal M6 reference is the DG4202 setting and the revised fixture uses CH2 only"
        ),
        "source_energized_by_attempt": True,
        "source_failsafe_off_proven": True,
        "source_restored_before_archive": True,
        "scope_capture_valid": False,
        "rtm_ch1_connected": False,
        "retry_fixture_scope_channels": [2],
        "passive_mirror_complete_frames": int(mirror["complete_frames"]),
        "point_manifest_sha256": point_manifest_sha,
        "group_result_sha256": group_sha,
        "wavebench_failed_package_original": str(failed_package),
        "dp800_writes": False,
        "fpga_changes": False,
    }
    core.write_json(destination / "archive.json", archive)
    core.write_sha256s(destination)
    return {**archive, "archive_path": str(destination)}


def restore_source(root: Path) -> dict[str, Any]:
    root = require_evidence_root(root)
    directory = root / "final-restore"
    if directory.exists():
        suffix = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S%z")
        directory = root / "restores" / f"restore-{suffix}"
    directory.mkdir(parents=True, exist_ok=False)
    config = checked_config()
    logger = CommandLogger(directory / "source-commands.log")
    base = SourceService(config=config, logger=logger)
    session = base.open_session()
    try:
        source = SourceService(config=config, logger=logger, session=session)
        source.set_output(SOURCE_CHANNEL, False)
        function_transition = set_source_sine_while_off(source)
        source.set_amplitude_vpp(SOURCE_CHANNEL, 0.05)
        source.set_frequency(SOURCE_CHANNEL, 100_000.0)
        profile = source.channel_profile(SOURCE_CHANNEL)
        assert_source_profile(
            profile,
            expected_frequency_hz=100_000.0,
            expected_vpp=0.05,
            expected_function="SIN",
        )
    except Exception as error:
        core.write_json(
            directory / "failure.json",
            {"error_type": type(error).__name__, "error": str(error)},
        )
        core.write_sha256s(directory)
        raise
    finally:
        session.close()
    payload = {
        "format": "CycleScope final calibration source restore v1",
        "profile": profile.as_dict(),
        "function_transition": function_transition,
        "dp800_writes": False,
        "fpga_changes": False,
        "pass": True,
    }
    core.write_json(directory / "restore.json", payload)
    core.write_sha256s(directory)
    return payload


def audit_450_safety(root: Path) -> dict[str, Any]:
    """Prove the accepted 450 mVpp points are unsafe before any source write."""

    root = require_evidence_root(root)
    directory = root / "safety-audits" / "m4-cross-450"
    directory.mkdir(parents=True, exist_ok=False)
    config = checked_config()
    logger = CommandLogger(directory / "source-readonly-commands.log")
    base = SourceService(config=config, logger=logger)
    session = base.open_session()
    try:
        source = SourceService(config=config, logger=logger, session=session)
        profile = source.channel_profile(SOURCE_CHANNEL)
        assert_source_profile(profile)
    finally:
        session.close()
    records: list[dict[str, Any]] = []
    for frequency in (10_000, 200_000, 500_000):
        basis: list[dict[str, Any]] = []
        for direction in ("up", "down"):
            case_id = f"m3-train-{direction}-{frequency}Hz"
            core.exact_point_analysis(root, case_id)
            point = core.read_json(root / "points" / case_id / "point.json")
            raw_vpp = float(point["scope"]["ch2_raw_vpp_v"])
            ratio = raw_vpp / 0.1
            basis.append(
                {
                    "case_id": case_id,
                    "measured_ch2_raw_vpp_v": raw_vpp,
                    "dg_setting_to_ch2_ratio": ratio,
                }
            )
        conservative_ratio = max(item["dg_setting_to_ch2_ratio"] for item in basis)
        predicted = conservative_ratio * 0.45
        records.append(
            {
                "case_id": f"m4-cross-{frequency}Hz-450mVpp",
                "frequency_hz": float(frequency),
                "source_vpp_v": 0.45,
                "basis": basis,
                "predicted_ch2_raw_vpp_v": predicted,
                "hard_stop_vpp_v": 2.5,
                "safe_to_energize": predicted <= 2.5,
            }
        )
    payload = {
        "format": "CycleScope final calibration 450 mVpp pre-energize safety audit v1",
        "created_at": now_iso(),
        "source_profile_readonly": profile.as_dict(),
        "source_energized": False,
        "source_writes": False,
        "dp800_writes": False,
        "fpga_changes": False,
        "records": records,
        "all_points_rejected_before_energize": all(
            not record["safe_to_energize"] for record in records
        ),
        "safety_gate_pass": all(not record["safe_to_energize"] for record in records),
        "measurement_complete": False,
    }
    core.write_json(directory / "safety-audit.json", payload)
    core.write_sha256s(directory)
    return payload


def recheck_input_load(root: Path) -> dict[str, Any]:
    """Safely recheck whether the DG setting now appears 1:1 at RTM CH1."""

    root = require_evidence_root(root)
    suffix = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S%z")
    case = core.CalibrationCase(
        case_id=f"diagnostic-input-load-100000Hz-020mVpp-{suffix}",
        milestone="M4-SAFETY",
        role="diagnostic",
        frequency_hz=100_000.0,
        source_vpp_v=0.020,
    )
    capture_tone(root, case)
    point_dir = root / "points" / case.case_id
    analysis = core.read_json(point_dir / "analysis.json")
    ksrc = float(analysis["ratios"]["ksrc_v_per_v"])
    load_restored = 0.9 <= ksrc <= 1.1
    predicted_450_ch2 = float(
        analysis["scope"]["ch2"]["known_frequency_fit"]["fundamental_vpp"]
    ) / case.source_vpp_v * 0.45
    verdict = {
        "format": "CycleScope input-load recheck v1",
        "created_at": now_iso(),
        "case_id": case.case_id,
        "dg_load_ohm": 50.0,
        "rtm_channels_high_impedance": True,
        "dg_setting_to_rtm_ch1_ratio": ksrc,
        "one_to_one_target": {"minimum": 0.9, "maximum": 1.1},
        "input_load_restored": load_restored,
        "predicted_450mvpp_ch2_vpp": predicted_450_ch2,
        "hard_stop_ch2_vpp": 2.5,
        "safe_to_attempt_450mvpp":
            load_restored and predicted_450_ch2 <= 2.35,
        "source_final_state": "OFF",
        "dp800_writes": False,
        "fpga_changes": False,
    }
    core.write_json(point_dir / "input-load-verdict.json", verdict)
    core.write_sha256s(point_dir)
    return verdict


def audit_m4_stop(root: Path) -> dict[str, Any]:
    """Freeze the required stop after the authorized 450 mVpp point fails."""

    root = require_evidence_root(root)
    group_audit = core.verify_sha256s(root / "groups")
    point_dir = root / "points" / "m4-cross-10000Hz-450mVpp"
    point_audit = core.verify_sha256s(point_dir)
    group = core.read_json(root / "groups" / "m4-cross-450.json")
    analysis = core.read_json(point_dir / "analysis.json")
    point = core.read_json(point_dir / "point.json")
    mirror = core.read_json(point_dir / "mirror" / "summary.json")
    restore_dirs = sorted((root / "restores").glob("restore-*"))
    if not restore_dirs:
        raise CampaignError("no post-failure source restore evidence")
    restore_dir = restore_dirs[-1]
    restore_audit = core.verify_sha256s(restore_dir)
    restore = core.read_json(restore_dir / "restore.json")
    amplitude_gate = analysis.get("m4_amplitude_dependence_gate")
    if (
        group.get("pass") is not False
        or analysis.get("pass") is not False
        or point.get("pass") is not False
        or not isinstance(amplitude_gate, dict)
        or amplitude_gate.get("pass") is not False
        or abs(float(amplitude_gate.get("deviation_percent", 0.0))) <= 1.0
        or group.get("remaining_case_ids_not_run")
        != [
            "m4-cross-200000Hz-450mVpp",
            "m4-cross-500000Hz-450mVpp",
        ]
        or int(mirror.get("network_writes", -1)) != 0
    ):
        raise CampaignError("M4 stop evidence is incomplete or inconsistent")
    profile = restore.get("profile", {})
    status = profile.get("status", {})
    if (
        restore.get("pass") is not True
        or status.get("output") != "OFF"
        or status.get("function") != "SIN"
        or not math.isclose(float(status.get("frequency_hz", 0.0)), 100_000.0)
        or not math.isclose(float(status.get("amplitude", 0.0)), 0.05)
        or not math.isclose(float(profile.get("load_ohm", 0.0)), 50.0)
    ):
        raise CampaignError("DG post-failure restore is not the frozen safe state")
    if any(
        (root / name).exists()
        for name in ("fit-v1", "fit-v2", "holdout-v1", "holdout-v2")
    ):
        raise CampaignError("M5/M6 artifacts exist despite the mandatory M4 stop")

    payload = {
        "format": "CycleScope final calibration mandatory M4 stop audit v1",
        "created_at": now_iso(),
        "campaign_status": "STOPPED_M4_AMPLITUDE_DEPENDENCE",
        "campaign_can_continue": False,
        "failed_case_id": "m4-cross-10000Hz-450mVpp",
        "end_to_end_deviation_percent": float(
            amplitude_gate["deviation_percent"]
        ),
        "hard_limit_percent": 1.0,
        "scope_ch2_fundamental_vpp": float(
            analysis["scope"]["ch2"]["known_frequency_fit"]
            ["fundamental_vpp"]
        ),
        "scope_ch2_wavebench_thd_ratio": float(
            analysis["scope"]["ch2"]["wavebench_fft"]["thd_ratio"]
        ),
        "fpga_fit_thd_ratio_median": float(
            analysis["adc"]["fit_thd_ratio"]["median"]
        ),
        "remaining_450_points_not_run": group[
            "remaining_case_ids_not_run"
        ],
        "m5_fit_created": False,
        "m6_holdouts_read": False,
        "source_restored": True,
        "source_restore_evidence": str(restore_dir.relative_to(root)),
        "dp800_writes": False,
        "fpga_changes": False,
        "network_writes_from_mirror": 0,
        "evidence_audits": {
            "groups_manifest_sha256": group_audit["manifest_sha256"],
            "point_manifest_sha256": point_audit["manifest_sha256"],
            "restore_manifest_sha256": restore_audit["manifest_sha256"],
        },
        "required_resolution": (
            "change/repair the analog input/gain path and restart the affected "
            "calibration campaign; do not fit a two-dimensional correction"
        ),
    }
    core.write_json(root / "campaign-stop.json", payload)
    root_manifest = core.write_sha256s(root)
    payload["root_sha256s_manifest"] = core.sha256_file(root_manifest)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("preflight")
    subparsers.add_parser("capture-zero")
    point = subparsers.add_parser("capture-point")
    point.add_argument("--case-id", choices=sorted(core.CASES_BY_ID))
    reanalyze = subparsers.add_parser("reanalyze-point")
    reanalyze.add_argument("--case-id", choices=sorted(core.CASES_BY_ID))
    group = subparsers.add_parser("capture-group")
    group.add_argument(
        "--group",
        required=True,
        choices=(
            "m2-low",
            "m3-up",
            "m3-down",
            "m4-minline",
            "m4-cross-low",
            "m6-holdout",
        ),
    )
    group.add_argument("--resume", action="store_true")
    subparsers.add_parser("amend-scope-v2")
    archive = subparsers.add_parser("archive-precondition-failure")
    archive.add_argument("--case-id", choices=sorted(core.HOLDOUT_CASE_IDS), required=True)
    scope_archive = subparsers.add_parser("archive-scope-capture-failure")
    scope_archive.add_argument(
        "--case-id", choices=sorted(core.HOLDOUT_CASE_IDS), required=True
    )
    scope_archive.add_argument("--failed-package", type=Path, required=True)
    m3_draft = subparsers.add_parser("summarize-m3")
    m3_draft.add_argument("--output", type=Path)
    m4_diagnosis = subparsers.add_parser("diagnose-m4-failure")
    m4_diagnosis.add_argument("--output", type=Path)
    fit = subparsers.add_parser("fit")
    fit.add_argument("--output", type=Path)
    holdout = subparsers.add_parser("validate-holdout")
    holdout.add_argument("--fit-dir", type=Path)
    holdout.add_argument("--output", type=Path)
    subparsers.add_parser("restore-source")
    subparsers.add_parser("audit-450-safety")
    subparsers.add_parser("recheck-input-load")
    subparsers.add_parser("audit-m4-stop")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "init":
        result = initialize(args.evidence_root)
    elif args.command == "preflight":
        result = readonly_preflight(args.evidence_root)
    elif args.command == "capture-zero":
        result = capture_zero(args.evidence_root)
    elif args.command == "capture-point":
        result = capture_tone(
            require_evidence_root(args.evidence_root), core.CASES_BY_ID[args.case_id]
        )
    elif args.command == "reanalyze-point":
        result = reanalyze_point(
            require_evidence_root(args.evidence_root), core.CASES_BY_ID[args.case_id]
        )
    elif args.command == "capture-group":
        result = capture_group(
            args.evidence_root,
            args.group,
            resume=args.resume,
        )
    elif args.command == "amend-scope-v2":
        root = require_evidence_root(args.evidence_root)
        result = core.build_scope_amendment(
            root, root / core.SCOPE_AMENDMENT_DIRECTORY
        )
    elif args.command == "archive-precondition-failure":
        result = archive_precondition_failure(
            args.evidence_root, args.case_id
        )
    elif args.command == "archive-scope-capture-failure":
        result = archive_scope_capture_failure(
            args.evidence_root, args.case_id, args.failed_package
        )
    elif args.command == "summarize-m3":
        root = require_evidence_root(args.evidence_root)
        output = args.output or (root / "m3-draft-v1")
        result = core.build_m3_draft(root, output)
    elif args.command == "diagnose-m4-failure":
        root = require_evidence_root(args.evidence_root)
        output = args.output or (root / "m4-failure-diagnosis-v1")
        result = core.build_m4_failure_diagnosis(root, output)
    elif args.command == "fit":
        root = require_evidence_root(args.evidence_root)
        output = args.output or (root / "fit-v2")
        result = core.build_fit(root, output)
    elif args.command == "validate-holdout":
        root = require_evidence_root(args.evidence_root)
        fit_dir = args.fit_dir or (root / "fit-v2")
        output = args.output or (root / "holdout-v2")
        result = core.validate_holdouts(root, fit_dir, output)
    elif args.command == "restore-source":
        result = restore_source(args.evidence_root)
    elif args.command == "audit-450-safety":
        result = audit_450_safety(args.evidence_root)
    elif args.command == "recheck-input-load":
        result = recheck_input_load(args.evidence_root)
    elif args.command == "audit-m4-stop":
        result = audit_m4_stop(args.evidence_root)
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
