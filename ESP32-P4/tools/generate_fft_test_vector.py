#!/usr/bin/env python3
"""Generate deterministic 8192-point S16_LE FFT test data for CycleScope.

The defaults mirror the on-device local test source in
``main/app/live_data_pipeline.cpp``: a non-coherent 40.75 kHz fundamental
whose third harmonic is stronger than the fundamental, plus a fourth
harmonic. Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
import struct
import zlib


SAMPLE_COUNT = 8192
DEFAULT_SAMPLE_RATE_HZ = 4_062_500.0
DEFAULT_FUNDAMENTAL_HZ = 40_750.0
DEFAULT_SCALE_UV_PER_LSB = 100
DEFAULT_OFFSET_UV = 500
DEFAULT_TONES = ("1:0.025:0.17", "3:0.070:0.92", "4:0.025:-0.51")


@dataclass(frozen=True)
class Tone:
    harmonic: int
    amplitude_volts_peak: float
    phase_radians: float


def parse_tone(value: str) -> Tone:
    try:
        harmonic_text, amplitude_text, phase_text = value.split(":", maxsplit=2)
        tone = Tone(int(harmonic_text), float(amplitude_text), float(phase_text))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "tone must be H:AMPLITUDE_VOLTS_PEAK:PHASE_RADIANS"
        ) from error
    if tone.harmonic < 1:
        raise argparse.ArgumentTypeError("harmonic order must be at least 1")
    if tone.amplitude_volts_peak <= 0.0:
        raise argparse.ArgumentTypeError("tone amplitude must be positive")
    return tone


def round_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0.0 else math.ceil(value - 0.5)


def synthesize_codes(
    sample_rate_hz: float,
    fundamental_hz: float,
    scale_uv_per_lsb: int,
    offset_uv: int,
    tones: tuple[Tone, ...],
) -> list[int]:
    volts_per_lsb = scale_uv_per_lsb * 1.0e-6
    offset_volts = offset_uv * 1.0e-6
    codes: list[int] = []
    for sample_index in range(SAMPLE_COUNT):
        time_seconds = sample_index / sample_rate_hz
        voltage = sum(
            tone.amplitude_volts_peak
            * math.sin(
                2.0
                * math.pi
                * fundamental_hz
                * tone.harmonic
                * time_seconds
                + tone.phase_radians
            )
            for tone in tones
        )
        code = round_away_from_zero((voltage - offset_volts) / volts_per_lsb)
        if not -2048 <= code <= 2047:
            raise ValueError(
                f"sample {sample_index} produces code {code}, outside the AD9226 "
                "normalized range -2048..2047; reduce amplitudes or change scale"
            )
        codes.append(code)
    return codes


def ideal_peak_to_peak(tones: tuple[Tone, ...], points: int = 65_536) -> float:
    minimum = math.inf
    maximum = -math.inf
    for point in range(points):
        base_phase = 2.0 * math.pi * point / points
        value = sum(
            tone.amplitude_volts_peak
            * math.sin(tone.harmonic * base_phase + tone.phase_radians)
            for tone in tones
        )
        minimum = min(minimum, value)
        maximum = max(maximum, value)
    return maximum - minimum


def resolve_output(path_text: str, project_root: Path) -> Path:
    path = Path(path_text).expanduser()
    return path if path.is_absolute() else project_root / path


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-rate-hz", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    parser.add_argument("--fundamental-hz", type=float, default=DEFAULT_FUNDAMENTAL_HZ)
    parser.add_argument("--scale-uv-per-lsb", type=int, default=DEFAULT_SCALE_UV_PER_LSB)
    parser.add_argument("--offset-uv", type=int, default=DEFAULT_OFFSET_UV)
    parser.add_argument(
        "--tone",
        action="append",
        type=parse_tone,
        help=(
            "repeatable H:AMPLITUDE_VOLTS_PEAK:PHASE_RADIANS; "
            "defaults to H1/H3/H4 = 25/70/25 mVpk"
        ),
    )
    parser.add_argument(
        "--output",
        default="build/fft_test_vector_s16le.bin",
        help="S16_LE output path, relative to ESP32-P4 unless absolute",
    )
    parser.add_argument(
        "--metadata",
        default="build/fft_test_vector.json",
        help="JSON metadata path, relative to ESP32-P4 unless absolute",
    )
    parser.add_argument(
        "--csv",
        help="optional CSV path containing sample_index,signed_code,input_volts",
    )
    return parser


def main() -> int:
    arguments = build_argument_parser().parse_args()
    if arguments.sample_rate_hz <= 0.0:
        raise SystemExit("--sample-rate-hz must be positive")
    if arguments.fundamental_hz <= 0.0:
        raise SystemExit("--fundamental-hz must be positive")
    if arguments.scale_uv_per_lsb <= 0:
        raise SystemExit("--scale-uv-per-lsb must be positive")

    tones = tuple(arguments.tone) if arguments.tone else tuple(map(parse_tone, DEFAULT_TONES))
    highest_frequency_hz = arguments.fundamental_hz * max(tone.harmonic for tone in tones)
    if highest_frequency_hz >= arguments.sample_rate_hz * 0.5:
        raise SystemExit("highest tone must be below Nyquist")

    codes = synthesize_codes(
        arguments.sample_rate_hz,
        arguments.fundamental_hz,
        arguments.scale_uv_per_lsb,
        arguments.offset_uv,
        tones,
    )
    payload = struct.pack(f"<{SAMPLE_COUNT}h", *codes)
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    bin_width_hz = arguments.sample_rate_hz / SAMPLE_COUNT
    true_rms_volts = math.sqrt(
        sum(tone.amplitude_volts_peak**2 for tone in tones) / 2.0
    )
    peak_to_peak_volts = ideal_peak_to_peak(tones)

    project_root = Path(__file__).resolve().parents[1]
    output_path = resolve_output(arguments.output, project_root)
    metadata_path = resolve_output(arguments.metadata, project_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)

    metadata = {
        "format": "S16_LE",
        "sample_count": SAMPLE_COUNT,
        "payload_bytes": len(payload),
        "crc32": f"0x{checksum:08X}",
        "sample_rate_hz": arguments.sample_rate_hz,
        "bin_width_hz": bin_width_hz,
        "fundamental_hz": arguments.fundamental_hz,
        "scale_uV_per_lsb": arguments.scale_uv_per_lsb,
        "offset_uV": arguments.offset_uv,
        "expected_true_rms_volts": true_rms_volts,
        "expected_peak_to_peak_volts": peak_to_peak_volts,
        "minimum_code": min(codes),
        "maximum_code": max(codes),
        "tones": [
            {
                "harmonic": tone.harmonic,
                "frequency_hz": arguments.fundamental_hz * tone.harmonic,
                "amplitude_volts_peak": tone.amplitude_volts_peak,
                "phase_radians": tone.phase_radians,
            }
            for tone in tones
        ],
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if arguments.csv:
        csv_path = resolve_output(arguments.csv, project_root)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        volts_per_lsb = arguments.scale_uv_per_lsb * 1.0e-6
        offset_volts = arguments.offset_uv * 1.0e-6
        with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
            csv_file.write("sample_index,signed_code,input_volts\n")
            for sample_index, code in enumerate(codes):
                voltage = code * volts_per_lsb + offset_volts
                csv_file.write(f"{sample_index},{code},{voltage:.9f}\n")

    print(f"wrote {len(payload)} bytes to {output_path}")
    print(f"metadata: {metadata_path}")
    print(
        f"Fs={arguments.sample_rate_hz:.3f} Hz, N={SAMPLE_COUNT}, "
        f"bin={bin_width_hz:.12f} Hz, CRC32=0x{checksum:08X}"
    )
    print(
        f"codes={min(codes)}..{max(codes)}, expected Vpp={peak_to_peak_volts:.9f} V, "
        f"true RMS={true_rms_volts:.9f} V"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
