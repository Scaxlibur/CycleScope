#!/usr/bin/env python3
"""Analyze saved CycleScope ADC frames against WaveBench scope evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ModuleNotFoundError as error:  # exercised by the minimal host environment
    np = None
    NUMPY_IMPORT_ERROR = error
else:
    NUMPY_IMPORT_ERROR = None


FRAME_SAMPLES = 8192
MIN_CALIBRATION_FRAMES = 21
RAW_SAMPLE_RATE_HZ = 65_000_000
SAMPLE_RATE_HZ = 4_062_500
PASSBAND_MAX_HZ = 500_000.0
STOPBAND_MIN_HZ = 1_000_000.0
STOPBAND_ADC_VPP_FLOOR_CODES = 1e-12
FOLDED_TONE_GUARD_HZ = SAMPLE_RATE_HZ / FRAME_SAMPLES
FREQUENCY_BINDING_ABS_TOLERANCE_HZ = 1e-6
MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION = 1e-4
FLAG_ADC_OVERRANGE = 0x0010
FLAG_FIFO_OVERFLOW = 0x0020
FLAG_TEST_PATTERN = 0x0040


class AnalysisError(RuntimeError):
    """Input evidence is missing, inconsistent, or unsuitable for analysis."""


class UnsafeReportPath(AnalysisError):
    """The requested output could overwrite or alias input evidence."""


def require_numpy() -> None:
    if np is None:
        raise AnalysisError(
            "NumPy is required; run this tool with tools/wavebench/.venv/bin/python"
        ) from NUMPY_IMPORT_ERROR


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise AnalysisError(f"JSON evidence is not an object: {path}")
    return value


def finite_float(value: str) -> float:
    """Parse one finite CLI float; argparse's built-in float accepts NaN/Inf."""
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid floating-point value: {value!r}"
        ) from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("value must be finite (NaN/Inf are forbidden)")
    return parsed


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise AnalysisError(f"cannot hash input evidence {path}: {error}") from error
    return digest.hexdigest()


def file_binding(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
    }


