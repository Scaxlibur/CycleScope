#!/usr/bin/env python3
"""Validate and print the fixed CycleScope G-problem downstream matrix.

This tool is deliberately offline: it imports the existing emulator's pure
waveform helpers, validates four boundary vectors, and prints exact replay
commands. It never creates a socket, starts a subprocess, or writes a file.
"""

from __future__ import annotations

import argparse
import math
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

EMULATOR_TOOLS = Path(__file__).resolve().parents[2] / "ESP32-P4" / "tools"
sys.path.insert(0, str(EMULATOR_TOOLS))
import cslp_fpga_emulator as emulator


PC_BIND_IP = "192.168.10.5"
P4_PEER_IP = "192.168.10.3"
SCALE_UV_PER_LSB = 100
OFFSET_UV = 500
CALIBRATION_ID = 1


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    requirement: str
    target_vpp_mv: float
    fundamental_hz: float
    band_max_hz: float
    tones: tuple[tuple[int, float, float], ...]
    note: str


CASES = (
    MatrixCase(
        "ua_100mv_10khz_h2",
        "u_a",
        100.0,
        10_000.0,
        200_000.0,
        (
            (1, 0.0444444444444444, 0.0),
            (2, 0.0222222222222222, math.pi / 2.0),
        ),
        "u_a lower voltage/frequency corner; one harmonic",
    ),
    MatrixCase(
        "ua_250mv_200khz_h3_h5",
        "u_a",
        250.0,
        40_000.0,
        200_000.0,
        (
            (1, 0.0125, 0.0),
            (3, 0.075, math.pi),
            (5, 0.0375, 0.0),
        ),
        "u_a upper voltage/component corner; weak fundamental",
    ),
    MatrixCase(
        "ub_50mv_10khz_h2",
        "u_b",
        50.0,
        10_000.0,
        500_000.0,
        (
            (1, 0.0055363321799308, 0.0),
            (2, 0.0221453287197232, -math.pi / 2.0),
        ),
        "u_b lower voltage/frequency corner; weak fundamental",
    ),
    MatrixCase(
        "ub_250mv_500khz_h3_h5",
        "u_b",
        250.0,
        100_000.0,
        500_000.0,
        (
            (1, 0.080, 0.0),
            (3, 0.030, math.pi),
            (5, 0.015, 0.0),
        ),
        "u_b upper voltage/component corner; two harmonics",
    ),
)


def tone_argument(tone: tuple[int, float, float]) -> str:
    harmonic, amplitude, phase = tone
    return f"{harmonic}:{amplitude!r}:{phase!r}"


def replay_command(case: MatrixCase) -> str:
    argv = [
        "python3",
        "ESP32-P4/tools/cslp_fpga_emulator.py",
        "--bind-ip",
        PC_BIND_IP,
        "--port",
        "50000",
        "--peer-ip",
        P4_PEER_IP,
        "--peer-port",
        "50001",
        "--scenario",
        "normal",
        "--waveform",
        "multitone",
        "--frames",
        "100",
        "--chunk-gap-us",
        "250",
        "--hold-seconds",
        "2",
        "--handshake-timeout",
        "15",
        "--scale-uv-per-lsb",
        str(SCALE_UV_PER_LSB),
        "--offset-uv",
        str(OFFSET_UV),
        "--calibration-id",
        str(CALIBRATION_ID),
        "--fundamental-hz",
        repr(case.fundamental_hz),
    ]
    for tone in case.tones:
        argv.extend(("--tone", tone_argument(tone)))
    return shlex.join(argv)


def metrics(case: MatrixCase) -> dict[str, float | int]:
    ideal_vpp, ideal_rms = emulator.expected_multitone_metrics(case.tones)
    samples = emulator.synthesize_multitone(
        case.fundamental_hz,
        case.tones,
        SCALE_UV_PER_LSB,
        OFFSET_UV,
    )
    volts_per_lsb = SCALE_UV_PER_LSB * 1.0e-6
    offset_volts = OFFSET_UV * 1.0e-6
    reconstructed = tuple(code * volts_per_lsb + offset_volts for code in samples)
    mean = sum(reconstructed) / len(reconstructed)
    quantized_rms = math.sqrt(
        sum((voltage - mean) ** 2 for voltage in reconstructed) / len(reconstructed)
    )
    return {
        "ideal_vpp_mv": ideal_vpp * 1000.0,
        "ideal_rms_mv": ideal_rms * 1000.0,
        "quantized_vpp_mv": (max(reconstructed) - min(reconstructed)) * 1000.0,
        "quantized_ac_rms_mv": quantized_rms * 1000.0,
        "code_min": min(samples),
        "code_max": max(samples),
    }


