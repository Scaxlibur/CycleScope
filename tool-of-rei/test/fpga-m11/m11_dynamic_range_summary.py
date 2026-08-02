#!/usr/bin/env python3
"""Build an immutable M11-D dynamic-range summary from closed point evidence."""

# ruff: noqa: E402 -- adjacent M11 and CSLP analysis modules are deliberate inputs.

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

import m11_sine_point as point
import m11_wavebench_safe as safety

PS_TOOLS = (
    safety.FPGA_ROOT / "Zynq_7010_PS" / "cyclescope_cslp" / "tools"
)
if str(PS_TOOLS) not in sys.path:
    sys.path.insert(0, str(PS_TOOLS))

import cslp_adc_analyze as adc_analysis


FREQUENCIES_HZ = (10_000, 100_000, 200_000, 500_000)
ALLOWED_AMPLITUDES_VPP = (0.01, 0.05, 0.1, 0.25, 0.45)
LINEAR_FIT_AMPLITUDES_VPP = (0.05, 0.25, 0.45)
MARGIN_CASE_ID = "d-10000Hz-500mVpp"
OUTLIER_FLOOR_CODE = 8.0
OUTLIER_MAD_MULTIPLIER = 8.0 * 1.4826


class DynamicRangeSummaryError(RuntimeError):
    """The M11-D evidence set cannot be summarized without ambiguity."""


def case_id(frequency_hz: int, amplitude_vpp: float) -> str:
    amplitude_mv = int(round(amplitude_vpp * 1000.0))
    return f"d-{frequency_hz}Hz-{amplitude_mv:03d}mVpp"


def robust_sine_outliers(
    values: np.ndarray,
    sample_rate_hz: float,
    frequency_hz: float,
    *,
    floor: float = OUTLIER_FLOOR_CODE,
) -> dict[str, float | int]:
    samples = np.asarray(values, dtype=np.float64)
    if samples.ndim != 1 or samples.size < 32 or not np.all(np.isfinite(samples)):
        raise DynamicRangeSummaryError("invalid ADC samples for robust outlier analysis")
    if not 0 < frequency_hz < sample_rate_hz / 2:
        raise DynamicRangeSummaryError("outlier fit frequency is outside Nyquist")
    if not math.isfinite(floor) or floor <= 0:
        raise DynamicRangeSummaryError("outlier floor must be finite and positive")

    time_s = np.arange(samples.size, dtype=np.float64) / sample_rate_hz
    columns = [np.ones(samples.size, dtype=np.float64)]
    for order in range(1, 6):
        harmonic_hz = order * frequency_hz
        if harmonic_hz >= sample_rate_hz / 2:
            break
        phase = 2.0 * math.pi * harmonic_hz * time_s
        columns.extend((np.sin(phase), np.cos(phase)))
    matrix = np.column_stack(columns)
    coefficients, *_ = np.linalg.lstsq(matrix, samples, rcond=None)
    residual = samples - matrix @ coefficients
    residual_median = float(np.median(residual))
    residual_mad = float(np.median(np.abs(residual - residual_median)))
    threshold = max(floor, OUTLIER_MAD_MULTIPLIER * residual_mad)
    outlier_count = int(
        np.count_nonzero(np.abs(residual - residual_median) > threshold)
    )
    return {
        "samples": int(samples.size),
        "outlier_count": outlier_count,
        "outlier_rate": float(outlier_count / samples.size),
        "threshold_code": float(threshold),
        "residual_mad_code": residual_mad,
    }


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        raise DynamicRangeSummaryError("cannot summarize an empty metric list")
    return {
        "minimum": float(min(values)),
        "median": float(statistics.median(values)),
        "maximum": float(max(values)),
    }


def _load_point_candidates(points_root: Path) -> dict[str, list[Path]]:
    candidates: dict[str, list[Path]] = {}
    for path in points_root.glob("*_d-*/point.json"):
        payload = point.load_json(path)
        case = payload.get("case")
        if not isinstance(case, dict) or case.get("stage") != "D":
            continue
        identifier = str(case.get("case_id", ""))
        candidates.setdefault(identifier, []).append(path.parent)
    return candidates