def positive_finite(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AnalysisError(f"{label} is not a finite number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise AnalysisError(f"{label} must be finite and positive")
    return parsed


def nonnegative_finite(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AnalysisError(f"{label} is not a finite number") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise AnalysisError(f"{label} must be finite and nonnegative")
    return parsed


def fold_raw_frequency_hz(input_frequency_hz: Any) -> float:
    """Fold one first-Nyquist-zone 65 MS/s input into the 4.0625 MS/s band."""
    try:
        frequency_hz = float(input_frequency_hz)
    except (TypeError, ValueError, OverflowError) as error:
        raise AnalysisError("raw input frequency is not a finite number") from error
    if (
        not math.isfinite(frequency_hz)
        or frequency_hz < 0
        or frequency_hz >= RAW_SAMPLE_RATE_HZ / 2
    ):
        raise AnalysisError(
            "raw input frequency must lie in [0, 32.5 MHz) before folding"
        )
    wrapped_hz = frequency_hz % SAMPLE_RATE_HZ
    folded_hz = min(wrapped_hz, SAMPLE_RATE_HZ - wrapped_hz)
    if not 0 <= folded_hz <= SAMPLE_RATE_HZ / 2:
        raise AnalysisError("internal frequency fold escaped the output Nyquist band")
    return float(folded_hz)


def usable_output_tone_frequency_hz(frequency_hz: Any, label: str) -> float:
    parsed_hz = positive_finite(frequency_hz, label)
    if parsed_hz > SAMPLE_RATE_HZ / 2:
        raise AnalysisError(f"{label} lies above output Nyquist")
    distance_from_nyquist_hz = SAMPLE_RATE_HZ / 2 - parsed_hz
    if (
        parsed_hz <= FOLDED_TONE_GUARD_HZ
        or distance_from_nyquist_hz <= FOLDED_TONE_GUARD_HZ
    ):
        raise AnalysisError(
            f"{label} folds too close to output DC/Nyquist for a stable tone fit: "
            f"{parsed_hz:.9g} Hz"
        )
    return parsed_hz


def usable_folded_frequency_hz(input_frequency_hz: Any, label: str) -> float:
    return usable_output_tone_frequency_hz(
        fold_raw_frequency_hz(input_frequency_hz), label
    )


def validate_tone_input_frequency(
    input_frequency_hz: Any,
    *,
    response_only: bool,
    label: str,
) -> tuple[float, float]:
    frequency_hz = positive_finite(input_frequency_hz, label)
    folded_hz = usable_folded_frequency_hz(frequency_hz, label)
    if frequency_hz > SAMPLE_RATE_HZ / 2 and not response_only:
        raise AnalysisError(
            f"{label} exceeds output Nyquist; --response-only is required"
        )
    return frequency_hz, folded_hz


def tone_report_frequency_binding(report: dict[str, Any], path: Path) -> dict[str, Any]:
    frame_count = report.get("frame_count")
    gates = report.get("gates")
    if type(frame_count) is not int or frame_count < MIN_CALIBRATION_FRAMES:
        raise AnalysisError(f"tone report has too few calibration frames: {path}")
    if (
        not isinstance(gates, dict)
        or gates.get("min_calibration_frames") != MIN_CALIBRATION_FRAMES
    ):
        raise AnalysisError(
            f"tone report is missing the calibration frame gate: {path}"
        )
    response_only = report.get("response_only")
    if type(response_only) is not bool:
        raise AnalysisError(f"tone report response_only is not boolean: {path}")
    try:
        legacy_frequency_hz = report["expected_frequency_hz"]
        input_frequency_hz = report["expected_input_frequency_hz"]
        reported_expected_folded_hz = report["expected_folded_frequency_hz"]
        scope_input_frequency_hz = report["scope_input_frequency_hz"]
        reported_scope_folded_hz = report["scope_folded_frequency_hz"]
        reported_adc_folded_hz = report["adc_folded_frequency_hz"]
    except KeyError as error:
        raise AnalysisError(
            f"tone report is missing explicit input/folded frequency binding: {path}"
        ) from error

    expected_input_hz, expected_folded_hz = validate_tone_input_frequency(
        input_frequency_hz,
        response_only=response_only,
        label=f"tone input frequency in {path}",
    )
    legacy_frequency_hz = positive_finite(
        legacy_frequency_hz, f"legacy tone frequency in {path}"
    )
    reported_expected_folded_hz = usable_output_tone_frequency_hz(
        reported_expected_folded_hz, f"reported expected folded frequency in {path}"
    )
    scope_input_frequency_hz = positive_finite(
        scope_input_frequency_hz, f"scope input frequency in {path}"
    )
    scope_folded_hz = usable_folded_frequency_hz(
        scope_input_frequency_hz, f"scope folded frequency in {path}"
    )
    reported_scope_folded_hz = usable_output_tone_frequency_hz(
        reported_scope_folded_hz, f"reported scope folded frequency in {path}"
    )
    adc_folded_hz = usable_output_tone_frequency_hz(
        reported_adc_folded_hz, f"ADC folded frequency in {path}"
    )

    bindings = (
        (legacy_frequency_hz, expected_input_hz, "legacy/input"),
        (reported_expected_folded_hz, expected_folded_hz, "expected fold"),
        (reported_scope_folded_hz, scope_folded_hz, "scope fold"),
    )
    for reported_hz, calculated_hz, name in bindings:
        if not math.isclose(
            reported_hz,
            calculated_hz,
            rel_tol=0.0,
            abs_tol=FREQUENCY_BINDING_ABS_TOLERANCE_HZ,
        ):
            raise AnalysisError(
                f"tone report {name} frequency binding mismatch: {path}"
            )
    if response_only and not math.isclose(
        adc_folded_hz,
        scope_folded_hz,
        rel_tol=0.0,
        abs_tol=FREQUENCY_BINDING_ABS_TOLERANCE_HZ,
    ):
        raise AnalysisError(f"response-only ADC/scope fold mismatch: {path}")
    return {
        "response_only": response_only,
        "input_frequency_hz": expected_input_hz,
        "expected_folded_frequency_hz": expected_folded_hz,
        "scope_input_frequency_hz": scope_input_frequency_hz,
        "scope_folded_frequency_hz": scope_folded_hz,
        "adc_folded_frequency_hz": adc_folded_hz,
    }


def verify_lan_report(path: Path) -> dict[str, Any]:
    report = read_json(path)
    if not report.get("pass"):
        raise AnalysisError(f"LAN report did not pass: {path}")
    return report


def load_capture(
    directory: Path,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], Any]]]:
    require_numpy()
    manifest_path = directory / "capture.json"
    manifest = read_json(manifest_path)
    if manifest.get("format") != "CycleScope CSLP independent complete frames v1":
        raise AnalysisError(f"unsupported capture format in {manifest_path}")
    if manifest.get("partial"):
        raise AnalysisError(f"capture is marked partial: {manifest_path}")
    if manifest.get("frame_samples") != FRAME_SAMPLES:
        raise AnalysisError("capture frame size does not match the 8192-point Profile")
    if manifest.get("sample_rate_hz") != SAMPLE_RATE_HZ:
        raise AnalysisError("capture sample rate does not match 4.0625 MS/s")
    records = manifest.get("frames")
    if not isinstance(records, list) or not records:
        raise AnalysisError("capture contains no complete frames")
    loaded = []
    for expected_index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("frame_index") != expected_index:
            raise AnalysisError("capture frame index is missing or non-contiguous")
        frame_path = directory / str(record.get("file", ""))
        raw = frame_path.read_bytes()
        if len(raw) != FRAME_SAMPLES * 2:
            raise AnalysisError(f"bad frame byte count: {frame_path}")
        if hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            raise AnalysisError(f"frame SHA-256 mismatch: {frame_path}")
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        loaded.append((record, samples))
    if manifest.get("frame_count") != len(loaded):
        raise AnalysisError("capture frame count conflicts with manifest")
    return manifest, loaded


def verify_capture_binding(
    lan_report: dict[str, Any],
    directory: Path,
    frame_count: int,
    *,
    manifest: dict[str, Any],
    frames: list[tuple[dict[str, Any], Any]],
) -> None:
    evidence = lan_report.get("capture")
    if not isinstance(evidence, dict):
        raise AnalysisError("LAN report does not contain capture evidence")
    reported_directory = Path(str(evidence.get("directory", "")))
    if reported_directory.resolve() != directory.resolve():
        raise AnalysisError("LAN report is bound to a different capture directory")
    if evidence.get("frame_count") != frame_count:
        raise AnalysisError("LAN report/capture frame count mismatch")
    if evidence.get("partial"):
        raise AnalysisError("LAN report marks the capture partial")
    if not lan_report.get("source_mode") == "real-adc":
        raise AnalysisError("calibration requires a real-adc LAN report")
    if not lan_report.get("overrange_policy") == "reject":
        raise AnalysisError("calibration requires overrange-policy=reject")
    for key in (
        "source_mode",
        "activity_policy",
        "overrange_policy",
        "session_id",
        "device_boot_id",
        "config_id",
    ):
        if not manifest.get(key) == lan_report.get(key):
            raise AnalysisError(f"LAN report/capture {key} mismatch")
    forbidden_flags = FLAG_ADC_OVERRANGE | FLAG_FIFO_OVERFLOW | FLAG_TEST_PATTERN
    for record, _samples in frames:
        if int(record.get("frame_flags", -1)) & forbidden_flags:
            raise AnalysisError("calibration capture contains forbidden frame flags")


