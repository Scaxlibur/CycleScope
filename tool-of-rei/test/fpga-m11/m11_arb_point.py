#!/usr/bin/env python3
"""Fail-closed M11 G/H/I ARB coordinator for WaveBench + CSLP LAN."""

# ruff: noqa: E402 -- sibling M11 modules establish the WaveBench import path.

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

import m11_arb_dry_run as arb_dry
import m11_calibration as calibration
import m11_fir_stopband_summary as fir_summary
import m11_repeat_arb_driver as repeat_arb
import m11_sine_point as sine
import m11_wavebench_safe as safety

from wavebench.data.fft import analyze_fft as wavebench_analyze_fft
from wavebench.logging import CommandLogger
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService
from wavebench.services.scope_service import ScopeService
from wavebench.services.source_service import SourceService


MATRIX_ROOT = sine.MATRIX_ROOT
EVIDENCE_ROOT = safety.EVIDENCE_ROOT
CALIBRATION_FIT_DIR = EVIDENCE_ROOT / "offline" / "calibration-fit-v1"
LIVE_ACK = sine.LIVE_ACK
G_STAGE_ACK = "M11_STAGE_G_CONTEST_MULTITONE"
H_STAGE_ACK = "M11_STAGE_H_UB_PLUS_UJ"
I_STAGE_ACK = "M11_STAGE_I_FORMAL_UPPER_FREQUENCY_COMBINATION"
STAGE_ACKNOWLEDGEMENTS = {
    "G": G_STAGE_ACK,
    "H": H_STAGE_ACK,
    "I": I_STAGE_ACK,
}
STAGE_MAX_SOURCE_VPP = {"G": 0.25, "H": 0.45, "I": 0.45}
STAGE_SCOPE_RANGE_S = {"G": 0.002, "H": 0.0002, "I": 0.0002}
CALIBRATION_TARGET_V = 0.003
CALIBRATION_HARD_LIMIT_V = 0.005
INTERMOD_PEAK_LIMIT_V = 0.001
OUTLIER_FLOOR_CODE = 8.0
OUTLIER_MAD_MULTIPLIER = 8.0 * 1.4826
COMMON_REPEAT_HZ = 500.0
RECONSTRUCTION_POINTS = 16_384


class M11ArbPointError(RuntimeError):
    """An M11 ARB point cannot be executed or accepted safely."""


def load_arb_case(case_id: str) -> dict[str, Any]:
    manifest_path = MATRIX_ROOT / "manifest.json"
    manifest = sine.load_json(manifest_path)
    try:
        arb_dry.validate_source_hashes(manifest)
    except RuntimeError as error:
        raise M11ArbPointError(str(error)) from error
    matches = [
        item
        for item in manifest.get("arb_points", [])
        if isinstance(item, dict) and item.get("case_id") == case_id
    ]
    if len(matches) != 1:
        raise M11ArbPointError(
            f"expected exactly one ARB case {case_id!r}, found {len(matches)}"
        )
    record = matches[0]
    try:
        waveform = arb_dry.validate_record(MATRIX_ROOT, record)
    except RuntimeError as error:
        raise M11ArbPointError(str(error)) from error
    stage = str(record.get("stage", ""))
    if stage not in STAGE_ACKNOWLEDGEMENTS:
        raise M11ArbPointError(f"unsupported M11 ARB stage: {stage!r}")
    return {
        **record,
        "waveform_path": str(waveform),
        "matrix_manifest": str(manifest_path.resolve()),
        "matrix_manifest_sha256": safety.sha256_file(manifest_path),
    }


def validate_stage(record: dict[str, Any], acknowledgement: str) -> None:
    stage = str(record["stage"])
    expected = STAGE_ACKNOWLEDGEMENTS[stage]
    if acknowledgement != expected:
        raise M11ArbPointError(
            f"stage {stage} requires --stage-acknowledge {expected!r}"
        )
    maximum = STAGE_MAX_SOURCE_VPP[stage]
    if float(record["source_vpp_v"]) > maximum:
        raise M11ArbPointError(
            f"stage {stage} ARB amplitude must not exceed {maximum:g} Vpp"
        )


def require_nonzero_calibration(manifest: Path | None) -> dict[str, Any]:
    identity = sine.expected_calibration_identity(manifest)
    if int(identity["calibration_id"]) == 0:
        raise M11ArbPointError(
            "formal ARB stages require --calibration-manifest with a validated nonzero ID"
        )
    return identity


def plan_text(record: dict[str, Any]) -> str:
    waveform = json.dumps(str(Path(record["waveform_path"]).resolve()))
    return f'''[experiment]
name = "CycleScope M11 {record['case_id']} ARB configuration"
label = "cyclescope_m11_{record['case_id']}_arb_config"

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
kind = "source.arb_load"
channel = 1
file = {waveform}
frequency_hz = {float(record['playback_frequency_hz']):.12g}
amplitude_vpp = {float(record['source_vpp_v']):.12g}
offset_v = 0.0
max_points = {int(record['points'])}
byte_order = "little"
output_on = false

[[steps]]
kind = "source.status"
channel = 1

[[steps]]
kind = "power.status"
channel = 1
'''