def select_canonical_points(points_root: Path) -> dict[str, Path]:
    candidates = _load_point_candidates(points_root)
    required = {
        case_id(frequency_hz, amplitude_vpp)
        for frequency_hz in FREQUENCIES_HZ
        for amplitude_vpp in ALLOWED_AMPLITUDES_VPP
    }
    required.add(MARGIN_CASE_ID)
    selected: dict[str, Path] = {}
    for identifier in sorted(required):
        eligible: list[Path] = []
        for candidate in candidates.get(identifier, []):
            payload = point.load_json(candidate / "point.json")
            archive = payload.get("lan", {}).get("packet_archive", {})
            if archive.get("pass") is True:
                eligible.append(candidate)
        if not eligible:
            raise DynamicRangeSummaryError(
                f"{identifier}: no point has a verified packet archive"
            )
        selected[identifier] = max(eligible, key=lambda path: path.name)
    return selected


def _scope_npy(scope_package: Path, channel: int) -> Path:
    path = scope_package / f"ch{channel}.npy"
    if not path.is_file():
        raise DynamicRangeSummaryError(f"missing WaveBench CH{channel} NPY: {path}")
    return path


def _supplemental_scope_metrics(
    scope_package: Path, frequency_hz: float, channel: int
) -> dict[str, float]:
    _times, values, sample_rate_hz = point.load_scope_trace(
        _scope_npy(scope_package, channel)
    )
    measured_frequency_hz = adc_analysis.estimate_frequency(
        values, sample_rate_hz, frequency_hz
    )
    metrics = adc_analysis.tone_metrics(
        values, sample_rate_hz, measured_frequency_hz
    )
    return {
        "measured_frequency_hz": measured_frequency_hz,
        "sfdr_db": float(metrics["sfdr_db"]),
    }


def _supplemental_adc_metrics(
    capture_dir: Path, frequency_hz: float
) -> dict[str, Any]:
    frame_paths = sorted(capture_dir.glob("frame_*.s16le"))
    if len(frame_paths) < point.MIN_POINT_FRAMES:
        raise DynamicRangeSummaryError(
            f"{capture_dir}: only {len(frame_paths)} complete ADC frames"
        )
    sfdr_values: list[float] = []
    measured_frequencies: list[float] = []
    thresholds: list[float] = []
    residual_mads: list[float] = []
    total_outliers = 0
    total_samples = 0
    for path in frame_paths:
        raw_values = np.fromfile(path, dtype="<i2")
        if raw_values.size != point.FRAME_SAMPLES:
            raise DynamicRangeSummaryError(
                f"{path}: expected {point.FRAME_SAMPLES} ADC samples"
            )
        values = raw_values.astype(np.float64)
        measured_frequency_hz = adc_analysis.estimate_frequency(
            values, point.OUTPUT_SAMPLE_RATE_HZ, frequency_hz
        )
        tone = adc_analysis.tone_metrics(
            values, point.OUTPUT_SAMPLE_RATE_HZ, measured_frequency_hz
        )
        outliers = robust_sine_outliers(
            values,
            point.OUTPUT_SAMPLE_RATE_HZ,
            measured_frequency_hz,
        )
        measured_frequencies.append(measured_frequency_hz)
        sfdr_values.append(float(tone["sfdr_db"]))
        thresholds.append(float(outliers["threshold_code"]))
        residual_mads.append(float(outliers["residual_mad_code"]))
        total_outliers += int(outliers["outlier_count"])
        total_samples += int(outliers["samples"])
    return {
        "frame_count": len(frame_paths),
        "frequency_hz": _summary(measured_frequencies),
        "sfdr_db": _summary(sfdr_values),
        "outliers": {
            "count": total_outliers,
            "rate": float(total_outliers / total_samples),
            "samples": total_samples,
            "threshold_code": _summary(thresholds),
            "residual_mad_code": _summary(residual_mads),
            "policy": (
                "report-only five-harmonic residual; threshold=max(8 code, "
                "8*1.4826*MAD); raw samples are never removed or replaced"
            ),
        },
    }