def require_calibration_frame_count(
    frames: list[tuple[dict[str, Any], Any]],
) -> None:
    if len(frames) < MIN_CALIBRATION_FRAMES:
        raise AnalysisError(
            f"calibration needs at least {MIN_CALIBRATION_FRAMES} complete frames; "
            f"got {len(frames)}"
        )


def capture_input_binding(
    lan_report_path: Path,
    capture_directory: Path,
    manifest: dict[str, Any],
    frames: list[tuple[dict[str, Any], Any]],
    *,
    scope_npy: Path | None = None,
) -> dict[str, Any]:
    """Record immutable file digests and the identity gates already verified above."""
    identity_keys = (
        "source_mode",
        "activity_policy",
        "overrange_policy",
        "session_id",
        "device_boot_id",
        "config_id",
    )
    return {
        "lan_report": file_binding(lan_report_path),
        "capture": {
            "directory": str(capture_directory.resolve()),
            "manifest": file_binding(capture_directory / "capture.json"),
            "format": manifest["format"],
            "frame_count": len(frames),
            "verified_frame_sha256_count": len(frames),
        },
        "scope_npy": None if scope_npy is None else file_binding(scope_npy),
        "identity": {key: manifest.get(key) for key in identity_keys},
    }


def percentile_summary(values: list[float]) -> dict[str, float]:
    require_numpy()
    if not values:
        raise AnalysisError("cannot summarize an empty metric")
    array = np.asarray(values, dtype=np.float64)
    return {
        "median": float(np.median(array)),
        "p05": float(np.percentile(array, 5)),
        "p95": float(np.percentile(array, 95)),
        "min": float(np.min(array)),
        "max": float(np.max(array)),
    }


def basic_metrics(values: Any) -> dict[str, float]:
    require_numpy()
    values = np.asarray(values, dtype=np.float64)
    mean = float(np.mean(values))
    centered = values - mean
    return {
        "mean": mean,
        "minimum": float(np.min(values)),
        "maximum": float(np.max(values)),
        "peak_to_peak": float(np.max(values) - np.min(values)),
        "rms_total": float(np.sqrt(np.mean(values * values))),
        "rms_ac": float(np.sqrt(np.mean(centered * centered))),
    }


def estimate_frequency(
    values: Any,
    sample_rate_hz: float,
    expected_frequency_hz: float,
) -> float:
    require_numpy()
    if not 0 < expected_frequency_hz < sample_rate_hz / 2:
        raise AnalysisError(
            "expected frequency must lie inside the sampled Nyquist band"
        )
    count = int(values.size)
    centered = values - float(np.mean(values))
    window = np.hanning(count)
    padded_count = count * 16
    spectrum = np.abs(np.fft.rfft(centered * window, n=padded_count))
    bin_hz = sample_rate_hz / padded_count
    half_width_hz = max(2_000.0, expected_frequency_hz * 0.03)
    first = max(1, int((expected_frequency_hz - half_width_hz) / bin_hz))
    last = min(
        spectrum.size - 2, int((expected_frequency_hz + half_width_hz) / bin_hz) + 1
    )
    if first >= last:
        raise AnalysisError("frequency search window is empty")
    peak = first + int(np.argmax(spectrum[first : last + 1]))
    left = math.log(max(float(spectrum[peak - 1]), 1e-300))
    middle = math.log(max(float(spectrum[peak]), 1e-300))
    right = math.log(max(float(spectrum[peak + 1]), 1e-300))
    denominator = left - 2.0 * middle + right
    offset = 0.0 if denominator == 0 else 0.5 * (left - right) / denominator
    return float((peak + max(-1.0, min(1.0, offset))) * bin_hz)


def tone_metrics(
    values: Any, sample_rate_hz: float, frequency_hz: float
) -> dict[str, float]:
    require_numpy()
    count = int(values.size)
    time_s = np.arange(count, dtype=np.float64) / sample_rate_hz
    harmonics = [
        order for order in range(1, 6) if order * frequency_hz < sample_rate_hz / 2
    ]
    columns = [np.ones(count, dtype=np.float64)]
    for order in harmonics:
        phase = 2.0 * math.pi * order * frequency_hz * time_s
        columns.extend((np.sin(phase), np.cos(phase)))
    design = np.column_stack(columns)
    coefficients, _residuals, _rank, _singular = np.linalg.lstsq(
        design, values, rcond=None
    )
    amplitudes = []
    for harmonic_index, _order in enumerate(harmonics):
        sine = float(coefficients[1 + harmonic_index * 2])
        cosine = float(coefficients[2 + harmonic_index * 2])
        amplitudes.append(math.hypot(sine, cosine))
    fundamental = amplitudes[0]
    harmonic_rss = math.sqrt(sum(value * value for value in amplitudes[1:]))
    fundamental_fit = (
        float(coefficients[0])
        + coefficients[1] * design[:, 1]
        + coefficients[2] * design[:, 2]
    )
    residual = values - fundamental_fit
    window = np.hanning(count)
    residual_spectrum = np.abs(np.fft.rfft(residual * window))
    fundamental_bin = int(round(frequency_hz * count / sample_rate_hz))
    residual_spectrum[:2] = 0.0
    residual_spectrum[
        max(0, fundamental_bin - 3) : min(residual_spectrum.size, fundamental_bin + 4)
    ] = 0.0
    spur_amplitude = float(2.0 * np.max(residual_spectrum) / np.sum(window))
    sfdr_db = 20.0 * (
        math.log10(max(fundamental, 1e-300)) - math.log10(max(spur_amplitude, 1e-300))
    )
    metrics = basic_metrics(values)
    metrics.update(
        {
            "frequency_hz": frequency_hz,
            "fundamental_peak": fundamental,
            "fundamental_vpp": 2.0 * fundamental,
            "fundamental_rms": fundamental / math.sqrt(2.0),
            "thd_ratio": harmonic_rss / max(fundamental, 1e-300),
            "sfdr_db": sfdr_db,
            "residual_rms": float(np.sqrt(np.mean(residual * residual))),
        }
    )
    for order, amplitude in zip(harmonics, amplitudes, strict=True):
        metrics[f"harmonic_{order}_peak"] = amplitude
    return metrics


