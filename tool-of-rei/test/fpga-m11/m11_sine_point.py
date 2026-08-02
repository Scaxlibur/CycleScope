#!/usr/bin/env python3
"""Fail-closed M11 sine-point coordinator for WaveBench + CSLP LAN."""

# ruff: noqa: E402 -- sibling safety module establishes the WaveBench source path.

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m11_wavebench_safe as safety
import m11_user_to_sine_driver as user_to_sine

PS_SCRIPTS = safety.FPGA_ROOT / "Zynq_7010_PS" / "cyclescope_cslp" / "scripts"
if str(PS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PS_SCRIPTS))

from cslp_calibration_profile import load_calibration_profile

from wavebench.logging import CommandLogger
from wavebench.data.fft import analyze_fft as wavebench_analyze_fft
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService
from wavebench.services.scope_service import ScopeService
from wavebench.services.source_service import SourceService


FPGA_ROOT = safety.FPGA_ROOT
EVIDENCE_ROOT = safety.EVIDENCE_ROOT
MATRIX_ROOT = EVIDENCE_ROOT / "offline" / "matrix-v3"
PHYSICAL_BASELINE = EVIDENCE_ROOT / "preflight" / "physical-topology-user-confirmed-v2.json"
USER_RESTORATION_WAIVER = (
    EVIDENCE_ROOT
    / "preflight"
    / "dg-user-restoration-waiver-20260801-v1"
    / "waiver.json"
)
LIVE_ACK = "M11_DG50_RTM12_HIGHZ_DP800_READONLY_FRONTEND_PHYSICAL_GATE"
C_STAGE_ACK = "M11_STAGE_C_LOW_AMPLITUDE_MONOTONIC"
D_STAGE_ACK = "M11_STAGE_D_DYNAMIC_RANGE_MONOTONIC"
E_STAGE_ACK = "M11_STAGE_E_CALIBRATION_SWEEP"
F_STAGE_ACK = "M11_STAGE_F_FIR_STOPBAND"
I_STAGE_ACK = "M11_STAGE_I_FORMAL_UPPER_FREQUENCY"
STAGE_ACKNOWLEDGEMENTS = {
    "C": C_STAGE_ACK,
    "D": D_STAGE_ACK,
    "E": E_STAGE_ACK,
    "F": F_STAGE_ACK,
    "I": I_STAGE_ACK,
}
STAGE_MAX_SOURCE_VPP = {
    "C": 0.1,
    "D": 0.45,
    "E": 0.45,
    "F": 0.2,
    "I": 0.2,
}
DYNAMIC_RANGE_CEILING_EVIDENCE = (
    EVIDENCE_ROOT / "preflight" / "dynamic-range-ceiling-20260731-v1.json"
)
PROVISIONAL_DISCOVERY_ACK = "M11_100KHZ_MONOTONIC_PROVISIONAL_NOT_CALIBRATION"
PROVISIONAL_CASE_SEQUENCE = (
    "c-100k-020mVpp",
    "c-100k-050mVpp",
    "c-100k-100mVpp",
)
OUTPUT_SAMPLE_RATE_HZ = 4_062_500.0
FRAME_SAMPLES = 8192
MIN_POINT_FRAMES = 22
MAX_CH1_VPP = 0.55
MAX_CH2_VPP = 2.35
IMMEDIATE_CH2_STOP_VPP = 2.5


class M11PointError(RuntimeError):
    """A point cannot be executed or accepted safely."""


def load_user_restoration_waiver(
    path: Path = USER_RESTORATION_WAIVER,
) -> dict[str, Any]:
    payload = load_json(path)
    sums_path = path.parent / "SHA256SUMS"
    expected = None
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if separator and relative == path.name:
            expected = digest
    if expected is None or expected != safety.sha256_file(path):
        raise M11PointError("DG USER restoration waiver SHA-256 binding failed")
    scope = payload.get("scope", {})
    transition = payload.get("transition_contract", {})
    invariants = payload.get("invariants_not_waived", {})
    if (
        payload.get("pass") is not True
        or scope.get("prior_user_waveform_restoration_required") is not False
        or scope.get("initial_user_off_may_transition_to_sin_under_checked_plan") is not True
        or scope.get("final_fixed_sine_restoration_required") is not False
        or scope.get("stage_i_sine_stimulus_still_requires_sin") is not True
        or transition.get("source_output_must_be_off_before_transition") is not True
        or transition.get("wavebench_checked_plan_required") is not True
        or transition.get("raw_scpi_forbidden") is not True
        or invariants.get("dg_load_ohm") != 50.0
        or invariants.get("dg_offset_v") != 0.0
        or invariants.get("rtm_ch1_ch2_high_impedance") is not True
        or invariants.get("dp832_writes_forbidden") is not True
    ):
        raise M11PointError("DG USER restoration waiver contract is incomplete")
    return {
        "path": str(path.resolve()),
        "sha256": safety.sha256_file(path),
        "payload": payload,
        "pass": True,
    }


def filter_i_user_off_preflight(generic: dict[str, Any]) -> dict[str, Any]:
    """Accept only the user-waived USER/OFF initial state for an I sine plan."""

    waiver = load_user_restoration_waiver()
    failures = list(generic.get("failures", []))
    status = generic.get("source", {}).get("profile", {}).get("status", {})
    function = str(status.get("function", "")).upper()
    function_failures = [
        item
        for item in failures
        if item.startswith("DG CH1 function is not safely restorable:")
    ]
    accepted_exception = None
    if function == "USER" and status.get("output") == "OFF":
        if len(function_failures) == 1:
            accepted_exception = function_failures[0]
            failures.remove(function_failures[0])
        else:
            failures.append("I USER/OFF state did not produce exactly one generic exception")
    elif function_failures:
        failures.append("I USER restoration waiver applies only to USER/OFF")
    return {
        **generic,
        "format": "CycleScope M11-I USER/OFF sine-transition preflight v1",
        "generic_preflight_pass": generic.get("pass") is True,
        "generic_preflight_evidence": generic.get("evidence_path"),
        "user_restoration_waiver": {
            "path": waiver["path"],
            "sha256": waiver["sha256"],
        },
        "accepted_exception": accepted_exception,
        "accepted_scope": (
            "USER/OFF may be changed to SIN/OFF by the checked I source plan; old "
            "volatile USER waveform restoration is not required"
            if accepted_exception
            else None
        ),
        "instrument_writes": False,
        "failures": failures,
        "pass": not failures,
    }


def i_readonly_preflight() -> dict[str, Any]:
    generic = safety.readonly_preflight()
    payload = filter_i_user_off_preflight(generic)
    generic_path = Path(str(generic["evidence_path"]))
    output = generic_path.parent / "i-sine-preflight.json"
    safety.write_json_exclusive(output, payload)
    payload["evidence_path"] = str(output.resolve())
    return payload


def expected_calibration_identity(manifest: Path | None) -> dict[str, Any]:
    if manifest is None:
        return {
            "calibration_id": 0,
            "scale_uv_per_lsb": 488,
            "offset_uv": 0,
            "calibrated": False,
            "manifest": None,
            "manifest_sha256": None,
        }
    profile = load_calibration_profile(manifest.resolve())
    return {
        "calibration_id": profile.calibration_id,
        "scale_uv_per_lsb": profile.scale_uv_per_lsb,
        "offset_uv": profile.offset_uv,
        "calibrated": True,
        "manifest": str(profile.manifest_path),
        "manifest_sha256": profile.manifest_sha256,
    }


def require_calibration_for_stage(
    record: dict[str, Any], identity: dict[str, Any]
) -> None:
    if str(record.get("stage")) in {"F", "I"} and int(identity["calibration_id"]) == 0:
        raise M11PointError(
            f"stage {record['stage']} requires --calibration-manifest with a validated nonzero ID"
        )


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise M11PointError(f"JSON object required: {path}")
    return value


