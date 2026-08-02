#!/usr/bin/env python3
"""Generate the deterministic offline M11 sine/ARB campaign matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np


FPGA_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_PLAN = FPGA_ROOT.parent / "public" / "信号前端测量方案.md"
M11_PLAN = FPGA_ROOT / "tool-of-rei" / "M11-真实全链路FIR与信号处理压力测试计划.md"
FIR_COEFFS = FPGA_ROOT / "Zynq_7010_PL" / "rtl" / "fir_coeffs_pkg.sv"
ARB_POINTS = 16_384
ARB_REPEAT_HZ = 500.0
ARB_SAMPLE_RATE_HZ = ARB_POINTS * ARB_REPEAT_HZ
MAX_SOURCE_VPP = 0.5
Q_SCALE = float(1 << 17)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_fir_stages() -> list[list[int]]:
    text = FIR_COEFFS.read_text(encoding="utf-8")
    stages: list[list[int]] = []
    for stage, taps in ((1, 21), (2, 31), (3, 79)):
        match = re.search(
            rf"STAGE{stage}_COEFFS\s*\[[^]]+\]\s*=\s*'\{{(.*?)\}};",
            text,
            flags=re.DOTALL,
        )
        if match is None:
            raise RuntimeError(f"cannot parse STAGE{stage}_COEFFS")
        values = [
            -int(value) if sign == "-" else int(value)
            for sign, value in re.findall(r"(-?)18'sd(\d+)", match.group(1))
        ]
        if len(values) != taps or sum(values) != 1 << 17:
            raise RuntimeError(f"invalid stage {stage} coefficient set")
        stages.append(values)
    return stages


def response(coefficients: list[int], frequency_hz: float, sample_rate_hz: float) -> complex:
    phase = 2.0 * math.pi * frequency_hz / sample_rate_hz
    return sum(
        coefficient / Q_SCALE * np.exp(-1j * phase * index)
        for index, coefficient in enumerate(coefficients)
    )


def alias_frequency(frequency_hz: float, sample_rate_hz: float) -> float:
    wrapped = frequency_hz % sample_rate_hz
    return min(wrapped, sample_rate_hz - wrapped)


def full_response(stages: list[list[int]], frequency_hz: float) -> float:
    amplitude = abs(response(stages[0], frequency_hz, 65_000_000.0))
    amplitude *= abs(
        response(
            stages[1],
            alias_frequency(frequency_hz, 16_250_000.0),
            16_250_000.0,
        )
    )
    amplitude *= abs(
        response(
            stages[2],
            alias_frequency(frequency_hz, 4_062_500.0),
            4_062_500.0,
        )
    )
    return float(amplitude)


def worst_stopband_points(stages: list[list[int]]) -> list[dict[str, float]]:
    frequencies = np.arange(1_000_000.0, 3_000_000.0 + 1.0, 500.0)
    amplitudes = np.asarray([full_response(stages, value) for value in frequencies])
    candidates: list[tuple[float, float]] = []
    for index in range(1, len(frequencies) - 1):
        if amplitudes[index] >= amplitudes[index - 1] and amplitudes[index] >= amplitudes[index + 1]:
            candidates.append((float(amplitudes[index]), float(frequencies[index])))
    candidates.sort(reverse=True)
    selected: list[tuple[float, float]] = []
    for amplitude, frequency in candidates:
        if all(abs(frequency - other_frequency) >= 5_000.0 for _, other_frequency in selected):
            selected.append((amplitude, frequency))
        if len(selected) == 8:
            break
    if len(selected) != 8:
        raise RuntimeError("could not select eight separated stopband residual maxima")
    return [
        {
            "frequency_hz": frequency,
            "theoretical_amplitude": amplitude,
            "theoretical_db": 20.0 * math.log10(max(amplitude, 1e-300)),
        }
        for amplitude, frequency in selected
    ]


def sine_point(stage: str, case_id: str, frequency_hz: float, vpp_v: float, **extra: Any) -> dict[str, Any]:
    if not 0 < vpp_v <= MAX_SOURCE_VPP:
        raise ValueError(f"{case_id}: Vpp is outside M11 source range")
    return {
        "stage": stage,
        "case_id": case_id,
        "kind": "sine",
        "frequency_hz": float(frequency_hz),
        "source_vpp_v": float(vpp_v),
        **extra,
    }


def sine_matrix(stages: list[list[int]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for amplitude_mv in (20, 50, 100):
        points.append(
            sine_point("C", f"c-100k-{amplitude_mv:03d}mVpp", 100_000, amplitude_mv / 1000)
        )
    for frequency in (10_000, 500_000, 3_000_000):
        points.append(sine_point("C", f"c-{frequency:g}Hz-100mVpp", frequency, 0.1))

    for frequency in (10_000, 100_000, 200_000, 500_000):
        for amplitude_mv in (10, 50, 100, 250, 450, 500):
            points.append(
                sine_point(
                    "D",
                    f"d-{frequency:g}Hz-{amplitude_mv:03d}mVpp",
                    frequency,
                    amplitude_mv / 1000,
                    boundary=amplitude_mv in {10, 450, 500},
                )
            )

    train = (10_000, 10_500, 20_000, 50_000, 100_000, 200_000, 300_000, 400_000, 450_000, 475_000, 490_000, 500_000)
    for direction, sequence in (("up", train), ("down", tuple(reversed(train)))):
        for frequency in sequence:
            points.append(sine_point("E", f"e-train-{direction}-{frequency:g}Hz", frequency, 0.1))
    for frequency in (10_000, 100_000, 500_000):
        points.append(sine_point("E", f"e-minline-{frequency:g}Hz", frequency, 0.01))
    for frequency in (10_000, 200_000, 500_000):
        for amplitude_mv in (50, 250, 450):
            points.append(
                sine_point("E", f"e-cross-{frequency:g}Hz-{amplitude_mv}mVpp", frequency, amplitude_mv / 1000)
            )
    for frequency in (15_000, 75_000, 150_000, 250_000, 350_000, 425_000, 485_000):
        points.append(sine_point("E", f"e-holdout-{frequency:g}Hz", frequency, 0.1, holdout=True))

    fixed = (
        1_000_000,
        1_050_000,
        1_100_000,
        1_250_000,
        1_500_000,
        1_750_000,
        1_900_000,
        2_000_000,
        2_006_250,
        2_056_250,
        2_100_000,
        2_250_000,
        2_500_000,
        2_750_000,
        3_000_000,
    )
    for frequency in fixed:
        points.append(
            sine_point(
                "F",
                f"f-fixed-{frequency:g}Hz",
                frequency,
                0.2,
                response_only=frequency > 2_031_250,
            )
        )
    for index, record in enumerate(worst_stopband_points(stages), start=1):
        frequency = record["frequency_hz"]
        points.append(
            sine_point(
                "F",
                f"f-worst-{index:02d}-{frequency:g}Hz",
                frequency,
                0.2,
                theoretical_response=record,
                response_only=frequency > 2_031_250,
            )
        )

    for frequency in (4_000_000, 5_000_000, 7_200_000, 7_500_000, 10_000_000):
        points.append(
            sine_point(
                "I",
                f"i-formal-{frequency:g}Hz",
                frequency,
                0.2,
                formal_10mhz_coverage=True,
                response_only=True,
                minimum_frames=64 if frequency == 10_000_000 else 22,
            )
        )
    return points


G_CASES = {
    "a-low": {"class": "u_a", "f0": 10_000.0, "orders": (1, 2, 3), "ratios": (1.0, 0.55, 0.3), "target_vpp": 0.1},
    "a-edge": {"class": "u_a", "f0": 40_000.0, "orders": (1, 3, 5), "ratios": (1.0, 0.55, 0.3), "target_vpp": 0.25},
    "b-low": {"class": "u_b", "f0": 125_000.0, "orders": (1, 2), "ratios": (1.0, 0.45), "target_vpp": 0.05},
    "b-edge": {"class": "u_b", "f0": 100_000.0, "orders": (1, 3, 5), "ratios": (1.0, 0.55, 0.3), "target_vpp": 0.25},
    "weak-line": {"class": "u_b", "f0": 50_000.0, "orders": (1, 3, 5), "physical_peaks": (0.005, 0.025, 0.015)},
}


def _waveform_from_components(
    components: list[dict[str, float]],
    *,
    repeat_hz: float = ARB_REPEAT_HZ,
) -> np.ndarray:
    sample_rate_hz = ARB_POINTS * repeat_hz
    time_axis = np.arange(ARB_POINTS, dtype=np.float64) / sample_rate_hz
    waveform = np.zeros(ARB_POINTS, dtype=np.float64)
    for component in components:
        waveform += component["peak_v"] * np.sin(
            2.0 * math.pi * component["frequency_hz"] * time_axis
            + component["phase_rad"]
        )
    return waveform


def _arb_record(
    *,
    case_id: str,
    stage: str,
    signal_class: str,
    components: list[dict[str, float]],
    output_dir: Path,
    metadata: dict[str, Any] | None = None,
    repeat_hz: float = ARB_REPEAT_HZ,
) -> dict[str, Any]:
    waveform = _waveform_from_components(components, repeat_hz=repeat_hz)
    low = float(np.min(waveform))
    high = float(np.max(waveform))
    midpoint = 0.5 * (low + high)
    vpp = high - low
    if not 0 < vpp <= MAX_SOURCE_VPP:
        raise RuntimeError(f"{case_id}: generated Vpp {vpp} is unsafe")
    normalized = (waveform - midpoint) / (vpp / 2.0)
    path = output_dir / "arb" / f"{case_id}.npy"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        np.save(stream, normalized)
    rms = float(np.sqrt(np.mean(np.square(waveform - float(np.mean(waveform))))))
    peak = float(np.max(np.abs(waveform - float(np.mean(waveform)))))
    return {
        "stage": stage,
        "case_id": case_id,
        "kind": "arb",
        "signal_class": signal_class,
        "file": str(path.relative_to(output_dir)),
        "sha256": sha256_file(path),
        "points": ARB_POINTS,
        "playback_frequency_hz": repeat_hz,
        "effective_sample_rate_hz": ARB_POINTS * repeat_hz,
        "source_vpp_v": vpp,
        "true_rms_v": rms,
        "crest_factor": peak / rms,
        "normalized_min": float(np.min(normalized)),
        "normalized_max": float(np.max(normalized)),
        "components": components,
        **(metadata or {}),
    }


def generate_g_arbs(output_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    phase_candidates = {
        "candidate-a": (0.0, 0.0, 0.0),
        "candidate-b": (0.0, 2.0 * math.pi / 3.0, -2.0 * math.pi / 3.0),
    }
    for name, definition in G_CASES.items():
        provisional: list[tuple[float, str, list[dict[str, float]]]] = []
        orders = tuple(definition["orders"])
        for candidate, phases in phase_candidates.items():
            phase_values = phases[: len(orders)]
            if "physical_peaks" in definition:
                peaks = tuple(definition["physical_peaks"])
            else:
                ratios = np.asarray(definition["ratios"], dtype=np.float64)
                raw_components = [
                    {
                        "frequency_hz": definition["f0"] * order,
                        "peak_v": float(ratio),
                        "phase_rad": float(phase),
                        "harmonic_order": int(order),
                    }
                    for order, ratio, phase in zip(orders, ratios, phase_values)
                ]
                raw_vpp = float(np.ptp(_waveform_from_components(raw_components)))
                peaks = tuple(
                    float(ratio * definition["target_vpp"] / raw_vpp)
                    for ratio in ratios
                )
            components = [
                {
                    "frequency_hz": float(definition["f0"] * order),
                    "peak_v": float(peak),
                    "phase_rad": float(phase),
                    "harmonic_order": int(order),
                }
                for order, peak, phase in zip(orders, peaks, phase_values)
            ]
            waveform = _waveform_from_components(components)
            centered = waveform - float(np.mean(waveform))
            crest = float(np.max(np.abs(centered)) / np.sqrt(np.mean(np.square(centered))))
            provisional.append((crest, candidate, components))
        provisional.sort(key=lambda item: item[0])
        for crest_label, item in (("low-crest", provisional[0]), ("high-crest", provisional[-1])):
            _crest, candidate, components = item
            records.append(
                _arb_record(
                    case_id=f"g-{name}-{crest_label}",
                    stage="G",
                    signal_class=str(definition["class"]),
                    components=components,
                    output_dir=output_dir,
                    metadata={"phase_candidate": candidate, "crest_variant": crest_label},
                )
            )
    return records


def generate_h_arbs(output_dir: Path, g_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_case = {record["case_id"]: record for record in g_records}
    records: list[dict[str, Any]] = []
    for base in ("b-low", "b-edge", "weak-line"):
        source = by_case[f"g-{base}-high-crest"]
        for interference_hz in (1_000_000, 1_500_000, 2_000_000, 2_500_000, 3_000_000):
            components = [dict(component) for component in source["components"]]
            components.append(
                {
                    "frequency_hz": float(interference_hz),
                    "peak_v": 0.1,
                    "phase_rad": 0.0,
                    "harmonic_order": int(interference_hz / ARB_REPEAT_HZ),
                    "role": "u_J",
                }
            )
            records.append(
                _arb_record(
                    case_id=f"h-{base}-j-{interference_hz:g}Hz",
                    stage="H",
                    signal_class="u_b+u_J",
                    components=components,
                    output_dir=output_dir,
                    metadata={
                        "u_b_source_case": source["case_id"],
                        "u_j_vpp_v": 0.2,
                        "u_j_frequency_hz": float(interference_hz),
                        "minimum_frames": 64 if base in {"b-edge", "weak-line"} else 22,
                    },
                )
            )
    return records


def generate_i_arbs(output_dir: Path, g_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_case = {record["case_id"]: record for record in g_records}
    source = by_case["g-b-edge-high-crest"]
    records: list[dict[str, Any]] = []
    for interference_hz in (5_000_000, 10_000_000):
        components = [dict(component) for component in source["components"]]
        components.append(
            {
                "frequency_hz": float(interference_hz),
                "peak_v": 0.1,
                "phase_rad": 0.0,
                "harmonic_order": int(interference_hz / 2_000.0),
                "role": "u_J",
            }
        )
        records.append(
            _arb_record(
                case_id=f"i-b-edge-j-{interference_hz:g}Hz",
                stage="I",
                signal_class="u_b+u_J",
                components=components,
                output_dir=output_dir,
                repeat_hz=2_000.0,
                metadata={
                    "u_b_source_case": source["case_id"],
                    "u_j_vpp_v": 0.2,
                    "u_j_frequency_hz": float(interference_hz),
                    "minimum_frames": 64,
                    "formal_10mhz_coverage": True,
                },
            )
        )
    return records


def validate_arb_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        if not 0 < record["source_vpp_v"] <= MAX_SOURCE_VPP:
            raise RuntimeError(f"{record['case_id']}: source amplitude exceeds M11 limit")
        for component in record["components"]:
            frequency = component["frequency_hz"]
            maximum_frequency = 10_000_000 if record["stage"] == "I" else 3_000_000
            if frequency > maximum_frequency or not math.isclose(
                frequency / 500.0, round(frequency / 500.0), abs_tol=1e-9
            ):
                raise RuntimeError(f"{record['case_id']}: invalid component frequency")
    weak = [record for record in records if record["case_id"].startswith("g-weak-line-")]
    for record in weak:
        peaks = [component["peak_v"] for component in record["components"]]
        if not any(math.isclose(value, 0.005, abs_tol=1e-12) for value in peaks):
            raise RuntimeError("weak-line case lost the 5 mVpeak component")
        if peaks[0] >= max(peaks[1:]):
            raise RuntimeError("weak-line fundamental must not be the largest component")
        if not 0.05 <= record["source_vpp_v"] <= 0.25:
            raise RuntimeError("weak-line total Vpp is outside the u_b range")


def build_manifest(output_dir: Path) -> dict[str, Any]:
    stages = parse_fir_stages()
    sine = sine_matrix(stages)
    g_records = generate_g_arbs(output_dir)
    h_records = generate_h_arbs(output_dir, g_records)
    formal_i_combinations = generate_i_arbs(output_dir, g_records)
    validate_arb_records(g_records + h_records + formal_i_combinations)
    return {
        "format": "CycleScope M11 deterministic campaign matrix v1",
        "source_hashes": {
            "public_measurement_plan": sha256_file(PUBLIC_PLAN),
            "m11_plan": sha256_file(M11_PLAN),
            "fir_coefficients": sha256_file(FIR_COEFFS),
        },
        "constants": {
            "max_source_vpp": MAX_SOURCE_VPP,
            "arb_points": ARB_POINTS,
            "arb_repeat_hz": ARB_REPEAT_HZ,
            "arb_effective_sample_rate_hz": ARB_SAMPLE_RATE_HZ,
            "intentional_multitone_max_frequency_hz": 3_000_000,
            "formal_single_tone_max_frequency_hz": 10_000_000,
        },
        "manual_stages": {
            "A": "physical topology, probe/channel and supply preflight",
            "B": "DG-OFF zero/noise plus at least 64 LAN frames",
            "J": "select worst passing H point for response and 10,001-frame longrun",
        },
        "sine_points": sine,
        "arb_points": g_records + h_records + formal_i_combinations,
        "formal_i_combinations": formal_i_combinations,
        "counts": {
            "sine_points": len(sine),
            "g_arbs": len(g_records),
            "h_arbs": len(h_records),
            "formal_i_combinations": len(formal_i_combinations),
        },
    }


def write_sha256sums(root: Path) -> Path:
    output = root / "SHA256SUMS"
    paths = [path for path in sorted(root.rglob("*")) if path.is_file() and path != output]
    with output.open("x", encoding="utf-8") as stream:
        for path in paths:
            stream.write(f"{sha256_file(path)}  {path.relative_to(root)}\n")
    return output


def generate(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing matrix directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = build_manifest(output_dir)
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    sums = write_sha256sums(output_dir)
    return {
        "pass": True,
        "output": str(output_dir.resolve()),
        "manifest": str(manifest_path.resolve()),
        "sha256sums": str(sums.resolve()),
        "counts": manifest["counts"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = generate(args.output)
    except Exception as error:
        print(f"M11_MATRIX_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
