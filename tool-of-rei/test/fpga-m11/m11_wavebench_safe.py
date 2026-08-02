#!/usr/bin/env python3
"""Fail-closed M11 WaveBench checks and bounded live acquisitions.

The zero-input paths keep DG4202 CH1 OFF, never write DP800, require both
RTM2032 channels to remain high impedance, and archive every WaveBench raw
package into the FPGA worktree evidence directory with size/SHA-256 checks.
"""

# ruff: noqa: E402 -- the adjacent WaveBench source tree is added deliberately.

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import hashlib
from importlib import metadata as importlib_metadata
import json
import math
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import time
from typing import Any

import numpy as np


FPGA_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = FPGA_ROOT.parent
WAVEBENCH_ROOT = WORKSPACE_ROOT / "tools" / "wavebench"
WAVEBENCH_SRC = WAVEBENCH_ROOT / "src"
if str(WAVEBENCH_SRC) not in sys.path:
    sys.path.insert(0, str(WAVEBENCH_SRC))

from wavebench.config import SafetyLimitsConfig, WaveBenchConfig, load_config
from wavebench.instruments.capabilities import require_capabilities
from wavebench.instruments.registry import resolve_instrument_descriptor
from wavebench.logging import CommandLogger
from wavebench.services.power_service import PowerService
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService
from wavebench.services.scope_service import ScopeService
from wavebench.services.source_service import SourceService


BASE_CONFIG = FPGA_ROOT / "tool-of-rei" / "private" / "m8-wavebench-safe.toml"
PLANS_DIR = Path(__file__).resolve().parent / "plans"
EVIDENCE_ROOT = (
    FPGA_ROOT / "tool-of-rei" / "evidence" / "m11-real-frontend-20260731"
)
WAVEBENCH_RAW_DIR = WAVEBENCH_ROOT / "data" / "raw"
MAX_SOURCE_VPP = 0.5
SOURCE_CHANNEL = 1
SCOPE_CHANNELS = (1, 2)
POWER_CHANNEL = 1
EXPECTED_SUPPLY_MIN_V = 4.75
EXPECTED_SUPPLY_MAX_V = 5.25
READONLY_PLAN_NAMES = {
    "00-readonly-preflight-ch1.toml",
    "01-readonly-preflight-ch2.toml",
}
SAFE_OFF_PLAN_NAME = "02-source-safe-off.toml"
READONLY_STEP_KINDS = {"source.status", "power.status"}
BASIC_SOURCE_FUNCTIONS = {"SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"}
SAFE_OFF_ACK = "M11_DG_CH1_50OHM_OUTPUT_OFF_NO_RESTORE_TO_ON"
LIVE_ACK = "M11_WIRING_DG50_RTM12_HIGHZ_DP800_READONLY"
NOMINAL_LOADED_GAIN = 4.515984016
ZERO_SCOPE_PROFILES: dict[str, dict[str, Any]] = {
    "wide": {
        "case_id": "b-zero-wide",
        "time_range_s": 0.002,
        "vertical_scale_v_per_div": 0.2,
        "spectrum_min_hz": 0.0,
        "spectrum_max_hz": 500_000.0,
        "max_fft_resolution_hz": 500.0,
        "require_dcl": False,
        "evidence_class": "legacy-wide-safety-smoke",
    },
    "noise-500k": {
        "case_id": "b-zero-noise-500k",
        "time_range_s": 0.005,
        "vertical_scale_v_per_div": 0.02,
        "spectrum_min_hz": 0.0,
        "spectrum_max_hz": 500_000.0,
        "max_fft_resolution_hz": 500.0,
        "require_dcl": True,
        "evidence_class": "formal-dcl-effective-band-zero",
    },
    "hf-spur": {
        "case_id": "b-zero-hf-spur",
        "time_range_s": 10e-6,
        "vertical_scale_v_per_div": 0.02,
        "spectrum_min_hz": 500_000.0,
        "spectrum_max_hz": None,
        "max_fft_resolution_hz": 200_000.0,
        "require_dcl": True,
        "evidence_class": "formal-dcl-high-frequency-oscillation-screen",
    },
}
LAN_TOOL = (
    FPGA_ROOT
    / "Zynq_7010_PS"
    / "cyclescope_cslp"
    / "tools"
    / "cslp_lan_stress.py"
)
PCAP_ANALYZER = (
    FPGA_ROOT
    / "Zynq_7010_PS"
    / "cyclescope_cslp"
    / "tools"
    / "cslp_pcap_analyze.py"
)
SOURCE_DATA_ROOT = FPGA_ROOT / "tool-of-rei" / "source_data"
TCPDUMP = Path("/usr/bin/tcpdump")
SUDO = Path("/usr/bin/sudo")
LAN_INTERFACE = "enp2s0"
FPGA_LAN_IP = "192.168.10.2"
HOST_LAN_IP = "192.168.10.4"
FPGA_LAN_PORT = 50000
HOST_LAN_PORT = 50001
WAVEBENCH_PYTHON = WAVEBENCH_ROOT / ".venv" / "bin" / "python"


