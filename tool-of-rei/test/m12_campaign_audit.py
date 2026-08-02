#!/usr/bin/env python3
"""Read-only completion audit for the P4-scoped M12 evidence campaign."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


MINIMUM_FRAMES = 64
FREQUENCY_TOLERANCE_HZ = 1000.0
AMPLITUDE_TOLERANCE_MV = 5.0
TIME_VIEW_VPP_TOLERANCE_MV = 1.0
TIME_VIEW_RMS_RESIDUAL_TOLERANCE_MV = 0.5
TIME_VIEW_PEAK_RESIDUAL_TOLERANCE_MV = 1.0


class AuditError(RuntimeError):
    """Evidence cannot support an M12 completion claim."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read {path}: {error}") from error
    if not isinstance(content, dict):
        raise AuditError(f"{path} does not contain a JSON object")
    return content


def expected_case_ids() -> set[str]:
    expected = {f"MASK-H{order:02d}" for order in range(2, 17)}
    expected.update(f"ORDER-400K-H{order:02d}" for order in range(2, 17))
    expected.update(
        f"PHASE-H2-{h2:03d}-H3-{h3:03d}"
        for h2 in (0, 90, 180, 270)
        for h3 in (0, 90, 180, 270)
    )
    expected.update(
        {
            "UA-CREST-LOW",
            "UA-CREST-HIGH",
            "UA-BASE-WEAK",
            "UB-LOW",
            "UB-HIGH",
            "UB-H16",
            "UB-WEAK",
            "J-H10-1M-BASE",
            "J-H10-1M",
            "J-H16-1M6-BASE",
            "J-H16-1M6",
            "J-H16-2M-BASE",
            "J-H16-2M",
        }
    )
    return expected


def manifest_for_case(case_directory: Path, report: dict[str, Any]) -> dict[str, Any]:
    local = case_directory / "manifest" / "manifest.json"
    if local.is_file():
        return read_json(local)
    recorded = report.get("manifest")
    if isinstance(recorded, str):
        candidate = Path(recorded)
        if candidate.is_file():
            return read_json(candidate)
    raise AuditError(f"cannot locate manifest for {case_directory}")