def physical_gate(path: Path = PHYSICAL_BASELINE) -> dict[str, Any]:
    payload = load_json(path)
    confirmed = payload.get("confirmed")
    pending = payload.get("not_yet_physically_verified")
    authorized_omissions = payload.get("user_authorized_omissions")
    measurements = payload.get("measured_values")
    value_basis = "meter_measurement"
    if not isinstance(measurements, dict):
        measurements = payload.get("component_values")
        value_basis = None if not isinstance(measurements, dict) else measurements.get("basis")
    failures: list[str] = []
    if not isinstance(confirmed, dict) or not confirmed or not all(
        value is True for value in confirmed.values()
    ):
        failures.append("user-confirmed input/output/impedance topology is incomplete")
    omitted_pending: list[str] = []
    if not isinstance(pending, dict):
        failures.append("physical verification record is missing")
    else:
        if not isinstance(authorized_omissions, dict):
            authorized_omissions = {}
        omitted_pending = sorted(
            key
            for key, value in pending.items()
            if value is True and authorized_omissions.get(key) is True
        )
        unwaived_pending = sorted(
            key
            for key, value in pending.items()
            if value is True and authorized_omissions.get(key) is not True
        )
        if unwaived_pending:
            failures.append(
                "unwaived physical verification remains pending: "
                + ", ".join(unwaived_pending)
            )
    if not isinstance(measurements, dict):
        failures.append("measured_values/component_values are missing")
    else:
        if value_basis not in {"meter_measurement", "user_explicit_nominal_confirmation"}:
            failures.append("component value basis is missing or unsupported")
        for key in ("rf_ohm", "rg_ohm", "series_resistor_ohm"):
            value = measurements.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                failures.append(f"confirmed {key} is missing or invalid")
        feedback_pickoff_omitted = (
            "ad8065_feedback_pickoff_before_or_after_series_resistor" in omitted_pending
        )
        if not feedback_pickoff_omitted and measurements.get("feedback_pickoff") not in {
            "before_series_resistor",
            "after_series_resistor",
        }:
            failures.append("feedback_pickoff is not physically frozen")
    return {
        "path": str(path.resolve()),
        "sha256": safety.sha256_file(path),
        "failures": failures,
        "user_authorized_omissions": omitted_pending,
        "pass": not failures,
        "payload": payload,
    }


def provisional_discovery_gate(
    record: dict[str, Any],
    path: Path = PHYSICAL_BASELINE,
) -> dict[str, Any]:
    payload = load_json(path)
    failures: list[str] = []
    case_id = str(record.get("case_id", ""))
    if case_id not in PROVISIONAL_CASE_SEQUENCE:
        failures.append("provisional discovery is limited to 100 kHz / 20, 50, 100 mVpp")
        case_index = 0
    else:
        case_index = PROVISIONAL_CASE_SEQUENCE.index(case_id)

    confirmed = payload.get("confirmed")
    if not isinstance(confirmed, dict) or not confirmed or not all(
        value is True for value in confirmed.values()
    ):
        failures.append("user-confirmed electrical/impedance topology is incomplete")
    components = payload.get("component_values")
    if not isinstance(components, dict) or components.get("basis") != (
        "user_explicit_nominal_confirmation"
    ):
        failures.append("nominal RF/RG/Rs user confirmation is missing")
    else:
        for key, expected in (
            ("rf_ohm", 1608.0),
            ("rg_ohm", 200.2),
            ("series_resistor_ohm", 50.0),
        ):
            value = components.get(key)
            if not isinstance(value, (int, float)) or not math.isclose(
                float(value), expected, rel_tol=0.0, abs_tol=1e-9
            ):
                failures.append(f"provisional nominal {key} mismatch")

    pending = payload.get("not_yet_physically_verified")
    allowed_pending = {
        "ad8065_feedback_pickoff_before_or_after_series_resistor",
        "probe_channel_swap_correction",
        "internal_supply_rails_and_temperature",
    }
    if not isinstance(pending, dict):
        failures.append("physical pending-item record is missing")
    else:
        unexpected = sorted(
            key for key, value in pending.items() if value is True and key not in allowed_pending
        )
        if unexpected:
            failures.append("unexpected provisional pending items: " + ", ".join(unexpected))

    instrument_state = payload.get("verified_instrument_state")
    if not isinstance(instrument_state, dict) or any(
        str(instrument_state.get(f"rtm2032_ch{channel}_coupling", "")).upper() != "DCL"
        for channel in (1, 2)
    ):
        failures.append("dual DCL instrument confirmation is missing")

    evidence = payload.get("provisional_low_amplitude_safety_evidence")
    expected_points = {
        "effective_band_zero_point": "b-zero-noise-500k",
        "high_frequency_zero_point": "b-zero-hf-spur",
    }
    evidence_records: list[dict[str, Any]] = []
    if not isinstance(evidence, dict):
        failures.append("bounded zero-input safety evidence is missing")
    else:
        for key, expected_case in expected_points.items():
            binding = evidence.get(key)
            if not isinstance(binding, dict):
                failures.append(f"{key} binding is missing")
                continue
            evidence_path = EVIDENCE_ROOT / str(binding.get("path", ""))
            if not evidence_path.is_file():
                failures.append(f"{key} point.json is missing")
                continue
            actual_sha256 = safety.sha256_file(evidence_path)
            if actual_sha256 != binding.get("sha256"):
                failures.append(f"{key} SHA-256 mismatch")
                continue
            point = load_json(evidence_path)
            if point.get("case_id") != expected_case or not point.get("pass"):
                failures.append(f"{key} did not pass as {expected_case}")
            if point.get("source_writes") is not False or point.get("power_write") is not False:
                failures.append(f"{key} contains a forbidden source/power write")
            scope = point.get("scope")
            if not isinstance(scope, dict):
                failures.append(f"{key} scope evidence is missing")
            else:
                for phase in ("couplings_before", "couplings_after"):
                    couplings = scope.get(phase)
                    if not isinstance(couplings, dict) or any(
                        str(couplings.get(str(channel), "")).upper() != "DCL"
                        for channel in (1, 2)
                    ):
                        failures.append(f"{key} does not prove dual DCL {phase}")
            evidence_records.append(
                {
                    "kind": key,
                    "path": str(evidence_path.resolve()),
                    "sha256": actual_sha256,
                    "case_id": point.get("case_id"),
                }
            )

    sine_evidence = payload.get("provisional_monotonic_sine_evidence")
    if not isinstance(sine_evidence, dict):
        sine_evidence = {}
    for prior_case in PROVISIONAL_CASE_SEQUENCE[:case_index]:
        binding = sine_evidence.get(prior_case)
        if not isinstance(binding, dict):
            failures.append(f"prior monotonic point {prior_case} binding is missing")
            continue
        point_path = EVIDENCE_ROOT / str(binding.get("point_path", ""))
        analysis_path = EVIDENCE_ROOT / str(binding.get("analysis_path", ""))
        if not point_path.is_file() or not analysis_path.is_file():
            failures.append(f"prior monotonic point {prior_case} files are missing")
            continue
        point_sha256 = safety.sha256_file(point_path)
        analysis_sha256 = safety.sha256_file(analysis_path)
        if point_sha256 != binding.get("point_sha256"):
            failures.append(f"prior monotonic point {prior_case} point SHA-256 mismatch")
            continue
        if analysis_sha256 != binding.get("analysis_sha256"):
            failures.append(f"prior monotonic point {prior_case} analysis SHA-256 mismatch")
            continue
        prior_point = load_json(point_path)
        prior_analysis = load_json(analysis_path)
        if (
            not prior_point.get("pass")
            or prior_point.get("case", {}).get("case_id") != prior_case
            or prior_point.get("provisional_discovery") is not True
            or prior_point.get("formal_calibration_eligible") is not False
            or prior_point.get("dp800_writes") is not False
            or prior_point.get("scope_impedance_writes") is not False
            or prior_point.get("source_window", {}).get("off_status", {}).get("output") != "OFF"
            or prior_point.get("lan", {}).get("frame_count", 0) < 64
        ):
            failures.append(f"prior monotonic point {prior_case} safety/result contract failed")
        ratios = prior_analysis.get("ratios")
        adc = prior_analysis.get("adc")
        scope = prior_analysis.get("scope_primary", prior_analysis.get("scope"))
        if isinstance(scope, dict) and isinstance(scope.get("ch2"), dict):
            ch2_entry = scope["ch2"]
            if isinstance(ch2_entry.get("metadata_summary"), dict):
                ch2_observed_vpp = float(
                    ch2_entry["metadata_summary"].get("voltage_vpp_v", math.inf)
                )
            else:
                ch2_observed_vpp = float(ch2_entry.get("peak_to_peak", math.inf))
        else:
            ch2_observed_vpp = math.inf
        if (
            not prior_analysis.get("pass")
            or prior_analysis.get("formal_calibration_eligible") is not False
            or int(prior_analysis.get("calibration_id", -1)) != 0
            or not isinstance(ratios, dict)
            or not 0.8 <= float(ratios.get("ksrc_v_per_vset", -1.0)) <= 1.2
            or not 1.0 <= float(ratios.get("gamp_v_per_v", -1.0)) <= 8.0
            or not 250.0 <= float(ratios.get("kadc_code_per_v", -1.0)) <= 600.0
            or not isinstance(adc, dict)
            or int(adc.get("frame_count", 0)) < 64
            or not isinstance(scope, dict)
            or ch2_observed_vpp > 1.5
        ):
            failures.append(f"prior monotonic point {prior_case} analysis plausibility failed")
        evidence_records.append(
            {
                "kind": "prior_monotonic_sine",
                "case_id": prior_case,
                "point_path": str(point_path.resolve()),
                "point_sha256": point_sha256,
                "analysis_path": str(analysis_path.resolve()),
                "analysis_sha256": analysis_sha256,
            }
        )
    return {
        "path": str(path.resolve()),
        "sha256": safety.sha256_file(path),
        "mode": "provisional-monotonic-low-amplitude-discovery-only",
        "formal_calibration_eligible": False,
        "evidence": evidence_records,
        "failures": failures,
        "pass": not failures,
    }