def analyze_allowed_point(identifier: str, point_dir: Path) -> dict[str, Any]:
    verification = point._verify_point_sha256sums(point_dir)
    payload = point.load_json(point_dir / "point.json")
    analysis = point.load_json(point_dir / "analysis.json")
    case = payload.get("case", {})
    if case.get("case_id") != identifier or analysis.get("case_id") != identifier:
        raise DynamicRangeSummaryError(f"{identifier}: point/analysis identity mismatch")
    if payload.get("pass") is not True or analysis.get("pass") is not True:
        raise DynamicRangeSummaryError(f"{identifier}: allowed point did not pass")
    if payload.get("dp800_writes") is not False:
        raise DynamicRangeSummaryError(f"{identifier}: DP832 zero-write gate failed")
    if payload.get("source_window", {}).get("off_status", {}).get("output") != "OFF":
        raise DynamicRangeSummaryError(f"{identifier}: DG final OFF gate failed")
    lan = payload.get("lan", {})
    if lan.get("pass") is not True or lan.get("packet_archive", {}).get("pass") is not True:
        raise DynamicRangeSummaryError(f"{identifier}: LAN/pcap gate failed")

    raw_archives = payload.get("wavebench_raw_archives")
    if not isinstance(raw_archives, list) or len(raw_archives) != 1:
        raise DynamicRangeSummaryError(f"{identifier}: one WaveBench raw archive required")
    scope_package = Path(str(raw_archives[0]["destination"])).resolve()
    capture_dir = Path(str(lan["capture_dir"])).resolve()
    for path in (scope_package, capture_dir):
        try:
            path.relative_to(point_dir.resolve())
        except ValueError as error:
            raise DynamicRangeSummaryError(
                f"{identifier}: evidence path escapes point directory"
            ) from error

    source = analysis["source"]
    scope = analysis["scope_primary"]
    adc = analysis["adc"]["metrics"]
    supplemental_adc = _supplemental_adc_metrics(
        capture_dir, float(source["frequency_hz"])
    )
    packet_archive = lan["packet_archive"]
    return {
        "case_id": identifier,
        "point_directory": str(point_dir.resolve()),
        "point_sha256": safety.sha256_file(point_dir / "point.json"),
        "analysis_sha256": safety.sha256_file(point_dir / "analysis.json"),
        "source_sha256sums": {
            "path": verification["manifest"],
            "sha256": verification["manifest_sha256"],
            "files_verified": verification["files_verified"],
        },
        "frequency_hz": float(source["frequency_hz"]),
        "source_vpp_v": float(source["vpp_v"]),
        "scope_primary_method": scope["primary_method"],
        "ch1": {
            "fundamental_vpp_v": float(scope["ch1"]["fundamental_vpp_v"]),
            "raw_vpp_v": float(scope["ch1"]["metadata_summary"]["voltage_vpp_v"]),
            "thd_ratio": float(scope["ch1"]["fft"]["thd_ratio"]),
            **_supplemental_scope_metrics(
                scope_package, float(source["frequency_hz"]), 1
            ),
        },
        "ch2": {
            "fundamental_vpp_v": float(scope["ch2"]["fundamental_vpp_v"]),
            "raw_vpp_v": float(scope["ch2"]["metadata_summary"]["voltage_vpp_v"]),
            "thd_ratio": float(scope["ch2"]["fft"]["thd_ratio"]),
            **_supplemental_scope_metrics(
                scope_package, float(source["frequency_hz"]), 2
            ),
        },
        "adc": {
            "fundamental_vpp_code": float(adc["fundamental_vpp"]["median"]),
            "minimum_code": float(adc["minimum"]["minimum"]),
            "maximum_code": float(adc["maximum"]["maximum"]),
            "mean_code": float(adc["mean"]["median"]),
            "thd_ratio": float(adc["thd_ratio"]["median"]),
            **supplemental_adc,
        },
        "ratios": analysis["ratios"],
        "lan": {
            "frame_count": int(lan["frame_count"]),
            "pcap_archive": str(packet_archive["directory"]),
            "pcap_sha256": str(packet_archive["wire_pcap_sha256"]),
            "pass": True,
        },
        "instrument_end_state": {
            "dg_output": "OFF",
            "dp832_writes": False,
            "scope_couplings": payload["scope"]["couplings_after"],
        },
    }


