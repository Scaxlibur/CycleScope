#!/usr/bin/env python3
"""Offline M12 acceptance: theory manifest -> FPGA mirror -> ESP32-P4 UART.

This is intentionally a private test fixture.  It never opens a socket or an
instrument.  It verifies the P4-facing acceptance scope only: fundamental and
harmonic frequency/amplitude/order, FFT-component Vpp/RMS reconstruction, and
the raw 3P time-view envelope implied by P4's waveform projection.  Scope data
remains diagnostic evidence and is not used to redefine the manifest theory.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np


SAMPLE_COUNT = 8192
POSITIVE_BIN_COUNT = SAMPLE_COUNT // 2 + 1
MINIMUM_HZ = 10_000.0
MAXIMUM_HZ = 500_000.0
MINIMUM_PEAK_V = 0.0005
RELATIVE_PEAK_THRESHOLD = 0.005
HARMONIC_TOLERANCE_BINS = 1.5
BAND_EDGE_TOLERANCE_BINS = 0.5
MAXIMUM_CANDIDATES = 12
MAXIMUM_FORMAL_LINES = 3
RECONSTRUCTION_POINTS = 4096

MEASUREMENT_RE = re.compile(
    r"measurement:.*?frame=(?P<frame>[0-9]+).*?"
    r"F0=(?P<f0>[0-9.]+)Hz Vpp=(?P<vpp>[0-9.]+)mV "
    r"RMS=(?P<rms>[0-9.]+)mV peaks=(?P<count>[0-9]+) "
    r"P1=(?P<p1f>[0-9.]+)Hz/(?P<p1a>[0-9.]+)mVpk "
    r"P2=(?P<p2f>[0-9.]+)Hz/(?P<p2a>[0-9.]+)mVpk "
    r"P3=(?P<p3f>[0-9.]+)Hz/(?P<p3a>[0-9.]+)mVpk"
)


class AcceptanceError(RuntimeError):
    """The captured data cannot support a trustworthy M12 verdict."""


def finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(f"{field} is not numeric") from exc
    if not math.isfinite(result):
        raise AcceptanceError(f"{field} is not finite")
    return result


def summarize(values: Iterable[float]) -> dict[str, float]:
    array = np.asarray(tuple(values), dtype=np.float64)
    if array.size == 0:
        raise AcceptanceError("cannot summarize an empty value set")
    return {
        "min": float(np.min(array)),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "max": float(np.max(array)),
        "std": float(np.std(array)),
    }


def load_target(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read manifest {manifest_path}: {exc}") from exc
    tones = manifest.get("tones")
    if not isinstance(tones, list) or not 2 <= len(tones) <= MAXIMUM_FORMAL_LINES:
        raise AcceptanceError("manifest must contain H1 plus one or two target tones")
    parsed: list[dict[str, Any]] = []
    orders: set[int] = set()
    for raw in tones:
        if not isinstance(raw, dict):
            raise AcceptanceError("manifest tone is not an object")
        order = raw.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 50:
            raise AcceptanceError("manifest tone order is invalid")
        if order in orders:
            raise AcceptanceError("manifest contains a duplicate harmonic order")
        orders.add(order)
        parsed.append(
            {
                "order": order,
                "frequency_hz": finite_float(raw.get("frequency_hz"), "tone frequency"),
                "amplitude_vpk": finite_float(raw.get("peak_v"), "tone peak"),
            }
        )
    parsed.sort(key=lambda item: int(item["order"]))
    if parsed[0]["order"] != 1:
        raise AcceptanceError("manifest target tones must start with H1")
    f0 = parsed[0]["frequency_hz"]
    for tone in parsed:
        if not math.isclose(
            tone["frequency_hz"], f0 * tone["order"], rel_tol=0.0, abs_tol=1.0e-6
        ):
            raise AcceptanceError("manifest tone frequency/order relation is inconsistent")
    theory = manifest.get("theory")
    if not isinstance(theory, dict):
        raise AcceptanceError("manifest is missing theory")
    target = {
        "fundamental_hz": f0,
        "voltage_peak_to_peak_v": finite_float(theory.get("voltage_peak_to_peak_v"), "theory Vpp"),
        "true_rms_v": finite_float(theory.get("true_rms_v"), "theory RMS"),
    }
    return target, parsed


def load_mirror(directory: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    try:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        frames = np.load(directory / "complete-frames-s16le.npy", allow_pickle=False)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot load passive mirror capture: {exc}") from exc
    records = summary.get("frames")
    if not isinstance(records, list) or frames.ndim != 2 or frames.shape[1] != SAMPLE_COUNT:
        raise AcceptanceError("mirror capture has invalid frame geometry")
    if len(records) != frames.shape[0] or len(records) == 0:
        raise AcceptanceError("mirror frame metadata does not match stored sample frames")
    if frames.dtype != np.dtype("<i2"):
        raise AcceptanceError("mirror samples are not S16_LE")
    required = {"sample_rate_hz", "scale_uv_per_lsb", "offset_uv", "frame_id", "session_id", "config_id"}
    if any(not isinstance(record, dict) or not required.issubset(record) for record in records):
        raise AcceptanceError("mirror metadata is incomplete")
    return frames, records


def projection(
    samples: np.ndarray,
    *,
    volts_per_lsb: float,
    offset_v: float,
    sample_rate_hz: float,
    window: np.ndarray,
    window_sum: float,
    frequency_hz: float,
) -> tuple[float, float]:
    calibrated = samples.astype(np.float64) * volts_per_lsb + offset_v
    centered = calibrated - float(np.mean(calibrated))
    phase = 2.0 * math.pi * frequency_hz * np.arange(SAMPLE_COUNT, dtype=np.float64) / sample_rate_hz
    weighted = centered * window
    real = float(np.dot(weighted, np.cos(phase)))
    imaginary = float(np.dot(weighted, -np.sin(phase)))
    amplitude = 2.0 * math.hypot(real, imaginary) / window_sum
    return amplitude, math.atan2(imaginary, real) + 0.5 * math.pi


def candidate_peaks(spectrum: np.ndarray, bin_width_hz: float) -> list[tuple[int, float, float]]:
    first = max(1, int(math.floor(MINIMUM_HZ / bin_width_hz)))
    last = min(POSITIVE_BIN_COUNT - 2, int(math.ceil(MAXIMUM_HZ / bin_width_hz)))
    maximum = float(np.max(spectrum[first : last + 1]))
    if not maximum > 0.0:
        return []
    threshold = max(MINIMUM_PEAK_V, maximum * RELATIVE_PEAK_THRESHOLD)
    candidates: list[tuple[int, float, float]] = []
    for index in range(first, last + 1):
        amplitude = float(spectrum[index])
        if amplitude < threshold or amplitude <= spectrum[index - 1] or amplitude < spectrum[index + 1]:
            continue
        left = math.log(max(float(spectrum[index - 1]), 1.0e-20))
        center = math.log(max(amplitude, 1.0e-20))
        right = math.log(max(float(spectrum[index + 1]), 1.0e-20))
        denominator = left - 2.0 * center + right
        offset = 0.0
        if abs(denominator) > 1.0e-12:
            offset = max(-0.5, min(0.5, 0.5 * (left - right) / denominator))
        frequency = (index + offset) * bin_width_hz
        if not MINIMUM_HZ - BAND_EDGE_TOLERANCE_BINS * bin_width_hz <= frequency <= MAXIMUM_HZ + BAND_EDGE_TOLERANCE_BINS * bin_width_hz:
            continue
        candidate = (index, frequency, amplitude)
        if len(candidates) < MAXIMUM_CANDIDATES:
            candidates.append(candidate)
            continue
        weakest = min(range(len(candidates)), key=lambda position: candidates[position][2])
        if amplitude > candidates[weakest][2]:
            candidates[weakest] = candidate
    return candidates


def select_family(candidates: list[tuple[int, float, float]]) -> list[tuple[int, int, float, float]]:
    if not candidates:
        return []
    best_matches: dict[int, int] | None = None
    best_score: tuple[int, float] | None = None
    for base_index, base in enumerate(candidates):
        matches: dict[int, int] = {1: base_index}
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index == base_index or candidate[1] <= base[1]:
                continue
            order = int(round(candidate[1] / base[1]))
            if not 2 <= order <= 50:
                continue
            tolerance = HARMONIC_TOLERANCE_BINS * (candidate[1] / candidate[0])
            if abs(candidate[1] - base[1] * order) > tolerance:
                continue
            if order not in matches or candidate[2] > candidates[matches[order]][2]:
                matches[order] = candidate_index
        energy = sum(candidates[index][2] ** 2 for index in matches.values())
        score = (len(matches), energy)
        if best_score is None or score > best_score:
            best_score = score
            best_matches = matches
    assert best_matches is not None
    ordered = sorted(best_matches)
    selected = [1]
    nonfundamentals = ordered[1:]
    nonfundamentals.sort(key=lambda order: candidates[best_matches[order]][2], reverse=True)
    selected.extend(nonfundamentals[: MAXIMUM_FORMAL_LINES - 1])
    selected.sort()
    return [(order, *candidates[best_matches[order]]) for order in selected]


def analyze_frame(samples: np.ndarray, metadata: dict[str, Any]) -> dict[str, Any]:
    sample_rate_hz = finite_float(metadata["sample_rate_hz"], "sample rate")
    volts_per_lsb = finite_float(metadata["scale_uv_per_lsb"], "scale") * 1.0e-6
    offset_v = finite_float(metadata["offset_uv"], "offset") * 1.0e-6
    if sample_rate_hz * 0.5 < MAXIMUM_HZ or volts_per_lsb <= 0.0:
        raise AcceptanceError("mirror metadata is outside the P4 analysis profile")
    window = np.hanning(SAMPLE_COUNT)
    window_sum = float(np.sum(window))
    calibrated = samples.astype(np.float64) * volts_per_lsb + offset_v
    centered = calibrated - float(np.mean(calibrated))
    spectrum = np.abs(np.fft.rfft(centered * window)) * 2.0 / window_sum
    spectrum[0] *= 0.5
    spectrum[-1] *= 0.5
    bin_width_hz = sample_rate_hz / SAMPLE_COUNT
    selected = select_family(candidate_peaks(spectrum, bin_width_hz))
    if not selected:
        raise AcceptanceError("no in-band spectral candidates in a selected signal frame")
    refined: list[tuple[int, float, float]] = []
    for order, _bin, initial_frequency, candidate_amplitude in selected:
        step = 0.15 * bin_width_hz
        left = projection(samples, volts_per_lsb=volts_per_lsb, offset_v=offset_v, sample_rate_hz=sample_rate_hz, window=window, window_sum=window_sum, frequency_hz=initial_frequency - step)[0] ** 2
        center = projection(samples, volts_per_lsb=volts_per_lsb, offset_v=offset_v, sample_rate_hz=sample_rate_hz, window=window, window_sum=window_sum, frequency_hz=initial_frequency)[0] ** 2
        right = projection(samples, volts_per_lsb=volts_per_lsb, offset_v=offset_v, sample_rate_hz=sample_rate_hz, window=window, window_sum=window_sum, frequency_hz=initial_frequency + step)[0] ** 2
        denominator = left - 2.0 * center + right
        offset = 0.0 if abs(denominator) < 1.0e-20 else max(-1.0, min(1.0, 0.5 * (left - right) / denominator))
        refined.append((order, initial_frequency + offset * step, candidate_amplitude))
    weights = [amplitude * amplitude * order * order for order, _frequency, amplitude in refined]
    fundamental_hz = sum((frequency / order) * weight for (order, frequency, _amplitude), weight in zip(refined, weights, strict=True)) / sum(weights)
    lines: list[dict[str, float | int]] = []
    rms_square = 0.0
    for order, _frequency, _candidate_amplitude in refined:
        amplitude, phase = projection(
            samples,
            volts_per_lsb=volts_per_lsb,
            offset_v=offset_v,
            sample_rate_hz=sample_rate_hz,
            window=window,
            window_sum=window_sum,
            frequency_hz=fundamental_hz * order,
        )
        rms_square += amplitude * amplitude * 0.5
        lines.append(
            {
                "order": order,
                "frequency_hz": fundamental_hz * order,
                "amplitude_vpk": amplitude,
                "phase_radians": phase,
            }
        )
    phase_grid = 2.0 * math.pi * np.arange(RECONSTRUCTION_POINTS, dtype=np.float64) / RECONSTRUCTION_POINTS
    reconstruction = sum(
        float(line["amplitude_vpk"]) * np.sin(int(line["order"]) * phase_grid + float(line["phase_radians"]))
        for line in lines
    )
    return {
        "fundamental_hz": fundamental_hz,
        "voltage_peak_to_peak_v": float(np.max(reconstruction) - np.min(reconstruction)),
        "true_rms_v": math.sqrt(rms_square),
        "lines": lines,
        "waveform_projection": {
            "points": RECONSTRUCTION_POINTS,
            "voltage_peak_to_peak_v": float(np.max(reconstruction) - np.min(reconstruction)),
            "true_rms_v": float(np.sqrt(np.mean(np.square(reconstruction)))),
        },
    }


def signal_run_indices(frames: np.ndarray, records: list[dict[str, Any]], target_f0_hz: float, target_h1_vpk: float) -> list[int]:
    metadata = records[0]
    fs = finite_float(metadata["sample_rate_hz"], "sample rate")
    scale = finite_float(metadata["scale_uv_per_lsb"], "scale") * 1.0e-6
    offset = finite_float(metadata["offset_uv"], "offset") * 1.0e-6
    window = np.hanning(SAMPLE_COUNT)
    gain = float(np.sum(window))
    bin_width = fs / SAMPLE_COUNT
    center_bin = int(round(target_f0_hz / bin_width))
    if not 1 <= center_bin < POSITIVE_BIN_COUNT - 1:
        raise AcceptanceError("target fundamental is outside the mirror FFT bins")
    calibrated = frames.astype(np.float64) * scale + offset
    calibrated -= np.mean(calibrated, axis=1, keepdims=True)
    spectrum = np.abs(np.fft.rfft(calibrated * window, axis=1)) * 2.0 / gain
    gate = np.max(spectrum[:, center_bin - 1 : center_bin + 2], axis=1)
    threshold = max(MINIMUM_PEAK_V, target_h1_vpk * 0.25)
    active = [index for index, value in enumerate(gate) if value > threshold]
    if not active:
        raise AcceptanceError("no source-ON frames passed the fundamental gate")
    runs: list[list[int]] = []
    run = [active[0]]
    for index in active[1:]:
        if index == run[-1] + 1:
            run.append(index)
        else:
            runs.append(run)
            run = [index]
    runs.append(run)
    best = max(runs, key=len)
    if len(best) < 8:
        raise AcceptanceError("source-ON run is too short for stable M12 analysis")
    return best


def representative_indices(run: list[int], maximum: int = 12) -> list[int]:
    trimmed = run[3:-3] if len(run) > 8 else run
    if len(trimmed) <= maximum:
        return trimmed
    return sorted({trimmed[round(position * (len(trimmed) - 1) / (maximum - 1))] for position in range(maximum)})


def summarize_analysis(results: list[dict[str, Any]]) -> dict[str, Any]:
    expected_orders = [int(line["order"]) for line in results[0]["lines"]]
    if any([int(line["order"]) for line in result["lines"]] != expected_orders for result in results):
        raise AcceptanceError("selected harmonic family changed inside the stable source-ON run")
    lines: list[dict[str, Any]] = []
    for line_index, order in enumerate(expected_orders):
        lines.append(
            {
                "order": order,
                "frequency_hz": summarize(float(result["lines"][line_index]["frequency_hz"]) for result in results),
                "amplitude_mVpk": summarize(float(result["lines"][line_index]["amplitude_vpk"]) * 1000.0 for result in results),
                "phase_radians": summarize(float(result["lines"][line_index]["phase_radians"]) for result in results),
            }
        )
    return {
        "fundamental_hz": summarize(float(result["fundamental_hz"]) for result in results),
        "voltage_peak_to_peak_mV": summarize(float(result["voltage_peak_to_peak_v"]) * 1000.0 for result in results),
        "true_rms_mV": summarize(float(result["true_rms_v"]) * 1000.0 for result in results),
        "lines": lines,
        "waveform_projection": {
            "voltage_peak_to_peak_mV": summarize(float(result["waveform_projection"]["voltage_peak_to_peak_v"]) * 1000.0 for result in results),
            "true_rms_mV": summarize(float(result["waveform_projection"]["true_rms_v"]) * 1000.0 for result in results),
        },
    }


def phase_anchor_sample(fundamental_phase_radians: float, samples_per_period: float,
                        span_samples: float, sample_count: int) -> float:
    """Mirror waveform_projection.cpp's centered H1 rising-zero anchor."""
    if (
        not math.isfinite(fundamental_phase_radians)
        or not math.isfinite(samples_per_period)
        or not math.isfinite(span_samples)
        or samples_per_period <= 0.0
        or span_samples <= 0.0
        or sample_count < 2
    ):
        raise AcceptanceError("invalid P4 time-view phase anchor input")
    phase_to_next_rising = math.fmod(-fundamental_phase_radians, 2.0 * math.pi)
    if phase_to_next_rising < 0.0:
        phase_to_next_rising += 2.0 * math.pi
    base_anchor = phase_to_next_rising / (2.0 * math.pi) * samples_per_period
    maximum_start = float(sample_count - 1) - span_samples
    if not math.isfinite(base_anchor) or base_anchor < 0.0 or base_anchor > maximum_start:
        raise AcceptanceError("P4 time-view phase anchor is outside the frame")
    maximum_periods = math.floor((maximum_start - base_anchor) / samples_per_period)
    target_periods = (maximum_start * 0.5 - base_anchor) / samples_per_period
    centered_periods = math.floor(target_periods + 0.5) if target_periods >= 0.0 else math.ceil(target_periods - 0.5)
    centered_periods = min(max(centered_periods, 0), maximum_periods)
    anchor = base_anchor + centered_periods * samples_per_period
    if not 0.0 <= anchor and anchor + span_samples <= float(sample_count - 1):
        raise AcceptanceError("P4 time-view anchor cannot fit the 3P span")
    return anchor


