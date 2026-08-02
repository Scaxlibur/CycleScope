#!/usr/bin/env python3
"""Pure analysis and fit helpers for CycleScope's final system calibration.

This module never opens an instrument or a network socket.  Every archived
scope trace and FPGA frame is analyzed with WaveBench's FFT.  A deterministic
known-frequency five-harmonic least-squares fit supplies the phase and the
non-coherent ADC amplitude used for the calibration ratio.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WAVEBENCH_ROOT = PROJECT_ROOT / "tools" / "wavebench"
WAVEBENCH_SRC = WAVEBENCH_ROOT / "src"
if str(WAVEBENCH_SRC) not in sys.path:
    sys.path.insert(0, str(WAVEBENCH_SRC))

from wavebench.data.fft import analyze_fft as wavebench_analyze_fft


OUTPUT_SAMPLE_RATE_HZ = 4_062_500.0
FRAME_SAMPLES = 8192
UPSTREAM_IDENTITY = {
    "calibration_id": 25030,
    "scale_uv_per_lsb": 516,
    "offset_uv": -6761,
    "filter_profile": 1,
    "sample_rate_hz": 4_062_500,
    "frame_sample_count": 8192,
}
TRAIN_FREQUENCIES_HZ = (
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
HOLDOUT_FREQUENCIES_HZ = (
    15_000,
    75_000,
    150_000,
    250_000,
    350_000,
    425_000,
    485_000,
)
HOLDOUT_TARGET_V = 0.003
HOLDOUT_HARD_LIMIT_V = 0.005
AMPLITUDE_DEPENDENCE_HARD_PERCENT = 1.0
SUPPORTED_SOURCE_MAX_VPP_V = 0.25
EXCLUDED_COMPRESSED_CASE_IDS = (
    "m4-cross-10000Hz-450mVpp",
    "m4-cross-200000Hz-450mVpp",
    "m4-cross-500000Hz-450mVpp",
)
SCOPE_AMENDMENT_DIRECTORY = "scope-amendment-v2"
SCOPE_AMENDMENT_FILENAME = "scope-amendment.json"
SCOPE_AMENDMENT_CATALOG_FILENAME = "case-catalog-v2.json"


class CalibrationError(RuntimeError):
    """Calibration input or evidence is incomplete, unsafe, or inconsistent."""


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    milestone: str
    role: str
    frequency_hz: float | None
    source_vpp_v: float
    direction: str | None = None


def build_cases() -> tuple[CalibrationCase, ...]:
    cases: list[CalibrationCase] = [
        CalibrationCase("m2-zero", "M2", "zero", None, 0.0),
    ]
    for amplitude_mv in (20, 50, 100):
        cases.append(
            CalibrationCase(
                f"m2-low-100000Hz-{amplitude_mv:03d}mVpp",
                "M2",
                "safety",
                100_000.0,
                amplitude_mv / 1000.0,
            )
        )
    for direction, frequencies in (
        ("up", TRAIN_FREQUENCIES_HZ),
        ("down", tuple(reversed(TRAIN_FREQUENCIES_HZ))),
    ):
        for frequency in frequencies:
            cases.append(
                CalibrationCase(
                    f"m3-train-{direction}-{frequency}Hz",
                    "M3",
                    "training",
                    float(frequency),
                    0.1,
                    direction,
                )
            )
    for frequency in (10_000, 100_000, 500_000):
        cases.append(
            CalibrationCase(
                f"m4-minline-{frequency}Hz",
                "M4",
                "training",
                float(frequency),
                0.01,
            )
        )
    for frequency in (10_000, 200_000, 500_000):
        for amplitude_mv in (50, 250):
            cases.append(
                CalibrationCase(
                    f"m4-cross-{frequency}Hz-{amplitude_mv}mVpp",
                    "M4",
                    "training",
                    float(frequency),
                    amplitude_mv / 1000.0,
                )
            )
    for frequency in HOLDOUT_FREQUENCIES_HZ:
        cases.append(
            CalibrationCase(
                f"m6-holdout-{frequency}Hz",
                "M6",
                "holdout",
                float(frequency),
                0.1,
            )
        )
    if len(cases) != 44 or len({case.case_id for case in cases}) != len(cases):
        raise AssertionError("final calibration case catalog is not exact")
    return tuple(cases)


CASES = build_cases()
CASES_BY_ID = {case.case_id: case for case in CASES}
M3_CASE_IDS = tuple(case.case_id for case in CASES if case.milestone == "M3")
M4_CASE_IDS = tuple(case.case_id for case in CASES if case.milestone == "M4")
TRAINING_CASE_IDS = M3_CASE_IDS + M4_CASE_IDS
HOLDOUT_CASE_IDS = tuple(case.case_id for case in CASES if case.role == "holdout")
if set(EXCLUDED_COMPRESSED_CASE_IDS) & set(CASES_BY_ID):
    raise AssertionError("excluded compression points leaked into the active catalog")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, payload: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def write_sha256s(directory: Path) -> Path:
    lines: list[str] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(directory)}")
    target = directory / "SHA256SUMS"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def verify_sha256s(directory: Path) -> dict[str, Any]:
    manifest = directory / "SHA256SUMS"
    records: list[dict[str, Any]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or len(digest) != 64:
            raise CalibrationError(f"bad SHA256SUMS line in {manifest}")
        path = (directory / relative).resolve()
        try:
            path.relative_to(directory.resolve())
        except ValueError as error:
            raise CalibrationError("SHA256SUMS path escapes evidence root") from error
        if not path.is_file() or sha256_file(path) != digest:
            raise CalibrationError(f"SHA-256 mismatch: {path}")
        records.append({"path": relative, "sha256": digest, "size": path.stat().st_size})
    if not records:
        raise CalibrationError(f"empty SHA256SUMS: {manifest}")
    return {
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "files_verified": len(records),
        "records": records,
    }


def basic_metrics(values: np.ndarray) -> dict[str, float | int]:
    data = np.asarray(values, dtype=np.float64)
    if data.ndim != 1 or data.size < 4 or not np.all(np.isfinite(data)):
        raise CalibrationError("invalid one-dimensional waveform")
    mean = float(np.mean(data))
    return {
        "samples": int(data.size),
        "mean": mean,
        "rms_ac": float(np.sqrt(np.mean(np.square(data - mean)))),
        "rms_total": float(np.sqrt(np.mean(np.square(data)))),
        "minimum": float(np.min(data)),
        "maximum": float(np.max(data)),
        "peak_to_peak": float(np.ptp(data)),
    }


def tone_fit(values: np.ndarray, sample_rate_hz: float, frequency_hz: float) -> dict[str, Any]:
    data = np.asarray(values, dtype=np.float64)
    if data.ndim != 1 or data.size < 32 or not np.all(np.isfinite(data)):
        raise CalibrationError("invalid tone-fit waveform")
    if not 0.0 < frequency_hz < sample_rate_hz / 2.0:
        raise CalibrationError("tone frequency is outside Nyquist")
    time_axis = np.arange(data.size, dtype=np.float64) / sample_rate_hz
    columns = [np.ones(data.size, dtype=np.float64)]
    orders: list[int] = []
    for order in range(1, 6):
        harmonic_hz = frequency_hz * order
        if harmonic_hz >= sample_rate_hz / 2.0:
            break
        angle = 2.0 * math.pi * harmonic_hz * time_axis
        columns.extend((np.sin(angle), np.cos(angle)))
        orders.append(order)
    matrix = np.column_stack(columns)
    coefficients, *_ = np.linalg.lstsq(matrix, data, rcond=None)
    peaks: dict[int, float] = {}
    phases: dict[int, float] = {}
    for index, order in enumerate(orders):
        sine = float(coefficients[1 + 2 * index])
        cosine = float(coefficients[2 + 2 * index])
        peaks[order] = math.hypot(sine, cosine)
        phases[order] = math.atan2(cosine, sine)
    fitted = matrix @ coefficients
    fundamental = peaks[1]
    harmonic_power = sum(value * value for order, value in peaks.items() if order > 1)
    return {
        **basic_metrics(data),
        "sample_rate_hz": float(sample_rate_hz),
        "frequency_hz": float(frequency_hz),
        "fit_offset": float(coefficients[0]),
        "fundamental_peak": fundamental,
        "fundamental_vpp": 2.0 * fundamental,
        "fundamental_phase_rad": phases[1],
        "harmonic_peaks": {str(order): value for order, value in peaks.items() if order > 1},
        "thd_ratio": 0.0 if fundamental == 0.0 else math.sqrt(harmonic_power) / fundamental,
        "fit_residual_rms": float(np.sqrt(np.mean(np.square(data - fitted)))),
    }


def load_scope_trace(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    waveform = np.load(path, allow_pickle=False)
    if waveform.ndim != 2 or waveform.shape[1] != 2 or waveform.shape[0] < 32:
        raise CalibrationError(f"invalid WaveBench trace: {path}")
    time_s = np.asarray(waveform[:, 0], dtype=np.float64)
    values = np.asarray(waveform[:, 1], dtype=np.float64)
    intervals = np.diff(time_s)
    if not np.all(np.isfinite(waveform)) or np.any(intervals <= 0.0):
        raise CalibrationError(f"invalid WaveBench time axis: {path}")
    return time_s, values, float(1.0 / np.median(intervals))


def wavebench_fft_trace(waveform: np.ndarray, expected_frequency_hz: float) -> dict[str, Any]:
    fft = wavebench_analyze_fft(waveform, max_harmonic_order=5)
    resolution = float(fft["resolution_hz"])
    offset_bins = (
        abs(float(fft["peak_frequency_hz"]) - expected_frequency_hz) / resolution
        if resolution > 0.0
        else math.inf
    )
    if fft.get("warnings"):
        raise CalibrationError(f"WaveBench FFT warnings: {fft['warnings']}")
    if offset_bins > 0.1:
        raise CalibrationError(
            f"WaveBench scope FFT peak misses expected frequency by {offset_bins:.3f} bins"
        )
    return {**fft, "expected_frequency_offset_bins": offset_bins}


def wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def summarize_numeric(values: Iterable[float]) -> dict[str, float | int]:
    data = [float(value) for value in values]
    if not data or not all(math.isfinite(value) for value in data):
        raise CalibrationError("cannot summarize empty or non-finite values")
    median = float(statistics.median(data))
    deviations = [abs(value - median) for value in data]
    return {
        "count": len(data),
        "minimum": min(data),
        "median": median,
        "maximum": max(data),
        "mad": float(statistics.median(deviations)),
        "mean": float(statistics.mean(data)),
    }


def validate_mirror_metadata(frame_records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(frame_records) < 64:
        raise CalibrationError("fewer than 64 selected complete FPGA frames")
    identities: set[tuple[int, int, int, int, int, int]] = set()
    core_flags: set[int] = set()
    frame_ids: list[int] = []
    for frame in frame_records:
        identities.add(
            (
                int(frame["calibration_id"]),
                int(frame["scale_uv_per_lsb"]),
                int(frame["offset_uv"]),
                int(frame["filter_profile"]),
                int(frame["sample_rate_hz"]),
                int(frame["frame_sample_count"]),
            )
        )
        core_flags.add(int(frame["core_flags"]))
        frame_ids.append(int(frame["frame_id"]))
    expected = (
        UPSTREAM_IDENTITY["calibration_id"],
        UPSTREAM_IDENTITY["scale_uv_per_lsb"],
        UPSTREAM_IDENTITY["offset_uv"],
        UPSTREAM_IDENTITY["filter_profile"],
        UPSTREAM_IDENTITY["sample_rate_hz"],
        UPSTREAM_IDENTITY["frame_sample_count"],
    )
    if identities != {expected}:
        raise CalibrationError(f"upstream identity mismatch: {sorted(identities)}")
    if core_flags != {0x000C}:
        raise CalibrationError(f"unsafe or incomplete FPGA frame flags: {sorted(core_flags)}")
    if any(next_id != ((current + 1) & 0xFFFFFFFF) for current, next_id in zip(frame_ids, frame_ids[1:])):
        raise CalibrationError("selected FPGA frame IDs are not contiguous")
    return {
        "selected_frames": len(frame_records),
        "identity": dict(UPSTREAM_IDENTITY),
        "core_flags": 0x000C,
        "first_frame_id": frame_ids[0],
        "last_frame_id": frame_ids[-1],
        "contiguous": True,
    }


def analyze_zero(scope_package: Path, mirror_frames_path: Path) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    for channel in (1, 2):
        path = scope_package / f"ch{channel}.npy"
        waveform = np.load(path, allow_pickle=False)
        _time, values, _sample_rate = load_scope_trace(path)
        fft = wavebench_analyze_fft(waveform, max_harmonic_order=5)
        scope[f"ch{channel}"] = {
            "basic": basic_metrics(values),
            "wavebench_fft": fft,
        }
    frames = np.load(mirror_frames_path, allow_pickle=False)
    if frames.ndim != 2 or frames.shape[1] != FRAME_SAMPLES or frames.shape[0] < 64:
        raise CalibrationError("invalid zero-input FPGA frame array")
    frame_records: list[dict[str, Any]] = []
    for frame in frames:
        time_s = np.arange(FRAME_SAMPLES, dtype=np.float64) / OUTPUT_SAMPLE_RATE_HZ
        waveform = np.column_stack((time_s, np.asarray(frame, dtype=np.float64)))
        fft = wavebench_analyze_fft(waveform, max_harmonic_order=5)
        metrics = basic_metrics(frame)
        frame_records.append(
            {
                "mean_code": metrics["mean"],
                "rms_ac_code": metrics["rms_ac"],
                "peak_to_peak_code": metrics["peak_to_peak"],
                "wavebench_fft_peak_hz": fft["peak_frequency_hz"],
                "wavebench_fft_peak_code": fft["peak_amplitude_v"],
                "wavebench_fft_noise_floor_code": fft["noise_floor_v"],
            }
        )
    return {
        "format": "CycleScope final calibration zero analysis v1",
        "scope": scope,
        "fpga": {
            "frames": int(frames.shape[0]),
            "mean_code": summarize_numeric(item["mean_code"] for item in frame_records),
            "rms_ac_code": summarize_numeric(item["rms_ac_code"] for item in frame_records),
            "peak_to_peak_code": summarize_numeric(
                item["peak_to_peak_code"] for item in frame_records
            ),
            "wavebench_fft_peak_hz": summarize_numeric(
                item["wavebench_fft_peak_hz"] for item in frame_records
            ),
            "wavebench_fft_peak_code": summarize_numeric(
                item["wavebench_fft_peak_code"] for item in frame_records
            ),
            "wavebench_fft_noise_floor_code": summarize_numeric(
                item["wavebench_fft_noise_floor_code"] for item in frame_records
            ),
            "raw_samples_modified": False,
        },
        "screenshots_used_for_numeric_results": False,
    }


def analyze_tone(
    case: CalibrationCase,
    scope_package: Path,
    mirror_frames_path: Path,
    *,
    scope_channels: tuple[int, ...] = (1, 2),
    ch2_immediate_stop_vpp: float = 2.5,
    ch2_limit_authorization: str | None = None,
) -> dict[str, Any]:
    if case.frequency_hz is None or case.source_vpp_v <= 0.0:
        raise CalibrationError("tone analysis requires a nonzero tone case")
    if (
        not math.isfinite(ch2_immediate_stop_vpp)
        or ch2_immediate_stop_vpp < 2.5
        or ch2_immediate_stop_vpp > 4.6
    ):
        raise CalibrationError("invalid CH2 immediate-stop limit")
    if ch2_immediate_stop_vpp > 2.5 and not ch2_limit_authorization:
        raise CalibrationError("raised CH2 limit requires explicit authorization")
    if scope_channels not in ((1, 2), (2,)):
        raise CalibrationError("tone analysis scope channels must be CH1+CH2 or CH2 only")
    frequency_hz = float(case.frequency_hz)
    scope_results: dict[str, Any] = {}
    for channel in scope_channels:
        path = scope_package / f"ch{channel}.npy"
        waveform = np.load(path, allow_pickle=False)
        _time, values, sample_rate = load_scope_trace(path)
        fft = wavebench_fft_trace(waveform, frequency_hz)
        fit = tone_fit(values, sample_rate, frequency_hz)
        delta = abs(float(fft["peak_amplitude_v"]) - float(fit["fundamental_peak"]))
        relative = delta / max(float(fit["fundamental_peak"]), 1.0e-15)
        if relative > 0.02:
            raise CalibrationError(f"CH{channel} WaveBench FFT/fit amplitude differs by >2%")
        scope_results[f"ch{channel}"] = {
            "npy": str(path),
            "npy_sha256": sha256_file(path),
            "wavebench_fft": fft,
            "known_frequency_fit": fit,
            "fft_fit_relative_delta": relative,
        }

    frames = np.load(mirror_frames_path, allow_pickle=False)
    if frames.ndim != 2 or frames.shape[1] != FRAME_SAMPLES or frames.shape[0] < 64:
        raise CalibrationError("invalid tone FPGA frame array")
    adc_records: list[dict[str, Any]] = []
    time_s = np.arange(FRAME_SAMPLES, dtype=np.float64) / OUTPUT_SAMPLE_RATE_HZ
    for frame in frames:
        values = np.asarray(frame, dtype=np.float64)
        waveform = np.column_stack((time_s, values))
        fft = wavebench_analyze_fft(waveform, max_harmonic_order=5)
        fit = tone_fit(values, OUTPUT_SAMPLE_RATE_HZ, frequency_hz)
        resolution = float(fft["resolution_hz"])
        offset_bins = abs(float(fft["peak_frequency_hz"]) - frequency_hz) / resolution
        if fft.get("warnings") or offset_bins > 0.51:
            raise CalibrationError("FPGA WaveBench FFT does not identify the expected tone")
        adc_records.append(
            {
                "wavebench_fft_peak_hz": float(fft["peak_frequency_hz"]),
                "wavebench_fft_peak_code": float(fft["peak_amplitude_v"]),
                "wavebench_fft_resolution_hz": resolution,
                "wavebench_fft_offset_bins": offset_bins,
                "fit_peak_code": float(fit["fundamental_peak"]),
                "fit_phase_rad": float(fit["fundamental_phase_rad"]),
                "fit_thd_ratio": float(fit["thd_ratio"]),
                "fit_residual_rms_code": float(fit["fit_residual_rms"]),
                "mean_code": float(fit["mean"]),
                "minimum_code": float(fit["minimum"]),
                "maximum_code": float(fit["maximum"]),
            }
        )

    ch2_fft_peak = float(scope_results["ch2"]["wavebench_fft"]["peak_amplitude_v"])
    code_peak = float(statistics.median(item["fit_peak_code"] for item in adc_records))
    source_peak = case.source_vpp_v / 2.0
    if min(source_peak, ch2_fft_peak, code_peak) <= 0.0:
        raise CalibrationError("non-positive calibration amplitude")
    if scope_channels == (1, 2):
        ch1_fft_peak = float(
            scope_results["ch1"]["wavebench_fft"]["peak_amplitude_v"]
        )
        ch1_phase = float(
            scope_results["ch1"]["known_frequency_fit"]["fundamental_phase_rad"]
        )
        ch2_phase = float(
            scope_results["ch2"]["known_frequency_fit"]["fundamental_phase_rad"]
        )
        if ch1_fft_peak <= 0.0:
            raise CalibrationError("non-positive RTM CH1 calibration amplitude")
        ratios = {
            "reference_mode": "RTM_CH1_AND_DG4202_SETTING",
            "ksrc_v_per_v": ch1_fft_peak / source_peak,
            "gamp_v_per_v": ch2_fft_peak / ch1_fft_peak,
            "kadc_code_per_v": code_peak / ch2_fft_peak,
            "ke2e_code_per_v": code_peak / source_peak,
            "input_uv_per_code": source_peak * 1_000_000.0 / code_peak,
            "physical_input_uv_per_code": ch1_fft_peak * 1_000_000.0 / code_peak,
            "analog_ch2_minus_ch1_phase_deg": wrap_degrees(
                math.degrees(ch2_phase - ch1_phase)
            ),
        }
    else:
        ratios = {
            "reference_mode": "DG4202_CH1_50OHM_SETTING_CH1_NOT_CONNECTED",
            "dg_setting_to_ch2_v_per_v": ch2_fft_peak / source_peak,
            "kadc_code_per_v": code_peak / ch2_fft_peak,
            "ke2e_code_per_v": code_peak / source_peak,
            "input_uv_per_code": source_peak * 1_000_000.0 / code_peak,
            "rtm_ch1_used": False,
        }
    ch2_basic = scope_results["ch2"]["known_frequency_fit"]
    failures: list[str] = []
    warnings: list[str] = []
    if float(ch2_basic["peak_to_peak"]) > 2.35:
        warnings.append("RTM CH2 raw Vpp exceeds the 2.35 V engineering target")
    if (
        ch2_immediate_stop_vpp > 2.5
        and float(ch2_basic["peak_to_peak"]) > 2.5
    ):
        warnings.append(
            "RTM CH2 raw Vpp exceeds legacy 2.5 V stop; exact 450 mVpp point "
            "was explicitly user-authorized"
        )
    if float(ch2_basic["peak_to_peak"]) > ch2_immediate_stop_vpp:
        failures.append(
            "RTM CH2 raw Vpp exceeds the active immediate-stop limit"
        )
    if max(
        max(abs(float(item["minimum_code"])), abs(float(item["maximum_code"])))
        for item in adc_records
    ) >= 32767:
        failures.append("FPGA samples reached S16 saturation")
    return {
        "format": "CycleScope final calibration tone analysis v2",
        "case": asdict(case),
        "formal_reference": "DG4202 CH1 50-ohm setting",
        "scope_channels": list(scope_channels),
        "scope_primary": "WaveBench archived NPY + wavebench.data.fft.analyze_fft",
        "adc_primary": (
            "per-frame known-frequency five-harmonic least squares; "
            "WaveBench FFT required as frequency/amplitude cross-check"
        ),
        "scope": scope_results,
        "adc": {
            "frames": int(frames.shape[0]),
            "fit_peak_code": summarize_numeric(item["fit_peak_code"] for item in adc_records),
            "wavebench_fft_peak_hz": summarize_numeric(
                item["wavebench_fft_peak_hz"] for item in adc_records
            ),
            "wavebench_fft_peak_code": summarize_numeric(
                item["wavebench_fft_peak_code"] for item in adc_records
            ),
            "wavebench_fft_offset_bins": summarize_numeric(
                item["wavebench_fft_offset_bins"] for item in adc_records
            ),
            "fit_thd_ratio": summarize_numeric(item["fit_thd_ratio"] for item in adc_records),
            "fit_residual_rms_code": summarize_numeric(
                item["fit_residual_rms_code"] for item in adc_records
            ),
            "mean_code": summarize_numeric(item["mean_code"] for item in adc_records),
            "raw_samples_modified": False,
        },
        "ratios": ratios,
        "screenshots_used_for_numeric_results": False,
        "ch2_limits": {
            "engineering_target_vpp": 2.35,
            "legacy_immediate_stop_vpp": 2.5,
            "active_immediate_stop_vpp": ch2_immediate_stop_vpp,
            "authorization": ch2_limit_authorization,
        },
        "warnings": warnings,
        "failures": failures,
        "pass": not failures,
    }


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CalibrationError(f"JSON object required: {path}")
    return payload


def exact_point_analysis(evidence_root: Path, case_id: str) -> dict[str, Any]:
    if case_id not in CASES_BY_ID:
        raise CalibrationError(f"unknown case ID: {case_id}")
    point = evidence_root / "points" / case_id
    if not point.is_dir():
        raise CalibrationError(f"point directory is missing: {point}")
    verify_sha256s(point)
    record = read_json(point / "point.json")
    analysis = read_json(point / "analysis.json")
    if record.get("case", {}).get("case_id") != case_id:
        raise CalibrationError(f"point identity mismatch: {case_id}")
    if analysis.get("case", {}).get("case_id") != case_id:
        raise CalibrationError(f"analysis identity mismatch: {case_id}")
    if record.get("pass") is not True or analysis.get("pass") is not True:
        raise CalibrationError(f"point did not pass: {case_id}")
    return analysis


def linear_interpolate(rows: list[dict[str, Any]], key: str, frequency_hz: float) -> float:
    frequencies = [float(row["frequency_hz"]) for row in rows]
    values = [float(row[key]) for row in rows]
    if len(frequencies) < 2 or any(
        right <= left for left, right in zip(frequencies, frequencies[1:])
    ):
        raise CalibrationError("response frequencies are not strictly increasing")
    if not all(math.isfinite(value) for value in frequencies + values):
        raise CalibrationError("response contains non-finite values")
    if frequency_hz < frequencies[0] or frequency_hz > frequencies[-1]:
        raise CalibrationError("frequency is outside 10..500 kHz; extrapolation forbidden")
    return float(np.interp(frequency_hz, frequencies, values))


def circular_mean_degrees(values: Iterable[float]) -> float:
    radians = np.radians(np.asarray(list(values), dtype=np.float64))
    return float(np.degrees(np.angle(np.mean(np.exp(1j * radians)))))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_m3_draft(evidence_root: Path, output_dir: Path) -> dict[str, Any]:
    """Create the M3-only response drafts without reading M4 or holdouts."""

    if output_dir.exists():
        raise CalibrationError(f"M3 draft output already exists: {output_dir}")
    training = {
        case_id: exact_point_analysis(evidence_root, case_id)
        for case_id in M3_CASE_IDS
    }
    response_rows: list[dict[str, Any]] = []
    amplifier_rows: list[dict[str, Any]] = []
    chain_rows: list[dict[str, Any]] = []
    for frequency in TRAIN_FREQUENCIES_HZ:
        up = training[f"m3-train-up-{frequency}Hz"]["ratios"]
        down = training[f"m3-train-down-{frequency}Hz"]["ratios"]
        ke2e_up = float(up["ke2e_code_per_v"])
        ke2e_down = float(down["ke2e_code_per_v"])
        ke2e_mean = statistics.mean((ke2e_up, ke2e_down))
        response_rows.append(
            {
                "frequency_hz": float(frequency),
                "ke2e_code_per_v": ke2e_mean,
                "input_uv_per_code": 1_000_000.0 / ke2e_mean,
                "up_code_per_v": ke2e_up,
                "down_code_per_v": ke2e_down,
                "down_minus_up_percent":
                    (ke2e_down / ke2e_up - 1.0) * 100.0,
            }
        )
        amplifier_rows.append(
            {
                "frequency_hz": float(frequency),
                "gamp_v_per_v": statistics.mean(
                    (float(up["gamp_v_per_v"]),
                     float(down["gamp_v_per_v"]))
                ),
                "up_gamp_v_per_v": float(up["gamp_v_per_v"]),
                "down_gamp_v_per_v": float(down["gamp_v_per_v"]),
                "analog_ch2_minus_ch1_phase_deg": circular_mean_degrees(
                    (
                        float(up["analog_ch2_minus_ch1_phase_deg"]),
                        float(down["analog_ch2_minus_ch1_phase_deg"]),
                    )
                ),
                "phase_semantics":
                    "same-acquisition RTM CH2 minus CH1; not end-to-end phase",
            }
        )
        chain_rows.append(
            {
                "frequency_hz": float(frequency),
                "ksrc_v_per_v": statistics.mean(
                    (float(up["ksrc_v_per_v"]),
                     float(down["ksrc_v_per_v"]))
                ),
                "kadc_code_per_v": statistics.mean(
                    (float(up["kadc_code_per_v"]),
                     float(down["kadc_code_per_v"]))
                ),
                "physical_input_uv_per_code": statistics.mean(
                    (
                        float(up["physical_input_uv_per_code"]),
                        float(down["physical_input_uv_per_code"]),
                    )
                ),
            }
        )

    maximum_direction_delta = max(
        abs(float(row["down_minus_up_percent"]))
        for row in response_rows
    )
    summary = {
        "format": "CycleScope final calibration M3 draft v1",
        "status": "M3_COMPLETE_M4_NOT_READ",
        "created_at": datetime.now().astimezone().isoformat(),
        "case_ids": list(M3_CASE_IDS),
        "training_frequency_hz": [
            float(value) for value in TRAIN_FREQUENCIES_HZ
        ],
        "maximum_up_down_percent": maximum_direction_delta,
        "direction_repeat_target_percent": 0.3,
        "direction_repeat_pass": maximum_direction_delta <= 0.3,
        "m4_paths_read": False,
        "holdout_paths_read": False,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    write_csv(
        output_dir / "response-draft.csv",
        [
            "frequency_hz",
            "ke2e_code_per_v",
            "input_uv_per_code",
            "up_code_per_v",
            "down_code_per_v",
            "down_minus_up_percent",
        ],
        response_rows,
    )
    write_csv(
        output_dir / "amplifier-response-draft.csv",
        [
            "frequency_hz",
            "gamp_v_per_v",
            "up_gamp_v_per_v",
            "down_gamp_v_per_v",
            "analog_ch2_minus_ch1_phase_deg",
            "phase_semantics",
        ],
        amplifier_rows,
    )
    write_csv(
        output_dir / "chain-decomposition-draft.csv",
        [
            "frequency_hz",
            "ksrc_v_per_v",
            "kadc_code_per_v",
            "physical_input_uv_per_code",
        ],
        chain_rows,
    )
    write_json(output_dir / "m3-summary.json", summary)
    write_sha256s(output_dir)
    return summary


def build_m4_failure_diagnosis(
    evidence_root: Path, output_dir: Path
) -> dict[str, Any]:
    """Decompose the 10 kHz amplitude failure without reading holdouts."""

    if output_dir.exists():
        raise CalibrationError(
            f"M4 failure diagnosis output already exists: {output_dir}"
        )

    def verified_analysis(case_id: str, *, require_pass: bool) -> dict[str, Any]:
        point_dir = evidence_root / "points" / case_id
        verify_sha256s(point_dir)
        analysis = read_json(point_dir / "analysis.json")
        if analysis.get("case", {}).get("case_id") != case_id:
            raise CalibrationError(f"case identity mismatch: {case_id}")
        if require_pass and analysis.get("pass") is not True:
            raise CalibrationError(f"required passing point failed: {case_id}")
        return analysis

    case_groups = (
        (
            0.05,
            ("m4-cross-10000Hz-50mVpp",),
            True,
            "M4_PASS",
        ),
        (
            0.10,
            ("m3-train-up-10000Hz", "m3-train-down-10000Hz"),
            True,
            "M3_BASELINE",
        ),
        (
            0.25,
            ("m4-cross-10000Hz-250mVpp",),
            True,
            "M4_PASS",
        ),
        (
            0.45,
            ("m4-cross-10000Hz-450mVpp",),
            False,
            "M4_HARD_FAIL",
        ),
    )
    rows: list[dict[str, Any]] = []
    for source_vpp, case_ids, require_pass, status in case_groups:
        analyses = [
            verified_analysis(case_id, require_pass=require_pass)
            for case_id in case_ids
        ]

        def mean(path: tuple[str, ...]) -> float:
            values: list[float] = []
            for analysis in analyses:
                item: Any = analysis
                for key in path:
                    item = item[key]
                values.append(float(item))
            return statistics.mean(values)

        rows.append(
            {
                "source_vpp_v": source_vpp,
                "case_ids": ";".join(case_ids),
                "status": status,
                "ch1_fundamental_vpp_v": mean(
                    ("scope", "ch1", "known_frequency_fit", "fundamental_vpp")
                ),
                "ch2_fundamental_vpp_v": mean(
                    ("scope", "ch2", "known_frequency_fit", "fundamental_vpp")
                ),
                "ch2_wavebench_thd_percent": 100.0
                * mean(("scope", "ch2", "wavebench_fft", "thd_ratio")),
                "fpga_code_peak": mean(("adc", "fit_peak_code", "median")),
                "fpga_fit_thd_percent": 100.0
                * mean(("adc", "fit_thd_ratio", "median")),
                "ksrc_v_per_v": mean(("ratios", "ksrc_v_per_v")),
                "gamp_v_per_v": mean(("ratios", "gamp_v_per_v")),
                "kadc_code_per_v": mean(("ratios", "kadc_code_per_v")),
                "ke2e_code_per_v": mean(("ratios", "ke2e_code_per_v")),
            }
        )

    baseline = next(row for row in rows if row["source_vpp_v"] == 0.10)
    for row in rows:
        row["ke2e_deviation_from_100mvpp_percent"] = (
            float(row["ke2e_code_per_v"])
            / float(baseline["ke2e_code_per_v"])
            - 1.0
        ) * 100.0
    failed = next(row for row in rows if row["source_vpp_v"] == 0.45)
    stage_deltas = {
        key: (
            float(failed[key]) / float(baseline[key]) - 1.0
        )
        * 100.0
        for key in (
            "ksrc_v_per_v",
            "gamp_v_per_v",
            "kadc_code_per_v",
            "ke2e_code_per_v",
        )
    }
    predicted_ch2_with_ksrc_one = (
        0.45 * float(baseline["gamp_v_per_v"])
    )
    predicted_fpga_code_peak_with_ksrc_one = (
        0.5
        * predicted_ch2_with_ksrc_one
        * float(baseline["kadc_code_per_v"])
    )
    diagnosis = {
        "format": "CycleScope M4 10 kHz amplitude failure diagnosis v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "DIAGNOSIS_COMPLETE_CAMPAIGN_REMAINS_STOPPED",
        "frequency_hz": 10_000.0,
        "formal_reference": "DG4202 CH1 50-ohm setting",
        "baseline_source_vpp_v": 0.10,
        "failed_source_vpp_v": 0.45,
        "stage_delta_percent": stage_deltas,
        "dominant_change": "gamp_v_per_v",
        "dominant_change_interpretation": (
            "RTM CH2/CH1 analog-path gain fell by more than the source and "
            "ADC conversion changes; analog front-end compression dominates"
        ),
        "restored_input_termination_projection": {
            "assumed_ksrc_v_per_v": 1.0,
            "predicted_ch2_fundamental_vpp_v": predicted_ch2_with_ksrc_one,
            "predicted_fpga_code_peak": predicted_fpga_code_peak_with_ksrc_one,
            "projection_only": True,
            "must_be_remeasured": True,
        },
        "wavebench_numeric_source": True,
        "screenshots_used_for_numeric_results": False,
        "m5_fit_read": False,
        "holdout_paths_read": False,
        "recommended_recovery": [
            "restore and verify the intended real 50-ohm input termination",
            "repeat only the 100 kHz / 20 mVpp Ksrc gate first",
            "create a new evidence root and rerun M2 through M4 after hardware changes",
            "do not reuse the failed response draft or fit a 2-D correction",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    write_csv(
        output_dir / "m4-amplitude-slice-10khz.csv",
        list(rows[0].keys()),
        rows,
    )
    write_json(output_dir / "m4-failure-diagnosis.json", diagnosis)
    write_sha256s(output_dir)
    return diagnosis


def scope_amendment_binding(evidence_root: Path) -> dict[str, Any]:
    """Verify and return the immutable v2 scope identity used by every fit."""

    directory = evidence_root / SCOPE_AMENDMENT_DIRECTORY
    audit = verify_sha256s(directory)
    amendment_path = directory / SCOPE_AMENDMENT_FILENAME
    catalog_path = directory / SCOPE_AMENDMENT_CATALOG_FILENAME
    amendment = read_json(amendment_path)
    catalog = read_json(catalog_path)
    if catalog != catalog_payload():
        raise CalibrationError("scope amendment active case catalog changed")
    if amendment.get("format") != "CycleScope final calibration scope amendment v2":
        raise CalibrationError("scope amendment format is not v2")
    if amendment.get("status") != "ACTIVE_SCOPE_REVISED":
        raise CalibrationError("scope amendment is not active")
    if amendment.get("holdout_paths_read") is not False:
        raise CalibrationError("scope amendment must predate all holdout reads")
    source_range = amendment.get("formal_source_vpp_range")
    if (
        not isinstance(source_range, dict)
        or not math.isclose(
            float(source_range.get("maximum", math.nan)),
            SUPPORTED_SOURCE_MAX_VPP_V,
            abs_tol=1e-12,
        )
    ):
        raise CalibrationError("scope amendment maximum source Vpp changed")
    excluded = amendment.get("excluded_historical_cases")
    if not isinstance(excluded, list) or tuple(
        item.get("case_id") if isinstance(item, dict) else None for item in excluded
    ) != EXCLUDED_COMPRESSED_CASE_IDS:
        raise CalibrationError("scope amendment exclusion list changed")
    if any(
        item.get("disposition")
        != "HISTORICAL_ONLY_EXCLUDED_FROM_FIT_VALIDATION_ACCEPTANCE"
        for item in excluded
    ):
        raise CalibrationError("scope amendment exclusion disposition changed")
    if amendment.get("active_case_catalog_sha256") != sha256_file(catalog_path):
        raise CalibrationError("scope amendment catalog SHA mismatch")

    original = amendment.get("original_evidence_snapshot")
    required_original = {
        "root_sha256s_manifest": evidence_root / "SHA256SUMS",
        "campaign": evidence_root / "campaign.json",
        "case_catalog_v1": evidence_root / "case-catalog.json",
        "mandatory_m4_stop": evidence_root / "campaign-stop.json",
    }
    if not isinstance(original, dict):
        raise CalibrationError("scope amendment original evidence binding is missing")
    for key, path in required_original.items():
        record = original.get(key)
        if (
            not isinstance(record, dict)
            or record.get("path") != path.name
            or record.get("sha256") != sha256_file(path)
        ):
            raise CalibrationError(f"scope amendment original SHA mismatch: {key}")
    stop = read_json(evidence_root / "campaign-stop.json")
    if (
        stop.get("campaign_status") != "STOPPED_M4_AMPLITUDE_DEPENDENCE"
        or stop.get("failed_case_id") != EXCLUDED_COMPRESSED_CASE_IDS[0]
        or stop.get("m5_fit_created") is not False
        or stop.get("m6_holdouts_read") is not False
    ):
        raise CalibrationError("original M4 stop audit changed")

    return {
        "version": 2,
        "manifest_sha256": audit["manifest_sha256"],
        "amendment_sha256": sha256_file(amendment_path),
        "case_catalog_sha256": sha256_file(catalog_path),
        "supported_source_max_vpp_v": SUPPORTED_SOURCE_MAX_VPP_V,
        "excluded_case_ids": list(EXCLUDED_COMPRESSED_CASE_IDS),
    }


def build_scope_amendment(evidence_root: Path, output_dir: Path) -> dict[str, Any]:
    """Append the user-approved v2 scope without rewriting the stopped v1 audit."""

    if output_dir.exists():
        raise CalibrationError(f"scope amendment output already exists: {output_dir}")
    if output_dir != evidence_root / SCOPE_AMENDMENT_DIRECTORY:
        raise CalibrationError("scope amendment must use the frozen v2 directory name")
    for name in ("fit-v1", "fit-v2", "holdout-v1", "holdout-v2"):
        if (evidence_root / name).exists():
            raise CalibrationError("scope amendment must be frozen before fit/holdout artifacts")

    original_audit = verify_sha256s(evidence_root)
    stop = read_json(evidence_root / "campaign-stop.json")
    if (
        stop.get("campaign_status") != "STOPPED_M4_AMPLITUDE_DEPENDENCE"
        or stop.get("failed_case_id") != EXCLUDED_COMPRESSED_CASE_IDS[0]
        or stop.get("m5_fit_created") is not False
        or stop.get("m6_holdouts_read") is not False
    ):
        raise CalibrationError("scope amendment requires the intact pre-fit M4 stop audit")

    catalog = catalog_payload()
    output_dir.mkdir(parents=True, exist_ok=False)
    catalog_path = output_dir / SCOPE_AMENDMENT_CATALOG_FILENAME
    write_json(catalog_path, catalog, exclusive=True)
    excluded = [
        {
            "case_id": EXCLUDED_COMPRESSED_CASE_IDS[0],
            "evidence_status": "MEASURED_COMPRESSION",
            "evidence_path": f"points/{EXCLUDED_COMPRESSED_CASE_IDS[0]}",
            "observed_end_to_end_deviation_percent": float(
                stop["end_to_end_deviation_percent"]
            ),
            "disposition":
                "HISTORICAL_ONLY_EXCLUDED_FROM_FIT_VALIDATION_ACCEPTANCE",
        },
        {
            "case_id": EXCLUDED_COMPRESSED_CASE_IDS[1],
            "evidence_status": "NOT_RUN_AFTER_HARD_STOP",
            "evidence_path": None,
            "disposition":
                "HISTORICAL_ONLY_EXCLUDED_FROM_FIT_VALIDATION_ACCEPTANCE",
        },
        {
            "case_id": EXCLUDED_COMPRESSED_CASE_IDS[2],
            "evidence_status": "NOT_RUN_AFTER_HARD_STOP",
            "evidence_path": None,
            "disposition":
                "HISTORICAL_ONLY_EXCLUDED_FROM_FIT_VALIDATION_ACCEPTANCE",
        },
    ]
    original_files = {
        "root_sha256s_manifest": evidence_root / "SHA256SUMS",
        "campaign": evidence_root / "campaign.json",
        "case_catalog_v1": evidence_root / "case-catalog.json",
        "mandatory_m4_stop": evidence_root / "campaign-stop.json",
    }
    amendment = {
        "format": "CycleScope final calibration scope amendment v2",
        "status": "ACTIVE_SCOPE_REVISED",
        "created_at": datetime.now().astimezone().isoformat(),
        "user_decision": (
            "Ignore all compressed points because the analog margin does not support "
            "them and the real system will not operate at that peak-to-peak voltage."
        ),
        "formal_source_vpp_range": {
            "minimum_nonzero_calibration_point": 0.01,
            "maximum": SUPPORTED_SOURCE_MAX_VPP_V,
            "unit": "Vpp at DG4202 CH1 50-ohm setting",
        },
        "compression_policy": {
            "rule": "compressed points are outside the supported envelope",
            "excluded_from_fit": True,
            "excluded_from_validation": True,
            "excluded_from_acceptance": True,
            "retained_points_must_still_pass_gain_dependence_percent":
                AMPLITUDE_DEPENDENCE_HARD_PERCENT,
        },
        "excluded_historical_cases": excluded,
        "active_case_count": len(CASES),
        "active_m4_case_count": len(M4_CASE_IDS),
        "active_case_catalog_path": SCOPE_AMENDMENT_CATALOG_FILENAME,
        "active_case_catalog_sha256": sha256_file(catalog_path),
        "original_evidence_snapshot": {
            key: {"path": path.name, "sha256": sha256_file(path)}
            for key, path in original_files.items()
        },
        "original_root_manifest_verified_files": original_audit["files_verified"],
        "original_stop_factual_evidence_preserved": True,
        "original_stop_scope_gate_superseded_by_user": True,
        "holdout_paths_read": False,
        "fit_created": False,
        "fpga_changes": False,
    }
    write_json(output_dir / SCOPE_AMENDMENT_FILENAME, amendment, exclusive=True)
    write_sha256s(output_dir)
    binding = scope_amendment_binding(evidence_root)
    return {**amendment, "binding": binding}


def build_fit(evidence_root: Path, output_dir: Path) -> dict[str, Any]:
    """Build only from the exact training IDs; holdout paths are never enumerated."""

    if output_dir.exists():
        raise CalibrationError(f"fit output already exists: {output_dir}")
    scope_binding = scope_amendment_binding(evidence_root)
    training = {
        case_id: exact_point_analysis(evidence_root, case_id)
        for case_id in TRAINING_CASE_IDS
    }
    response_rows: list[dict[str, Any]] = []
    amplifier_rows: list[dict[str, Any]] = []
    chain_rows: list[dict[str, Any]] = []
    for frequency in TRAIN_FREQUENCIES_HZ:
        up = training[f"m3-train-up-{frequency}Hz"]["ratios"]
        down = training[f"m3-train-down-{frequency}Hz"]["ratios"]
        ke2e_up = float(up["ke2e_code_per_v"])
        ke2e_down = float(down["ke2e_code_per_v"])
        ke2e_mean = statistics.mean((ke2e_up, ke2e_down))
        response_rows.append(
            {
                "frequency_hz": float(frequency),
                "ke2e_code_per_v": ke2e_mean,
                "input_uv_per_code": 1_000_000.0 / ke2e_mean,
                "up_code_per_v": ke2e_up,
                "down_code_per_v": ke2e_down,
                "down_minus_up_percent": (ke2e_down / ke2e_up - 1.0) * 100.0,
            }
        )
        amplifier_rows.append(
            {
                "frequency_hz": float(frequency),
                "gamp_v_per_v": statistics.mean(
                    (float(up["gamp_v_per_v"]), float(down["gamp_v_per_v"]))
                ),
                "up_gamp_v_per_v": float(up["gamp_v_per_v"]),
                "down_gamp_v_per_v": float(down["gamp_v_per_v"]),
                "analog_ch2_minus_ch1_phase_deg": circular_mean_degrees(
                    (
                        float(up["analog_ch2_minus_ch1_phase_deg"]),
                        float(down["analog_ch2_minus_ch1_phase_deg"]),
                    )
                ),
                "phase_semantics": "same-acquisition RTM CH2 minus CH1; not end-to-end phase",
            }
        )
        chain_rows.append(
            {
                "frequency_hz": float(frequency),
                "ksrc_v_per_v": statistics.mean(
                    (float(up["ksrc_v_per_v"]), float(down["ksrc_v_per_v"]))
                ),
                "kadc_code_per_v": statistics.mean(
                    (float(up["kadc_code_per_v"]), float(down["kadc_code_per_v"]))
                ),
                "physical_input_uv_per_code": statistics.mean(
                    (
                        float(up["physical_input_uv_per_code"]),
                        float(down["physical_input_uv_per_code"]),
                    )
                ),
            }
        )

    amplitude_case_ids = [case_id for case_id in M4_CASE_IDS]
    amplitude_rows: list[dict[str, Any]] = []
    deviations: list[float] = []
    for case_id in amplitude_case_ids:
        analysis = training[case_id]
        case = CASES_BY_ID[case_id]
        ratio = float(analysis["ratios"]["ke2e_code_per_v"])
        baseline = linear_interpolate(response_rows, "ke2e_code_per_v", float(case.frequency_hz))
        factor = ratio / baseline
        deviation_percent = (factor - 1.0) * 100.0
        deviations.append(abs(deviation_percent))
        amplitude_rows.append(
            {
                "case_id": case_id,
                "frequency_hz": float(case.frequency_hz),
                "source_vpp_v": case.source_vpp_v,
                "ke2e_code_per_v": ratio,
                "baseline_code_per_v": baseline,
                "relative_gain_factor": factor,
                "deviation_percent": deviation_percent,
            }
        )
    maximum_amplitude_deviation = max(deviations)
    if maximum_amplitude_deviation > AMPLITUDE_DEPENDENCE_HARD_PERCENT:
        raise CalibrationError(
            f"amplitude dependence {maximum_amplitude_deviation:.6f}% exceeds 1% hard stop"
        )

    zero = exact_point_analysis(evidence_root, "m2-zero")
    zero_mean_code = float(zero["fpga"]["mean_code"]["median"])
    identity_payload = {
        "format": "CycleScope P4 local response identity v2",
        "upstream_identity": dict(UPSTREAM_IDENTITY),
        "reference_basis": "DG4202_CH1_50OHM_SETTING",
        "response_rows": response_rows,
        "zero_mean_code": zero_mean_code,
        "amplitude_model": (
            "one-dimensional frequency response; retained M4 points through "
            "250 mVpp only validate dependence"
        ),
        "scope_amendment": scope_binding,
        "phase_compensation": False,
    }
    identity_sha256 = canonical_sha256(identity_payload)
    profile_id = int(identity_sha256[:8], 16) or 1

    output_dir.mkdir(parents=True, exist_ok=False)
    write_csv(
        output_dir / "response.csv",
        [
            "frequency_hz",
            "ke2e_code_per_v",
            "input_uv_per_code",
            "up_code_per_v",
            "down_code_per_v",
            "down_minus_up_percent",
        ],
        response_rows,
    )
    write_csv(
        output_dir / "amplifier-response.csv",
        [
            "frequency_hz",
            "gamp_v_per_v",
            "up_gamp_v_per_v",
            "down_gamp_v_per_v",
            "analog_ch2_minus_ch1_phase_deg",
            "phase_semantics",
        ],
        amplifier_rows,
    )
    write_csv(
        output_dir / "chain-decomposition.csv",
        [
            "frequency_hz",
            "ksrc_v_per_v",
            "kadc_code_per_v",
            "physical_input_uv_per_code",
        ],
        chain_rows,
    )
    write_csv(
        output_dir / "amplitude-response.csv",
        [
            "case_id",
            "frequency_hz",
            "source_vpp_v",
            "ke2e_code_per_v",
            "baseline_code_per_v",
            "relative_gain_factor",
            "deviation_percent",
        ],
        amplitude_rows,
    )
    calibration = {
        "format": "CycleScope P4 local final calibration v2",
        "status": "fit-frozen-before-holdout",
        "p4_response_profile_id": profile_id,
        "identity_sha256": identity_sha256,
        "upstream_identity": dict(UPSTREAM_IDENTITY),
        "reference_basis": "DG4202_CH1_50OHM_SETTING",
        "scope_amendment": scope_binding,
        "supported_source_max_vpp_v": SUPPORTED_SOURCE_MAX_VPP_V,
        "excluded_compressed_case_ids": list(EXCLUDED_COMPRESSED_CASE_IDS),
        "valid_frequency_hz": {"minimum": 10_000.0, "maximum": 500_000.0},
        "frequency_interpolation": "piecewise linear; no extrapolation",
        "phase_compensation": False,
        "zero_mean_code": zero_mean_code,
        "maximum_amplitude_dependence_percent": maximum_amplitude_deviation,
        "training_case_ids": list(TRAINING_CASE_IDS),
        "holdout_case_ids_reserved_but_not_read": list(HOLDOUT_CASE_IDS),
    }
    write_json(output_dir / "calibration.json", calibration)
    write_json(
        output_dir / "uncertainty-draft.json",
        {
            "format": "CycleScope final calibration uncertainty draft v2",
            "p4_response_profile_id": profile_id,
            "scope_amendment": scope_binding,
            "maximum_up_down_percent": max(
                abs(float(row["down_minus_up_percent"])) for row in response_rows
            ),
            "maximum_amplitude_dependence_percent": maximum_amplitude_deviation,
            "holdout_pending": True,
        },
    )
    response_sha = sha256_file(output_dir / "response.csv")
    write_json(
        output_dir / "calibration-build-manifest.json",
        {
            "format": "CycleScope P4 local calibration build manifest v2",
            "p4_response_profile_id": profile_id,
            "identity_sha256": identity_sha256,
            "response_csv": {"path": "response.csv", "sha256": response_sha},
            "upstream_identity": dict(UPSTREAM_IDENTITY),
            "scope_amendment": scope_binding,
            "fpga_changes_required": False,
            "fpga_worktree_access_required": False,
        },
    )
    write_sha256s(output_dir)
    return calibration


def validate_holdouts(evidence_root: Path, fit_dir: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise CalibrationError(f"holdout output already exists: {output_dir}")
    verify_sha256s(fit_dir)
    calibration = read_json(fit_dir / "calibration.json")
    scope_binding = scope_amendment_binding(evidence_root)
    if calibration.get("scope_amendment") != scope_binding:
        raise CalibrationError("frozen fit does not match the active scope amendment")
    with (fit_dir / "response.csv").open("r", encoding="utf-8", newline="") as stream:
        response_rows = [dict(row) for row in csv.DictReader(stream)]
    if [float(row["frequency_hz"]) for row in response_rows] != [
        float(value) for value in TRAIN_FREQUENCIES_HZ
    ]:
        raise CalibrationError("frozen response anchor set changed")
    records: list[dict[str, Any]] = []
    for case_id in HOLDOUT_CASE_IDS:
        analysis = exact_point_analysis(evidence_root, case_id)
        case = CASES_BY_ID[case_id]
        scale_uv_per_code = linear_interpolate(
            response_rows, "input_uv_per_code", float(case.frequency_hz)
        )
        code_peak = float(analysis["adc"]["fit_peak_code"]["median"])
        predicted_vpp = 2.0 * code_peak * scale_uv_per_code / 1_000_000.0
        signed_error = predicted_vpp - case.source_vpp_v
        frequency_measured = float(analysis["adc"]["wavebench_fft_peak_hz"]["median"])
        frequency_error = abs(frequency_measured - float(case.frequency_hz))
        records.append(
            {
                "case_id": case_id,
                "frequency_hz": float(case.frequency_hz),
                "expected_source_vpp_v": case.source_vpp_v,
                "predicted_source_vpp_v": predicted_vpp,
                "signed_error_v": signed_error,
                "absolute_error_v": abs(signed_error),
                "target_3mv_pass": abs(signed_error) <= HOLDOUT_TARGET_V,
                "hard_5mv_pass": abs(signed_error) <= HOLDOUT_HARD_LIMIT_V,
                "wavebench_fft_frequency_hz": frequency_measured,
                "frequency_error_hz": frequency_error,
                "frequency_500hz_target_pass": frequency_error <= 500.0,
                "frequency_1khz_hard_pass": frequency_error <= 1000.0,
            }
        )
    hard_pass = all(
        row["hard_5mv_pass"] and row["frequency_1khz_hard_pass"] for row in records
    )
    target_pass = all(
        row["target_3mv_pass"] and row["frequency_500hz_target_pass"] for row in records
    )
    report = {
        "format": "CycleScope P4 local final calibration holdout report v2",
        "fit_frozen_before_holdout": calibration.get("status") == "fit-frozen-before-holdout",
        "holdout_refit_performed": False,
        "p4_response_profile_id": calibration["p4_response_profile_id"],
        "scope_amendment": scope_binding,
        "records": records,
        "maximum_absolute_error_v": max(float(row["absolute_error_v"]) for row in records),
        "maximum_frequency_error_hz": max(float(row["frequency_error_hz"]) for row in records),
        "target_3mv_500hz_pass": target_pass,
        "hard_5mv_1khz_pass": hard_pass,
        "pass": hard_pass,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "holdout-report.json", report)
    write_json(
        output_dir / "uncertainty.json",
        {
            "format": "CycleScope final calibration uncertainty v2",
            "p4_response_profile_id": calibration["p4_response_profile_id"],
            "scope_amendment": scope_binding,
            "maximum_absolute_holdout_error_v": report["maximum_absolute_error_v"],
            "maximum_frequency_error_hz": report["maximum_frequency_error_hz"],
            "engineering_target_pass": target_pass,
            "hard_limit_pass": hard_pass,
        },
    )
    write_sha256s(output_dir)
    if not hard_pass:
        raise CalibrationError("independent holdout exceeds the 5 mV / 1 kHz hard gate")
    return report


def catalog_payload() -> dict[str, Any]:
    return {
        "format": "CycleScope final calibration active case catalog v2",
        "formal_reference": "DG4202 CH1 50-ohm setting",
        "supported_source_max_vpp_v": SUPPORTED_SOURCE_MAX_VPP_V,
        "excluded_historical_case_ids": list(EXCLUDED_COMPRESSED_CASE_IDS),
        "compression_policy": (
            "exclude compressed points from fit, validation, and acceptance; "
            "retained points must pass the 1% gain-dependence gate"
        ),
        "upstream_identity": dict(UPSTREAM_IDENTITY),
        "cases": [asdict(case) for case in CASES],
        "counts": {
            "all": len(CASES),
            "m2": sum(case.milestone == "M2" for case in CASES),
            "m3": sum(case.milestone == "M3" for case in CASES),
            "m4": sum(case.milestone == "M4" for case in CASES),
            "m6": sum(case.milestone == "M6" for case in CASES),
        },
    }
