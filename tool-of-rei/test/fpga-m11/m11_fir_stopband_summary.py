#!/usr/bin/env python3
"""Build an immutable M11-F CH2-to-FPGA FIR stopband lower-bound summary."""

# ruff: noqa: E402 -- adjacent M11 modules are deliberate evidence inputs.

from __future__ import annotations

import argparse
import csv
from datetime import datetime
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

import m11_calibration as calibration
import m11_sine_point as point
import m11_wavebench_safe as safety


SUMMARY_FORMAT = "CycleScope M11-F FIR stopband summary v1"
ATTENUATION_LIMIT_DB = 50.0
FFT_COHERENT_GRID_OFFSET_BINS = 0.05
FFT_MAX_GRID_OFFSET_BINS = 0.5
FFT_COHERENT_CROSSCHECK_LIMIT = 0.02
FFT_NONCOHERENT_CROSSCHECK_LIMIT = 0.10
CALIBRATION_DIR = safety.EVIDENCE_ROOT / "offline" / "calibration-v1"
CALIBRATION_MANIFEST = CALIBRATION_DIR / "calibration-build-manifest.json"
FIT_DIR = safety.EVIDENCE_ROOT / "offline" / "calibration-fit-v1"


class FirStopbandSummaryError(RuntimeError):
    """F-stage point evidence is incomplete, stale, or fails a hard gate."""


def f_cases() -> list[dict[str, Any]]:
    manifest = point.load_json(point.MATRIX_ROOT / "manifest.json")
    expected_sources = {
        "public_measurement_plan": safety.sha256_file(
            safety.FPGA_ROOT.parent / "public" / "信号前端测量方案.md"
        ),
        "m11_plan": safety.sha256_file(
            safety.FPGA_ROOT
            / "tool-of-rei"
            / "M11-真实全链路FIR与信号处理压力测试计划.md"
        ),
        "fir_coefficients": safety.sha256_file(
            safety.FPGA_ROOT / "Zynq_7010_PL" / "rtl" / "fir_coeffs_pkg.sv"
        ),
    }
    if manifest.get("source_hashes") != expected_sources:
        raise FirStopbandSummaryError("matrix-v3 source hashes are stale")
    records = [
        record
        for record in manifest.get("sine_points", [])
        if isinstance(record, dict) and record.get("stage") == "F"
    ]
    fixed = [record for record in records if str(record.get("case_id", "")).startswith("f-fixed-")]
    worst = [record for record in records if str(record.get("case_id", "")).startswith("f-worst-")]
    if len(records) != 23 or len(fixed) != 15 or len(worst) != 8:
        raise FirStopbandSummaryError("matrix-v3 must contain 15 fixed and 8 worst F points")
    if len({str(record["case_id"]) for record in records}) != len(records):
        raise FirStopbandSummaryError("matrix-v3 F case IDs are not unique")
    return records


def empirical_p95(values: list[float]) -> float:
    samples = np.asarray(values, dtype=np.float64)
    if samples.ndim != 1 or samples.size < point.MIN_POINT_FRAMES:
        raise FirStopbandSummaryError("at least 22 residual amplitudes are required")
    if not np.all(np.isfinite(samples)) or np.any(samples < 0):
        raise FirStopbandSummaryError("residual amplitudes must be finite and nonnegative")
    return float(np.quantile(samples, 0.95, method="linear"))


def attenuation_lower_bound_db(
    ch2_vpp_v: float,
    kadc_lower_code_per_v: float,
    residual_vpp_upper_code: float,
) -> float:
    if not all(
        math.isfinite(value) and value > 0
        for value in (ch2_vpp_v, kadc_lower_code_per_v, residual_vpp_upper_code)
    ):
        raise FirStopbandSummaryError("attenuation inputs must be finite and positive")
    unfiltered_code_vpp_lower = ch2_vpp_v * kadc_lower_code_per_v
    return 20.0 * math.log10(unfiltered_code_vpp_lower / residual_vpp_upper_code)


def f_scope_crosscheck_limit(grid_offset_bins: float) -> float:
    if not math.isfinite(grid_offset_bins) or not 0.0 <= grid_offset_bins <= 0.5 + 1e-9:
        raise FirStopbandSummaryError("F-stage FFT peak is outside the nearest half-bin")
    if grid_offset_bins <= FFT_COHERENT_GRID_OFFSET_BINS:
        return FFT_COHERENT_CROSSCHECK_LIMIT
    return FFT_NONCOHERENT_CROSSCHECK_LIMIT


