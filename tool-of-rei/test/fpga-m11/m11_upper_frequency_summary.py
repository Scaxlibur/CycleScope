#!/usr/bin/env python3
"""Build the immutable M11-I 4..10 MHz sine and combination summary."""

# ruff: noqa: E402 -- adjacent M11 modules are deliberate evidence inputs.

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m11_arb_point as arb
import m11_calibration as calibration
import m11_fir_stopband_summary as fir
import m11_multitone_summary as multitone
import m11_repeat_arb_driver as repeat_arb
import m11_sine_point as sine
import m11_wavebench_safe as safety


SUMMARY_FORMAT = "CycleScope M11-I formal 4-to-10-MHz coverage summary v1"
CALIBRATION_MANIFEST = multitone.CALIBRATION_MANIFEST
FFT_MAX_GRID_OFFSET_BINS = 0.5
SCOPE_CROSSCHECK_LIMIT = 0.02
I_SINE_REANALYSIS_ROOT = (
    safety.EVIDENCE_ROOT / "offline" / "i-sine-reanalysis-v1"
)
REANALYZABLE_I_SINE_CASES = {"i-formal-7.2e+06Hz"}
EXPECTED_7P2_ANALYSIS_FAILURES = {
    "WaveBench CH1 FFT frequency differs by more than 1%",
    "WaveBench CH2 FFT frequency differs by more than 1%",
    "CH1 WaveBench/least-squares fundamental differs by more than 2%",
    "CH2 WaveBench/least-squares fundamental differs by more than 2%",
}


class UpperFrequencySummaryError(RuntimeError):
    """The M11-I evidence set is incomplete, stale, or ambiguous."""


def i_cases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = sine.load_json(sine.MATRIX_ROOT / "manifest.json")
    try:
        arb.arb_dry.validate_source_hashes(manifest)
    except RuntimeError as error:
        raise UpperFrequencySummaryError(str(error)) from error
    sine_records = [
        item
        for item in manifest.get("sine_points", [])
        if isinstance(item, dict) and item.get("stage") == "I"
    ]
    arb_records = [
        item
        for item in manifest.get("arb_points", [])
        if isinstance(item, dict) and item.get("stage") == "I"
    ]
    if (
        len(sine_records) != 5
        or {float(item["frequency_hz"]) for item in sine_records}
        != {4e6, 5e6, 7.2e6, 7.5e6, 10e6}
        or len(arb_records) != 2
        or {float(item["u_j_frequency_hz"]) for item in arb_records} != {5e6, 10e6}
        or len(
            {item["case_id"] for item in [*sine_records, *arb_records]}
        )
        != 7
    ):
        raise UpperFrequencySummaryError("matrix-v3 does not contain the exact I grid")
    return sine_records, arb_records