def aggregate_frame_metrics(metrics: list[dict[str, float]]) -> dict[str, Any]:
    keys = sorted(set.intersection(*(set(item) for item in metrics)))
    return {key: percentile_summary([item[key] for item in metrics]) for key in keys}


def load_scope_trace(path: Path) -> tuple[Any, Any, float]:
    require_numpy()
    try:
        data = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as error:
        raise AnalysisError(
            f"cannot read WaveBench scope NPY {path}: {error}"
        ) from error
    if (
        not isinstance(data, np.ndarray)
        or data.ndim != 2
        or data.shape[1] != 2
        or data.shape[0] < 32
    ):
        raise AnalysisError("WaveBench scope NPY must contain Nx2 time/voltage data")
    times = np.asarray(data[:, 0], dtype=np.float64)
    values = np.asarray(data[:, 1], dtype=np.float64)
    differences = np.diff(times)
    if (
        not np.all(np.isfinite(times))
        or not np.all(np.isfinite(values))
        or np.any(differences <= 0)
    ):
        raise AnalysisError(
            "WaveBench scope trace contains invalid samples or time order"
        )
    median_interval = float(np.median(differences))
    relative_deviation = float(
        np.max(np.abs(differences - median_interval)) / median_interval
    )
    if relative_deviation > MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION:
        raise AnalysisError(
            "WaveBench scope time axis is not uniformly sampled: "
            f"maximum interval deviation {relative_deviation:.6g} exceeds "
            f"{MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION:.6g}"
        )
    sample_rate_hz = float(1.0 / median_interval)
    if not math.isfinite(sample_rate_hz):
        raise AnalysisError("WaveBench scope time axis has an invalid sample rate")
    return times, values, sample_rate_hz


def analyze_zero(args: argparse.Namespace) -> dict[str, Any]:
    lan = verify_lan_report(args.lan_report)
    manifest, frames = load_capture(args.capture)
    require_calibration_frame_count(frames)
    verify_capture_binding(
        lan, args.capture, len(frames), manifest=manifest, frames=frames
    )
    frame_metrics = [basic_metrics(samples) for _record, samples in frames]
    aggregate = aggregate_frame_metrics(frame_metrics)
    failures = []
    mean_code = aggregate["mean"]["median"]
    rms_code = aggregate["rms_ac"]["median"]
    if args.max_abs_mean_code is not None and abs(mean_code) > args.max_abs_mean_code:
        failures.append(f"absolute zero mean {abs(mean_code):.6g} code exceeds limit")
    if args.max_rms_code is not None and rms_code > args.max_rms_code:
        failures.append(f"zero-input AC RMS {rms_code:.6g} code exceeds limit")
    scope = None
    scope_rate = None
    if args.scope_npy is not None:
        _times, scope_values, scope_rate = load_scope_trace(args.scope_npy)
        scope = basic_metrics(scope_values)
        scope["sample_rate_hz"] = scope_rate
    result = {
        "analysis_type": "zero",
        "pass": not failures,
        "failures": failures,
        "capture": str(args.capture.resolve()),
        "lan_report": str(args.lan_report.resolve()),
        "frame_count": len(frames),
        "adc_codes": aggregate,
        "scope_volts": scope,
        "calibration_id": int(frames[0][0]["calibration_id"]),
        "metadata_scale_uv_per_lsb": int(frames[0][0]["scale_uv_per_lsb"]),
        "metadata_offset_uv": int(frames[0][0]["offset_uv"]),
        "lan_session_id": lan.get("session_id"),
        "manifest_format": manifest["format"],
        "input_binding": capture_input_binding(
            args.lan_report,
            args.capture,
            manifest,
            frames,
            scope_npy=args.scope_npy,
        ),
        "analysis_parameters": {
            "scale_uv_per_lsb": args.scale_uv_per_lsb,
        },
        "gates": {
            "min_calibration_frames": MIN_CALIBRATION_FRAMES,
            "max_abs_mean_code": args.max_abs_mean_code,
            "max_rms_code": args.max_rms_code,
            "max_scope_interval_relative_deviation": (
                MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION
            ),
        },
    }
    if args.scale_uv_per_lsb is not None:
        scope_mean_v = 0.0 if scope is None else scope["mean"]
        result["candidate_offset_uv"] = (
            scope_mean_v * 1_000_000.0 - mean_code * args.scale_uv_per_lsb
        )
    return result


