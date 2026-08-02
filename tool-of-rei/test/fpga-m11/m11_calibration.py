#!/usr/bin/env python3
"""Freeze and validate the M11-E end-to-end calibration without holdout leakage."""

# ruff: noqa: E402 -- adjacent M11 helpers establish the WaveBench environment.

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m11_sine_point as point
import m11_wavebench_safe as safety

from wavebench.data.fft import analyze_fft as wavebench_analyze_fft


FIT_FORMAT = "CycleScope M11-E frozen calibration fit v1"
CALIBRATION_FORMAT = "CycleScope M11 end-to-end calibration v1"
UNCERTAINTY_FORMAT = "CycleScope M11 calibration uncertainty v1"
HOLDOUT_FORMAT = "CycleScope M11-E holdout validation v1"
BUILD_MANIFEST_FORMAT = "CycleScope M11 validated calibration build manifest v1"
ALGORITHM_ID = "m11-e-linear-frequency-global-amplitude-v1"
REFERENCE_FREQUENCY_HZ = 100_000.0
HOLDOUT_LIMIT_V = 0.005
HOLDOUT_TARGET_V = 0.003
MATRIX_MANIFEST = point.MATRIX_ROOT / "manifest.json"
ZERO_POINT_DIRECTORY = (
    safety.EVIDENCE_ROOT
    / "points"
    / "20260731_190216_579170+0800_b-zero-noise-500k"
)

SWEEP_FREQUENCIES_HZ = (
    10_000,
    10_500,
    20_000,
    50_000,
    100_000,
    200_000,
    300_000,
    400_000,
    450_000,
    475_000,
    490_000,
    500_000,
)
TRAINING_SWEEP_CASE_IDS = tuple(
    f"e-train-{direction}-{frequency}Hz"
    for direction in ("up", "down")
    for frequency in (
        SWEEP_FREQUENCIES_HZ if direction == "up" else reversed(SWEEP_FREQUENCIES_HZ)
    )
)
MINLINE_CASE_IDS = tuple(
    f"e-minline-{frequency}Hz" for frequency in (10_000, 100_000, 500_000)
)
CROSS_CASE_IDS = tuple(
    f"e-cross-{frequency}Hz-{amplitude}mVpp"
    for frequency in (10_000, 200_000, 500_000)
    for amplitude in (50, 250, 450)
)
TRAINING_CASE_IDS = TRAINING_SWEEP_CASE_IDS + MINLINE_CASE_IDS + CROSS_CASE_IDS
HOLDOUT_FREQUENCIES_HZ = (15_000, 75_000, 150_000, 250_000, 350_000, 425_000, 485_000)
HOLDOUT_CASE_IDS = tuple(f"e-holdout-{frequency}Hz" for frequency in HOLDOUT_FREQUENCIES_HZ)


class CalibrationError(RuntimeError):
    """Calibration evidence is incomplete, stale, ambiguous, or unsafe."""


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calibration_id_from_identity(identity_sha256: str) -> int:
    if len(identity_sha256) != 64:
        raise CalibrationError("fit identity SHA-256 is malformed")
    value = int(identity_sha256[:4], 16)
    return value if value != 0 else 1


