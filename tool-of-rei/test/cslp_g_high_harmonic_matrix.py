#!/usr/bin/env python3
"""Validate and print adversarial high-harmonic G-problem replay cases.

The four boundary cases in ``cslp_g_acceptance_matrix.py`` cover voltage and
band edges.  This companion matrix covers the remaining shape risks from the
organizer clarification: an input may contain any one or two harmonics whose
frequencies remain in band, and every component may be as small as 5 mVpk.

This script is offline.  It validates synthesis and prints commands; it never
opens a socket, starts a subprocess, or writes a result file.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import re

import cslp_g_acceptance_matrix as boundary


CASES = (
    boundary.MatrixCase(
        "ua_h20_weak_h1",
        "u_a",
        190.593791,
        10_000.0,
        200_000.0,
        (
            (1, 0.005, 0.3),
            (19, 0.045, -1.1),
            (20, 0.050, 2.0),
        ),
        "weak 5 mVpk H1 with the highest possible u_a harmonic order",
    ),
    boundary.MatrixCase(
        "ua_h19_500hz_grid",
        "u_a",
        179.857423,
        10_500.0,
        200_000.0,
        (
            (1, 0.005, -0.7),
            (18, 0.040, 0.5),
            (19, 0.050, -2.2),
        ),
        "500 Hz input grid with 189/199.5 kHz high-order components",
    ),
    boundary.MatrixCase(
        "ua_h2_min_component",
        "u_a",
        100.002029,
        100_000.0,
        200_000.0,
        (
            (1, 0.050, -0.2),
            (2, 0.005, 1.2),
        ),
        "upper u_a fundamental with an exactly 5 mVpk harmonic",
    ),
    boundary.MatrixCase(
        "ub_h50_weak_h1",
        "u_b",
        90.339619,
        10_000.0,
        500_000.0,
        (
            (1, 0.005, 0.9),
            (49, 0.020, -0.4),
            (50, 0.025, 1.7),
        ),
        "weak 5 mVpk H1 with H49/H50 at 490/500 kHz",
    ),
    boundary.MatrixCase(
        "ub_h47_500hz_grid",
        "u_b",
        111.439909,
        10_500.0,
        500_000.0,
        (
            (1, 0.005, -1.4),
            (46, 0.025, 0.2),
            (47, 0.030, 2.4),
        ),
        "500 Hz input grid with 483/493.5 kHz high-order components",
    ),
    boundary.MatrixCase(
        "ub_h2_min_component",
        "u_b",
        51.775270,
        250_000.0,
        500_000.0,
        (
            (1, 0.025, 0.1),
            (2, 0.005, -2.0),
        ),
        "500 kHz band edge with an exactly 5 mVpk H2 component",
    ),
)


MEASUREMENT_PATTERN = re.compile(
    r"measurement: session=(?P<session>[0-9A-Fa-f]+)\b"
    r"[^\r\n]*?\bframe=(?P<frame>[0-9]+)\b"
    r"[^\r\n]*?\bF0=(?P<f0>[0-9.]+)Hz "
    r"Vpp=(?P<vpp>[0-9.]+)mV RMS=(?P<rms>[0-9.]+)mV "
    r"peaks=(?P<peak_count>[0-9]+) "
    r"P1=(?P<p1_frequency>[0-9.]+)Hz/(?P<p1_amplitude>[0-9.]+)mVpk "
    r"P2=(?P<p2_frequency>[0-9.]+)Hz/(?P<p2_amplitude>[0-9.]+)mVpk "
    r"P3=(?P<p3_frequency>[0-9.]+)Hz/(?P<p3_amplitude>[0-9.]+)mVpk"
)


def validate_matrix() -> None:
    boundary.validate_matrix()
    if len({case.case_id for case in CASES}) != len(CASES):
        raise RuntimeError("high-harmonic case IDs must be unique")

    covered_orders: set[int] = set()
    for case in CASES:
        harmonics = tuple(harmonic for harmonic, _, _ in case.tones)
        frequencies = tuple(
            harmonic * case.fundamental_hz for harmonic in harmonics
        )
        covered_orders.update(harmonics)
        if harmonics[0] != 1 or len(set(harmonics)) != len(harmonics):
            raise RuntimeError(f"{case.case_id}: H1/unique-harmonic invariant failed")
        if len(case.tones) not in (2, 3):
            raise RuntimeError(f"{case.case_id}: expected one or two harmonics")
        if min(amplitude for _, amplitude, _ in case.tones) < 0.005:
            raise RuntimeError(f"{case.case_id}: component is below 5 mVpk")
        if min(frequencies) < 10_000.0 or max(frequencies) > case.band_max_hz:
            raise RuntimeError(f"{case.case_id}: frequency leaves its G-problem band")
        if any(
            not math.isclose(frequency / 500.0, round(frequency / 500.0))
            for frequency in frequencies
        ):
            raise RuntimeError(f"{case.case_id}: frequency is off the 500 Hz grid")

        lower_vpp_mv = 100.0 if case.requirement == "u_a" else 50.0
        result = boundary.metrics(case)
        ideal_vpp_mv = float(result["ideal_vpp_mv"])
        if not lower_vpp_mv <= ideal_vpp_mv <= 250.0:
            raise RuntimeError(f"{case.case_id}: Vpp leaves its requirement range")
        if abs(ideal_vpp_mv - case.target_vpp_mv) > 0.001:
            raise RuntimeError(f"{case.case_id}: analytic Vpp changed")
        if abs(float(result["quantized_vpp_mv"]) - ideal_vpp_mv) > 0.2:
            raise RuntimeError(f"{case.case_id}: quantized Vpp drift is too large")
        if (
            abs(
                float(result["quantized_ac_rms_mv"])
                - float(result["ideal_rms_mv"])
            )
            > 0.2
        ):
            raise RuntimeError(f"{case.case_id}: quantized RMS drift is too large")

    if not {19, 20, 47, 50}.issubset(covered_orders):
        raise RuntimeError("matrix does not cover the intended high harmonic orders")


def validate_serial_log(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    fatal_markers = (
        "Measurement rejected",
        "FFT failed for session",
        "Guru Meditation",
        "Task watchdog got triggered",
        "assert failed",
        "abort() was called",
    )
    present = tuple(marker for marker in fatal_markers if marker in text)
    if present:
        raise RuntimeError(f"serial log contains fatal markers: {present}")

    matches = tuple(MEASUREMENT_PATTERN.finditer(text))
    measurement_line_count = sum(
        "measurement:" in line for line in text.splitlines()
    )
    if measurement_line_count != len(matches):
        raise RuntimeError(
            f"serial log has {measurement_line_count} measurement lines but only "
            f"{len(matches)} match the complete measurement schema"
        )
    if len(matches) == len(CASES):
        mode = "6x1"
        checkpoints = (1,)
    elif len(matches) == len(CASES) * 2:
        mode = "6x100"
        checkpoints = (1, 100)
    else:
        raise RuntimeError(
            f"serial log has {len(matches)} matrix measurements; expected "
            f"{len(CASES)} (6x1) or {len(CASES) * 2} (6x100)"
        )

    measurements_per_case = len(checkpoints)
    matrix_matches = tuple(
        matches[index : index + measurements_per_case]
        for index in range(0, len(matches), measurements_per_case)
    )
    sessions: list[str] = []
    for case, case_matches in zip(CASES, matrix_matches, strict=True):
        actual_checkpoints = tuple(
            int(match.group("frame")) for match in case_matches
        )
        if actual_checkpoints != checkpoints:
            raise RuntimeError(
                f"{case.case_id}: measurement checkpoints are "
                f"{actual_checkpoints}; expected {checkpoints} in order"
            )
        case_sessions = tuple(
            match.group("session").upper() for match in case_matches
        )
        if len(set(case_sessions)) != 1:
            raise RuntimeError(
                f"{case.case_id}: frame checkpoints do not share one session"
            )
        sessions.append(case_sessions[0])
    if len(set(sessions)) != len(sessions):
        raise RuntimeError("serial log reused a session across different matrix cases")

    maximum_f0_error_hz = 0.0
    maximum_vpp_error_mv = 0.0
    maximum_rms_error_mv = 0.0
    maximum_line_frequency_error_hz = 0.0
    maximum_line_amplitude_error_mv = 0.0
    for case, case_matches in zip(CASES, matrix_matches, strict=True):
        expected = boundary.metrics(case)
        for match in case_matches:
            frame_id = int(match.group("frame"))
            actual_f0_hz = float(match.group("f0"))
            actual_vpp_mv = float(match.group("vpp"))
            actual_rms_mv = float(match.group("rms"))
            actual_peak_count = int(match.group("peak_count"))
            actual_peaks = tuple(
                (
                    float(match.group(f"p{index}_frequency")),
                    float(match.group(f"p{index}_amplitude")),
                )
                for index in range(1, 4)
            )[:actual_peak_count]

            maximum_f0_error_hz = max(
                maximum_f0_error_hz, abs(actual_f0_hz - case.fundamental_hz)
            )
            maximum_vpp_error_mv = max(
                maximum_vpp_error_mv,
                abs(actual_vpp_mv - float(expected["ideal_vpp_mv"])),
            )
            maximum_rms_error_mv = max(
                maximum_rms_error_mv,
                abs(actual_rms_mv - float(expected["ideal_rms_mv"])),
            )
            if abs(actual_f0_hz - case.fundamental_hz) > 1_000.0:
                raise RuntimeError(
                    f"{case.case_id} frame {frame_id}: fundamental error exceeds 1 kHz"
                )
            if abs(actual_vpp_mv - float(expected["ideal_vpp_mv"])) > 5.0:
                raise RuntimeError(
                    f"{case.case_id} frame {frame_id}: Vpp error exceeds 5 mV"
                )
            if abs(actual_rms_mv - float(expected["ideal_rms_mv"])) > 5.0:
                raise RuntimeError(
                    f"{case.case_id} frame {frame_id}: RMS error exceeds 5 mV"
                )
            if actual_peak_count != len(case.tones):
                raise RuntimeError(
                    f"{case.case_id} frame {frame_id}: spectral line count changed"
                )

            for (harmonic, amplitude_volts, _), (
                frequency_hz,
                amplitude_mv,
            ) in zip(case.tones, actual_peaks, strict=True):
                expected_frequency_hz = harmonic * case.fundamental_hz
                maximum_line_frequency_error_hz = max(
                    maximum_line_frequency_error_hz,
                    abs(frequency_hz - expected_frequency_hz),
                )
                maximum_line_amplitude_error_mv = max(
                    maximum_line_amplitude_error_mv,
                    abs(amplitude_mv - amplitude_volts * 1000.0),
                )
                if abs(frequency_hz - expected_frequency_hz) > 1_000.0:
                    raise RuntimeError(
                        f"{case.case_id} frame {frame_id}: H{harmonic} "
                        "frequency error exceeds 1 kHz"
                    )
                if abs(amplitude_mv - amplitude_volts * 1000.0) > 5.0:
                    raise RuntimeError(
                        f"{case.case_id} frame {frame_id}: H{harmonic} "
                        "amplitude error exceeds 5 mV"
                    )

    print(
        "G high-harmonic serial matrix passed: "
        f"mode={mode} checkpoints={','.join(map(str, checkpoints))} "
        f"cases={len(CASES)} measurements={len(matches)} "
        f"sessions={','.join(sessions)} "
        f"max_error(F0/Vpp/RMS/line_f/line_a)="
        f"{maximum_f0_error_hz:.3f}Hz/{maximum_vpp_error_mv:.3f}mV/"
        f"{maximum_rms_error_mv:.3f}mV/"
        f"{maximum_line_frequency_error_hz:.3f}Hz/"
        f"{maximum_line_amplitude_error_mv:.3f}mV"
    )


def print_matrix(cases: tuple[boundary.MatrixCase, ...]) -> None:
    print("# CycleScope G 题高阶谐波 P4 重放矩阵")
    print()
    print("| Case | 范围 | F0 | 分量 | Vpp | 真 RMS | 量化码程 |")
    print("|---|---|---:|---|---:|---:|---:|")
    for case in cases:
        result = boundary.metrics(case)
        frequencies = "/".join(
            f"{harmonic * case.fundamental_hz / 1000.0:g}k"
            for harmonic, _, _ in case.tones
        )
        print(
            f"| `{case.case_id}` | {case.requirement} | "
            f"{case.fundamental_hz / 1000.0:g} kHz | {frequencies} | "
            f"{float(result['ideal_vpp_mv']):.6f} mV | "
            f"{float(result['ideal_rms_mv']):.6f} mV | "
            f"{result['code_min']}…{result['code_max']} |"
        )

    for case in cases:
        print()
        print(f"## {case.case_id}")
        print()
        print(case.note)
        print()
        print("```bash")
        print(boundary.replay_command(case))
        print("```")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument(
        "--serial-log",
        type=Path,
        help=(
            "validate a complete six-case capture with frame 1, or with "
            "frames 1 and 100 per case"
        ),
    )
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
    if args.serial_log is not None:
        validate_serial_log(args.serial_log)
        return 0
    if args.self_test_only:
        print(f"G high-harmonic matrix self-test passed ({len(CASES)} cases)")
        return 0
    selected = set(args.case) if args.case else None
    cases = tuple(
        case for case in CASES if selected is None or case.case_id in selected
    )
    print_matrix(cases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
