#!/usr/bin/env python3
"""Resume-aware, no-retry M11-I campaign coordinator.

Passing point evidence is immutable and skipped.  A failed, incomplete, or
ambiguous attempt blocks live continuation so instrument writes are never
retried blindly.  The SIN-to-USER transition is a separate OFF-only ARB prime,
which lets both formal I combination points use the required USER-to-USER
repeat-upload path.
"""

# ruff: noqa: E402 -- sibling M11 modules establish the WaveBench source path.

from __future__ import annotations

import argparse
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
import m11_sine_point as sine
import m11_upper_frequency_summary as upper
import m11_wavebench_safe as safety

from wavebench.logging import CommandLogger
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService
from wavebench.services.source_service import SourceService


POINTS_ROOT = safety.EVIDENCE_ROOT / "points"
SUMMARY_DIR = safety.EVIDENCE_ROOT / "offline" / "upper-frequency-summary-v1"
CALIBRATION_MANIFEST = upper.CALIBRATION_MANIFEST
I_CAMPAIGN_ACK = "M11_STAGE_I_ONLY_MISSING_POINTS_NO_AUTOMATIC_RETRY"
AUTHORIZED_PREOUTPUT_NONPOINT = (
    "20260801_013931_534999+0800_i-formal-4e+06Hz"
)


class M11ICampaignError(RuntimeError):
    """M11-I cannot continue without risking duplicate or blind live writes."""


def ordered_cases() -> list[dict[str, Any]]:
    sine_cases, arb_cases = upper.i_cases()
    ordered_sine = sorted(sine_cases, key=lambda item: float(item["frequency_hz"]))
    ordered_arb = sorted(arb_cases, key=lambda item: float(item["u_j_frequency_hz"]))
    return [*ordered_sine, *ordered_arb]


def _attempt_directories(points_root: Path, case_id: str) -> list[Path]:
    return sorted(
        candidate.resolve()
        for candidate in points_root.glob(f"*_{case_id}")
        if candidate.is_dir() and candidate.name.endswith(f"_{case_id}")
    )