def _f_wavebench_fft_evidence(
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
    )
    recorded_crosscheck = analysis.get("scope_fit_crosscheck", {}).get("comparison")
    if not isinstance(recorded_crosscheck, dict):
        raise FirStopbandSummaryError("F-stage scope cross-check evidence is missing")

    verified: dict[str, Any] = {}
    for channel in (1, 2):
        name = f"ch{channel}"
        channel_fft = fft["channels"][name]
        grid_offset_bins = float(channel_fft["frequency_offset_bins"])
        limit = f_scope_crosscheck_limit(grid_offset_bins)
        coherent = grid_offset_bins <= FFT_COHERENT_GRID_OFFSET_BINS
        _times, values, sample_rate_hz = point.load_scope_trace(
            Path(channel_fft["npy"])
        )
        fitted = point.tone_metrics(values, sample_rate_hz, expected_frequency_hz)
        fitted_vpp = float(fitted["fundamental_vpp"])
        wavebench_vpp = float(channel_fft["fundamental_vpp_v"])
        relative_delta = abs(fitted_vpp - wavebench_vpp) / wavebench_vpp
        recorded = recorded_crosscheck.get(name)
        if not isinstance(recorded, dict):
            raise FirStopbandSummaryError(f"F-stage {name} cross-check is missing")
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
                limit,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        )
        if (
            not numeric_matches
            or recorded.get("fft_grid_coherent") is not coherent
            or recorded.get("pass") is not True
            or relative_delta > limit
        ):
            raise FirStopbandSummaryError(
                f"F-stage {name} WaveBench/least-squares cross-check failed"
            )
        verified[name] = {
            "wavebench_fundamental_vpp_v": wavebench_vpp,
            "least_squares_fundamental_vpp_v": fitted_vpp,
            "relative_delta": relative_delta,
            "fft_grid_offset_bins": grid_offset_bins,
            "fft_grid_coherent": coherent,
            "limit": limit,
            "pass": True,
        }
    fft["known_frequency_crosscheck"] = {
        "role": "bounded cross-check only; WaveBench Hann FFT remains primary",
        "channels": verified,
        "pass": True,
    }
    return fft


def reference_kadc(fit: dict[str, Any]) -> dict[str, Any]:
    values: list[float] = []
    records: list[dict[str, Any]] = []
    for record in fit.get("training_records", []):
        source_vpp = float(record["source_vpp_v"])
        if source_vpp < 0.05:
            continue
        ch2_vpp = float(
            record["wavebench"]["channels"]["ch2"]["fundamental_vpp_v"]
        )
        adc_vpp = float(record["adc_fundamental_vpp_code"])
        value = adc_vpp / ch2_vpp
        values.append(value)
        records.append(
            {
                "case_id": record["case_id"],
                "source_vpp_v": source_vpp,
                "frequency_hz": float(record["frequency_hz"]),
                "kadc_code_per_v": value,
            }
        )
    if len(values) != 33:
        raise FirStopbandSummaryError("expected 33 >=50 mV calibration anchors")
    return {
        "policy": (
            "minimum of all 33 >=50 mV E training anchors; conservative CH2-to-code "
            "passband reference for attenuation lower bounds"
        ),
        "anchor_count": len(values),
        "minimum_code_per_v": float(min(values)),
        "median_code_per_v": float(statistics.median(values)),
        "maximum_code_per_v": float(max(values)),
        "records": records,
    }


def _select_point(points_root: Path, case_id: str) -> Path:
    candidates: list[Path] = []
    for candidate in sorted(points_root.glob(f"*_{case_id}")):
        if not candidate.is_dir() or not candidate.name.endswith(f"_{case_id}"):
            continue
        payload = point.load_json(candidate / "point.json")
        analysis = point.load_json(candidate / "analysis.json")
        if payload.get("pass") is True and analysis.get("pass") is True:
            candidates.append(candidate.resolve())
    if len(candidates) != 1:
        raise FirStopbandSummaryError(
            f"{case_id}: exactly one passing point is required, found {len(candidates)}"
        )
    return candidates[0]


def _adc_residual_distribution(capture_dir: Path, input_frequency_hz: float) -> dict[str, Any]:
    folded_hz = point.folded_frequency(input_frequency_hz)
    values: list[float] = []
    for path in sorted(capture_dir.glob("frame_*.s16le")):
        samples = np.fromfile(path, dtype="<i2")
        if samples.size != point.FRAME_SAMPLES:
            raise FirStopbandSummaryError(f"{path}: incomplete ADC frame")
        metrics = point.tone_metrics(
            samples.astype(np.float64), point.OUTPUT_SAMPLE_RATE_HZ, folded_hz
        )
        values.append(float(metrics["fundamental_vpp"]))
    if len(values) < point.MIN_POINT_FRAMES:
        raise FirStopbandSummaryError("F point has fewer than 22 complete ADC frames")
    return {
        "frame_count": len(values),
        "input_frequency_hz": input_frequency_hz,
        "folded_frequency_hz": folded_hz,
        "fundamental_vpp_code": {
            "minimum": float(min(values)),
            "median": float(statistics.median(values)),
            "empirical_p95_upper": empirical_p95(values),
            "maximum": float(max(values)),
        },
        "p95_policy": (
            "empirical 95th percentile of per-frame known-folded-frequency five-harmonic "
            "least-squares Vpp; no noise subtraction and no raw sample modification"
        ),
    }


