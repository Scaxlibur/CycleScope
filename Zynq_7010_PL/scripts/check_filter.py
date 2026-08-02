#!/usr/bin/env python3
"""Generate and independently check the fixed-point multistage decimator.

No third-party Python packages are required. The dense sweep evaluates the
complete multirate alias path from 0..32.5 MHz after Q1.17 quantization.
"""

from __future__ import annotations

import cmath
import math
import sys


Q_BITS = 17


def i0(value: float) -> float:
    result = 1.0
    term = 1.0
    scaled = value * value / 4.0
    for index in range(1, 50):
        term *= scaled / (index * index)
        result += term
        if abs(term) < 1e-16 * abs(result):
            break
    return result


def kaiser_lowpass(
    taps: int,
    sample_rate: float,
    passband: float,
    stopband: float,
    cutoff_bias: float,
    beta: float = 8.0,
) -> list[float]:
    cutoff = (passband + (stopband - passband) * cutoff_bias) / sample_rate
    middle = (taps - 1) / 2.0
    window_scale = i0(beta)
    coefficients: list[float] = []
    for index in range(taps):
        offset = index - middle
        sinc = 2.0 * cutoff if offset == 0.0 else math.sin(2.0 * math.pi * cutoff * offset) / (math.pi * offset)
        ratio = offset / middle
        window = i0(beta * math.sqrt(max(0.0, 1.0 - ratio * ratio))) / window_scale
        coefficients.append(sinc * window)
    dc_gain = sum(coefficients)
    return [coefficient / dc_gain for coefficient in coefficients]


def quantize(coefficients: list[float]) -> list[int]:
    result = [round(coefficient * (1 << Q_BITS)) for coefficient in coefficients]
    result[len(result) // 2] += (1 << Q_BITS) - sum(result)
    return result


def response(coefficients: list[int], frequency: float, sample_rate: float) -> complex:
    phase_step = 2.0 * math.pi * frequency / sample_rate
    scale = float(1 << Q_BITS)
    return sum((coefficient / scale) * cmath.exp(-1j * phase_step * index)
               for index, coefficient in enumerate(coefficients))


def alias_frequency(frequency: float, sample_rate: float) -> float:
    wrapped = frequency % sample_rate
    return min(wrapped, sample_rate - wrapped)


def main() -> int:
    configurations = (
        (21, 65_000_000.0, 1_000_000.0, 15_250_000.0, 0.40),
        (31, 16_250_000.0, 1_000_000.0, 3_062_500.0, 0.50),
        (79, 4_062_500.0, 500_000.0, 1_000_000.0, 0.50),
    )
    stages = [quantize(kaiser_lowpass(*configuration)) for configuration in configurations]

    for index, coefficients in enumerate(stages, start=1):
        if sum(coefficients) != (1 << Q_BITS):
            print(f"stage {index}: DC coefficient sum is invalid", file=sys.stderr)
            return 1
        print(f"stage{index}: taps={len(coefficients)} q17_sum={sum(coefficients)}")

    pass_min = float("inf")
    pass_max = 0.0
    stop_max = 0.0
    stop_frequency = 0.0
    grid_points = 65_001
    for point in range(grid_points):
        frequency = 32_500_000.0 * point / (grid_points - 1)
        amplitude = abs(response(stages[0], frequency, 65_000_000.0))
        amplitude *= abs(response(stages[1], alias_frequency(frequency, 16_250_000.0), 16_250_000.0))
        amplitude *= abs(response(stages[2], alias_frequency(frequency, 4_062_500.0), 4_062_500.0))
        if frequency <= 500_000.0:
            pass_min = min(pass_min, amplitude)
            pass_max = max(pass_max, amplitude)
        if frequency >= 1_000_000.0 and amplitude > stop_max:
            stop_max = amplitude
            stop_frequency = frequency

    ripple_db = 20.0 * math.log10(pass_max / pass_min)
    stop_db = 20.0 * math.log10(stop_max)
    print(f"passband_ripple_db={ripple_db:.6f}")
    print(f"worst_stopband_db={stop_db:.6f} at {stop_frequency:.1f} Hz")
    if ripple_db > 0.1 or stop_db > -50.0:
        print("FILTER_CHECK_FAIL", file=sys.stderr)
        return 1
    print("FILTER_CHECK_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