def _recompute_sine_analysis(
    point_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    raw_archives = payload.get("wavebench_raw_archives")
    lan = payload.get("lan")
    case = payload.get("case")
    if (
        not isinstance(raw_archives, list)
        or len(raw_archives) != 1
        or not isinstance(lan, dict)
        or not isinstance(case, dict)
    ):
        raise UpperFrequencySummaryError("I sine point lacks raw/LAN/case bindings")
    scope_package = Path(str(raw_archives[0].get("destination", ""))).resolve()
    capture_dir = Path(str(lan.get("capture_dir", ""))).resolve()
    lan_report = Path(str(lan.get("report", ""))).resolve()
    for path in (scope_package, capture_dir, lan_report):
        try:
            path.relative_to(point_dir.resolve())
        except ValueError as error:
            raise UpperFrequencySummaryError(
                f"I sine reanalysis input escapes point: {path}"
            ) from error
    recomputed = sine.analyze_point(
        record=case,
        scope_package=scope_package,
        capture_dir=capture_dir,
        lan_report=lan_report,
    )
    stage = str(case.get("stage", "")).lower()
    recomputed["evidence_class"] = payload.get("analysis") and (
        "provisional-low-amplitude-discovery"
        if payload.get("provisional_discovery")
        else f"formal-stage-{stage}-point"
    )
    recomputed["formal_calibration_eligible"] = bool(
        payload.get("formal_calibration_eligible")
    )
    recomputed["component_value_basis"] = payload.get("physical_gate", {}).get(
        "payload", {}
    ).get("component_values", {}).get("basis")
    return recomputed


def validated_sine_analysis(
    point_dir: Path,
    case_id: str,
) -> dict[str, Any]:
    """Return the immutable live verdict or one narrowly validated offline correction."""

    point_dir = point_dir.resolve()
    point_path = point_dir / "point.json"
    original_path = point_dir / "analysis.json"
    payload = sine.load_json(point_path)
    original = sine.load_json(original_path)
    if payload.get("pass") is True and original.get("pass") is True:
        return {
            "mode": "original_live_analysis",
            "analysis": original,
            "analysis_path": str(original_path),
            "analysis_sha256": safety.sha256_file(original_path),
            "original_point_pass": True,
            "pass": True,
        }
    if case_id not in REANALYZABLE_I_SINE_CASES:
        raise UpperFrequencySummaryError(
            f"{case_id}: failed live analysis has no offline correction policy"
        )
    if (
        payload.get("case", {}).get("case_id") != case_id
        or original.get("case_id") != case_id
        or payload.get("scope", {}).get("pass") is not True
        or payload.get("lan", {}).get("pass") is not True
        or set(original.get("failures", [])) != EXPECTED_7P2_ANALYSIS_FAILURES
        or set(payload.get("failures", []))
        != {f"analysis: {item}" for item in EXPECTED_7P2_ANALYSIS_FAILURES}
    ):
        raise UpperFrequencySummaryError(
            f"{case_id}: live failure is not the frozen RTM FFT-grid analysis-only case"
        )

    directory = (I_SINE_REANALYSIS_ROOT / case_id).resolve()
    verification = calibration.verify_sha256sums(directory)
    analysis_path = directory / "analysis-wavebench-primary.json"
    corrected = sine.load_json(analysis_path)
    offline = corrected.get("offline_reanalysis")
    point_verification = sine._verify_point_sha256sums(point_dir)
    if not isinstance(offline, dict):
        raise UpperFrequencySummaryError(f"{case_id}: offline reanalysis binding is missing")
    if (
        offline.get("source_point") != str(point_path.resolve())
        or offline.get("source_point_sha256") != safety.sha256_file(point_path)
        or offline.get("instrument_io") is not False
        or offline.get("source_point_modified") is not False
        or offline.get("source_sha256sums", {}).get("manifest_sha256")
        != point_verification["manifest_sha256"]
    ):
        raise UpperFrequencySummaryError(f"{case_id}: offline reanalysis source binding failed")
    recomputed = _recompute_sine_analysis(point_dir, payload)
    corrected_core = dict(corrected)
    corrected_core.pop("offline_reanalysis", None)
    if calibration.canonical_sha256(corrected_core) != calibration.canonical_sha256(
        recomputed
    ):
        raise UpperFrequencySummaryError(f"{case_id}: offline raw reanalysis is not reproducible")
    if corrected.get("pass") is not True:
        raise UpperFrequencySummaryError(f"{case_id}: corrected analysis does not pass")
    for channel in ("ch1", "ch2"):
        selection = corrected.get("scope_primary", {}).get(channel, {}).get(
            "fft_input_selection", {}
        )
        if (
            selection.get("mode") != "longest_integer-cycle_archived_prefix"
            or selection.get("analyzed_samples") != 3125
            or selection.get("expected_cycles") != 9
            or selection.get("outlier_selection_used") is not False
            or selection.get("raw_samples_modified") is not False
        ):
            raise UpperFrequencySummaryError(
                f"{case_id}: corrected {channel} FFT prefix policy changed"
            )
    return {
        "mode": "validated_offline_analysis_correction",
        "analysis": corrected,
        "analysis_path": str(analysis_path.resolve()),
        "analysis_sha256": safety.sha256_file(analysis_path),
        "reanalysis_sha256sums": verification,
        "original_analysis_path": str(original_path.resolve()),
        "original_analysis_sha256": safety.sha256_file(original_path),
        "original_point_pass": False,
        "original_failure_class": "RTM time-range quantization caused a noncoherent full FFT grid",
        "raw_samples_modified": False,
        "point_repeated": False,
        "pass": True,
    }


def _i_scope_fft_evidence(
    point_dir: Path,
    payload: dict[str, Any],
    analysis: dict[str, Any],
    expected_frequency_hz: float,
) -> dict[str, Any]:
    fft = calibration._wavebench_fft_evidence(
        point_dir,
        payload,
        analysis,
        expected_frequency_hz,
        max_frequency_offset_bins=FFT_MAX_GRID_OFFSET_BINS,
        allow_coherent_prefix=True,
    )
    recorded_crosscheck = analysis.get("scope_fit_crosscheck", {}).get("comparison")
    if not isinstance(recorded_crosscheck, dict):
        raise UpperFrequencySummaryError("I scope cross-check evidence is missing")
    verified: dict[str, Any] = {}
    for channel in (1, 2):
        name = f"ch{channel}"
        channel_fft = fft["channels"][name]
        _times, values, sample_rate_hz = sine.load_scope_trace(
            Path(channel_fft["npy"])
        )
        fitted = sine.tone_metrics(values, sample_rate_hz, expected_frequency_hz)
        fitted_vpp = float(fitted["fundamental_vpp"])
        wavebench_vpp = float(channel_fft["fundamental_vpp_v"])
        relative_delta = abs(fitted_vpp - wavebench_vpp) / wavebench_vpp
        grid_offset_bins = float(channel_fft["frequency_offset_bins"])
        coherent = grid_offset_bins <= fir.FFT_COHERENT_GRID_OFFSET_BINS
        recorded = recorded_crosscheck.get(name)
        if not isinstance(recorded, dict):
            raise UpperFrequencySummaryError(f"I {name} cross-check is missing")
        numeric_matches = (
            math.isclose(
                float(recorded.get("wavebench_fundamental_vpp_v", math.nan)),
                wavebench_vpp,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            and math.isclose(
                float(recorded.get("least_squares_fundamental_vpp_v", math.nan)),
                fitted_vpp,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            and math.isclose(
                float(recorded.get("relative_delta", math.nan)),
                relative_delta,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            and math.isclose(
                float(recorded.get("fft_grid_offset_bins", math.nan)),
                grid_offset_bins,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            and math.isclose(
                float(recorded.get("limit", math.nan)),
                SCOPE_CROSSCHECK_LIMIT,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        )
        if (
            not numeric_matches
            or recorded.get("fft_grid_coherent") is not coherent
            or recorded.get("pass") is not True
            or relative_delta > SCOPE_CROSSCHECK_LIMIT
        ):
            raise UpperFrequencySummaryError(
                f"I {name} WaveBench/least-squares cross-check failed"
            )
        verified[name] = {
            "wavebench_fundamental_vpp_v": wavebench_vpp,
            "least_squares_fundamental_vpp_v": fitted_vpp,
            "relative_delta": relative_delta,
            "fft_grid_offset_bins": grid_offset_bins,
            "fft_grid_coherent": coherent,
            "limit": SCOPE_CROSSCHECK_LIMIT,
            "pass": True,
        }
    fft["known_frequency_crosscheck"] = {
        "role": "bounded 2% cross-check only; WaveBench Hann FFT remains primary",
        "channels": verified,
        "pass": True,
    }
    return fft


def _select_sine_point(points_root: Path, case_id: str) -> Path:
    eligible: list[Path] = []
    for candidate in sorted(points_root.glob(f"*_{case_id}")):
        if not candidate.is_dir() or not candidate.name.endswith(f"_{case_id}"):
            continue
        if not (candidate / "point.json").is_file() or not (
            candidate / "analysis.json"
        ).is_file():
            continue
        try:
            validated = validated_sine_analysis(candidate, case_id)
        except Exception:
            continue
        if validated.get("pass") is True:
            eligible.append(candidate.resolve())
    if len(eligible) != 1:
        raise UpperFrequencySummaryError(
            f"{case_id}: exactly one passing sine point is required, found {len(eligible)}"
        )
    return eligible[0]


def analyze_i_sine(
    record: dict[str, Any],
    point_dir: Path,
    source_data_root: Path,
    kadc: dict[str, Any],
    expected_identity: dict[str, int],
) -> dict[str, Any]:
    verification = sine._verify_point_sha256sums(point_dir)
    payload = sine.load_json(point_dir / "point.json")
    case_id = str(record["case_id"])
    validated_analysis = validated_sine_analysis(point_dir, case_id)
    analysis = validated_analysis["analysis"]
    case = payload.get("case", {})
    if (
        case.get("case_id") != case_id
        or case.get("stage") != "I"
        or analysis.get("case_id") != case_id
        or case.get("matrix_manifest_sha256")
        != safety.sha256_file(sine.MATRIX_ROOT / "manifest.json")
    ):
        raise UpperFrequencySummaryError(f"{case_id}: sine identity/binding failed")
    if (
        payload.get("dp800_writes") is not False
        or payload.get("scope_impedance_writes") is not False
        or payload.get("source_window", {}).get("off_status", {}).get("output") != "OFF"
        or payload.get("source_window", {}).get("off_status", {}).get("function") != "SIN"
        or payload.get("scope", {}).get("couplings_after") != {"1": "DCL", "2": "DCL"}
    ):
        raise UpperFrequencySummaryError(f"{case_id}: sine final safety state failed")
    observed = analysis.get("calibration", {})
    for key, expected in expected_identity.items():
        if int(observed.get(key, -1)) != expected:
            raise UpperFrequencySummaryError(f"{case_id}: calibration {key} mismatch")
    if observed.get("calibrated") is not True:
        raise UpperFrequencySummaryError(f"{case_id}: CALIBRATED flag is missing")

    frequency_hz = float(record["frequency_hz"])
    fft = _i_scope_fft_evidence(point_dir, payload, analysis, frequency_hz)
    source_archive = calibration._source_archive_evidence(
        point_dir, payload, source_data_root
    )
    capture_dir = Path(str(payload["lan"]["capture_dir"])).resolve()
    try:
        capture_dir.relative_to(point_dir)
    except ValueError as error:
        raise UpperFrequencySummaryError(f"{case_id}: capture path escapes point") from error
    residual = fir._adc_residual_distribution(capture_dir, frequency_hz)
    ch2_vpp_v = float(fft["channels"]["ch2"]["fundamental_vpp_v"])
    residual_vpp_code = float(
        residual["fundamental_vpp_code"]["empirical_p95_upper"]
    )
    attenuation_db = fir.attenuation_lower_bound_db(
        ch2_vpp_v,
        float(kadc["minimum_code_per_v"]),
        residual_vpp_code,
    )
    if attenuation_db < fir.ATTENUATION_LIMIT_DB:
        raise UpperFrequencySummaryError(f"{case_id}: attenuation is below 50 dB")
    return {
        "kind": "sine",
        "case_id": case_id,
        "point_directory": str(point_dir),
        "frequency_hz": frequency_hz,
        "folded_frequency_hz": residual["folded_frequency_hz"],
        "source_vpp_v": float(record["source_vpp_v"]),
        "ch1_fundamental_vpp_v": float(
            fft["channels"]["ch1"]["fundamental_vpp_v"]
        ),
        "ch2_fundamental_vpp_v": ch2_vpp_v,
        "adc_residual": residual,
        "attenuation_lower_bound_db": attenuation_db,
        "attenuation_pass": True,
        "frame_count": int(payload["lan"]["frame_count"]),
        "wave_packets": int(source_archive["wave_packets"]),
        "pcap": source_archive,
        "point_sha256sums_sha256": verification["manifest_sha256"],
        "point_json_sha256": safety.sha256_file(point_dir / "point.json"),
        "analysis_json_sha256": validated_analysis["analysis_sha256"],
        "analysis_evidence": {
            key: value
            for key, value in validated_analysis.items()
            if key != "analysis"
        },
        "scope_fft": fft,
        "screenshots_used_for_numeric_results": False,
        "raw_samples_modified": False,
    }


def analyze_i_arb(
    record: dict[str, Any],
    point_dir: Path,
    source_data_root: Path,
    expected_identity: dict[str, int],
) -> dict[str, Any]:
    verification = sine._verify_point_sha256sums(point_dir)
    payload = sine.load_json(point_dir / "point.json")
    recorded = sine.load_json(point_dir / "analysis.json")
    case_id = str(record["case_id"])
    case = payload.get("case", {})
    if (
        case.get("case_id") != case_id
        or case.get("stage") != "I"
        or case.get("matrix_manifest_sha256")
        != safety.sha256_file(arb.MATRIX_ROOT / "manifest.json")
    ):
        raise UpperFrequencySummaryError(f"{case_id}: ARB identity/binding failed")
    if (
        payload.get("dp800_writes") is not False
        or payload.get("scope_impedance_writes") is not False
        or payload.get("source_window", {}).get("off_status", {}).get("output") != "OFF"
        or payload.get("source_window", {}).get("off_status", {}).get("function") != "USER"
        or payload.get("scope", {}).get("couplings_after") != {"1": "DCL", "2": "DCL"}
        or payload.get("arb_configuration_mode")
        != "hash-bound-wavebench-repeat-user-to-user"
    ):
        raise UpperFrequencySummaryError(f"{case_id}: ARB final safety state failed")
    raw_archives = payload.get("wavebench_raw_archives")
    lan = payload.get("lan")
    if not isinstance(raw_archives, list) or len(raw_archives) != 1 or not isinstance(lan, dict):
        raise UpperFrequencySummaryError(f"{case_id}: ARB raw/LAN binding failed")
    scope_package = Path(str(raw_archives[0]["destination"])).resolve()
    capture_dir = Path(str(lan["capture_dir"])).resolve()
    lan_report = Path(str(lan["report"])).resolve()
    for path in (scope_package, capture_dir, lan_report):
        try:
            path.relative_to(point_dir)
        except ValueError as error:
            raise UpperFrequencySummaryError(f"{case_id}: raw input escapes point") from error
    recomputed = arb.analyze_point(
        record=case,
        scope_package=scope_package,
        capture_dir=capture_dir,
        lan_report_path=lan_report,
    )
    if calibration.canonical_sha256(recomputed) != calibration.canonical_sha256(recorded):
        raise UpperFrequencySummaryError(f"{case_id}: raw reanalysis is not reproducible")
    if (
        recomputed.get("pass") is not True
        or recomputed.get("screenshots_used_for_numeric_results") is not False
        or "wavebench.data.fft.analyze_fft"
        not in str(recomputed["scope_primary"].get("primary_method", ""))
    ):
        raise UpperFrequencySummaryError(f"{case_id}: ARB analysis policy/pass failed")
    observed = recomputed["calibration"]
    for key, expected in expected_identity.items():
        if int(observed.get(key, -1)) != expected:
            raise UpperFrequencySummaryError(f"{case_id}: calibration {key} mismatch")
    repeat_path = point_dir / "wavebench" / "run" / "repeat-arb-upload" / "run.json"
    repeat_payload = sine.load_json(repeat_path)
    audit = repeat_payload.get("result", {}).get("audit", {})
    if (
        audit.get("distribution") != repeat_arb.EXPECTED_DISTRIBUTION
        or audit.get("version") != repeat_arb.EXPECTED_VERSION
        or audit.get("driver_source_sha256") != repeat_arb.EXPECTED_DRIVER_SHA256
        or repeat_payload.get("pass") is not True
    ):
        raise UpperFrequencySummaryError(f"{case_id}: repeat-ARB audit failed")
    source_archive = calibration._source_archive_evidence(
        point_dir, payload, source_data_root
    )
    recovery = recomputed["adc_recovery"]
    aggregate = recovery["aggregate_metrics"]
    interference = recomputed.get("interference_rejection")
    maximum_line_error = multitone._maximum_line_error(recomputed)
    if (
        not isinstance(interference, dict)
        or interference.get("pass") is not True
        or maximum_line_error > arb.CALIBRATION_HARD_LIMIT_V
        or float(aggregate["vpp"]["absolute_error_v"]) > arb.CALIBRATION_HARD_LIMIT_V
        or float(aggregate["true_rms"]["absolute_error_v"])
        > arb.CALIBRATION_HARD_LIMIT_V
        or recovery["effective_band_residual_intermod"].get("pass") is not True
    ):
        raise UpperFrequencySummaryError(f"{case_id}: ARB hard gate failed")
    return {
        "kind": "arb",
        "case_id": case_id,
        "point_directory": str(point_dir),
        "u_b_source_case": case["u_b_source_case"],
        "u_j_frequency_hz": float(case["u_j_frequency_hz"]),
        "source_vpp_v": float(case["source_vpp_v"]),
        "maximum_line_absolute_error_v": maximum_line_error,
        "line_results": recovery["line_results"],
        "vpp": aggregate["vpp"],
        "true_rms": aggregate["true_rms"],
        "intermod_residual_peak_v_p95": float(
            recovery["effective_band_residual_intermod"]["largest_spur_input_peak_v"][
                "empirical_p95"
            ]
        ),
        "attenuation_lower_bound_db": float(
            interference["attenuation_lower_bound_db"]
        ),
        "attenuation_pass": True,
        "outlier_count": int(recovery["outliers"]["count"]),
        "outlier_rate": float(recovery["outliers"]["rate"]),
        "target_warnings": list(recovery["target_warnings"]),
        "target_pass": (
            maximum_line_error <= arb.CALIBRATION_TARGET_V
            and aggregate["vpp"]["target_pass"] is True
            and aggregate["true_rms"]["target_pass"] is True
        ),
        "frame_count": int(lan["frame_count"]),
        "wave_packets": int(source_archive["wave_packets"]),
        "pcap": source_archive,
        "point_sha256sums_sha256": verification["manifest_sha256"],
        "point_json_sha256": safety.sha256_file(point_dir / "point.json"),
        "analysis_json_sha256": safety.sha256_file(point_dir / "analysis.json"),
        "repeat_arb_run_json_sha256": safety.sha256_file(repeat_path),
        "screenshots_used_for_numeric_results": False,
        "raw_samples_modified": False,
    }


def build_summary(points_root: Path, source_data_root: Path) -> dict[str, Any]:
    manifest = sine.load_json(CALIBRATION_MANIFEST)
    if (
        manifest.get("status") != "validated"
        or manifest.get("validation", {}).get("holdout_pass") is not True
    ):
        raise UpperFrequencySummaryError("calibration manifest is not validated")
    expected_identity = {
        "calibration_id": int(manifest["calibration_id"]),
        "scale_uv_per_lsb": int(manifest["scale_uv_per_lsb"]),
        "offset_uv": int(manifest["offset_uv"]),
    }
    fit, fit_verification = calibration.load_frozen_fit(fir.FIT_DIR)
    kadc = fir.reference_kadc(fit)
    sine_cases, arb_cases = i_cases()
    sine_records = [
        analyze_i_sine(
            record,
            _select_sine_point(points_root, str(record["case_id"])),
            source_data_root,
            kadc,
            expected_identity,
        )
        for record in sine_cases
    ]
    arb_records = [
        analyze_i_arb(
            record,
            multitone._select_point(points_root, str(record["case_id"])),
            source_data_root,
            expected_identity,
        )
        for record in arb_cases
    ]
    all_records = [*sine_records, *arb_records]
    target_pass = all(record["target_pass"] for record in arb_records)
    minimum_attenuation = min(
        float(record["attenuation_lower_bound_db"]) for record in all_records
    )
    return {
        "format": SUMMARY_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "pass",
        "target_status": "pass" if target_pass else "warning",
        "scope_numeric_policy": (
            "WaveBench archived NPY + wavebench.data.fft.analyze_fft is primary; "
            "known-frequency sine or joint-component fits are bounded cross-checks"
        ),
        "screenshots_used_for_numeric_results": False,
        "raw_samples_modified": False,
        "calibration_manifest": str(CALIBRATION_MANIFEST.resolve()),
        "calibration_manifest_sha256": safety.sha256_file(CALIBRATION_MANIFEST),
        "fit_sha256": safety.sha256_file(fir.FIT_DIR / "fit.json"),
        "fit_sha256sums_sha256": fit_verification["sha256"],
        "expected_calibration_identity": expected_identity,
        "reference_kadc": kadc,
        "sine_point_count": len(sine_records),
        "arb_point_count": len(arb_records),
        "point_count": len(all_records),
        "frame_count": sum(int(record["frame_count"]) for record in all_records),
        "wave_packet_count": sum(
            int(record["wave_packets"]) for record in all_records
        ),
        "minimum_attenuation_lower_bound_db": minimum_attenuation,
        "attenuation_limit_db": fir.ATTENUATION_LIMIT_DB,
        "maximum_line_absolute_error_v": max(
            float(record["maximum_line_absolute_error_v"]) for record in arb_records
        ),
        "maximum_vpp_absolute_error_v": max(
            float(record["vpp"]["absolute_error_v"]) for record in arb_records
        ),
        "maximum_true_rms_absolute_error_v": max(
            float(record["true_rms"]["absolute_error_v"]) for record in arb_records
        ),
        "maximum_intermod_residual_peak_v_p95": max(
            float(record["intermod_residual_peak_v_p95"]) for record in arb_records
        ),
        "total_outlier_count": sum(
            int(record["outlier_count"]) for record in arb_records
        ),
        "sine_records": sine_records,
        "arb_records": arb_records,
        "failures": [],
        "pass": minimum_attenuation >= fir.ATTENUATION_LIMIT_DB,
        "target_pass": target_pass,
    }


CSV_FIELDS = (
    "kind",
    "case_id",
    "frequency_hz",
    "source_vpp_v",
    "folded_frequency_hz",
    "attenuation_lower_bound_db",
    "maximum_line_absolute_error_v",
    "vpp_absolute_error_v",
    "true_rms_absolute_error_v",
    "intermod_residual_peak_v_p95",
    "frame_count",
    "target_pass",
)


def _csv_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "kind": "sine",
            "case_id": record["case_id"],
            "frequency_hz": record["frequency_hz"],
            "source_vpp_v": record["source_vpp_v"],
            "folded_frequency_hz": record["folded_frequency_hz"],
            "attenuation_lower_bound_db": record["attenuation_lower_bound_db"],
            "maximum_line_absolute_error_v": "",
            "vpp_absolute_error_v": "",
            "true_rms_absolute_error_v": "",
            "intermod_residual_peak_v_p95": "",
            "frame_count": record["frame_count"],
            "target_pass": "",
        }
        for record in summary["sine_records"]
    ]
    rows.extend(
        {
            "kind": "arb",
            "case_id": record["case_id"],
            "frequency_hz": record["u_j_frequency_hz"],
            "source_vpp_v": record["source_vpp_v"],
            "folded_frequency_hz": sine.folded_frequency(
                float(record["u_j_frequency_hz"])
            ),
            "attenuation_lower_bound_db": record["attenuation_lower_bound_db"],
            "maximum_line_absolute_error_v": record[
                "maximum_line_absolute_error_v"
            ],
            "vpp_absolute_error_v": record["vpp"]["absolute_error_v"],
            "true_rms_absolute_error_v": record["true_rms"]["absolute_error_v"],
            "intermod_residual_peak_v_p95": record[
                "intermod_residual_peak_v_p95"
            ],
            "frame_count": record["frame_count"],
            "target_pass": record["target_pass"],
        }
        for record in summary["arb_records"]
    )
    return rows


def write_summary(output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if output_dir.exists():
        raise UpperFrequencySummaryError(f"output already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    summary_path = output_dir / "summary.json"
    safety.write_json_exclusive(summary_path, summary)
    csv_path = output_dir / "points.csv"
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_rows(summary))
    sums = safety._write_sha256sums(output_dir)
    return {
        "pass": summary["pass"],
        "target_pass": summary["target_pass"],
        "point_count": summary["point_count"],
        "frame_count": summary["frame_count"],
        "minimum_attenuation_lower_bound_db": summary[
            "minimum_attenuation_lower_bound_db"
        ],
        "maximum_line_absolute_error_v": summary["maximum_line_absolute_error_v"],
        "maximum_vpp_absolute_error_v": summary["maximum_vpp_absolute_error_v"],
        "maximum_true_rms_absolute_error_v": summary[
            "maximum_true_rms_absolute_error_v"
        ],
        "summary": str(summary_path.resolve()),
        "csv": str(csv_path.resolve()),
        "sha256sums": str(sums.resolve()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--points-root", type=Path, default=safety.EVIDENCE_ROOT / "points"
    )
    parser.add_argument(
        "--source-data-root", type=Path, default=safety.SOURCE_DATA_ROOT
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = write_summary(
            args.output_dir.resolve(),
            build_summary(
                args.points_root.resolve(), args.source_data_root.resolve()
            ),
        )
    except Exception as error:
        print(
            f"M11_UPPER_FREQUENCY_SUMMARY_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