def load_sine_case(case_id: str) -> dict[str, Any]:
    manifest_path = MATRIX_ROOT / "manifest.json"
    manifest = load_json(manifest_path)
    expected_sources = {
        "public_measurement_plan": safety.sha256_file(FPGA_ROOT.parent / "public" / "信号前端测量方案.md"),
        "m11_plan": safety.sha256_file(FPGA_ROOT / "tool-of-rei" / "M11-真实全链路FIR与信号处理压力测试计划.md"),
        "fir_coefficients": safety.sha256_file(FPGA_ROOT / "Zynq_7010_PL" / "rtl" / "fir_coeffs_pkg.sv"),
    }
    if manifest.get("source_hashes") != expected_sources:
        raise M11PointError("current matrix source hashes are stale")
    matches = [item for item in manifest.get("sine_points", []) if item.get("case_id") == case_id]
    if len(matches) != 1:
        raise M11PointError(f"expected exactly one sine case {case_id!r}, found {len(matches)}")
    record = matches[0]
    if record.get("kind") != "sine":
        raise M11PointError(f"{case_id}: not a sine point")
    frequency = float(record["frequency_hz"])
    amplitude = float(record["source_vpp_v"])
    if not math.isfinite(frequency) or frequency <= 0:
        raise M11PointError(f"{case_id}: invalid frequency")
    if not math.isfinite(amplitude) or not 0 < amplitude <= safety.MAX_SOURCE_VPP:
        raise M11PointError(f"{case_id}: amplitude violates M11 0.5 Vpp limit")
    return {
        **record,
        "matrix_manifest": str(manifest_path.resolve()),
        "matrix_manifest_sha256": safety.sha256_file(manifest_path),
    }


def validate_sine_stage_case(
    record: dict[str, Any], stage_acknowledgement: str
) -> None:
    stage = str(record.get("stage", ""))
    expected_acknowledgement = STAGE_ACKNOWLEDGEMENTS.get(stage)
    if expected_acknowledgement is None:
        raise M11PointError(f"unsupported M11 sine stage: {stage!r}")
    if stage_acknowledgement != expected_acknowledgement:
        raise M11PointError(
            f"stage {stage} requires --stage-acknowledge {expected_acknowledgement!r}"
        )
    maximum_vpp = STAGE_MAX_SOURCE_VPP[stage]
    if float(record["source_vpp_v"]) > maximum_vpp:
        raise M11PointError(
            f"stage {stage} source amplitude must not exceed {maximum_vpp:g} Vpp"
        )


def plan_text(record: dict[str, Any]) -> str:
    case_id = record["case_id"]
    return f'''[experiment]
name = "CycleScope M11 {case_id} source configuration"
label = "cyclescope_m11_{case_id}_source_config"

[safety]
scope_guard_channel = 1
require_scope_coupling_not = ["DC", "AC"]
allow_50ohm = false

[[steps]]
kind = "source.status"
channel = 1

[[steps]]
kind = "power.status"
channel = 1

[[steps]]
kind = "source.output"
channel = 1
state = "off"

[[steps]]
kind = "source.set_func"
channel = 1
function = "SIN"

[[steps]]
kind = "source.set_vpp"
channel = 1
value_vpp = {float(record["source_vpp_v"]):.12g}

[[steps]]
kind = "source.set_freq"
channel = 1
frequency_hz = {float(record["frequency_hz"]):.12g}

[[steps]]
kind = "source.status"
channel = 1

[[steps]]
kind = "power.status"
channel = 1
'''


def validate_configuration_plan(path: Path, record: dict[str, Any], config: Any) -> dict[str, Any]:
    plan = load_run_plan(path)
    kinds = [step.kind for step in plan.steps]
    expected = [
        "source.status",
        "power.status",
        "source.output",
        "source.set_func",
        "source.set_vpp",
        "source.set_freq",
        "source.status",
        "power.status",
    ]
    if kinds != expected:
        raise M11PointError(f"configuration plan sequence mismatch: {kinds}")
    if plan.restore.source_state:
        raise M11PointError("configuration plan must leave the requested settings OFF")
    output_steps = [step for step in plan.steps if step.kind == "source.output"]
    if len(output_steps) != 1 or output_steps[0].fields.get("state") != "off":
        raise M11PointError("configuration plan may contain only one source.output OFF")
    if any(step.kind in {"power.set", "power.output"} for step in plan.steps):
        raise M11PointError("DP800 writes are forbidden")
    if plan.safety.allow_50ohm or plan.safety.scope_guard_channel != 1:
        raise M11PointError("configuration plan scope guard changed")
    RunService(config=config, logger=CommandLogger()).check(plan)
    return {
        "path": str(path.resolve()),
        "sha256": safety.sha256_file(path),
        "steps": kinds,
        "source_vpp_v": float(record["source_vpp_v"]),
        "frequency_hz": float(record["frequency_hz"]),
    }


def choose_vertical_scale(expected_ch2_vpp: float) -> float:
    required = max(expected_ch2_vpp / 5.0, 0.002)
    for exponent in range(-4, 2):
        for base in (1.0, 2.0, 5.0):
            candidate = base * (10.0**exponent)
            if candidate >= required:
                return candidate
    raise M11PointError("required RTM vertical scale is out of supported M11 range")


def folded_frequency(frequency_hz: float) -> float:
    wrapped = frequency_hz % OUTPUT_SAMPLE_RATE_HZ
    return min(wrapped, OUTPUT_SAMPLE_RATE_HZ - wrapped)


def scope_window(record: dict[str, Any]) -> tuple[float, float]:
    frequency_hz = float(record["frequency_hz"])
    if record.get("stage") == "E":
        # RTM2000 time ranges quantize to a 1/2/5 sequence.  These two exact
        # ranges keep every E-matrix tone on a WaveBench FFT bin: low tones
        # are integer-cycle in 2 ms and all >=50 kHz tones are integer-cycle
        # in 200 us.  This avoids half-bin amplitude loss without replacing
        # WaveBench's archived-NPY FFT as the primary scope analysis.
        time_range_s = 0.002 if frequency_hz < 50_000.0 else 0.0002
    elif record.get("stage") == "F":
        # Keep 10 kpoints at about 50 MS/s so the RTM FFT still resolves and
        # samples every 1..3 MHz input.  A 200 us window also places all fixed
        # 5 kHz-grid tones exactly on WaveBench bins.  The deliberately
        # half-kHz worst-residual points remain mildly non-coherent; their
        # WaveBench Hann-bin amplitude is conservative for CH2->FPGA
        # attenuation and the known-frequency fit stays a bounded cross-check.
        time_range_s = 0.0002
    elif record.get("stage") == "I":
        # Use actual RTM 1/2/5 time-range values, not 20/f values which the
        # instrument silently quantizes.  Every formal I tone is coherent on
        # one of these two grids: 4/5/7.2 MHz use 5 us and 7.5/10 MHz use 2 us.
        time_range_s = 5e-6 if frequency_hz <= 7_200_000.0 else 2e-6
    else:
        time_range_s = max(20.0 / frequency_hz, 2e-6)
    return time_range_s, frequency_hz * time_range_s


def basic_metrics(values: np.ndarray) -> dict[str, float | int]:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or values.size < 4 or not np.all(np.isfinite(values)):
        raise M11PointError("invalid waveform values")
    mean = float(np.mean(values))
    return {
        "samples": int(values.size),
        "mean": mean,
        "rms_ac": float(np.sqrt(np.mean(np.square(values - mean)))),
        "rms_total": float(np.sqrt(np.mean(np.square(values)))),
        "minimum": float(np.min(values)),
        "maximum": float(np.max(values)),
        "peak_to_peak": float(np.ptp(values)),
    }


