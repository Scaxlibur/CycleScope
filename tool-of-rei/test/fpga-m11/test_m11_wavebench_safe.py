from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from wavebench.instruments.models import (
    PowerProtectionStatus,
    PowerStatus,
    SourceChannelProfile,
    SourceCouplingProfile,
    SourceStatus,
)


SCRIPT = Path(__file__).with_name("m11_wavebench_safe.py")
SPEC = importlib.util.spec_from_file_location("m11_wavebench_safe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
m11 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m11)


def source_profile(**changes):
    status_values = {
        "channel": 1,
        "output": "OFF",
        "function": "SIN",
        "frequency_hz": 100_000.0,
        "amplitude": 0.05,
        "amplitude_unit": "VPP",
        "offset_v": 0.0,
        "phase_deg": 0.0,
        "frequency_mode": "FIX",
        "sweep_enabled": "OFF",
        "apply_raw": None,
        "square_duty_cycle_percent": None,
    }
    status_values.update(changes.pop("status", {}))
    values = {
        "status": SourceStatus(**status_values),
        "load_ohm": 50.0,
        "polarity": "NORMAL",
        "noise_enabled": False,
        "noise_scale_percent": 0.0,
        "sync_enabled": True,
        "sync_polarity": "POSITIVE",
        "burst_enabled": False,
        "modulation_enabled": False,
        "modulation_type": "AM",
        "marker_enabled": False,
        "pulse_hold": "DUTY",
    }
    values.update(changes)
    return SourceChannelProfile(**values)


def source_coupling(**changes):
    values = {
        "base_channel": 1,
        "frequency_enabled": False,
        "frequency_deviation_hz": 0.0,
        "phase_enabled": False,
        "phase_deviation_deg": 0.0,
        "amplitude_enabled": False,
        "amplitude_deviation_vpp": 0.0,
    }
    values.update(changes)
    return SourceCouplingProfile(**values)


def power_status(**changes):
    values = {
        "channel": 1,
        "output": "ON",
        "mode": "CV",
        "rating": "30V/3A",
        "set_voltage_v": 5.0,
        "set_current_a": 0.2,
        "measured_voltage_v": 5.01,
        "measured_current_a": 0.1,
        "measured_power_w": 0.501,
    }
    values.update(changes)
    return PowerStatus(**values)


def protection(**changes):
    values = {
        "channel": 1,
        "ovp_enabled": "YES",
        "ovp_threshold_v": 5.5,
        "ovp_tripped": "NO",
        "ocp_enabled": "YES",
        "ocp_threshold_a": 0.3,
        "ocp_tripped": "NO",
    }
    values.update(changes)
    return PowerProtectionStatus(**values)