def raw_time_view_metrics(samples: np.ndarray, metadata: dict[str, Any], analysis: dict[str, Any]) -> dict[str, float]:
    """Check the raw 3P envelope that P4 sends to WaveformView, not a UI image."""
    lines = analysis.get("lines")
    if not isinstance(lines, list) or not lines:
        raise AcceptanceError("P4 time-view analysis has no H1 component")
    sample_rate_hz = finite_float(metadata["sample_rate_hz"], "sample rate")
    volts_per_lsb = finite_float(metadata["scale_uv_per_lsb"], "scale") * 1.0e-6
    offset_v = finite_float(metadata["offset_uv"], "offset") * 1.0e-6
    fundamental_hz = finite_float(analysis["fundamental_hz"], "P4 waveform fundamental")
    fundamental_phase = finite_float(lines[0]["phase_radians"], "P4 waveform H1 phase")
    if sample_rate_hz <= 0.0 or volts_per_lsb <= 0.0 or fundamental_hz <= 0.0:
        raise AcceptanceError("P4 time-view metadata is invalid")
    samples_per_period = sample_rate_hz / fundamental_hz
    span_samples = 3.0 * samples_per_period
    anchor = phase_anchor_sample(
        fundamental_phase, samples_per_period, span_samples, int(samples.size)
    )
    positions = np.arange(
        math.ceil(anchor), math.floor(anchor + span_samples) + 1, dtype=np.int64
    )
    if positions.size == 0 or positions[-1] >= samples.size:
        raise AcceptanceError("P4 time-view sample positions are invalid")
    calibrated = samples.astype(np.float64) * volts_per_lsb + offset_v
    centered = calibrated - float(np.mean(calibrated))
    positions_as_float = positions.astype(np.float64)
    model = np.zeros(positions.size, dtype=np.float64)
    for line in lines:
        model += finite_float(line["amplitude_vpk"], "P4 waveform amplitude") * np.sin(
            2.0 * math.pi * finite_float(line["frequency_hz"], "P4 waveform frequency")
            * positions_as_float / sample_rate_hz
            + finite_float(line["phase_radians"], "P4 waveform phase")
        )
    raw_segment = centered[positions]
    residual = raw_segment - model
    raw_minimum = float(np.min(raw_segment))
    raw_maximum = float(np.max(raw_segment))
    maximum_magnitude = max(abs(raw_minimum), abs(raw_maximum))
    return {
        "raw_three_period_vpp_mV": (raw_maximum - raw_minimum) * 1000.0,
        "fft_model_three_period_vpp_mV": float(np.max(model) - np.min(model)) * 1000.0,
        "residual_rms_mV": float(np.sqrt(np.mean(np.square(residual)))) * 1000.0,
        "residual_peak_mV": float(np.max(np.abs(residual))) * 1000.0,
        "p4_vertical_mV_per_div": 2.0 * max(0.01, maximum_magnitude * 1.15) / 8.0 * 1000.0,
    }