def validate_configuration_plan(
    path: Path, record: dict[str, Any], config: Any
) -> dict[str, Any]:
    plan = load_run_plan(path)
    kinds = [step.kind for step in plan.steps]
    expected = [
        "source.status",
        "power.status",
        "source.arb_load",
        "source.status",
        "power.status",
    ]
    if kinds != expected:
        raise M11ArbPointError(f"ARB configuration plan sequence mismatch: {kinds}")
    if plan.restore.source_state:
        raise M11ArbPointError("ARB configuration plan must not use generic source restore")
    upload = plan.steps[2]
    if upload.fields.get("output_on") is not False:
        raise M11ArbPointError("ARB upload may not enable output")
    if Path(str(upload.fields["file"])).resolve() != Path(record["waveform_path"]).resolve():
        raise M11ArbPointError("ARB plan waveform path mismatch")
    if not math.isclose(
        float(upload.fields["amplitude_vpp"]),
        float(record["source_vpp_v"]),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise M11ArbPointError("ARB plan amplitude mismatch")
    if not math.isclose(
        float(upload.fields["frequency_hz"]),
        float(record["playback_frequency_hz"]),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise M11ArbPointError("ARB plan playback frequency mismatch")
    if any(step.kind in {"power.set", "power.output"} for step in plan.steps):
        raise M11ArbPointError("DP832 writes are forbidden")
    if plan.safety.allow_50ohm or plan.safety.scope_guard_channel != 1:
        raise M11ArbPointError("ARB plan scope guard changed")
    RunService(config=config, logger=CommandLogger()).check(plan)
    return {
        "path": str(path.resolve()),
        "sha256": safety.sha256_file(path),
        "steps": kinds,
        "waveform": str(Path(record["waveform_path"]).resolve()),
        "waveform_sha256": safety.sha256_file(Path(record["waveform_path"])),
        "source_vpp_v": float(record["source_vpp_v"]),
        "playback_frequency_hz": float(record["playback_frequency_hz"]),
        "output_on_during_upload": False,
        "off_gate": (
            "ARB read-only preflight requires OFF and the DG4000 upload driver rechecks OFF "
            "before binary I/O; no redundant fixed-wave OFF write is issued from USER"
        ),
    }


def filter_arb_preflight(generic: dict[str, Any]) -> dict[str, Any]:
    failures = list(generic.get("failures", []))
    status = generic.get("source", {}).get("profile", {}).get("status", {})
    function = str(status.get("function", "")).upper()
    accepted_exception: str | None = None
    function_failures = [
        item
        for item in failures
        if item.startswith("DG CH1 function is not safely restorable:")
    ]
    if function == "USER" and status.get("output") == "OFF":
        if len(function_failures) != 1:
            failures.append("ARB USER function did not produce exactly one generic exception")
        else:
            failures.remove(function_failures[0])
            accepted_exception = function_failures[0]
    elif function_failures:
        failures.append("generic source-function failure is not an OFF/USER ARB state")
    return {
        **generic,
        "format": "CycleScope M11 ARB read-only preflight wrapper v1",
        "generic_preflight_pass": generic.get("pass") is True,
        "generic_preflight_evidence": generic.get("evidence_path"),
        "accepted_arb_exception": accepted_exception,
        "accepted_arb_exception_scope": (
            "only OFF/USER between checked binary uploads; output must remain OFF outside windows"
            if accepted_exception
            else None
        ),
        "instrument_writes": False,
        "failures": failures,
        "pass": not failures,
    }


def arb_readonly_preflight() -> dict[str, Any]:
    generic = safety.readonly_preflight()
    payload = filter_arb_preflight(generic)
    generic_path = Path(str(generic["evidence_path"]))
    output = generic_path.parent / "arb-preflight.json"
    safety.write_json_exclusive(output, payload)
    payload["evidence_path"] = str(output.resolve())
    return payload


def _profile_matches_arb(profile: Any, record: dict[str, Any]) -> list[str]:
    status = profile.status
    failures: list[str] = []
    if status.output != "OFF":
        failures.append("DG output is not OFF before ARB point window")
    if status.function.upper() != "USER":
        failures.append("DG function is not USER after ARB upload")
    if not math.isclose(
        status.frequency_hz,
        float(record["playback_frequency_hz"]),
        rel_tol=0.0,
        abs_tol=0.01,
    ):
        failures.append("DG ARB playback frequency readback mismatch")
    if not math.isclose(
        status.amplitude,
        float(record["source_vpp_v"]),
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        failures.append("DG ARB amplitude readback mismatch")
    if profile.load_ohm != 50.0 or not math.isclose(
        status.offset_v, 0.0, rel_tol=0.0, abs_tol=1e-9
    ):
        failures.append("DG ARB load/offset readback mismatch")
    if profile.burst_enabled or profile.modulation_enabled or status.sweep_enabled != "OFF":
        failures.append("DG burst/modulation/sweep is not fully OFF")
    return failures


def _scope_capture(config: Any, record: dict[str, Any], label: str) -> dict[str, Any]:
    expected_ch2_vpp = float(record["source_vpp_v"]) * 5.0
    capture_config = replace(
        config,
        waveform=replace(
            config.waveform,
            points="DEF",
            time_range_s=STAGE_SCOPE_RANGE_S[str(record["stage"])],
            expected_frequency_hz=None,
            target_cycles=None,
            window_frequency_hz=None,
            vertical_scale_v_per_div=sine.choose_vertical_scale(expected_ch2_vpp),
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
    metadata = sine.load_json(result.metadata_path)
    operation = metadata.get("operation", {})
    failures: list[str] = []
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


def _joint_fit(
    values: np.ndarray, sample_rate_hz: float, frequencies_hz: list[float]
) -> tuple[dict[float, tuple[float, float]], np.ndarray, dict[str, float]]:
    samples = np.asarray(values, dtype=np.float64)
    frequencies = sorted(set(float(value) for value in frequencies_hz))
    if samples.ndim != 1 or samples.size < 32 or not np.all(np.isfinite(samples)):
        raise M11ArbPointError("invalid samples for known-component fit")
    if len(frequencies) != len(frequencies_hz):
        raise M11ArbPointError("known-component frequencies are not unique")
    if any(not 0.0 < value < sample_rate_hz / 2.0 for value in frequencies):
        raise M11ArbPointError("known component is outside sampled Nyquist")
    time_s = np.arange(samples.size, dtype=np.float64) / sample_rate_hz
    columns = [np.ones(samples.size, dtype=np.float64)]
    for frequency_hz in frequencies:
        phase = 2.0 * math.pi * frequency_hz * time_s
        columns.extend((np.sin(phase), np.cos(phase)))
    matrix = np.column_stack(columns)
    coefficients, *_ = np.linalg.lstsq(matrix, samples, rcond=None)
    fitted = matrix @ coefficients
    residual = samples - fitted
    components = {
        frequency_hz: (
            float(coefficients[1 + 2 * index]),
            float(coefficients[2 + 2 * index]),
        )
        for index, frequency_hz in enumerate(frequencies)
    }
    return components, residual, {
        "mean": float(coefficients[0]),
        "residual_rms": float(np.sqrt(np.mean(np.square(residual)))),
        "sample_rate_hz": float(sample_rate_hz),
        "samples": int(samples.size),
    }


def _component_report(
    components: dict[float, tuple[float, float]],
) -> list[dict[str, float]]:
    return [
        {
            "frequency_hz": frequency_hz,
            "sine_coefficient": sine_coefficient,
            "cosine_coefficient": cosine_coefficient,
            "peak": math.hypot(sine_coefficient, cosine_coefficient),
            "phase_rad": math.atan2(cosine_coefficient, sine_coefficient),
        }
        for frequency_hz, (sine_coefficient, cosine_coefficient) in components.items()
    ]


def _scope_analysis(record: dict[str, Any], package: Path) -> dict[str, Any]:
    frequencies = [float(item["frequency_hz"]) for item in record["components"]]
    failures: list[str] = []
    channels: dict[str, Any] = {}
    for channel in (1, 2):
        waveform_path = package / f"ch{channel}.npy"
        waveform = np.load(waveform_path, allow_pickle=False)
        _times, values, sample_rate_hz = sine.load_scope_trace(waveform_path)
        fft = wavebench_analyze_fft(waveform, max_harmonic_order=5)
        components, _residual, fit = _joint_fit(values, sample_rate_hz, frequencies)
        peak_frequency_hz = float(fft["peak_frequency_hz"])
        nearest_frequency_hz = min(
            frequencies, key=lambda value: abs(value - peak_frequency_hz)
        )
        resolution_hz = float(fft["resolution_hz"])
        frequency_error_hz = abs(peak_frequency_hz - nearest_frequency_hz)
        fitted_peak = math.hypot(*components[nearest_frequency_hz])
        wavebench_peak = float(fft["peak_amplitude_v"])
        crosscheck_delta = abs(fitted_peak - wavebench_peak) / wavebench_peak
        if fft.get("warnings"):
            failures.append(f"WaveBench CH{channel} FFT warnings: {fft['warnings']}")
        if frequency_error_hz > resolution_hz * 0.5 + 1e-6:
            failures.append(f"WaveBench CH{channel} peak misses all manifest components")
        if crosscheck_delta > 0.02:
            failures.append(
                f"WaveBench CH{channel} global peak and known-component fit differ by more than 2%"
            )
        raw_vpp = float(np.ptp(values))
        if channel == 1 and raw_vpp > sine.MAX_CH1_VPP:
            failures.append("RTM CH1 exceeds 0.55 Vpp")
        if channel == 2 and raw_vpp > sine.MAX_CH2_VPP:
            failures.append("RTM CH2 exceeds 2.35 Vpp")
        if channel == 2 and raw_vpp > sine.IMMEDIATE_CH2_STOP_VPP:
            failures.append("RTM CH2 crossed the 2.5 Vpp immediate stop threshold")
        channels[f"ch{channel}"] = {
            "npy": str(waveform_path.resolve()),
            "npy_sha256": safety.sha256_file(waveform_path),
            "wavebench_fft": fft,
            "known_component_fit": {
                **fit,
                "components": _component_report(components),
            },
            "global_peak_crosscheck": {
                "nearest_manifest_frequency_hz": nearest_frequency_hz,
                "frequency_error_hz": frequency_error_hz,
                "wavebench_peak_v": wavebench_peak,
                "least_squares_peak_v": fitted_peak,
                "relative_delta": crosscheck_delta,
                "limit": 0.02,
                "pass": crosscheck_delta <= 0.02,
            },
            "raw_vpp_v": raw_vpp,
        }
    return {
        "primary_method": (
            "WaveBench archived NPY + wavebench.data.fft.analyze_fft for the global "
            "spectrum; manifest-frequency joint least squares decomposes every line"
        ),
        "screenshots_used_for_numeric_results": False,
        "channels": channels,
        "failures": failures,
        "pass": not failures,
    }


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        raise M11ArbPointError("cannot summarize an empty distribution")
    return {
        "minimum": float(min(values)),
        "median": float(statistics.median(values)),
        "empirical_p95": float(np.quantile(values, 0.95, method="linear")),
        "maximum": float(max(values)),
    }


def _amplitude_factor(model: dict[str, Any], source_vpp_v: float) -> float:
    rows = model["amplitude_rows"]
    amplitudes = np.asarray([float(row["source_vpp_v"]) for row in rows])
    factors = np.asarray([float(row["global_gain_factor"]) for row in rows])
    if source_vpp_v < amplitudes[0] or source_vpp_v > amplitudes[-1]:
        raise M11ArbPointError("ARB total Vpp is outside the frozen calibration range")
    return float(np.interp(source_vpp_v, amplitudes, factors))


def _band_target(record: dict[str, Any]) -> dict[str, Any]:
    source_case = record.get("u_b_source_case")
    if source_case is None:
        return record
    return load_arb_case(str(source_case))


def _residual_spur_peak(residual: np.ndarray) -> tuple[float, float]:
    samples = residual.size
    window = np.hanning(samples)
    coherent_gain = float(np.mean(window))
    spectrum = np.fft.rfft(residual * window)
    frequencies = np.fft.rfftfreq(samples, d=1.0 / sine.OUTPUT_SAMPLE_RATE_HZ)
    amplitudes = np.abs(spectrum) * 2.0 / (samples * coherent_gain)
    mask = (frequencies >= 1_000.0) & (frequencies <= 500_000.0)
    if not np.any(mask):
        raise M11ArbPointError("ADC residual spectrum has no effective-band bins")
    indices = np.flatnonzero(mask)
    index = int(indices[np.argmax(amplitudes[mask])])
    return float(frequencies[index]), float(amplitudes[index])


def _adc_analysis(
    record: dict[str, Any], capture_dir: Path, model: dict[str, Any]
) -> dict[str, Any]:
    all_components = list(record["components"])
    folded_by_input: dict[float, float] = {}
    for component in all_components:
        input_frequency_hz = float(component["frequency_hz"])
        folded_by_input[input_frequency_hz] = sine.folded_frequency(input_frequency_hz)
    if len(set(folded_by_input.values())) != len(folded_by_input):
        raise M11ArbPointError("ARB components collide after output-rate folding")

    target = _band_target(record)
    expected_band = [
        component
        for component in record["components"]
        if component.get("role") != "u_J" and float(component["frequency_hz"]) <= 500_000.0
    ]
    expected_target_components = {
        float(component["frequency_hz"]): float(component["peak_v"])
        for component in target["components"]
        if component.get("role") != "u_J" and float(component["frequency_hz"]) <= 500_000.0
    }
    if {float(item["frequency_hz"]) for item in expected_band} != set(
        expected_target_components
    ):
        raise M11ArbPointError("ARB band target components do not match the live stimulus")

    factor = _amplitude_factor(model, float(record["source_vpp_v"]))
    gain_by_frequency = {
        frequency_hz: calibration._linear_interpolate(
            model["response_rows"], "ke2e_code_per_v", frequency_hz
        )
        * factor
        for frequency_hz in expected_target_components
    }
    minimum_band_gain = min(gain_by_frequency.values())
    frame_paths = sorted(capture_dir.glob("frame_*.s16le"))
    if len(frame_paths) < sine.MIN_POINT_FRAMES:
        raise M11ArbPointError("ARB point has fewer than 22 complete ADC frames")

    recovered_line_peaks: dict[float, list[float]] = {
        frequency_hz: [] for frequency_hz in expected_target_components
    }
    recovered_vpp: list[float] = []
    recovered_rms: list[float] = []
    residual_spur_peak_v: list[float] = []
    u_j_vpp_code: list[float] = []
    frame_records: list[dict[str, Any]] = []
    all_outliers: list[dict[str, Any]] = []
    dense_time = np.arange(RECONSTRUCTION_POINTS, dtype=np.float64) / (
        COMMON_REPEAT_HZ * RECONSTRUCTION_POINTS
    )

    u_j_components = [item for item in all_components if item.get("role") == "u_J"]
    for frame_index, path in enumerate(frame_paths):
        raw = np.fromfile(path, dtype="<i2")
        if raw.size != sine.FRAME_SAMPLES:
            raise M11ArbPointError(f"{path}: incomplete ADC frame")
        values = raw.astype(np.float64)
        folded_frequencies = [
            folded_by_input[float(item["frequency_hz"])] for item in all_components
        ]
        coefficients, residual, fit = _joint_fit(
            values, sine.OUTPUT_SAMPLE_RATE_HZ, folded_frequencies
        )
        reconstructed = np.zeros(RECONSTRUCTION_POINTS, dtype=np.float64)
        line_record: dict[str, float] = {}
        for frequency_hz in expected_target_components:
            folded_hz = folded_by_input[frequency_hz]
            sine_code, cosine_code = coefficients[folded_hz]
            gain = gain_by_frequency[frequency_hz]
            sine_v = sine_code / gain
            cosine_v = cosine_code / gain
            recovered_peak = math.hypot(sine_v, cosine_v)
            recovered_line_peaks[frequency_hz].append(recovered_peak)
            line_record[f"{frequency_hz:g}"] = recovered_peak
            phase = 2.0 * math.pi * frequency_hz * dense_time
            reconstructed += sine_v * np.sin(phase) + cosine_v * np.cos(phase)
        recovered_vpp.append(float(np.ptp(reconstructed)))
        recovered_rms.append(float(np.sqrt(np.mean(np.square(reconstructed)))))

        spur_frequency_hz, spur_peak_code = _residual_spur_peak(residual)
        spur_peak_v = spur_peak_code / minimum_band_gain
        residual_spur_peak_v.append(spur_peak_v)
        residual_median = float(np.median(residual))
        residual_mad = float(np.median(np.abs(residual - residual_median)))
        threshold = max(OUTLIER_FLOOR_CODE, OUTLIER_MAD_MULTIPLIER * residual_mad)
        indices = np.flatnonzero(np.abs(residual - residual_median) > threshold)
        positions = [
            {
                "frame_index": frame_index,
                "sample_index": int(index),
                "raw_code": int(raw[index]),
                "residual_code": float(residual[index]),
            }
            for index in indices
        ]
        all_outliers.extend(positions)
        for component in u_j_components:
            folded_hz = folded_by_input[float(component["frequency_hz"])]
            u_j_vpp_code.append(2.0 * math.hypot(*coefficients[folded_hz]))
        frame_records.append(
            {
                "file": path.name,
                "known_fit_residual_rms_code": fit["residual_rms"],
                "recovered_line_peak_v": line_record,
                "recovered_band_vpp_v": recovered_vpp[-1],
                "recovered_band_true_rms_v": recovered_rms[-1],
                "largest_residual_spur_frequency_hz": spur_frequency_hz,
                "largest_residual_spur_input_peak_v": spur_peak_v,
                "outlier_threshold_code": threshold,
                "outlier_count": len(positions),
            }
        )

    line_results: list[dict[str, Any]] = []
    failures: list[str] = []
    target_warnings: list[str] = []
    for frequency_hz, expected_peak_v in expected_target_components.items():
        distribution = _distribution(recovered_line_peaks[frequency_hz])
        error_v = float(distribution["median"] - expected_peak_v)
        absolute_error_v = abs(error_v)
        target_pass = absolute_error_v <= CALIBRATION_TARGET_V
        hard_pass = absolute_error_v <= CALIBRATION_HARD_LIMIT_V
        if not target_pass:
            target_warnings.append(
                f"{frequency_hz:g} Hz line misses the 3 mV target"
            )
        if not hard_pass:
            failures.append(f"{frequency_hz:g} Hz line misses the 5 mV hard limit")
        line_results.append(
            {
                "frequency_hz": frequency_hz,
                "expected_peak_v": expected_peak_v,
                "recovered_peak_v": distribution,
                "error_v": error_v,
                "absolute_error_v": absolute_error_v,
                "target_limit_v": CALIBRATION_TARGET_V,
                "hard_limit_v": CALIBRATION_HARD_LIMIT_V,
                "target_pass": target_pass,
                "hard_pass": hard_pass,
            }
        )

    expected_vpp = float(target["source_vpp_v"])
    expected_rms = float(target["true_rms_v"])
    vpp_distribution = _distribution(recovered_vpp)
    rms_distribution = _distribution(recovered_rms)
    aggregate_metrics: dict[str, Any] = {}
    for name, expected, distribution in (
        ("vpp", expected_vpp, vpp_distribution),
        ("true_rms", expected_rms, rms_distribution),
    ):
        error_v = float(distribution["median"] - expected)
        absolute_error_v = abs(error_v)
        target_pass = absolute_error_v <= CALIBRATION_TARGET_V
        hard_pass = absolute_error_v <= CALIBRATION_HARD_LIMIT_V
        if not target_pass:
            target_warnings.append(f"recovered {name} misses the 3 mV target")
        if not hard_pass:
            failures.append(f"recovered {name} misses the 5 mV hard limit")
        aggregate_metrics[name] = {
            "expected_v": expected,
            "recovered_v": distribution,
            "error_v": error_v,
            "absolute_error_v": absolute_error_v,
            "target_limit_v": CALIBRATION_TARGET_V,
            "hard_limit_v": CALIBRATION_HARD_LIMIT_V,
            "target_pass": target_pass,
            "hard_pass": hard_pass,
        }

    intermod = _distribution(residual_spur_peak_v)
    intermod_pass = float(intermod["empirical_p95"]) < INTERMOD_PEAK_LIMIT_V
    if not intermod_pass:
        failures.append("effective-band residual/intermod p95 is not below 1 mVpeak")
    return {
        "frame_count": len(frame_paths),
        "sample_rate_hz": sine.OUTPUT_SAMPLE_RATE_HZ,
        "calibration": {
            "total_source_vpp_v_for_global_factor": float(record["source_vpp_v"]),
            "global_amplitude_factor": factor,
            "gain_code_per_v_by_frequency": {
                f"{key:g}": value for key, value in gain_by_frequency.items()
            },
            "frequency_policy": "piecewise-linear response.csv; no extrapolation",
        },
        "band_target_case_id": target["case_id"],
        "line_results": line_results,
        "aggregate_metrics": aggregate_metrics,
        "effective_band_residual_intermod": {
            "largest_spur_input_peak_v": intermod,
            "limit_vpeak": INTERMOD_PEAK_LIMIT_V,
            "pass": intermod_pass,
            "policy": (
                "known manifest components are jointly fitted first; Hann residual peak "
                "uses the minimum calibrated band gain and no noise subtraction"
            ),
        },
        "u_j_residual_vpp_code": None if not u_j_vpp_code else _distribution(u_j_vpp_code),
        "outliers": {
            "count": len(all_outliers),
            "rate": float(len(all_outliers) / (len(frame_paths) * sine.FRAME_SAMPLES)),
            "positions": all_outliers,
            "policy": (
                "report-only joint-known-tone residual threshold=max(8 code, "
                "8*1.4826*MAD); raw samples are never removed or replaced"
            ),
        },
        "frames": frame_records,
        "raw_samples_modified": False,
        "target_warnings": target_warnings,
        "failures": failures,
        "pass": not failures,
    }


def _scope_component_peak(
    scope: dict[str, Any], channel: int, frequency_hz: float
) -> float:
    components = scope["channels"][f"ch{channel}"]["known_component_fit"]["components"]
    matches = [
        item
        for item in components
        if math.isclose(float(item["frequency_hz"]), frequency_hz, abs_tol=1e-6)
    ]
    if len(matches) != 1:
        raise M11ArbPointError("scope component fit does not contain the requested line")
    return float(matches[0]["peak"])


def analyze_point(
    *,
    record: dict[str, Any],
    scope_package: Path,
    capture_dir: Path,
    lan_report_path: Path,
) -> dict[str, Any]:
    fit, _verification = calibration.load_frozen_fit(CALIBRATION_FIT_DIR)
    scope = _scope_analysis(record, scope_package)
    adc = _adc_analysis(record, capture_dir, fit["model"])
    lan = sine.load_json(lan_report_path)
    observed_calibration = sine.capture_calibration_identity(capture_dir, lan)
    failures = [f"scope: {item}" for item in scope["failures"]]
    failures.extend(f"ADC: {item}" for item in adc["failures"])
    if lan.get("pass") is not True:
        failures.append("LAN report did not pass")

    interference: dict[str, Any] | None = None
    u_j = [item for item in record["components"] if item.get("role") == "u_J"]
    if u_j:
        if len(u_j) != 1 or adc["u_j_residual_vpp_code"] is None:
            failures.append("exactly one u_J component and ADC residual are required")
        else:
            input_frequency_hz = float(u_j[0]["frequency_hz"])
            ch2_vpp_v = 2.0 * _scope_component_peak(scope, 2, input_frequency_hz)
            kadc = fir_summary.reference_kadc(fit)
            residual_vpp_code = float(
                adc["u_j_residual_vpp_code"]["empirical_p95"]
            )
            attenuation_db = fir_summary.attenuation_lower_bound_db(
                ch2_vpp_v,
                float(kadc["minimum_code_per_v"]),
                residual_vpp_code,
            )
            interference = {
                "input_frequency_hz": input_frequency_hz,
                "folded_frequency_hz": sine.folded_frequency(input_frequency_hz),
                "ch2_vpp_v": ch2_vpp_v,
                "reference_kadc_minimum_code_per_v": kadc["minimum_code_per_v"],
                "adc_residual_vpp_code_p95": residual_vpp_code,
                "attenuation_lower_bound_db": attenuation_db,
                "limit_db": fir_summary.ATTENUATION_LIMIT_DB,
                "pass": attenuation_db >= fir_summary.ATTENUATION_LIMIT_DB,
            }
            if not interference["pass"]:
                failures.append("u_J attenuation lower bound is below 50 dB")
    return {
        "format": "CycleScope M11 coordinated ARB point analysis v1",
        "case_id": record["case_id"],
        "stage": record["stage"],
        "scope_primary": scope,
        "adc_recovery": adc,
        "interference_rejection": interference,
        "calibration": observed_calibration,
        "screenshots_used_for_numeric_results": False,
        "raw_samples_modified": False,
        "failures": failures,
        "pass": not failures,
    }


def _minimum_frames(record: dict[str, Any], requested: int) -> int:
    minimum = max(sine.MIN_POINT_FRAMES, int(record.get("minimum_frames", 0)))
    if record.get("stage") in {"G", "H", "I"}:
        minimum = max(minimum, 64)
    return max(requested, minimum)


def _ensure_arb_off(config: Any, logger: CommandLogger) -> dict[str, Any]:
    base = SourceService(config=config, logger=logger)
    session = base.open_session()
    try:
        source = SourceService(config=config, logger=logger, session=session)
        off = source.set_output(1, False).as_dict()
        if off.get("output") != "OFF":
            raise M11ArbPointError("DG OFF readback failed during ARB recovery")
        if off.get("function") != "USER":
            raise M11ArbPointError("DG USER/OFF readback failed during ARB recovery")
        return off
    finally:
        session.close()


def run_live(
    *,
    case_id: str,
    frames: int,
    acknowledgement: str,
    stage_acknowledgement: str,
    calibration_manifest: Path | None,
) -> dict[str, Any]:
    if acknowledgement != LIVE_ACK:
        raise M11ArbPointError(f"live ARB point requires --acknowledge {LIVE_ACK!r}")
    record = load_arb_case(case_id)
    validate_stage(record, stage_acknowledgement)
    calibration_identity = require_nonzero_calibration(calibration_manifest)
    physical = sine.physical_gate()
    if not physical["pass"]:
        raise M11ArbPointError(
            "formal physical gate is incomplete: " + "; ".join(physical["failures"])
        )
    frames = _minimum_frames(record, frames)

    before = arb_readonly_preflight()
    if not before.get("pass"):
        raise M11ArbPointError(
            "read-only preflight failed: " + "; ".join(before.get("failures", []))
        )
    lan_smoke = safety.lan_preflight(
        safety.LIVE_ACK,
        instrument_preflight=before,
        expected_calibration_id=int(calibration_identity["calibration_id"]),
        expected_scale_uv_per_lsb=int(calibration_identity["scale_uv_per_lsb"]),
        expected_offset_uv=int(calibration_identity["offset_uv"]),
    )
    if not lan_smoke.get("pass"):
        raise M11ArbPointError("LAN preflight failed")

    stamp = safety.now_stamp()
    point_dir = EVIDENCE_ROOT / "points" / f"{stamp}_{case_id}"
    point_dir.mkdir(parents=True, exist_ok=False)
    plan_path = point_dir / "source-arb-config-plan.toml"
    plan_path.write_text(plan_text(record), encoding="utf-8")
    config = safety.derived_config()
    plan_record = validate_configuration_plan(plan_path, record, config)
    plan = load_run_plan(plan_path)
    service = RunService(config=config, logger=CommandLogger())
    verify = service.verify(plan)
    initial_function = str(
        before["source"]["profile"]["status"]["function"]
    ).upper()
    arb_loaded = False
    configuration_mode: str
    if initial_function == "SIN":
        configuration_mode = "wavebench-run-service-basic-to-user"
        runs_before = safety._run_directories()
        run_result = service.run(plan)
        arb_loaded = True
        try:
            runs_after = safety._run_directories()
            new_runs = runs_after - runs_before
            if run_result.run_dir.resolve() not in new_runs:
                raise M11ArbPointError(
                    "WaveBench ARB run directory was not uniquely created"
                )
            run_archive = safety.archive_run(
                run_result.run_dir.resolve(), point_dir / "wavebench" / "run"
            )
            run_json = sine.load_json(run_result.run_json_path)
            if run_json.get("status") != "ok":
                raise M11ArbPointError("WaveBench ARB configuration run did not pass")
        except Exception as error:
            try:
                _ensure_arb_off(
                    config,
                    CommandLogger(point_dir / "source-config-failure-restore.log"),
                )
            except Exception as restore_error:
                raise M11ArbPointError(
                    f"ARB configuration evidence failed and USER/OFF recovery also failed: "
                    f"{type(restore_error).__name__}: {restore_error}"
                ) from error
            raise
    elif initial_function == "USER":
        configuration_mode = "hash-bound-wavebench-repeat-user-to-user"
        repeat_dir = point_dir / "wavebench" / "run" / "repeat-arb-upload"
        repeat_dir.mkdir(parents=True, exist_ok=False)
        repeat_logger = CommandLogger(repeat_dir / "commands.log")
        try:
            repeat_result = repeat_arb.upload_repeated_arb(
                config=config,
                logger=repeat_logger,
                waveform=Path(record["waveform_path"]),
                playback_frequency_hz=float(record["playback_frequency_hz"]),
                amplitude_vpp=float(record["source_vpp_v"]),
                points=int(record["points"]),
            )
            arb_loaded = True
            repeat_payload = {
                "format": "CycleScope M11 hash-bound WaveBench repeated ARB upload v1",
                "timestamp": datetime.now().astimezone().isoformat(),
                "case_id": case_id,
                "checked_plan": plan_record,
                "result": repeat_result,
                "pass": True,
            }
            repeat_path = repeat_dir / "run.json"
            safety.write_json_exclusive(repeat_path, repeat_payload)
            safety._write_sha256sums(repeat_dir)
            run_archive = repeat_path
        except Exception as error:
            failure_path = repeat_dir / "failure.json"
            safety.write_json_exclusive(
                failure_path,
                {
                    "format": "CycleScope M11 repeated ARB upload failure v1",
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "case_id": case_id,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "output_was_never_requested_on": True,
                    "pass": False,
                },
            )
            safety._write_sha256sums(repeat_dir)
            raise
    else:
        raise M11ArbPointError(
            f"ARB campaign requires SIN/OFF or USER/OFF, got {initial_function!r}"
        )

    raw_before = safety._raw_directories()
    scope_label = f"cyclescope_m11_{case_id}_{stamp}"
    source_logger = CommandLogger(point_dir / "source-window-commands.log")
    source_session = None
    configured: dict[str, Any] | None = None
    scope_result: dict[str, Any] | None = None
    lan_result: dict[str, Any] | None = None
    output_on_status: dict[str, Any] | None = None
    output_off_status: dict[str, Any] | None = None
    operation_errors: list[str] = []
    on_ns: int | None = None
    off_ns: int | None = None
    try:
        configured = arb_readonly_preflight()
        if not configured.get("pass"):
            operation_errors.append(
                "configured ARB preflight: " + "; ".join(configured.get("failures", []))
            )
        source_base = SourceService(config=config, logger=source_logger)
        source_session = source_base.open_session()
        source = SourceService(config=config, logger=source_logger, session=source_session)
        profile_failures = _profile_matches_arb(source.channel_profile(1), record)
        if profile_failures:
            operation_errors.extend(profile_failures)
        if not operation_errors:
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
            if source_session is None and arb_loaded:
                source_base = SourceService(config=config, logger=source_logger)
                source_session = source_base.open_session()
            if source_session is not None:
                source = SourceService(
                    config=config, logger=source_logger, session=source_session
                )
                output_off_status = source.set_output(1, False).as_dict()
                off_ns = time.monotonic_ns()
        except Exception as error:
            operation_errors.append(f"source USER/OFF recovery: {type(error).__name__}: {error}")
        finally:
            if source_session is not None:
                source_session.close()

    raw_after = safety._raw_directories()
    raw_archives: list[dict[str, Any]] = []
    if scope_result is not None:
        packages = safety._select_new_scope_raw_packages(
            scope_result=scope_result,
            raw_before=raw_before,
            raw_after=raw_after,
        )
        raw_archives = safety.archive_raw_packages(
            packages, point_dir / "wavebench" / "raw"
        )
    after = arb_readonly_preflight()
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
    if output_off_status is None or output_off_status.get("function") != "USER":
        failures.append("DG USER/OFF ARB end-state readback is missing")
    overlap_ns = None
    if scope_result is not None and lan_result is not None:
        overlap_ns = min(
            scope_result["finished_monotonic_ns"], lan_result["finished_monotonic_ns"]
        ) - max(
            scope_result["started_monotonic_ns"], lan_result["started_monotonic_ns"]
        )
        if overlap_ns <= 0:
            failures.append("scope and LAN windows do not overlap")

    analysis: dict[str, Any] | None = None
    if scope_result is not None and lan_result is not None and raw_archives:
        analysis = analyze_point(
            record=record,
            scope_package=Path(raw_archives[0]["destination"]),
            capture_dir=Path(lan_result["capture_dir"]),
            lan_report_path=Path(lan_result["report"]),
        )
        for key in ("calibration_id", "scale_uv_per_lsb", "offset_uv"):
            if int(analysis["calibration"].get(key, -1)) != int(
                calibration_identity[key]
            ):
                analysis["failures"].append(f"calibrated metadata {key} mismatch")
                analysis["pass"] = False
        failures.extend(f"analysis: {item}" for item in analysis["failures"])
        safety.write_json_exclusive(point_dir / "analysis.json", analysis)

    payload = {
        "format": "CycleScope M11 coordinated ARB point v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "case": record,
        "physical_gate": physical,
        "acknowledgement": acknowledgement,
        "stage_acknowledgement": stage_acknowledgement,
        "expected_calibration_identity": calibration_identity,
        "preflight_evidence": before.get("evidence_path"),
        "lan_preflight_evidence": lan_smoke.get("evidence_path"),
        "configured_arb_preflight_evidence": (
            None if configured is None else configured.get("evidence_path")
        ),
        "postflight_evidence": after.get("evidence_path"),
        "plan": plan_record,
        "verify": [
            {
                "instrument": item.instrument,
                "idn": item.idn,
                "resource_sha256": safety.sha256_text(item.resource),
            }
            for item in verify
        ],
        "wavebench_run_archive_manifest": str(run_archive),
        "arb_configuration_mode": configuration_mode,
        "wavebench_raw_archives": raw_archives,
        "source_window": {
            "on_monotonic_ns": on_ns,
            "off_monotonic_ns": off_ns,
            "on_status": output_on_status,
            "off_status": output_off_status,
            "restoration_boundary": (
                "output OFF; USER intentionally remains for the next checked ARB upload; "
                "no unvalidated USER-to-SIN transaction is attempted"
            ),
        },
        "scope": scope_result,
        "lan": lan_result,
        "overlap_ns": overlap_ns,
        "analysis": None if analysis is None else "analysis.json",
        "dp800_writes": False,
        "scope_impedance_writes": False,
        "screenshots_used_for_numeric_results": False,
        "failures": failures,
        "pass": not failures,
    }
    point_path = point_dir / "point.json"
    safety.write_json_exclusive(point_path, payload)
    sums = safety._write_sha256sums(point_dir)
    payload["evidence_path"] = str(point_path)
    payload["sha256sums"] = str(sums)
    return payload


def offline_check(
    case_id: str, calibration_manifest: Path | None
) -> dict[str, Any]:
    record = load_arb_case(case_id)
    identity = require_nonzero_calibration(calibration_manifest)
    config = safety.derived_config()
    temporary = EVIDENCE_ROOT / "offline" / f"{safety.now_stamp()}_{case_id}_arb-plan.toml"
    temporary.write_text(plan_text(record), encoding="utf-8")
    plan = validate_configuration_plan(temporary, record, config)
    gate = sine.physical_gate()
    return {
        "pass": True,
        "instrument_io": False,
        "case": record,
        "expected_calibration_identity": identity,
        "plan": plan,
        "physical_gate": gate,
        "live_ready": gate["pass"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--case-id", required=True)
    check.add_argument("--calibration-manifest", type=Path, required=True)
    live = commands.add_parser("arb-live")
    live.add_argument("--case-id", required=True)
    live.add_argument("--frames", type=int, default=sine.MIN_POINT_FRAMES)
    live.add_argument("--acknowledge", required=True)
    live.add_argument("--stage-acknowledge", required=True)
    live.add_argument("--calibration-manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "check":
            result = offline_check(args.case_id, args.calibration_manifest)
        else:
            result = run_live(
                case_id=args.case_id,
                frames=args.frames,
                acknowledgement=args.acknowledge,
                stage_acknowledgement=args.stage_acknowledge,
                calibration_manifest=args.calibration_manifest,
            )
    except Exception as error:
        print(
            f"M11_ARB_POINT_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
