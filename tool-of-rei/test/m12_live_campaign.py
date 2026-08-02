#!/usr/bin/env python3
"""Safely run selected M12 ARB -> FPGA mirror -> ESP32-P4 acceptance cases.

This private fixture deliberately uses only reviewed WaveBench source commands.
It never calls the DP800, never emits raw SCPI, never sends CSLP control
traffic, and always attempts DG4202 CH1 OFF before returning from a live case.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WAVEBENCH_ROOT = PROJECT_ROOT / "tools" / "wavebench"
WAVEBENCH = WAVEBENCH_ROOT / ".venv" / "bin" / "wavebench"
PYTHON = WAVEBENCH_ROOT / ".venv" / "bin" / "python"
GENERATOR = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_generate_arb.py"
MIRROR_CAPTURE = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_passive_mirror_capture.py"
UART_CAPTURE = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_passive_uart_capture.py"
ACCEPTANCE = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_p4_acceptance.py"
USER_TO_SINE = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_user_to_sine_transition.py"
HARM_TO_SINE = PROJECT_ROOT / "tool-of-rei" / "test" / "m12_harm_to_sine_transition.py"
DEFAULT_CONFIG = PROJECT_ROOT / "tool-of-rei" / "m12-wavebench-safe.toml"

MIRROR_BIND = "192.168.10.4"
MIRROR_PORT = 50002
FPGA_IP = "192.168.10.2"
MINIMUM_COMPLETE_FRAMES = 64
DEFAULT_PROGRAMMED_AMPLITUDE_SCALE = 0.5


class CampaignError(RuntimeError):
    """A live M12 case cannot safely continue."""


@dataclass(frozen=True)
class Tone:
    order: int
    peak_mv: float
    phase_deg: float


@dataclass(frozen=True)
class Case:
    case_id: str
    group: str
    fundamental_hz: float
    source_tones: tuple[Tone, ...]
    analysis_tones: tuple[Tone, ...]


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def tones(*values: tuple[int, float, float]) -> tuple[Tone, ...]:
    return tuple(Tone(*value) for value in values)


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for order in range(2, 17):
        line_set = tones((1, 5.0, 0.0), (order, 20.0, 0.0))
        cases.append(Case(f"MASK-H{order:02d}", "mask-h2-h16", 25_000.0, line_set, line_set))
    for order in range(2, 17):
        line_set = tones((1, 5.0, 0.0), (order, 25.0, 0.0))
        cases.append(
            Case(
                f"ORDER-400K-H{order:02d}",
                "order-400k",
                400_000.0 / order,
                line_set,
                line_set,
            )
        )
    for h2_phase in (0.0, 90.0, 180.0, 270.0):
        for h3_phase in (0.0, 90.0, 180.0, 270.0):
            line_set = tones((1, 40.0, 0.0), (2, 25.0, h2_phase), (3, 15.0, h3_phase))
            cases.append(
                Case(
                    f"PHASE-H2-{int(h2_phase):03d}-H3-{int(h3_phase):03d}",
                    "phase",
                    50_000.0,
                    line_set,
                    line_set,
                )
            )
    crest_low = tones((1, 40.0, 0.0), (3, 30.0, 270.0), (5, 20.0, 300.0))
    crest_high = tones((1, 40.0, 0.0), (3, 30.0, 180.0), (5, 20.0, 0.0))
    cases.extend(
        (
            Case("UA-CREST-LOW", "ua-ub", 40_000.0, crest_low, crest_low),
            Case("UA-CREST-HIGH", "ua-ub", 40_000.0, crest_high, crest_high),
        )
    )
    ua_base_weak = tones((1, 10.0, 0.0), (3, 50.0, 0.0), (4, 30.0, 0.0))
    ub_low = tones((1, 20.0, 0.0), (2, 10.0, 0.0), (5, 5.0, 0.0))
    ub_high = tones((1, 99.0, 0.0), (3, 59.0, 0.0), (5, 29.0, 0.0))
    ub_h16 = tones((1, 20.0, 0.0), (7, 40.0, 0.0), (16, 50.0, 0.0))
    ub_weak = tones((1, 5.0, 0.0), (8, 40.0, 0.0), (16, 50.0, 0.0))
    cases.extend(
        (
            Case("UA-BASE-WEAK", "ua-ub", 40_000.0, ua_base_weak, ua_base_weak),
            Case("UB-LOW", "ua-ub", 10_000.0, ub_low, ub_low),
            Case("UB-HIGH", "ua-ub", 100_000.0, ub_high, ub_high),
            Case("UB-H16", "ua-ub", 30_000.0, ub_h16, ub_h16),
            Case("UB-WEAK", "ua-ub", 25_000.0, ub_weak, ub_weak),
        )
    )
    j_100k = tones((1, 50.0, 0.0), (3, 30.0, 0.0), (5, 20.0, 0.0))
    j_125k = tones((1, 50.0, 0.0), (2, 30.0, 0.0), (4, 20.0, 0.0))
    cases.extend(
        (
            Case("J-H10-1M-BASE", "ub-plus-interference", 100_000.0, j_100k, j_100k),
            Case(
                "J-H10-1M",
                "ub-plus-interference",
                100_000.0,
                j_100k + tones((10, 100.0, 90.0)),
                j_100k,
            ),
            Case("J-H16-1M6-BASE", "ub-plus-interference", 100_000.0, j_100k, j_100k),
            Case(
                "J-H16-1M6",
                "ub-plus-interference",
                100_000.0,
                j_100k + tones((16, 100.0, 90.0)),
                j_100k,
            ),
            Case("J-H16-2M-BASE", "ub-plus-interference", 125_000.0, j_125k, j_125k),
            Case(
                "J-H16-2M",
                "ub-plus-interference",
                125_000.0,
                j_125k + tones((16, 100.0, 90.0)),
                j_125k,
            ),
        )
    )
    return cases


def command_text(command: list[str | Path]) -> str:
    return " ".join(str(part) for part in command)


def run_logged(
    command: list[str | Path],
    output: Path,
    *,
    timeout_s: float = 60.0,
    check: bool = True,
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
    if check and result.returncode != 0:
        raise CampaignError(f"command failed ({result.returncode}): {command_text(command)}")
    return result


def profile_from_text(text: str) -> dict[str, Any]:
    status = re.search(
        r"^CH1: output=(?P<output>\S+) func=(?P<function>\S+) "
        r"freq=(?P<frequency>[0-9.eE+-]+)Hz "
        r"amp=(?P<amplitude>[0-9.eE+-]+)VPP .*?offset=(?P<offset>[0-9.eE+-]+)V(?:\s|$)",
        text,
        re.MULTILINE,
    )
    load = re.search(r"^load_ohm=(?P<load>[0-9.eE+-]+)$", text, re.MULTILINE)
    if status is None or load is None:
        raise CampaignError("could not parse required DG4202 source profile fields")
    return {
        "output": status.group("output"),
        "function": status.group("function"),
        "frequency_hz": float(status.group("frequency")),
        "amplitude_vpp": float(status.group("amplitude")),
        "offset_v": float(status.group("offset")),
        "load_ohm": float(load.group("load")),
        "raw": text,
    }


def source_profile(config: Path, output: Path) -> dict[str, Any]:
    run_logged(
        [WAVEBENCH, "source", "profile", "--config", config, "--channel", "1"],
        output,
    )
    return profile_from_text(output.read_text(encoding="utf-8", errors="replace"))


def source_output(config: Path, enabled: bool, output: Path) -> None:
    run_logged(
        [
            WAVEBENCH,
            "source",
            "output",
            "on" if enabled else "off",
            "--config",
            config,
            "--channel",
            "1",
        ],
        output,
    )


def assert_safe_profile(
    profile: dict[str, Any],
    *,
    expected_function: str | None = None,
    expected_frequency_hz: float | None = None,
) -> None:
    failures: list[str] = []
    if profile["output"] != "OFF":
        failures.append("output is not OFF")
    if not math.isclose(float(profile["load_ohm"]), 50.0, abs_tol=1.0e-9):
        failures.append("load is not 50 ohm")
    if not math.isclose(float(profile["offset_v"]), 0.0, abs_tol=1.0e-9):
        failures.append("offset is not 0 V")
    if not 0.0 < float(profile["amplitude_vpp"]) <= 0.5:
        failures.append("amplitude is outside the 0..0.5 Vpp safety envelope")
    if expected_function is not None and profile["function"] != expected_function:
        failures.append(f"function is {profile['function']}, expected {expected_function}")
    if expected_frequency_hz is not None and not math.isclose(
        # WaveBench's human-readable DG4202 profile prints frequency to
        # 0.1 Hz, so its audit readback cannot truthfully support a tighter
        # textual comparison for fractional 400 kHz/order frequencies.
        float(profile["frequency_hz"]), expected_frequency_hz, rel_tol=0.0, abs_tol=0.1
    ):
        failures.append("frequency readback differs from the manifest")
    if failures:
        raise CampaignError("unsafe/unexpected DG4202 profile: " + "; ".join(failures))


def ensure_source_off(config: Path, directory: Path, label: str) -> dict[str, Any]:
    profile = source_profile(config, directory / f"{label}-profile.txt")
    if profile["output"] != "OFF":
        source_output(config, False, directory / f"{label}-forced-off.txt")
        raise CampaignError("DG4202 was unexpectedly ON; forced OFF and stopped the campaign")
    assert_safe_profile(profile)
    return profile


def transition_to_sine_if_needed(profile: dict[str, Any], config: Path, directory: Path) -> None:
    function = str(profile["function"])
    if function == "SIN":
        return
    if function == "USER":
        transition = USER_TO_SINE
        stem = "user-to-sine"
    elif function == "HARM":
        transition = HARM_TO_SINE
        stem = "harm-to-sine"
    else:
        raise CampaignError(f"refusing ARB upload from unsupported source function {function}")
    run_logged(
        [PYTHON, transition, "--config", config, "--output", directory / f"{stem}.json"],
        directory / f"{stem}.log",
    )
    transitioned = source_profile(config, directory / f"{stem}-profile.txt")
    assert_safe_profile(transitioned, expected_function="SIN")


def case_arguments(case: Case, manifest_directory: Path, programmed_scale: float) -> list[str | Path]:
    arguments: list[str | Path] = [
        PYTHON,
        GENERATOR,
        "--case-id",
        case.case_id,
        "--fundamental-hz",
        f"{case.fundamental_hz:.12g}",
        "--programmed-amplitude-scale",
        f"{programmed_scale:.12g}",
    ]
    for tone in case.source_tones:
        arguments.extend(
            ("--source-tone", str(tone.order), f"{tone.peak_mv:.12g}", f"{tone.phase_deg:.12g}")
        )
    for tone in case.analysis_tones:
        arguments.extend(
            ("--analysis-tone", str(tone.order), f"{tone.peak_mv:.12g}", f"{tone.phase_deg:.12g}")
        )
    arguments.extend(("--output-dir", manifest_directory))
    return arguments


def start_capture_process(command: list[str | Path], output: Path) -> tuple[subprocess.Popen[bytes], Any]:
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
    if stream is not None:
        stream.close()


def verify_mirror(directory: Path) -> dict[str, Any]:
    summary_path = directory / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"cannot read mirror summary: {error}") from error
    if summary.get("network_writes") != 0:
        raise CampaignError("passive mirror capture reported network writes")
    if int(summary.get("complete_frames", 0)) < MINIMUM_COMPLETE_FRAMES:
        raise CampaignError("mirror capture contains fewer than 64 complete frames")
    forbidden = (
        "unexpected_source",
        "invalid_cslp",
        "bad_cslp_crc",
        "invalid_wave_geometry",
        "invalid_wave_payload_bytes",
        "bad_reassembled_length",
    )
    counts = summary.get("counts", {})
    if not isinstance(counts, dict):
        raise CampaignError("mirror capture counts are malformed")
    present = [key for key in forbidden if int(counts.get(key, 0)) != 0]
    if present:
        raise CampaignError("mirror capture protocol errors: " + ", ".join(present))
    return summary


def write_sha256s(directory: Path) -> None:
    lines: list[str] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        lines.append(f"{digest(path)}  {path.relative_to(directory)}")
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_acceptance(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"cannot read P4 acceptance report: {error}") from error
    if report.get("pass") is not True:
        raise CampaignError("P4 acceptance failed: " + "; ".join(report.get("failures", [])))
    source_on = report.get("selected_mirror_frames", {}).get("source_on_complete_frames", 0)
    if int(source_on) < MINIMUM_COMPLETE_FRAMES:
        raise CampaignError("P4 acceptance selected fewer than 64 source-ON complete frames")
    return report


def compact_case_result(case: Case, report: dict[str, Any], mirror: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "group": case.group,
        "pass": report["pass"],
        "complete_mirror_frames": mirror["complete_frames"],
        "source_on_mirror_frames": report["selected_mirror_frames"]["source_on_complete_frames"],
        "p4": {
            "fundamental_hz": report["p4"]["fundamental_hz"]["median"],
            "voltage_peak_to_peak_mV": report["p4"]["voltage_peak_to_peak_mV"]["median"],
            "true_rms_mV": report["p4"]["true_rms_mV"]["median"],
            "lines": [
                {
                    "order": line["order"],
                    "frequency_hz": line["frequency_hz"]["median"],
                    "amplitude_mVpk": line["amplitude_mVpk"]["median"],
                }
                for line in report["p4"]["lines"]
            ],
        },
        "target": report["target"],
        "p4_time_view_from_mirror": report["p4_time_view_from_mirror"],
    }


def run_case(case: Case, args: argparse.Namespace, campaign_directory: Path) -> dict[str, Any]:
    directory = campaign_directory / case.case_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest_directory = directory / "manifest"
    run_logged(
        case_arguments(case, manifest_directory, args.programmed_amplitude_scale),
        directory / "generator.log",
    )
    manifest_path = manifest_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    waveform_path = manifest_directory / str(manifest["waveform"]["path"])
    if not waveform_path.is_file():
        raise CampaignError("generated ARB waveform is missing")

    before_upload = ensure_source_off(args.config, directory, "before-upload")
    transition_to_sine_if_needed(before_upload, args.config, directory)
    arb_name = "M12" + hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:8].upper()
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
            directory / "wavebench-arb-payload.json",
            "--export-dg4000-dac-block",
            directory / "wavebench-dg4000-dac.bin",
            "--dg4000-byte-order",
            "little",
            "--config",
            args.config,
        ],
        directory / "wavebench-arb-upload.txt",
        timeout_s=90.0,
    )
    uploaded = source_profile(args.config, directory / "source-after-upload.txt")
    assert_safe_profile(
        uploaded,
        expected_function="USER",
        expected_frequency_hz=case.fundamental_hz,
    )

    uart_process: subprocess.Popen[bytes] | None = None
    mirror_process: subprocess.Popen[bytes] | None = None
    uart_stream: Any = None
    mirror_stream: Any = None
    source_enabled = False
    error_text: str | None = None
    try:
        uart_process, uart_stream = start_capture_process(
            [
                PYTHON,
                UART_CAPTURE,
                "--port",
                args.uart_port,
                "--baud",
                "115200",
                "--seconds",
                f"{args.on_seconds + 4.0:.6g}",
                "--output",
                directory / "p4-uart.log",
            ],
            directory / "p4-uart-capture.txt",
        )
        mirror_process, mirror_stream = start_capture_process(
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
                directory / "mirror",
            ],
            directory / "mirror-capture.txt",
        )
        time.sleep(0.8)
        source_output(args.config, True, directory / "source-on.txt")
        source_enabled = True
        mirror_return = mirror_process.wait(timeout=args.on_seconds + 8.0)
        if mirror_return != 0:
            raise CampaignError(f"passive mirror capture exited {mirror_return}")
        source_output(args.config, False, directory / "source-off.txt")
        source_enabled = False
        uart_return = uart_process.wait(timeout=args.on_seconds + 12.0)
        if uart_return != 0:
            raise CampaignError(f"passive UART capture exited {uart_return}")
        final_profile = source_profile(args.config, directory / "source-after.txt")
        assert_safe_profile(final_profile, expected_function="USER")
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        raise
    finally:
        if source_enabled or error_text is not None:
            try:
                source_output(args.config, False, directory / "source-failsafe-off.txt")
            except Exception as error:
                error_text = error_text or f"failsafe source OFF: {type(error).__name__}: {error}"
        stop_capture(mirror_process, mirror_stream)
        stop_capture(uart_process, uart_stream)
        if error_text is not None:
            (directory / "case-error.txt").write_text(error_text + "\n", encoding="utf-8")

    mirror = verify_mirror(directory / "mirror")
    acceptance_path = directory / "p4-acceptance.json"
    run_logged(
        [
            PYTHON,
            ACCEPTANCE,
            "--manifest",
            manifest_path,
            "--mirror-dir",
            directory / "mirror",
            "--uart-log",
            directory / "p4-uart.log",
            "--output",
            acceptance_path,
        ],
        directory / "p4-acceptance.log",
    )
    report = read_acceptance(acceptance_path)
    result = compact_case_result(case, report, mirror)
    (directory / "case-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_sha256s(directory)
    return result


def selected_cases(args: argparse.Namespace) -> list[Case]:
    cases = build_cases()
    by_id = {case.case_id: case for case in cases}
    if args.list:
        for case in cases:
            print(f"{case.group:22s} {case.case_id}")
        return []
    if not args.all and not args.case and not args.group:
        raise CampaignError("select --all, --group, or --case explicitly")
    requested: list[Case] = []
    if args.all:
        requested.extend(cases)
    for group in args.group:
        group_cases = [case for case in cases if case.group == group]
        if not group_cases:
            raise CampaignError(f"unknown case group: {group}")
        requested.extend(group_cases)
    for case_id in args.case:
        case = by_id.get(case_id)
        if case is None:
            raise CampaignError(f"unknown case id: {case_id}")
        requested.append(case)
    seen: set[str] = set()
    return [case for case in requested if not (case.case_id in seen or seen.add(case.case_id))]


def generate_only(case: Case, args: argparse.Namespace, campaign_directory: Path) -> dict[str, Any]:
    directory = campaign_directory / case.case_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest_directory = directory / "manifest"
    run_logged(
        case_arguments(case, manifest_directory, args.programmed_amplitude_scale),
        directory / "generator.log",
    )
    manifest = json.loads((manifest_directory / "manifest.json").read_text(encoding="utf-8"))
    result = {
        "case_id": case.case_id,
        "group": case.group,
        "dry_run": True,
        "source_component_vpp_sum_mV": manifest["source"]["component_vpp_sum"] * 1000.0,
        "source_fullscale_vpp_mV": manifest["source"]["source_fullscale_vpp"] * 1000.0,
        "target_vpp_mV": manifest["theory"]["voltage_peak_to_peak_v"] * 1000.0,
        "target_rms_mV": manifest["theory"]["true_rms_v"] * 1000.0,
    }
    (directory / "case-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_sha256s(directory)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=False)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--on-seconds", type=float, default=12.0)
    parser.add_argument("--uart-port", default="/dev/ttyUSB1")
    parser.add_argument(
        "--programmed-amplitude-scale",
        type=float,
        default=DEFAULT_PROGRAMMED_AMPLITUDE_SCALE,
    )
    args = parser.parse_args()
    if args.list:
        selected_cases(args)
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required unless --list is used")
    if args.output_dir.exists():
        parser.error("--output-dir must not already exist")
    if not args.config.is_file() or not WAVEBENCH.is_file() or not PYTHON.is_file():
        raise CampaignError("required WaveBench executable or config is unavailable")
    if not 8.0 <= args.on_seconds <= 30.0:
        parser.error("--on-seconds must be in 8..30")
    if not 0.0 < args.programmed_amplitude_scale <= 1.0:
        parser.error("--programmed-amplitude-scale must be in (0, 1]")
    cases = selected_cases(args)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    campaign = {
        "format": "CycleScope M12 P4-only live campaign v1",
        "dry_run": args.dry_run,
        "config": str(args.config),
        "source_control": "WaveBench source profile/arb-load/output only",
        "dp800_operations": 0,
        "raw_scpi_entrypoint_used": False,
        "cases": [asdict(case) for case in cases],
        "on_seconds": args.on_seconds,
        "programmed_amplitude_scale": args.programmed_amplitude_scale,
    }
    (args.output_dir / "campaign-manifest.json").write_text(
        json.dumps(campaign, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    results: list[dict[str, Any]] = []
    try:
        if not args.dry_run:
            ensure_source_off(args.config, args.output_dir, "preflight")
        for case in cases:
            result = generate_only(case, args, args.output_dir) if args.dry_run else run_case(case, args, args.output_dir)
            results.append(result)
            (args.output_dir / "campaign-progress.json").write_text(
                json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        if not args.dry_run:
            ensure_source_off(args.config, args.output_dir, "final")
    except Exception as error:
        if not args.dry_run:
            try:
                source_output(args.config, False, args.output_dir / "campaign-failsafe-off.txt")
            except Exception:
                pass
        summary = {"pass": False, "results": results, "error": f"{type(error).__name__}: {error}"}
        (args.output_dir / "campaign-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        write_sha256s(args.output_dir)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 2
    summary = {"pass": True, "results": results}
    (args.output_dir / "campaign-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_sha256s(args.output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