def frequency_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_amplitude = {float(item["source_vpp_v"]): item for item in records}
    fit_records = [by_amplitude[value] for value in LINEAR_FIT_AMPLITUDES_VPP]
    x = np.asarray(LINEAR_FIT_AMPLITUDES_VPP, dtype=np.float64)
    y = np.asarray(
        [item["adc"]["fundamental_vpp_code"] for item in fit_records],
        dtype=np.float64,
    )
    slope = float(np.dot(x, y) / np.dot(x, x))
    residual_mv = np.abs(y - slope * x) / slope * 1000.0
    gamp_250 = float(by_amplitude[0.25]["ratios"]["gamp_v_per_v"])
    gamp_450 = float(by_amplitude[0.45]["ratios"]["gamp_v_per_v"])
    change_percent = (gamp_450 / gamp_250 - 1.0) * 100.0
    large = by_amplitude[0.45]
    return {
        "linear_fit": {
            "amplitudes_vpp": list(LINEAR_FIT_AMPLITUDES_VPP),
            "ke2e_code_per_v": slope,
            "maximum_equivalent_input_residual_mv": float(np.max(residual_mv)),
        },
        "compression": {
            "gamp_250_v_per_v": gamp_250,
            "gamp_450_v_per_v": gamp_450,
            "change_450_vs_250_percent": change_percent,
            "absolute_change_percent": abs(change_percent),
            "engineering_target_0_5_percent_pass": abs(change_percent) <= 0.5,
            "hard_limit_1_percent_pass": abs(change_percent) <= 1.0,
        },
        "large_signal": {
            "ch1_thd_ratio": large["ch1"]["thd_ratio"],
            "ch2_thd_ratio": large["ch2"]["thd_ratio"],
            "adc_thd_ratio": large["adc"]["thd_ratio"],
            "adc_sfdr_db_median": large["adc"]["sfdr_db"]["median"],
            "adc_outlier_count": large["adc"]["outliers"]["count"],
            "thd_gate_pass": (
                large["adc"]["thd_ratio"] <= 0.001
                and large["ch2"]["thd_ratio"]
                <= large["ch1"]["thd_ratio"] + 0.001
            ),
        },
        "safety": {
            "ch1_raw_vpp_v": large["ch1"]["raw_vpp_v"],
            "ch2_raw_vpp_v": large["ch2"]["raw_vpp_v"],
            "adc_minimum_code": large["adc"]["minimum_code"],
            "adc_maximum_code": large["adc"]["maximum_code"],
        },
    }


def _verify_margin_point(point_dir: Path) -> dict[str, Any]:
    verification = point._verify_point_sha256sums(point_dir)
    payload = point.load_json(point_dir / "point.json")
    analysis = point.load_json(point_dir / "analysis.json")
    if payload.get("pass") is not False or analysis.get("pass") is not False:
        raise DynamicRangeSummaryError("500 mV margin point must remain a recorded FAIL")
    return {
        "case_id": MARGIN_CASE_ID,
        "point_directory": str(point_dir.resolve()),
        "point_sha256": safety.sha256_file(point_dir / "point.json"),
        "analysis_sha256": safety.sha256_file(point_dir / "analysis.json"),
        "source_sha256sums_sha256": verification["manifest_sha256"],
        "failures": analysis.get("failures", []),
        "dg_final_output": payload["source_window"]["off_status"]["output"],
        "pcap_pass": payload["lan"]["packet_archive"]["pass"],
    }


def build_summary(points_root: Path) -> dict[str, Any]:
    selected = select_canonical_points(points_root)
    records: list[dict[str, Any]] = []
    for frequency_hz in FREQUENCIES_HZ:
        for amplitude_vpp in ALLOWED_AMPLITUDES_VPP:
            identifier = case_id(frequency_hz, amplitude_vpp)
            records.append(analyze_allowed_point(identifier, selected[identifier]))

    per_frequency = {
        str(frequency_hz): frequency_summary(
            [item for item in records if item["frequency_hz"] == frequency_hz]
        )
        for frequency_hz in FREQUENCIES_HZ
    }
    ceiling_path = point.DYNAMIC_RANGE_CEILING_EVIDENCE
    ceiling = point.load_json(ceiling_path)
    margin = _verify_margin_point(selected[MARGIN_CASE_ID])
    if margin["point_sha256"] != ceiling["failed_point"]["point_sha256"]:
        raise DynamicRangeSummaryError("dynamic ceiling does not bind the margin point")

    compression_values = [
        item["compression"]["absolute_change_percent"]
        for item in per_frequency.values()
    ]
    warnings: list[str] = []
    if max(compression_values) > 0.5:
        warnings.append(
            "100 kHz 450-vs-250 mV Gamp change misses the 0.5% engineering target"
        )
    total_outliers = sum(item["adc"]["outliers"]["count"] for item in records)
    total_samples = sum(item["adc"]["outliers"]["samples"] for item in records)
    hard_pass = (
        all(item["compression"]["hard_limit_1_percent_pass"] for item in per_frequency.values())
        and all(item["large_signal"]["thd_gate_pass"] for item in per_frequency.values())
        and all(item["ch1"]["raw_vpp_v"] <= point.MAX_CH1_VPP for item in records)
        and all(item["ch2"]["raw_vpp_v"] <= point.MAX_CH2_VPP for item in records)
        and all(item["lan"]["pass"] for item in records)
    )
    return {
        "format": "CycleScope M11-D dynamic range summary v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "scope_primary_method": (
            "WaveBench archived NPY + wavebench.data.fft.analyze_fft; screenshots are "
            "qualitative only and provide no numeric values"
        ),
        "supplemental_method": (
            "CSLP NumPy refined-frequency tone fit for SFDR plus report-only robust "
            "five-harmonic residual outlier counting"
        ),
        "decision": (
            "D stage is closed through 0.45 Vpp; the non-contest 0.5 Vpp margin point "
            "remains FAIL and is not repeated at other frequencies"
        ),
        "hard_gate_pass": hard_pass,
        "engineering_target_pass": not warnings,
        "warnings": warnings,
        "allowed_points": records,
        "allowed_point_count": len(records),
        "allowed_frame_count": sum(item["lan"]["frame_count"] for item in records),
        "outliers": {
            "count": total_outliers,
            "samples": total_samples,
            "rate": float(total_outliers / total_samples),
            "raw_samples_modified": False,
        },
        "per_frequency": per_frequency,
        "maximum_absolute_gamp_change_percent": max(compression_values),
        "margin_point": margin,
        "dynamic_ceiling": {
            "path": str(ceiling_path.resolve()),
            "sha256": safety.sha256_file(ceiling_path),
            "decision": ceiling["decision"],
        },
        "calibration_ready": False,
        "next_step": (
            "M11-E training sweep, response fit, seven independent holdouts, and a "
            "nonzero calibration_id"
        ),
    }


