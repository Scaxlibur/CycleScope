#!/usr/bin/env python3
"""Build an immutable M11-G multitone recovery summary from closed evidence."""

# ruff: noqa: E402 -- adjacent M11 modules are deliberate evidence inputs.

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m11_arb_point as arb
import m11_calibration as calibration
import m11_repeat_arb_driver as repeat_arb
import m11_sine_point as sine
import m11_wavebench_safe as safety


SUMMARY_FORMAT = "CycleScope M11-G multitone recovery summary v1"
CALIBRATION_MANIFEST = (
    safety.EVIDENCE_ROOT
    / "offline"
    / "calibration-v1"
    / "calibration-build-manifest.json"
)


class MultitoneSummaryError(RuntimeError):
    """The M11-G evidence set is incomplete, stale, or ambiguous."""


def g_cases() -> list[dict[str, Any]]:
    manifest = sine.load_json(arb.MATRIX_ROOT / "manifest.json")
    try:
        arb.arb_dry.validate_source_hashes(manifest)
    except RuntimeError as error:
        raise MultitoneSummaryError(str(error)) from error
    records = [
        item
        for item in manifest.get("arb_points", [])
        if isinstance(item, dict) and item.get("stage") == "G"
    ]
    if len(records) != 10 or len({item["case_id"] for item in records}) != 10:
        raise MultitoneSummaryError("matrix-v3 must contain exactly 10 unique G cases")
    return records


def _select_point(points_root: Path, case_id: str) -> Path:
    eligible: list[Path] = []
    for candidate in sorted(points_root.glob(f"*_{case_id}")):
        if not candidate.is_dir() or not candidate.name.endswith(f"_{case_id}"):
            continue
        point_path = candidate / "point.json"
        analysis_path = candidate / "analysis.json"
        if not point_path.is_file() or not analysis_path.is_file():
            continue
        payload = sine.load_json(point_path)
        analysis = sine.load_json(analysis_path)
        if payload.get("pass") is True and analysis.get("pass") is True:
            eligible.append(candidate.resolve())
    if len(eligible) != 1:
        raise MultitoneSummaryError(
            f"{case_id}: exactly one passing point is required, found {len(eligible)}"
        )
    return eligible[0]


def _maximum_line_error(analysis: dict[str, Any]) -> float:
    lines = analysis["adc_recovery"]["line_results"]
    if not lines:
        raise MultitoneSummaryError("G point has no recovered line metrics")
    return max(float(item["absolute_error_v"]) for item in lines)