def verify_sha256sums(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = root / "SHA256SUMS"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CalibrationError(f"cannot read SHA256SUMS: {manifest}") from error
    records: list[dict[str, Any]] = []
    for line in lines:
        expected, separator, relative_text = line.partition("  ")
        if not separator or len(expected) != 64:
            raise CalibrationError(f"invalid SHA256SUMS line in {manifest}: {line!r}")
        path = (root / relative_text).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise CalibrationError(f"SHA256SUMS path escapes root: {relative_text}") from error
        if not path.is_file():
            raise CalibrationError(f"SHA256SUMS file is missing: {relative_text}")
        actual = safety.sha256_file(path)
        if actual != expected:
            raise CalibrationError(f"SHA256 mismatch: {path}")
        records.append(
            {"path": relative_text, "sha256": actual, "size": path.stat().st_size}
        )
    if not records:
        raise CalibrationError(f"empty SHA256SUMS: {manifest}")
    return {
        "path": str(manifest),
        "sha256": safety.sha256_file(manifest),
        "files_verified": len(records),
        "records": records,
    }


def _exact_case_directories(points_root: Path, case_id: str) -> list[Path]:
    # The exact suffix is intentional: fit must never enumerate or open holdout JSON.
    return sorted(
        path.resolve()
        for path in points_root.glob(f"*_{case_id}")
        if path.is_dir() and path.name.endswith(f"_{case_id}")
    )


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CalibrationError(f"cannot read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise CalibrationError(f"JSON object required: {path}")
    return value


def _wavebench_fft_evidence(
    point_dir: Path,
    payload: dict[str, Any],
    analysis: dict[str, Any],
    expected_frequency_hz: float,
    *,
    max_frequency_offset_bins: float | None = None,
    allow_coherent_prefix: bool = False,
) -> dict[str, Any]:
    if max_frequency_offset_bins is not None and (
        not math.isfinite(max_frequency_offset_bins)
        or not 0.0 < max_frequency_offset_bins <= 0.5
    ):
        raise CalibrationError("FFT bin-offset gate must be in (0, 0.5]")
    archives = payload.get("wavebench_raw_archives")
    if not isinstance(archives, list) or len(archives) != 1:
        raise CalibrationError("exactly one WaveBench raw archive is required")
    package = Path(str(archives[0].get("destination", ""))).resolve()
    try:
        package.relative_to(point_dir)
    except ValueError as error:
        raise CalibrationError("WaveBench raw archive escapes point evidence") from error
    primary = analysis.get("scope_primary")
    if not isinstance(primary, dict) or "wavebench.data.fft.analyze_fft" not in str(
        primary.get("primary_method", "")
    ):
        raise CalibrationError("WaveBench analyze_fft is not the scope primary method")

    result: dict[str, Any] = {
        "method": "wavebench.data.fft.analyze_fft",
        "screenshots_used_for_numeric_results": False,
        "package": str(package),
        "channels": {},
    }
    for channel in (1, 2):
        path = package / f"ch{channel}.npy"
        if not path.is_file():
            raise CalibrationError(f"WaveBench CH{channel} NPY is missing")
        waveform = np.load(path, allow_pickle=False)
        if allow_coherent_prefix:
            fft_record = point.wavebench_fft_for_expected_frequency(
                waveform,
                expected_frequency_hz,
                allow_coherent_prefix=True,
            )
            fft = fft_record["fft"]
        else:
            fft = wavebench_analyze_fft(waveform, max_harmonic_order=5)
            fft_record = {
                "selection": {
                    "mode": "full_archived_trace",
                    "archived_samples": int(waveform.shape[0]),
                    "analyzed_samples": int(waveform.shape[0]),
                    "dropped_tail_samples": 0,
                    "raw_samples_modified": False,
                }
            }
        recorded = primary.get(f"ch{channel}")
        if not isinstance(recorded, dict):
            raise CalibrationError(f"recorded WaveBench CH{channel} result is missing")
        if fft.get("warnings"):
            raise CalibrationError(f"WaveBench CH{channel} FFT warnings: {fft['warnings']}")
        frequency_error_hz = abs(
            float(fft["peak_frequency_hz"]) - expected_frequency_hz
        )
        resolution_hz = float(fft["resolution_hz"])
        frequency_offset_bins = (
            frequency_error_hz / resolution_hz if resolution_hz > 0.0 else math.inf
        )
        if max_frequency_offset_bins is None:
            if frequency_error_hz > 1_000.0:
                raise CalibrationError(
                    f"WaveBench CH{channel} frequency misses the 1 kHz gate"
                )
        elif frequency_offset_bins > max_frequency_offset_bins + 1e-9:
            raise CalibrationError(
                f"WaveBench CH{channel} frequency misses the "
                f"{max_frequency_offset_bins:g}-bin gate"
            )
        recorded_vpp = float(recorded["fundamental_vpp_v"])
        direct_vpp = 2.0 * float(fft["peak_amplitude_v"])
        if not math.isclose(direct_vpp, recorded_vpp, rel_tol=1e-12, abs_tol=1e-15):
            raise CalibrationError(f"WaveBench CH{channel} archived FFT is not reproducible")
        result["channels"][f"ch{channel}"] = {
            "npy": str(path),
            "npy_sha256": safety.sha256_file(path),
            "samples": int(fft["samples"]),
            "resolution_hz": resolution_hz,
            "peak_frequency_hz": float(fft["peak_frequency_hz"]),
            "frequency_error_hz": frequency_error_hz,
            "frequency_offset_bins": frequency_offset_bins,
            "fundamental_vpp_v": direct_vpp,
            "thd_ratio": None if fft["thd_ratio"] is None else float(fft["thd_ratio"]),
            "fft_input_selection": fft_record["selection"],
        }
    return result


def _source_archive_evidence(
    point_dir: Path,
    payload: dict[str, Any],
    source_data_root: Path,
) -> dict[str, Any]:
    archive = payload.get("lan", {}).get("packet_archive", {})
    expected_dir = (source_data_root / point_dir.name).resolve()
    actual_dir = Path(str(archive.get("directory", ""))).resolve()
    if actual_dir != expected_dir:
        raise CalibrationError("source_data archive is not bound to the point directory")
    verification = verify_sha256sums(actual_dir)
    manifest = _load_object(actual_dir / "manifest.json")
    if verification["sha256"] != safety.sha256_file(actual_dir / "SHA256SUMS"):
        raise CalibrationError("source_data SHA256SUMS changed while reading")
    if archive.get("manifest_sha256") != safety.sha256_file(actual_dir / "manifest.json"):
        raise CalibrationError("point source_data manifest SHA-256 mismatch")
    counts = manifest.get("packet_counts", {})
    if (
        manifest.get("pcap_analysis_pass") is not True
        or int(counts.get("target_udp_checksum_bad", -1)) != 0
        or int(counts.get("target_udp_checksum_zero", -1)) != 0
        or int(counts.get("source_udp_fragments", -1)) != 0
        or int(counts.get("target_wave_packets", 0)) <= 0
    ):
        raise CalibrationError("source_data pcap gate failed")
    return {
        "directory": str(actual_dir),
        "manifest_sha256": safety.sha256_file(actual_dir / "manifest.json"),
        "sha256sums_sha256": verification["sha256"],
        "files_verified": verification["files_verified"],
        "wave_packets": int(counts["target_wave_packets"]),
        "pcap_pass": True,
    }


def load_case_record(
    points_root: Path,
    source_data_root: Path,
    case_id: str,
    *,
    expected_role: str,
) -> dict[str, Any]:
    if expected_role not in {"training_candidate", "holdout"}:
        raise CalibrationError("unsupported calibration evidence role")
    candidates = _exact_case_directories(points_root, case_id)
    eligible: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    excluded: list[str] = []
    for candidate in candidates:
        payload = _load_object(candidate / "point.json")
        analysis = _load_object(candidate / "analysis.json")
        if payload.get("pass") is True and analysis.get("pass") is True:
            eligible.append((candidate, payload, analysis))
        else:
            excluded.append(str(candidate))
    if len(eligible) != 1:
        raise CalibrationError(
            f"{case_id}: exactly one passing point is required, found {len(eligible)}"
        )
    point_dir, payload, analysis = eligible[0]
    point_verification = verify_sha256sums(point_dir)
    case = payload.get("case", {})
    if (
        case.get("case_id") != case_id
        or analysis.get("case_id") != case_id
        or case.get("stage") != "E"
        or analysis.get("formal_calibration_eligible") is not True
        or analysis.get("calibration_role") != expected_role
    ):
        raise CalibrationError(f"{case_id}: case identity or calibration role mismatch")
    matrix_sha256 = safety.sha256_file(MATRIX_MANIFEST)
    if case.get("matrix_manifest_sha256") != matrix_sha256:
        raise CalibrationError(f"{case_id}: stale matrix binding")
    if (
        payload.get("dp800_writes") is not False
        or payload.get("scope_impedance_writes") is not False
        or payload.get("source_window", {}).get("off_status", {}).get("output") != "OFF"
        or payload.get("scope", {}).get("couplings_after") != {"1": "DCL", "2": "DCL"}
    ):
        raise CalibrationError(f"{case_id}: final hardware safety state failed")
    lan = payload.get("lan", {})
    calibration = analysis.get("calibration", {})
    if (
        lan.get("pass") is not True
        or lan.get("packet_archive", {}).get("pass") is not True
        or int(calibration.get("calibration_id", -1)) != 0
        or calibration.get("calibrated") is not False
    ):
        raise CalibrationError(f"{case_id}: LAN or uncalibrated-input gate failed")

    frequency_hz = float(case["frequency_hz"])
    source_vpp_v = float(case["source_vpp_v"])
    fft = _wavebench_fft_evidence(point_dir, payload, analysis, frequency_hz)
    source_archive = _source_archive_evidence(point_dir, payload, source_data_root)
    adc_metrics = analysis.get("adc", {}).get("metrics", {})
    ratios = analysis.get("ratios", {})
    return {
        "case_id": case_id,
        "role": expected_role,
        "point_directory": str(point_dir),
        "point_timestamp": payload.get("timestamp"),
        "excluded_failed_candidates": excluded,
        "frequency_hz": frequency_hz,
        "source_vpp_v": source_vpp_v,
        "adc_fundamental_vpp_code": float(adc_metrics["fundamental_vpp"]["median"]),
        "adc_mean_code": float(adc_metrics["mean"]["median"]),
        "ke2e_code_per_v": float(ratios["ke2e_code_per_vset_v"]),
        "analog_ch2_minus_ch1_phase_deg": float(ratios["ch2_minus_ch1_phase_deg"]),
        "gamp_v_per_v": float(ratios["gamp_v_per_v"]),
        "point_json_sha256": safety.sha256_file(point_dir / "point.json"),
        "analysis_json_sha256": safety.sha256_file(point_dir / "analysis.json"),
        "point_sha256sums_sha256": point_verification["sha256"],
        "point_files_verified": point_verification["files_verified"],
        "source_data": source_archive,
        "wavebench": fft,
        "scope_fit_crosscheck": analysis.get("scope_fit_crosscheck", {}),
        "lan_frame_count": int(lan["frame_count"]),
    }


def _linear_interpolate(rows: list[dict[str, Any]], key: str, frequency_hz: float) -> float:
    frequencies = np.asarray([float(row["frequency_hz"]) for row in rows], dtype=float)
    values = np.asarray([float(row[key]) for row in rows], dtype=float)
    if frequency_hz < frequencies[0] or frequency_hz > frequencies[-1]:
        raise CalibrationError("frequency is outside the frozen 10..500 kHz response")
    return float(np.interp(frequency_hz, frequencies, values))


def _circular_mean_deg(values: list[float]) -> float:
    phasors = np.exp(1j * np.radians(np.asarray(values, dtype=float)))
    return float(np.degrees(np.angle(np.mean(phasors))))


def build_fit_model(training: list[dict[str, Any]], zero: dict[str, Any]) -> dict[str, Any]:
    if {record["case_id"] for record in training} != set(TRAINING_CASE_IDS):
        raise CalibrationError("fit inputs are not the exact 36-case training set")
    if any(record["role"] != "training_candidate" for record in training):
        raise CalibrationError("holdout evidence reached the fit model")

    by_id = {record["case_id"]: record for record in training}
    response_rows: list[dict[str, Any]] = []
    for frequency_hz in SWEEP_FREQUENCIES_HZ:
        up = by_id[f"e-train-up-{frequency_hz}Hz"]
        down = by_id[f"e-train-down-{frequency_hz}Hz"]
        up_gain = float(up["ke2e_code_per_v"])
        down_gain = float(down["ke2e_code_per_v"])
        mean_gain = statistics.mean((up_gain, down_gain))
        response_rows.append(
            {
                "frequency_hz": float(frequency_hz),
                "ke2e_code_per_v": mean_gain,
                "input_uv_per_code": 1_000_000.0 / mean_gain,
                "up_code_per_v": up_gain,
                "down_code_per_v": down_gain,
                "down_minus_up_percent": (down_gain / up_gain - 1.0) * 100.0,
                "analog_ch2_minus_ch1_phase_deg": _circular_mean_deg(
                    [
                        float(up["analog_ch2_minus_ch1_phase_deg"]),
                        float(down["analog_ch2_minus_ch1_phase_deg"]),
                    ]
                ),
                "phase_semantics": (
                    "same-acquisition RTM CH2 minus CH1 analog-front-end phase only; "
                    "not DG-to-FPGA absolute phase"
                ),
            }
        )

    amplitude_sources = [
        record
        for record in training
        if record["case_id"] in set(MINLINE_CASE_IDS + CROSS_CASE_IDS)
    ]
    amplitude_rows: list[dict[str, Any]] = []
    for amplitude_vpp in (0.01, 0.05, 0.1, 0.25, 0.45):
        if amplitude_vpp == 0.1:
            factors = [
                float(record["ke2e_code_per_v"])
                / _linear_interpolate(response_rows, "ke2e_code_per_v", float(record["frequency_hz"]))
                for record in training
                if record["case_id"] in set(TRAINING_SWEEP_CASE_IDS)
            ]
            factor = 1.0
        else:
            records = [
                record
                for record in amplitude_sources
                if math.isclose(float(record["source_vpp_v"]), amplitude_vpp, abs_tol=1e-12)
            ]
            if len(records) != 3:
                raise CalibrationError(f"expected three amplitude anchors at {amplitude_vpp} Vpp")
            factors = [
                float(record["ke2e_code_per_v"])
                / _linear_interpolate(response_rows, "ke2e_code_per_v", float(record["frequency_hz"]))
                for record in records
            ]
            factor = float(statistics.median(factors))
        amplitude_rows.append(
            {
                "source_vpp_v": amplitude_vpp,
                "global_gain_factor": factor,
                "anchor_count": len(factors),
                "minimum_factor": float(min(factors)),
                "median_factor": float(statistics.median(factors)),
                "maximum_factor": float(max(factors)),
            }
        )

    reference_gain = _linear_interpolate(
        response_rows, "ke2e_code_per_v", REFERENCE_FREQUENCY_HZ
    )
    scale_exact = 1_000_000.0 / reference_gain
    scale_integer = int(round(scale_exact))
    zero_mean_code = float(zero["mean_code_median"])
    offset_uv = int(round(-zero_mean_code * scale_integer))
    return {
        "algorithm_id": ALGORITHM_ID,
        "valid_frequency_hz": {"minimum": 10_000.0, "maximum": 500_000.0},
        "valid_source_vpp_v": {"minimum": 0.01, "maximum": 0.45},
        "frequency_interpolation": "piecewise linear; no extrapolation",
        "amplitude_model": (
            "global piecewise-linear gain factor from 10/50/250/450 mV training; "
            "100 mV factor fixed to one"
        ),
        "response_rows": response_rows,
        "amplitude_rows": amplitude_rows,
        "scalar_metadata": {
            "reference_frequency_hz": REFERENCE_FREQUENCY_HZ,
            "scale_uv_per_lsb_exact": scale_exact,
            "scale_uv_per_lsb": scale_integer,
            "offset_code": zero_mean_code,
            "offset_uv": offset_uv,
            "limitation": (
                "CSLP metadata is scalar. Frequency-dependent correction remains in "
                "response.csv and is not applied sample-by-sample by this firmware."
            ),
        },
        "phase": {
            "available": "RTM CH2 minus CH1 analog-front-end phase",
            "unavailable": (
                "DG-to-FPGA absolute phase; DG4202 and FPGA do not share a clock or trigger"
            ),
        },
    }


def _zero_record(zero_dir: Path) -> dict[str, Any]:
    verification = verify_sha256sums(zero_dir)
    payload = _load_object(zero_dir / "point.json")
    analysis = _load_object(zero_dir / "analysis.json")
    metrics = analysis.get("adc", {}).get("metrics", {})
    if (
        payload.get("pass") is not True
        or payload.get("profile_name") != "noise-500k"
        or payload.get("source_writes") is not False
        or payload.get("power_write") is not False
        or payload.get("scope", {}).get("couplings_after") != {"1": "DCL", "2": "DCL"}
        or analysis.get("raw_samples_modified") is not False
        or analysis.get("adc", {}).get("raw_samples_modified") is not False
    ):
        raise CalibrationError("zero-point evidence did not pass without sample modification")
    return {
        "point_directory": str(zero_dir.resolve()),
        "point_json_sha256": safety.sha256_file(zero_dir / "point.json"),
        "analysis_json_sha256": safety.sha256_file(zero_dir / "analysis.json"),
        "sha256sums_sha256": verification["sha256"],
        "files_verified": verification["files_verified"],
        "frame_count": int(analysis["adc"]["frame_count"]),
        "mean_code_median": float(metrics["mean_code"]["median"]),
        "mean_code_minimum": float(metrics["mean_code"]["minimum"]),
        "mean_code_maximum": float(metrics["mean_code"]["maximum"]),
        "rms_ac_code_median": float(metrics["rms_ac_code"]["median"]),
        "outlier_rate": float(analysis["adc"]["outlier_rate"]),
        "raw_samples_modified": False,
    }


def create_fit(
    points_root: Path,
    source_data_root: Path,
    zero_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise CalibrationError(f"fit output already exists: {output_dir}")
    matrix_sha256 = safety.sha256_file(MATRIX_MANIFEST)
    training = [
        load_case_record(
            points_root,
            source_data_root,
            case_id,
            expected_role="training_candidate",
        )
        for case_id in TRAINING_CASE_IDS
    ]
    zero = _zero_record(zero_dir)
    model = build_fit_model(training, zero)
    identity_basis = {
        "algorithm_id": ALGORITHM_ID,
        "matrix_manifest_sha256": matrix_sha256,
        "training": [
            {
                "case_id": record["case_id"],
                "point_json_sha256": record["point_json_sha256"],
                "analysis_json_sha256": record["analysis_json_sha256"],
                "point_sha256sums_sha256": record["point_sha256sums_sha256"],
                "source_data_sha256sums_sha256": record["source_data"]["sha256sums_sha256"],
            }
            for record in training
        ],
        "zero": {
            "point_json_sha256": zero["point_json_sha256"],
            "analysis_json_sha256": zero["analysis_json_sha256"],
            "sha256sums_sha256": zero["sha256sums_sha256"],
        },
    }
    identity_sha256 = canonical_sha256(identity_basis)
    calibration_id = calibration_id_from_identity(identity_sha256)
    payload = {
        "format": FIT_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "frozen",
        "calibration_id": calibration_id,
        "fit_identity_sha256": identity_sha256,
        "fit_identity_basis": identity_basis,
        "matrix_manifest": str(MATRIX_MANIFEST.resolve()),
        "matrix_manifest_sha256": matrix_sha256,
        "training_case_count": len(training),
        "training_case_ids": list(TRAINING_CASE_IDS),
        "holdout_isolation": {
            "holdout_case_ids": list(HOLDOUT_CASE_IDS),
            "fit_path_opens_only_exact_training_case_suffixes": True,
            "holdout_json_read_by_fit": False,
            "holdout_used_for_parameter_estimation": False,
        },
        "scope_numeric_policy": {
            "primary": "WaveBench archived NPY + wavebench.data.fft.analyze_fft",
            "least_squares": "known-frequency five-harmonic cross-check only",
            "screenshots": "qualitative clipping/ringing evidence only; no numeric reads",
        },
        "phase_limit": (
            "CH2-CH1 is analog-front-end phase only. No end-to-end absolute phase is claimed "
            "because DG4202 and FPGA have no common clock/trigger."
        ),
        "zero": zero,
        "training_records": training,
        "model": model,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    fit_path = output_dir / "fit.json"
    safety.write_json_exclusive(fit_path, payload)
    _write_response_csv(output_dir / "response-fit.csv", model["response_rows"])
    _write_amplitude_csv(output_dir / "amplitude-fit.csv", model["amplitude_rows"])
    sums = safety._write_sha256sums(output_dir)
    return {
        "pass": True,
        "fit": str(fit_path),
        "fit_sha256": safety.sha256_file(fit_path),
        "sha256sums": str(sums),
        "calibration_id": calibration_id,
        "training_case_count": len(training),
        "holdout_case_count_read": 0,
    }


def _write_response_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = (
        "frequency_hz",
        "ke2e_code_per_v",
        "input_uv_per_code",
        "up_code_per_v",
        "down_code_per_v",
        "down_minus_up_percent",
        "analog_ch2_minus_ch1_phase_deg",
        "phase_semantics",
    )
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in rows)


def _write_amplitude_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = (
        "source_vpp_v",
        "global_gain_factor",
        "anchor_count",
        "minimum_factor",
        "median_factor",
        "maximum_factor",
    )
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in rows)


def load_frozen_fit(fit_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    verification = verify_sha256sums(fit_dir)
    payload = _load_object(fit_dir / "fit.json")
    if payload.get("format") != FIT_FORMAT or payload.get("status") != "frozen":
        raise CalibrationError("fit artifact is not frozen M11-E v1")
    identity = payload.get("fit_identity_basis")
    identity_sha256 = canonical_sha256(identity)
    if identity_sha256 != payload.get("fit_identity_sha256"):
        raise CalibrationError("fit identity binding mismatch")
    if calibration_id_from_identity(identity_sha256) != payload.get("calibration_id"):
        raise CalibrationError("fit calibration_id is not deterministic")
    if payload.get("matrix_manifest_sha256") != safety.sha256_file(MATRIX_MANIFEST):
        raise CalibrationError("fit matrix manifest is stale")
    return payload, verification


def predict_source_vpp(model: dict[str, Any], frequency_hz: float, adc_vpp_code: float) -> float:
    if not math.isfinite(adc_vpp_code) or adc_vpp_code <= 0:
        raise CalibrationError("ADC fundamental amplitude must be finite and positive")
    response = _linear_interpolate(model["response_rows"], "ke2e_code_per_v", frequency_hz)
    amplitude_rows = model["amplitude_rows"]
    amplitudes = np.asarray([float(row["source_vpp_v"]) for row in amplitude_rows])
    factors = np.asarray([float(row["global_gain_factor"]) for row in amplitude_rows])
    estimate = adc_vpp_code / response
    for _ in range(12):
        if estimate < amplitudes[0] * 0.9 or estimate > amplitudes[-1] * 1.01:
            raise CalibrationError("predicted amplitude is outside the calibrated range")
        factor = float(np.interp(estimate, amplitudes, factors))
        updated = adc_vpp_code / (response * factor)
        if abs(updated - estimate) < 1e-12:
            return updated
        estimate = updated
    return estimate


def _max_scope_crosscheck_delta(training: list[dict[str, Any]]) -> float:
    values: list[float] = []
    for record in training:
        comparison = record.get("scope_fit_crosscheck", {}).get("comparison", {})
        for channel in ("ch1", "ch2"):
            value = comparison.get(channel, {}).get("relative_delta")
            if value is not None:
                values.append(abs(float(value)))
    if not values:
        raise CalibrationError("scope FFT/least-squares cross-check evidence is missing")
    return max(values)


def _iso_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CalibrationError("evidence timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CalibrationError(f"invalid evidence timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise CalibrationError("evidence timestamp must include a timezone")
    return parsed


def validate_holdouts(
    fit_dir: Path,
    points_root: Path,
    source_data_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise CalibrationError(f"validation output already exists: {output_dir}")
    fit, fit_verification = load_frozen_fit(fit_dir)
    fit_created_at = _iso_timestamp(fit["created_at"])
    holdouts = [
        load_case_record(
            points_root,
            source_data_root,
            case_id,
            expected_role="holdout",
        )
        for case_id in HOLDOUT_CASE_IDS
    ]
    records: list[dict[str, Any]] = []
    for record in holdouts:
        point_timestamp = _iso_timestamp(record["point_timestamp"])
        if point_timestamp <= fit_created_at:
            raise CalibrationError(
                f"{record['case_id']}: holdout was captured before the model was frozen"
            )
        prediction = predict_source_vpp(
            fit["model"],
            float(record["frequency_hz"]),
            float(record["adc_fundamental_vpp_code"]),
        )
        expected = float(record["source_vpp_v"])
        error = prediction - expected
        records.append(
            {
                **record,
                "predicted_source_vpp_v": prediction,
                "expected_dg_50ohm_source_vpp_v": expected,
                "signed_error_v": error,
                "absolute_error_v": abs(error),
                "target_3mv_pass": abs(error) <= HOLDOUT_TARGET_V,
                "hard_limit_5mv_pass": abs(error) <= HOLDOUT_LIMIT_V,
            }
        )
    errors = [float(record["signed_error_v"]) for record in records]
    max_error = max(abs(value) for value in errors)
    hard_pass = all(record["hard_limit_5mv_pass"] for record in records)
    target_pass = all(record["target_3mv_pass"] for record in records)

    training = fit["training_records"]
    repeatability_percent = [
        abs(float(row["down_minus_up_percent"]))
        for row in fit["model"]["response_rows"]
    ]
    amplitude_spreads = [
        float(row["maximum_factor"]) - float(row["minimum_factor"])
        for row in fit["model"]["amplitude_rows"]
    ]
    fit_path = fit_dir / "fit.json"
    fit_sha256 = safety.sha256_file(fit_path)
    calibration_id = int(fit["calibration_id"])
    scalar = fit["model"]["scalar_metadata"]
    calibration_payload = {
        "format": CALIBRATION_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "validated" if hard_pass else "holdout_failed",
        "calibration_id": calibration_id,
        "matrix_manifest_sha256": fit["matrix_manifest_sha256"],
        "fit": {
            "path": str(fit_path.resolve()),
            "sha256": fit_sha256,
            "sha256sums_sha256": fit_verification["sha256"],
            "identity_sha256": fit["fit_identity_sha256"],
        },
        "scale_uv_per_lsb": int(scalar["scale_uv_per_lsb"]),
        "offset_uv": int(scalar["offset_uv"]),
        "offset_code": float(scalar["offset_code"]),
        "scalar_reference_frequency_hz": float(scalar["reference_frequency_hz"]),
        "sample_rate_hz": int(point.OUTPUT_SAMPLE_RATE_HZ),
        "frame_samples": point.FRAME_SAMPLES,
        "filter_profile": 1,
        "frequency_response_artifact": "response.csv",
        "scope_numeric_policy": fit["scope_numeric_policy"],
        "phase_limit": fit["phase_limit"],
        "firmware_limit": scalar["limitation"],
        "raw_samples_modified": False,
    }
    holdout_payload = {
        "format": HOLDOUT_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "pass" if hard_pass else "fail",
        "fit_frozen_before_holdout": True,
        "fit_sha256": fit_sha256,
        "fit_identity_sha256": fit["fit_identity_sha256"],
        "holdout_refit_performed": False,
        "holdout_point_count": len(records),
        "expected_case_ids": list(HOLDOUT_CASE_IDS),
        "maximum_absolute_error_v": max_error,
        "rms_error_v": math.sqrt(statistics.mean(value * value for value in errors)),
        "mean_signed_error_v": statistics.mean(errors),
        "target_3mv_pass": target_pass,
        "hard_limit_5mv_pass": hard_pass,
        "pass_reference": "DG4202 CH1 50-ohm source setting; scope does not redefine pass",
        "scope_numeric_policy": fit["scope_numeric_policy"],
        "records": records,
    }
    uncertainty_payload = {
        "format": UNCERTAINTY_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "calibration_id": calibration_id,
        "model": {
            "maximum_up_down_repeatability_percent": max(repeatability_percent),
            "maximum_amplitude_factor_anchor_spread": max(amplitude_spreads),
            "maximum_wavebench_vs_known_frequency_fit_relative_delta": (
                _max_scope_crosscheck_delta(training)
            ),
            "frequency_interpolation": fit["model"]["frequency_interpolation"],
            "amplitude_model": fit["model"]["amplitude_model"],
        },
        "zero": {
            "mean_code_minimum": fit["zero"]["mean_code_minimum"],
            "mean_code_median": fit["zero"]["mean_code_median"],
            "mean_code_maximum": fit["zero"]["mean_code_maximum"],
            "rms_ac_code_median": fit["zero"]["rms_ac_code_median"],
            "equivalent_input_rms_v_at_scalar_reference": (
                float(fit["zero"]["rms_ac_code_median"])
                * int(scalar["scale_uv_per_lsb"])
                / 1_000_000.0
            ),
            "outlier_rate": fit["zero"]["outlier_rate"],
        },
        "holdout": {
            "maximum_absolute_error_v": max_error,
            "rms_error_v": holdout_payload["rms_error_v"],
            "hard_limit_v": HOLDOUT_LIMIT_V,
            "target_v": HOLDOUT_TARGET_V,
        },
        "excluded_characterization": [
            "probe/channel swap correction by user direction",
            "internal supply rail and temperature characterization by user direction",
            "feedback pickoff characterization by user direction",
        ],
        "interpretation": (
            "This is an evidence-bound validation envelope, not a metrology-lab expanded "
            "uncertainty claim. DG 50-ohm setting is the formal amplitude reference."
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=False)
    calibration_path = output_dir / "calibration.json"
    response_path = output_dir / "response.csv"
    uncertainty_path = output_dir / "uncertainty.json"
    holdout_path = output_dir / "holdout-report.json"
    safety.write_json_exclusive(calibration_path, calibration_payload)
    _write_response_csv(response_path, fit["model"]["response_rows"])
    safety.write_json_exclusive(uncertainty_path, uncertainty_payload)
    safety.write_json_exclusive(holdout_path, holdout_payload)
    artifacts = {
        "calibration_json": {
            "path": calibration_path.name,
            "sha256": safety.sha256_file(calibration_path),
        },
        "response_csv": {
            "path": response_path.name,
            "sha256": safety.sha256_file(response_path),
        },
        "uncertainty_json": {
            "path": uncertainty_path.name,
            "sha256": safety.sha256_file(uncertainty_path),
        },
        "holdout_report": {
            "path": holdout_path.name,
            "sha256": safety.sha256_file(holdout_path),
        },
    }
    build_manifest = {
        "format": BUILD_MANIFEST_FORMAT,
        "status": "validated" if hard_pass else "rejected",
        "calibration_id": calibration_id,
        "scale_uv_per_lsb": int(scalar["scale_uv_per_lsb"]),
        "offset_uv": int(scalar["offset_uv"]),
        "filter_profile": 1,
        "sample_rate_hz": int(point.OUTPUT_SAMPLE_RATE_HZ),
        "frame_samples": point.FRAME_SAMPLES,
        "matrix_manifest_sha256": fit["matrix_manifest_sha256"],
        "fit_sha256": fit_sha256,
        "validation": {
            "holdout_pass": hard_pass,
            "holdout_point_count": len(records),
            "max_absolute_error_v": max_error,
        },
        "artifacts": artifacts,
    }
    manifest_path = output_dir / "calibration-build-manifest.json"
    safety.write_json_exclusive(manifest_path, build_manifest)
    sums = safety._write_sha256sums(output_dir)
    return {
        "pass": hard_pass,
        "target_3mv_pass": target_pass,
        "calibration_id": calibration_id,
        "scale_uv_per_lsb": int(scalar["scale_uv_per_lsb"]),
        "offset_uv": int(scalar["offset_uv"]),
        "holdout_point_count": len(records),
        "maximum_absolute_error_v": max_error,
        "manifest": str(manifest_path),
        "manifest_sha256": safety.sha256_file(manifest_path),
        "sha256sums": str(sums),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit")
    fit.add_argument("--points-root", type=Path, default=safety.EVIDENCE_ROOT / "points")
    fit.add_argument("--source-data-root", type=Path, default=safety.SOURCE_DATA_ROOT)
    fit.add_argument("--zero-point", type=Path, default=ZERO_POINT_DIRECTORY)
    fit.add_argument("--output-dir", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--fit-dir", type=Path, required=True)
    validate.add_argument("--points-root", type=Path, default=safety.EVIDENCE_ROOT / "points")
    validate.add_argument("--source-data-root", type=Path, default=safety.SOURCE_DATA_ROOT)
    validate.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "fit":
            result = create_fit(
                args.points_root.resolve(),
                args.source_data_root.resolve(),
                args.zero_point.resolve(),
                args.output_dir.resolve(),
            )
        else:
            result = validate_holdouts(
                args.fit_dir.resolve(),
                args.points_root.resolve(),
                args.source_data_root.resolve(),
                args.output_dir.resolve(),
            )
    except Exception as error:
        print(f"M11_CALIBRATION_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