def tone_metrics(values: np.ndarray, sample_rate_hz: float, frequency_hz: float) -> dict[str, float | int]:
    values = np.asarray(values, dtype=np.float64)
    if not 0 < frequency_hz < sample_rate_hz / 2:
        raise M11PointError("tone frequency is outside the sampled Nyquist band")
    time_axis = np.arange(values.size, dtype=np.float64) / sample_rate_hz
    columns = [np.ones(values.size, dtype=np.float64)]
    harmonics: list[int] = []
    for order in range(1, 6):
        harmonic_frequency = order * frequency_hz
        if harmonic_frequency >= sample_rate_hz / 2:
            break
        phase = 2.0 * math.pi * harmonic_frequency * time_axis
        columns.extend((np.sin(phase), np.cos(phase)))
        harmonics.append(order)
    matrix = np.column_stack(columns)
    coefficients, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    peaks: dict[int, float] = {}
    phases: dict[int, float] = {}
    for index, order in enumerate(harmonics):
        sine = float(coefficients[1 + 2 * index])
        cosine = float(coefficients[2 + 2 * index])
        peaks[order] = math.hypot(sine, cosine)
        phases[order] = math.atan2(cosine, sine)
    fitted = matrix @ coefficients
    fundamental = peaks[1]
    harmonic_power = sum(value * value for order, value in peaks.items() if order > 1)
    return {
        **basic_metrics(values),
        "sample_rate_hz": float(sample_rate_hz),
        "frequency_hz": float(frequency_hz),
        "fundamental_peak": fundamental,
        "fundamental_vpp": 2.0 * fundamental,
        "fundamental_phase_rad": phases[1],
        "harmonic_2_peak": peaks.get(2, 0.0),
        "harmonic_3_peak": peaks.get(3, 0.0),
        "harmonic_4_peak": peaks.get(4, 0.0),
        "harmonic_5_peak": peaks.get(5, 0.0),
        "thd_ratio": 0.0 if fundamental == 0 else math.sqrt(harmonic_power) / fundamental,
        "fit_residual_rms": float(np.sqrt(np.mean(np.square(values - fitted)))),
    }