def _verify_source_archive(point_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    lan = payload.get("lan")
    if not isinstance(lan, dict) or lan.get("pass") is not True:
        raise M11ICampaignError(f"{point_dir.name}: LAN evidence is not passing")
    archive = lan.get("packet_archive")
    if not isinstance(archive, dict) or archive.get("pass") is not True:
        raise M11ICampaignError(f"{point_dir.name}: source_data archive is not passing")
    directory = Path(str(archive.get("directory", ""))).resolve()
    try:
        directory.relative_to(safety.SOURCE_DATA_ROOT.resolve())
    except ValueError as error:
        raise M11ICampaignError(
            f"{point_dir.name}: source_data archive escapes its root"
        ) from error
    verification = calibration.verify_sha256sums(directory)
    wire = directory / "wire.pcap"
    expected_hash = archive.get("wire_pcap_sha256")
    if (
        not wire.is_file()
        or wire.stat().st_size <= 24
        or expected_hash != safety.sha256_file(wire)
        or archive.get("copy_verified_by_size_and_sha256") is not True
    ):
        raise M11ICampaignError(f"{point_dir.name}: copied wire.pcap binding failed")
    return {
        "directory": str(directory),
        "sha256sums": verification,
        "wire_pcap": str(wire),
        "wire_pcap_sha256": expected_hash,
        "pass": True,
    }


def inspect_attempt(point_dir: Path, case_id: str) -> dict[str, Any]:
    point_path = point_dir / "point.json"
    analysis_path = point_dir / "analysis.json"
    if not point_path.is_file() or not analysis_path.is_file():
        attempt_path = point_dir / "attempt.json"
        if attempt_path.is_file():
            try:
                payload = sine.load_json(attempt_path)
                verification = sine._verify_point_sha256sums(point_dir)
            except Exception as error:
                return {
                    "directory": str(point_dir),
                    "state": "invalid",
                    "reason": f"{type(error).__name__}: {error}",
                }
            if payload.get("case_id") != case_id:
                return {
                    "directory": str(point_dir),
                    "state": "invalid",
                    "reason": "attempt marker case identity mismatch",
                }
            if payload.get("disposition") == "authorized_pre_output_non_point":
                failures = _validate_authorized_preoutput_nonpoint(
                    point_dir,
                    payload,
                )
                return {
                    "directory": str(point_dir),
                    "state": "audited_nonpoint" if not failures else "invalid",
                    "reason": (
                        "user-authorized pre-output configuration failure is not an I point"
                        if not failures
                        else "; ".join(failures)
                    ),
                    "sha256sums": verification,
                }
            return {
                "directory": str(point_dir),
                "state": "failed",
                "reason": str(
                    payload.get(
                        "failure_phase",
                        "recorded failed attempt without passing point evidence",
                    )
                ),
                "sha256sums": verification,
            }
        return {
            "directory": str(point_dir),
            "state": "incomplete",
            "reason": "point.json or analysis.json is missing",
        }
    try:
        payload = sine.load_json(point_path)
        analysis = sine.load_json(analysis_path)
    except Exception as error:
        return {
            "directory": str(point_dir),
            "state": "invalid",
            "reason": f"{type(error).__name__}: {error}",
        }
    observed_case = payload.get("case", {}).get("case_id")
    analysis_case = analysis.get("case_id")
    if observed_case != case_id or analysis_case != case_id:
        return {
            "directory": str(point_dir),
            "state": "invalid",
            "reason": "case identity mismatch",
        }
    validated_analysis = None
    if payload.get("pass") is not True or analysis.get("pass") is not True:
        try:
            validated_analysis = upper.validated_sine_analysis(point_dir, case_id)
        except Exception as error:
            return {
                "directory": str(point_dir),
                "state": "failed",
                "reason": (
                    "recorded point or analysis did not pass; offline correction unavailable: "
                    f"{type(error).__name__}: {error}"
                ),
            }
    try:
        point_verification = sine._verify_point_sha256sums(point_dir)
        source_archive = _verify_source_archive(point_dir, payload)
    except Exception as error:
        return {
            "directory": str(point_dir),
            "state": "invalid",
            "reason": f"{type(error).__name__}: {error}",
        }
    return {
        "directory": str(point_dir),
        "state": "pass",
        "point_sha256sums": point_verification,
        "source_archive": source_archive,
        "validated_analysis": (
            None
            if validated_analysis is None
            else {
                key: value
                for key, value in validated_analysis.items()
                if key != "analysis"
            }
        ),
    }


def _validate_authorized_preoutput_nonpoint(
    point_dir: Path,
    payload: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if point_dir.name != AUTHORIZED_PREOUTPUT_NONPOINT:
        failures.append("pre-output non-point authorization applies to one frozen directory")
    if payload.get("format") != "CycleScope M11 audited pre-output non-point v1":
        failures.append("pre-output non-point marker format changed")
    required_false = (
        "source_output_ever_enabled_by_point",
        "scope_acquisition_started",
        "formal_lan_acquisition_started",
        "point_measurement_started",
        "dp832_writes",
    )
    if any(payload.get(field) is not False for field in required_false):
        failures.append("pre-output non-point marker claims a forbidden live action")
    if payload.get("automatic_retry_authorized") is not True:
        failures.append("pre-output non-point lacks the one-time retry authorization")
    waiver = sine.load_user_restoration_waiver()
    waiver_record = payload.get("user_waiver", {})
    if (
        waiver_record.get("path") != waiver["path"]
        or waiver_record.get("sha256") != waiver["sha256"]
    ):
        failures.append("pre-output non-point waiver binding changed")

    relative_run = payload.get("archived_run_json")
    if not isinstance(relative_run, str):
        failures.append("pre-output non-point archived run binding is missing")
        return failures
    run_path = (point_dir / relative_run).resolve()
    try:
        run_path.relative_to(point_dir.resolve())
    except ValueError:
        failures.append("pre-output non-point archived run escapes point directory")
        return failures
    if not run_path.is_file() or payload.get("archived_run_json_sha256") != safety.sha256_file(
        run_path
    ):
        failures.append("pre-output non-point archived run SHA-256 failed")
        return failures
    try:
        run = sine.load_json(run_path)
    except Exception as error:
        failures.append(f"archived failed run is invalid: {type(error).__name__}: {error}")
        return failures
    kinds = [step.get("kind") for step in run.get("steps", [])]
    if kinds != ["source.status", "power.status", "source.output"]:
        failures.append("archived failed run crossed the pre-output boundary")
    if run.get("status") != "failed" or run.get("error", {}).get("message") != (
        "DG4000 fixed-wave transactions require a restorable basic function snapshot"
    ):
        failures.append("archived failed run error identity changed")
    source_steps = [
        step for step in run.get("steps", []) if str(step.get("kind", "")).startswith("source.")
    ]
    for step in source_steps:
        status = step.get("artifact", {}).get("source_status", {})
        if status.get("output") != "OFF":
            failures.append("archived failed run does not prove DG remained OFF")
            break
    return failures


def classify_attempts(case_id: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    passing = [item for item in attempts if item.get("state") == "pass"]
    audited_nonpoints = [
        item for item in attempts if item.get("state") == "audited_nonpoint"
    ]
    nonpassing = [
        item
        for item in attempts
        if item.get("state") not in {"pass", "audited_nonpoint"}
    ]
    failures: list[str] = []
    if len(passing) > 1:
        failures.append(f"{case_id}: multiple passing attempts are ambiguous")
    elif len(audited_nonpoints) > 1:
        failures.append(f"{case_id}: multiple pre-output non-points are ambiguous")
    elif not passing and nonpassing:
        failures.append(
            f"{case_id}: an existing failed/incomplete attempt requires manual review; "
            "automatic retry is forbidden"
        )
    state = "done" if len(passing) == 1 else "blocked" if failures else "pending"
    return {
        "case_id": case_id,
        "state": state,
        "passing_attempt": None if len(passing) != 1 else passing[0]["directory"],
        "audited_nonpoints": audited_nonpoints,
        "attempts": attempts,
        "failures": failures,
    }


def summary_status(summary_dir: Path = SUMMARY_DIR) -> dict[str, Any]:
    if not summary_dir.exists():
        return {"state": "pending", "directory": str(summary_dir), "pass": False}
    try:
        verification = calibration.verify_sha256sums(summary_dir)
        payload = sine.load_json(summary_dir / "summary.json")
        failures = []
        if payload.get("pass") is not True or payload.get("target_pass") is not True:
            failures.append("I summary hard/target acceptance is not fully passing")
        if (
            int(payload.get("point_count", -1)) != 7
            or int(payload.get("sine_point_count", -1)) != 5
            or int(payload.get("arb_point_count", -1)) != 2
        ):
            failures.append("I summary point counts changed")
        return {
            "state": "pass" if not failures else "invalid",
            "directory": str(summary_dir.resolve()),
            "verification": verification,
            "failures": failures,
            "pass": not failures,
        }
    except Exception as error:
        return {
            "state": "invalid",
            "directory": str(summary_dir.resolve()),
            "failures": [f"{type(error).__name__}: {error}"],
            "pass": False,
        }


def campaign_status(points_root: Path = POINTS_ROOT) -> dict[str, Any]:
    records = ordered_cases()
    cases: list[dict[str, Any]] = []
    for record in records:
        case_id = str(record["case_id"])
        attempts = [
            inspect_attempt(path, case_id)
            for path in _attempt_directories(points_root, case_id)
        ]
        classified = classify_attempts(case_id, attempts)
        classified["kind"] = str(record["kind"])
        classified["minimum_frames"] = int(record["minimum_frames"])
        cases.append(classified)
    failures = [failure for item in cases for failure in item["failures"]]
    pending = [item for item in cases if item["state"] == "pending"]
    done = [item for item in cases if item["state"] == "done"]
    summary = summary_status()
    if summary["state"] == "invalid":
        failures.extend(f"summary: {item}" for item in summary.get("failures", []))
    return {
        "format": "CycleScope M11-I resume-aware campaign status v1",
        "instrument_io": False,
        "live_writes": False,
        "point_count": len(cases),
        "done_count": len(done),
        "pending_count": len(pending),
        "next_case_id": None if not pending else pending[0]["case_id"],
        "cases": cases,
        "summary": summary,
        "failures": failures,
        "live_ready": not failures and bool(pending),
        "points_complete": len(done) == len(cases) and not failures,
        "pass": len(done) == len(cases) and summary.get("pass") is True and not failures,
    }


def arb_prime_required(function: str) -> bool:
    normalized = function.upper()
    if normalized == "SIN":
        return True
    if normalized == "USER":
        return False
    raise M11ICampaignError(
        f"I ARB continuation requires SIN/OFF or USER/OFF, got {function!r}"
    )


def resolve_arb_prime_record(record: dict[str, Any]) -> dict[str, Any]:
    """Load and hash-check the derived waveform path before creating evidence."""

    case_id = str(record.get("case_id", ""))
    loaded = arb.load_arb_case(case_id)
    changed = [
        key
        for key, value in record.items()
        if key not in loaded or loaded[key] != value
    ]
    if changed:
        raise M11ICampaignError(
            f"{case_id}: ARB prime matrix/loaded record mismatch: {changed}"
        )
    if not Path(str(loaded.get("waveform_path", ""))).is_file():
        raise M11ICampaignError(f"{case_id}: validated ARB waveform path is missing")
    return loaded


def prime_first_arb(record: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    record = resolve_arb_prime_record(record)
    rendered_plan = arb.plan_text(record)
    status = before.get("source", {}).get("profile", {}).get("status", {})
    if before.get("pass") is not True or status.get("output") != "OFF":
        raise M11ICampaignError("I ARB prime requires a passing OFF preflight")
    if not arb_prime_required(str(status.get("function", ""))):
        return {
            "format": "CycleScope M11-I ARB prime v1",
            "performed": False,
            "reason": "DG already USER/OFF",
            "pass": True,
        }

    directory = (
        safety.EVIDENCE_ROOT
        / "preflight"
        / f"{safety.now_stamp()}_i-arb-prime-{record['case_id']}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    plan_path = directory / "source-arb-prime-plan.toml"
    plan_path.write_text(rendered_plan, encoding="utf-8")
    config = safety.derived_config()
    checked = arb.validate_configuration_plan(plan_path, record, config)
    plan = load_run_plan(plan_path)
    service = RunService(config=config, logger=CommandLogger())
    verify = service.verify(plan)
    run_archive = None
    error_text = None
    recovery = None
    try:
        runs_before = safety._run_directories()
        result = service.run(plan)
        runs_after = safety._run_directories()
        if result.run_dir.resolve() not in runs_after - runs_before:
            raise M11ICampaignError("ARB prime run directory was not uniquely created")
        run_archive = safety.archive_run(result.run_dir, directory / "wavebench" / "run")
        run_payload = sine.load_json(result.run_json_path)
        if run_payload.get("status") != "ok":
            raise M11ICampaignError("WaveBench ARB prime run did not pass")
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"

    try:
        after = arb.arb_readonly_preflight()
        after_status = after.get("source", {}).get("profile", {}).get("status", {})
        if after_status.get("output") == "ON":
            recovery = arb._ensure_arb_off(
                config,
                CommandLogger(directory / "source-prime-recovery.log"),
            )
            after = arb.arb_readonly_preflight()
    except Exception as error:
        after = {
            "pass": False,
            "evidence_path": None,
            "failures": [f"{type(error).__name__}: {error}"],
        }

    profile_failures: list[str] = []
    if after.get("pass") is True:
        logger = CommandLogger(directory / "source-prime-readback.log")
        source = SourceService(config=config, logger=logger)
        profile_failures = arb._profile_matches_arb(source.channel_profile(1), record)
    failures = list(profile_failures)
    if error_text is not None:
        failures.append(error_text)
    if after.get("pass") is not True:
        failures.extend(f"postflight: {item}" for item in after.get("failures", []))
    payload = {
        "format": "CycleScope M11-I checked OFF-only ARB prime v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "case_id": record["case_id"],
        "purpose": (
            "configuration-only SIN-to-USER transition so both formal I ARB points "
            "retain USER-to-USER repeat-upload evidence"
        ),
        "checked_plan": checked,
        "verify": [
            {
                "instrument": item.instrument,
                "idn": item.idn,
                "resource_sha256": safety.sha256_text(item.resource),
            }
            for item in verify
        ],
        "wavebench_run_archive": None if run_archive is None else str(run_archive.resolve()),
        "postflight_evidence": after.get("evidence_path"),
        "recovery": recovery,
        "dg_output_enabled": False,
        "scope_capture": False,
        "fpga_control": False,
        "dp832_writes": False,
        "failures": failures,
        "performed": True,
        "pass": not failures,
    }
    evidence_path = directory / "prime.json"
    safety.write_json_exclusive(evidence_path, payload)
    sums = safety._write_sha256sums(directory)
    payload["evidence_path"] = str(evidence_path.resolve())
    payload["sha256sums"] = str(sums.resolve())
    if failures:
        raise M11ICampaignError("ARB prime failed: " + "; ".join(failures))
    return payload


def _write_summary_if_ready() -> dict[str, Any]:
    existing = summary_status()
    if existing["state"] == "pass":
        return {"created": False, **existing}
    if existing["state"] == "invalid":
        raise M11ICampaignError(
            "existing I summary is invalid and will not be overwritten: "
            + "; ".join(existing.get("failures", []))
        )
    result = upper.write_summary(
        SUMMARY_DIR,
        upper.build_summary(POINTS_ROOT, safety.SOURCE_DATA_ROOT),
    )
    if result.get("pass") is not True or result.get("target_pass") is not True:
        raise M11ICampaignError("I summary did not pass both hard and 3 mV target gates")
    return {"created": True, **result}


def run_missing(acknowledgement: str) -> dict[str, Any]:
    if acknowledgement != I_CAMPAIGN_ACK:
        raise M11ICampaignError(
            f"live campaign requires --acknowledge {I_CAMPAIGN_ACK!r}"
        )
    initial = campaign_status()
    if initial["failures"]:
        raise M11ICampaignError("; ".join(initial["failures"]))
    executed: list[dict[str, Any]] = []
    for record in ordered_cases():
        case_id = str(record["case_id"])
        current = campaign_status()
        by_case = {item["case_id"]: item for item in current["cases"]}
        state = by_case[case_id]["state"]
        if state == "done":
            print(f"M11_I_SKIP_PASS case={case_id}", flush=True)
            continue
        if state != "pending":
            raise M11ICampaignError(f"{case_id}: state changed to {state!r}")
        print(f"M11_I_RUN_START case={case_id}", flush=True)
        if record["kind"] == "sine":
            result = sine.run_live(
                case_id=case_id,
                frames=int(record["minimum_frames"]),
                acknowledgement=sine.LIVE_ACK,
                stage_acknowledgement=sine.I_STAGE_ACK,
                calibration_manifest=CALIBRATION_MANIFEST,
            )
        else:
            preflight = arb.arb_readonly_preflight()
            if preflight.get("pass") is not True:
                raise M11ICampaignError(
                    f"{case_id}: ARB preflight failed: "
                    + "; ".join(preflight.get("failures", []))
                )
            prime = prime_first_arb(record, preflight)
            result = arb.run_live(
                case_id=case_id,
                frames=int(record["minimum_frames"]),
                acknowledgement=arb.LIVE_ACK,
                stage_acknowledgement=arb.I_STAGE_ACK,
                calibration_manifest=CALIBRATION_MANIFEST,
            )
            result = {**result, "campaign_prime": prime}
        executed.append(
            {
                "case_id": case_id,
                "pass": result.get("pass") is True,
                "evidence_path": result.get("evidence_path"),
            }
        )
        if result.get("pass") is not True:
            raise M11ICampaignError(
                f"{case_id}: live point failed; campaign stopped without retry"
            )
        verified = campaign_status()
        verified_case = next(item for item in verified["cases"] if item["case_id"] == case_id)
        if verified_case["state"] != "done":
            raise M11ICampaignError(f"{case_id}: new evidence did not verify as done")
        print(f"M11_I_RUN_DONE case={case_id}", flush=True)

    final_points = campaign_status()
    if final_points["points_complete"] is not True:
        raise M11ICampaignError("I point set is not complete after run")
    summary = _write_summary_if_ready()
    final = campaign_status()
    return {
        "format": "CycleScope M11-I missing-only campaign result v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "acknowledgement": acknowledgement,
        "initial": initial,
        "executed": executed,
        "summary": summary,
        "final": final,
        "automatic_retries": 0,
        "passing_points_repeated": 0,
        "dp832_writes": False,
        "pass": final.get("pass") is True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    live = commands.add_parser("run-missing")
    live.add_argument("--acknowledge", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "status":
            result = campaign_status()
        else:
            result = run_missing(args.acknowledge)
    except Exception as error:
        print(
            f"M11_I_CAMPAIGN_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    if args.command == "status":
        return 2 if result["failures"] else 0
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
