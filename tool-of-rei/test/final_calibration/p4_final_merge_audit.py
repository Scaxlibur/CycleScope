#!/usr/bin/env python3
"""Merge and audit the split final calibrated-P4 M8/M9 evidence roots."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


EXPECTED_PROFILE = "C5DCDE41"
EXPECTED_M8 = (
    "M8-010K-H1",
    "M8-015K-H1",
    "M8-075K-H5",
    "M8-100K-H1",
    "M8-150K-H2",
    "M8-250K-H2",
    "M8-350K-H5",
    "M8-425K-H5",
    "M8-485K-H5",
    "M8-500K-H2",
)
EXPECTED_M9 = (
    "M9-UA-100MV-LOW",
    "M9-UA-250MV-HIGH",
    "M9-UB-50MV-LOW",
    "M9-UB-250MV-HIGH",
    "M9-CREST-LOW",
    "M9-CREST-HIGH",
    "M9-WEAK-H1-5MV",
    "M9-66K7-H1H3H5",
    "M9-UJ-BASE",
    "M9-UJ-1MHZ-200MVPP",
)
BASE_CASES = frozenset(EXPECTED_M8[:-1])
CONTINUATION_CASES = frozenset((EXPECTED_M8[-1], *EXPECTED_M9))
FATAL_UART_MARKERS = (
    "Guru Meditation",
    "Task watchdog got triggered",
    "assert failed",
    "Spectrum display projection rejected",
    "Waveform projection rejected",
    "FFT processing failed",
)
ERROR_FIELDS = (
    "fundamental_hz",
    "voltage_peak_to_peak_mV",
    "true_rms_mV",
    "line_frequency_hz",
    "line_amplitude_mVpk",
)


class AuditError(RuntimeError):
    """The split evidence cannot be accepted as one final campaign."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise AuditError(f"JSON root is not an object: {path}")
    return value