def analyze_tone(args: argparse.Namespace) -> dict[str, Any]:
    expected_input_frequency_hz, expected_folded_frequency_hz = (
        validate_tone_input_frequency(
            args.expected_frequency_hz,
            response_only=args.response_only,
            label="expected tone input frequency",
        )
    )
    lan = verify_lan_report(args.lan_report)
    manifest, frames = load_capture(args.capture)
    require_calibration_frame_count(frames)
    verify_capture_binding(
        lan, args.capture, len(frames), manifest=manifest, frames=frames
    )
    _times, scope_values, scope_rate = load_scope_trace(args.scope_npy)
    scope_frequency = estimate_frequency(
        scope_values, scope_rate, expected_input_frequency_hz
    )
    scope_folded_frequency_hz = usable_folded_frequency_hz(
        scope_frequency, "measured scope input frequency"
    )
    scope_metrics = tone_metrics(scope_values, scope_rate, scope_frequency)
    scope_metrics["sample_rate_hz"] = scope_rate
    scope_metrics["input_frequency_hz"] = scope_frequency
    scope_metrics["folded_frequency_hz"] = scope_folded_frequency_hz
    adc_items = []
    for _record, samples in frames:
        if args.response_only:
            measured_frequency = scope_folded_frequency_hz
        else:
            measured_frequency = estimate_frequency(
                samples, SAMPLE_RATE_HZ, scope_frequency
            )
        adc_items.append(tone_metrics(samples, SAMPLE_RATE_HZ, measured_frequency))
    adc = aggregate_frame_metrics(adc_items)
    adc_vpp_codes = adc["fundamental_vpp"]["median"]
    scope_vpp_v = scope_metrics["fundamental_vpp"]
    if scope_vpp_v <= 0:
        raise AnalysisError("scope tone amplitude is not positive")
    if adc_vpp_codes < 0 or (adc_vpp_codes == 0 and not args.response_only):
        raise AnalysisError("ADC tone amplitude is not positive")
    candidate_scale = (
        None if adc_vpp_codes == 0 else scope_vpp_v * 1_000_000.0 / adc_vpp_codes
    )
    metadata_scale = float(frames[0][0]["scale_uv_per_lsb"])
    metadata_vpp_v = adc_vpp_codes * metadata_scale / 1_000_000.0
    failures = []
    if abs(scope_frequency - expected_input_frequency_hz) > args.max_frequency_error_hz:
        failures.append("scope frequency differs from the programmed frequency")
    adc_frequency = adc["frequency_hz"]["median"]
    if (
        not args.response_only
        and abs(adc_frequency - scope_folded_frequency_hz) > args.max_frequency_error_hz
    ):
        failures.append("ADC frequency differs from the scope reference")
    checked_scale = args.scale_uv_per_lsb
    amplitude_error_v = None
    if checked_scale is not None:
        adc_vpp_v = adc_vpp_codes * checked_scale / 1_000_000.0
        amplitude_error_v = adc_vpp_v - scope_vpp_v
        if abs(amplitude_error_v) > args.max_amplitude_error_v:
            failures.append(
                f"ADC Vpp error {amplitude_error_v:.9g} V exceeds "
                f"{args.max_amplitude_error_v:.9g} V"
            )
    return {
        "analysis_type": "tone",
        "pass": not failures,
        "failures": failures,
        "capture": str(args.capture.resolve()),
        "scope_npy": str(args.scope_npy.resolve()),
        "lan_report": str(args.lan_report.resolve()),
        "frame_count": len(frames),
        "expected_frequency_hz": expected_input_frequency_hz,
        "expected_input_frequency_hz": expected_input_frequency_hz,
        "expected_folded_frequency_hz": expected_folded_frequency_hz,
        "scope_input_frequency_hz": scope_frequency,
        "scope_folded_frequency_hz": scope_folded_frequency_hz,
        "adc_folded_frequency_hz": adc_frequency,
        "response_only": args.response_only,
        "scope": scope_metrics,
        "adc_codes": adc,
        "candidate_scale_uv_per_lsb": candidate_scale,
        "checked_scale_uv_per_lsb": checked_scale,
        "checked_amplitude_error_v": amplitude_error_v,
        "metadata_scale_uv_per_lsb": metadata_scale,
        "metadata_vpp_v": metadata_vpp_v,
        "metadata_amplitude_error_v": metadata_vpp_v - scope_vpp_v,
        "calibration_id": int(frames[0][0]["calibration_id"]),
        "lan_session_id": lan.get("session_id"),
        "manifest_format": manifest["format"],
        "input_binding": capture_input_binding(
            args.lan_report,
            args.capture,
            manifest,
            frames,
            scope_npy=args.scope_npy,
        ),
        "analysis_parameters": {
            "response_only": args.response_only,
            "scale_uv_per_lsb": args.scale_uv_per_lsb,
            "raw_sample_rate_hz": RAW_SAMPLE_RATE_HZ,
            "output_sample_rate_hz": SAMPLE_RATE_HZ,
        },
        "gates": {
            "min_calibration_frames": MIN_CALIBRATION_FRAMES,
            "expected_input_frequency_hz": expected_input_frequency_hz,
            "expected_folded_frequency_hz": expected_folded_frequency_hz,
            "max_frequency_error_hz": args.max_frequency_error_hz,
            "max_amplitude_error_v": args.max_amplitude_error_v,
            "folded_tone_guard_hz": FOLDED_TONE_GUARD_HZ,
            "raw_nyquist_exclusive_hz": RAW_SAMPLE_RATE_HZ / 2,
            "response_only_required_above_hz": SAMPLE_RATE_HZ / 2,
            "max_scope_interval_relative_deviation": (
                MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION
            ),
        },
    }


def level_duty(values: Any) -> tuple[float, float, float]:
    require_numpy()
    low = float(np.percentile(values, 10))
    high = float(np.percentile(values, 90))
    threshold = 0.5 * (low + high)
    return float(np.mean(values > threshold)), low, high


