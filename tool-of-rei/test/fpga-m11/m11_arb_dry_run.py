#!/usr/bin/env python3
"""Validate every M11 ARB through WaveBench without instrument I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any


FPGA_ROOT = Path(__file__).resolve().parents[2]
WAVEBENCH_ROOT = FPGA_ROOT.parent / "tools" / "wavebench"
WAVEBENCH_CLI = WAVEBENCH_ROOT / ".venv" / "bin" / "wavebench"
BASE_CONFIG = FPGA_ROOT / "tool-of-rei" / "private" / "m8-wavebench-safe.toml"
PUBLIC_PLAN = FPGA_ROOT.parent / "public" / "信号前端测量方案.md"
M11_PLAN = FPGA_ROOT / "tool-of-rei" / "M11-真实全链路FIR与信号处理压力测试计划.md"
FIR_COEFFS = FPGA_ROOT / "Zynq_7010_PL" / "rtl" / "fir_coeffs_pkg.sv"
MAX_SOURCE_VPP = 0.5
EXPECTED_ARB_COUNT = 27


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def current_branch() -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=FPGA_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def expected_source_hashes() -> dict[str, str]:
    return {
        "public_measurement_plan": sha256_file(PUBLIC_PLAN),
        "m11_plan": sha256_file(M11_PLAN),
        "fir_coefficients": sha256_file(FIR_COEFFS),
    }


def validate_source_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    expected = expected_source_hashes()
    if manifest.get("source_hashes") != expected:
        raise RuntimeError("matrix source hashes are stale; regenerate the matrix")
    return expected


def validate_record(matrix_root: Path, record: dict[str, Any]) -> Path:
    case_id = record.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise RuntimeError("ARB record has no case_id")
    if record.get("kind") != "arb":
        raise RuntimeError(f"{case_id}: record is not an ARB")
    relative = Path(str(record.get("file", "")))
    if relative.is_absolute() or not relative.parts:
        raise RuntimeError(f"{case_id}: invalid relative waveform path")
    root = matrix_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"{case_id}: waveform escapes matrix root") from error
    if not path.is_file():
        raise RuntimeError(f"{case_id}: waveform is missing: {path}")
    if sha256_file(path) != record.get("sha256"):
        raise RuntimeError(f"{case_id}: waveform SHA-256 mismatch")
    points = record.get("points")
    if not isinstance(points, int) or not 1 <= points <= 16_384:
        raise RuntimeError(f"{case_id}: point count exceeds DG4202 guard")
    amplitude = record.get("source_vpp_v")
    if (
        not isinstance(amplitude, (int, float))
        or not math.isfinite(float(amplitude))
        or not 0 < float(amplitude) <= MAX_SOURCE_VPP
    ):
        raise RuntimeError(f"{case_id}: source amplitude exceeds M11 0.5 Vpp limit")
    frequency = record.get("playback_frequency_hz")
    if (
        not isinstance(frequency, (int, float))
        or not math.isfinite(float(frequency))
        or float(frequency) <= 0
    ):
        raise RuntimeError(f"{case_id}: invalid playback frequency")
    return path


def build_command(
    *,
    record: dict[str, Any],
    waveform: Path,
    arb_name: str,
    payload_path: Path,
) -> list[str]:
    return [
        str(WAVEBENCH_CLI),
        "source",
        "arb-load",
        "--channel",
        "1",
        "--file",
        str(waveform.resolve()),
        "--name",
        arb_name,
        "--amplitude",
        repr(float(record["source_vpp_v"])),
        "--frequency",
        repr(float(record["playback_frequency_hz"])),
        "--offset",
        "0",
        "--max-points",
        str(record["points"]),
        "--dry-run",
        "--export-payload",
        str(payload_path.resolve()),
        "--config",
        str(BASE_CONFIG),
    ]


def validate_payload(
    *,
    record: dict[str, Any],
    waveform: Path,
    arb_name: str,
    payload: dict[str, Any],
) -> None:
    case_id = record["case_id"]
    target = payload.get("target", {})
    source = payload.get("source", {})
    encoded = payload.get("payload", {})
    if payload.get("format") != "wavebench.arbitrary.v1":
        raise RuntimeError(f"{case_id}: unexpected WaveBench payload format")
    if target.get("channel") != 1 or target.get("name") != arb_name:
        raise RuntimeError(f"{case_id}: WaveBench target identity mismatch")
    if not math.isclose(
        float(target.get("amplitude_vpp", math.nan)),
        float(record["source_vpp_v"]),
        abs_tol=1e-12,
    ):
        raise RuntimeError(f"{case_id}: WaveBench target amplitude mismatch")
    if float(target.get("offset_v", math.nan)) != 0.0:
        raise RuntimeError(f"{case_id}: WaveBench target offset is not zero")
    if Path(str(source.get("source_path", ""))).resolve() != waveform.resolve():
        raise RuntimeError(f"{case_id}: WaveBench source path mismatch")
    if source.get("points") != record["points"]:
        raise RuntimeError(f"{case_id}: WaveBench point count mismatch")
    values = encoded.get("values")
    if encoded.get("encoding") != "dac14_unsigned_integer" or not isinstance(values, list):
        raise RuntimeError(f"{case_id}: invalid WaveBench DAC payload")
    if len(values) != record["points"] or min(values) < 0 or max(values) > 16_383:
        raise RuntimeError(f"{case_id}: WaveBench DAC payload violates 14-bit bounds")


def write_sha256sums(root: Path) -> Path:
    output = root / "SHA256SUMS"
    files = [path for path in sorted(root.rglob("*")) if path.is_file() and path != output]
    with output.open("x", encoding="utf-8") as stream:
        for path in files:
            stream.write(f"{sha256_file(path)}  {path.relative_to(root)}\n")
    return output


def run_all(matrix_root: Path, output_root: Path) -> dict[str, Any]:
    matrix_root = matrix_root.resolve()
    output_root = output_root.resolve()
    if current_branch() != "codex/FPGA":
        raise RuntimeError("refusing to run outside codex/FPGA")
    if output_root.exists():
        raise RuntimeError(f"refusing to overwrite evidence directory: {output_root}")
    if not WAVEBENCH_CLI.is_file():
        raise RuntimeError(f"WaveBench CLI is missing: {WAVEBENCH_CLI}")
    if not BASE_CONFIG.is_file() or stat.S_IMODE(BASE_CONFIG.stat().st_mode) & 0o077:
        raise RuntimeError("private WaveBench config is missing or not mode 0600")

    manifest_path = matrix_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "CycleScope M11 deterministic campaign matrix v1":
        raise RuntimeError("unexpected matrix manifest format")
    source_hashes = validate_source_hashes(manifest)
    records = manifest.get("arb_points")
    if not isinstance(records, list) or len(records) != EXPECTED_ARB_COUNT:
        raise RuntimeError(f"matrix must contain exactly {EXPECTED_ARB_COUNT} ARBs")
    case_ids = [record.get("case_id") for record in records]
    if len(set(case_ids)) != len(case_ids):
        raise RuntimeError("matrix contains duplicate ARB case_id values")

    output_root.mkdir(parents=True, exist_ok=False)
    payload_root = output_root / "payloads"
    log_root = output_root / "logs"
    payload_root.mkdir()
    log_root.mkdir()
    stage_indices: dict[str, int] = {}
    results: list[dict[str, Any]] = []
    for record in records:
        waveform = validate_record(matrix_root, record)
        stage = str(record.get("stage", ""))
        if stage not in {"G", "H", "I"}:
            raise RuntimeError(f"{record['case_id']}: unexpected ARB stage {stage!r}")
        stage_indices[stage] = stage_indices.get(stage, 0) + 1
        arb_name = f"M11{stage}{stage_indices[stage]:02d}"
        payload_path = payload_root / f"{record['case_id']}.json"
        log_path = log_root / f"{record['case_id']}.log"
        command = build_command(
            record=record,
            waveform=waveform,
            arb_name=arb_name,
            payload_path=payload_path,
        )
        completed = subprocess.run(
            command,
            cwd=WAVEBENCH_ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                f"{record['case_id']}: WaveBench dry-run exit code {completed.returncode}"
            )
        for marker in ("output_on=False", "dry_run=true", "upload=not_requested"):
            if marker not in completed.stdout:
                raise RuntimeError(f"{record['case_id']}: missing dry-run marker {marker}")
        if not payload_path.is_file():
            raise RuntimeError(f"{record['case_id']}: WaveBench payload was not created")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        validate_payload(
            record=record,
            waveform=waveform,
            arb_name=arb_name,
            payload=payload,
        )
        results.append(
            {
                "case_id": record["case_id"],
                "stage": stage,
                "arb_name": arb_name,
                "source_vpp_v": record["source_vpp_v"],
                "playback_frequency_hz": record["playback_frequency_hz"],
                "points": record["points"],
                "waveform": str(waveform),
                "waveform_sha256": sha256_file(waveform),
                "payload": str(payload_path.relative_to(output_root)),
                "payload_sha256": sha256_file(payload_path),
                "log": str(log_path.relative_to(output_root)),
                "wavebench_exit_code": completed.returncode,
                "dry_run": True,
                "instrument_io": False,
                "output_on": False,
            }
        )

    summary = {
        "format": "CycleScope M11 WaveBench ARB dry-run evidence v1",
        "pass": True,
        "instrument_io": False,
        "source_output_write": False,
        "matrix_manifest": str(manifest_path.resolve()),
        "matrix_manifest_sha256": sha256_file(manifest_path),
        "matrix_source_hashes": source_hashes,
        "runner_sha256": sha256_file(Path(__file__)),
        "base_config_sha256": sha256_file(BASE_CONFIG),
        "max_source_vpp": MAX_SOURCE_VPP,
        "case_count": len(results),
        "stage_counts": stage_indices,
        "cases": results,
    }
    summary_path = output_root / "summary.json"
    write_json_exclusive(summary_path, summary)
    sums = write_sha256sums(output_root)
    return {
        "pass": True,
        "output": str(output_root.resolve()),
        "summary": str(summary_path.resolve()),
        "sha256sums": str(sums.resolve()),
        "case_count": len(results),
        "stage_counts": stage_indices,
        "instrument_io": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_all(args.matrix, args.output)
    except Exception as error:
        print(f"M11_ARB_DRY_RUN_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