class M11SafetyError(RuntimeError):
    """An M11 invariant is missing or contradicted by current evidence."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def current_branch() -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=FPGA_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def derived_config() -> WaveBenchConfig:
    """Load local resources without copying them and tighten M11 in memory."""

    base = load_config(BASE_CONFIG)
    if base.power is None:
        raise M11SafetyError("base private config has no [power] section")
    limits = SafetyLimitsConfig(
        max_source_vpp=MAX_SOURCE_VPP,
        max_power_voltage_v=base.safety_limits.max_power_voltage_v,
        max_power_current_limit_a=base.safety_limits.max_power_current_limit_a,
    )
    # WaveBench preserves a relative [output].directory from TOML.  Anchor it
    # explicitly so this safety entry behaves identically from the documented
    # FPGA-root working directory and from the WaveBench repository itself.
    output = replace(base.output, directory=WAVEBENCH_RAW_DIR)
    # Select the installed, capability-audited DP800 plugin only in memory.
    power = replace(base.power, driver="rigol.dp800")
    return replace(base, safety_limits=limits, output=output, power=power)


def _require_resource(value: str | None, label: str) -> None:
    if value is None or not value.strip():
        raise M11SafetyError(f"{label} resource is missing")


def plan_paths() -> list[Path]:
    paths = sorted(PLANS_DIR.glob("*.toml"))
    names = {path.name for path in paths}
    expected = READONLY_PLAN_NAMES | {SAFE_OFF_PLAN_NAME}
    if names != expected:
        raise M11SafetyError(
            "M11 plan set mismatch: "
            f"missing={sorted(expected - names)} extra={sorted(names - expected)}"
        )
    return [path for path in paths if path.name in READONLY_PLAN_NAMES]


def safe_off_plan_path() -> Path:
    plan_paths()
    return PLANS_DIR / SAFE_OFF_PLAN_NAME


def validate_readonly_plan(path: Path, config: WaveBenchConfig) -> dict[str, Any]:
    plan = load_run_plan(path)
    kinds = [step.kind for step in plan.steps]
    if not kinds or set(kinds) - READONLY_STEP_KINDS:
        raise M11SafetyError(f"{path.name}: non-readonly step present: {kinds}")
    if plan.restore.source_state:
        raise M11SafetyError(f"{path.name}: readonly plan must not request restore writes")
    expected_channel = 1 if path.name.startswith("00-") else 2
    if plan.safety.scope_guard_channel != expected_channel:
        raise M11SafetyError(
            f"{path.name}: scope guard must target CH{expected_channel}"
        )
    blocked = {item.strip().upper() for item in plan.safety.require_scope_coupling_not}
    if not {"DC", "AC"}.issubset(blocked):
        raise M11SafetyError(f"{path.name}: DC/AC 50-ohm aliases must be blocked")
    if plan.safety.allow_50ohm:
        raise M11SafetyError(f"{path.name}: allow_50ohm must remain false")
    RunService(config=config, logger=CommandLogger()).check(plan)
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "scope_guard_channel": expected_channel,
        "steps": kinds,
        "wavebench_run_check": "pass",
    }


def validate_safe_off_plan(config: WaveBenchConfig) -> dict[str, Any]:
    path = safe_off_plan_path()
    plan = load_run_plan(path)
    kinds = [step.kind for step in plan.steps]
    expected = [
        "source.status",
        "power.status",
        "source.output",
        "source.status",
        "power.status",
    ]
    if kinds != expected:
        raise M11SafetyError(f"{path.name}: exact safe-OFF sequence changed: {kinds}")
    output_steps = [step for step in plan.steps if step.kind == "source.output"]
    if len(output_steps) != 1 or output_steps[0].fields.get("state") != "off":
        raise M11SafetyError(f"{path.name}: only one source.output OFF is allowed")
    if plan.restore.source_state:
        raise M11SafetyError(f"{path.name}: safe-OFF must never restore output ON")
    if plan.safety.scope_guard_channel != 1:
        raise M11SafetyError(f"{path.name}: CH1 high-impedance guard is required")
    blocked = {item.strip().upper() for item in plan.safety.require_scope_coupling_not}
    if not {"DC", "AC"}.issubset(blocked) or plan.safety.allow_50ohm:
        raise M11SafetyError(f"{path.name}: scope 50-ohm bypass is forbidden")
    RunService(config=config, logger=CommandLogger()).check(plan)
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "scope_guard_channel": 1,
        "steps": kinds,
        "wavebench_run_check": "pass",
        "only_write": "source.output off",
        "restore_to_on": False,
    }


def validate_capabilities(config: WaveBenchConfig) -> dict[str, Any]:
    requirements = {
        "scope": (
            config.scope.driver,
            (
                "scope.idn",
                "scope.channel_coupling",
                "scope.capture_waveforms",
            ),
        ),
        "source": (
            config.source.driver if config.source is not None else "",
            (
                "source.idn",
                "source.status",
                "source.channel_profile",
                "source.coupling_profile",
                "source.burst_profile",
                "source.sweep_profile",
            ),
        ),
        "power": (
            config.power.driver if config.power is not None else "",
            ("power.idn", "power.status", "power.measurement", "power.protection"),
        ),
    }
    records: dict[str, Any] = {}
    for kind, (driver, capabilities) in requirements.items():
        descriptor = resolve_instrument_descriptor(driver, expected_kind=kind)
        require_capabilities(
            descriptor,
            capabilities,
            operation=f"CycleScope M11 {kind} preflight",
        )
        records[kind] = {
            "driver": descriptor.driver_id,
            "origin": descriptor.origin,
            "required_capabilities": list(capabilities),
        }
    return records


def validate_static(*, write_evidence: bool = True) -> dict[str, Any]:
    if current_branch() != "codex/FPGA":
        raise M11SafetyError("refusing to run outside codex/FPGA")
    if not BASE_CONFIG.is_file():
        raise M11SafetyError(f"base private config is missing: {BASE_CONFIG}")
    mode = stat.S_IMODE(BASE_CONFIG.stat().st_mode)
    if mode & 0o077:
        raise M11SafetyError("base private config must not be group/world accessible")

    config = derived_config()
    if config.safety_limits.max_source_vpp != MAX_SOURCE_VPP:
        raise M11SafetyError("derived config max_source_vpp must be exactly 0.5")
    if config.scope.driver != "rohde-schwarz.rtm2032":
        raise M11SafetyError("M11 requires reviewed rohde-schwarz.rtm2032 driver")
    if config.source is None or config.source.driver != "rigol.dg4202":
        raise M11SafetyError("M11 requires reviewed rigol.dg4202 driver")
    if config.power is None or config.power.driver != "rigol.dp800":
        raise M11SafetyError("M11 requires reviewed rigol.dp800 driver")
    if config.scope.default_channel != SOURCE_CHANNEL:
        raise M11SafetyError("scope default channel must remain CH1")
    if config.source.default_channel != SOURCE_CHANNEL:
        raise M11SafetyError("source default channel must remain CH1")
    if config.power.default_channel != POWER_CHANNEL:
        raise M11SafetyError("power default channel must remain CH1")
    _require_resource(config.connection.resource, "scope")
    _require_resource(config.source.resource, "source")
    _require_resource(config.power.resource, "power")
    if config.output.directory.resolve() != WAVEBENCH_RAW_DIR.resolve():
        raise M11SafetyError("WaveBench raw output must remain tools/wavebench/data/raw")
    if not (
        config.output.save_csv
        and config.output.save_npy
        and config.output.save_json
        and config.output.save_commands_log
    ):
        raise M11SafetyError("M11 requires CSV/NPY/JSON/commands.log evidence")
    if config.scope.reset_before_run:
        raise M11SafetyError("scope reset_before_run must remain false")

    plans = [validate_readonly_plan(path, config) for path in plan_paths()]
    plans.append(validate_safe_off_plan(config))
    capabilities = validate_capabilities(config)
    packages = {
        name: importlib_metadata.version(name)
        for name in (
            "wavebench",
            "wavebench-rigol-dg4000",
            "wavebench-rohde-schwarz-rtm2000",
            "wavebench-rigol-dp800",
        )
    }
    payload = {
        "format": "CycleScope M11 WaveBench offline safety check v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "offline_only": True,
        "instrument_io": False,
        "branch": "codex/FPGA",
        "base_config": {
            "path": str(BASE_CONFIG.resolve()),
            "sha256": sha256_file(BASE_CONFIG),
            "mode_octal": f"{mode:04o}",
            "resources_recorded_as_sha256_only": {
                "scope": sha256_text(config.connection.resource),
                "source": sha256_text(config.source.resource or ""),
                "power": sha256_text(config.power.resource or ""),
            },
        },
        "derived_config": {
            "materialized_to_disk": False,
            "max_source_vpp": config.safety_limits.max_source_vpp,
            "source_channel": SOURCE_CHANNEL,
            "scope_channels": list(SCOPE_CHANNELS),
            "power_channel": POWER_CHANNEL,
            "power_operations": [
                "idn",
                "status",
                "measurement",
                "protection_status",
            ],
            "power_writes_forbidden": True,
            "allow_50ohm": False,
        },
        "packages": packages,
        "capabilities": capabilities,
        "plans": plans,
        "pass": True,
    }
    if write_evidence:
        path = EVIDENCE_ROOT / "offline" / f"{now_stamp()}_m11-offline-check.json"
        write_json_exclusive(path, payload)
        payload["evidence_path"] = str(path)
    return payload


def _safe_float_close(value: float | None, expected: float, tolerance: float) -> bool:
    return value is not None and math.isfinite(value) and math.isclose(
        value, expected, rel_tol=0.0, abs_tol=tolerance
    )


def evaluate_preflight(
    *,
    source_profile: Any,
    source_coupling: Any,
    scope_couplings: dict[int, str],
    power_status: Any,
    power_protection: Any,
) -> list[str]:
    failures: list[str] = []
    status = source_profile.status
    if status.output != "OFF":
        failures.append(f"DG CH1 output must be OFF, got {status.output!r}")
    if source_profile.load_ohm is None or not math.isclose(
        source_profile.load_ohm, 50.0, rel_tol=0.0, abs_tol=1e-9
    ):
        failures.append(f"DG CH1 load must remain 50 ohm, got {source_profile.load_ohm!r}")
    if status.amplitude_unit != "VPP":
        failures.append(f"DG CH1 amplitude unit must be VPP, got {status.amplitude_unit!r}")
    if not _safe_float_close(status.offset_v, 0.0, 1e-9):
        failures.append(f"DG CH1 offset must be 0 V, got {status.offset_v!r}")
    if status.function.upper() not in BASIC_SOURCE_FUNCTIONS:
        failures.append(f"DG CH1 function is not safely restorable: {status.function!r}")
    if status.frequency_mode != "FIX" or status.sweep_enabled != "OFF":
        failures.append("DG CH1 must be FIX mode with sweep OFF")
    if source_profile.noise_enabled:
        failures.append("DG CH1 noise must be OFF")
    if source_profile.burst_enabled:
        failures.append("DG CH1 burst must be OFF")
    if source_profile.modulation_enabled:
        failures.append("DG CH1 modulation must be OFF")
    if (
        source_coupling.frequency_enabled
        or source_coupling.phase_enabled
        or source_coupling.amplitude_enabled
    ):
        failures.append("DG channel coupling must be fully OFF before CH1 writes")

    for channel in SCOPE_CHANNELS:
        coupling = scope_couplings.get(channel)
        if coupling is None:
            failures.append(f"RTM CH{channel} high-impedance evidence is missing")

    if power_status.output != "ON":
        failures.append(f"DP800 CH1 must already be ON, got {power_status.output!r}")
    set_voltage = power_status.set_voltage_v
    measured_voltage = power_status.measured_voltage_v
    if set_voltage is None or not EXPECTED_SUPPLY_MIN_V <= set_voltage <= EXPECTED_SUPPLY_MAX_V:
        failures.append(
            f"DP800 CH1 set voltage {set_voltage!r} V is outside formal single-5V gate"
        )
    if (
        measured_voltage is None
        or not EXPECTED_SUPPLY_MIN_V <= measured_voltage <= EXPECTED_SUPPLY_MAX_V
    ):
        failures.append(
            f"DP800 CH1 measured voltage {measured_voltage!r} V is outside formal single-5V gate"
        )
    if power_protection.ovp_tripped != "NO":
        failures.append(f"DP800 CH1 OVP trip is active: {power_protection.ovp_tripped!r}")
    if power_protection.ocp_tripped != "NO":
        failures.append(f"DP800 CH1 OCP trip is active: {power_protection.ocp_tripped!r}")
    return failures


def readonly_preflight() -> dict[str, Any]:
    static = validate_static(write_evidence=True)
    config = derived_config()
    directory = EVIDENCE_ROOT / "preflight" / now_stamp()
    directory.mkdir(parents=True, exist_ok=False)

    verify_records: list[dict[str, Any]] = []
    for path in plan_paths():
        plan = load_run_plan(path)
        records = RunService(config=config, logger=CommandLogger()).verify(plan)
        verify_records.append(
            {
                "plan": str(path.resolve()),
                "plan_sha256": sha256_file(path),
                "records": [
                    {
                        "instrument": record.instrument,
                        "resource_sha256": sha256_text(record.resource),
                        "idn": record.idn,
                    }
                    for record in records
                ],
            }
        )

    source_logger = CommandLogger(directory / "source-readonly-commands.log")
    source = SourceService(config=config, logger=source_logger)
    source_idn = source.idn()
    source_profile = source.channel_profile(SOURCE_CHANNEL)
    source_coupling = source.coupling_profile()
    source_burst = source.burst_profile(SOURCE_CHANNEL)
    source_sweep = source.sweep_profile(SOURCE_CHANNEL)

    scope_logger = CommandLogger(directory / "scope-readonly-commands.log")
    scope = ScopeService(config=config, logger=scope_logger)
    scope_idn = scope.idn()
    scope_couplings = {
        channel: scope.require_high_impedance(channel, allow_50ohm=False)
        for channel in SCOPE_CHANNELS
    }

    power_logger = CommandLogger(directory / "power-readonly-commands.log")
    power = PowerService(config=config, logger=power_logger)
    power_idn = power.idn()
    power_status = power.status(POWER_CHANNEL)
    power_measurement = power.measurement(POWER_CHANNEL)
    power_protection = power.protection_status(POWER_CHANNEL)

    failures = evaluate_preflight(
        source_profile=source_profile,
        source_coupling=source_coupling,
        scope_couplings=scope_couplings,
        power_status=power_status,
        power_protection=power_protection,
    )
    if source_burst.enabled:
        failures.append("DG CH1 detailed burst profile is enabled")
    if source_sweep.enabled:
        failures.append("DG CH1 detailed sweep profile is enabled")

    payload = {
        "format": "CycleScope M11 live read-only preflight v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "read_only": True,
        "instrument_writes": False,
        "scope_capture": False,
        "fpga_control": False,
        "static_check_evidence": static.get("evidence_path"),
        "verify": verify_records,
        "source": {
            "idn": source_idn,
            "profile": source_profile.as_dict(),
            "coupling": source_coupling.as_dict(),
            "burst": source_burst.as_dict(),
            "sweep": source_sweep.as_dict(),
        },
        "scope": {
            "idn": scope_idn,
            "couplings": {str(key): value for key, value in scope_couplings.items()},
        },
        "power": {
            "idn": power_idn,
            "status": power_status.as_dict(),
            "measurement": {
                "channel": power_measurement.channel,
                "measured_voltage_v": power_measurement.measured_voltage_v,
                "measured_current_a": power_measurement.measured_current_a,
                "measured_power_w": power_measurement.measured_power_w,
            },
            "protection": power_protection.as_dict(),
            "allowed_operations": [
                "idn",
                "status",
                "measurement",
                "protection_status",
            ],
            "writes_forbidden": True,
        },
        "failures": failures,
        "pass": not failures,
    }
    path = directory / "preflight.json"
    write_json_exclusive(path, payload)
    payload["evidence_path"] = str(path)
    return payload


def _tree_manifest(root: Path) -> dict[str, dict[str, Any]]:
    return {
        str(path.relative_to(root)): {
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def archive_run(run_dir: Path, destination_root: Path) -> Path:
    runs_root = (WAVEBENCH_RAW_DIR.parent / "runs").resolve()
    source = run_dir.resolve()
    try:
        source.relative_to(runs_root)
    except ValueError as error:
        raise M11SafetyError(f"WaveBench run is outside data/runs: {source}") from error
    if not (source / "run.json").is_file():
        raise M11SafetyError(f"WaveBench run lacks run.json: {source}")
    destination = destination_root / source.name
    if destination.exists():
        raise M11SafetyError(f"archive destination exists: {destination}")
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    source_manifest = _tree_manifest(source)
    destination_manifest = _tree_manifest(destination)
    if source_manifest != destination_manifest:
        raise M11SafetyError("WaveBench run archive hash comparison failed")
    manifest_path = destination.with_name(destination.name + "-archive.json")
    write_json_exclusive(
        manifest_path,
        {
            "format": "CycleScope M11 verified WaveBench run archive v1",
            "source_run": str(source),
            "destination_run": str(destination),
            "files": destination_manifest,
            "copy_verified_by_size_and_sha256": True,
        },
    )
    return manifest_path


def _run_directories() -> set[Path]:
    root = WAVEBENCH_RAW_DIR.parent / "runs"
    if not root.exists():
        return set()
    return {path.resolve() for path in root.iterdir() if path.is_dir()}


def _raw_directories() -> set[Path]:
    if not WAVEBENCH_RAW_DIR.exists():
        return set()
    return {path.resolve() for path in WAVEBENCH_RAW_DIR.iterdir() if path.is_dir()}


def _select_new_scope_raw_packages(
    *,
    scope_result: dict[str, Any] | None,
    raw_before: set[Path],
    raw_after: set[Path],
) -> set[Path]:
    """Select the exact package returned by WaveBench for this acquisition."""

    if scope_result is None:
        return set()
    package_value = scope_result.get("package")
    if not isinstance(package_value, str) or not package_value:
        raise M11SafetyError("scope result does not identify its WaveBench raw package")
    package = Path(package_value).resolve()
    new_packages = raw_after - raw_before
    if package not in new_packages:
        raise M11SafetyError(
            "scope raw package was not created by the current acquisition: " f"{package}"
        )
    return {package}


def archive_raw_packages(packages: set[Path], destination_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in sorted(packages):
        try:
            source.relative_to(WAVEBENCH_RAW_DIR.resolve())
        except ValueError as error:
            raise M11SafetyError(f"WaveBench raw package is outside data/raw: {source}") from error
        destination = destination_root / source.name
        if destination.exists():
            raise M11SafetyError(f"raw archive destination exists: {destination}")
        shutil.copytree(source, destination, copy_function=shutil.copy2)
        source_manifest = _tree_manifest(source)
        destination_manifest = _tree_manifest(destination)
        if source_manifest != destination_manifest:
            raise M11SafetyError(f"raw archive hash comparison failed: {source.name}")
        records.append(
            {
                "source": str(source),
                "destination": str(destination),
                "files": destination_manifest,
                "copy_verified_by_size_and_sha256": True,
            }
        )
    return records


def _zero_scope_profile(name: str) -> dict[str, Any]:
    try:
        return dict(ZERO_SCOPE_PROFILES[name])
    except KeyError as error:
        raise M11SafetyError(f"unknown zero-input profile: {name!r}") from error


def _spectrum_metrics(
    values: np.ndarray,
    sample_rate_hz: float,
    *,
    band_min_hz: float,
    band_max_hz: float | None,
) -> dict[str, float | int]:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or values.size < 32 or not np.all(np.isfinite(values)):
        raise M11SafetyError("spectrum input must contain at least 32 finite samples")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise M11SafetyError("spectrum sample rate is invalid")
    nyquist_hz = sample_rate_hz / 2.0
    actual_max_hz = nyquist_hz if band_max_hz is None else float(band_max_hz)
    if (
        not math.isfinite(band_min_hz)
        or band_min_hz < 0
        or not math.isfinite(actual_max_hz)
        or actual_max_hz <= band_min_hz
        or actual_max_hz > nyquist_hz * (1.0 + 1e-9)
    ):
        raise M11SafetyError(
            f"invalid spectrum band {band_min_hz}..{actual_max_hz} Hz "
            f"for Nyquist {nyquist_hz} Hz"
        )

    centered = values - np.mean(values)
    window = np.hanning(values.size)
    window_sum = float(np.sum(window))
    window_power = float(np.sum(np.square(window)))
    transformed = np.fft.rfft(centered * window)
    frequencies = np.fft.rfftfreq(values.size, d=1.0 / sample_rate_hz)
    resolution_hz = float(sample_rate_hz / values.size)

    psd = np.square(np.abs(transformed)) / (sample_rate_hz * window_power)
    amplitude_peak = 2.0 * np.abs(transformed) / window_sum
    if values.size % 2 == 0:
        if psd.size > 2:
            psd[1:-1] *= 2.0
        amplitude_peak[-1] *= 0.5
    elif psd.size > 1:
        psd[1:] *= 2.0
    amplitude_peak[0] *= 0.5

    mask = (
        (frequencies >= float(band_min_hz))
        & (frequencies <= actual_max_hz)
        & (frequencies > 0.0)
    )
    if not np.any(mask):
        raise M11SafetyError("spectrum band contains no non-DC FFT bins")
    band_indices = np.flatnonzero(mask)
    peak_index = int(band_indices[np.argmax(amplitude_peak[mask])])
    return {
        "samples": int(values.size),
        "sample_rate_hz": float(sample_rate_hz),
        "nyquist_hz": float(nyquist_hz),
        "resolution_hz": resolution_hz,
        "band_min_hz": float(band_min_hz),
        "band_max_hz": float(actual_max_hz),
        "band_bin_count": int(np.count_nonzero(mask)),
        "band_integrated_rms": float(math.sqrt(float(np.sum(psd[mask])) * resolution_hz)),
        "max_spur_frequency_hz": float(frequencies[peak_index]),
        "max_spur_peak": float(amplitude_peak[peak_index]),
        "window_enbw_hz": float(sample_rate_hz * window_power / (window_sum**2)),
    }


def _scope_trace_metrics(
    path: Path,
    *,
    band_min_hz: float = 0.0,
    band_max_hz: float | None = None,
) -> dict[str, Any]:
    raw = np.load(path, allow_pickle=False)
    if raw.ndim != 2 or raw.shape[1] != 2 or raw.shape[0] < 2:
        raise M11SafetyError(f"invalid WaveBench NPY trace: {path}")
    times = np.asarray(raw[:, 0], dtype=np.float64)
    values = np.asarray(raw[:, 1], dtype=np.float64)
    if not np.all(np.isfinite(times)) or not np.all(np.isfinite(values)):
        raise M11SafetyError(f"non-finite WaveBench trace: {path}")
    differences = np.diff(times)
    if np.any(differences <= 0):
        raise M11SafetyError(f"non-increasing WaveBench time axis: {path}")
    sample_rate_hz = float(1.0 / np.median(differences))
    mean = float(np.mean(values))
    result = {
        "samples": int(values.size),
        "sample_rate_hz": sample_rate_hz,
        "mean_v": mean,
        "rms_ac_v": float(np.sqrt(np.mean(np.square(values - mean)))),
        "vpp_v": float(np.max(values) - np.min(values)),
        "min_v": float(np.min(values)),
        "max_v": float(np.max(values)),
    }
    result["spectrum"] = _spectrum_metrics(
        values,
        sample_rate_hz,
        band_min_hz=band_min_hz,
        band_max_hz=band_max_hz,
    )
    result["spectrum"]["unit"] = "V"
    return result


def _capture_zero_scope(
    config: WaveBenchConfig,
    label: str,
    profile_name: str = "wide",
) -> dict[str, Any]:
    profile = _zero_scope_profile(profile_name)
    started_ns = time.monotonic_ns()
    started_at = datetime.now().astimezone().isoformat()
    capture_config = replace(
        config,
        waveform=replace(
            config.waveform,
            points="DEF",
            time_range_s=float(profile["time_range_s"]),
            expected_frequency_hz=None,
            target_cycles=None,
            window_frequency_hz=None,
            vertical_scale_v_per_div=float(profile["vertical_scale_v_per_div"]),
            target_vpp=None,
        ),
    ).with_output_overrides(save_csv=True, save_npy=True, save_screenshot=True)
    logger = CommandLogger()
    base_service = ScopeService(config=capture_config, logger=logger)
    session = base_service.open_session()
    try:
        service = ScopeService(config=capture_config, logger=logger, session=session)
        before = {
            channel: service.require_high_impedance(channel, allow_50ohm=False)
            for channel in SCOPE_CHANNELS
        }
        result = service.capture_waveforms(channels=list(SCOPE_CHANNELS), label=label)
        after = {
            channel: service.require_high_impedance(channel, allow_50ohm=False)
            for channel in SCOPE_CHANNELS
        }
    finally:
        session.close()
    finished_ns = time.monotonic_ns()
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    operation = metadata.get("operation", {})
    failures: list[str] = []
    if profile["require_dcl"]:
        for phase, couplings in (("before", before), ("after", after)):
            for channel in SCOPE_CHANNELS:
                if str(couplings[channel]).upper() != "DCL":
                    failures.append(
                        f"RTM CH{channel} must remain DCL for {profile_name} ({phase})"
                    )
    if operation.get("label") != label:
        failures.append("scope metadata label does not match the requested acquisition")
    if operation.get("channels") != [1, 2]:
        failures.append("scope metadata does not bind CH1 and CH2")
    if operation.get("trigger_mode") != "single_acquisition":
        failures.append("scope metadata does not prove one shared acquisition")
    if result.screenshot_path is None or not result.screenshot_path.is_file():
        failures.append("scope screenshot is missing")
    metrics: dict[str, Any] = {}
    for channel in SCOPE_CHANNELS:
        path = Path(result.files[str(channel)]["npy"])
        metrics[str(channel)] = _scope_trace_metrics(
            path,
            band_min_hz=float(profile["spectrum_min_hz"]),
            band_max_hz=profile["spectrum_max_hz"],
        )
        if metrics[str(channel)]["spectrum"]["resolution_hz"] > float(
            profile["max_fft_resolution_hz"]
        ):
            failures.append(
                f"RTM CH{channel} FFT resolution exceeds "
                f"{profile['max_fft_resolution_hz']} Hz"
            )
    if metrics["1"]["vpp_v"] > 0.55:
        failures.append("RTM CH1 exceeds 0.55 Vpp safety gate")
    if metrics["2"]["vpp_v"] > 2.35:
        failures.append("RTM CH2 exceeds 2.35 Vpp safety gate")
    return {
        "started_at": started_at,
        "started_monotonic_ns": started_ns,
        "finished_at": datetime.now().astimezone().isoformat(),
        "finished_monotonic_ns": finished_ns,
        "profile_name": profile_name,
        "profile": profile,
        "scope_setup_restored": False,
        "package": str(result.package_dir.resolve()),
        "metadata": str(result.metadata_path.resolve()),
        "couplings_before": {str(key): value for key, value in before.items()},
        "couplings_after": {str(key): value for key, value in after.items()},
        "metrics": metrics,
        "failures": failures,
        "pass": not failures,
    }


def _packet_capture_command(pcap_path: Path) -> list[str]:
    return [
        str(SUDO),
        "-n",
        str(TCPDUMP),
        "-U",
        "-n",
        "-i",
        LAN_INTERFACE,
        "-s",
        "0",
        "-w",
        str(pcap_path),
        f"src host {FPGA_LAN_IP} and dst host {HOST_LAN_IP} and udp",
    ]


def _start_packet_capture(
    pcap_path: Path, log_path: Path
) -> tuple[subprocess.Popen[str], Any]:
    for executable in (SUDO, TCPDUMP):
        if not executable.is_file():
            raise M11SafetyError(f"packet capture executable is missing: {executable}")
    if pcap_path.exists() or log_path.exists():
        raise M11SafetyError("packet capture refuses to overwrite an existing artifact")
    log = log_path.open("x", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _packet_capture_command(pcap_path),
            cwd=FPGA_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if pcap_path.is_file() and pcap_path.stat().st_size >= 24:
                return process, log
            returncode = process.poll()
            if returncode is not None:
                raise M11SafetyError(
                    f"tcpdump exited before capture readiness with code {returncode}"
                )
            time.sleep(0.02)
        raise M11SafetyError("tcpdump did not create a complete pcap header within 3 seconds")
    except Exception:
        if "process" in locals() and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        log.close()
        raise


def _stop_packet_capture(
    process: subprocess.Popen[str], log: Any
) -> tuple[int | None, list[str]]:
    failures: list[str] = []
    try:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            failures.append("tcpdump did not stop within 5 seconds after SIGINT")
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait(timeout=3)
    finally:
        log.close()
    if returncode not in {0, 130}:
        failures.append(f"tcpdump exit code is {returncode}")
    return returncode, failures


def _analyze_packet_capture(
    pcap_path: Path,
    tcpdump_log_path: Path,
    lan_report_path: Path,
    output_path: Path,
    console_path: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    command = [
        str(WAVEBENCH_PYTHON),
        str(PCAP_ANALYZER),
        str(pcap_path),
        "--source-ip",
        FPGA_LAN_IP,
        "--destination-ip",
        HOST_LAN_IP,
        "--source-port",
        str(FPGA_LAN_PORT),
        "--destination-port",
        str(HOST_LAN_PORT),
        "--lan-report",
        str(lan_report_path),
        "--tcpdump-log",
        str(tcpdump_log_path),
        "--report",
        str(output_path),
    ]
    with console_path.open("x", encoding="utf-8") as console:
        result = subprocess.run(
            command,
            cwd=FPGA_ROOT,
            check=False,
            text=True,
            stdout=console,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    failures: list[str] = []
    report = None
    if output_path.is_file():
        report = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        failures.append("pcap analyzer report is missing")
    if result.returncode != 0:
        failures.append(f"pcap analyzer exit code is {result.returncode}")
    if report is not None and report.get("pass") is not True:
        failures.extend(f"pcap: {item}" for item in report.get("failures", []))
    return report, failures


def _archive_packet_source(
    point_dir: Path,
    *,
    pcap_path: Path,
    tcpdump_log_path: Path,
    pcap_report_path: Path,
    lan_report_path: Path,
    pcap_report: dict[str, Any] | None,
) -> dict[str, Any]:
    destination = SOURCE_DATA_ROOT / point_dir.name
    destination.mkdir(parents=True, exist_ok=False)
    source_files = {
        "wire.pcap": pcap_path,
        "tcpdump.log": tcpdump_log_path,
        "pcap-analysis.json": pcap_report_path,
        "lan-report.json": lan_report_path,
    }
    bindings: dict[str, Any] = {}
    for name, source in source_files.items():
        if not source.is_file():
            raise M11SafetyError(f"packet archive source is missing: {source}")
        target = destination / name
        shutil.copy2(source, target)
        if target.stat().st_size != source.stat().st_size or sha256_file(target) != sha256_file(
            source
        ):
            raise M11SafetyError(f"packet archive copy verification failed: {name}")
        bindings[name] = {
            "size": target.stat().st_size,
            "sha256": sha256_file(target),
        }
    manifest = {
        "format": "CycleScope M11 replay source packet archive v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "source_point_directory": point_dir.name,
        "capture_scope": (
            f"complete Ethernet frames for Zynq {FPGA_LAN_IP} -> host {HOST_LAN_IP} "
            "UDP traffic during the measurement LAN collection window"
        ),
        "interface": LAN_INTERFACE,
        "bpf_filter": f"src host {FPGA_LAN_IP} and dst host {HOST_LAN_IP} and udp",
        "snaplen": 0,
        "pcap_analysis_pass": bool(pcap_report and pcap_report.get("pass")),
        "packet_counts": None if pcap_report is None else pcap_report.get("counts"),
        "cslp_message_types": (
            None if pcap_report is None else pcap_report.get("cslp_message_types")
        ),
        "files": bindings,
        "replay_boundary": (
            "archive only; no ESP32-P4 operation was performed. Replay must use an "
            "isolated test network and account for captured L2/L3 addresses."
        ),
    }
    manifest_path = destination / "manifest.json"
    write_json_exclusive(manifest_path, manifest)
    sums = _write_sha256sums(destination)
    return {
        "directory": str(destination.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "sha256sums": str(sums.resolve()),
        "wire_pcap": str((destination / "wire.pcap").resolve()),
        "wire_pcap_sha256": bindings["wire.pcap"]["sha256"],
        "copy_verified_by_size_and_sha256": True,
        "pass": bool(pcap_report and pcap_report.get("pass")),
    }


def _capture_zero_lan(
    point_dir: Path,
    frames: int,
    *,
    frame_samples: int = 8192,
    activity_policy: str = "allow",
    expected_calibration_id: int = 0,
    expected_scale_uv_per_lsb: int = 488,
    expected_offset_uv: int = 0,
    archive_packets: bool = True,
    run_timeout_s: float = 15.0,
    progress_every: int | None = None,
) -> dict[str, Any]:
    if frame_samples not in {8192, 16384}:
        raise M11SafetyError("LAN frame samples must be 8192 or 16384")
    if activity_policy not in {"allow", "require"}:
        raise M11SafetyError(f"invalid LAN activity policy: {activity_policy!r}")
    if not 0 <= expected_calibration_id <= 0xFFFF:
        raise M11SafetyError("expected calibration ID must fit u16")
    if not 1 <= expected_scale_uv_per_lsb <= 0xFFFFFFFF:
        raise M11SafetyError("expected scale must fit nonzero u32")
    if not -0x80000000 <= expected_offset_uv <= 0x7FFFFFFF:
        raise M11SafetyError("expected offset must fit i32")
    if not math.isfinite(run_timeout_s) or run_timeout_s <= 0.0:
        raise M11SafetyError("LAN run timeout must be finite and positive")
    if progress_every is None:
        progress_every = frames
    if progress_every < 1:
        raise M11SafetyError("LAN progress interval must be positive")
    lan_dir = point_dir / "lan"
    capture_dir = lan_dir / "capture"
    report_path = lan_dir / "lan.json"
    log_path = lan_dir / "console.log"
    pcap_path = lan_dir / "wire.pcap"
    tcpdump_log_path = lan_dir / "tcpdump.log"
    pcap_report_path = lan_dir / "pcap-analysis.json"
    pcap_console_path = lan_dir / "pcap-analysis.log"
    lan_dir.mkdir(parents=True, exist_ok=False)
    command = [
        str(WAVEBENCH_PYTHON),
        str(LAN_TOOL),
        "--frame-samples",
        str(frame_samples),
        "--source-mode",
        "real-adc",
        "--activity-policy",
        activity_policy,
        "--overrange-policy",
        "reject",
        "--expected-calibration-id",
        str(expected_calibration_id),
        "--expected-scale-uv-per-lsb",
        str(expected_scale_uv_per_lsb),
        "--expected-offset-uv",
        str(expected_offset_uv),
        "--frames",
        str(frames),
        "--run-timeout",
        f"{run_timeout_s:g}",
        "--progress-every",
        str(progress_every),
        "--capture-dir",
        str(capture_dir),
        "--report",
        str(report_path),
    ]
    started_ns = time.monotonic_ns()
    started_at = datetime.now().astimezone().isoformat()
    capture_process = None
    capture_log = None
    capture_returncode = None
    capture_failures: list[str] = []
    if archive_packets:
        capture_process, capture_log = _start_packet_capture(pcap_path, tcpdump_log_path)
    try:
        with log_path.open("x", encoding="utf-8") as log:
            result = subprocess.run(
                command,
                cwd=FPGA_ROOT,
                check=False,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=max(25.0, run_timeout_s + 15.0),
            )
    finally:
        if capture_process is not None and capture_log is not None:
            capture_returncode, capture_failures = _stop_packet_capture(
                capture_process, capture_log
            )
    finished_ns = time.monotonic_ns()
    failures: list[str] = list(capture_failures)
    report: dict[str, Any] | None = None
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        failures.append("CSLP LAN report is missing")
    if result.returncode != 0:
        failures.append(f"CSLP LAN collector exit code is {result.returncode}")
    if report is not None and not report.get("pass"):
        failures.extend(f"LAN: {item}" for item in report.get("failures", []))
    frame_count = None
    if report is not None and isinstance(report.get("capture"), dict):
        frame_count = report["capture"].get("frame_count")
    if not isinstance(frame_count, int) or frame_count < frames:
        failures.append(f"LAN capture has {frame_count!r} frames; at least {frames} required")
    pcap_report = None
    packet_archive = None
    if archive_packets:
        if not pcap_path.is_file() or pcap_path.stat().st_size <= 24:
            failures.append("wire pcap is missing or contains no packets")
        elif report_path.is_file() and tcpdump_log_path.is_file():
            pcap_report, pcap_failures = _analyze_packet_capture(
                pcap_path,
                tcpdump_log_path,
                report_path,
                pcap_report_path,
                pcap_console_path,
            )
            failures.extend(pcap_failures)
            if pcap_report_path.is_file():
                packet_archive = _archive_packet_source(
                    point_dir,
                    pcap_path=pcap_path,
                    tcpdump_log_path=tcpdump_log_path,
                    pcap_report_path=pcap_report_path,
                    lan_report_path=report_path,
                    pcap_report=pcap_report,
                )
                if packet_archive.get("pass") is not True:
                    failures.append("source_data packet archive did not pass pcap analysis")
    return {
        "started_at": started_at,
        "started_monotonic_ns": started_ns,
        "finished_at": datetime.now().astimezone().isoformat(),
        "finished_monotonic_ns": finished_ns,
        "command": [
            str(WAVEBENCH_PYTHON),
            str(LAN_TOOL),
            "--frame-samples",
            str(frame_samples),
            "--source-mode",
            "real-adc",
            "--activity-policy",
            activity_policy,
            "--overrange-policy",
            "reject",
            "--expected-calibration-id",
            str(expected_calibration_id),
            "--expected-scale-uv-per-lsb",
            str(expected_scale_uv_per_lsb),
            "--expected-offset-uv",
            str(expected_offset_uv),
            "--frames",
            str(frames),
            "--run-timeout",
            f"{run_timeout_s:g}",
            "--progress-every",
            str(progress_every),
            "<local/default network parameters>",
        ],
        "capture_dir": str(capture_dir.resolve()),
        "report": str(report_path.resolve()),
        "console_log": str(log_path.resolve()),
        "returncode": result.returncode,
        "packet_capture_enabled": archive_packets,
        "tcpdump_returncode": capture_returncode,
        "wire_pcap": str(pcap_path.resolve()) if archive_packets else None,
        "pcap_report": str(pcap_report_path.resolve()) if archive_packets else None,
        "packet_archive": packet_archive,
        "frame_count": frame_count,
        "failures": failures,
        "pass": not failures,
    }


def _require_zero_profile_preflight(before: dict[str, Any], profile_name: str) -> None:
    profile = _zero_scope_profile(profile_name)
    if not profile["require_dcl"]:
        return
    scope = before.get("scope")
    couplings = scope.get("couplings") if isinstance(scope, dict) else None
    failures = []
    for channel in SCOPE_CHANNELS:
        value = None if not isinstance(couplings, dict) else couplings.get(str(channel))
        if str(value).upper() != "DCL":
            failures.append(f"RTM CH{channel} is {value!r}, expected DCL")
    if failures:
        raise M11SafetyError(
            f"zero-input profile {profile_name!r} requires dual DCL: " + "; ".join(failures)
        )


def _aggregate_metric(records: list[dict[str, float]], key: str) -> dict[str, float]:
    values = [float(record[key]) for record in records]
    return {
        "median": float(np.median(values)),
        "minimum": float(min(values)),
        "maximum": float(max(values)),
    }


def _adc_zero_metrics(capture_dir: Path) -> dict[str, Any]:
    manifest_path = capture_dir / "capture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "CycleScope CSLP independent complete frames v1":
        raise M11SafetyError("unsupported CSLP capture manifest for zero analysis")
    if manifest.get("partial"):
        raise M11SafetyError("zero-input CSLP capture is partial")
    sample_rate_hz = float(manifest.get("sample_rate_hz", 0))
    if not math.isclose(sample_rate_hz, 4_062_500.0, rel_tol=0.0, abs_tol=0.1):
        raise M11SafetyError("zero-input CSLP sample rate is not 4.0625 MS/s")
    frame_records = manifest.get("frames")
    if not isinstance(frame_records, list) or not frame_records:
        raise M11SafetyError("zero-input CSLP capture contains no frames")

    basic_records: list[dict[str, float]] = []
    spectrum_records: list[dict[str, float | int]] = []
    unique_codes: set[int] = set()
    global_min: int | None = None
    global_max: int | None = None
    total_samples = 0
    total_outliers = 0
    for expected_index, record in enumerate(frame_records):
        if not isinstance(record, dict) or record.get("frame_index") != expected_index:
            raise M11SafetyError("zero-input frame index is missing or non-contiguous")
        path = capture_dir / str(record.get("file", ""))
        raw = path.read_bytes()
        if len(raw) != 8192 * 2 or hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            raise M11SafetyError(f"zero-input frame size/hash mismatch: {path.name}")
        integer_values = np.frombuffer(raw, dtype="<i2")
        values = integer_values.astype(np.float64)
        mean = float(np.mean(values))
        centered = values - mean
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        outlier_threshold = max(8.0, 8.0 * 1.4826 * mad)
        outliers = int(np.count_nonzero(np.abs(values - median) > outlier_threshold))
        minimum = int(np.min(integer_values))
        maximum = int(np.max(integer_values))
        unique_codes.update(int(value) for value in np.unique(integer_values))
        global_min = minimum if global_min is None else min(global_min, minimum)
        global_max = maximum if global_max is None else max(global_max, maximum)
        total_samples += int(values.size)
        total_outliers += outliers
        basic_records.append(
            {
                "mean_code": mean,
                "rms_ac_code": float(np.sqrt(np.mean(np.square(centered)))),
                "vpp_code": float(maximum - minimum),
                "outlier_rate": float(outliers / values.size),
                "outlier_threshold_code": float(outlier_threshold),
            }
        )
        spectrum_records.append(
            _spectrum_metrics(
                values,
                sample_rate_hz,
                band_min_hz=0.0,
                band_max_hz=500_000.0,
            )
        )

    if manifest.get("frame_count") != len(frame_records):
        raise M11SafetyError("zero-input manifest frame count mismatch")
    worst_spur = max(spectrum_records, key=lambda item: float(item["max_spur_peak"]))
    return {
        "frame_count": len(frame_records),
        "sample_rate_hz": sample_rate_hz,
        "sample_min_code": global_min,
        "sample_max_code": global_max,
        "sample_unique_values": len(unique_codes),
        "total_samples": total_samples,
        "total_outliers": total_outliers,
        "outlier_rate": float(total_outliers / total_samples),
        "metrics": {
            key: _aggregate_metric(basic_records, key)
            for key in (
                "mean_code",
                "rms_ac_code",
                "vpp_code",
                "outlier_rate",
                "outlier_threshold_code",
            )
        },
        "spectrum_0_500k": {
            "unit": "code",
            "resolution_hz": float(spectrum_records[0]["resolution_hz"]),
            "integrated_rms_code": _aggregate_metric(
                spectrum_records, "band_integrated_rms"
            ),
            "max_spur_peak_code": _aggregate_metric(spectrum_records, "max_spur_peak"),
            "worst_spur_frequency_hz": float(worst_spur["max_spur_frequency_hz"]),
            "worst_spur_peak_code": float(worst_spur["max_spur_peak"]),
        },
        "raw_samples_modified": False,
        "outlier_policy": (
            "report-only robust median/MAD flag; threshold=max(8 code, "
            "8*1.4826*MAD); no replacement or deletion"
        ),
    }


def _zero_analysis(
    *,
    profile_name: str,
    scope_result: dict[str, Any],
    lan_result: dict[str, Any],
) -> dict[str, Any]:
    adc = _adc_zero_metrics(Path(lan_result["capture_dir"]))
    profile = _zero_scope_profile(profile_name)
    analysis: dict[str, Any] = {
        "format": "CycleScope M11 zero-input analysis v1",
        "profile_name": profile_name,
        "evidence_class": profile["evidence_class"],
        "scope": scope_result["metrics"],
        "adc": adc,
        "raw_samples_modified": False,
        "formal_input_equivalent_acceptance": "deferred until measured Gamp/probe correction",
    }
    if profile_name == "noise-500k":
        ch1_spectrum = scope_result["metrics"]["1"]["spectrum"]
        ch2_spectrum = scope_result["metrics"]["2"]["spectrum"]
        nominal_input_rms = float(ch2_spectrum["band_integrated_rms"]) / NOMINAL_LOADED_GAIN
        nominal_input_spur = float(ch2_spectrum["max_spur_peak"]) / NOMINAL_LOADED_GAIN
        analysis["nominal_gain_reference_only"] = {
            "loaded_gain_v_per_v": NOMINAL_LOADED_GAIN,
            "source": "user-confirmed nominal RF/RG/Rs; not measured gain",
            "ch1_direct_band_rms_v": float(ch1_spectrum["band_integrated_rms"]),
            "ch1_direct_max_spur_peak_v": float(ch1_spectrum["max_spur_peak"]),
            "ch2_referred_input_band_rms_v": nominal_input_rms,
            "ch2_referred_input_max_spur_peak_v": nominal_input_spur,
            "engineering_targets": {
                "input_equivalent_rms_v_max": 0.0005,
                "input_equivalent_spur_peak_v_max": 0.001,
            },
            "provisional_target_pass": (
                nominal_input_rms <= 0.0005 and nominal_input_spur <= 0.001
            ),
        }
    return analysis


def _write_sha256sums(root: Path) -> Path:
    output = root / "SHA256SUMS"
    files = [path for path in sorted(root.rglob("*")) if path.is_file() and path != output]
    with output.open("x", encoding="utf-8") as stream:
        for path in files:
            stream.write(f"{sha256_file(path)}  {path.relative_to(root)}\n")
    return output


def lan_preflight(
    acknowledgement: str,
    *,
    instrument_preflight: dict[str, Any] | None = None,
    frame_samples: int = 8192,
    expected_calibration_id: int = 0,
    expected_scale_uv_per_lsb: int = 488,
    expected_offset_uv: int = 0,
) -> dict[str, Any]:
    if acknowledgement != LIVE_ACK:
        raise M11SafetyError(f"lan-preflight requires --acknowledge {LIVE_ACK!r}")
    before = instrument_preflight or readonly_preflight()
    if not before.get("pass"):
        raise M11SafetyError(
            "LAN preflight requires a passing instrument preflight: "
            + "; ".join(before.get("failures", []))
        )
    directory = EVIDENCE_ROOT / "preflight" / f"{now_stamp()}_lan-smoke"
    directory.mkdir(parents=True, exist_ok=False)
    result = _capture_zero_lan(
        directory,
        2,
        frame_samples=frame_samples,
        expected_calibration_id=expected_calibration_id,
        expected_scale_uv_per_lsb=expected_scale_uv_per_lsb,
        expected_offset_uv=expected_offset_uv,
        archive_packets=False,
    )
    payload = {
        "format": "CycleScope M11 CSLP LAN preflight v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "acknowledgement": acknowledgement,
        "instrument_preflight_evidence": before.get("evidence_path"),
        "source_expected_output": "OFF",
        "source_writes": False,
        "scope_writes": False,
        "power_writes": False,
        "fpga_action": "CSLP HELLO/CONFIG/ENABLE/DISABLE over LAN",
        "frame_samples": frame_samples,
        "expected_calibration_identity": {
            "calibration_id": expected_calibration_id,
            "scale_uv_per_lsb": expected_scale_uv_per_lsb,
            "offset_uv": expected_offset_uv,
            "calibrated": expected_calibration_id != 0,
        },
        "lan": result,
        "failures": list(result["failures"]),
        "pass": result["pass"],
    }
    path = directory / "lan-preflight.json"
    write_json_exclusive(path, payload)
    sums = _write_sha256sums(directory)
    payload["evidence_path"] = str(path)
    payload["sha256sums"] = str(sums)
    return payload


def zero_live(
    acknowledgement: str,
    *,
    frames: int,
    profile_name: str = "wide",
    expected_calibration_id: int = 0,
    expected_scale_uv_per_lsb: int = 488,
    expected_offset_uv: int = 0,
) -> dict[str, Any]:
    if acknowledgement != LIVE_ACK:
        raise M11SafetyError(f"zero-live requires --acknowledge {LIVE_ACK!r}")
    if frames < 64:
        raise M11SafetyError("M11 zero-live requires at least 64 complete LAN frames")
    profile = _zero_scope_profile(profile_name)
    before = readonly_preflight()
    if not before.get("pass"):
        raise M11SafetyError(
            "zero-live requires a passing read-only preflight: "
            + "; ".join(before.get("failures", []))
        )
    _require_zero_profile_preflight(before, profile_name)
    lan_smoke = lan_preflight(
        acknowledgement,
        instrument_preflight=before,
        expected_calibration_id=expected_calibration_id,
        expected_scale_uv_per_lsb=expected_scale_uv_per_lsb,
        expected_offset_uv=expected_offset_uv,
    )
    if not lan_smoke.get("pass"):
        raise M11SafetyError(
            "zero-live refused because CSLP LAN preflight failed: "
            + "; ".join(lan_smoke.get("failures", []))
        )

    stamp = now_stamp()
    case_id = str(profile["case_id"])
    scope_label = f"cyclescope_m11_{case_id.replace('-', '_')}_{stamp}"
    point_dir = EVIDENCE_ROOT / "points" / f"{stamp}_{case_id}"
    point_dir.mkdir(parents=True, exist_ok=False)
    raw_before = _raw_directories()
    config = derived_config()
    scope_result: dict[str, Any] | None = None
    lan_result: dict[str, Any] | None = None
    operation_errors: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        scope_future = executor.submit(
            _capture_zero_scope,
            config,
            scope_label,
            profile_name,
        )
        lan_future = executor.submit(
            _capture_zero_lan,
            point_dir,
            frames,
            expected_calibration_id=expected_calibration_id,
            expected_scale_uv_per_lsb=expected_scale_uv_per_lsb,
            expected_offset_uv=expected_offset_uv,
        )
        try:
            scope_result = scope_future.result()
        except Exception as error:
            operation_errors.append(f"scope: {type(error).__name__}: {error}")
        try:
            lan_result = lan_future.result()
        except Exception as error:
            operation_errors.append(f"LAN: {type(error).__name__}: {error}")

    raw_after = _raw_directories()
    archive_failures: list[str] = []
    raw_archives: list[dict[str, Any]] = []
    try:
        matching_raw = _select_new_scope_raw_packages(
            scope_result=scope_result,
            raw_before=raw_before,
            raw_after=raw_after,
        )
        raw_archives = archive_raw_packages(matching_raw, point_dir / "rtm" / "raw")
    except Exception as error:
        archive_failures.append(f"{type(error).__name__}: {error}")

    analysis_result: dict[str, Any] | None = None
    analysis_path: Path | None = None
    analysis_failures: list[str] = []
    if scope_result is not None and lan_result is not None:
        try:
            analysis_result = _zero_analysis(
                profile_name=profile_name,
                scope_result=scope_result,
                lan_result=lan_result,
            )
            analysis_path = point_dir / "analysis.json"
            write_json_exclusive(analysis_path, analysis_result)
        except Exception as error:
            analysis_failures.append(f"analysis: {type(error).__name__}: {error}")

    after = readonly_preflight()
    failures = list(operation_errors) + archive_failures + analysis_failures
    if scope_result is None:
        failures.append("scope result is missing")
    else:
        failures.extend(scope_result["failures"])
    if lan_result is None:
        failures.append("LAN result is missing")
    else:
        failures.extend(lan_result["failures"])
    if not raw_archives:
        failures.append("no WaveBench data/raw package was archived")
    if not after.get("pass"):
        failures.extend(f"postflight: {item}" for item in after.get("failures", []))
    try:
        _require_zero_profile_preflight(after, profile_name)
    except Exception as error:
        failures.append(f"postflight: {type(error).__name__}: {error}")

    overlap_ns = None
    if scope_result is not None and lan_result is not None:
        overlap_ns = min(
            scope_result["finished_monotonic_ns"],
            lan_result["finished_monotonic_ns"],
        ) - max(
            scope_result["started_monotonic_ns"],
            lan_result["started_monotonic_ns"],
        )
        if overlap_ns <= 0:
            failures.append("RTM acquisition and LAN capture windows do not overlap")

    payload = {
        "format": "CycleScope M11 coordinated zero-input point v2",
        "case_id": case_id,
        "profile_name": profile_name,
        "profile": profile,
        "evidence_class": profile["evidence_class"],
        "timestamp": datetime.now().astimezone().isoformat(),
        "acknowledgement": acknowledgement,
        "source_expected_output": "OFF",
        "source_writes": False,
        "scope_capture": "CH1+CH2 single acquisition",
        "scope_setup_restored": False,
        "scope_impedance_write": False,
        "power_write": False,
        "fpga_action": "CSLP ENABLE/DISABLE over LAN only",
        "preflight_evidence": before.get("evidence_path"),
        "lan_preflight_evidence": lan_smoke.get("evidence_path"),
        "postflight_evidence": after.get("evidence_path"),
        "scope": scope_result,
        "lan": lan_result,
        "analysis": analysis_result,
        "analysis_path": None if analysis_path is None else str(analysis_path.resolve()),
        "window_overlap_ns": overlap_ns,
        "wavebench_raw_archives": raw_archives,
        "failures": failures,
        "pass": not failures,
    }
    point_path = point_dir / "point.json"
    write_json_exclusive(point_path, payload)
    sums = _write_sha256sums(point_dir)
    payload["evidence_path"] = str(point_path)
    payload["sha256sums"] = str(sums)
    return payload


def safe_source_off(acknowledgement: str) -> dict[str, Any]:
    if acknowledgement != SAFE_OFF_ACK:
        raise M11SafetyError(f"safe-source-off requires --acknowledge {SAFE_OFF_ACK!r}")

    before = readonly_preflight()
    expected_failure = "DG CH1 output must be OFF, got 'ON'"
    if before.get("failures") != [expected_failure]:
        raise M11SafetyError(
            "safe-source-off is allowed only when the sole preflight failure is DG output ON"
        )

    config = derived_config()
    plan_path = safe_off_plan_path()
    plan = load_run_plan(plan_path)
    service = RunService(config=config, logger=CommandLogger())
    verify = service.verify(plan)
    action_root = EVIDENCE_ROOT / "safety-actions" / now_stamp()
    action_root.mkdir(parents=True, exist_ok=False)
    runs_before = _run_directories()
    run_result = None
    run_error: Exception | None = None
    archive_manifests: list[str] = []
    archive_failures: list[str] = []
    try:
        run_result = service.run(plan)
    except Exception as error:
        run_error = error
    finally:
        runs_after = _run_directories()
        for run_dir in sorted(runs_after - runs_before):
            try:
                archive_manifests.append(
                    str(archive_run(run_dir, action_root / "wavebench"))
                )
            except Exception as error:
                archive_failures.append(
                    f"archive {run_dir.name}: {type(error).__name__}: {error}"
                )

    # Never retry an ambiguous output write.  The only post-action operation is
    # a fresh read-only preflight that records the actual resulting state.
    after = readonly_preflight()
    failures: list[str] = list(archive_failures)
    if run_error is not None:
        failures.append(f"{type(run_error).__name__}: {run_error}")
    if not after.get("pass"):
        failures.extend(f"post-action: {item}" for item in after.get("failures", []))
    if run_result is not None:
        run_data = json.loads(run_result.run_json_path.read_text(encoding="utf-8"))
        if run_data.get("status") != "ok":
            failures.append(f"WaveBench run status is {run_data.get('status')!r}")

    payload = {
        "format": "CycleScope M11 source safe-OFF action v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "acknowledgement": acknowledgement,
        "authorized_write": "DG4202 CH1 output OFF",
        "source_load_write": False,
        "scope_write": False,
        "power_write": False,
        "restore_output_to_on": False,
        "preflight_evidence": before.get("evidence_path"),
        "postflight_evidence": after.get("evidence_path"),
        "verify": [
            {
                "instrument": record.instrument,
                "resource_sha256": sha256_text(record.resource),
                "idn": record.idn,
            }
            for record in verify
        ],
        "run_dir": None if run_result is None else str(run_result.run_dir),
        "archive_manifests": archive_manifests,
        "failures": failures,
        "pass": not failures,
    }
    path = action_root / "safe-source-off.json"
    write_json_exclusive(path, payload)
    payload["evidence_path"] = str(path)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="offline-only M11 safety and capability checks")
    commands.add_parser(
        "preflight-readonly",
        help="live read-only DG/RTM/DP800 preflight; no capture or FPGA control",
    )
    safe_off = commands.add_parser(
        "safe-source-off",
        help="one checked DG CH1 output-OFF write; never restores output ON",
    )
    safe_off.add_argument("--acknowledge", required=True)
    zero = commands.add_parser(
        "zero-live",
        help="DG-OFF CH1+CH2 single acquisition plus >=64 real-ADC LAN frames",
    )
    zero.add_argument("--acknowledge", required=True)
    zero.add_argument("--frames", type=int, default=64)
    zero.add_argument(
        "--profile",
        dest="profile_name",
        choices=sorted(ZERO_SCOPE_PROFILES),
        default="wide",
    )
    zero.add_argument("--expected-calibration-id", type=lambda value: int(value, 0), default=0)
    zero.add_argument("--expected-scale-uv-per-lsb", type=lambda value: int(value, 0), default=488)
    zero.add_argument("--expected-offset-uv", type=lambda value: int(value, 0), default=0)
    lan = commands.add_parser(
        "lan-preflight",
        help="DG-OFF two-frame CSLP handshake gate; no scope capture or instrument write",
    )
    lan.add_argument("--acknowledge", required=True)
    lan.add_argument("--expected-calibration-id", type=lambda value: int(value, 0), default=0)
    lan.add_argument("--expected-scale-uv-per-lsb", type=lambda value: int(value, 0), default=488)
    lan.add_argument("--expected-offset-uv", type=lambda value: int(value, 0), default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "check":
            result = validate_static(write_evidence=True)
        elif args.command == "preflight-readonly":
            result = readonly_preflight()
        elif args.command == "safe-source-off":
            result = safe_source_off(args.acknowledge)
        elif args.command == "zero-live":
            result = zero_live(
                args.acknowledge,
                frames=args.frames,
                profile_name=args.profile_name,
                expected_calibration_id=args.expected_calibration_id,
                expected_scale_uv_per_lsb=args.expected_scale_uv_per_lsb,
                expected_offset_uv=args.expected_offset_uv,
            )
        else:
            result = lan_preflight(
                args.acknowledge,
                expected_calibration_id=args.expected_calibration_id,
                expected_scale_uv_per_lsb=args.expected_scale_uv_per_lsb,
                expected_offset_uv=args.expected_offset_uv,
            )
    except Exception as error:
        print(f"M11_SAFETY_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(f"pass={str(result['pass']).lower()}")
    if "evidence_path" in result:
        print(f"evidence={result['evidence_path']}")
    if result.get("failures"):
        for failure in result["failures"]:
            print(f"failure={failure}")
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