def analyze_square(args: argparse.Namespace) -> dict[str, Any]:
    lan = verify_lan_report(args.lan_report)
    manifest, frames = load_capture(args.capture)
    require_calibration_frame_count(frames)
    verify_capture_binding(
        lan, args.capture, len(frames), manifest=manifest, frames=frames
    )
    _times, scope_values, scope_rate = load_scope_trace(args.scope_npy)
    scope_frequency = estimate_frequency(
        scope_values, scope_rate, args.expected_frequency_hz
    )
    scope_duty, scope_low, scope_high = level_duty(scope_values)
    adc_duties = []
    adc_spans = []
    adc_frequencies = []
    for _record, samples in frames:
        duty, low, high = level_duty(samples)
        adc_duties.append(duty)
        adc_spans.append(high - low)
        adc_frequencies.append(
            estimate_frequency(samples, SAMPLE_RATE_HZ, scope_frequency)
        )
    adc_duty = percentile_summary(adc_duties)
    adc_span = percentile_summary(adc_spans)
    adc_frequency = percentile_summary(adc_frequencies)
    normal_error = abs(adc_duty["median"] - scope_duty)
    inverted_error = abs(adc_duty["median"] - (1.0 - scope_duty))
    if abs(normal_error - inverted_error) < args.polarity_margin:
        polarity = "ambiguous"
    else:
        polarity = "normal" if normal_error < inverted_error else "inverted"
    expected_duty = (
        scope_duty if args.expected_polarity == "normal" else 1.0 - scope_duty
    )
    duty_error = abs(adc_duty["median"] - expected_duty)
    failures = []
    if abs(scope_frequency - args.expected_frequency_hz) > args.max_frequency_error_hz:
        failures.append(
            "scope square-wave frequency differs from the programmed frequency"
        )
    if abs(adc_frequency["median"] - scope_frequency) > args.max_frequency_error_hz:
        failures.append("ADC square-wave frequency differs from the scope reference")
    if adc_span["median"] < args.min_level_span_codes:
        failures.append("ADC square-wave level span is below the configured minimum")
    if polarity != args.expected_polarity:
        failures.append(
            f"ADC polarity is {polarity}, expected {args.expected_polarity}"
        )
    if duty_error > args.max_duty_error:
        failures.append(
            "ADC square-wave duty differs from the expected polarity reference"
        )
    return {
        "analysis_type": "square",
        "pass": not failures,
        "failures": failures,
        "capture": str(args.capture.resolve()),
        "scope_npy": str(args.scope_npy.resolve()),
        "lan_report": str(args.lan_report.resolve()),
        "frame_count": len(frames),
        "lan_session_id": lan.get("session_id"),
        "manifest_format": manifest["format"],
        "expected_frequency_hz": args.expected_frequency_hz,
        "expected_polarity": args.expected_polarity,
        "scope": {
            "frequency_hz": scope_frequency,
            "sample_rate_hz": scope_rate,
            "high_fraction": scope_duty,
            "low_v": scope_low,
            "high_v": scope_high,
        },
        "adc_codes": {
            "frequency_hz": adc_frequency,
            "high_fraction": adc_duty,
            "level_span": adc_span,
        },
        "polarity": polarity,
        "expected_duty": expected_duty,
        "duty_error": duty_error,
        "normal_duty_error": normal_error,
        "inverted_duty_error": inverted_error,
        "input_binding": capture_input_binding(
            args.lan_report,
            args.capture,
            manifest,
            frames,
            scope_npy=args.scope_npy,
        ),
        "analysis_parameters": {
            "expected_polarity": args.expected_polarity,
        },
        "gates": {
            "min_calibration_frames": MIN_CALIBRATION_FRAMES,
            "expected_frequency_hz": args.expected_frequency_hz,
            "expected_polarity": args.expected_polarity,
            "max_frequency_error_hz": args.max_frequency_error_hz,
            "max_duty_error": args.max_duty_error,
            "min_level_span_codes": args.min_level_span_codes,
            "polarity_margin": args.polarity_margin,
            "max_scope_interval_relative_deviation": (
                MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION
            ),
        },
    }