class M11WaveBenchSafetyTests(unittest.TestCase):
    def test_derived_config_is_private_in_memory_and_bounded(self):
        config = m11.derived_config()
        self.assertEqual(config.safety_limits.max_source_vpp, 0.5)
        self.assertEqual(config.source.driver, "rigol.dg4202")
        self.assertEqual(config.scope.driver, "rohde-schwarz.rtm2032")
        self.assertEqual(config.power.driver, "rigol.dp800")
        self.assertTrue(config.output.directory.is_absolute())
        self.assertEqual(config.output.directory.resolve(), m11.WAVEBENCH_RAW_DIR.resolve())

    def test_nominal_preflight_passes(self):
        failures = m11.evaluate_preflight(
            source_profile=source_profile(),
            source_coupling=source_coupling(),
            scope_couplings={1: "DCL", 2: "DCL"},
            power_status=power_status(),
            power_protection=protection(),
        )
        self.assertEqual(failures, [])

    def test_impedance_and_power_deviations_fail_closed(self):
        failures = m11.evaluate_preflight(
            source_profile=source_profile(load_ohm=None),
            source_coupling=source_coupling(amplitude_enabled=True),
            scope_couplings={1: "DCL"},
            power_status=power_status(set_voltage_v=9.0, measured_voltage_v=9.0),
            power_protection=protection(ocp_tripped="YES"),
        )
        joined = "\n".join(failures)
        self.assertIn("load must remain 50 ohm", joined)
        self.assertIn("coupling must be fully OFF", joined)
        self.assertIn("RTM CH2", joined)
        self.assertIn("single-5V gate", joined)
        self.assertIn("OCP trip", joined)

    def test_readonly_plans_have_no_mutating_steps(self):
        config = m11.derived_config()
        records = [m11.validate_readonly_plan(path, config) for path in m11.plan_paths()]
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertTrue(set(record["steps"]) <= m11.READONLY_STEP_KINDS)

    def test_safe_off_plan_has_one_non_restored_off_write(self):
        record = m11.validate_safe_off_plan(m11.derived_config())
        self.assertEqual(record["only_write"], "source.output off")
        self.assertFalse(record["restore_to_on"])

    def test_scope_trace_metrics_are_finite_and_physical(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.npy"
            times = np.arange(1000, dtype=np.float64) * 1e-6
            values = 0.1 * np.sin(2 * np.pi * 1000 * times)
            np.save(path, np.column_stack((times, values)))
            metrics = m11._scope_trace_metrics(path)
        self.assertEqual(metrics["samples"], 1000)
        self.assertAlmostEqual(metrics["sample_rate_hz"], 1_000_000.0, places=3)
        self.assertGreater(metrics["vpp_v"], 0.19)

    def test_zero_profiles_separate_effective_band_and_hf_screen(self):
        noise = m11._zero_scope_profile("noise-500k")
        hf = m11._zero_scope_profile("hf-spur")
        self.assertTrue(noise["require_dcl"])
        self.assertTrue(hf["require_dcl"])
        self.assertEqual(noise["spectrum_max_hz"], 500_000.0)
        self.assertEqual(hf["spectrum_min_hz"], 500_000.0)
        self.assertLess(hf["time_range_s"], noise["time_range_s"])
        self.assertLessEqual(noise["vertical_scale_v_per_div"], 0.02)

    def test_formal_zero_profile_requires_dual_dcl(self):
        m11._require_zero_profile_preflight(
            {"scope": {"couplings": {"1": "DCL", "2": "DCL"}}},
            "noise-500k",
        )
        with self.assertRaisesRegex(m11.M11SafetyError, "requires dual DCL"):
            m11._require_zero_profile_preflight(
                {"scope": {"couplings": {"1": "ACL", "2": "DCL"}}},
                "noise-500k",
            )

    def test_spectrum_metrics_find_known_tone(self):
        sample_rate_hz = 2_000_000.0
        samples = 10_000
        times = np.arange(samples, dtype=np.float64) / sample_rate_hz
        values = 0.01 * np.sin(2 * np.pi * 100_000.0 * times)
        metrics = m11._spectrum_metrics(
            values,
            sample_rate_hz,
            band_min_hz=0.0,
            band_max_hz=500_000.0,
        )
        self.assertAlmostEqual(metrics["resolution_hz"], 200.0)
        self.assertAlmostEqual(metrics["max_spur_frequency_hz"], 100_000.0)
        self.assertAlmostEqual(metrics["max_spur_peak"], 0.01, places=5)
        self.assertAlmostEqual(
            metrics["band_integrated_rms"], 0.01 / np.sqrt(2.0), places=5
        )

    def test_adc_zero_metrics_verify_hash_and_preserve_raw(self):
        with TemporaryDirectory() as temporary:
            capture = Path(temporary)
            frames = []
            for index in range(2):
                values = np.full(8192, 13, dtype="<i2")
                values[index::2] = 14
                path = capture / f"frame_{index:05d}.s16le"
                raw = values.tobytes()
                path.write_bytes(raw)
                frames.append(
                    {
                        "frame_index": index,
                        "file": path.name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            (capture / "capture.json").write_text(
                json.dumps(
                    {
                        "format": "CycleScope CSLP independent complete frames v1",
                        "partial": False,
                        "sample_rate_hz": 4_062_500,
                        "frame_count": len(frames),
                        "frames": frames,
                    }
                ),
                encoding="utf-8",
            )
            metrics = m11._adc_zero_metrics(capture)
        self.assertEqual(metrics["frame_count"], 2)
        self.assertEqual(metrics["sample_min_code"], 13)
        self.assertEqual(metrics["sample_max_code"], 14)
        self.assertEqual(metrics["sample_unique_values"], 2)
        self.assertEqual(metrics["total_outliers"], 0)
        self.assertFalse(metrics["raw_samples_modified"])
        self.assertLessEqual(metrics["spectrum_0_500k"]["resolution_hz"], 500.0)

    def test_raw_archive_selection_uses_exact_returned_package(self):
        with TemporaryDirectory() as temporary:
            raw_root = Path(temporary) / "data" / "raw"
            old_package = raw_root / "old"
            new_package = raw_root / "timestamp_label_with_sanitized__0800"
            old_package.mkdir(parents=True)
            new_package.mkdir()
            with patch.object(m11, "WAVEBENCH_RAW_DIR", raw_root):
                selected = m11._select_new_scope_raw_packages(
                    scope_result={"package": str(new_package)},
                    raw_before={old_package.resolve()},
                    raw_after={old_package.resolve(), new_package.resolve()},
                )
        self.assertEqual(selected, {new_package.resolve()})

    def test_raw_archive_selection_rejects_preexisting_package(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary) / "data" / "raw" / "old"
            package.mkdir(parents=True)
            with self.assertRaisesRegex(m11.M11SafetyError, "current acquisition"):
                m11._select_new_scope_raw_packages(
                    scope_result={"package": str(package)},
                    raw_before={package.resolve()},
                    raw_after={package.resolve()},
                )

    def test_zero_live_rejects_too_few_frames_before_io(self):
        with self.assertRaisesRegex(m11.M11SafetyError, "at least 64"):
            m11.zero_live(m11.LIVE_ACK, frames=63)

    def test_zero_live_rejects_unknown_profile_before_io(self):
        with self.assertRaisesRegex(m11.M11SafetyError, "unknown zero-input profile"):
            m11.zero_live(m11.LIVE_ACK, frames=64, profile_name="mystery")

    def test_lan_capture_has_explicit_uncalibrated_metadata_defaults(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"--expected-calibration-id"', source)
        self.assertIn('"--expected-scale-uv-per-lsb"', source)
        self.assertIn('"--expected-offset-uv"', source)

    def test_lan_capture_rejects_invalid_longrun_controls_before_io(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(m11.M11SafetyError, "frame samples"):
                m11._capture_zero_lan(root, 2, frame_samples=4096)
            with self.assertRaisesRegex(m11.M11SafetyError, "timeout"):
                m11._capture_zero_lan(root, 2, run_timeout_s=0.0)
            with self.assertRaisesRegex(m11.M11SafetyError, "progress"):
                m11._capture_zero_lan(root, 2, progress_every=0)
        self.assertFalse((root / "lan").exists())

    def test_lan_preflight_forwards_a_nonzero_calibration_identity(self):
        with TemporaryDirectory() as temporary:
            with (
                patch.object(m11, "EVIDENCE_ROOT", Path(temporary)),
                patch.object(m11, "now_stamp", return_value="calibrated"),
                patch.object(
                    m11,
                    "_capture_zero_lan",
                    return_value={"pass": True, "failures": []},
                ) as capture,
            ):
                result = m11.lan_preflight(
                    m11.LIVE_ACK,
                    instrument_preflight={"pass": True, "evidence_path": "preflight.json"},
                    frame_samples=16384,
                    expected_calibration_id=25030,
                    expected_scale_uv_per_lsb=516,
                    expected_offset_uv=-6761,
                )
        self.assertTrue(result["pass"])
        capture.assert_called_once_with(
            Path(temporary) / "preflight" / "calibrated_lan-smoke",
            2,
            frame_samples=16384,
            expected_calibration_id=25030,
            expected_scale_uv_per_lsb=516,
            expected_offset_uv=-6761,
            archive_packets=False,
        )

    def test_packet_capture_command_is_complete_and_direction_scoped(self):
        command = m11._packet_capture_command(Path("/tmp/cyclescope-test.pcap"))
        self.assertEqual(command[:3], [str(m11.SUDO), "-n", str(m11.TCPDUMP)])
        self.assertIn("-s", command)
        self.assertEqual(command[command.index("-s") + 1], "0")
        self.assertIn("-w", command)
        self.assertIn("src host 192.168.10.2", command[-1])
        self.assertIn("dst host 192.168.10.4", command[-1])
        self.assertIn("udp", command[-1])

    def test_source_data_archive_copies_pcap_exactly(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            point_dir = root / "point"
            point_dir.mkdir()
            pcap_path = point_dir / "wire.pcap"
            tcpdump_log = point_dir / "tcpdump.log"
            pcap_report = point_dir / "pcap-analysis.json"
            lan_report = point_dir / "lan.json"
            pcap_path.write_bytes(b"pcap-test-payload")
            tcpdump_log.write_text("0 packets dropped by kernel\n", encoding="utf-8")
            pcap_report.write_text('{"pass": true}\n', encoding="utf-8")
            lan_report.write_text('{"pass": true}\n', encoding="utf-8")
            report = {
                "pass": True,
                "counts": {"pcap_packets": 1, "target_wave_packets": 1},
                "cslp_message_types": {"0x20": 1},
            }
            with patch.object(m11, "SOURCE_DATA_ROOT", root / "source_data"):
                archive = m11._archive_packet_source(
                    point_dir,
                    pcap_path=pcap_path,
                    tcpdump_log_path=tcpdump_log,
                    pcap_report_path=pcap_report,
                    lan_report_path=lan_report,
                    pcap_report=report,
                )
            copied = Path(archive["wire_pcap"])
            self.assertEqual(copied.read_bytes(), pcap_path.read_bytes())
            self.assertEqual(archive["wire_pcap_sha256"], m11.sha256_file(pcap_path))
            self.assertTrue(archive["copy_verified_by_size_and_sha256"])
            self.assertTrue(Path(archive["sha256sums"]).is_file())

    def test_power_write_service_calls_are_absent(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "power.set_voltage_current_limit(",
            "power.set_protection(",
            "power.set_output(",
        ):
            self.assertNotIn(forbidden, source)

    def test_complete_static_check_is_offline(self):
        result = m11.validate_static(write_evidence=False)
        self.assertTrue(result["pass"])
        self.assertTrue(result["offline_only"])
        self.assertFalse(result["instrument_io"])
        self.assertTrue(result["derived_config"]["power_writes_forbidden"])


if __name__ == "__main__":
    unittest.main()