def analyze_f_point(
    record: dict[str, Any],
    point_dir: Path,
    source_data_root: Path,
    kadc: dict[str, Any],
    expected_identity: dict[str, int],
) -> dict[str, Any]:
    verification = calibration.verify_sha256sums(point_dir)
    payload = point.load_json(point_dir / "point.json")
    analysis = point.load_json(point_dir / "analysis.json")
    case_id = str(record["case_id"])
    if (
        payload.get("case", {}).get("case_id") != case_id
        or analysis.get("case_id") != case_id
        or payload.get("case", {}).get("matrix_manifest_sha256")
        != safety.sha256_file(point.MATRIX_ROOT / "manifest.json")
    ):
        raise FirStopbandSummaryError(f"{case_id}: point identity or matrix binding failed")
    if (
        payload.get("dp800_writes") is not False
        or payload.get("scope_impedance_writes") is not False
        or payload.get("source_window", {}).get("off_status", {}).get("output") != "OFF"
        or payload.get("scope", {}).get("couplings_after") != {"1": "DCL", "2": "DCL"}
    ):
        raise FirStopbandSummaryError(f"{case_id}: final hardware safety state failed")
    observed_identity = analysis.get("calibration", {})
    for key, expected in expected_identity.items():
        if int(observed_identity.get(key, -1)) != expected:
            raise FirStopbandSummaryError(f"{case_id}: calibrated metadata {key} mismatch")
    if observed_identity.get("calibrated") is not True:
        raise FirStopbandSummaryError(f"{case_id}: CALIBRATED flag was not observed")

    frequency_hz = float(record["frequency_hz"])
    fft = _f_wavebench_fft_evidence(
        point_dir, payload, analysis, frequency_hz
    )
    source_archive = calibration._source_archive_evidence(
        point_dir, payload, source_data_root
    )
    capture_dir = Path(str(payload["lan"]["capture_dir"])).resolve()
    try:
        capture_dir.relative_to(point_dir)
    except ValueError as error:
        raise FirStopbandSummaryError(f"{case_id}: capture path escapes point") from error
    residual = _adc_residual_distribution(capture_dir, frequency_hz)
    ch1_vpp = float(fft["channels"]["ch1"]["fundamental_vpp_v"])
    ch2_vpp = float(fft["channels"]["ch2"]["fundamental_vpp_v"])
    residual_upper = float(residual["fundamental_vpp_code"]["empirical_p95_upper"])
    attenuation_db = attenuation_lower_bound_db(
        ch2_vpp, float(kadc["minimum_code_per_v"]), residual_upper
    )
    return {
        "case_id": case_id,
        "point_directory": str(point_dir),
        "frequency_hz": frequency_hz,
        "folded_frequency_hz": residual["folded_frequency_hz"],
        "source_vpp_v": float(record["source_vpp_v"]),
        "response_only": bool(record.get("response_only", False)),
        "theoretical_response": record.get("theoretical_response"),
        "scope_primary_method": fft["method"],
        "screenshots_used_for_numeric_results": False,
        "ch1_fundamental_vpp_v": ch1_vpp,
        "ch2_fundamental_vpp_v": ch2_vpp,
        "adc_residual": residual,
        "unfiltered_code_vpp_lower": ch2_vpp * float(kadc["minimum_code_per_v"]),
        "equivalent_ch2_residual_vpp_upper": (
            residual_upper / float(kadc["minimum_code_per_v"])
        ),
        "attenuation_lower_bound_db": attenuation_db,
        "attenuation_limit_db": ATTENUATION_LIMIT_DB,
        "attenuation_pass": attenuation_db >= ATTENUATION_LIMIT_DB,
        "calibration": {
            key: int(observed_identity[key]) for key in expected_identity
        },
        "lan_frame_count": int(payload["lan"]["frame_count"]),
        "pcap": source_archive,
        "point_sha256sums_sha256": verification["sha256"],
        "point_json_sha256": safety.sha256_file(point_dir / "point.json"),
        "analysis_json_sha256": safety.sha256_file(point_dir / "analysis.json"),
        "instrument_end_state": {
            "dg_output": "OFF",
            "scope_couplings": payload["scope"]["couplings_after"],
            "dp832_writes": False,
        },
    }