def verify_sha256s(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise AuditError(f"missing SHA256SUMS: {root}")
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as error:
            raise AuditError(f"bad SHA256SUMS line in {manifest}: {line!r}") from error
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise AuditError(f"bad SHA256 in {manifest}: {expected!r}")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise AuditError(f"SHA256SUMS path escapes root: {relative}") from error
        if not target.is_file() or sha256(target) != expected:
            raise AuditError(f"SHA256 mismatch: {target}")
        checked += 1
    if checked == 0:
        raise AuditError(f"empty SHA256SUMS: {manifest}")
    return {
        "root": str(root),
        "files_verified": checked,
        "manifest_sha256": sha256(manifest),
    }


def require_source_restore(summary: dict[str, Any], root: Path) -> None:
    restore = summary.get("source_restore")
    if not isinstance(restore, dict) or restore.get("pass") is not True:
        raise AuditError(f"source restore did not pass: {root}")
    if restore.get("dp800_writes") != 0 or restore.get("fpga_changes") is not False:
        raise AuditError(f"forbidden DP800/FPGA operation recorded: {root}")
    profile = restore.get("profile")
    if not isinstance(profile, dict) or profile.get("output") != "OFF":
        raise AuditError(f"DG output was not restored OFF: {root}")
    expected = restore.get("expected")
    required = {
        "function": "SIN",
        "frequency_hz": 100000.0,
        "amplitude_vpp": 0.05,
        "offset_v": 0.0,
        "load_ohm": 50.0,
        "output": "OFF",
    }
    if expected != required:
        raise AuditError(f"unexpected DG restore target: {root}")


def require_campaign_manifest(root: Path) -> dict[str, Any]:
    manifest = load_json(root / "campaign-manifest.json")
    profile = manifest.get("profile")
    required = {
        "normal_case_maximum_programmed_vpp": 0.25,
        "interference_case_maximum_programmed_vpp": 0.38,
        "user_measured_maximum_undistorted_vpp": 0.38,
        "dp800_operations": 0,
        "fpga_changes": False,
        "scope_channels": [2],
        "rtm_ch1_used": False,
    }
    for key, expected in required.items():
        if manifest.get(key) != expected:
            raise AuditError(f"campaign manifest {key} mismatch in {root}")
    if not isinstance(profile, dict) or profile.get("profile_id") != EXPECTED_PROFILE:
        raise AuditError(f"campaign profile mismatch in {root}")
    return manifest


def require_case(root: Path, result: dict[str, Any]) -> dict[str, Any]:
    case_id = result.get("case_id")
    if not isinstance(case_id, str) or result.get("pass") is not True:
        raise AuditError(f"campaign result is not an explicit PASS in {root}")
    case_dir = root / case_id
    case_result = load_json(case_dir / "case-result.json")
    acceptance = load_json(case_dir / "p4-final-acceptance.json")
    if case_result != result:
        raise AuditError(f"summary/case-result mismatch: {case_id}")
    if acceptance.get("pass") is not True or acceptance.get("failures") != []:
        raise AuditError(f"acceptance did not pass: {case_id}")
    p4 = result.get("p4")
    profile = acceptance.get("profile")
    if (
        not isinstance(p4, dict)
        or p4.get("profile") != EXPECTED_PROFILE
        or not isinstance(profile, dict)
        or profile.get("profile_id") != EXPECTED_PROFILE
    ):
        raise AuditError(f"P4 profile mismatch: {case_id}")
    if result.get("active_mirror_frames") != 256:
        raise AuditError(f"active mirror frame count is not 256: {case_id}")
    if int(result.get("complete_mirror_frames", 0)) < 256:
        raise AuditError(f"insufficient complete mirror frames: {case_id}")
    wavebench = result.get("wavebench_ch2")
    if (
        not isinstance(wavebench, dict)
        or wavebench.get("channels") != [2]
        or wavebench.get("rtm_ch1_used") is not False
        or wavebench.get("formal_reference") != "DG4202 CH1 50-ohm setting"
    ):
        raise AuditError(f"scope/reference boundary mismatch: {case_id}")
    uart_text = (case_dir / "p4-uart.log").read_text(
        encoding="utf-8", errors="replace"
    )
    fatal = [marker for marker in FATAL_UART_MARKERS if marker in uart_text]
    if fatal:
        raise AuditError(f"fatal UART marker in {case_id}: {fatal}")
    rows = acceptance.get("selected_p4_uart_rows")
    legacy_uart_schema = not isinstance(rows, list)
    if legacy_uart_schema:
        rows = [
            line
            for line in uart_text.splitlines()
            if "measurement:" in line
            and "p4cal=1" in line
            and f"profile={EXPECTED_PROFILE}" in line
        ]
    if not rows:
        raise AuditError(f"no P4CAL UART rows: {case_id}")
    return {
        "case_id": case_id,
        "root": str(root.resolve()),
        "case_result_sha256": sha256(case_dir / "case-result.json"),
        "acceptance_sha256": sha256(case_dir / "p4-final-acceptance.json"),
        "uart_sha256": sha256(case_dir / "p4-uart.log"),
        "audited_uart_rows": len(rows),
        "legacy_uart_schema": legacy_uart_schema,
        "active_mirror_frames": result["active_mirror_frames"],
        "complete_mirror_frames": result["complete_mirror_frames"],
    }


def require_campaign(
    root: Path,
    expected_cases: frozenset[str],
    require_pass: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    require_campaign_manifest(root)
    summary = load_json(root / "campaign-summary.json")
    require_source_restore(summary, root)
    if require_pass and summary.get("pass") is not True:
        raise AuditError(f"continuation campaign did not pass: {root}")
    results = summary.get("results")
    if not isinstance(results, list):
        raise AuditError(f"campaign results are malformed: {root}")
    by_id = {
        result.get("case_id"): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("case_id"), str)
    }
    if len(by_id) != len(results) or frozenset(by_id) != expected_cases:
        raise AuditError(f"campaign case set mismatch: {root}")
    records = [require_case(root, by_id[case_id]) for case_id in sorted(by_id)]
    return summary, list(by_id.values()), records


def absolute_errors(result: dict[str, Any]) -> dict[str, float]:
    target = result["target"]
    measured = result["p4"]
    errors = {
        "fundamental_hz": abs(measured["fundamental_hz"] - target["fundamental_hz"]),
        "voltage_peak_to_peak_mV": abs(
            measured["voltage_peak_to_peak_mV"] - target["voltage_peak_to_peak_mV"]
        ),
        "true_rms_mV": abs(measured["true_rms_mV"] - target["true_rms_mV"]),
        "line_frequency_hz": 0.0,
        "line_amplitude_mVpk": 0.0,
    }
    target_lines = {line["order"]: line for line in target["lines"]}
    measured_lines = {line["order"]: line for line in measured["lines"]}
    if target_lines.keys() != measured_lines.keys():
        raise AuditError(f"line-order mismatch: {result['case_id']}")
    for order, expected in target_lines.items():
        actual = measured_lines[order]
        errors["line_frequency_hz"] = max(
            errors["line_frequency_hz"],
            abs(actual["frequency_hz"] - expected["frequency_hz"]),
        )
        errors["line_amplitude_mVpk"] = max(
            errors["line_amplitude_mVpk"],
            abs(actual["amplitude_mVpk"] - expected["amplitude_mVpk"]),
        )
    return errors


def require_watchdog_ab(failure_root: Path, recheck_root: Path) -> dict[str, Any]:
    failure_audit = verify_sha256s(failure_root)
    recheck_audit = verify_sha256s(recheck_root)
    failed = load_json(
        failure_root / "M9-UA-250MV-HIGH" / "p4-final-acceptance.json"
    )
    if failed.get("pass") is not False or not any(
        "Task watchdog got triggered" in str(item)
        for item in failed.get("failures", [])
    ):
        raise AuditError("watchdog failure evidence is not the expected negative")
    recheck = load_json(recheck_root / "campaign-summary.json")
    if recheck.get("pass") is not True or recheck.get("case_count") != 1:
        raise AuditError("watchdog recheck campaign did not pass")
    uart = recheck_root / "M9-UA-250MV-HIGH" / "p4-uart.log"
    uart_text = uart.read_text(encoding="utf-8", errors="replace")
    if any(marker in uart_text for marker in FATAL_UART_MARKERS):
        raise AuditError("watchdog marker remains in recheck UART")
    return {
        "pass": True,
        "negative_manifest_sha256": failure_audit["manifest_sha256"],
        "negative_acceptance_sha256": sha256(
            failure_root / "M9-UA-250MV-HIGH" / "p4-final-acceptance.json"
        ),
        "positive_manifest_sha256": recheck_audit["manifest_sha256"],
        "positive_uart_sha256": sha256(uart),
    }


def strict_reaccept_base_cases(
    base_root: Path, frozen_root: Path
) -> dict[str, dict[str, Any]]:
    required = {
        "fit": frozen_root / "fit-v2",
        "holdout": frozen_root / "holdout-v2",
        "asset": frozen_root / "p4-asset-v2",
    }
    if any(not path.is_dir() for path in required.values()):
        raise AuditError(f"frozen profile directories are incomplete: {frozen_root}")
    acceptance_script = Path(__file__).with_name("p4_final_acceptance.py")
    outputs: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="cyclescope-m8-reaccept-") as temporary:
        temporary_root = Path(temporary)
        for case_id in EXPECTED_M8[:-1]:
            case_dir = base_root / case_id
            output = temporary_root / f"{case_id}.json"
            command = [
                sys.executable,
                str(acceptance_script),
                "--manifest",
                str(case_dir / "manifest" / "manifest.json"),
                "--mirror-dir",
                str(case_dir / "mirror"),
                "--uart-log",
                str(case_dir / "p4-uart.log"),
                "--fit-dir",
                str(required["fit"]),
                "--holdout-dir",
                str(required["holdout"]),
                "--asset-dir",
                str(required["asset"]),
                "--maximum-programmed-vpp",
                "0.25",
                "--output",
                str(output),
            ]
            completed = subprocess.run(
                command, check=False, text=True, capture_output=True
            )
            if completed.returncode != 0:
                raise AuditError(
                    f"strict reaccept failed for {case_id}: {completed.stderr or completed.stdout}"
                )
            payload = load_json(output)
            rows = payload.get("selected_p4_uart_rows")
            if (
                payload.get("pass") is not True
                or payload.get("failures") != []
                or not isinstance(rows, list)
                or not rows
            ):
                raise AuditError(f"strict reaccept is incomplete for {case_id}")
            outputs[case_id] = {
                "payload": payload,
                "text": output.read_text(encoding="utf-8"),
                "sha256": sha256(output),
                "uart_rows": len(rows),
            }
    return outputs