def analyze_g_point(
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
        or case.get("stage") != "G"
        or case.get("matrix_manifest_sha256")
        != safety.sha256_file(arb.MATRIX_ROOT / "manifest.json")
    ):
        raise MultitoneSummaryError(f"{case_id}: case identity or matrix binding failed")
    if (
        payload.get("dp800_writes") is not False
        or payload.get("scope_impedance_writes") is not False
        or payload.get("source_window", {}).get("off_status", {}).get("output") != "OFF"
        or payload.get("source_window", {}).get("off_status", {}).get("function") != "USER"
        or payload.get("scope", {}).get("couplings_after") != {"1": "DCL", "2": "DCL"}
    ):
        raise MultitoneSummaryError(f"{case_id}: final hardware safety state failed")
    if payload.get("arb_configuration_mode") != "hash-bound-wavebench-repeat-user-to-user":
        raise MultitoneSummaryError(f"{case_id}: repeated ARB configuration mode is not bound")

    raw_archives = payload.get("wavebench_raw_archives")
    lan = payload.get("lan")
    if not isinstance(raw_archives, list) or len(raw_archives) != 1 or not isinstance(lan, dict):
        raise MultitoneSummaryError(f"{case_id}: raw/LAN evidence binding is incomplete")
    scope_package = Path(str(raw_archives[0]["destination"])).resolve()
    capture_dir = Path(str(lan["capture_dir"])).resolve()
    lan_report = Path(str(lan["report"])).resolve()
    for path in (scope_package, capture_dir, lan_report):
        try:
            path.relative_to(point_dir)
        except ValueError as error:
            raise MultitoneSummaryError(f"{case_id}: raw input escapes point") from error
    recomputed = arb.analyze_point(
        record=case,
        scope_package=scope_package,
        capture_dir=capture_dir,
        lan_report_path=lan_report,
    )
    if calibration.canonical_sha256(recomputed) != calibration.canonical_sha256(recorded):
        raise MultitoneSummaryError(f"{case_id}: raw reanalysis is not reproducible")
    if "wavebench.data.fft.analyze_fft" not in str(
        recomputed["scope_primary"].get("primary_method", "")
    ):
        raise MultitoneSummaryError(f"{case_id}: WaveBench FFT is not the scope primary")
    if recomputed.get("screenshots_used_for_numeric_results") is not False:
        raise MultitoneSummaryError(f"{case_id}: screenshot entered numeric analysis")
    observed = recomputed["calibration"]
    for key, expected in expected_identity.items():
        if int(observed.get(key, -1)) != expected:
            raise MultitoneSummaryError(f"{case_id}: calibration {key} mismatch")
    if observed.get("calibrated") is not True:
        raise MultitoneSummaryError(f"{case_id}: CALIBRATED flag is missing")

    repeat_path = point_dir / "wavebench" / "run" / "repeat-arb-upload" / "run.json"
    repeat_payload = sine.load_json(repeat_path)
    audit = repeat_payload.get("result", {}).get("audit", {})
    if (
        audit.get("distribution") != repeat_arb.EXPECTED_DISTRIBUTION
        or audit.get("version") != repeat_arb.EXPECTED_VERSION
        or audit.get("driver_source_sha256") != repeat_arb.EXPECTED_DRIVER_SHA256
        or repeat_payload.get("pass") is not True
    ):
        raise MultitoneSummaryError(f"{case_id}: repeated ARB driver audit failed")

    source_archive = calibration._source_archive_evidence(
        point_dir, payload, source_data_root
    )
    recovery = recomputed["adc_recovery"]
    aggregate = recovery["aggregate_metrics"]
    maximum_line_error_v = _maximum_line_error(recomputed)
    if (
        maximum_line_error_v > arb.CALIBRATION_HARD_LIMIT_V
        or float(aggregate["vpp"]["absolute_error_v"]) > arb.CALIBRATION_HARD_LIMIT_V
        or float(aggregate["true_rms"]["absolute_error_v"])
        > arb.CALIBRATION_HARD_LIMIT_V
        or recovery["effective_band_residual_intermod"].get("pass") is not True
    ):
        raise MultitoneSummaryError(f"{case_id}: hard recovery gate failed")
    return {
        "case_id": case_id,
        "point_directory": str(point_dir),
        "signal_class": case["signal_class"],
        "crest_variant": case["crest_variant"],
        "crest_factor": float(case["crest_factor"]),
        "source_vpp_v": float(case["source_vpp_v"]),
        "expected_true_rms_v": float(case["true_rms_v"]),
        "component_count": len(case["components"]),
        "line_results": recovery["line_results"],
        "maximum_line_absolute_error_v": maximum_line_error_v,
        "vpp": aggregate["vpp"],
        "true_rms": aggregate["true_rms"],
        "intermod_residual_peak_v_p95": float(
            recovery["effective_band_residual_intermod"]["largest_spur_input_peak_v"][
                "empirical_p95"
            ]
        ),
        "outlier_count": int(recovery["outliers"]["count"]),
        "outlier_rate": float(recovery["outliers"]["rate"]),
        "frame_count": int(lan["frame_count"]),
        "wave_packets": int(source_archive["wave_packets"]),
        "target_warnings": list(recovery["target_warnings"]),
        "target_pass": (
            maximum_line_error_v <= arb.CALIBRATION_TARGET_V
            and aggregate["vpp"]["target_pass"] is True
            and aggregate["true_rms"]["target_pass"] is True
        ),
        "hard_pass": True,
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
        raise MultitoneSummaryError("calibration manifest is not validated")
    expected_identity = {
        "calibration_id": int(manifest["calibration_id"]),
        "scale_uv_per_lsb": int(manifest["scale_uv_per_lsb"]),
        "offset_uv": int(manifest["offset_uv"]),
    }
    records = [
        analyze_g_point(
            record,
            _select_point(points_root, str(record["case_id"])),
            source_data_root,
            expected_identity,
        )
        for record in g_cases()
    ]
    maximum_line_error = max(
        float(item["maximum_line_absolute_error_v"]) for item in records
    )
    maximum_vpp_error = max(float(item["vpp"]["absolute_error_v"]) for item in records)
    maximum_rms_error = max(
        float(item["true_rms"]["absolute_error_v"]) for item in records
    )
    maximum_intermod = max(float(item["intermod_residual_peak_v_p95"]) for item in records)
    target_pass = all(item["target_pass"] for item in records)
    hard_pass = all(item["hard_pass"] for item in records)
    return {
        "format": SUMMARY_FORMAT,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "pass" if hard_pass else "fail",
        "target_status": "pass" if target_pass else "warning",
        "scope_numeric_policy": (
            "WaveBench archived NPY + wavebench.data.fft.analyze_fft is the global "
            "spectrum primary; manifest-frequency joint least squares resolves every line"
        ),
        "screenshots_used_for_numeric_results": False,
        "raw_samples_modified": False,
        "calibration_manifest": str(CALIBRATION_MANIFEST.resolve()),
        "calibration_manifest_sha256": safety.sha256_file(CALIBRATION_MANIFEST),
        "expected_calibration_identity": expected_identity,
        "point_count": len(records),
        "frame_count": sum(int(item["frame_count"]) for item in records),
        "wave_packet_count": sum(int(item["wave_packets"]) for item in records),
        "component_count": sum(int(item["component_count"]) for item in records),
        "maximum_line_absolute_error_v": maximum_line_error,
        "maximum_vpp_absolute_error_v": maximum_vpp_error,
        "maximum_true_rms_absolute_error_v": maximum_rms_error,
        "maximum_intermod_residual_peak_v_p95": maximum_intermod,
        "total_outlier_count": sum(int(item["outlier_count"]) for item in records),
        "target_limit_v": arb.CALIBRATION_TARGET_V,
        "hard_limit_v": arb.CALIBRATION_HARD_LIMIT_V,
        "intermod_limit_vpeak": arb.INTERMOD_PEAK_LIMIT_V,
        "records": records,
        "failures": [],
        "pass": hard_pass,
        "target_pass": target_pass,
    }


CSV_FIELDS = (
    "case_id",
    "signal_class",
    "crest_variant",
    "crest_factor",
    "source_vpp_v",
    "maximum_line_absolute_error_v",
    "vpp_absolute_error_v",
    "true_rms_absolute_error_v",
    "intermod_residual_peak_v_p95",
    "outlier_count",
    "frame_count",
    "wave_packets",
    "target_pass",
    "hard_pass",
)


def _csv_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": record["case_id"],
        "signal_class": record["signal_class"],
        "crest_variant": record["crest_variant"],
        "crest_factor": record["crest_factor"],
        "source_vpp_v": record["source_vpp_v"],
        "maximum_line_absolute_error_v": record["maximum_line_absolute_error_v"],
        "vpp_absolute_error_v": record["vpp"]["absolute_error_v"],
        "true_rms_absolute_error_v": record["true_rms"]["absolute_error_v"],
        "intermod_residual_peak_v_p95": record["intermod_residual_peak_v_p95"],
        "outlier_count": record["outlier_count"],
        "frame_count": record["frame_count"],
        "wave_packets": record["wave_packets"],
        "target_pass": record["target_pass"],
        "hard_pass": record["hard_pass"],
    }


def write_summary(output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if output_dir.exists():
        raise MultitoneSummaryError(f"output already exists: {output_dir}")
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
        "target_pass": summary["target_pass"],
        "point_count": summary["point_count"],
        "frame_count": summary["frame_count"],
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
            f"M11_MULTITONE_SUMMARY_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