def analyze_sweep(args: argparse.Namespace) -> dict[str, Any]:
    require_numpy()
    reference_frequency_hz = positive_finite(
        args.reference_frequency_hz, "sweep reference frequency"
    )
    max_amplitude_error_v = positive_finite(
        args.max_amplitude_error_v, "sweep amplitude-error gate"
    )
    max_passband_ripple_db = positive_finite(
        args.max_passband_ripple_db, "sweep passband-ripple gate"
    )
    min_stopband_attenuation_db = positive_finite(
        args.min_stopband_attenuation_db, "sweep stopband-attenuation gate"
    )
    specification = read_json(args.points)
    raw_points = specification.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise AnalysisError("sweep points JSON must contain a non-empty points list")
    points = []
    for item in raw_points:
        if not isinstance(item, dict) or "analysis" not in item:
            raise AnalysisError("each sweep point needs an analysis path")
        path = Path(item["analysis"])
        if not path.is_absolute():
            path = args.points.parent / path
        report = read_json(path)
        if report.get("analysis_type") != "tone":
            raise AnalysisError(f"sweep point is not a tone report: {path}")
        if report.get("pass") is not True:
            raise AnalysisError(f"sweep point did not pass: {path}")
        point_class = item.get("class", "passband")
        if point_class not in ("passband", "stopband"):
            raise AnalysisError(f"invalid sweep point class {point_class!r}: {path}")
        frequency_binding = tone_report_frequency_binding(report, path)
        frequency = frequency_binding["input_frequency_hz"]
        if point_class == "passband" and frequency > PASSBAND_MAX_HZ:
            raise AnalysisError(
                f"passband point exceeds {PASSBAND_MAX_HZ:.9g} Hz: {path}"
            )
        if point_class == "stopband" and frequency < STOPBAND_MIN_HZ:
            raise AnalysisError(
                f"stopband point is below {STOPBAND_MIN_HZ:.9g} Hz: {path}"
            )
        if (
            point_class == "stopband"
            and frequency > SAMPLE_RATE_HZ / 2
            and not frequency_binding["response_only"]
        ):
            raise AnalysisError(
                f"high-frequency stopband point requires response_only: {path}"
            )
        amplitude_statistic = "median" if point_class == "passband" else "p95"
        try:
            raw_scope_vpp = report["scope"]["fundamental_vpp"]
            raw_adc_vpp = report["adc_codes"]["fundamental_vpp"][amplitude_statistic]
        except (KeyError, TypeError) as error:
            raise AnalysisError(
                f"tone report is missing sweep metrics: {path}"
            ) from error
        scope_vpp = positive_finite(raw_scope_vpp, f"scope Vpp in {path}")
        if point_class == "stopband":
            measured_adc_vpp = nonnegative_finite(raw_adc_vpp, f"ADC Vpp p95 in {path}")
            adc_vpp = max(measured_adc_vpp, STOPBAND_ADC_VPP_FLOOR_CODES)
        else:
            measured_adc_vpp = positive_finite(raw_adc_vpp, f"ADC Vpp in {path}")
            adc_vpp = measured_adc_vpp
        gain_codes_per_v = positive_finite(
            adc_vpp / scope_vpp, f"ADC/scope gain in {path}"
        )
        points.append(
            {
                "frequency_hz": frequency,
                "scope_vpp_v": scope_vpp,
                "adc_vpp_codes": adc_vpp,
                "measured_adc_vpp_codes": measured_adc_vpp,
                "adc_vpp_floor_codes": (
                    STOPBAND_ADC_VPP_FLOOR_CODES if point_class == "stopband" else None
                ),
                "gain_codes_per_v": gain_codes_per_v,
                "gain_upper_bound": (
                    gain_codes_per_v if point_class == "stopband" else None
                ),
                "class": point_class,
                "amplitude_statistic": amplitude_statistic,
                "response_only": frequency_binding["response_only"],
                "expected_folded_frequency_hz": frequency_binding[
                    "expected_folded_frequency_hz"
                ],
                "scope_input_frequency_hz": frequency_binding[
                    "scope_input_frequency_hz"
                ],
                "scope_folded_frequency_hz": frequency_binding[
                    "scope_folded_frequency_hz"
                ],
                "adc_folded_frequency_hz": frequency_binding["adc_folded_frequency_hz"],
                "analysis": str(path.resolve()),
                "analysis_sha256": file_sha256(path),
            }
        )
    passband = [point for point in points if point["class"] == "passband"]
    stopband = [point for point in points if point["class"] == "stopband"]
    if len(passband) < 2:
        raise AnalysisError("sweep needs at least two passing passband points")
    if not stopband:
        raise AnalysisError("sweep needs at least one passing stopband point")
    reference = min(
        passband,
        key=lambda point: abs(point["frequency_hz"] - reference_frequency_hz),
    )
    reference_gain = reference["gain_codes_per_v"]
    for point in points:
        point["response_db"] = 20.0 * (
            math.log10(point["gain_codes_per_v"]) - math.log10(reference_gain)
        )
        point["response_upper_bound_db"] = (
            point["response_db"] if point["class"] == "stopband" else None
        )
    input_codes = np.asarray([point["adc_vpp_codes"] for point in passband])
    reference_volts = np.asarray([point["scope_vpp_v"] for point in passband])
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        scale_v_per_code = positive_finite(
            np.dot(input_codes, reference_volts) / np.dot(input_codes, input_codes),
            "sweep fitted volts-per-code scale",
        )
    amplitude_errors = input_codes * scale_v_per_code - reference_volts
    if not np.all(np.isfinite(amplitude_errors)):
        raise AnalysisError("sweep calibration residuals are not finite")
    ripple_db = max(point["response_db"] for point in passband) - min(
        point["response_db"] for point in passband
    )
    worst_stopband_db = max(point["response_upper_bound_db"] for point in stopband)
    failures = []
    if ripple_db > max_passband_ripple_db:
        failures.append(f"passband ripple {ripple_db:.6g} dB exceeds limit")
    max_error_v = float(np.max(np.abs(amplitude_errors)))
    if max_error_v > max_amplitude_error_v:
        failures.append(f"calibration residual {max_error_v:.9g} V exceeds limit")
    if stopband and worst_stopband_db > -min_stopband_attenuation_db:
        failures.append(
            f"worst stopband response {worst_stopband_db:.6g} dB "
            f"does not reach {-min_stopband_attenuation_db:.6g} dB"
        )
    return {
        "analysis_type": "sweep",
        "pass": not failures,
        "failures": failures,
        "points": points,
        "points_json": str(args.points.resolve()),
        "requested_reference_frequency_hz": reference_frequency_hz,
        "reference_frequency_hz": reference["frequency_hz"],
        "candidate_scale_uv_per_lsb": scale_v_per_code * 1_000_000.0,
        "max_calibration_residual_v": max_error_v,
        "passband_ripple_db": ripple_db,
        "worst_stopband_response_db": worst_stopband_db,
        "worst_stopband_response_upper_bound_db": worst_stopband_db,
        "input_binding": {
            "points_json": file_binding(args.points),
            "tone_reports": [
                {
                    "path": point["analysis"],
                    "sha256": point["analysis_sha256"],
                }
                for point in points
            ],
        },
        "gates": {
            "reference_frequency_hz": reference_frequency_hz,
            "passband_max_hz": PASSBAND_MAX_HZ,
            "stopband_min_hz": STOPBAND_MIN_HZ,
            "stopband_adc_vpp_floor_codes": STOPBAND_ADC_VPP_FLOOR_CODES,
            "max_amplitude_error_v": max_amplitude_error_v,
            "max_passband_ripple_db": max_passband_ripple_db,
            "min_stopband_attenuation_db": min_stopband_attenuation_db,
        },
    }


def direct_report_input_paths(args: argparse.Namespace) -> list[Path]:
    if args.command == "sweep":
        return [args.points]
    paths = [args.lan_report, args.capture, args.capture / "capture.json"]
    scope_npy = getattr(args, "scope_npy", None)
    if scope_npy is not None:
        paths.append(scope_npy)
    return paths


