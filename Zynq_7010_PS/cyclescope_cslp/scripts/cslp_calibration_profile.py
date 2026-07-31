#!/usr/bin/env python3
"""Validate an evidence-bound CSLP calibration profile for a firmware build."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


FORMAT = "CycleScope M11 validated calibration build manifest v1"
REQUIRED_ARTIFACTS = (
    "calibration_json",
    "response_csv",
    "uncertainty_json",
    "holdout_report",
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class CalibrationProfileError(ValueError):
    """A calibration manifest is incomplete, stale, or unsafe to build."""


@dataclass(frozen=True)
class CalibrationProfile:
    calibration_id: int
    scale_uv_per_lsb: int
    offset_uv: int
    manifest_path: Path
    manifest_sha256: str
    artifact_records: tuple[dict[str, Any], ...]

    def compile_definitions(self) -> str:
        return (
            f"CSLP_WAVE_SCALE_UV_PER_LSB={self.scale_uv_per_lsb}U;"
            f"CSLP_WAVE_OFFSET_UV={self.offset_uv};"
            f"CSLP_WAVE_CALIBRATION_ID={self.calibration_id}U"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CalibrationProfileError(f"cannot read calibration manifest: {path}") from error
    if not isinstance(value, dict):
        raise CalibrationProfileError("calibration manifest must be a JSON object")
    return value


def _integer(payload: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationProfileError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise CalibrationProfileError(f"{key} must be in {minimum}..{maximum}")
    return value


def _resolve_artifact(root: Path, relative_text: Any) -> Path:
    if not isinstance(relative_text, str) or not relative_text:
        raise CalibrationProfileError("calibration artifact path must be a non-empty string")
    relative = Path(relative_text)
    if relative.is_absolute():
        raise CalibrationProfileError("calibration artifact path must be relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise CalibrationProfileError("calibration artifact path escapes manifest directory") from error
    return path


def load_calibration_profile(path: Path) -> CalibrationProfile:
    path = path.resolve()
    payload = _load_object(path)
    if payload.get("format") != FORMAT:
        raise CalibrationProfileError("unsupported calibration manifest format")
    if payload.get("status") != "validated":
        raise CalibrationProfileError("calibration status must be validated")

    calibration_id = _integer(payload, "calibration_id", 1, 0xFFFF)
    scale_uv_per_lsb = _integer(payload, "scale_uv_per_lsb", 1, 0xFFFFFFFF)
    offset_uv = _integer(payload, "offset_uv", -0x80000000, 0x7FFFFFFF)
    if _integer(payload, "filter_profile", 1, 0xFFFF) != 1:
        raise CalibrationProfileError("M11 calibration requires filter_profile=1")
    if _integer(payload, "sample_rate_hz", 1, 0xFFFFFFFF) != 4_062_500:
        raise CalibrationProfileError("M11 calibration sample_rate_hz mismatch")
    if _integer(payload, "frame_samples", 1, 0xFFFFFFFF) != 8192:
        raise CalibrationProfileError("M11 calibration frame_samples mismatch")

    matrix_sha256 = payload.get("matrix_manifest_sha256")
    if not isinstance(matrix_sha256, str) or SHA256_PATTERN.fullmatch(matrix_sha256) is None:
        raise CalibrationProfileError("matrix_manifest_sha256 is missing or invalid")

    validation = payload.get("validation")
    if not isinstance(validation, dict) or validation.get("holdout_pass") is not True:
        raise CalibrationProfileError("validated calibration requires holdout_pass=true")
    holdout_points = _integer(validation, "holdout_point_count", 7, 1_000_000)
    if holdout_points < 7:
        raise CalibrationProfileError("validated calibration requires all seven holdout points")
    max_error = validation.get("max_absolute_error_v")
    if (
        isinstance(max_error, bool)
        or not isinstance(max_error, (int, float))
        or not math.isfinite(float(max_error))
        or not 0 <= float(max_error) <= 0.005
    ):
        raise CalibrationProfileError("holdout max_absolute_error_v must be within 5 mV")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CalibrationProfileError("calibration artifacts object is missing")
    records: list[dict[str, Any]] = []
    for name in REQUIRED_ARTIFACTS:
        binding = artifacts.get(name)
        if not isinstance(binding, dict):
            raise CalibrationProfileError(f"calibration artifact binding is missing: {name}")
        artifact = _resolve_artifact(path.parent, binding.get("path"))
        expected = binding.get("sha256")
        if not isinstance(expected, str) or SHA256_PATTERN.fullmatch(expected) is None:
            raise CalibrationProfileError(f"calibration artifact SHA-256 is invalid: {name}")
        if not artifact.is_file():
            raise CalibrationProfileError(f"calibration artifact is missing: {name}")
        actual = sha256_file(artifact)
        if actual != expected:
            raise CalibrationProfileError(f"calibration artifact SHA-256 mismatch: {name}")
        records.append(
            {
                "name": name,
                "path": str(artifact),
                "sha256": actual,
                "size": artifact.stat().st_size,
            }
        )

    return CalibrationProfile(
        calibration_id=calibration_id,
        scale_uv_per_lsb=scale_uv_per_lsb,
        offset_uv=offset_uv,
        manifest_path=path,
        manifest_sha256=sha256_file(path),
        artifact_records=tuple(records),
    )