def load_scope_trace(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    raw = np.load(path, allow_pickle=False)
    if raw.ndim != 2 or raw.shape[1] != 2 or raw.shape[0] < 4:
        raise M11PointError(f"invalid WaveBench trace: {path}")
    times = np.asarray(raw[:, 0], dtype=np.float64)
    values = np.asarray(raw[:, 1], dtype=np.float64)
    intervals = np.diff(times)
    if not np.all(np.isfinite(times)) or np.any(intervals <= 0):
        raise M11PointError(f"invalid WaveBench time axis: {path}")
    return times, values, float(1.0 / np.median(intervals))


def wavebench_fft_for_expected_frequency(
    waveform: np.ndarray,
    expected_frequency_hz: float,
    *,
    allow_coherent_prefix: bool,
) -> dict[str, Any]:
    """Use WaveBench FFT on the full trace or a deterministic coherent prefix."""

    data = np.asarray(waveform, dtype=np.float64)
    full_fft = wavebench_analyze_fft(data, max_harmonic_order=5)
    resolution_hz = float(full_fft["resolution_hz"])
    full_offset_bins = (
        abs(float(full_fft["peak_frequency_hz"]) - expected_frequency_hz)
        / resolution_hz
        if resolution_hz > 0.0
        else math.inf
    )
    if full_offset_bins <= 0.05 or not allow_coherent_prefix:
        return {
            "fft": full_fft,
            "full_trace_fft": full_fft,
            "selection": {
                "mode": "full_archived_trace",
                "archived_samples": int(data.shape[0]),
                "analyzed_samples": int(data.shape[0]),
                "dropped_tail_samples": 0,
                "full_trace_frequency_offset_bins": full_offset_bins,
                "raw_samples_modified": False,
            },
        }

    if data.ndim != 2 or data.shape[1] < 2 or data.shape[0] < 2048:
        raise M11PointError("coherent-prefix FFT requires an Nx2 archived waveform")
    intervals = np.diff(data[:, 0])
    if np.any(intervals <= 0.0):
        raise M11PointError("coherent-prefix FFT requires an increasing time axis")
    sample_interval_s = float(np.median(intervals))
    minimum_samples = max(1024, math.ceil(data.shape[0] * 0.5))
    selected_samples = None
    selected_cycles = None
    for count in range(int(data.shape[0]), minimum_samples - 1, -1):
        cycles = expected_frequency_hz * sample_interval_s * count
        nearest = round(cycles)
        if nearest >= 8 and abs(cycles - nearest) <= 1e-7:
            selected_samples = count
            selected_cycles = int(nearest)
            break
    if selected_samples is None or selected_cycles is None:
        raise M11PointError(
            "no >=50% archived prefix contains an integer number of expected cycles"
        )
    selected = data[:selected_samples]
    selected_fft = wavebench_analyze_fft(selected, max_harmonic_order=5)
    selected_resolution_hz = float(selected_fft["resolution_hz"])
    selected_offset_bins = (
        abs(float(selected_fft["peak_frequency_hz"]) - expected_frequency_hz)
        / selected_resolution_hz
        if selected_resolution_hz > 0.0
        else math.inf
    )
    if selected_offset_bins > 0.05:
        raise M11PointError("selected coherent prefix did not produce the expected FFT bin")
    return {
        "fft": selected_fft,
        "full_trace_fft": full_fft,
        "selection": {
            "mode": "longest_integer-cycle_archived_prefix",
            "archived_samples": int(data.shape[0]),
            "analyzed_samples": int(selected_samples),
            "dropped_tail_samples": int(data.shape[0] - selected_samples),
            "expected_cycles": selected_cycles,
            "minimum_archived_fraction": 0.5,
            "full_trace_frequency_offset_bins": full_offset_bins,
            "selected_frequency_offset_bins": selected_offset_bins,
            "prefix_starts_at_first_archived_sample": True,
            "outlier_selection_used": False,
            "raw_samples_modified": False,
        },
    }


def wavebench_scope_analysis(
    scope_package: Path,
    expected_frequency_hz: float,
    *,
    allow_coherent_prefix: bool = False,
) -> dict[str, Any]:
    metadata_path = scope_package / "metadata.json"
    metadata = load_json(metadata_path)
    operation = metadata.get("operation")
    channels = metadata.get("channels")
    failures: list[str] = []
    if not isinstance(operation, dict) or operation.get("channels") != [1, 2]:
        failures.append("WaveBench metadata does not bind one CH1+CH2 acquisition")
    if not isinstance(operation, dict) or operation.get("trigger_mode") != "single_acquisition":
        failures.append("WaveBench metadata is not a single acquisition")
    if not isinstance(channels, dict):
        raise M11PointError("WaveBench metadata channels are missing")

    results: dict[str, Any] = {}
    for channel in (1, 2):
        entry = channels.get(str(channel))
        summary = entry.get("summary") if isinstance(entry, dict) else None
        if not isinstance(summary, dict):
            raise M11PointError(f"WaveBench CH{channel} quality summary is missing")
        waveform = np.load(scope_package / f"ch{channel}.npy", allow_pickle=False)
        fft_record = wavebench_fft_for_expected_frequency(
            waveform,
            expected_frequency_hz,
            allow_coherent_prefix=allow_coherent_prefix,
        )
        fft = fft_record["fft"]
        frequency_error_ratio = abs(
            float(fft["peak_frequency_hz"]) - expected_frequency_hz
        ) / expected_frequency_hz
        if fft.get("warnings"):
            failures.append(f"WaveBench CH{channel} FFT warnings: {fft['warnings']}")
        metadata_warnings = list(summary.get("quality_warnings") or [])
        if frequency_error_ratio > 0.01:
            failures.append(f"WaveBench CH{channel} FFT frequency differs by more than 1%")
        results[f"ch{channel}"] = {
            "metadata_summary": summary,
            "metadata_quality_warnings_advisory": metadata_warnings,
            "metadata_frequency_gate_superseded_by_fft": True,
            "fft": fft,
            "full_trace_fft": fft_record["full_trace_fft"],
            "fft_input_selection": fft_record["selection"],
            "fundamental_peak_v": float(fft["peak_amplitude_v"]),
            "fundamental_vpp_v": 2.0 * float(fft["peak_amplitude_v"]),
            "fft_frequency_error_ratio": frequency_error_ratio,
        }
    return {
        "primary_method": (
            "WaveBench archived NPY full trace or deterministic integer-cycle prefix + "
            "wavebench.data.fft.analyze_fft; "
            "metadata time-domain frequency estimate is advisory"
        ),
        "metadata": str(metadata_path.resolve()),
        "metadata_sha256": safety.sha256_file(metadata_path),
        "single_acquisition": True,
        "ch1": results["ch1"],
        "ch2": results["ch2"],
        "failures": failures,
        "pass": not failures,
    }


def aggregate_adc_tone(capture_dir: Path, frequency_hz: float) -> dict[str, Any]:
    paths = sorted(capture_dir.glob("frame_*.s16le"))
    if len(paths) < MIN_POINT_FRAMES:
        raise M11PointError(f"only {len(paths)} ADC frames are available")
    records: list[dict[str, float | int]] = []
    for path in paths:
        values = np.fromfile(path, dtype="<i2")
        if values.size != FRAME_SAMPLES:
            raise M11PointError(f"{path.name}: expected {FRAME_SAMPLES} samples")
        records.append(tone_metrics(values, OUTPUT_SAMPLE_RATE_HZ, frequency_hz))
    numeric_keys = [key for key, value in records[0].items() if isinstance(value, (int, float))]
    aggregate = {
        key: {
            "median": float(statistics.median(float(record[key]) for record in records)),
            "minimum": float(min(float(record[key]) for record in records)),
            "maximum": float(max(float(record[key]) for record in records)),
        }
        for key in numeric_keys
    }
    return {"frame_count": len(records), "metrics": aggregate}


def capture_calibration_identity(
    capture_dir: Path,
    lan_report: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = capture_dir / "capture.json"
    manifest = load_json(manifest_path)
    frames = manifest.get("frames")
    if not isinstance(frames, list) or not frames:
        raise M11PointError("capture calibration metadata is missing")
    identities: set[tuple[int, int, int]] = set()
    calibrated_flags: set[bool] = set()
    for frame in frames:
        if not isinstance(frame, dict):
            raise M11PointError("capture frame metadata is invalid")
        try:
            identity = (
                int(frame["calibration_id"]),
                int(frame["scale_uv_per_lsb"]),
                int(frame["offset_uv"]),
            )
            flags = int(frame["frame_flags"])
        except (KeyError, TypeError, ValueError) as error:
            raise M11PointError("capture frame lacks calibration identity") from error
        if not 0 <= identity[0] <= 0xFFFF or not 1 <= identity[1] <= 0xFFFFFFFF:
            raise M11PointError("capture calibration identity is outside protocol bounds")
        if not -0x80000000 <= identity[2] <= 0x7FFFFFFF:
            raise M11PointError("capture calibration offset is outside protocol bounds")
        identities.add(identity)
        calibrated_flags.add(bool(flags & 0x0008))
        if bool(flags & 0x0008) != (identity[0] != 0):
            raise M11PointError("CALIBRATED flag and calibration_id disagree")
    if len(identities) != 1 or len(calibrated_flags) != 1:
        raise M11PointError("capture calibration identity changed between frames")
    calibration_id, scale_uv_per_lsb, offset_uv = next(iter(identities))

    expected_values = {
        "calibration_id": calibration_id,
        "scale_uv_per_lsb": scale_uv_per_lsb,
        "offset_uv": offset_uv,
    }
    for source_name, source in (("capture manifest", manifest), ("LAN report", lan_report)):
        for key, expected in expected_values.items():
            if key in source and source[key] is not None and int(source[key]) != expected:
                raise M11PointError(f"{source_name} {key} disagrees with frames")
    expected = lan_report.get("expected_wave_metadata")
    if isinstance(expected, dict):
        for key, actual in expected_values.items():
            value = expected.get(key)
            if value is not None and int(value) != actual:
                raise M11PointError(f"LAN expected {key} disagrees with frames")
    return {
        **expected_values,
        "calibrated": calibration_id != 0,
        "frame_count": len(frames),
        "capture_manifest": str(manifest_path.resolve()),
        "capture_manifest_sha256": safety.sha256_file(manifest_path),
    }


def phase_delta_degrees(ch2_phase: float, ch1_phase: float) -> float:
    delta = math.degrees(ch2_phase - ch1_phase)
    return (delta + 180.0) % 360.0 - 180.0


def analyze_point(
    *,
    record: dict[str, Any],
    scope_package: Path,
    capture_dir: Path,
    lan_report: Path,
) -> dict[str, Any]:
    frequency = float(record["frequency_hz"])
    _t1, ch1_values, ch1_rate = load_scope_trace(scope_package / "ch1.npy")
    _t2, ch2_values, ch2_rate = load_scope_trace(scope_package / "ch2.npy")
    fit_ch1 = tone_metrics(ch1_values, ch1_rate, frequency)
    fit_ch2 = tone_metrics(ch2_values, ch2_rate, frequency)
    scope = wavebench_scope_analysis(
        scope_package,
        frequency,
        allow_coherent_prefix=record.get("stage") == "I",
    )
    adc = aggregate_adc_tone(capture_dir, folded_frequency(frequency))
    lan = load_json(lan_report)
    calibration = capture_calibration_identity(capture_dir, lan)
    adc_vpp = adc["metrics"]["fundamental_vpp"]["median"]
    ch1_vpp = float(scope["ch1"]["fundamental_vpp_v"])
    ch2_vpp = float(scope["ch2"]["fundamental_vpp_v"])
    failures: list[str] = list(scope["failures"])
    if not lan.get("pass"):
        failures.append("LAN report did not pass")
    if float(scope["ch1"]["metadata_summary"]["voltage_vpp_v"]) > MAX_CH1_VPP:
        failures.append("RTM CH1 exceeds 0.55 Vpp")
    if float(scope["ch2"]["metadata_summary"]["voltage_vpp_v"]) > MAX_CH2_VPP:
        failures.append("RTM CH2 exceeds 2.35 Vpp")
    if float(scope["ch2"]["metadata_summary"]["voltage_vpp_v"]) > IMMEDIATE_CH2_STOP_VPP:
        failures.append("RTM CH2 crossed the 2.5 Vpp immediate stop threshold")
    fit_crosscheck: dict[str, Any] = {}
    for channel, primary_vpp, fitted in (
        (1, ch1_vpp, fit_ch1),
        (2, ch2_vpp, fit_ch2),
    ):
        fitted_vpp = float(fitted["fundamental_vpp"])
        relative_delta = abs(fitted_vpp - primary_vpp) / primary_vpp
        fft = scope[f"ch{channel}"]["fft"]
        resolution_hz = float(fft["resolution_hz"])
        grid_offset_bins = (
            abs(float(fft["peak_frequency_hz"]) - frequency) / resolution_hz
            if resolution_hz > 0
            else math.inf
        )
        fft_grid_coherent = grid_offset_bins <= 0.05
        crosscheck_limit = (
            0.10 if record.get("stage") == "F" and not fft_grid_coherent else 0.02
        )
        fit_crosscheck[f"ch{channel}"] = {
            "wavebench_fundamental_vpp_v": primary_vpp,
            "least_squares_fundamental_vpp_v": fitted_vpp,
            "relative_delta": relative_delta,
            "fft_grid_offset_bins": grid_offset_bins,
            "fft_grid_coherent": fft_grid_coherent,
            "limit": crosscheck_limit,
            "pass": relative_delta <= crosscheck_limit,
        }
        if relative_delta > crosscheck_limit:
            failures.append(
                f"CH{channel} WaveBench/least-squares fundamental differs by more than "
                f"{crosscheck_limit * 100:g}%"
            )
    if min(ch1_vpp, ch2_vpp, adc_vpp) <= 0:
        failures.append("one signal layer has no positive fundamental amplitude")
    ratios = None
    if min(ch1_vpp, ch2_vpp, adc_vpp) > 0:
        ratios = {
            "ksrc_v_per_vset": ch1_vpp / float(record["source_vpp_v"]),
            "gamp_v_per_v": ch2_vpp / ch1_vpp,
            "kadc_code_per_v": adc_vpp / ch2_vpp,
            "ke2e_code_per_vset_v": adc_vpp / float(record["source_vpp_v"]),
            "candidate_adc_uv_per_code": ch2_vpp * 1_000_000.0 / adc_vpp,
            "candidate_input_uv_per_code": float(record["source_vpp_v"]) * 1_000_000.0 / adc_vpp,
            "ch2_minus_ch1_phase_deg": phase_delta_degrees(
                float(fit_ch2["fundamental_phase_rad"]),
                float(fit_ch1["fundamental_phase_rad"]),
            ),
        }
    return {
        "format": "CycleScope M11 sine point analysis v2",
        "case_id": record["case_id"],
        "source": {
            "frequency_hz": frequency,
            "vpp_v": float(record["source_vpp_v"]),
            "offset_v": 0.0,
            "load_ohm": 50.0,
        },
        "scope_primary": scope,
        "scope_fit_crosscheck": {
            "method": "known-frequency five-harmonic least squares",
            "ch1": fit_ch1,
            "ch2": fit_ch2,
            "comparison": fit_crosscheck,
        },
        "adc": adc,
        "ratios": ratios,
        "calibration": calibration,
        "calibration_id": calibration["calibration_id"],
        "failures": failures,
        "pass": not failures,
    }


def _scope_capture(config: Any, record: dict[str, Any], label: str) -> dict[str, Any]:
    frequency = float(record["frequency_hz"])
    time_range_s, target_cycles = scope_window(record)
    # Formal low-amplitude points measured a loaded gain below 4.7x.  Budget
    # 5x for subsequent points so 0.5 Vpp maps to the 2.5 Vpp immediate-stop
    # boundary while retaining useful RTM resolution at 10 mVpp.
    expected_ch2 = float(record["source_vpp_v"]) * 5.0
    capture_config = replace(
        config,
        waveform=replace(
            config.waveform,
            points="DEF",
            time_range_s=time_range_s,
            expected_frequency_hz=frequency,
            target_cycles=target_cycles,
            window_frequency_hz=frequency,
            vertical_scale_v_per_div=choose_vertical_scale(expected_ch2),
            target_vpp=None,
        ),
    ).with_output_overrides(save_csv=True, save_npy=True, save_screenshot=True)
    logger = CommandLogger()
    base = ScopeService(config=capture_config, logger=logger)
    session = base.open_session()
    started_ns = time.monotonic_ns()
    try:
        service = ScopeService(config=capture_config, logger=logger, session=session)
        before = {channel: service.require_high_impedance(channel) for channel in (1, 2)}
        result = service.capture_waveforms(channels=[1, 2], label=label)
        after = {channel: service.require_high_impedance(channel) for channel in (1, 2)}
    finally:
        session.close()
    finished_ns = time.monotonic_ns()
    metadata = load_json(result.metadata_path)
    failures: list[str] = []
    operation = metadata.get("operation", {})
    if operation.get("channels") != [1, 2] or operation.get("trigger_mode") != "single_acquisition":
        failures.append("scope metadata does not prove one CH1+CH2 acquisition")
    if operation.get("label") != label:
        failures.append("scope metadata label mismatch")
    if result.screenshot_path is None or not result.screenshot_path.is_file():
        failures.append("scope screenshot is missing")
    return {
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": finished_ns,
        "package": str(result.package_dir.resolve()),
        "metadata": str(result.metadata_path.resolve()),
        "couplings_before": {str(key): value for key, value in before.items()},
        "couplings_after": {str(key): value for key, value in after.items()},
        "vertical_scale_v_per_div": capture_config.waveform.vertical_scale_v_per_div,
        "time_range_s": capture_config.waveform.time_range_s,
        "failures": failures,
        "pass": not failures,
    }


def _profile_matches_case(profile: Any, record: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    status = profile.status
    if status.output != "OFF":
        failures.append("DG output is not OFF before point window")
    if status.function.upper() != "SIN":
        failures.append("DG function is not SIN")
    if not math.isclose(status.frequency_hz, float(record["frequency_hz"]), rel_tol=0, abs_tol=0.01):
        failures.append("DG frequency readback mismatch")
    if not math.isclose(status.amplitude, float(record["source_vpp_v"]), rel_tol=0, abs_tol=1e-6):
        failures.append("DG amplitude readback mismatch")
    if profile.load_ohm != 50.0:
        failures.append("DG load readback is not 50 ohm")
    if not math.isclose(status.offset_v, 0.0, rel_tol=0, abs_tol=1e-9):
        failures.append("DG offset readback is not zero")
    if profile.burst_enabled or profile.modulation_enabled or status.sweep_enabled != "OFF":
        failures.append("DG burst/modulation/sweep is not fully OFF")
    return failures


def _write_pre_output_attempt_failure(
    *,
    point_dir: Path,
    record: dict[str, Any],
    phase: str,
    failures: list[str],
    postflight: dict[str, Any],
    transition_path: Path | None,
    run_archives: list[str],
) -> dict[str, Any]:
    """Seal a failed configuration attempt so it can never be retried blindly."""

    payload = {
        "format": "CycleScope M11 pre-output failed attempt v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "case_id": record["case_id"],
        "stage": record["stage"],
        "disposition": "failed_live_attempt",
        "failure_phase": phase,
        "source_configuration_attempted": True,
        "source_output_on_commands": 0,
        "source_output_ever_enabled_by_point": False,
        "scope_acquisition_started": False,
        "formal_lan_acquisition_started": False,
        "lan_preflight_may_have_completed_before_configuration": True,
        "point_measurement_started": False,
        "automatic_retry_authorized": False,
        "transition_evidence": (
            None if transition_path is None else str(transition_path.resolve())
        ),
        "wavebench_run_archive_manifests": run_archives,
        "postflight_evidence": postflight.get("evidence_path"),
        "postflight_pass": postflight.get("pass") is True,
        "restoration_boundary": (
            "output OFF only; old volatile USER waveform restoration is not required"
        ),
        "dp832_writes": False,
        "failures": failures,
        "pass": False,
    }
    path = point_dir / "attempt.json"
    safety.write_json_exclusive(path, payload)
    sums = safety._write_sha256sums(point_dir)
    payload["evidence_path"] = str(path.resolve())
    payload["sha256sums"] = str(sums.resolve())
    return payload


def _i_postflight_after_configuration_failure() -> dict[str, Any]:
    try:
        return i_readonly_preflight()
    except Exception as error:
        return {
            "pass": False,
            "evidence_path": None,
            "failures": [f"{type(error).__name__}: {error}"],
        }


def _archive_failed_configuration_runs(
    *,
    point_dir: Path,
    runs_before: set[Path],
) -> tuple[list[str], list[str]]:
    manifests: list[str] = []
    failures: list[str] = []
    runs_after = safety._run_directories()
    new_runs = sorted(runs_after - runs_before)
    if len(new_runs) != 1:
        failures.append(
            f"configuration failure created {len(new_runs)} WaveBench run directories, expected 1"
        )
    for run_dir in new_runs:
        try:
            manifest = safety.archive_run(
                run_dir,
                point_dir / "wavebench" / "failed-run",
            )
            manifests.append(str(manifest.resolve()))
        except Exception as error:
            failures.append(f"run archive: {type(error).__name__}: {error}")
    return manifests, failures


def run_live(
    *,
    case_id: str,
    frames: int,
    acknowledgement: str,
    stage_acknowledgement: str,
    provisional_discovery_acknowledgement: str | None = None,
    calibration_manifest: Path | None = None,
) -> dict[str, Any]:
    if acknowledgement != LIVE_ACK:
        raise M11PointError(f"live point requires --acknowledge {LIVE_ACK!r}")
    record = load_sine_case(case_id)
    validate_sine_stage_case(record, stage_acknowledgement)
    calibration_identity = expected_calibration_identity(calibration_manifest)
    require_calibration_for_stage(record, calibration_identity)
    formal_gate = physical_gate()
    discovery_gate = provisional_discovery_gate(record)
    provisional_discovery = not formal_gate["pass"]
    if provisional_discovery:
        if provisional_discovery_acknowledgement != PROVISIONAL_DISCOVERY_ACK:
            raise M11PointError(
                "incomplete formal physical gate requires "
                f"--provisional-discovery-acknowledge {PROVISIONAL_DISCOVERY_ACK!r}"
            )
        if not discovery_gate["pass"]:
            raise M11PointError(
                "provisional discovery gate is incomplete: "
                + "; ".join(discovery_gate["failures"])
            )
        execution_gate = discovery_gate
    else:
        execution_gate = formal_gate
    minimum_frames = int(record.get("minimum_frames", MIN_POINT_FRAMES))
    if float(record["source_vpp_v"]) <= 0.02 or record.get("boundary") is True:
        minimum_frames = max(minimum_frames, 64)
    frames = max(frames, minimum_frames)
    before = (
        i_readonly_preflight()
        if str(record.get("stage")) == "I"
        else safety.readonly_preflight()
    )
    if not before.get("pass"):
        raise M11PointError("read-only preflight failed: " + "; ".join(before.get("failures", [])))
    lan_smoke = safety.lan_preflight(
        safety.LIVE_ACK,
        instrument_preflight=before,
        expected_calibration_id=int(calibration_identity["calibration_id"]),
        expected_scale_uv_per_lsb=int(calibration_identity["scale_uv_per_lsb"]),
        expected_offset_uv=int(calibration_identity["offset_uv"]),
    )
    if not lan_smoke.get("pass"):
        raise M11PointError("LAN preflight failed")

    stamp = safety.now_stamp()
    point_dir = EVIDENCE_ROOT / "points" / f"{stamp}_{case_id}"
    point_dir.mkdir(parents=True, exist_ok=False)
    plan_path = point_dir / "source-config-plan.toml"
    plan_path.write_text(plan_text(record), encoding="utf-8")
    config = safety.derived_config()
    plan_record = validate_configuration_plan(plan_path, record, config)
    plan = load_run_plan(plan_path)
    service = RunService(config=config, logger=CommandLogger())
    verify = service.verify(plan)
    transition_path: Path | None = None
    if str(record.get("stage")) == "I":
        before_function = str(
            before.get("source", {}).get("profile", {}).get("status", {}).get(
                "function", ""
            )
        ).upper()
        if before_function == "USER":
            transition = user_to_sine.transition_user_off_to_sine(
                config=config,
                logger=CommandLogger(point_dir / "source-user-to-sine-commands.log"),
                plan_path=plan_path,
                checked_plan=plan_record,
                frequency_hz=float(record["frequency_hz"]),
                source_vpp_v=float(record["source_vpp_v"]),
            )
        elif before_function == "SIN":
            transition = {
                "format": "CycleScope M11 DG USER/OFF to SIN/OFF one-way transaction v1",
                "performed": False,
                "reason": "DG was already SIN/OFF at the checked I preflight",
                "source_output_on_writes": 0,
                "dp832_writes": 0,
                "old_volatile_user_waveform_restored": False,
                "failures": [],
                "pass": True,
            }
        else:
            transition = {
                "format": "CycleScope M11 DG USER/OFF to SIN/OFF one-way transaction v1",
                "performed": False,
                "reason": f"unsupported checked preflight function {before_function!r}",
                "source_output_on_writes": 0,
                "dp832_writes": 0,
                "failures": ["I preflight function changed before source configuration"],
                "pass": False,
            }
        transition_path = point_dir / "user-to-sine-transition.json"
        safety.write_json_exclusive(transition_path, transition)
        if transition.get("pass") is not True:
            postflight = _i_postflight_after_configuration_failure()
            failures = list(transition.get("failures", []))
            failures.extend(
                f"postflight: {item}" for item in postflight.get("failures", [])
            )
            attempt = _write_pre_output_attempt_failure(
                point_dir=point_dir,
                record=record,
                phase="user-off-to-sin-off",
                failures=failures,
                postflight=postflight,
                transition_path=transition_path,
                run_archives=[],
            )
            raise M11PointError(
                "USER/OFF to SIN/OFF transition failed; no point acquisition started; "
                f"evidence={attempt['evidence_path']}"
            )

    runs_before = safety._run_directories()
    try:
        run_result = service.run(plan)
    except Exception as error:
        run_archives, archive_failures = _archive_failed_configuration_runs(
            point_dir=point_dir,
            runs_before=runs_before,
        )
        postflight = _i_postflight_after_configuration_failure()
        failures = [f"{type(error).__name__}: {error}", *archive_failures]
        failures.extend(
            f"postflight: {item}" for item in postflight.get("failures", [])
        )
        attempt = _write_pre_output_attempt_failure(
            point_dir=point_dir,
            record=record,
            phase="checked-source-configuration-plan",
            failures=failures,
            postflight=postflight,
            transition_path=transition_path,
            run_archives=run_archives,
        )
        raise M11PointError(
            "WaveBench source configuration failed; no point acquisition started; "
            f"evidence={attempt['evidence_path']}"
        ) from error
    runs_after = safety._run_directories()
    new_runs = runs_after - runs_before
    if run_result.run_dir.resolve() not in new_runs:
        raise M11PointError("WaveBench run directory was not uniquely created by this point")
    run_archive = safety.archive_run(run_result.run_dir.resolve(), point_dir / "wavebench" / "run")
    run_json = load_json(run_result.run_json_path)
    if run_json.get("status") != "ok":
        postflight = _i_postflight_after_configuration_failure()
        attempt = _write_pre_output_attempt_failure(
            point_dir=point_dir,
            record=record,
            phase="checked-source-configuration-plan-status",
            failures=["WaveBench source configuration run did not pass"],
            postflight=postflight,
            transition_path=transition_path,
            run_archives=[str(run_archive.resolve())],
        )
        raise M11PointError(
            "WaveBench source configuration run did not pass; "
            f"evidence={attempt['evidence_path']}"
        )

    configured = safety.readonly_preflight()
    if not configured.get("pass"):
        raise M11PointError("configured preflight failed")
    # Use the structured preflight payload without reconnecting for comparison.
    source_payload = configured["source"]["profile"]
    status_payload = source_payload["status"]
    profile_failures: list[str] = []
    if status_payload["output"] != "OFF" or status_payload["function"].upper() != "SIN":
        profile_failures.append("configured DG status is not OFF/SIN")
    if not math.isclose(status_payload["frequency_hz"], float(record["frequency_hz"]), abs_tol=0.01):
        profile_failures.append("configured DG frequency mismatch")
    if not math.isclose(status_payload["amplitude"], float(record["source_vpp_v"]), abs_tol=1e-6):
        profile_failures.append("configured DG amplitude mismatch")
    if source_payload["load_ohm"] != 50.0 or status_payload["offset_v"] != 0.0:
        profile_failures.append("configured DG load/offset mismatch")
    if profile_failures:
        raise M11PointError("; ".join(profile_failures))

    raw_before = safety._raw_directories()
    scope_label = f"cyclescope_m11_{case_id}_{stamp}"
    source_logger = CommandLogger(point_dir / "source-window-commands.log")
    source_base = SourceService(config=config, logger=source_logger)
    source_session = source_base.open_session()
    scope_result: dict[str, Any] | None = None
    lan_result: dict[str, Any] | None = None
    output_on_status: dict[str, Any] | None = None
    output_off_status: dict[str, Any] | None = None
    operation_errors: list[str] = []
    on_ns: int | None = None
    off_ns: int | None = None
    try:
        source = SourceService(config=config, logger=source_logger, session=source_session)
        profile = source.channel_profile(1)
        profile_failures = _profile_matches_case(profile, record)
        if profile_failures:
            raise M11PointError("; ".join(profile_failures))
        output_on_status = source.set_output(1, True).as_dict()
        on_ns = time.monotonic_ns()
        time.sleep(1.0)
        with ThreadPoolExecutor(max_workers=2) as executor:
            scope_future = executor.submit(_scope_capture, config, record, scope_label)
            lan_future = executor.submit(
                safety._capture_zero_lan,
                point_dir,
                frames,
                activity_policy="require",
                expected_calibration_id=int(calibration_identity["calibration_id"]),
                expected_scale_uv_per_lsb=int(calibration_identity["scale_uv_per_lsb"]),
                expected_offset_uv=int(calibration_identity["offset_uv"]),
            )
            try:
                scope_result = scope_future.result()
            except Exception as error:
                operation_errors.append(f"scope: {type(error).__name__}: {error}")
            try:
                lan_result = lan_future.result()
            except Exception as error:
                operation_errors.append(f"LAN: {type(error).__name__}: {error}")
    finally:
        try:
            source = SourceService(config=config, logger=source_logger, session=source_session)
            output_off_status = source.set_output(1, False).as_dict()
            off_ns = time.monotonic_ns()
        except Exception as error:
            operation_errors.append(f"source OFF: {type(error).__name__}: {error}")
        finally:
            source_session.close()

    raw_after = safety._raw_directories()
    raw_archives: list[dict[str, Any]] = []
    if scope_result is not None:
        packages = safety._select_new_scope_raw_packages(
            scope_result=scope_result,
            raw_before=raw_before,
            raw_after=raw_after,
        )
        raw_archives = safety.archive_raw_packages(packages, point_dir / "wavebench" / "raw")
    after = safety.readonly_preflight()
    failures = list(operation_errors)
    if not after.get("pass"):
        failures.extend(f"postflight: {item}" for item in after.get("failures", []))
    if scope_result is None or not scope_result.get("pass"):
        failures.append("scope acquisition did not pass")
    if lan_result is None or not lan_result.get("pass"):
        failures.append("LAN acquisition did not pass")
    if not raw_archives:
        failures.append("WaveBench raw package was not archived")
    if output_off_status is None or output_off_status.get("output") != "OFF":
        failures.append("DG OFF readback is missing")
    overlap_ns = None
    if scope_result is not None and lan_result is not None:
        overlap_ns = min(
            scope_result["finished_monotonic_ns"], lan_result["finished_monotonic_ns"]
        ) - max(scope_result["started_monotonic_ns"], lan_result["started_monotonic_ns"])
        if overlap_ns <= 0:
            failures.append("scope and LAN windows do not overlap")
    if on_ns is None or off_ns is None:
        failures.append("DG ON/OFF monotonic window is incomplete")

    analysis = None
    if scope_result is not None and lan_result is not None and raw_archives:
        scope_archive = Path(raw_archives[0]["destination"])
        analysis = analyze_point(
            record=record,
            scope_package=scope_archive,
            capture_dir=Path(lan_result["capture_dir"]),
            lan_report=Path(lan_result["report"]),
        )
        stage = str(record["stage"])
        analysis["evidence_class"] = (
            "provisional-low-amplitude-discovery"
            if provisional_discovery
            else f"formal-stage-{stage.lower()}-point"
        )
        analysis["formal_calibration_eligible"] = (
            not provisional_discovery and stage in {"C", "D", "E"}
        )
        analysis["calibration_role"] = (
            "holdout" if record.get("holdout") is True else "training_candidate"
            if stage in {"C", "D", "E"}
            else "response_only"
        )
        analysis["component_value_basis"] = formal_gate.get("payload", {}).get(
            "component_values", {}
        ).get("basis")
        failures.extend(f"analysis: {item}" for item in analysis["failures"])
        safety.write_json_exclusive(point_dir / "analysis.json", analysis)

    payload = {
        "format": "CycleScope M11 coordinated sine point v2",
        "timestamp": datetime.now().astimezone().isoformat(),
        "case": record,
        "physical_gate": formal_gate,
        "execution_gate": execution_gate,
        "provisional_discovery_gate": discovery_gate,
        "provisional_discovery": provisional_discovery,
        "formal_calibration_eligible": (
            not provisional_discovery and str(record["stage"]) in {"C", "D", "E"}
        ),
        "acknowledgement": acknowledgement,
        "stage_acknowledgement": stage_acknowledgement,
        "expected_calibration_identity": calibration_identity,
        "provisional_discovery_acknowledgement": provisional_discovery_acknowledgement,
        "preflight_evidence": before.get("evidence_path"),
        "lan_preflight_evidence": lan_smoke.get("evidence_path"),
        "configured_preflight_evidence": configured.get("evidence_path"),
        "postflight_evidence": after.get("evidence_path"),
        "plan": plan_record,
        "verify": [
            {"instrument": item.instrument, "idn": item.idn, "resource_sha256": safety.sha256_text(item.resource)}
            for item in verify
        ],
        "wavebench_run_archive_manifest": str(run_archive),
        "user_to_sine_transition_evidence": (
            None if transition_path is None else str(transition_path.resolve())
        ),
        "wavebench_raw_archives": raw_archives,
        "source_window": {
            "on_monotonic_ns": on_ns,
            "off_monotonic_ns": off_ns,
            "on_status": output_on_status,
            "off_status": output_off_status,
            "restoration_boundary": "output OFF; frequency/amplitude remain at this point",
        },
        "scope": scope_result,
        "lan": lan_result,
        "overlap_ns": overlap_ns,
        "analysis": None if analysis is None else "analysis.json",
        "dp800_writes": False,
        "scope_impedance_writes": False,
        "failures": failures,
        "pass": not failures,
    }
    point_path = point_dir / "point.json"
    safety.write_json_exclusive(point_path, payload)
    sums = safety._write_sha256sums(point_dir)
    payload["evidence_path"] = str(point_path)
    payload["sha256sums"] = str(sums)
    return payload


def _verify_point_sha256sums(point_dir: Path) -> dict[str, Any]:
    manifest_path = point_dir / "SHA256SUMS"
    records: list[dict[str, Any]] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        expected, separator, relative_text = line.partition("  ")
        if not separator or len(expected) != 64:
            raise M11PointError(f"invalid SHA256SUMS line in {manifest_path}: {line!r}")
        path = (point_dir / relative_text).resolve()
        try:
            path.relative_to(point_dir.resolve())
        except ValueError as error:
            raise M11PointError(f"SHA256SUMS path escapes point: {relative_text}") from error
        if not path.is_file() or safety.sha256_file(path) != expected:
            raise M11PointError(f"point SHA-256 verification failed: {relative_text}")
        records.append({"path": relative_text, "sha256": expected, "size": path.stat().st_size})
    if not records:
        raise M11PointError(f"empty SHA256SUMS: {manifest_path}")
    return {
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": safety.sha256_file(manifest_path),
        "files_verified": len(records),
        "records": records,
    }


def reanalyze_existing_point(point_dir: Path, output_dir: Path) -> dict[str, Any]:
    point_dir = point_dir.resolve()
    output_dir = output_dir.resolve()
    try:
        point_dir.relative_to((EVIDENCE_ROOT / "points").resolve())
        output_dir.relative_to((EVIDENCE_ROOT / "offline").resolve())
    except ValueError as error:
        raise M11PointError("reanalyze paths must stay inside M11 evidence points/offline") from error
    if output_dir.exists():
        raise M11PointError(f"reanalyze output already exists: {output_dir}")
    verification = _verify_point_sha256sums(point_dir)
    point = load_json(point_dir / "point.json")
    case = point.get("case")
    raw_archives = point.get("wavebench_raw_archives")
    lan = point.get("lan")
    if not isinstance(case, dict) or not isinstance(raw_archives, list) or len(raw_archives) != 1:
        raise M11PointError("point lacks one case/raw archive binding")
    if not isinstance(lan, dict):
        raise M11PointError("point lacks LAN evidence")
    scope_package = Path(str(raw_archives[0].get("destination", ""))).resolve()
    capture_dir = Path(str(lan.get("capture_dir", ""))).resolve()
    lan_report = Path(str(lan.get("report", ""))).resolve()
    for path in (scope_package, capture_dir, lan_report):
        try:
            path.relative_to(point_dir)
        except ValueError as error:
            raise M11PointError(f"reanalyze input escapes point evidence: {path}") from error

    analysis = analyze_point(
        record=case,
        scope_package=scope_package,
        capture_dir=capture_dir,
        lan_report=lan_report,
    )
    stage = str(case.get("stage", "")).lower()
    analysis["evidence_class"] = point.get("analysis") and (
        "provisional-low-amplitude-discovery"
        if point.get("provisional_discovery")
        else f"formal-stage-{stage}-point"
    )
    analysis["formal_calibration_eligible"] = bool(
        point.get("formal_calibration_eligible")
    )
    analysis["component_value_basis"] = point.get("physical_gate", {}).get(
        "payload", {}
    ).get("component_values", {}).get("basis")
    analysis["offline_reanalysis"] = {
        "source_point": str((point_dir / "point.json").resolve()),
        "source_point_sha256": safety.sha256_file(point_dir / "point.json"),
        "source_sha256sums": verification,
        "instrument_io": False,
        "source_point_modified": False,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    analysis_path = output_dir / "analysis-wavebench-primary.json"
    safety.write_json_exclusive(analysis_path, analysis)
    sums = safety._write_sha256sums(output_dir)
    return {
        "pass": analysis["pass"],
        "instrument_io": False,
        "analysis": str(analysis_path),
        "sha256sums": str(sums),
        "failures": analysis["failures"],
    }


def offline_check(case_id: str, calibration_manifest: Path | None = None) -> dict[str, Any]:
    record = load_sine_case(case_id)
    calibration_identity = expected_calibration_identity(calibration_manifest)
    require_calibration_for_stage(record, calibration_identity)
    config = safety.derived_config()
    temporary = EVIDENCE_ROOT / "offline" / f"{safety.now_stamp()}_{case_id}_plan.toml"
    temporary.write_text(plan_text(record), encoding="utf-8")
    plan = validate_configuration_plan(temporary, record, config)
    gate = physical_gate()
    discovery_gate = provisional_discovery_gate(record)
    return {
        "pass": True,
        "instrument_io": False,
        "case": record,
        "expected_calibration_identity": calibration_identity,
        "plan": plan,
        "physical_gate": gate,
        "provisional_discovery_gate": discovery_gate,
        "live_ready": gate["pass"] and record.get("stage") in STAGE_ACKNOWLEDGEMENTS,
        "provisional_live_ready": (
            not gate["pass"]
            and discovery_gate["pass"]
            and record.get("stage") == "C"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--case-id", required=True)
    check.add_argument("--calibration-manifest", type=Path)
    live = commands.add_parser("sine-live")
    live.add_argument("--case-id", required=True)
    live.add_argument("--frames", type=int, default=22)
    live.add_argument("--acknowledge", required=True)
    live.add_argument("--stage-acknowledge", required=True)
    live.add_argument("--provisional-discovery-acknowledge")
    live.add_argument("--calibration-manifest", type=Path)
    reanalyze = commands.add_parser("reanalyze")
    reanalyze.add_argument("--point-dir", type=Path, required=True)
    reanalyze.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "check":
            result = offline_check(args.case_id, args.calibration_manifest)
        elif args.command == "reanalyze":
            result = reanalyze_existing_point(args.point_dir, args.output_dir)
        else:
            result = run_live(
                case_id=args.case_id,
                frames=args.frames,
                acknowledgement=args.acknowledge,
                stage_acknowledgement=args.stage_acknowledge,
                provisional_discovery_acknowledgement=(
                    args.provisional_discovery_acknowledge
                ),
                calibration_manifest=args.calibration_manifest,
            )
    except Exception as error:
        print(f"M11_POINT_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