def referenced_report_input_paths(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.command == "sweep":
        specification = read_json(args.points)
        raw_points = specification.get("points")
        if isinstance(raw_points, list):
            for item in raw_points:
                if not isinstance(item, dict) or not isinstance(
                    item.get("analysis"), str
                ):
                    continue
                path = Path(item["analysis"])
                paths.append(path if path.is_absolute() else args.points.parent / path)
        return paths

    manifest = read_json(args.capture / "capture.json")
    records = manifest.get("frames")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("file"), str):
                continue
            paths.append(args.capture / record["file"])
    return paths


def reject_report_input_collision(report: Path, inputs: list[Path]) -> None:
    report_resolved = report.resolve()
    for input_path in inputs:
        if report_resolved == input_path.resolve():
            raise UnsafeReportPath(
                f"report path conflicts with input evidence: {input_path.resolve()}"
            )


def validate_report_destination(args: argparse.Namespace) -> None:
    report = args.report
    reject_report_input_collision(report, direct_report_input_paths(args))
    if report.exists() or report.is_symlink():
        raise UnsafeReportPath(
            f"report path already exists; refusing overwrite: {report}"
        )
    reject_report_input_collision(report, referenced_report_input_paths(args))


def write_report_exclusive(path: Path, encoded: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
    except FileExistsError as error:
        raise UnsafeReportPath(
            f"report path appeared during analysis; refusing overwrite: {path}"
        ) from error
    except OSError as error:
        raise AnalysisError(f"cannot write analysis report {path}: {error}") from error


def add_common_capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--lan-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    zero = commands.add_parser("zero", help="analyze source-off/zero-input evidence")
    add_common_capture_arguments(zero)
    zero.add_argument("--scope-npy", type=Path)
    zero.add_argument("--scale-uv-per-lsb", type=finite_float)
    zero.add_argument("--max-abs-mean-code", type=finite_float)
    zero.add_argument("--max-rms-code", type=finite_float)

    tone = commands.add_parser("tone", help="analyze one sine-wave test point")
    add_common_capture_arguments(tone)
    tone.add_argument("--scope-npy", type=Path, required=True)
    tone.add_argument("--expected-frequency-hz", type=finite_float, required=True)
    tone.add_argument("--scale-uv-per-lsb", type=finite_float)
    tone.add_argument("--max-frequency-error-hz", type=finite_float, default=1_000.0)
    tone.add_argument("--max-amplitude-error-v", type=finite_float, default=0.005)
    tone.add_argument("--response-only", action="store_true")

    square = commands.add_parser(
        "square", help="infer ADC polarity from asymmetric duty"
    )
    add_common_capture_arguments(square)
    square.add_argument("--scope-npy", type=Path, required=True)
    square.add_argument("--expected-frequency-hz", type=finite_float, required=True)
    square.add_argument(
        "--expected-polarity", choices=("normal", "inverted"), default="normal"
    )
    square.add_argument("--polarity-margin", type=finite_float, default=0.05)
    square.add_argument("--max-frequency-error-hz", type=finite_float, default=1_000.0)
    square.add_argument("--max-duty-error", type=finite_float, default=0.05)
    square.add_argument("--min-level-span-codes", type=finite_float, default=10.0)

    sweep = commands.add_parser("sweep", help="aggregate tone reports into M7 gates")
    sweep.add_argument("--points", type=Path, required=True)
    sweep.add_argument("--report", type=Path, required=True)
    sweep.add_argument("--reference-frequency-hz", type=finite_float, default=100_000.0)
    sweep.add_argument("--max-amplitude-error-v", type=finite_float, default=0.005)
    sweep.add_argument("--max-passband-ripple-db", type=finite_float, default=0.1)
    sweep.add_argument("--min-stopband-attenuation-db", type=finite_float, default=50.0)

    args = parser.parse_args(argv)
    for name in (
        "max_abs_mean_code",
        "max_rms_code",
        "scale_uv_per_lsb",
        "max_frequency_error_hz",
        "max_amplitude_error_v",
        "polarity_margin",
        "max_duty_error",
        "min_level_span_codes",
        "reference_frequency_hz",
        "max_passband_ripple_db",
        "min_stopband_attenuation_db",
    ):
        value = getattr(args, name, None)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    expected_frequency = getattr(args, "expected_frequency_hz", None)
    if args.command == "tone":
        try:
            validate_tone_input_frequency(
                expected_frequency,
                response_only=args.response_only,
                label="--expected-frequency-hz",
            )
        except AnalysisError as error:
            parser.error(str(error))
    elif expected_frequency is not None and not (
        0 < expected_frequency < SAMPLE_RATE_HZ / 2
    ):
        parser.error("--expected-frequency-hz must lie in (0, 2.03125 MHz)")
    return args


def failed_report(command: str, error: Exception) -> dict[str, Any]:
    return {
        "analysis_type": command,
        "pass": False,
        "failures": [f"{type(error).__name__}: {error}"],
    }


def print_report(report: dict[str, Any]) -> str:
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    print(encoded)
    print("CSLP_ADC_ANALYSIS_PASS" if report["pass"] else "CSLP_ADC_ANALYSIS_FAIL")
    return encoded


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report: dict[str, Any] | None = None
    try:
        validate_report_destination(args)
    except UnsafeReportPath as error:
        print_report(failed_report(args.command, error))
        return 1
    except Exception as error:
        report = failed_report(args.command, error)

    if report is None:
        try:
            if args.command == "zero":
                report = analyze_zero(args)
            elif args.command == "tone":
                report = analyze_tone(args)
            elif args.command == "square":
                report = analyze_square(args)
            else:
                report = analyze_sweep(args)
        except Exception as error:
            report = failed_report(args.command, error)

    try:
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        report = failed_report(args.command, error)
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    try:
        write_report_exclusive(args.report, encoded)
    except Exception as error:
        print_report(failed_report(args.command, error))
        return 1
    print(encoded)
    print("CSLP_ADC_ANALYSIS_PASS" if report["pass"] else "CSLP_ADC_ANALYSIS_FAIL")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
