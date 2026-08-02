#!/usr/bin/env python3
"""Verify CycleScope ramp/sine/multitone captures transported over CSLP/LAN."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ModuleNotFoundError as error:
    np = None
    NUMPY_IMPORT_ERROR = error
else:
    NUMPY_IMPORT_ERROR = None


FRAME_SAMPLES = 8192
SAMPLE_RATE_HZ = 4_062_500
FLAG_FILTERED = 0x0004
FLAG_ADC_OVERRANGE = 0x0010
FLAG_FIFO_OVERFLOW = 0x0020
FLAG_TEST_PATTERN = 0x0040
MULTITONE_BINS = (96, 320, 736)
MULTITONE_WEIGHTS = (0.5, 0.25, 0.25)


class AnalysisError(RuntimeError):
    """Captured evidence is incomplete, inconsistent, or the wrong profile."""


def require_numpy() -> None:
    if np is None:
        raise AnalysisError(
            "NumPy is required; use the WaveBench virtual environment"
        ) from NUMPY_IMPORT_ERROR


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise AnalysisError(f"JSON evidence is not an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise AnalysisError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def load_evidence(
    capture: Path, lan_report: Path
) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    require_numpy()
    lan = read_json(lan_report)
    manifest_path = capture / "capture.json"
    manifest = read_json(manifest_path)
    if lan.get("pass") is not True or lan.get("source_mode") != "test-pattern":
        raise AnalysisError("LAN report is not a passing test-pattern run")
    if lan.get("expected_test_faults", 0) != 0:
        raise AnalysisError("waveform analysis requires a fault-free diagnostic run")
    capture_binding = lan.get("capture")
    if not isinstance(capture_binding, dict):
        raise AnalysisError("LAN report has no capture binding")
    if Path(str(capture_binding.get("directory", ""))).resolve() != capture.resolve():
        raise AnalysisError("LAN report is bound to another capture directory")
    if capture_binding.get("partial") or manifest.get("partial"):
        raise AnalysisError("capture is marked partial")
    if manifest.get("format") != "CycleScope CSLP independent complete frames v1":
        raise AnalysisError("unsupported capture format")
    if (
        manifest.get("source_mode") != "test-pattern"
        or manifest.get("frame_samples") != FRAME_SAMPLES
        or manifest.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or manifest.get("expected_test_faults", 0) != 0
    ):
        raise AnalysisError("capture manifest is not the fault-free test profile")
    for key in ("session_id", "device_boot_id", "config_id", "source_mode"):
        if manifest.get(key) != lan.get(key):
            raise AnalysisError(f"LAN report/capture {key} mismatch")
    records = manifest.get("frames")
    if not isinstance(records, list) or len(records) < 2:
        raise AnalysisError("at least two complete frames are required")
    if (
        manifest.get("frame_count") != len(records)
        or capture_binding.get("frame_count") != len(records)
    ):
        raise AnalysisError("capture frame count binding mismatch")

    frames: list[Any] = []
    required_flags = FLAG_FILTERED | FLAG_TEST_PATTERN
    forbidden_flags = FLAG_ADC_OVERRANGE | FLAG_FIFO_OVERFLOW
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("frame_index") != index:
            raise AnalysisError("capture frame index is missing or non-contiguous")
        flags = record.get("frame_flags")
        if type(flags) is not int or flags & required_flags != required_flags:
            raise AnalysisError("capture frame lacks FILTERED/TEST_PATTERN flags")
        if flags & forbidden_flags:
            raise AnalysisError("fault-free capture contains OTR/overflow flags")
        path = capture / str(record.get("file", ""))
        raw = path.read_bytes()
        if len(raw) != FRAME_SAMPLES * 2:
            raise AnalysisError(f"bad frame byte count: {path}")
        if hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            raise AnalysisError(f"frame SHA-256 mismatch: {path}")
        frames.append(np.frombuffer(raw, dtype="<i2").astype(np.float64))
    return lan, manifest, frames


def median_spectrum(frames: list[Any]) -> Any:
    require_numpy()
    spectra = []
    for frame in frames:
        centered = frame - float(np.mean(frame))
        spectra.append(2.0 * np.abs(np.fft.rfft(centered)) / FRAME_SAMPLES)
    return np.median(np.stack(spectra), axis=0)


def db_ratio(numerator: float, denominator: float) -> float:
    return 20.0 * math.log10(max(numerator, 1e-300) / max(denominator, 1e-300))


def analyze_ramp(frames: list[Any]) -> tuple[dict[str, Any], list[str]]:
    require_numpy()
    all_samples = np.concatenate(frames)
    differences = np.concatenate([np.diff(frame) for frame in frames])
    ordinary_rises = differences[(differences > 0) & (differences < 64)]
    wrap_count = int(np.count_nonzero(differences < -256))
    minimum = int(np.min(all_samples))
    maximum = int(np.max(all_samples))
    span = maximum - minimum
    unique = int(np.unique(all_samples).size)
    median_step = (
        None if ordinary_rises.size == 0 else float(np.median(ordinary_rises))
    )
    failures = []
    if minimum > -1850 or maximum < 1850 or span < 3800:
        failures.append("ramp did not cover the expected near-full 12-bit range")
    if unique < 200:
        failures.append(f"ramp has too few distinct output codes: {unique}")
    if wrap_count < len(frames):
        failures.append(f"ramp wrap count is too small: {wrap_count}")
    if median_step is None or not 12.0 <= median_step <= 20.0:
        failures.append(f"ramp ordinary step is not the expected /16 stride: {median_step}")
    return {
        "minimum": minimum,
        "maximum": maximum,
        "span": span,
        "unique_codes": unique,
        "wrap_count": wrap_count,
        "ordinary_positive_step_median": median_step,
    }, failures


def analyze_sine(
    frames: list[Any], amplitude: int, coherent_bin: int
) -> tuple[dict[str, Any], list[str]]:
    spectrum = median_spectrum(frames)
    target = float(spectrum[coherent_bin])
    residual = spectrum.copy()
    residual[0] = 0.0
    residual[coherent_bin] = 0.0
    strongest_bin = int(np.argmax(spectrum[1:]) + 1)
    strongest_spur = float(np.max(residual))
    sfdr_db = db_ratio(target, strongest_spur)
    failures = []
    if strongest_bin != coherent_bin:
        failures.append(
            f"sine strongest bin {strongest_bin} does not match {coherent_bin}"
        )
    if not 0.90 * amplitude <= target <= 1.05 * amplitude:
        failures.append(
            f"sine peak amplitude {target:.6g} is inconsistent with {amplitude}"
        )
    if sfdr_db < 30.0:
        failures.append(f"sine SFDR {sfdr_db:.3f} dB is below 30 dB")
    return {
        "coherent_bin": coherent_bin,
        "frequency_hz": coherent_bin * SAMPLE_RATE_HZ / FRAME_SAMPLES,
        "strongest_bin": strongest_bin,
        "target_peak_codes": target,
        "strongest_spur_peak_codes": strongest_spur,
        "sfdr_db": sfdr_db,
    }, failures


def analyze_multitone(
    frames: list[Any], amplitude: int
) -> tuple[dict[str, Any], list[str]]:
    spectrum = median_spectrum(frames)
    targets = [float(spectrum[index]) for index in MULTITONE_BINS]
    expected = [amplitude * weight for weight in MULTITONE_WEIGHTS]
    residual = spectrum.copy()
    residual[0] = 0.0
    for index in MULTITONE_BINS:
        residual[index] = 0.0
    strongest_spur = float(np.max(residual))
    margin_db = db_ratio(min(targets), strongest_spur)
    top_three = tuple(sorted((np.argsort(spectrum[1:])[-3:] + 1).tolist()))
    failures = []
    if top_three != tuple(sorted(MULTITONE_BINS)):
        failures.append(f"multitone strongest bins are {top_three}")
    for index, actual, wanted in zip(MULTITONE_BINS, targets, expected, strict=True):
        if not 0.85 * wanted <= actual <= 1.15 * wanted:
            failures.append(
                f"multitone bin {index} peak {actual:.6g} differs from {wanted:.6g}"
            )
    if margin_db < 25.0:
        failures.append(
            f"multitone non-target margin {margin_db:.3f} dB is below 25 dB"
        )
    return {
        "target_bins": list(MULTITONE_BINS),
        "target_frequencies_hz": [
            index * SAMPLE_RATE_HZ / FRAME_SAMPLES for index in MULTITONE_BINS
        ],
        "target_peak_codes": targets,
        "expected_peak_codes": expected,
        "strongest_bins": list(top_three),
        "strongest_non_target_peak_codes": strongest_spur,
        "non_target_margin_db": margin_db,
    }, failures


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    lan, manifest, frames = load_evidence(args.capture, args.lan_report)
    if args.mode == "ramp":
        metrics, failures = analyze_ramp(frames)
    elif args.mode == "sine":
        metrics, failures = analyze_sine(frames, args.amplitude, args.coherent_bin)
    else:
        metrics, failures = analyze_multitone(frames, args.amplitude)
    return {
        "analysis_type": "lan-test-pattern",
        "mode": args.mode,
        "pass": not failures,
        "failures": failures,
        "frame_count": len(frames),
        "amplitude": args.amplitude,
        "coherent_bin": args.coherent_bin if args.mode == "sine" else None,
        "metrics": metrics,
        "capture": str(args.capture.resolve()),
        "lan_report": str(args.lan_report.resolve()),
        "input_binding": {
            "lan_report_sha256": sha256(args.lan_report),
            "manifest_sha256": sha256(args.capture / "capture.json"),
            "session_id": lan.get("session_id"),
            "device_boot_id": lan.get("device_boot_id"),
            "config_id": manifest.get("config_id"),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("ramp", "sine", "multitone"), required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--lan-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--amplitude", type=int, default=2047)
    parser.add_argument("--coherent-bin", type=int, default=256)
    args = parser.parse_args(argv)
    if not 0 <= args.amplitude <= 2047:
        parser.error("--amplitude must be in 0..2047")
    if not 1 <= args.coherent_bin < FRAME_SAMPLES // 2:
        parser.error("--coherent-bin must lie inside the output Nyquist band")
    if args.report.exists() or args.report.is_symlink():
        parser.error(f"--report refuses to overwrite existing path: {args.report}")
    report = args.report.resolve()
    inputs = (
        args.lan_report.resolve(),
        args.capture.resolve(),
        (args.capture / "capture.json").resolve(),
    )
    if report in inputs:
        parser.error("--report must not overwrite input evidence")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = analyze(args)
    except Exception as error:
        report = {
            "analysis_type": "lan-test-pattern",
            "mode": args.mode,
            "pass": False,
            "failures": [f"{type(error).__name__}: {error}"],
        }
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    except OSError as error:
        print(f"cannot write report: {error}")
        return 1
    print(encoded)
    print("CSLP_TEST_PATTERN_PASS" if report["pass"] else "CSLP_TEST_PATTERN_FAIL")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
