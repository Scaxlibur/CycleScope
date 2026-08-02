#!/usr/bin/env python3
"""Offline M8/M9 acceptance for the frozen P4 response profile.

The fixture compares three independent records of the same live stimulus:

* the DG4202 theory manifest (the formal reference),
* FPGA mirror frames recomputed with the frozen response.csv, and
* ESP32-P4 ``p4cal=1 profile=C5DCDE41`` UART measurements.

It never opens an instrument, serial port, or network socket.  Frequencies
above 500 kHz are measured only as diagnostics and are deliberately never
passed through the in-band response table.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np


THIS_DIR = Path(__file__).resolve().parent
TEST_DIR = THIS_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import m12_p4_acceptance as legacy
import calibration_campaign as campaign
import calibration_core as core


EXPECTED_PROFILE_ID = 0xC5DCDE41
EXPECTED_RESPONSE_SHA256 = (
    "9d1aace37d65d82a7d3b1ff585ae95459a58e6cf90ece5030f7c313d80781c5f"
)
SUPPORTED_SOURCE_MAX_VPP = 0.25
MINIMUM_IN_BAND_HZ = 10_000.0
MAXIMUM_IN_BAND_HZ = 500_000.0
RECONSTRUCTION_POINTS = 4096

MEASUREMENT_RE = re.compile(
    r"measurement: session=(?P<session>[0-9A-Fa-f]{8}) "
    r"config=(?P<config>[0-9A-Fa-f]{8}) epoch=(?P<epoch>[0-9]+) "
    r"frame=(?P<frame>[0-9]+) gen=(?P<generation>[0-9]+) "
    r"F0=(?P<f0>[0-9.]+)Hz Vpp=(?P<vpp>[0-9.]+)mV "
    r"RMS=(?P<rms>[0-9.]+)mV peaks=(?P<count>[0-9]+) "
    r"P1=(?P<p1f>[0-9.]+)Hz/(?P<p1a>[0-9.]+)mVpk "
    r"P2=(?P<p2f>[0-9.]+)Hz/(?P<p2a>[0-9.]+)mVpk "
    r"P3=(?P<p3f>[0-9.]+)Hz/(?P<p3a>[0-9.]+)mVpk "
    r"up_cal=(?P<up_cal>[01]) test=(?P<test>[01]) "
    r"p4cal=(?P<p4cal>[01]) profile=(?P<profile>[0-9A-Fa-f]{8})"
)


class FinalAcceptanceError(RuntimeError):
    """The evidence cannot support a fail-closed M8/M9 verdict."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FinalAcceptanceError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise FinalAcceptanceError(f"JSON root is not an object: {path}")
    return payload


def finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise FinalAcceptanceError(f"{field} is not numeric") from error
    if not math.isfinite(result):
        raise FinalAcceptanceError(f"{field} is not finite")
    return result


@dataclass(frozen=True)
class FrozenResponseProfile:
    profile_id: int
    identity_sha256: str
    response_sha256: str
    upstream: dict[str, int]
    frequencies_hz: tuple[float, ...]
    input_uv_per_code: tuple[float, ...]
    fit_manifest_sha256: str
    holdout_manifest_sha256: str
    asset_manifest_sha256: str

    def interpolate_input_uv_per_code(self, frequency_hz: float) -> float:
        frequency = finite_float(frequency_hz, "response interpolation frequency")
        if frequency < self.frequencies_hz[0] or frequency > self.frequencies_hz[-1]:
            raise FinalAcceptanceError(
                f"response interpolation outside {self.frequencies_hz[0]:g}.."
                f"{self.frequencies_hz[-1]:g} Hz"
            )
        for index, upper_frequency in enumerate(self.frequencies_hz):
            if frequency == upper_frequency:
                return self.input_uv_per_code[index]
            if frequency < upper_frequency:
                lower_frequency = self.frequencies_hz[index - 1]
                fraction = (frequency - lower_frequency) / (
                    upper_frequency - lower_frequency
                )
                return self.input_uv_per_code[index - 1] + fraction * (
                    self.input_uv_per_code[index]
                    - self.input_uv_per_code[index - 1]
                )
        raise AssertionError("bounded interpolation did not find an interval")

    def correction_factor(self, frequency_hz: float, incoming_scale_uv: int) -> float:
        if incoming_scale_uv != self.upstream["scale_uv_per_lsb"]:
            raise FinalAcceptanceError("incoming scale differs from the frozen profile")
        return self.interpolate_input_uv_per_code(frequency_hz) / float(
            incoming_scale_uv
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": f"{self.profile_id:08X}",
            "identity_sha256": self.identity_sha256,
            "response_sha256": self.response_sha256,
            "upstream": self.upstream,
            "anchor_count": len(self.frequencies_hz),
            "valid_frequency_hz": [
                self.frequencies_hz[0],
                self.frequencies_hz[-1],
            ],
            "fit_manifest_sha256": self.fit_manifest_sha256,
            "holdout_manifest_sha256": self.holdout_manifest_sha256,
            "asset_manifest_sha256": self.asset_manifest_sha256,
        }