def summarize_time_view(metrics: list[dict[str, float]]) -> dict[str, Any]:
    if not metrics:
        raise AcceptanceError("cannot summarize an empty P4 time-view set")
    return {
        field: summarize(metric[field] for metric in metrics)
        for field in (
            "raw_three_period_vpp_mV",
            "fft_model_three_period_vpp_mV",
            "residual_rms_mV",
            "residual_peak_mV",
            "p4_vertical_mV_per_div",
        )
    }


def parse_uart(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AcceptanceError(f"cannot read P4 UART log: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for match in MEASUREMENT_RE.finditer(text):
        count = int(match.group("count"))
        lines = []
        for index in range(1, count + 1):
            lines.append(
                {
                    "frequency_hz": float(match.group(f"p{index}f")),
                    "amplitude_mVpk": float(match.group(f"p{index}a")),
                }
            )
        rows.append(
            {
                "frame_id": int(match.group("frame")),
                "fundamental_hz": float(match.group("f0")),
                "voltage_peak_to_peak_mV": float(match.group("vpp")),
                "true_rms_mV": float(match.group("rms")),
                "lines": lines,
            }
        )
    if not rows:
        raise AcceptanceError("P4 UART log contains no measurement lines")
    return rows


def select_uart_rows(rows: list[dict[str, Any]], target: dict[str, Any], tones: list[dict[str, Any]], frequency_tolerance_hz: float) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    source_on_threshold_mV = max(
        MINIMUM_PEAK_V * 1000.0,
        float(tones[0]["amplitude_vpk"]) * 1000.0 * 0.25,
    )
    for row in rows:
        if len(row["lines"]) != len(tones):
            continue
        # UART reports are sparse.  Do not let the source-on transition's
        # low-amplitude first report enter a steady-state comparison merely
        # because its frequency family already matches the target.
        if float(row["lines"][0]["amplitude_mVpk"]) <= source_on_threshold_mV:
            continue
        if abs(float(row["fundamental_hz"]) - float(target["fundamental_hz"])) > frequency_tolerance_hz:
            continue
        if any(
            abs(float(line["frequency_hz"]) - float(tone["frequency_hz"])) > frequency_tolerance_hz
            for line, tone in zip(row["lines"], tones, strict=True)
        ):
            continue
        selected.append(row)
    if not selected:
        raise AcceptanceError("no P4 UART measurement passed the target source-ON gate")
    return selected


def summarize_uart(rows: list[dict[str, Any]], tones: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "frames": [int(row["frame_id"]) for row in rows],
        "fundamental_hz": summarize(float(row["fundamental_hz"]) for row in rows),
        "voltage_peak_to_peak_mV": summarize(float(row["voltage_peak_to_peak_mV"]) for row in rows),
        "true_rms_mV": summarize(float(row["true_rms_mV"]) for row in rows),
        "lines": [
            {
                "order": int(tone["order"]),
                "frequency_hz": summarize(float(row["lines"][index]["frequency_hz"]) for row in rows),
                "amplitude_mVpk": summarize(float(row["lines"][index]["amplitude_mVpk"]) for row in rows),
            }
            for index, tone in enumerate(tones)
        ],
    }


def compare_to_theory(
    source: str,
    measured: dict[str, Any],
    target: dict[str, Any],
    tones: list[dict[str, Any]],
    *,
    frequency_tolerance_hz: float,
    amplitude_tolerance_mV: float,
) -> list[str]:
    failures: list[str] = []
    if abs(measured["fundamental_hz"]["median"] - target["fundamental_hz"]) > frequency_tolerance_hz:
        failures.append(f"{source} fundamental exceeds frequency tolerance")
    if abs(measured["voltage_peak_to_peak_mV"]["median"] - target["voltage_peak_to_peak_v"] * 1000.0) > amplitude_tolerance_mV:
        failures.append(f"{source} reconstructed Vpp exceeds amplitude tolerance")
    if abs(measured["true_rms_mV"]["median"] - target["true_rms_v"] * 1000.0) > amplitude_tolerance_mV:
        failures.append(f"{source} reconstructed RMS exceeds amplitude tolerance")
    if [int(line["order"]) for line in measured["lines"]] != [int(tone["order"]) for tone in tones]:
        failures.append(f"{source} harmonic orders do not match target")
        return failures
    for line, tone in zip(measured["lines"], tones, strict=True):
        if abs(line["frequency_hz"]["median"] - tone["frequency_hz"]) > frequency_tolerance_hz:
            failures.append(f"{source} H{tone['order']} frequency exceeds tolerance")
        if abs(line["amplitude_mVpk"]["median"] - tone["amplitude_vpk"] * 1000.0) > amplitude_tolerance_mV:
            failures.append(f"{source} H{tone['order']} amplitude exceeds tolerance")
    return failures


def compare_p4_to_mirror(p4: dict[str, Any], mirror: dict[str, Any], tolerance_mV: float, frequency_tolerance_hz: float) -> list[str]:
    failures: list[str] = []
    for field in ("voltage_peak_to_peak_mV", "true_rms_mV"):
        if abs(p4[field]["median"] - mirror[field]["median"]) > tolerance_mV:
            failures.append(f"P4 and mirror {field} differ beyond consistency tolerance")
    if abs(p4["fundamental_hz"]["median"] - mirror["fundamental_hz"]["median"]) > frequency_tolerance_hz:
        failures.append("P4 and mirror fundamental frequency differ beyond consistency tolerance")
    if len(p4["lines"]) != len(mirror["lines"]):
        return failures + ["P4 and mirror line counts differ"]
    for p4_line, mirror_line in zip(p4["lines"], mirror["lines"], strict=True):
        if p4_line["order"] != mirror_line["order"]:
            failures.append("P4 and mirror harmonic orders differ")
            continue
        if abs(p4_line["frequency_hz"]["median"] - mirror_line["frequency_hz"]["median"]) > frequency_tolerance_hz:
            failures.append(f"P4 and mirror H{p4_line['order']} frequency differ beyond consistency tolerance")
        if abs(p4_line["amplitude_mVpk"]["median"] - mirror_line["amplitude_mVpk"]["median"]) > tolerance_mV:
            failures.append(f"P4 and mirror H{p4_line['order']} amplitude differ beyond consistency tolerance")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mirror-dir", type=Path, required=True)
    parser.add_argument("--uart-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frequency-tolerance-hz", type=float, default=1000.0)
    parser.add_argument("--amplitude-tolerance-mv", type=float, default=5.0)
    parser.add_argument("--p4-mirror-amplitude-tolerance-mv", type=float, default=1.0)
    parser.add_argument("--p4-mirror-frequency-tolerance-hz", type=float, default=2.0)
    args = parser.parse_args()
    if min(
        args.frequency_tolerance_hz,
        args.amplitude_tolerance_mv,
        args.p4_mirror_amplitude_tolerance_mv,
        args.p4_mirror_frequency_tolerance_hz,
    ) <= 0.0:
        parser.error("all tolerances must be positive")

    payload: dict[str, Any] = {
        "format": "CycleScope M12 P4 acceptance v1",
        "manifest": str(args.manifest),
        "mirror_dir": str(args.mirror_dir),
        "uart_log": str(args.uart_log),
        "pass": False,
        "failures": [],
    }
    try:
        target, tones = load_target(args.manifest)
        frames, records = load_mirror(args.mirror_dir)
        active_run = signal_run_indices(frames, records, target["fundamental_hz"], tones[0]["amplitude_vpk"])
        indexes = representative_indices(active_run)
        analyses = [analyze_frame(frames[index], records[index]) for index in indexes]
        mirror = summarize_analysis(analyses)
        time_view = summarize_time_view(
            [
                raw_time_view_metrics(frames[index], records[index], analysis)
                for index, analysis in zip(indexes, analyses, strict=True)
            ]
        )
        uart_rows = select_uart_rows(
            parse_uart(args.uart_log), target, tones, args.frequency_tolerance_hz
        )
        p4 = summarize_uart(uart_rows, tones)
        failures = compare_to_theory(
            "mirror", mirror, target, tones,
            frequency_tolerance_hz=args.frequency_tolerance_hz,
            amplitude_tolerance_mV=args.amplitude_tolerance_mv,
        )
        failures.extend(
            compare_to_theory(
                "P4", p4, target, tones,
                frequency_tolerance_hz=args.frequency_tolerance_hz,
                amplitude_tolerance_mV=args.amplitude_tolerance_mv,
            )
        )
        failures.extend(
            compare_p4_to_mirror(
                p4,
                mirror,
                args.p4_mirror_amplitude_tolerance_mv,
                args.p4_mirror_frequency_tolerance_hz,
            )
        )
        projected_vpp = mirror["waveform_projection"]["voltage_peak_to_peak_mV"]["median"]
        projected_rms = mirror["waveform_projection"]["true_rms_mV"]["median"]
        if abs(projected_vpp - mirror["voltage_peak_to_peak_mV"]["median"]) > 0.001:
            failures.append("waveform projection Vpp is inconsistent with FFT reconstruction")
        if abs(projected_rms - mirror["true_rms_mV"]["median"]) > 0.001:
            failures.append("waveform projection RMS is inconsistent with FFT components")
        payload.update(
            {
                "target": {
                    "fundamental_hz": target["fundamental_hz"],
                    "voltage_peak_to_peak_mV": target["voltage_peak_to_peak_v"] * 1000.0,
                    "true_rms_mV": target["true_rms_v"] * 1000.0,
                    "lines": [
                        {
                            "order": tone["order"],
                            "frequency_hz": tone["frequency_hz"],
                            "amplitude_mVpk": tone["amplitude_vpk"] * 1000.0,
                        }
                        for tone in tones
                    ],
                },
                "mirror": mirror,
                "p4_time_view_from_mirror": time_view,
                "p4": p4,
                "selected_mirror_frames": {
                    "source_on_run_frame_ids": [
                        int(records[active_run[0]]["frame_id"]),
                        int(records[active_run[-1]]["frame_id"]),
                    ],
                    "source_on_complete_frames": len(active_run),
                    "analyzed_frame_ids": [int(records[index]["frame_id"]) for index in indexes],
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
    except AcceptanceError as error:
        payload["failures"] = [str(error)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