CSV_FIELDS = (
    "case_id",
    "frequency_hz",
    "source_vpp_v",
    "ch1_fundamental_vpp_v",
    "ch2_fundamental_vpp_v",
    "adc_fundamental_vpp_code",
    "gamp_v_per_v",
    "ke2e_code_per_vset_v",
    "ch1_thd_ratio",
    "ch2_thd_ratio",
    "adc_thd_ratio",
    "ch1_sfdr_db",
    "ch2_sfdr_db",
    "adc_sfdr_db_median",
    "adc_outlier_count",
    "adc_outlier_rate",
    "frame_count",
    "point_sha256",
)


def _csv_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": record["case_id"],
        "frequency_hz": record["frequency_hz"],
        "source_vpp_v": record["source_vpp_v"],
        "ch1_fundamental_vpp_v": record["ch1"]["fundamental_vpp_v"],
        "ch2_fundamental_vpp_v": record["ch2"]["fundamental_vpp_v"],
        "adc_fundamental_vpp_code": record["adc"]["fundamental_vpp_code"],
        "gamp_v_per_v": record["ratios"]["gamp_v_per_v"],
        "ke2e_code_per_vset_v": record["ratios"]["ke2e_code_per_vset_v"],
        "ch1_thd_ratio": record["ch1"]["thd_ratio"],
        "ch2_thd_ratio": record["ch2"]["thd_ratio"],
        "adc_thd_ratio": record["adc"]["thd_ratio"],
        "ch1_sfdr_db": record["ch1"]["sfdr_db"],
        "ch2_sfdr_db": record["ch2"]["sfdr_db"],
        "adc_sfdr_db_median": record["adc"]["sfdr_db"]["median"],
        "adc_outlier_count": record["adc"]["outliers"]["count"],
        "adc_outlier_rate": record["adc"]["outliers"]["rate"],
        "frame_count": record["lan"]["frame_count"],
        "point_sha256": record["point_sha256"],
    }


def write_summary(output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if output_dir.exists():
        raise DynamicRangeSummaryError(f"output already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    summary_path = output_dir / "summary.json"
    safety.write_json_exclusive(summary_path, summary)
    csv_path = output_dir / "points.csv"
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_row(record) for record in summary["allowed_points"])
    sums = safety._write_sha256sums(output_dir)
    return {
        "pass": summary["hard_gate_pass"],
        "engineering_target_pass": summary["engineering_target_pass"],
        "summary": str(summary_path.resolve()),
        "csv": str(csv_path.resolve()),
        "sha256sums": str(sums.resolve()),
        "warnings": summary["warnings"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--points-root",
        type=Path,
        default=safety.EVIDENCE_ROOT / "points",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = write_summary(
            args.output_dir.resolve(), build_summary(args.points_root.resolve())
        )
    except Exception as error:
        print(
            f"M11_DYNAMIC_RANGE_SUMMARY_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