def _verified_manifest(directory: Path, label: str) -> dict[str, Any]:
    try:
        audit = core.verify_sha256s(directory)
    except (OSError, core.CalibrationError) as error:
        raise FinalAcceptanceError(f"{label} SHA256 audit failed: {error}") from error
    if not isinstance(audit, dict) or not isinstance(
        audit.get("manifest_sha256"), str
    ):
        raise FinalAcceptanceError(f"{label} SHA256 audit result is malformed")
    return audit


def load_frozen_profile(
    fit_dir: Path, holdout_dir: Path, asset_dir: Path
) -> FrozenResponseProfile:
    fit_dir = fit_dir.resolve()
    holdout_dir = holdout_dir.resolve()
    asset_dir = asset_dir.resolve()
    fit_audit = _verified_manifest(fit_dir, "fit")
    holdout_audit = _verified_manifest(holdout_dir, "holdout")
    asset_audit = _verified_manifest(asset_dir, "P4 asset")

    calibration = read_json(fit_dir / "calibration.json")
    build = read_json(fit_dir / "calibration-build-manifest.json")
    holdout = read_json(holdout_dir / "holdout-report.json")
    asset = read_json(asset_dir / "p4-asset-manifest.json")

    profile_ids = {
        int(calibration.get("p4_response_profile_id", 0)),
        int(build.get("p4_response_profile_id", 0)),
        int(holdout.get("p4_response_profile_id", 0)),
        int(asset.get("p4_response_profile_id", 0)),
    }
    if profile_ids != {EXPECTED_PROFILE_ID}:
        raise FinalAcceptanceError(
            f"frozen artifact profile IDs differ: {sorted(profile_ids)}"
        )
    if calibration.get("status") != "fit-frozen-before-holdout":
        raise FinalAcceptanceError("fit is not marked frozen before holdout")
    if (
        holdout.get("pass") is not True
        or holdout.get("hard_5mv_1khz_pass") is not True
        or holdout.get("fit_frozen_before_holdout") is not True
        or holdout.get("holdout_refit_performed") is not False
    ):
        raise FinalAcceptanceError("holdout is not an untouched passing M6 result")
    if asset.get("holdout_hard_gate_pass") is not True:
        raise FinalAcceptanceError("P4 asset does not bind a passing hard holdout gate")

    expected_upstream = {key: int(value) for key, value in core.UPSTREAM_IDENTITY.items()}
    for label, payload in (
        ("calibration", calibration.get("upstream_identity")),
        ("build", build.get("upstream_identity")),
        ("asset", asset.get("upstream_identity")),
    ):
        if payload != expected_upstream:
            raise FinalAcceptanceError(f"{label} upstream identity differs")

    identity_sha = str(calibration.get("identity_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", identity_sha):
        raise FinalAcceptanceError("frozen identity SHA256 is invalid")
    if build.get("identity_sha256") != identity_sha or asset.get(
        "identity_sha256"
    ) != identity_sha:
        raise FinalAcceptanceError("identity SHA256 differs across frozen artifacts")

    response_path = fit_dir / "response.csv"
    response_sha = sha256(response_path)
    if response_sha != EXPECTED_RESPONSE_SHA256:
        raise FinalAcceptanceError(
            f"unexpected response.csv SHA256 {response_sha}"
        )
    if (
        build.get("response_csv", {}).get("sha256") != response_sha
        or asset.get("response_csv_sha256") != response_sha
    ):
        raise FinalAcceptanceError("response.csv SHA256 binding differs")
    if asset.get("fit_sha256s_manifest") != fit_audit["manifest_sha256"]:
        raise FinalAcceptanceError("P4 asset does not bind the audited fit manifest")
    if asset.get("holdout_sha256s_manifest") != holdout_audit["manifest_sha256"]:
        raise FinalAcceptanceError("P4 asset does not bind the audited holdout manifest")

    rows: list[tuple[float, float]] = []
    try:
        with response_path.open("r", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                rows.append(
                    (
                        finite_float(row.get("frequency_hz"), "response frequency"),
                        finite_float(
                            row.get("input_uv_per_code"),
                            "response input_uv_per_code",
                        ),
                    )
                )
    except OSError as error:
        raise FinalAcceptanceError(f"cannot read response.csv: {error}") from error
    if len(rows) != 12:
        raise FinalAcceptanceError("response.csv must contain exactly 12 anchors")
    if any(
        not frequency > 0.0
        or not scale > 0.0
        or (index and frequency <= rows[index - 1][0])
        for index, (frequency, scale) in enumerate(rows)
    ):
        raise FinalAcceptanceError("response.csv anchors are invalid or non-monotonic")
    if rows[0][0] != MINIMUM_IN_BAND_HZ or rows[-1][0] != MAXIMUM_IN_BAND_HZ:
        raise FinalAcceptanceError("response.csv endpoints differ from 10..500 kHz")

    generated = asset.get("generated_header", {})
    header_path = asset_dir / str(generated.get("path", ""))
    header_sha = str(generated.get("sha256", ""))
    if not header_path.is_file() or sha256(header_path) != header_sha:
        raise FinalAcceptanceError("generated P4 header is missing or changed")
    installed_path = Path(str(asset.get("installed_header", "")))
    if not installed_path.is_file() or sha256(installed_path) != header_sha:
        raise FinalAcceptanceError("installed P4 response header differs from frozen asset")

    return FrozenResponseProfile(
        profile_id=EXPECTED_PROFILE_ID,
        identity_sha256=identity_sha,
        response_sha256=response_sha,
        upstream=expected_upstream,
        frequencies_hz=tuple(row[0] for row in rows),
        input_uv_per_code=tuple(row[1] for row in rows),
        fit_manifest_sha256=str(fit_audit["manifest_sha256"]),
        holdout_manifest_sha256=str(holdout_audit["manifest_sha256"]),
        asset_manifest_sha256=str(asset_audit["manifest_sha256"]),
    )


def load_target_and_safety(
    manifest_path: Path,
    *,
    maximum_programmed_vpp: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    try:
        target, tones = legacy.load_target(manifest_path)
    except legacy.AcceptanceError as error:
        raise FinalAcceptanceError(str(error)) from error
    manifest = read_json(manifest_path)
    source = manifest.get("source")
    source_tones = manifest.get("source_tones")
    if not isinstance(source, dict) or not isinstance(source_tones, list):
        raise FinalAcceptanceError("manifest is missing source settings or source tones")
    programmed_scale = finite_float(
        source.get("programmed_amplitude_scale"), "programmed amplitude scale"
    )
    programmed_vpp = finite_float(
        source.get("programmed_amplitude_vpp"), "programmed source Vpp"
    )
    if programmed_scale != 1.0:
        raise FinalAcceptanceError(
            "final P4 acceptance requires DG-setting theory (programmed scale 1.0)"
        )
    if not 0.0 < programmed_vpp <= maximum_programmed_vpp + 1.0e-12:
        raise FinalAcceptanceError(
            f"DG programmed Vpp {programmed_vpp:.9g} exceeds "
            f"{maximum_programmed_vpp:.9g} Vpp"
        )
    if source.get("load_ohm") != 50 or finite_float(
        source.get("offset_v"), "source offset"
    ) != 0.0:
        raise FinalAcceptanceError("source is not frozen at 50 ohm / 0 V offset")
    if source.get("output_function") != "USER":
        raise FinalAcceptanceError("final live manifest is not a USER ARB waveform")

    parsed_source: list[dict[str, Any]] = []
    for raw in source_tones:
        if not isinstance(raw, dict):
            raise FinalAcceptanceError("source tone is not an object")
        parsed_source.append(
            {
                "order": int(raw["order"]),
                "frequency_hz": finite_float(raw.get("frequency_hz"), "source tone frequency"),
                "amplitude_vpk": finite_float(raw.get("peak_v"), "source tone amplitude"),
                "phase_deg": finite_float(raw.get("phase_deg"), "source tone phase"),
            }
        )
    if any(tone["amplitude_vpk"] < 0.005 for tone in parsed_source):
        raise FinalAcceptanceError("a source component is below the formal 5 mVpeak floor")
    if any(
        tone["frequency_hz"] < MINIMUM_IN_BAND_HZ
        for tone in parsed_source
    ):
        raise FinalAcceptanceError("a source component is below 10 kHz")
    out_of_band = [
        tone for tone in parsed_source if tone["frequency_hz"] > MAXIMUM_IN_BAND_HZ
    ]
    return target, tones, manifest, out_of_band


def load_verified_mirror(
    mirror_dir: Path, profile: FrozenResponseProfile
) -> tuple[list[dict[str, Any]], np.ndarray]:
    try:
        records, frames = campaign.mirror_frame_records(mirror_dir)
        identity = core.validate_mirror_metadata(records)
    except (OSError, ValueError, KeyError, campaign.CampaignError, core.CalibrationError) as error:
        raise FinalAcceptanceError(f"mirror identity/integrity audit failed: {error}") from error
    if identity.get("identity") != profile.upstream:
        raise FinalAcceptanceError("mirror identity differs from the frozen P4 profile")
    if frames.dtype != np.dtype("<i2") or frames.shape[1] != core.FRAME_SAMPLES:
        raise FinalAcceptanceError("mirror samples are not 8192-point S16_LE frames")
    return records, frames


def corrected_analysis(
    samples: np.ndarray,
    metadata: dict[str, Any],
    profile: FrozenResponseProfile,
) -> dict[str, Any]:
    try:
        result = legacy.analyze_frame(samples, metadata)
    except legacy.AcceptanceError as error:
        raise FinalAcceptanceError(str(error)) from error
    scale_uv = int(metadata["scale_uv_per_lsb"])
    edge_tolerance_hz = (
        0.5 * float(metadata["sample_rate_hz"]) / float(core.FRAME_SAMPLES)
    )
    corrected_lines: list[dict[str, Any]] = []
    rms_square = 0.0
    for raw_line in result["lines"]:
        frequency_hz = finite_float(raw_line["frequency_hz"], "line frequency")
        if not (
            MINIMUM_IN_BAND_HZ - edge_tolerance_hz
            <= frequency_hz
            <= MAXIMUM_IN_BAND_HZ + edge_tolerance_hz
        ):
            raise FinalAcceptanceError("a formal spectral line is outside response range")
        response_query_hz = min(
            MAXIMUM_IN_BAND_HZ,
            max(MINIMUM_IN_BAND_HZ, frequency_hz),
        )
        factor = profile.correction_factor(response_query_hz, scale_uv)
        amplitude = finite_float(raw_line["amplitude_vpk"], "raw line amplitude") * factor
        phase = finite_float(raw_line["phase_radians"], "line phase")
        rms_square += amplitude * amplitude * 0.5
        corrected_lines.append(
            {
                "order": int(raw_line["order"]),
                "frequency_hz": frequency_hz,
                "amplitude_vpk": amplitude,
                "phase_radians": phase,
                "response_factor": factor,
                "response_query_frequency_hz": response_query_hz,
            }
        )
    phase_grid = 2.0 * math.pi * np.arange(
        RECONSTRUCTION_POINTS, dtype=np.float64
    ) / float(RECONSTRUCTION_POINTS)
    waveform = sum(
        line["amplitude_vpk"]
        * np.sin(line["order"] * phase_grid + line["phase_radians"])
        for line in corrected_lines
    )
    vpp = float(np.ptp(waveform))
    rms = math.sqrt(rms_square)
    return {
        "fundamental_hz": float(result["fundamental_hz"]),
        "voltage_peak_to_peak_v": vpp,
        "true_rms_v": rms,
        "lines": corrected_lines,
        "waveform_projection": {
            "points": RECONSTRUCTION_POINTS,
            "voltage_peak_to_peak_v": vpp,
            "true_rms_v": float(np.sqrt(np.mean(np.square(waveform)))),
        },
    }


def summarize(values: Iterable[float]) -> dict[str, float]:
    try:
        return legacy.summarize(values)
    except legacy.AcceptanceError as error:
        raise FinalAcceptanceError(str(error)) from error


def summarize_analysis(results: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        summary = legacy.summarize_analysis(results)
    except legacy.AcceptanceError as error:
        raise FinalAcceptanceError(str(error)) from error
    summary["response_factors"] = [
        {
            "order": int(line["order"]),
            "factor": summarize(
                float(result["lines"][line_index]["response_factor"])
                for result in results
            ),
        }
        for line_index, line in enumerate(results[0]["lines"])
    ]
    return summary


def parse_uart(path: Path, expected_profile_id: int) -> tuple[list[dict[str, Any]], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise FinalAcceptanceError(f"cannot read P4 UART log: {error}") from error
    rows: list[dict[str, Any]] = []
    for match in MEASUREMENT_RE.finditer(text):
        count = int(match.group("count"))
        if not 2 <= count <= 3:
            continue
        profile_id = int(match.group("profile"), 16)
        if (
            int(match.group("up_cal")) != 1
            or int(match.group("test")) != 0
            or int(match.group("p4cal")) != 1
            or profile_id != expected_profile_id
        ):
            continue
        lines = [
            {
                "frequency_hz": float(match.group(f"p{index}f")),
                "amplitude_mVpk": float(match.group(f"p{index}a")),
            }
            for index in range(1, count + 1)
        ]
        rows.append(
            {
                "session_id": int(match.group("session"), 16),
                "config_id": int(match.group("config"), 16),
                "epoch": int(match.group("epoch")),
                "frame_id": int(match.group("frame")),
                "generation": int(match.group("generation")),
                "fundamental_hz": float(match.group("f0")),
                "voltage_peak_to_peak_mV": float(match.group("vpp")),
                "true_rms_mV": float(match.group("rms")),
                "lines": lines,
                "p4_response_profile_id": profile_id,
            }
        )
    if not rows:
        raise FinalAcceptanceError(
            f"UART contains no p4cal=1 profile={expected_profile_id:08X} measurement"
        )
    return rows, text


def select_uart_rows(
    rows: list[dict[str, Any]],
    *,
    target: dict[str, Any],
    tones: list[dict[str, Any]],
    active_records: list[dict[str, Any]],
    frequency_tolerance_hz: float,
) -> list[dict[str, Any]]:
    sessions = {
        (int(record["session_id"]), int(record["config_id"]))
        for record in active_records
    }
    if len(sessions) != 1:
        raise FinalAcceptanceError("active mirror run spans multiple session/config pairs")
    first_frame = int(active_records[0]["frame_id"])
    last_frame = int(active_records[-1]["frame_id"])
    source_on_threshold_mV = max(
        legacy.MINIMUM_PEAK_V * 1000.0,
        float(tones[0]["amplitude_vpk"]) * 1000.0 * 0.25,
    )
    selected: list[dict[str, Any]] = []
    for row in rows:
        if (row["session_id"], row["config_id"]) not in sessions:
            continue
        if not first_frame <= int(row["frame_id"]) <= last_frame:
            continue
        if len(row["lines"]) != len(tones):
            continue
        if float(row["lines"][0]["amplitude_mVpk"]) <= source_on_threshold_mV:
            continue
        if abs(float(row["fundamental_hz"]) - float(target["fundamental_hz"])) > frequency_tolerance_hz:
            continue
        if any(
            abs(float(line["frequency_hz"]) - float(tone["frequency_hz"]))
            > frequency_tolerance_hz
            for line, tone in zip(row["lines"], tones, strict=True)
        ):
            continue
        selected.append(row)
    if not selected:
        raise FinalAcceptanceError(
            "no profile-bound P4 UART measurement overlaps the active mirror run"
        )
    return selected


def summarize_uart(rows: list[dict[str, Any]], tones: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        summary = legacy.summarize_uart(rows, tones)
    except legacy.AcceptanceError as error:
        raise FinalAcceptanceError(str(error)) from error
    summary["sessions"] = sorted({f"{int(row['session_id']):08X}" for row in rows})
    summary["configs"] = sorted({f"{int(row['config_id']):08X}" for row in rows})
    summary["p4_response_profile_id"] = f"{EXPECTED_PROFILE_ID:08X}"
    return summary


def interference_diagnostics(
    frames: np.ndarray,
    records: list[dict[str, Any]],
    indexes: list[int],
    out_of_band: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not out_of_band:
        return []
    window = np.hanning(core.FRAME_SAMPLES)
    window_sum = float(np.sum(window))
    diagnostics: list[dict[str, Any]] = []
    for tone in out_of_band:
        amplitudes: list[float] = []
        for index in indexes:
            record = records[index]
            sample_rate_hz = float(record["sample_rate_hz"])
            frequency_hz = float(tone["frequency_hz"])
            if frequency_hz >= sample_rate_hz * 0.5:
                raise FinalAcceptanceError("interference tone is at/above mirror Nyquist")
            amplitude, _phase = legacy.projection(
                frames[index],
                volts_per_lsb=float(record["scale_uv_per_lsb"]) * 1.0e-6,
                offset_v=float(record["offset_uv"]) * 1.0e-6,
                sample_rate_hz=sample_rate_hz,
                window=window,
                window_sum=window_sum,
                frequency_hz=frequency_hz,
            )
            amplitudes.append(amplitude * 1000.0)
        diagnostics.append(
            {
                "order": int(tone["order"]),
                "frequency_hz": float(tone["frequency_hz"]),
                "source_amplitude_mVpk": float(tone["amplitude_vpk"]) * 1000.0,
                "mirror_raw_516uV_per_code_amplitude_mVpk": summarize(amplitudes),
                "response_interpolation_attempted": False,
                "response_applied": False,
                "formal_P4_line": False,
            }
        )
    return diagnostics


def compare_to_theory(
    source: str,
    measured: dict[str, Any],
    target: dict[str, Any],
    tones: list[dict[str, Any]],
    *,
    frequency_tolerance_hz: float,
    amplitude_tolerance_mV: float,
) -> list[str]:
    return legacy.compare_to_theory(
        source,
        measured,
        target,
        tones,
        frequency_tolerance_hz=frequency_tolerance_hz,
        amplitude_tolerance_mV=amplitude_tolerance_mV,
    )


def compare_p4_to_mirror(
    p4: dict[str, Any],
    mirror: dict[str, Any],
    *,
    amplitude_tolerance_mV: float,
    frequency_tolerance_hz: float,
) -> list[str]:
    return legacy.compare_p4_to_mirror(
        p4, mirror, amplitude_tolerance_mV, frequency_tolerance_hz
    )


def compare_uart_rows_to_theory(
    rows: list[dict[str, Any]],
    target: dict[str, Any],
    tones: list[dict[str, Any]],
    *,
    frequency_tolerance_hz: float,
    amplitude_tolerance_mV: float,
) -> list[str]:
    """Reject any sparse board log outlier; a passing median is insufficient."""

    failures: list[str] = []
    for row in rows:
        frame = int(row["frame_id"])
        if abs(float(row["fundamental_hz"]) - float(target["fundamental_hz"])) > frequency_tolerance_hz:
            failures.append(f"P4 frame {frame} fundamental exceeds frequency tolerance")
        if abs(
            float(row["voltage_peak_to_peak_mV"])
            - float(target["voltage_peak_to_peak_v"]) * 1000.0
        ) > amplitude_tolerance_mV:
            failures.append(f"P4 frame {frame} Vpp exceeds amplitude tolerance")
        if abs(
            float(row["true_rms_mV"])
            - float(target["true_rms_v"]) * 1000.0
        ) > amplitude_tolerance_mV:
            failures.append(f"P4 frame {frame} RMS exceeds amplitude tolerance")
        if len(row["lines"]) != len(tones):
            failures.append(f"P4 frame {frame} line count differs from target")
            continue
        for line, tone in zip(row["lines"], tones, strict=True):
            order = int(tone["order"])
            if abs(
                float(line["frequency_hz"]) - float(tone["frequency_hz"])
            ) > frequency_tolerance_hz:
                failures.append(
                    f"P4 frame {frame} H{order} frequency exceeds tolerance"
                )
            if abs(
                float(line["amplitude_mVpk"])
                - float(tone["amplitude_vpk"]) * 1000.0
            ) > amplitude_tolerance_mV:
                failures.append(
                    f"P4 frame {frame} H{order} amplitude exceeds tolerance"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mirror-dir", type=Path, required=True)
    parser.add_argument("--uart-log", type=Path, required=True)
    parser.add_argument("--fit-dir", type=Path, required=True)
    parser.add_argument("--holdout-dir", type=Path, required=True)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frequency-tolerance-hz", type=float, default=1000.0)
    parser.add_argument("--amplitude-tolerance-mv", type=float, default=5.0)
    parser.add_argument("--p4-mirror-amplitude-tolerance-mv", type=float, default=1.0)
    parser.add_argument("--p4-mirror-frequency-tolerance-hz", type=float, default=2.0)
    parser.add_argument(
        "--maximum-programmed-vpp",
        type=float,
        default=SUPPORTED_SOURCE_MAX_VPP,
    )
    args = parser.parse_args()
    if min(
        args.frequency_tolerance_hz,
        args.amplitude_tolerance_mv,
        args.p4_mirror_amplitude_tolerance_mv,
        args.p4_mirror_frequency_tolerance_hz,
        args.maximum_programmed_vpp,
    ) <= 0.0:
        parser.error("all tolerances and maximum Vpp must be positive")

    payload: dict[str, Any] = {
        "format": "CycleScope final P4 response acceptance v1",
        "manifest": str(args.manifest),
        "mirror_dir": str(args.mirror_dir),
        "uart_log": str(args.uart_log),
        "pass": False,
        "failures": [],
    }
    try:
        profile = load_frozen_profile(args.fit_dir, args.holdout_dir, args.asset_dir)
        target, tones, manifest, out_of_band = load_target_and_safety(
            args.manifest,
            maximum_programmed_vpp=args.maximum_programmed_vpp,
        )
        records, frames = load_verified_mirror(args.mirror_dir, profile)
        try:
            active_run = legacy.signal_run_indices(
                frames,
                records,
                target["fundamental_hz"],
                tones[0]["amplitude_vpk"],
            )
            indexes = legacy.representative_indices(active_run)
        except legacy.AcceptanceError as error:
            raise FinalAcceptanceError(str(error)) from error
        analyses = [
            corrected_analysis(frames[index], records[index], profile)
            for index in indexes
        ]
        mirror = summarize_analysis(analyses)
        active_records = [records[index] for index in active_run]
        uart_rows, uart_text = parse_uart(args.uart_log, profile.profile_id)
        selected_uart = select_uart_rows(
            uart_rows,
            target=target,
            tones=tones,
            active_records=active_records,
            frequency_tolerance_hz=args.frequency_tolerance_hz,
        )
        p4 = summarize_uart(selected_uart, tones)

        failures = compare_to_theory(
            "response-corrected mirror",
            mirror,
            target,
            tones,
            frequency_tolerance_hz=args.frequency_tolerance_hz,
            amplitude_tolerance_mV=args.amplitude_tolerance_mv,
        )
        failures.extend(
            compare_to_theory(
                "P4",
                p4,
                target,
                tones,
                frequency_tolerance_hz=args.frequency_tolerance_hz,
                amplitude_tolerance_mV=args.amplitude_tolerance_mv,
            )
        )
        failures.extend(
            compare_p4_to_mirror(
                p4,
                mirror,
                amplitude_tolerance_mV=args.p4_mirror_amplitude_tolerance_mv,
                frequency_tolerance_hz=args.p4_mirror_frequency_tolerance_hz,
            )
        )
        failures.extend(
            compare_uart_rows_to_theory(
                selected_uart,
                target,
                tones,
                frequency_tolerance_hz=args.frequency_tolerance_hz,
                amplitude_tolerance_mV=args.amplitude_tolerance_mv,
            )
        )
        projection = mirror["waveform_projection"]
        if abs(
            projection["voltage_peak_to_peak_mV"]["median"]
            - mirror["voltage_peak_to_peak_mV"]["median"]
        ) > 0.001:
            failures.append("corrected 1P/3P model Vpp differs from FFT reconstruction")
        if abs(
            projection["true_rms_mV"]["median"]
            - mirror["true_rms_mV"]["median"]
        ) > 0.001:
            failures.append("corrected 1P/3P model RMS differs from FFT reconstruction")
        forbidden_uart = (
            "Guru Meditation",
            "Task watchdog got triggered",
            "assert failed",
            "Spectrum display projection rejected",
            "Waveform projection rejected",
            "FFT processing failed",
        )
        present_forbidden = [item for item in forbidden_uart if item in uart_text]
        if present_forbidden:
            failures.append(
                "UART contains fatal/analysis failures: " + ", ".join(present_forbidden)
            )

        payload.update(
            {
                "profile": profile.as_dict(),
                "source_safety": {
                    "formal_supported_max_vpp": SUPPORTED_SOURCE_MAX_VPP,
                    "acceptance_maximum_programmed_vpp": args.maximum_programmed_vpp,
                    "programmed_vpp": float(
                        manifest["source"]["programmed_amplitude_vpp"]
                    ),
                    "programmed_amplitude_scale": float(
                        manifest["source"]["programmed_amplitude_scale"]
                    ),
                    "dg_reference": "DG4202_CH1_50OHM_SETTING",
                    "dp800_writes": 0,
                    "fpga_changes": False,
                },
                "target": {
                    "fundamental_hz": target["fundamental_hz"],
                    "voltage_peak_to_peak_mV": target["voltage_peak_to_peak_v"] * 1000.0,
                    "true_rms_mV": target["true_rms_v"] * 1000.0,
                    "lines": [
                        {
                            "order": int(tone["order"]),
                            "frequency_hz": float(tone["frequency_hz"]),
                            "amplitude_mVpk": float(tone["amplitude_vpk"]) * 1000.0,
                        }
                        for tone in tones
                    ],
                },
                "response_corrected_mirror": mirror,
                "p4": p4,
                "selected_p4_uart_rows": selected_uart,
                "out_of_band_diagnostics": interference_diagnostics(
                    frames, records, indexes, out_of_band
                ),
                "selected_mirror_frames": {
                    "source_on_run_frame_ids": [
                        int(records[active_run[0]]["frame_id"]),
                        int(records[active_run[-1]]["frame_id"]),
                    ],
                    "source_on_complete_frames": len(active_run),
                    "analyzed_frame_ids": [
                        int(records[index]["frame_id"]) for index in indexes
                    ],
                    "session_id": f"{int(active_records[0]['session_id']):08X}",
                    "config_id": f"{int(active_records[0]['config_id']):08X}",
                    "core_flags": "000C",
                },
                "tolerances": {
                    "frequency_hz": args.frequency_tolerance_hz,
                    "amplitude_mV": args.amplitude_tolerance_mv,
                    "p4_mirror_amplitude_mV": args.p4_mirror_amplitude_tolerance_mv,
                    "p4_mirror_frequency_hz": args.p4_mirror_frequency_tolerance_hz,
                },
                "failures": failures,
                "pass": not failures,
            }
        )
    except (
        FinalAcceptanceError,
        legacy.AcceptanceError,
        core.CalibrationError,
        campaign.CampaignError,
        KeyError,
        OSError,
        ValueError,
    ) as error:
        payload["failures"] = [str(error)]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
