#!/usr/bin/env python3
"""Generate a reproducible DG4202 ARB waveform and its theory manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np


MAXIMUM_COMPONENT_VPP_SUM = 0.45


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_tone(value: list[str]) -> dict[str, float | int]:
    order = int(value[0])
    peak_mv = float(value[1])
    phase_deg = float(value[2])
    if order < 1 or order > 50:
        raise argparse.ArgumentTypeError("harmonic order must be 1..50")
    if not math.isfinite(peak_mv) or peak_mv <= 0.0:
        raise argparse.ArgumentTypeError("peak amplitude must be positive and finite")
    if not math.isfinite(phase_deg):
        raise argparse.ArgumentTypeError("phase must be finite")
    return {"order": order, "peak_mv": peak_mv, "phase_deg": phase_deg}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--fundamental-hz", type=float, required=True)
    parser.add_argument(
        "--tone",
        action="append",
        nargs=3,
        metavar=("ORDER", "PEAK_MV", "PHASE_DEG"),
        help="legacy: repeat for every source and analysis sine component",
    )
    parser.add_argument(
        "--source-tone",
        action="append",
        nargs=3,
        metavar=("ORDER", "PEAK_MV", "PHASE_DEG"),
        help="repeat for every sine component sent to DG4202",
    )
    parser.add_argument(
        "--analysis-tone",
        action="append",
        nargs=3,
        metavar=("ORDER", "PEAK_MV", "PHASE_DEG"),
        help="repeat for each source component P4 is expected to report",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--points", type=int, default=16384)
    parser.add_argument(
        "--programmed-amplitude-scale",
        type=float,
        default=1.0,
        help=(
            "DG programmed Vpp divided by the physical-theory Vpp; use only "
            "for an externally verified fixed source/load transfer"
        ),
    )
    args = parser.parse_args()

    if not math.isfinite(args.fundamental_hz) or args.fundamental_hz <= 0.0:
        parser.error("--fundamental-hz must be positive and finite")
    if not 2 <= args.points <= 16384:
        parser.error("--points must be in 2..16384")
    if (
        not math.isfinite(args.programmed_amplitude_scale)
        or not 0.0 < args.programmed_amplitude_scale <= 1.0
    ):
        parser.error("--programmed-amplitude-scale must be finite and in (0, 1]")
    legacy_tones = [parse_tone(item) for item in args.tone or []]
    source_tones = [parse_tone(item) for item in args.source_tone or []]
    analysis_tones = [parse_tone(item) for item in args.analysis_tone or []]
    if legacy_tones and (source_tones or analysis_tones):
        parser.error("--tone cannot be mixed with --source-tone or --analysis-tone")
    if legacy_tones:
        source_tones = legacy_tones
        analysis_tones = legacy_tones
    elif not source_tones:
        parser.error("provide --tone or at least one --source-tone")
    elif not analysis_tones:
        analysis_tones = source_tones

    for label, tones in (("source", source_tones), ("analysis", analysis_tones)):
        orders = [int(item["order"]) for item in tones]
        if len(set(orders)) != len(orders):
            parser.error(f"each {label} harmonic order may appear only once")
        if 1 not in orders:
            parser.error(f"the {label} components must contain base component H1")
    source_by_order = {int(tone["order"]): tone for tone in source_tones}
    for tone in analysis_tones:
        source_tone = source_by_order.get(int(tone["order"]))
        if source_tone is None:
            parser.error("every analysis component must be present in the source waveform")
        if (
            not math.isclose(float(tone["peak_mv"]), float(source_tone["peak_mv"]), abs_tol=1.0e-12)
            or not math.isclose(float(tone["phase_deg"]), float(source_tone["phase_deg"]), abs_tol=1.0e-12)
        ):
            parser.error("analysis components must retain their source amplitude and phase")

    source_tones.sort(key=lambda item: int(item["order"]))
    analysis_tones.sort(key=lambda item: int(item["order"]))
    component_vpp_sum = sum(float(tone["peak_mv"]) * 2.0e-3 for tone in source_tones)
    if component_vpp_sum > MAXIMUM_COMPONENT_VPP_SUM + 1.0e-12:
        parser.error(
            "source component Vpp sum "
            f"{component_vpp_sum:.9g} exceeds {MAXIMUM_COMPONENT_VPP_SUM:.2f} Vpp safety limit"
        )

    phase = (2.0 * math.pi / args.points) * np.arange(args.points, dtype=np.float64)
    source_physical_v = np.zeros(args.points, dtype=np.float64)
    analysis_physical_v = np.zeros(args.points, dtype=np.float64)
    for tone in source_tones:
        source_physical_v += float(tone["peak_mv"]) * 1.0e-3 * np.sin(
            int(tone["order"]) * phase + math.radians(float(tone["phase_deg"]))
        )
    for tone in analysis_tones:
        analysis_physical_v += float(tone["peak_mv"]) * 1.0e-3 * np.sin(
            int(tone["order"]) * phase + math.radians(float(tone["phase_deg"]))
        )
    source_full_scale_v = float(np.max(np.abs(source_physical_v)))
    if not math.isfinite(source_full_scale_v) or source_full_scale_v <= 0.0:
        parser.error("generated waveform has no finite non-zero amplitude")
    normalized = source_physical_v / source_full_scale_v
    if np.max(np.abs(normalized)) > 1.0 + 1.0e-12:
        parser.error("normalization exceeded the DG4000 [-1, 1] DAC range")

    # The DG output amplitude is the full scale of the normalized DAC shape.
    # This is intentionally 2*max(abs(u)), not necessarily max(u)-min(u) for
    # phase-asymmetric waveforms.
    source_vpp = 2.0 * source_full_scale_v
    if source_vpp > 0.5 + 1.0e-12:
        parser.error(f"ARB source Vpp {source_vpp:.9g} exceeds the 0.5 Vpp safety ceiling")
    programmed_source_vpp = source_vpp * args.programmed_amplitude_scale
    theory_vpp = float(np.ptp(analysis_physical_v))
    theory_rms = float(np.sqrt(np.mean(np.square(analysis_physical_v))))
    theory_full_scale_v = float(np.max(np.abs(analysis_physical_v)))
    source_theory_vpp = float(np.ptp(source_physical_v))
    source_theory_rms = float(np.sqrt(np.mean(np.square(source_physical_v))))

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    waveform_path = output / "waveform-normalized.npy"
    np.save(waveform_path, normalized, allow_pickle=False)
    manifest = {
        "format": "CycleScope M12 DG4202 ARB manifest v1",
        "case_id": args.case_id,
        "source": {
            "channel": 1,
            "load_ohm": 50,
            "offset_v": 0.0,
            "output_function": "USER",
            "playback_fundamental_hz": args.fundamental_hz,
            "source_fullscale_vpp": source_vpp,
            "programmed_amplitude_vpp": programmed_source_vpp,
            "programmed_amplitude_scale": args.programmed_amplitude_scale,
            "points": args.points,
            "component_vpp_sum": component_vpp_sum,
        },
        "tones": [
            {
                **tone,
                "frequency_hz": int(tone["order"]) * args.fundamental_hz,
                "peak_v": float(tone["peak_mv"]) * 1.0e-3,
            }
            for tone in analysis_tones
        ],
        "source_tones": [
            {
                **tone,
                "frequency_hz": int(tone["order"]) * args.fundamental_hz,
                "peak_v": float(tone["peak_mv"]) * 1.0e-3,
            }
            for tone in source_tones
        ],
        "theory": {
            "voltage_peak_to_peak_v": theory_vpp,
            "true_rms_v": theory_rms,
            "full_scale_peak_v": theory_full_scale_v,
            "definition": "sum(analysis U_peak*sin(2*pi*order*f0*t + phase))",
        },
        "source_theory": {
            "voltage_peak_to_peak_v": source_theory_vpp,
            "true_rms_v": source_theory_rms,
            "full_scale_peak_v": source_full_scale_v,
            "definition": "sum(source U_peak*sin(2*pi*order*f0*t + phase))",
        },
        "waveform": {
            "path": waveform_path.name,
            "sha256": sha256(waveform_path),
            "normalized_min": float(np.min(normalized)),
            "normalized_max": float(np.max(normalized)),
        },
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "waveform": str(waveform_path),
                **manifest["theory"],
                **manifest["source"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