def median(block: dict[str, Any], name: str) -> float:
    try:
        result = float(block[name]["median"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuditError(f"missing median {name}") from error
    if not math.isfinite(result):
        raise AuditError(f"non-finite median {name}")
    return result


def read_source_profile(case_directory: Path) -> list[str]:
    candidate = case_directory / "source-after.txt"
    if not candidate.is_file():
        return ["missing source-after.txt"]
    text = candidate.read_text(encoding="utf-8", errors="replace")
    first = next((line for line in text.splitlines() if line.startswith("CH1:")), "")
    failures: list[str] = []
    if "output=OFF" not in first:
        failures.append("DG source-after is not OFF")
    if "load_ohm=50" not in text:
        failures.append("DG source-after is not 50 ohm")
    if "offset=0.0V" not in first:
        failures.append("DG source-after offset is not 0 V")
    return failures


def audit_case(case_directory: Path) -> tuple[str, dict[str, Any], list[str], dict[str, float]]:
    report = read_json(case_directory / "p4-acceptance.json")
    manifest = manifest_for_case(case_directory, report)
    case_id = str(manifest.get("case_id", case_directory.name))
    failures: list[str] = []
    if report.get("pass") is not True:
        failures.extend(str(item) for item in report.get("failures", ["P4 acceptance did not pass"]))
    mirror_dir = case_directory / "mirror"
    summary = read_json(mirror_dir / "summary.json")
    if summary.get("network_writes") != 0:
        failures.append("mirror capture reported network writes")
    if int(summary.get("complete_frames", 0)) < MINIMUM_FRAMES:
        failures.append("mirror complete frame count is below 64")
    source_on_frames = int(
        report.get("selected_mirror_frames", {}).get("source_on_complete_frames", 0)
    )
    if source_on_frames < MINIMUM_FRAMES:
        failures.append("source-ON complete frame count is below 64")
    for key, value in summary.get("counts", {}).items():
        if key != "accepted_datagrams" and int(value) != 0:
            failures.append(f"mirror count {key} is non-zero")
    failures.extend(read_source_profile(case_directory))

    target = report.get("target", {})
    p4 = report.get("p4", {})
    mirror = report.get("mirror", {})
    try:
        error_fields = {
            "f0_hz": abs(median(p4, "fundamental_hz") - float(target["fundamental_hz"])),
            "vpp_mV": abs(
                median(p4, "voltage_peak_to_peak_mV")
                - float(target["voltage_peak_to_peak_mV"])
            ),
            "rms_mV": abs(
                median(p4, "true_rms_mV") - float(target["true_rms_mV"])
            ),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise AuditError(f"{case_id} lacks target/P4 scalar fields: {error}") from error
    if error_fields["f0_hz"] > FREQUENCY_TOLERANCE_HZ:
        failures.append("P4 F0 exceeds 1 kHz")
    if error_fields["vpp_mV"] > AMPLITUDE_TOLERANCE_MV:
        failures.append("P4 reconstructed Vpp exceeds 5 mV")
    if error_fields["rms_mV"] > AMPLITUDE_TOLERANCE_MV:
        failures.append("P4 reconstructed RMS exceeds 5 mV")

    target_lines = target.get("lines")
    p4_lines = p4.get("lines")
    mirror_lines = mirror.get("lines")
    if not isinstance(target_lines, list) or not isinstance(p4_lines, list):
        raise AuditError(f"{case_id} lacks line arrays")
    if len(target_lines) != len(p4_lines):
        failures.append("P4 target line count differs from manifest")
    line_frequency_errors: list[float] = []
    line_amplitude_errors: list[float] = []
    for observed, expected in zip(p4_lines, target_lines, strict=False):
        if observed.get("order") != expected.get("order"):
            failures.append("P4 harmonic order differs from manifest")
            continue
        frequency_error = abs(median(observed, "frequency_hz") - float(expected["frequency_hz"]))
        amplitude_error = abs(median(observed, "amplitude_mVpk") - float(expected["amplitude_mVpk"]))
        line_frequency_errors.append(frequency_error)
        line_amplitude_errors.append(amplitude_error)
        if frequency_error > FREQUENCY_TOLERANCE_HZ:
            failures.append(f"P4 H{expected['order']} frequency exceeds 1 kHz")
        if amplitude_error > AMPLITUDE_TOLERANCE_MV:
            failures.append(f"P4 H{expected['order']} amplitude exceeds 5 mV")
    if len(mirror_lines) != len(target_lines):
        failures.append("mirror selected line count differs from manifest")

    time_view = report.get("p4_time_view_from_mirror", {})
    try:
        raw_vpp = median(time_view, "raw_three_period_vpp_mV")
        model_vpp = median(time_view, "fft_model_three_period_vpp_mV")
        residual_rms = median(time_view, "residual_rms_mV")
        residual_peak = median(time_view, "residual_peak_mV")
    except AuditError as error:
        failures.append(str(error))
        raw_vpp = model_vpp = residual_rms = residual_peak = math.inf
    if abs(raw_vpp - model_vpp) > TIME_VIEW_VPP_TOLERANCE_MV:
        failures.append("raw P4 3P envelope differs from FFT model by more than 1 mV")
    if residual_rms > TIME_VIEW_RMS_RESIDUAL_TOLERANCE_MV:
        failures.append("raw P4 3P residual RMS exceeds 0.5 mV")
    if residual_peak > TIME_VIEW_PEAK_RESIDUAL_TOLERANCE_MV:
        failures.append("raw P4 3P residual peak exceeds 1 mV")

    source_tones = manifest.get("source_tones", manifest.get("tones", []))
    analysis_tones = manifest.get("tones", [])
    if not isinstance(source_tones, list) or not isinstance(analysis_tones, list):
        failures.append("manifest source/analysis tone arrays are malformed")
    if case_id.startswith("J-"):
        if case_id.endswith("-BASE"):
            if len(source_tones) != len(analysis_tones):
                failures.append("u_b baseline has unexpected source-only tone")
        elif len(source_tones) != len(analysis_tones) + 1:
            failures.append("u_J case does not isolate exactly one source-only interferer")

    values = {
        **error_fields,
        "line_frequency_hz": max(line_frequency_errors, default=math.inf),
        "line_amplitude_mV": max(line_amplitude_errors, default=math.inf),
        "raw_model_vpp_delta_mV": abs(raw_vpp - model_vpp),
        "raw_residual_rms_mV": residual_rms,
        "raw_residual_peak_mV": residual_peak,
    }
    return case_id, manifest, failures, values


def audit_interference_pairs(cases: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    pairs = {
        "J-H10-1M": "J-H10-1M-BASE",
        "J-H16-1M6": "J-H16-1M6-BASE",
        "J-H16-2M": "J-H16-2M-BASE",
    }
    failures: list[str] = []
    summary: dict[str, Any] = {}
    for interference, baseline in pairs.items():
        if interference not in cases or baseline not in cases:
            failures.append(f"missing interference pair {baseline}/{interference}")
            continue
        base = cases[baseline]["report"]
        test = cases[interference]["report"]
        target_base = base["target"]
        target_test = test["target"]
        if target_base != target_test:
            failures.append(f"{interference} target u_b differs from its baseline")
        p4_base = base["p4"]
        p4_test = test["p4"]
        drift = {
            "f0_hz": abs(median(p4_test, "fundamental_hz") - median(p4_base, "fundamental_hz")),
            "vpp_mV": abs(
                median(p4_test, "voltage_peak_to_peak_mV")
                - median(p4_base, "voltage_peak_to_peak_mV")
            ),
            "rms_mV": abs(
                median(p4_test, "true_rms_mV") - median(p4_base, "true_rms_mV")
            ),
            "lines": [],
        }
        for observed, reference in zip(p4_test["lines"], p4_base["lines"], strict=False):
            if observed["order"] != reference["order"]:
                failures.append(f"{interference} changed a u_b harmonic order")
                continue
            item = {
                "order": observed["order"],
                "frequency_hz": abs(
                    median(observed, "frequency_hz") - median(reference, "frequency_hz")
                ),
                "amplitude_mVpk": abs(
                    median(observed, "amplitude_mVpk") - median(reference, "amplitude_mVpk")
                ),
            }
            drift["lines"].append(item)
        if drift["f0_hz"] > FREQUENCY_TOLERANCE_HZ:
            failures.append(f"{interference} u_b F0 drift exceeds 1 kHz")
        for field in ("vpp_mV", "rms_mV"):
            if drift[field] > AMPLITUDE_TOLERANCE_MV:
                failures.append(f"{interference} u_b {field} drift exceeds 5 mV")
        for item in drift["lines"]:
            if item["frequency_hz"] > FREQUENCY_TOLERANCE_HZ:
                failures.append(f"{interference} H{item['order']} frequency drift exceeds 1 kHz")
            if item["amplitude_mVpk"] > AMPLITUDE_TOLERANCE_MV:
                failures.append(f"{interference} H{item['order']} amplitude drift exceeds 5 mV")
        summary[interference] = drift
    return summary, failures


def final_source_failures(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    line = next((line for line in text.splitlines() if line.startswith("CH1:")), "")
    expected = ("output=OFF", "func=SIN", "freq=100000.0Hz", "amp=0.05VPP", "offset=0.0V")
    failures = [f"final DG profile missing {token}" for token in expected if token not in line]
    if "load_ohm=50" not in text:
        failures.append("final DG profile is not 50 ohm")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, action="append", required=True)
    parser.add_argument("--extra-case-dir", type=Path, action="append", default=[])
    parser.add_argument("--final-source-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    case_directories: list[Path] = []
    campaign_failures: list[str] = []
    for directory in args.campaign_dir:
        campaign = read_json(directory / "campaign-manifest.json")
        if campaign.get("dp800_operations") != 0:
            campaign_failures.append(f"{directory}: DP800 operation count is non-zero")
        if campaign.get("raw_scpi_entrypoint_used") is not False:
            campaign_failures.append(f"{directory}: raw SCPI entrypoint was used")
        case_directories.extend(sorted(path.parent for path in directory.rglob("p4-acceptance.json")))
    case_directories.extend(args.extra_case_dir)

    cases: dict[str, dict[str, Any]] = {}
    all_failures = list(campaign_failures)
    maxima: dict[str, float] = {}
    for directory in case_directories:
        try:
            case_id, manifest, failures, values = audit_case(directory)
            report = read_json(directory / "p4-acceptance.json")
        except AuditError as error:
            all_failures.append(f"{directory}: {error}")
            continue
        if case_id in cases:
            all_failures.append(f"duplicate case evidence for {case_id}")
            continue
        cases[case_id] = {
            "directory": str(directory),
            "manifest": manifest,
            "report": report,
            "failures": failures,
            "values": values,
        }
        all_failures.extend(f"{case_id}: {failure}" for failure in failures)
        for key, value in values.items():
            maxima[key] = max(maxima.get(key, -math.inf), value)

    expected = expected_case_ids()
    observed = set(cases)
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected - {"M12-H1H3H5-66K7-TARGET"})
    if missing:
        all_failures.append("missing planned cases: " + ", ".join(missing))
    if unexpected:
        all_failures.append("unexpected case ids: " + ", ".join(unexpected))
    pair_summary, pair_failures = audit_interference_pairs(cases)
    all_failures.extend(pair_failures)
    if not args.final_source_profile.is_file():
        all_failures.append("final source profile evidence is missing")
    else:
        all_failures.extend(final_source_failures(args.final_source_profile))

    payload = {
        "format": "CycleScope M12 P4-only completion audit v1",
        "pass": not all_failures,
        "planned_case_count": len(expected),
        "observed_case_count": len(observed),
        "reference_case_present": "M12-H1H3H5-66K7-TARGET" in observed,
        "missing_cases": missing,
        "unexpected_cases": unexpected,
        "max_abs_error": maxima,
        "interference_pair_drift": pair_summary,
        "failures": all_failures,
        "cases": {
            case_id: {
                "directory": item["directory"],
                "failures": item["failures"],
                "values": item["values"],
            }
            for case_id, item in sorted(cases.items())
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
