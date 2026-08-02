#!/usr/bin/env python3
"""Fail-closed DG -> FPGA mirror -> calibrated P4 M8/M9 campaign.

Only WaveBench source/scope entry points are allowed.  The fixture has no
DP800 configuration and emits no CSLP traffic.  Every WaveBench raw scope
package is copied into the case evidence directory before acceptance.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


THIS_DIR = Path(__file__).resolve().parent
TEST_DIR = THIS_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import m12_live_campaign as legacy
import p4_final_acceptance as acceptance


PROJECT_ROOT = THIS_DIR.parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
WAVEBENCH_ROOT = WORKSPACE_ROOT / "tools" / "wavebench"
WAVEBENCH = WAVEBENCH_ROOT / ".venv" / "bin" / "wavebench"
PYTHON = WAVEBENCH_ROOT / ".venv" / "bin" / "python"
GENERATOR = TEST_DIR / "m12_generate_arb.py"
UART_CAPTURE = TEST_DIR / "m12_passive_uart_capture.py"
MIRROR_CAPTURE = TEST_DIR / "m12_passive_mirror_capture.py"
USER_TO_SINE = TEST_DIR / "m12_user_to_sine_transition.py"
HARM_TO_SINE = TEST_DIR / "m12_harm_to_sine_transition.py"
FINAL_ACCEPTANCE = THIS_DIR / "p4_final_acceptance.py"
DEFAULT_CONFIG = PROJECT_ROOT / "tool-of-rei" / "m12-wavebench-safe.toml"
DEFAULT_FROZEN_ROOT = (
    PROJECT_ROOT
    / "tool-of-rei"
    / "evidence"
    / "final-calibration-20260801_145546+0800"
)
MIRROR_BIND = "192.168.10.4"
MIRROR_PORT = 50002
FPGA_IP = "192.168.10.2"
SUPPORTED_MAXIMUM_VPP = 0.25
INTERFERENCE_ENVELOPE_MAXIMUM_VPP = 0.38
MINIMUM_COMPLETE_FRAMES = 64


class LiveCampaignError(RuntimeError):
    """The live campaign cannot safely continue."""


@dataclass(frozen=True)
class Tone:
    order: int
    peak_mv: float
    phase_deg: float


@dataclass(frozen=True)
class FinalCase:
    case_id: str
    stage: str
    purpose: str
    fundamental_hz: float
    source_tones: tuple[Tone, ...]
    analysis_tones: tuple[Tone, ...]
    maximum_programmed_vpp: float = SUPPORTED_MAXIMUM_VPP
    formal_vpp_min_mv: float | None = None
    formal_vpp_max_mv: float | None = None
    focus_frequency_hz: float | None = None
    interference_pair: str | None = None


def tones(*values: tuple[int, float, float]) -> tuple[Tone, ...]:
    return tuple(Tone(*value) for value in values)


def build_cases() -> tuple[FinalCase, ...]:
    m8: list[FinalCase] = [
        FinalCase(
            "M8-010K-H1", "m8", "10 kHz response endpoint", 10_000.0,
            tones((1, 40.0, 0.0), (3, 20.0, 0.0)),
            tones((1, 40.0, 0.0), (3, 20.0, 0.0)),
            focus_frequency_hz=10_000.0,
        ),
        FinalCase(
            "M8-015K-H1", "m8", "15 kHz isolated M6 holdout", 15_000.0,
            tones((1, 40.0, 0.0), (3, 20.0, 0.0)),
            tones((1, 40.0, 0.0), (3, 20.0, 0.0)),
            focus_frequency_hz=15_000.0,
        ),
        FinalCase(
            "M8-075K-H5", "m8", "75 kHz isolated M6 holdout", 15_000.0,
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            focus_frequency_hz=75_000.0,
        ),
        FinalCase(
            "M8-100K-H1", "m8", "100 kHz response anchor", 100_000.0,
            tones((1, 40.0, 0.0), (3, 20.0, 0.0)),
            tones((1, 40.0, 0.0), (3, 20.0, 0.0)),
            focus_frequency_hz=100_000.0,
        ),
        FinalCase(
            "M8-150K-H2", "m8", "150 kHz isolated M6 holdout", 75_000.0,
            tones((1, 20.0, 0.0), (2, 50.0, 90.0)),
            tones((1, 20.0, 0.0), (2, 50.0, 90.0)),
            focus_frequency_hz=150_000.0,
        ),
        FinalCase(
            "M8-250K-H2", "m8", "250 kHz isolated M6 holdout", 125_000.0,
            tones((1, 20.0, 0.0), (2, 50.0, 90.0)),
            tones((1, 20.0, 0.0), (2, 50.0, 90.0)),
            focus_frequency_hz=250_000.0,
        ),
        FinalCase(
            "M8-350K-H5", "m8", "350 kHz isolated M6 holdout", 70_000.0,
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            focus_frequency_hz=350_000.0,
        ),
        FinalCase(
            "M8-425K-H5", "m8", "425 kHz isolated M6 holdout", 85_000.0,
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            focus_frequency_hz=425_000.0,
        ),
        FinalCase(
            "M8-485K-H5", "m8", "485 kHz isolated M6 holdout", 97_000.0,
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            tones((1, 20.0, 0.0), (5, 50.0, 0.0)),
            focus_frequency_hz=485_000.0,
        ),
        FinalCase(
            "M8-500K-H2", "m8", "500 kHz response endpoint", 250_000.0,
            tones((1, 20.0, 0.0), (2, 50.0, 90.0)),
            tones((1, 20.0, 0.0), (2, 50.0, 90.0)),
            focus_frequency_hz=500_000.0,
        ),
    ]

    ua_low = tones(
        (1, 44.4444444444444, 0.0),
        (2, 22.2222222222222, 90.0),
    )
    ua_high = tones((1, 12.5, 0.0), (3, 75.0, 180.0), (5, 37.5, 0.0))
    ub_low = tones(
        (1, 5.5363321799308, 0.0),
        (2, 22.1453287197232, 270.0),
    )
    ub_high = tones((1, 80.0, 0.0), (3, 30.0, 180.0), (5, 15.0, 0.0))
    crest_low = tones((1, 40.0, 0.0), (3, 30.0, 270.0), (5, 20.0, 300.0))
    crest_high = tones((1, 40.0, 0.0), (3, 30.0, 180.0), (5, 20.0, 0.0))
    weak_h1 = tones((1, 5.0, 0.0), (8, 40.0, 0.0), (16, 50.0, 0.0))
    baseline_66k7 = tones((1, 70.0, 0.0), (3, 39.0, 0.0), (5, 21.0, 0.0))
    interference_base = tones(
        (1, 21.78040735, 76.7675175),
        (9, 17.25449723, 17.35110899),
        (10, 24.86918102, 19.50260744),
    )
    interference = tones((20, 100.0, 129.20522577))
    m9 = [
        FinalCase(
            "M9-UA-100MV-LOW", "m9", "u_a 100 mVpp lower boundary", 10_000.0,
            ua_low, ua_low, formal_vpp_min_mv=99.999, formal_vpp_max_mv=100.001,
        ),
        FinalCase(
            "M9-UA-250MV-HIGH", "m9", "u_a 250 mVpp / 200 kHz boundary", 40_000.0,
            ua_high, ua_high, formal_vpp_min_mv=249.999, formal_vpp_max_mv=250.001,
        ),
        FinalCase(
            "M9-UB-50MV-LOW", "m9", "u_b 50 mVpp lower boundary", 10_000.0,
            ub_low, ub_low, formal_vpp_min_mv=49.999, formal_vpp_max_mv=50.001,
        ),
        FinalCase(
            "M9-UB-250MV-HIGH", "m9", "u_b 250 mVpp / 500 kHz boundary", 100_000.0,
            ub_high, ub_high, formal_vpp_min_mv=249.999, formal_vpp_max_mv=250.001,
        ),
        FinalCase(
            "M9-CREST-LOW", "m9", "same spectrum, low crest factor", 40_000.0,
            crest_low, crest_low, formal_vpp_min_mv=100.0, formal_vpp_max_mv=250.0,
        ),
        FinalCase(
            "M9-CREST-HIGH", "m9", "same spectrum, high crest factor", 40_000.0,
            crest_high, crest_high, formal_vpp_min_mv=100.0, formal_vpp_max_mv=250.0,
        ),
        FinalCase(
            "M9-WEAK-H1-5MV", "m9", "5 mVpeak H1 is not the strongest line", 25_000.0,
            weak_h1, weak_h1, formal_vpp_min_mv=50.0, formal_vpp_max_mv=250.0,
        ),
        FinalCase(
            "M9-66K7-H1H3H5", "m9", "manual 66.7 kHz H1/H3/H5 regression", 66_700.0,
            baseline_66k7, baseline_66k7, formal_vpp_min_mv=50.0, formal_vpp_max_mv=250.0,
        ),
        FinalCase(
            "M9-UJ-BASE", "m9", "u_b baseline paired with 1 MHz interference", 50_000.0,
            interference_base, interference_base,
            formal_vpp_min_mv=50.0, formal_vpp_max_mv=250.0,
            interference_pair="M9-UJ-1MHZ-200MVPP",
        ),
        FinalCase(
            "M9-UJ-1MHZ-200MVPP", "m9",
            "same u_b plus exact 1 MHz / 200 mVpp interference", 50_000.0,
            interference_base + interference, interference_base,
            maximum_programmed_vpp=INTERFERENCE_ENVELOPE_MAXIMUM_VPP,
            formal_vpp_min_mv=50.0, formal_vpp_max_mv=250.0,
            interference_pair="M9-UJ-BASE",
        ),
    ]
    return tuple(m8 + m9)


CASES = build_cases()
CASES_BY_ID = {case.case_id: case for case in CASES}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_logged(
    command: list[str | Path],
    output: Path,
    *,
    timeout_s: float = 90.0,
) -> subprocess.CompletedProcess[bytes]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        result = subprocess.run(
            [str(part) for part in command],
            cwd=PROJECT_ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    if result.returncode != 0:
        raise LiveCampaignError(
            f"command failed ({result.returncode}): "
            + " ".join(str(part) for part in command)
        )
    return result


def git_snapshot() -> dict[str, str]:
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if branch != "main":
        raise LiveCampaignError(f"M8/M9 requires main, found {branch!r}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short", "--branch"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {"branch": branch, "head": head, "status": status}


def case_arguments(case: FinalCase, output_dir: Path) -> list[str | Path]:
    command: list[str | Path] = [
        PYTHON,
        GENERATOR,
        "--case-id",
        case.case_id,
        "--fundamental-hz",
        f"{case.fundamental_hz:.12g}",
        "--programmed-amplitude-scale",
        "1",
    ]
    for tone in case.source_tones:
        command.extend(
            (
                "--source-tone",
                str(tone.order),
                f"{tone.peak_mv:.12g}",
                f"{tone.phase_deg:.12g}",
            )
        )
    for tone in case.analysis_tones:
        command.extend(
            (
                "--analysis-tone",
                str(tone.order),
                f"{tone.peak_mv:.12g}",
                f"{tone.phase_deg:.12g}",
            )
        )
    command.extend(("--output-dir", output_dir))
    return command


def validate_generated_case(case: FinalCase, manifest: dict[str, Any]) -> dict[str, Any]:
    source = manifest.get("source", {})
    theory = manifest.get("theory", {})
    programmed = float(source.get("programmed_amplitude_vpp", math.nan))
    target_vpp_mv = float(theory.get("voltage_peak_to_peak_v", math.nan)) * 1000.0
    if not math.isfinite(programmed) or not 0.0 < programmed <= case.maximum_programmed_vpp + 1e-12:
        raise LiveCampaignError(
            f"{case.case_id}: programmed {programmed:g} Vpp exceeds "
            f"{case.maximum_programmed_vpp:g} Vpp"
        )
    if float(source.get("programmed_amplitude_scale", math.nan)) != 1.0:
        raise LiveCampaignError(f"{case.case_id}: final manifest scale is not 1.0")
    if case.formal_vpp_min_mv is not None and target_vpp_mv < case.formal_vpp_min_mv - 1e-9:
        raise LiveCampaignError(f"{case.case_id}: formal Vpp is below its requirement")
    if case.formal_vpp_max_mv is not None and target_vpp_mv > case.formal_vpp_max_mv + 1e-9:
        raise LiveCampaignError(f"{case.case_id}: formal Vpp is above its requirement")
    if case.focus_frequency_hz is not None and not any(
        math.isclose(
            float(tone["frequency_hz"]), case.focus_frequency_hz,
            rel_tol=0.0, abs_tol=1e-6,
        )
        for tone in manifest.get("tones", [])
    ):
        raise LiveCampaignError(f"{case.case_id}: focus frequency is absent")
    return {
        "case_id": case.case_id,
        "stage": case.stage,
        "programmed_vpp": programmed,
        "maximum_programmed_vpp": case.maximum_programmed_vpp,
        "target_vpp_mv": target_vpp_mv,
        "target_rms_mv": float(theory["true_rms_v"]) * 1000.0,
        "focus_frequency_hz": case.focus_frequency_hz,
        "source_frequencies_hz": [
            float(tone["frequency_hz"]) for tone in manifest["source_tones"]
        ],
        "analysis_frequencies_hz": [
            float(tone["frequency_hz"]) for tone in manifest["tones"]
        ],
    }


def generate_case(case: FinalCase, directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_dir = directory / "manifest"
    run_logged(case_arguments(case, manifest_dir), directory / "generator.log")
    manifest = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
    validation = validate_generated_case(case, manifest)
    write_json(directory / "generation-verdict.json", validation)
    return manifest, validation


def start_capture(
    command: list[str | Path], output: Path
) -> tuple[subprocess.Popen[bytes], Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    stream = output.open("wb")
    process = subprocess.Popen(
        [str(part) for part in command],
        cwd=PROJECT_ROOT,
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    return process, stream


def stop_capture(process: subprocess.Popen[bytes] | None, stream: Any) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
    if stream is not None and not stream.closed:
        stream.close()


def transition_to_sine(profile: dict[str, Any], config: Path, directory: Path) -> None:
    function = str(profile["function"])
    if function == "SIN":
        return
    if function == "USER":
        script = USER_TO_SINE
        stem = "user-to-sine"
    elif function in {"HARM", "HARMONIC"}:
        script = HARM_TO_SINE
        stem = "harm-to-sine"
    else:
        raise LiveCampaignError(f"unsupported source transition from {function}")
    run_logged(
        [PYTHON, script, "--config", config, "--output", directory / f"{stem}.json"],
        directory / f"{stem}.log",
    )
    after = legacy.source_profile(config, directory / f"{stem}-profile.txt")
    legacy.assert_safe_profile(after, expected_function="SIN")


def copy_scope_package(log_path: Path, case_dir: Path) -> Path:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^package=(?P<path>.+)$", text, re.MULTILINE)
    if match is None:
        raise LiveCampaignError("WaveBench scope capture did not report a package")
    source = Path(match.group("path").strip()).resolve()
    if not source.is_dir():
        raise LiveCampaignError(f"WaveBench package does not exist: {source}")
    destination = case_dir / "wavebench" / "raw" / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    if not (destination / "ch2.npy").is_file() or not (destination / "metadata.json").is_file():
        raise LiveCampaignError("copied WaveBench package is missing CH2 evidence")
    return destination


def capture_scope(case: FinalCase, manifest: dict[str, Any], case_dir: Path, config: Path) -> dict[str, Any]:
    source_vpp = float(manifest["source"]["programmed_amplitude_vpp"])
    time_range_s = min(0.005, max(0.0002, 50.0 / case.fundamental_hz))
    target_ch2_vpp = min(3.5, max(0.1, source_vpp * 10.0))
    log_path = case_dir / "wavebench-scope-capture.log"
    run_logged(
        [
            WAVEBENCH,
            "scope",
            "capture",
            "--channel",
            "2",
            "--label",
            "final_" + case.case_id.lower().replace("-", "_"),
            "--points",
            "def",
            "--time-range",
            f"{time_range_s:.12g}",
            "--target-vpp",
            f"{target_ch2_vpp:.12g}",
            "--screenshot",
            "--config",
            config,
        ],
        log_path,
        timeout_s=90.0,
    )
    package = copy_scope_package(log_path, case_dir)
    run_logged(
        [WAVEBENCH, "capture", "inspect", package, "--fft", "--harmonics", "20"],
        case_dir / "wavebench-inspect-fft.txt",
        timeout_s=60.0,
    )
    metadata = json.loads((package / "metadata.json").read_text(encoding="utf-8"))
    return {
        "package": str(package.relative_to(case_dir)),
        "package_metadata_sha256": digest(package / "metadata.json"),
        "ch2_npy_sha256": digest(package / "ch2.npy"),
        "screenshot_sha256": digest(package / "screenshot.png"),
        "channels": [2],
        "rtm_ch1_used": False,
        "formal_reference": "DG4202 CH1 50-ohm setting",
        "time_range_s": time_range_s,
        "target_ch2_vpp_for_scale_only": target_ch2_vpp,
        "wavebench_operation": metadata.get("operation"),
    }


def run_acceptance(
    case: FinalCase,
    case_dir: Path,
    frozen_root: Path,
) -> dict[str, Any]:
    output = case_dir / "p4-final-acceptance.json"
    run_logged(
        [
            PYTHON,
            FINAL_ACCEPTANCE,
            "--manifest",
            case_dir / "manifest" / "manifest.json",
            "--mirror-dir",
            case_dir / "mirror",
            "--uart-log",
            case_dir / "p4-uart.log",
            "--fit-dir",
            frozen_root / "fit-v2",
            "--holdout-dir",
            frozen_root / "holdout-v2",
            "--asset-dir",
            frozen_root / "p4-asset-v2",
            "--maximum-programmed-vpp",
            f"{case.maximum_programmed_vpp:.12g}",
            "--output",
            output,
        ],
        case_dir / "p4-final-acceptance.log",
        timeout_s=90.0,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    if report.get("pass") is not True:
        raise LiveCampaignError(
            f"{case.case_id} acceptance failed: "
            + "; ".join(str(item) for item in report.get("failures", []))
        )
    if int(report["selected_mirror_frames"]["source_on_complete_frames"]) < MINIMUM_COMPLETE_FRAMES:
        raise LiveCampaignError(f"{case.case_id} has fewer than 64 active frames")
    return report


def compact_result(
    case: FinalCase,
    generation: dict[str, Any],
    scope: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    target = report["target"]
    p4 = report["p4"]
    return {
        "case_id": case.case_id,
        "stage": case.stage,
        "purpose": case.purpose,
        "pass": True,
        "programmed_vpp": generation["programmed_vpp"],
        "formal_target_vpp_mv": target["voltage_peak_to_peak_mV"],
        "p4": {
            "fundamental_hz": p4["fundamental_hz"]["median"],
            "voltage_peak_to_peak_mV": p4["voltage_peak_to_peak_mV"]["median"],
            "true_rms_mV": p4["true_rms_mV"]["median"],
            "lines": [
                {
                    "order": line["order"],
                    "frequency_hz": line["frequency_hz"]["median"],
                    "amplitude_mVpk": line["amplitude_mVpk"]["median"],
                }
                for line in p4["lines"]
            ],
            "profile": p4["p4_response_profile_id"],
        },
        "target": target,
        "active_mirror_frames": report["selected_mirror_frames"]["source_on_complete_frames"],
        "wavebench_ch2": scope,
        "out_of_band_diagnostics": report["out_of_band_diagnostics"],
    }


def run_live_case(
    case: FinalCase,
    case_dir: Path,
    manifest: dict[str, Any],
    generation: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    before = legacy.ensure_source_off(args.config, case_dir, "before-upload")
    transition_to_sine(before, args.config, case_dir)
    waveform_path = case_dir / "manifest" / str(manifest["waveform"]["path"])
    arb_name = "FC" + hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:10].upper()
    run_logged(
        [
            WAVEBENCH,
            "source",
            "arb-load",
            "--channel",
            "1",
            "--file",
            waveform_path,
            "--name",
            arb_name,
            "--amplitude",
            f"{float(manifest['source']['programmed_amplitude_vpp']):.12g}",
            "--frequency",
            f"{case.fundamental_hz:.12g}",
            "--offset",
            "0",
            "--max-points",
            "16384",
            "--export-payload",
            case_dir / "wavebench-arb-payload.json",
            "--export-dg4000-dac-block",
            case_dir / "wavebench-dg4000-dac.bin",
            "--dg4000-byte-order",
            "little",
            "--config",
            args.config,
        ],
        case_dir / "wavebench-arb-upload.txt",
        timeout_s=90.0,
    )
    uploaded = legacy.source_profile(args.config, case_dir / "source-after-upload.txt")
    legacy.assert_safe_profile(
        uploaded, expected_function="USER", expected_frequency_hz=case.fundamental_hz
    )
    if float(uploaded["amplitude_vpp"]) > case.maximum_programmed_vpp + 1e-9:
        raise LiveCampaignError(f"{case.case_id}: DG readback exceeds case ceiling")

    uart_process: subprocess.Popen[bytes] | None = None
    mirror_process: subprocess.Popen[bytes] | None = None
    uart_stream: Any = None
    mirror_stream: Any = None
    source_enabled = False
    error_text: str | None = None
    scope: dict[str, Any] = {}
    try:
        uart_process, uart_stream = start_capture(
            [
                PYTHON,
                UART_CAPTURE,
                "--port",
                args.uart_port,
                "--baud",
                "115200",
                "--seconds",
                f"{args.on_seconds + 6.0:.6g}",
                "--output",
                case_dir / "p4-uart.log",
            ],
            case_dir / "p4-uart-capture.txt",
        )
        mirror_process, mirror_stream = start_capture(
            [
                PYTHON,
                MIRROR_CAPTURE,
                "--bind",
                MIRROR_BIND,
                "--port",
                str(MIRROR_PORT),
                "--expected-source",
                FPGA_IP,
                "--seconds",
                f"{args.on_seconds:.6g}",
                "--keep-complete-frames",
                "256",
                "--output-dir",
                case_dir / "mirror",
            ],
            case_dir / "mirror-capture.txt",
        )
        time.sleep(0.8)
        if uart_process.poll() is not None:
            raise LiveCampaignError("passive UART capture exited before source ON")
        if mirror_process.poll() is not None:
            raise LiveCampaignError("passive mirror capture exited before source ON")
        legacy.source_output(args.config, True, case_dir / "source-on.txt")
        source_enabled = True
        time.sleep(0.8)
        if not args.no_scope:
            scope = capture_scope(case, manifest, case_dir, args.config)
        mirror_return = mirror_process.wait(timeout=args.on_seconds + 12.0)
        if mirror_return != 0:
            raise LiveCampaignError(f"passive mirror capture exited {mirror_return}")
        legacy.source_output(args.config, False, case_dir / "source-off.txt")
        source_enabled = False
        uart_return = uart_process.wait(timeout=args.on_seconds + 16.0)
        if uart_return != 0:
            raise LiveCampaignError(f"passive UART capture exited {uart_return}")
        final_profile = legacy.source_profile(args.config, case_dir / "source-after.txt")
        legacy.assert_safe_profile(final_profile, expected_function="USER")
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        raise
    finally:
        if source_enabled or error_text is not None:
            try:
                legacy.source_output(
                    args.config, False, case_dir / "source-failsafe-off.txt"
                )
            except Exception as off_error:
                error_text = error_text or f"failsafe OFF failed: {off_error}"
        stop_capture(mirror_process, mirror_stream)
        stop_capture(uart_process, uart_stream)
        if error_text is not None:
            (case_dir / "case-error.txt").write_text(error_text + "\n", encoding="utf-8")

    mirror_summary = legacy.verify_mirror(case_dir / "mirror")
    report = run_acceptance(case, case_dir, args.frozen_root)
    result = compact_result(case, generation, scope, report)
    result["complete_mirror_frames"] = int(mirror_summary["complete_frames"])
    write_json(case_dir / "case-result.json", result)
    legacy.write_sha256s(case_dir)
    return result


def restore_source(config: Path, root: Path) -> dict[str, Any]:
    directory = root / "source-restore"
    directory.mkdir(parents=True, exist_ok=False)
    profile = legacy.source_profile(config, directory / "before-profile.txt")
    if profile["output"] != "OFF":
        legacy.source_output(config, False, directory / "forced-off.txt")
        profile = legacy.source_profile(config, directory / "after-forced-off-profile.txt")
    legacy.assert_safe_profile(profile)
    transition_to_sine(profile, config, directory)
    run_logged(
        [WAVEBENCH, "source", "set-vpp", "0.05", "--channel", "1", "--config", config],
        directory / "set-vpp.txt",
    )
    run_logged(
        [WAVEBENCH, "source", "set-freq", "100000", "--channel", "1", "--config", config],
        directory / "set-frequency.txt",
    )
    final = legacy.source_profile(config, directory / "final-profile.txt")
    legacy.assert_safe_profile(
        final, expected_function="SIN", expected_frequency_hz=100_000.0
    )
    if not math.isclose(float(final["amplitude_vpp"]), 0.05, abs_tol=1e-9):
        raise LiveCampaignError("DG restore amplitude is not 50 mVpp")
    payload = {
        "pass": True,
        "profile": final,
        "expected": {
            "output": "OFF",
            "function": "SIN",
            "frequency_hz": 100_000.0,
            "amplitude_vpp": 0.05,
            "load_ohm": 50.0,
            "offset_v": 0.0,
        },
        "dp800_writes": 0,
        "fpga_changes": False,
    }
    write_json(directory / "restore.json", payload)
    legacy.write_sha256s(directory)
    return payload


def metric_errors(result: dict[str, Any]) -> dict[str, float]:
    p4 = result["p4"]
    target = result["target"]
    errors = {
        "fundamental_hz": abs(float(p4["fundamental_hz"]) - float(target["fundamental_hz"])),
        "voltage_peak_to_peak_mV": abs(
            float(p4["voltage_peak_to_peak_mV"])
            - float(target["voltage_peak_to_peak_mV"])
        ),
        "true_rms_mV": abs(float(p4["true_rms_mV"]) - float(target["true_rms_mV"])),
        "line_frequency_hz": 0.0,
        "line_amplitude_mVpk": 0.0,
    }
    for measured, expected in zip(p4["lines"], target["lines"], strict=True):
        errors["line_frequency_hz"] = max(
            errors["line_frequency_hz"],
            abs(float(measured["frequency_hz"]) - float(expected["frequency_hz"])),
        )
        errors["line_amplitude_mVpk"] = max(
            errors["line_amplitude_mVpk"],
            abs(float(measured["amplitude_mVpk"]) - float(expected["amplitude_mVpk"])),
        )
    return errors


def paired_interference_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {result["case_id"]: result for result in results}
    if "M9-UJ-BASE" not in by_id or "M9-UJ-1MHZ-200MVPP" not in by_id:
        return {"executed": False}
    baseline = by_id["M9-UJ-BASE"]["p4"]
    interfered = by_id["M9-UJ-1MHZ-200MVPP"]["p4"]
    deltas = {
        "fundamental_hz": abs(float(interfered["fundamental_hz"]) - float(baseline["fundamental_hz"])),
        "voltage_peak_to_peak_mV": abs(
            float(interfered["voltage_peak_to_peak_mV"])
            - float(baseline["voltage_peak_to_peak_mV"])
        ),
        "true_rms_mV": abs(float(interfered["true_rms_mV"]) - float(baseline["true_rms_mV"])),
        "line_frequency_hz": 0.0,
        "line_amplitude_mVpk": 0.0,
    }
    for first, second in zip(baseline["lines"], interfered["lines"], strict=True):
        deltas["line_frequency_hz"] = max(
            deltas["line_frequency_hz"],
            abs(float(first["frequency_hz"]) - float(second["frequency_hz"])),
        )
        deltas["line_amplitude_mVpk"] = max(
            deltas["line_amplitude_mVpk"],
            abs(float(first["amplitude_mVpk"]) - float(second["amplitude_mVpk"])),
        )
    passed = (
        deltas["fundamental_hz"] <= 1000.0
        and deltas["line_frequency_hz"] <= 1000.0
        and deltas["voltage_peak_to_peak_mV"] <= 5.0
        and deltas["true_rms_mV"] <= 5.0
        and deltas["line_amplitude_mVpk"] <= 5.0
    )
    return {
        "executed": True,
        "pass": passed,
        "deltas": deltas,
        "interference": "1 MHz / 200 mVpp",
        "combined_programmed_envelope_limit_vpp": INTERFERENCE_ENVELOPE_MAXIMUM_VPP,
        "user_measured_undistorted_limit_vpp": 0.38,
    }


def campaign_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [metric_errors(result) for result in results]
    maxima = {
        key: max((item[key] for item in errors), default=0.0)
        for key in (
            "fundamental_hz",
            "voltage_peak_to_peak_mV",
            "true_rms_mV",
            "line_frequency_hz",
            "line_amplitude_mVpk",
        )
    }
    interference = paired_interference_result(results)
    passed = all(result.get("pass") is True for result in results) and (
        interference.get("executed") is not True or interference.get("pass") is True
    )
    return {
        "pass": passed,
        "case_count": len(results),
        "stage_counts": {
            stage: sum(result["stage"] == stage for result in results)
            for stage in ("m8", "m9")
        },
        "maximum_absolute_errors": maxima,
        "interference_pair": interference,
        "results": results,
    }


def selected_cases(args: argparse.Namespace) -> list[FinalCase]:
    if args.list:
        for case in CASES:
            print(f"{case.stage:3s} {case.case_id:25s} {case.purpose}")
        return []
    requested: list[FinalCase] = []
    if args.all:
        requested.extend(CASES)
    for stage in args.stage:
        requested.extend(case for case in CASES if case.stage == stage)
    for case_id in args.case:
        if case_id not in CASES_BY_ID:
            raise LiveCampaignError(f"unknown case: {case_id}")
        requested.append(CASES_BY_ID[case_id])
    if not requested:
        raise LiveCampaignError("select --all, --stage, or --case explicitly")
    seen: set[str] = set()
    return [case for case in requested if not (case.case_id in seen or seen.add(case.case_id))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--frozen-root", type=Path, default=DEFAULT_FROZEN_ROOT)
    parser.add_argument("--uart-port", default="/dev/ttyUSB0")
    parser.add_argument("--on-seconds", type=float, default=16.0)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--stage", action="append", choices=("m8", "m9"), default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-scope", action="store_true")
    args = parser.parse_args()
    cases = selected_cases(args)
    if args.list:
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required")
    if args.output_dir.exists():
        parser.error("--output-dir must not already exist")
    if not 10.0 <= args.on_seconds <= 30.0:
        parser.error("--on-seconds must be in 10..30")
    for required in (
        WAVEBENCH,
        PYTHON,
        GENERATOR,
        UART_CAPTURE,
        MIRROR_CAPTURE,
        FINAL_ACCEPTANCE,
        args.config,
        args.frozen_root / "fit-v2",
        args.frozen_root / "holdout-v2",
        args.frozen_root / "p4-asset-v2",
    ):
        if not required.exists():
            raise LiveCampaignError(f"required path is missing: {required}")

    profile = acceptance.load_frozen_profile(
        args.frozen_root / "fit-v2",
        args.frozen_root / "holdout-v2",
        args.frozen_root / "p4-asset-v2",
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    manifest_payload = {
        "format": "CycleScope final calibrated P4 M8/M9 campaign v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "dry_run": args.dry_run,
        "git": git_snapshot(),
        "profile": profile.as_dict(),
        "cases": [asdict(case) for case in cases],
        "reference": "DG4202 CH1 50-ohm setting",
        "normal_case_maximum_programmed_vpp": SUPPORTED_MAXIMUM_VPP,
        "interference_case_maximum_programmed_vpp": INTERFERENCE_ENVELOPE_MAXIMUM_VPP,
        "user_measured_maximum_undistorted_vpp": 0.38,
        "scope_channels": [2],
        "rtm_ch1_used": False,
        "dp800_operations": 0,
        "fpga_changes": False,
        "source_control": "WaveBench only",
        "scope_analysis": "WaveBench capture inspect --fft",
        "wavebench_raw_packages_copied_into_each_case": not args.no_scope,
    }
    write_json(args.output_dir / "campaign-manifest.json", manifest_payload)

    generated: list[tuple[FinalCase, Path, dict[str, Any], dict[str, Any]]] = []
    results: list[dict[str, Any]] = []
    error_text: str | None = None
    restore: dict[str, Any] | None = None
    try:
        for case in cases:
            case_dir = args.output_dir / case.case_id
            case_dir.mkdir(parents=True, exist_ok=False)
            manifest, generation = generate_case(case, case_dir)
            generated.append((case, case_dir, manifest, generation))
        write_json(
            args.output_dir / "generation-summary.json",
            [item[3] for item in generated],
        )
        if not args.dry_run:
            legacy.ensure_source_off(args.config, args.output_dir, "preflight")
            for case, case_dir, manifest, generation in generated:
                result = run_live_case(case, case_dir, manifest, generation, args)
                results.append(result)
                write_json(args.output_dir / "campaign-progress.json", results)
        else:
            results = [
                {
                    "case_id": case.case_id,
                    "stage": case.stage,
                    "purpose": case.purpose,
                    "dry_run": True,
                    **generation,
                }
                for case, _case_dir, _manifest, generation in generated
            ]
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
    finally:
        if not args.dry_run:
            try:
                restore = restore_source(args.config, args.output_dir)
            except Exception as restore_error:
                restore_text = f"{type(restore_error).__name__}: {restore_error}"
                error_text = (
                    f"{error_text}; restore={restore_text}"
                    if error_text is not None
                    else f"restore={restore_text}"
                )

    if args.dry_run:
        summary = {"pass": error_text is None, "dry_run": True, "results": results}
    else:
        summary = campaign_summary(results)
        summary["source_restore"] = restore
        summary["expected_case_count"] = len(cases)
        if len(results) != len(cases):
            summary["pass"] = False
        if error_text is not None:
            summary["pass"] = False
            summary["error"] = error_text
    write_json(args.output_dir / "campaign-summary.json", summary)
    legacy.write_sha256s(args.output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
