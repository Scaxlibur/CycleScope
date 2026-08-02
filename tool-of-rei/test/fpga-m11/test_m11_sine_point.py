from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np


SCRIPT = Path(__file__).with_name("m11_sine_point.py")
SPEC = importlib.util.spec_from_file_location("m11_sine_point", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
point = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(point)


class M11SinePointTests(unittest.TestCase):
    def test_user_restoration_waiver_is_hash_bound_and_narrow(self):
        waiver = point.load_user_restoration_waiver()
        self.assertTrue(waiver["pass"])
        generic = {
            "pass": False,
            "evidence_path": "generic.json",
            "source": {
                "profile": {"status": {"function": "USER", "output": "OFF"}}
            },
            "failures": ["DG CH1 function is not safely restorable: 'USER'"],
        }
        accepted = point.filter_i_user_off_preflight(generic)
        self.assertTrue(accepted["pass"])
        self.assertIsNotNone(accepted["accepted_exception"])
        self.assertFalse(accepted["instrument_writes"])

        unsafe = json.loads(json.dumps(generic))
        unsafe["source"]["profile"]["status"]["output"] = "ON"
        self.assertFalse(point.filter_i_user_off_preflight(unsafe)["pass"])

        unrelated = json.loads(json.dumps(generic))
        unrelated["failures"].append("RTM CH2 high-impedance evidence is missing")
        filtered = point.filter_i_user_off_preflight(unrelated)
        self.assertFalse(filtered["pass"])
        self.assertIn("RTM CH2 high-impedance evidence is missing", filtered["failures"])

    def test_current_physical_gate_accepts_explicit_user_omissions(self):
        gate = point.physical_gate()
        self.assertTrue(gate["pass"], gate["failures"])
        self.assertEqual(
            gate["user_authorized_omissions"],
            [
                "ad8065_feedback_pickoff_before_or_after_series_resistor",
                "internal_supply_rails_and_temperature",
                "probe_channel_swap_correction",
            ],
        )

    def test_unwaived_pending_physical_item_still_fails_closed(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical.json"
            path.write_text(
                json.dumps(
                    {
                        "confirmed": {"topology": True},
                        "not_yet_physically_verified": {"unexpected_item": True},
                        "component_values": {
                            "rf_ohm": 1608.0,
                            "rg_ohm": 200.2,
                            "series_resistor_ohm": 50.0,
                            "feedback_pickoff": "before_series_resistor",
                            "basis": "user_explicit_nominal_confirmation",
                        },
                    }
                ),
                encoding="utf-8",
            )
            gate = point.physical_gate(path)
        self.assertFalse(gate["pass"])
        self.assertTrue(any("unwaived" in item for item in gate["failures"]))

    def test_current_provisional_discovery_gate_is_bounded_and_evidence_backed(self):
        record = point.load_sine_case("c-100k-020mVpp")
        gate = point.provisional_discovery_gate(record)
        self.assertTrue(gate["pass"], gate["failures"])
        self.assertFalse(gate["formal_calibration_eligible"])
        self.assertEqual(len(gate["evidence"]), 2)
        next_point = point.provisional_discovery_gate(
            point.load_sine_case("c-100k-050mVpp")
        )
        self.assertTrue(next_point["pass"], next_point["failures"])
        next_point = point.provisional_discovery_gate(
            point.load_sine_case("c-100k-100mVpp")
        )
        self.assertTrue(next_point["pass"], next_point["failures"])
        blocked_frequency = point.provisional_discovery_gate(
            point.load_sine_case("c-500000Hz-100mVpp")
        )
        self.assertFalse(blocked_frequency["pass"])
        self.assertTrue(
            any("limited to 100 kHz" in failure for failure in blocked_frequency["failures"])
        )

    def test_provisional_live_requires_extra_ack_before_instrument_io(self):
        with patch.object(
            point,
            "physical_gate",
            return_value={"pass": False, "failures": ["test pending gate"]},
        ):
            with self.assertRaisesRegex(
                point.M11PointError, "provisional-discovery-acknowledge"
            ):
                point.run_live(
                    case_id="c-100k-020mVpp",
                    frames=64,
                    acknowledgement=point.LIVE_ACK,
                    stage_acknowledgement=point.C_STAGE_ACK,
                )

    def test_complete_physical_gate_passes(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical.json"
            path.write_text(
                json.dumps(
                    {
                        "confirmed": {"topology": True},
                        "not_yet_physically_verified": {"anything": False},
                        "measured_values": {
                            "rf_ohm": 1608.0,
                            "rg_ohm": 200.2,
                            "series_resistor_ohm": 50.0,
                            "feedback_pickoff": "before_series_resistor",
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(point.physical_gate(path)["pass"])

    def test_user_confirmed_nominal_component_values_are_truthfully_accepted(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical.json"
            path.write_text(
                json.dumps(
                    {
                        "confirmed": {"topology": True},
                        "not_yet_physically_verified": {"anything": False},
                        "component_values": {
                            "rf_ohm": 1608.0,
                            "rg_ohm": 200.2,
                            "series_resistor_ohm": 50.0,
                            "feedback_pickoff": "before_series_resistor",
                            "basis": "user_explicit_nominal_confirmation",
                            "not_claimed_as_meter_measurement": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(point.physical_gate(path)["pass"])

    def test_plan_has_no_source_on_or_power_write(self):
        record = point.load_sine_case("c-100k-020mVpp")
        text = point.plan_text(record)
        self.assertNotIn('state = "on"', text)
        self.assertNotIn('kind = "power.set"', text)
        self.assertNotIn('kind = "power.output"', text)
        self.assertIn('allow_50ohm = false', text)
        self.assertIn('kind = "source.set_func"', text)
        self.assertIn('function = "SIN"', text)

    def test_stage_gate_requires_exact_stage_acknowledgement(self):
        point.validate_sine_stage_case(
            point.load_sine_case("c-100k-020mVpp"), point.C_STAGE_ACK
        )
        point.validate_sine_stage_case(
            point.load_sine_case("d-100000Hz-250mVpp"), point.D_STAGE_ACK
        )
        point.validate_sine_stage_case(
            point.load_sine_case("e-holdout-15000Hz"), point.E_STAGE_ACK
        )
        with self.assertRaisesRegex(point.M11PointError, "stage D requires"):
            point.validate_sine_stage_case(
                point.load_sine_case("d-100000Hz-250mVpp"), point.C_STAGE_ACK
            )
        with self.assertRaisesRegex(point.M11PointError, "must not exceed 0.45 Vpp"):
            point.validate_sine_stage_case(
                point.load_sine_case("d-100000Hz-500mVpp"), point.D_STAGE_ACK
            )

    def test_post_calibration_stages_require_a_validated_nonzero_identity(self):
        record = point.load_sine_case("f-fixed-1e+06Hz")
        with self.assertRaisesRegex(point.M11PointError, "calibration-manifest"):
            point.require_calibration_for_stage(
                record,
                {"calibration_id": 0},
            )
        point.require_calibration_for_stage(
            record,
            {"calibration_id": 25030},
        )

    def test_uncalibrated_identity_remains_explicit_for_historical_points(self):
        identity = point.expected_calibration_identity(None)
        self.assertEqual(identity["calibration_id"], 0)
        self.assertEqual(identity["scale_uv_per_lsb"], 488)
        self.assertEqual(identity["offset_uv"], 0)
        self.assertFalse(identity["calibrated"])

    def test_vertical_scale_covers_expected_loaded_output(self):
        for vpp in (0.02, 0.05, 0.1, 0.5):
            expected = vpp * 4.515984016
            scale = point.choose_vertical_scale(expected)
            self.assertGreaterEqual(scale * 5.0, expected)
            self.assertLessEqual(scale, 0.5)
        conservative = point.choose_vertical_scale(0.02 * 9.031968032)
        self.assertGreaterEqual(conservative * 5.0, 0.02 * 9.031968032)
        self.assertEqual(point.choose_vertical_scale(0.5 * 5.0), 0.5)

    def test_e_scope_windows_are_fft_coherent_on_rtm_ranges(self):
        for frequency_hz in (10_000.0, 10_500.0, 20_000.0):
            window, cycles = point.scope_window(
                {"stage": "E", "frequency_hz": frequency_hz}
            )
            self.assertEqual(window, 0.002)
            self.assertAlmostEqual(cycles, round(cycles))
        for frequency_hz in (
            50_000.0,
            75_000.0,
            350_000.0,
            425_000.0,
            450_000.0,
            475_000.0,
            485_000.0,
            490_000.0,
            500_000.0,
        ):
            window, cycles = point.scope_window(
                {"stage": "E", "frequency_hz": frequency_hz}
            )
            self.assertEqual(window, 0.0002)
            self.assertAlmostEqual(cycles, round(cycles))
        d_window, d_cycles = point.scope_window(
            {"stage": "D", "frequency_hz": 500_000.0}
        )
        self.assertEqual(d_window, 20.0 / 500_000.0)
        self.assertEqual(d_cycles, 20.0)

    def test_f_scope_window_preserves_scope_bandwidth_and_fixed_grid_coherence(self):
        for frequency_hz in (1_000_000.0, 2_250_000.0, 3_000_000.0):
            window, cycles = point.scope_window(
                {"stage": "F", "frequency_hz": frequency_hz}
            )
            self.assertEqual(window, 0.0002)
            self.assertAlmostEqual(cycles, round(cycles))
        worst_window, worst_cycles = point.scope_window(
            {"stage": "F", "frequency_hz": 1_088_500.0}
        )
        self.assertEqual(worst_window, 0.0002)
        self.assertNotAlmostEqual(worst_cycles, round(worst_cycles))

    def test_i_scope_windows_are_exact_rtm_ranges_and_fft_coherent(self):
        for frequency_hz, expected_window in (
            (4_000_000.0, 5e-6),
            (5_000_000.0, 5e-6),
            (7_200_000.0, 5e-6),
            (7_500_000.0, 2e-6),
            (10_000_000.0, 2e-6),
        ):
            window, cycles = point.scope_window(
                {"stage": "I", "frequency_hz": frequency_hz}
            )
            self.assertEqual(window, expected_window)
            self.assertAlmostEqual(cycles, round(cycles))

    def test_i_noncoherent_full_trace_uses_deterministic_wavebench_prefix(self):
        sample_rate = 2_500_000_000.0
        frequency = 7_200_000.0
        samples = 5000
        times = np.arange(samples, dtype=np.float64) / sample_rate
        waveform = np.column_stack(
            (times, 0.1 * np.sin(2.0 * np.pi * frequency * times))
        )
        result = point.wavebench_fft_for_expected_frequency(
            waveform,
            frequency,
            allow_coherent_prefix=True,
        )
        self.assertEqual(
            result["selection"]["mode"],
            "longest_integer-cycle_archived_prefix",
        )
        self.assertEqual(result["selection"]["analyzed_samples"], 3125)
        self.assertEqual(result["selection"]["expected_cycles"], 9)
        self.assertFalse(result["selection"]["raw_samples_modified"])
        self.assertAlmostEqual(
            result["fft"]["peak_frequency_hz"], frequency, delta=1e-3
        )
        self.assertAlmostEqual(result["fft"]["peak_amplitude_v"], 0.1, places=4)

    def test_tone_metrics_and_phase_delta(self):
        sample_rate = 2_000_000.0
        frequency = 100_000.0
        time_axis = np.arange(20_000) / sample_rate
        values = 0.2 * np.sin(2 * np.pi * frequency * time_axis + 0.3)
        metrics = point.tone_metrics(values, sample_rate, frequency)
        self.assertAlmostEqual(metrics["fundamental_peak"], 0.2, places=9)
        self.assertAlmostEqual(metrics["fundamental_vpp"], 0.4, places=9)
        self.assertAlmostEqual(metrics["fundamental_phase_rad"], 0.3, places=9)
        self.assertLess(metrics["fit_residual_rms"], 1e-10)

    def test_capture_calibration_identity_supports_legacy_and_calibrated_reports(self):
        with TemporaryDirectory() as temporary:
            capture = Path(temporary)
            frames = [
                {
                    "calibration_id": 0,
                    "scale_uv_per_lsb": 488,
                    "offset_uv": 0,
                    "frame_flags": 0x0004,
                }
                for _ in range(2)
            ]
            (capture / "capture.json").write_text(
                json.dumps({"frames": frames}), encoding="utf-8"
            )
            legacy = point.capture_calibration_identity(capture, {})
            self.assertEqual(legacy["calibration_id"], 0)
            self.assertFalse(legacy["calibrated"])

            for frame in frames:
                frame.update(
                    calibration_id=17,
                    scale_uv_per_lsb=516,
                    offset_uv=-6708,
                    frame_flags=0x000C,
                )
            (capture / "capture.json").write_text(
                json.dumps({"frames": frames}), encoding="utf-8"
            )
            calibrated = point.capture_calibration_identity(
                capture,
                {
                    "calibration_id": 17,
                    "scale_uv_per_lsb": 516,
                    "offset_uv": -6708,
                    "expected_wave_metadata": {
                        "calibration_id": 17,
                        "scale_uv_per_lsb": 516,
                        "offset_uv": -6708,
                    },
                },
            )
        self.assertEqual(calibrated["calibration_id"], 17)
        self.assertTrue(calibrated["calibrated"])

    def test_capture_calibration_flag_mismatch_is_rejected(self):
        with TemporaryDirectory() as temporary:
            capture = Path(temporary)
            (capture / "capture.json").write_text(
                json.dumps(
                    {
                        "frames": [
                            {
                                "calibration_id": 17,
                                "scale_uv_per_lsb": 516,
                                "offset_uv": -6708,
                                "frame_flags": 0x0004,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(point.M11PointError, "CALIBRATED flag"):
                point.capture_calibration_identity(capture, {})

    def test_wavebench_scope_analysis_is_primary_and_quality_gated(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            sample_rate = 2_000_000.0
            frequency = 100_000.0
            times = np.arange(10_000, dtype=np.float64) / sample_rate
            for channel, peak in ((1, 0.01), (2, 0.05)):
                values = peak * np.sin(2 * np.pi * frequency * times)
                np.save(package / f"ch{channel}.npy", np.column_stack((times, values)))
            summary = {
                "quality_warnings": [],
                "frequency_in_tolerance": True,
                "voltage_vpp_v": 0.1,
            }
            (package / "metadata.json").write_text(
                json.dumps(
                    {
                        "operation": {
                            "channels": [1, 2],
                            "trigger_mode": "single_acquisition",
                        },
                        "channels": {
                            "1": {"summary": summary},
                            "2": {"summary": summary},
                        },
                    }
                ),
                encoding="utf-8",
            )
            analysis = point.wavebench_scope_analysis(package, frequency)
        self.assertTrue(analysis["pass"])
        self.assertIn("wavebench.data.fft.analyze_fft", analysis["primary_method"])
        self.assertAlmostEqual(analysis["ch1"]["fundamental_peak_v"], 0.01, places=5)
        self.assertAlmostEqual(analysis["ch2"]["fundamental_peak_v"], 0.05, places=5)

    def test_wavebench_fft_supersedes_unreliable_metadata_frequency_estimate(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary)
            sample_rate = 2_000_000.0
            frequency = 100_000.0
            times = np.arange(10_000, dtype=np.float64) / sample_rate
            for channel, peak in ((1, 0.01), (2, 0.05)):
                values = peak * np.sin(2 * np.pi * frequency * times)
                np.save(package / f"ch{channel}.npy", np.column_stack((times, values)))
            summary = {
                "quality_warnings": [
                    "frequency_mismatch: estimated frequency differs from expected frequency"
                ],
                "frequency_in_tolerance": False,
                "frequency_estimate_hz": 208_000.0,
                "frequency_method": "hysteresis_rising_crossing",
                "voltage_vpp_v": 0.1,
            }
            (package / "metadata.json").write_text(
                json.dumps(
                    {
                        "operation": {
                            "channels": [1, 2],
                            "trigger_mode": "single_acquisition",
                        },
                        "channels": {
                            "1": {"summary": summary},
                            "2": {"summary": summary},
                        },
                    }
                ),
                encoding="utf-8",
            )
            analysis = point.wavebench_scope_analysis(package, frequency)
        self.assertTrue(analysis["pass"], analysis["failures"])
        self.assertTrue(analysis["ch1"]["metadata_frequency_gate_superseded_by_fft"])
        self.assertEqual(len(analysis["ch1"]["metadata_quality_warnings_advisory"]), 1)

    def test_source_has_no_dp800_write_or_scope_50ohm_bypass(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "set_voltage_current_limit(",
            "power.set_output(",
            "set_protection(",
            "allow_50ohm=True",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