def build_summary(points_root: Path, source_data_root: Path) -> dict[str, Any]:
    fit, fit_verification = calibration.load_frozen_fit(FIT_DIR)
    manifest = point.load_json(CALIBRATION_MANIFEST)
    if manifest.get("status") != "validated" or manifest.get("validation", {}).get("holdout_pass") is not True:
        raise FirStopbandSummaryError("calibration manifest is not validated")
    expected_identity = {
        "calibration_id": int(manifest["calibration_id"]),
        "scale_uv_per_lsb": int(manifest["scale_uv_per_lsb"]),
        "offset_uv": int(manifest["offset_uv"]),
    }
    kadc = reference_kadc(fit)
    records = [
        analyze_f_point(
            record,
            _select_point(points_root, str(record["case_id"])),
            source_data_root,
            kadc,
            expected_identity,
        )
        for record in f_cases()
    ]
    failures = [
        f"{record['case_id']}: attenuation lower bound is below 50 dB"
        for record in records
        if not record["attenuation_pass"]
    ]
    return {
        "format": SUMMARY_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "pass" if not failures else "fail",
        "scope_numeric_policy": (
            "WaveBench archived CH1/CH2 NPY + wavebench.data.fft.analyze_fft; screenshots "
            "are qualitative only"
        ),
        "adc_residual_policy": (
            "known final folded frequency least squares per raw frame; empirical p95 is "
            "used as an upper residual without noise subtraction"
        ),
        "calibration_manifest": str(CALIBRATION_MANIFEST.resolve()),
        "calibration_manifest_sha256": safety.sha256_file(CALIBRATION_MANIFEST),
        "fit_sha256": safety.sha256_file(FIT_DIR / "fit.json"),
        "fit_sha256sums_sha256": fit_verification["sha256"],
        "expected_calibration_identity": expected_identity,
        "reference_kadc": kadc,
        "point_count": len(records),
        "frame_count": sum(int(record["lan_frame_count"]) for record in records),
        "minimum_attenuation_lower_bound_db": min(
            float(record["attenuation_lower_bound_db"]) for record in records
        ),
        "attenuation_limit_db": ATTENUATION_LIMIT_DB,
        "records": records,
        "raw_samples_modified": False,
        "failures": failures,
        "pass": not failures,
    }


CSV_FIELDS = (
    "case_id",
    "frequency_hz",
    "folded_frequency_hz",
    "source_vpp_v",
    "ch1_fundamental_vpp_v",
    "ch2_fundamental_vpp_v",
    "adc_residual_vpp_code_median",
    "adc_residual_vpp_code_p95_upper",
    "equivalent_ch2_residual_vpp_upper",
    "attenuation_lower_bound_db",
    "attenuation_pass",
    "lan_frame_count",
    "point_json_sha256",
)


def _csv_row(record: dict[str, Any]) -> dict[str, Any]:
    residual = record["adc_residual"]["fundamental_vpp_code"]
    return {
        "case_id": record["case_id"],
        "frequency_hz": record["frequency_hz"],
        "folded_frequency_hz": record["folded_frequency_hz"],
        "source_vpp_v": record["source_vpp_v"],
        "ch1_fundamental_vpp_v": record["ch1_fundamental_vpp_v"],
        "ch2_fundamental_vpp_v": record["ch2_fundamental_vpp_v"],
        "adc_residual_vpp_code_median": residual["median"],
        "adc_residual_vpp_code_p95_upper": residual["empirical_p95_upper"],
        "equivalent_ch2_residual_vpp_upper": record["equivalent_ch2_residual_vpp_upper"],
        "attenuation_lower_bound_db": record["attenuation_lower_bound_db"],
        "attenuation_pass": record["attenuation_pass"],
        "lan_frame_count": record["lan_frame_count"],
        "point_json_sha256": record["point_json_sha256"],
    }


def write_summary(output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if output_dir.exists():
        raise FirStopbandSummaryError(f"output already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    summary_path = output_dir / "summary.json"
    safety.write_json_exclusive(summary_path, summary)
    csv_path = output_dir / "points.csv"
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_row(record) for record in summary["records"])
    sums = safety._write_sha256sums(output_dir)
    return {
        "pass": summary["pass"],
        "point_count": summary["point_count"],
        "frame_count": summary["frame_count"],
        "minimum_attenuation_lower_bound_db": summary[
            "minimum_attenuation_lower_bound_db"
        ],
        "summary": str(summary_path.resolve()),
        "csv": str(csv_path.resolve()),
        "sha256sums": str(sums.resolve()),
        "failures": summary["failures"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points-root", type=Path, default=safety.EVIDENCE_ROOT / "points")
    parser.add_argument("--source-data-root", type=Path, default=safety.SOURCE_DATA_ROOT)
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
            f"M11_FIR_STOPBAND_SUMMARY_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
