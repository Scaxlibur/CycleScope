#!/usr/bin/env python3
"""Verify a downloaded nonzero M11 calibration identity over real ADC LAN frames."""

# ruff: noqa: E402 -- sibling M11 helpers and the PS validator are deliberate inputs.

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m11_wavebench_safe as safety

PS_SCRIPTS = (
    safety.FPGA_ROOT / "Zynq_7010_PS" / "cyclescope_cslp" / "scripts"
)
if str(PS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PS_SCRIPTS))

from cslp_calibration_profile import load_calibration_profile


class CalibratedLanSmokeError(RuntimeError):
    """The calibrated LAN smoke cannot be executed or accepted safely."""


def run_smoke(manifest: Path, output_root: Path, frames: int) -> dict[str, Any]:
    if frames < 2:
        raise CalibratedLanSmokeError("at least two frames are required")
    profile = load_calibration_profile(manifest)
    preflight = safety.readonly_preflight()
    if preflight.get("pass") is not True:
        raise CalibratedLanSmokeError(
            "instrument read-only preflight failed: "
            + "; ".join(preflight.get("failures", []))
        )
    stamp = safety.now_stamp()
    point_dir = output_root / f"{stamp}_calibrated-metadata-smoke"
    point_dir.mkdir(parents=True, exist_ok=False)
    lan = safety._capture_zero_lan(
        point_dir,
        frames,
        activity_policy="allow",
        expected_calibration_id=profile.calibration_id,
        expected_scale_uv_per_lsb=profile.scale_uv_per_lsb,
        expected_offset_uv=profile.offset_uv,
        archive_packets=True,
    )
    payload = {
        "format": "CycleScope M11 calibrated LAN metadata smoke v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "calibration_manifest": str(profile.manifest_path),
        "calibration_manifest_sha256": profile.manifest_sha256,
        "expected_identity": {
            "calibration_id": profile.calibration_id,
            "scale_uv_per_lsb": profile.scale_uv_per_lsb,
            "offset_uv": profile.offset_uv,
            "calibrated_flag": True,
        },
        "source_mode": "real-adc",
        "instrument_preflight": preflight.get("evidence_path"),
        "instrument_writes": False,
        "fpga_action": "none; verifies the already downloaded volatile image",
        "qspi_write": False,
        "mio47_access": False,
        "lan": lan,
        "failures": lan.get("failures", []),
        "pass": lan.get("pass") is True,
    }
    evidence = point_dir / "board-verify.json"
    safety.write_json_exclusive(evidence, payload)
    sums = safety._write_sha256sums(point_dir)
    return {
        "pass": payload["pass"],
        "evidence": str(evidence.resolve()),
        "sha256sums": str(sums.resolve()),
        "frame_count": lan.get("frame_count"),
        "calibration_id": profile.calibration_id,
        "scale_uv_per_lsb": profile.scale_uv_per_lsb,
        "offset_uv": profile.offset_uv,
        "failures": payload["failures"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            safety.EVIDENCE_ROOT
            / "firmware"
            / "calibrated-peer4-v1"
            / "board-verification"
        ),
    )
    parser.add_argument("--frames", type=int, default=22)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_smoke(
            args.manifest.resolve(), args.output_root.resolve(), args.frames
        )
    except Exception as error:
        print(
            f"M11_CALIBRATED_LAN_SMOKE_ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