def validate_matrix() -> None:
    emulator.self_test()
    if len({case.case_id for case in CASES}) != len(CASES):
        raise RuntimeError("matrix case IDs must be unique")
    if {case.target_vpp_mv for case in CASES} != {50.0, 100.0, 250.0}:
        raise RuntimeError("matrix must cover 50/100/250 mVpp")
    if {len(case.tones) - 1 for case in CASES} != {1, 2}:
        raise RuntimeError("matrix must cover one and two harmonics")

    maxima: dict[str, set[float]] = {"u_a": set(), "u_b": set()}
    for case in CASES:
        harmonics = tuple(harmonic for harmonic, _, _ in case.tones)
        frequencies = tuple(harmonic * case.fundamental_hz for harmonic in harmonics)
        maxima[case.requirement].add(max(frequencies))
        if harmonics[0] != 1 or len(set(harmonics)) != len(harmonics):
            raise RuntimeError(f"{case.case_id}: H1/unique-harmonic invariant failed")
        if min(frequencies) < 10_000.0 or max(frequencies) > case.band_max_hz:
            raise RuntimeError(f"{case.case_id}: frequency leaves its G-problem band")
        for tone in case.tones:
            if emulator.parse_tone(tone_argument(tone)) != tone:
                raise RuntimeError(f"{case.case_id}: CLI tone round-trip failed")

        result = metrics(case)
        if abs(float(result["ideal_vpp_mv"]) - case.target_vpp_mv) > 0.01:
            raise RuntimeError(f"{case.case_id}: analytic Vpp misses its target")
        if abs(float(result["quantized_vpp_mv"]) - case.target_vpp_mv) > 0.2:
            raise RuntimeError(f"{case.case_id}: quantized Vpp misses its target")
        if (
            abs(
                float(result["quantized_ac_rms_mv"])
                - float(result["ideal_rms_mv"])
            )
            > 0.1
        ):
            raise RuntimeError(f"{case.case_id}: quantized RMS drift is too large")

    if 200_000.0 not in maxima["u_a"] or 500_000.0 not in maxima["u_b"]:
        raise RuntimeError("matrix does not reach both frequency-component limits")


def print_matrix(cases: tuple[MatrixCase, ...]) -> None:
    print("# CycleScope G 题 P4 下游可重放矩阵")
    print()
    print("| Case | 范围 | F0 | 最高分量 | Vpp | 真 RMS | 分量数 | 量化码程 |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for case in cases:
        result = metrics(case)
        highest_hz = max(h * case.fundamental_hz for h, _, _ in case.tones)
        print(
            f"| `{case.case_id}` | {case.requirement} | "
            f"{case.fundamental_hz / 1000.0:.3f} kHz | "
            f"{highest_hz / 1000.0:.3f} kHz | "
            f"{float(result['ideal_vpp_mv']):.6f} mV | "
            f"{float(result['ideal_rms_mv']):.6f} mV | "
            f"{len(case.tones)} | {result['code_min']}…{result['code_max']} |"
        )

    for case in cases:
        print()
        print(f"## {case.case_id}")
        print()
        print(case.note)
        print()
        print("```bash")
        print(replay_command(case))
        print("```")

    print()
    print(
        "> 仅覆盖 PC 合成的滤后 CSLP 数据到 ESP32-P4 数值链；不覆盖真实 "
        "BNC/ADC/FPGA、200 mVpp ≥1 MHz 前端抗干扰、LVGL 人工视觉或冷启动 2 秒。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument(
        "--case",
        action="append",
        choices=tuple(case.case_id for case in CASES),
        help="repeat to select cases; defaults to the complete matrix",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_matrix()
    if args.self_test_only:
        print(f"G downstream matrix self-test passed ({len(CASES)} cases)")
        return 0
    selected = set(args.case) if args.case else None
    cases = tuple(case for case in CASES if selected is None or case.case_id in selected)
    print_matrix(cases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