def copy_build_artifacts(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    artifacts_dir = output / "build-artifacts"
    artifacts_dir.mkdir(parents=True)
    sources = {
        "CycleScopeP4.bin": args.app_bin,
        "CycleScopeP4-readback.bin": args.app_readback,
        "CycleScopeP4.elf": args.elf,
        "sdkconfig": args.sdkconfig,
        "CMakeCache.txt": args.cmake_cache,
    }
    for name, source in sources.items():
        if not source.is_file():
            raise AuditError(f"missing build artifact: {source}")
        shutil.copy2(source, artifacts_dir / name)
    app = artifacts_dir / "CycleScopeP4.bin"
    readback = artifacts_dir / "CycleScopeP4-readback.bin"
    if app.read_bytes() != readback.read_bytes():
        raise AuditError("P4 app Flash readback differs from the built BIN")
    sdkconfig = (artifacts_dir / "sdkconfig").read_text()
    cache = (artifacts_dir / "CMakeCache.txt").read_text()
    required_sdkconfig = (
        'CONFIG_CYCLESCOPE_CSLP_PEER_IPV4="192.168.10.2"',
        "# CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER is not set",
        "# CONFIG_LV_USE_PERF_MONITOR is not set",
    )
    if not all(item in sdkconfig for item in required_sdkconfig):
        raise AuditError("formal sdkconfig boundary mismatch")
    if "CYCLESCOPE_LOCAL_TEST_CMAKE:FILEPATH=" not in cache:
        raise AuditError("local test CMake entry is not empty")
    return {
        "app_size_bytes": app.stat().st_size,
        "app_bin_sha256": sha256(app),
        "app_readback_sha256": sha256(readback),
        "app_readback_identical": True,
        "elf_sha256": sha256(artifacts_dir / "CycleScopeP4.elf"),
        "sdkconfig_sha256": sha256(artifacts_dir / "sdkconfig"),
        "cmake_cache_sha256": sha256(artifacts_dir / "CMakeCache.txt"),
        "peer_ipv4": "192.168.10.2",
        "local_test_cmake": "",
        "lvgl_perf_monitor": False,
    }


def write_sha256s(root: Path) -> str:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            entries.append(f"{sha256(path)}  {path.relative_to(root)}")
    manifest = root / "SHA256SUMS"
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return sha256(manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--continuation-root", type=Path, required=True)
    parser.add_argument("--watchdog-failure-root", type=Path, required=True)
    parser.add_argument("--watchdog-recheck-root", type=Path, required=True)
    parser.add_argument("--frozen-root", type=Path, required=True)
    parser.add_argument("--app-bin", type=Path, required=True)
    parser.add_argument("--app-readback", type=Path, required=True)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--sdkconfig", type=Path, required=True)
    parser.add_argument("--cmake-cache", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("--output-dir must not already exist")

    base_audit = verify_sha256s(args.base_root)
    continuation_audit = verify_sha256s(args.continuation_root)
    base_summary, base_results, base_records = require_campaign(
        args.base_root, BASE_CASES, require_pass=False
    )
    if base_summary.get("pass") is not False or "M8-500K-H2" not in str(
        base_summary.get("error")
    ):
        raise AuditError("base root is not the expected fail-closed 500 kHz stop")
    continuation_summary, continuation_results, continuation_records = require_campaign(
        args.continuation_root, CONTINUATION_CASES, require_pass=True
    )
    all_results = base_results + continuation_results
    by_id = {result["case_id"]: result for result in all_results}
    expected_all = frozenset((*EXPECTED_M8, *EXPECTED_M9))
    if len(by_id) != 20 or frozenset(by_id) != expected_all:
        raise AuditError("combined M8/M9 case set is not exactly 20 unique cases")

    maxima = {field: 0.0 for field in ERROR_FIELDS}
    for result in all_results:
        if result["stage"] != result["case_id"][0:2].lower():
            raise AuditError(f"stage mismatch: {result['case_id']}")
        if result["case_id"] != "M9-UJ-1MHZ-200MVPP" and result["programmed_vpp"] > 0.25:
            raise AuditError(f"normal case exceeds 250 mVpp: {result['case_id']}")
        errors = absolute_errors(result)
        for field in ERROR_FIELDS:
            maxima[field] = max(maxima[field], errors[field])

    interference = continuation_summary.get("interference_pair")
    if (
        not isinstance(interference, dict)
        or interference.get("pass") is not True
        or interference.get("user_measured_undistorted_limit_vpp") != 0.38
    ):
        raise AuditError("1 MHz interference pair did not pass the 380 mVpp boundary")
    interference_case = by_id["M9-UJ-1MHZ-200MVPP"]
    if not 0.25 < interference_case["programmed_vpp"] < 0.38:
        raise AuditError("interference envelope is outside the approved exception")

    strict_reanalysis = strict_reaccept_base_cases(args.base_root, args.frozen_root)
    watchdog = require_watchdog_ab(
        args.watchdog_failure_root, args.watchdog_recheck_root
    )
    args.output_dir.mkdir(parents=True)
    reanalysis_dir = args.output_dir / "base-m8-strict-reanalysis"
    reanalysis_dir.mkdir()
    for case_id, result in strict_reanalysis.items():
        (reanalysis_dir / f"{case_id}.json").write_text(
            result["text"], encoding="utf-8"
        )
    build = copy_build_artifacts(args, args.output_dir)
    git_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[3],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    old_500 = args.base_root / "M8-500K-H2" / "p4-final-acceptance.json"
    payload = {
        "format": "CycleScope final calibrated P4 M8/M9 combined audit v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "pass": True,
        "branch": "main",
        "git_head_before_final_commit": git_head,
        "profile_id": EXPECTED_PROFILE,
        "case_count": 20,
        "stage_counts": {"m8": 10, "m9": 10},
        "case_ids": [*EXPECTED_M8, *EXPECTED_M9],
        "maximum_absolute_errors": maxima,
        "interference_pair": interference,
        "input_boundaries": {
            "formal_normal_maximum_vpp": 0.25,
            "user_measured_maximum_undistorted_vpp": 0.38,
            "interference_programmed_envelope_vpp": interference_case["programmed_vpp"],
            "rtm_ch1_used": False,
            "formal_reference": "DG4202 CH1 50-ohm setting",
        },
        "selected_evidence_roots": {
            "base_first_nine_m8": base_audit,
            "continuation_500k_and_m9": continuation_audit,
        },
        "superseded_negative": {
            "case_id": "M8-500K-H2",
            "reason": "pre-fix response discontinuity above 500 kHz",
            "acceptance_sha256": sha256(old_500),
            "selected_for_final_result": False,
        },
        "watchdog_ab": watchdog,
        "base_m8_strict_reanalysis": {
            case_id: {
                "pass": True,
                "sha256": result["sha256"],
                "audited_uart_rows": result["uart_rows"],
            }
            for case_id, result in strict_reanalysis.items()
        },
        "build_and_flash": build,
        "operations": {
            "dp800_writes": 0,
            "fpga_changes": False,
            "dg_final_output": "OFF",
        },
        "case_evidence": base_records + continuation_records,
    }
    audit_path = args.output_dir / "combined-audit.json"
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_sha = write_sha256s(args.output_dir)
    print(
        json.dumps(
            {
                "pass": True,
                "case_count": 20,
                "maximum_absolute_errors": maxima,
                "combined_audit_sha256": sha256(audit_path),
                "sha256s_manifest_sha256": manifest_sha,
                "output_dir": str(args.output_dir.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
