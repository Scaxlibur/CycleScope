from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "cslp_adc_analyze.py"
SPEC = importlib.util.spec_from_file_location("cslp_adc_analyze", MODULE_PATH)
analyze = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = analyze
SPEC.loader.exec_module(analyze)


@unittest.skipIf(analyze.np is None, "WaveBench NumPy environment is not active")
class AdcAnalysisTests(unittest.TestCase):
    def test_basic_metrics_promotes_int16_before_square(self):
        values = analyze.np.asarray((-30_000, 30_000), dtype="<i2")
        metrics = analyze.basic_metrics(values)
        self.assertEqual(metrics["rms_total"], 30_000.0)
        self.assertEqual(metrics["rms_ac"], 30_000.0)

    def write_capture(self, root, frames):
        capture = root / "adc"
        capture.mkdir()
        records = []
        for index, values in enumerate(frames):
            raw = analyze.np.asarray(values, dtype="<i2").tobytes()
            name = f"frame_{index:05d}_{index + 1:08x}.s16le"
            (capture / name).write_bytes(raw)
            records.append(
                {
                    "frame_index": index,
                    "frame_id": index + 1,
                    "timestamp_us": 1_000_000 + index * 50_000,
                    "file": name,
                    "frame_bytes": len(raw),
                    "sample_count": analyze.FRAME_SAMPLES,
                    "sample_rate_hz": analyze.SAMPLE_RATE_HZ,
                    "scale_uv_per_lsb": 488,
                    "offset_uv": 0,
                    "config_id": 7,
                    "filter_profile": 1,
                    "calibration_id": 0,
                    "frame_flags": 4,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        manifest = {
            "format": "CycleScope CSLP independent complete frames v1",
            "sample_encoding": "S16_LE",
            "frame_count": len(records),
            "frame_samples": analyze.FRAME_SAMPLES,
            "sample_rate_hz": analyze.SAMPLE_RATE_HZ,
            "source_mode": "real-adc",
            "activity_policy": "require",
            "overrange_policy": "reject",
            "session_id": 9,
            "device_boot_id": 10,
            "config_id": 7,
            "frames": records,
            "partial": False,
        }
        (capture / "capture.json").write_text(json.dumps(manifest), encoding="utf-8")
        return capture

    def write_lan_report(self, root, capture, frame_count):
        path = root / "lan.json"
        path.write_text(
            json.dumps(
                {
                    "pass": True,
                    "source_mode": "real-adc",
                    "activity_policy": "require",
                    "overrange_policy": "reject",
                    "session_id": 9,
                    "device_boot_id": 10,
                    "config_id": 7,
                    "capture": {
                        "directory": str(capture.resolve()),
                        "frame_count": frame_count,
                        "partial": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def tone_report_payload(
        self,
        frequency_hz,
        scope_vpp,
        adc_vpp_median,
        *,
        adc_vpp_p95=None,
        response_only=False,
    ):
        folded_hz = analyze.usable_folded_frequency_hz(frequency_hz, "synthetic tone")
        if adc_vpp_p95 is None:
            adc_vpp_p95 = adc_vpp_median
        return {
            "analysis_type": "tone",
            "pass": True,
            "frame_count": analyze.MIN_CALIBRATION_FRAMES,
            "gates": {
                "min_calibration_frames": analyze.MIN_CALIBRATION_FRAMES,
            },
            "response_only": response_only,
            "expected_frequency_hz": frequency_hz,
            "expected_input_frequency_hz": frequency_hz,
            "expected_folded_frequency_hz": folded_hz,
            "scope_input_frequency_hz": frequency_hz,
            "scope_folded_frequency_hz": folded_hz,
            "adc_folded_frequency_hz": folded_hz,
            "scope": {"fundamental_vpp": scope_vpp},
            "adc_codes": {
                "fundamental_vpp": {
                    "median": adc_vpp_median,
                    "p95": adc_vpp_p95,
                }
            },
        }

    def test_noncoherent_tone_recovers_frequency_scale_thd_and_sfdr(self):
        np = analyze.np
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            frequency = 100_123.4
            time_adc = np.arange(analyze.FRAME_SAMPLES) / analyze.SAMPLE_RATE_HZ
            frames = []
            phases = (0.1, 0.7, 1.4)
            for frame_index in range(analyze.MIN_CALIBRATION_FRAMES):
                phase = phases[frame_index % len(phases)]
                values = (
                    30.0
                    + 400.0 * np.sin(2 * np.pi * frequency * time_adc + phase)
                    + 4.0 * np.sin(4 * np.pi * frequency * time_adc + 0.3)
                    + 2.0 * np.sin(6 * np.pi * frequency * time_adc + 0.8)
                )
                frames.append(np.rint(values))
            capture = self.write_capture(root, frames)
            lan_report = root / "lan.json"
            lan_report.write_text(
                json.dumps(
                    {
                        "pass": True,
                        "source_mode": "real-adc",
                        "activity_policy": "require",
                        "overrange_policy": "reject",
                        "session_id": 9,
                        "device_boot_id": 10,
                        "config_id": 7,
                        "capture": {
                            "directory": str(capture.resolve()),
                            "frame_count": len(frames),
                            "partial": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            scope_rate = 2_000_000.0
            time_scope = np.arange(analyze.FRAME_SAMPLES) / scope_rate
            scope_values = 0.02 + 0.1 * np.sin(2 * np.pi * frequency * time_scope + 0.4)
            scope_npy = root / "scope.npy"
            np.save(scope_npy, np.column_stack((time_scope, scope_values)))

            report = analyze.analyze_tone(
                SimpleNamespace(
                    capture=capture,
                    lan_report=lan_report,
                    scope_npy=scope_npy,
                    expected_frequency_hz=frequency,
                    max_frequency_error_hz=20.0,
                    response_only=False,
                    scale_uv_per_lsb=None,
                    max_amplitude_error_v=0.005,
                )
            )

            self.assertTrue(report["pass"], report["failures"])
            self.assertAlmostEqual(
                report["adc_codes"]["frequency_hz"]["median"],
                frequency,
                delta=10.0,
            )
            self.assertAlmostEqual(
                report["candidate_scale_uv_per_lsb"],
                250.0,
                delta=1.0,
            )
            self.assertAlmostEqual(
                report["adc_codes"]["thd_ratio"]["median"],
                (4.0**2 + 2.0**2) ** 0.5 / 400.0,
                delta=0.002,
            )
            self.assertGreater(report["adc_codes"]["sfdr_db"]["median"], 35.0)
            self.assertEqual(report["gates"]["max_frequency_error_hz"], 20.0)
            self.assertEqual(report["gates"]["max_amplitude_error_v"], 0.005)
            self.assertEqual(
                report["input_binding"]["scope_npy"]["sha256"],
                hashlib.sha256(scope_npy.read_bytes()).hexdigest(),
            )
            self.assertEqual(report["input_binding"]["identity"]["device_boot_id"], 10)

    def test_raw_frequency_fold_maps_first_nyquist_zone_strictly(self):
        expected = {
            1_000_000.0: 1_000_000.0,
            2_000_000.0: 2_000_000.0,
            3_316_500.0: 746_000.0,
            5_000_000.0: 937_500.0,
            10_000_000.0: 1_875_000.0,
            20_000_000.0: 312_500.0,
            32_000_000.0: 500_000.0,
        }
        for input_frequency_hz, folded_frequency_hz in expected.items():
            with self.subTest(input_frequency_hz=input_frequency_hz):
                self.assertEqual(
                    analyze.fold_raw_frequency_hz(input_frequency_hz),
                    folded_frequency_hz,
                )
        for invalid in (-1.0, 32_500_000.0, 33_000_000.0, float("nan")):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(analyze.AnalysisError),
            ):
                analyze.fold_raw_frequency_hz(invalid)

    def test_high_tone_cli_requires_response_only_and_rejects_degenerate_folds(self):
        base = [
            "tone",
            "--capture",
            "capture",
            "--lan-report",
            "lan",
            "--scope-npy",
            "scope",
            "--report",
            "out",
            "--expected-frequency-hz",
        ]
        parsed = analyze.parse_args([*base, "5000000", "--response-only"])
        self.assertTrue(parsed.response_only)
        for frequency_hz, response_only in (
            (5_000_000.0, False),
            (33_000_000.0, True),
            (analyze.SAMPLE_RATE_HZ, True),
            (analyze.SAMPLE_RATE_HZ / 2, True),
        ):
            arguments = [*base, str(frequency_hz)]
            if response_only:
                arguments.append("--response-only")
            with self.subTest(frequency_hz=frequency_hz):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    analyze.parse_args(arguments)

    def test_response_only_high_inputs_fit_adc_at_scope_folded_frequency(self):
        np = analyze.np
        for input_frequency_hz in (5e6, 10e6, 20e6, 32e6):
            with self.subTest(input_frequency_hz=input_frequency_hz):
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    folded_hz = analyze.fold_raw_frequency_hz(input_frequency_hz)
                    adc_time = np.arange(analyze.FRAME_SAMPLES) / analyze.SAMPLE_RATE_HZ
                    adc_values = np.rint(
                        400.0 * np.sin(2 * np.pi * folded_hz * adc_time + 0.2)
                    )
                    frames = [adc_values] * analyze.MIN_CALIBRATION_FRAMES
                    capture = self.write_capture(root, frames)
                    lan_report = self.write_lan_report(root, capture, len(frames))

                    scope_rate_hz = 100_000_000.0
                    scope_time = np.arange(analyze.FRAME_SAMPLES) / scope_rate_hz
                    scope_values = 0.1 * np.sin(
                        2 * np.pi * input_frequency_hz * scope_time + 0.4
                    )
                    scope_npy = root / "scope.npy"
                    np.save(scope_npy, np.column_stack((scope_time, scope_values)))

                    report = analyze.analyze_tone(
                        SimpleNamespace(
                            capture=capture,
                            lan_report=lan_report,
                            scope_npy=scope_npy,
                            expected_frequency_hz=input_frequency_hz,
                            max_frequency_error_hz=2_000.0,
                            response_only=True,
                            scale_uv_per_lsb=None,
                            max_amplitude_error_v=0.005,
                        )
                    )

                    self.assertTrue(report["pass"], report["failures"])
                    self.assertEqual(
                        report["expected_input_frequency_hz"], input_frequency_hz
                    )
                    self.assertEqual(report["expected_folded_frequency_hz"], folded_hz)
                    self.assertAlmostEqual(
                        report["scope_input_frequency_hz"],
                        input_frequency_hz,
                        delta=2_000.0,
                    )
                    self.assertAlmostEqual(
                        report["adc_folded_frequency_hz"],
                        report["scope_folded_frequency_hz"],
                        delta=analyze.FREQUENCY_BINDING_ABS_TOLERANCE_HZ,
                    )

    def test_response_only_tone_accepts_fully_suppressed_adc(self):
        np = analyze.np
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_frequency_hz = 5_000_000.0
            frames = [np.zeros(analyze.FRAME_SAMPLES, dtype=np.float64)] * (
                analyze.MIN_CALIBRATION_FRAMES
            )
            capture = self.write_capture(root, frames)
            lan_report = self.write_lan_report(root, capture, len(frames))
            scope_rate_hz = 100_000_000.0
            scope_time = np.arange(analyze.FRAME_SAMPLES) / scope_rate_hz
            scope_values = 0.1 * np.sin(2 * np.pi * input_frequency_hz * scope_time)
            scope_npy = root / "scope.npy"
            np.save(scope_npy, np.column_stack((scope_time, scope_values)))

            report = analyze.analyze_tone(
                SimpleNamespace(
                    capture=capture,
                    lan_report=lan_report,
                    scope_npy=scope_npy,
                    expected_frequency_hz=input_frequency_hz,
                    max_frequency_error_hz=2_000.0,
                    response_only=True,
                    scale_uv_per_lsb=None,
                    max_amplitude_error_v=0.005,
                )
            )

            self.assertTrue(report["pass"], report["failures"])
            self.assertEqual(report["adc_codes"]["fundamental_vpp"]["median"], 0.0)
            self.assertIsNone(report["candidate_scale_uv_per_lsb"])

    def test_zero_report_records_disabled_and_active_gates_and_input_binding(self):
        np = analyze.np
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = np.tile(
                np.asarray((4, 5), dtype=np.float64), analyze.FRAME_SAMPLES // 2
            )
            frames = [values] * analyze.MIN_CALIBRATION_FRAMES
            capture = self.write_capture(root, frames)
            lan_report = self.write_lan_report(root, capture, len(frames))
            report = analyze.analyze_zero(
                SimpleNamespace(
                    capture=capture,
                    lan_report=lan_report,
                    scope_npy=None,
                    scale_uv_per_lsb=488.0,
                    max_abs_mean_code=10.0,
                    max_rms_code=None,
                )
            )

            self.assertTrue(report["pass"], report["failures"])
            self.assertEqual(
                report["gates"],
                {
                    "min_calibration_frames": analyze.MIN_CALIBRATION_FRAMES,
                    "max_abs_mean_code": 10.0,
                    "max_rms_code": None,
                    "max_scope_interval_relative_deviation": (
                        analyze.MAX_SCOPE_INTERVAL_RELATIVE_DEVIATION
                    ),
                },
            )
            self.assertEqual(report["analysis_parameters"]["scale_uv_per_lsb"], 488.0)
            self.assertEqual(
                report["input_binding"]["capture"]["verified_frame_sha256_count"],
                analyze.MIN_CALIBRATION_FRAMES,
            )
            self.assertIsNone(report["input_binding"]["scope_npy"])

    def test_zero_tone_and_square_reject_twenty_calibration_frames(self):
        np = analyze.np
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [np.zeros(analyze.FRAME_SAMPLES)] * (
                analyze.MIN_CALIBRATION_FRAMES - 1
            )
            capture = self.write_capture(root, frames)
            lan_report = self.write_lan_report(root, capture, len(frames))
            common = {
                "capture": capture,
                "lan_report": lan_report,
                "scope_npy": root / "unused.npy",
            }
            cases = (
                (
                    analyze.analyze_zero,
                    SimpleNamespace(
                        **common,
                        scale_uv_per_lsb=None,
                        max_abs_mean_code=None,
                        max_rms_code=None,
                    ),
                ),
                (
                    analyze.analyze_tone,
                    SimpleNamespace(
                        **common,
                        expected_frequency_hz=100_000.0,
                        response_only=False,
                        max_frequency_error_hz=1_000.0,
                        scale_uv_per_lsb=None,
                        max_amplitude_error_v=0.005,
                    ),
                ),
                (
                    analyze.analyze_square,
                    SimpleNamespace(
                        **common,
                        expected_frequency_hz=10_000.0,
                        expected_polarity="normal",
                        polarity_margin=0.05,
                        max_frequency_error_hz=1_000.0,
                        max_duty_error=0.05,
                        min_level_span_codes=10.0,
                    ),
                ),
            )
            for analysis_function, arguments in cases:
                with self.subTest(analysis=analysis_function.__name__):
                    with self.assertRaisesRegex(
                        analyze.AnalysisError, "at least 21 complete frames"
                    ):
                        analysis_function(arguments)

    def test_square_level_duty_distinguishes_inversion(self):
        np = analyze.np
        normal = np.concatenate((np.ones(300), -np.ones(700)))
        inverted = -normal
        normal_duty, _low, _high = analyze.level_duty(normal)
        inverted_duty, _low, _high = analyze.level_duty(inverted)
        self.assertAlmostEqual(normal_duty, 0.3)
        self.assertAlmostEqual(inverted_duty, 0.7)

    def test_square_analysis_rejects_false_normal_duty(self):
        np = analyze.np

        def run(adc_duty):
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                frequency = 10_000.0
                adc_time = np.arange(analyze.FRAME_SAMPLES) / analyze.SAMPLE_RATE_HZ
                adc_values = np.where(
                    (adc_time * frequency) % 1.0 < adc_duty, 100.0, -100.0
                )
                frames = [
                    np.roll(adc_values, frame_index * 37)
                    for frame_index in range(analyze.MIN_CALIBRATION_FRAMES)
                ]
                capture = self.write_capture(root, frames)
                lan_report = root / "lan.json"
                lan_report.write_text(
                    json.dumps(
                        {
                            "pass": True,
                            "source_mode": "real-adc",
                            "activity_policy": "require",
                            "overrange_policy": "reject",
                            "session_id": 9,
                            "device_boot_id": 10,
                            "config_id": 7,
                            "capture": {
                                "directory": str(capture.resolve()),
                                "frame_count": len(frames),
                                "partial": False,
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                scope_rate = 1_000_000.0
                scope_time = np.arange(analyze.FRAME_SAMPLES) / scope_rate
                scope_values = np.where((scope_time * frequency) % 1.0 < 0.3, 0.1, -0.1)
                scope_npy = root / "scope.npy"
                np.save(scope_npy, np.column_stack((scope_time, scope_values)))
                return analyze.analyze_square(
                    SimpleNamespace(
                        capture=capture,
                        lan_report=lan_report,
                        scope_npy=scope_npy,
                        expected_frequency_hz=frequency,
                        expected_polarity="normal",
                        polarity_margin=0.05,
                        max_frequency_error_hz=100.0,
                        max_duty_error=0.05,
                        min_level_span_codes=10.0,
                    )
                )

        normal = run(0.3)
        self.assertTrue(normal["pass"], normal["failures"])
        self.assertEqual(normal["gates"]["expected_polarity"], "normal")
        self.assertEqual(normal["input_binding"]["identity"]["config_id"], 7)
        false_normal = run(0.01)
        self.assertFalse(false_normal["pass"])
        self.assertTrue(any("duty" in item for item in false_normal["failures"]))

    def test_sweep_gates_passband_calibration_and_stopband(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = []
            definitions = (
                (10_000.0, 0.1, 200.0, 200.0, "passband", False),
                (100_000.0, 0.2, 400.0, 400.0, "passband", False),
                (500_000.0, 0.25, 500.0, 500.0, "passband", False),
                (32_000_000.0, 0.2, 0.2, 0.4, "stopband", True),
            )
            for index, definition in enumerate(definitions):
                (
                    frequency,
                    scope_vpp,
                    adc_vpp_median,
                    adc_vpp_p95,
                    point_class,
                    response_only,
                ) = definition
                report_path = root / f"point-{index}.json"
                report_path.write_text(
                    json.dumps(
                        self.tone_report_payload(
                            frequency,
                            scope_vpp,
                            adc_vpp_median,
                            adc_vpp_p95=adc_vpp_p95,
                            response_only=response_only,
                        )
                    ),
                    encoding="utf-8",
                )
                entries.append({"analysis": report_path.name, "class": point_class})
            points_path = root / "points.json"
            points_path.write_text(json.dumps({"points": entries}), encoding="utf-8")

            report = analyze.analyze_sweep(
                SimpleNamespace(
                    points=points_path,
                    reference_frequency_hz=100_000.0,
                    max_amplitude_error_v=0.005,
                    max_passband_ripple_db=0.1,
                    min_stopband_attenuation_db=50.0,
                )
            )

            self.assertTrue(report["pass"], report["failures"])
            self.assertAlmostEqual(report["candidate_scale_uv_per_lsb"], 500.0)
            self.assertLessEqual(report["passband_ripple_db"], 1e-12)
            self.assertLessEqual(report["worst_stopband_response_db"], -59.9)
            self.assertEqual(
                report["input_binding"]["points_json"]["sha256"],
                hashlib.sha256(points_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(len(report["input_binding"]["tone_reports"]), 4)
            stopband = report["points"][-1]
            self.assertEqual(stopband["amplitude_statistic"], "p95")
            self.assertEqual(stopband["measured_adc_vpp_codes"], 0.4)
            self.assertEqual(stopband["adc_vpp_floor_codes"], 1e-12)
            self.assertEqual(stopband["gain_upper_bound"], 2.0)
            self.assertLessEqual(stopband["response_upper_bound_db"], -59.9)

    def test_sweep_stopband_uses_p95_upper_bound_instead_of_median(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            definitions = (
                (10_000.0, 0.1, 200.0, 200.0, "passband", False),
                (100_000.0, 0.2, 400.0, 400.0, "passband", False),
                (1_000_000.0, 0.2, 0.4, 2.0, "stopband", False),
            )
            entries = []
            for index, definition in enumerate(definitions):
                frequency, scope_vpp, median, p95, point_class, response_only = (
                    definition
                )
                tone = root / f"tone-{index}.json"
                tone.write_text(
                    json.dumps(
                        self.tone_report_payload(
                            frequency,
                            scope_vpp,
                            median,
                            adc_vpp_p95=p95,
                            response_only=response_only,
                        )
                    ),
                    encoding="utf-8",
                )
                entries.append({"analysis": tone.name, "class": point_class})
            points = root / "points.json"
            points.write_text(json.dumps({"points": entries}), encoding="utf-8")

            report = analyze.analyze_sweep(
                SimpleNamespace(
                    points=points,
                    reference_frequency_hz=100_000.0,
                    max_amplitude_error_v=0.005,
                    max_passband_ripple_db=0.1,
                    min_stopband_attenuation_db=50.0,
                )
            )

            self.assertFalse(report["pass"])
            stopband = report["points"][-1]
            self.assertEqual(stopband["amplitude_statistic"], "p95")
            self.assertEqual(stopband["adc_vpp_codes"], 2.0)
            self.assertGreater(stopband["response_upper_bound_db"], -50.0)
            self.assertEqual(
                report["worst_stopband_response_upper_bound_db"],
                stopband["response_upper_bound_db"],
            )
            self.assertTrue(any("stopband" in item for item in report["failures"]))

    def test_sweep_zero_stopband_uses_explicit_numeric_floor(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            definitions = (
                (10_000.0, 0.1, 200.0, "passband"),
                (100_000.0, 0.2, 400.0, "passband"),
                (1_000_000.0, 0.2, 0.0, "stopband"),
            )
            entries = []
            for index, (frequency, scope_vpp, adc_vpp, point_class) in enumerate(
                definitions
            ):
                tone = root / f"tone-{index}.json"
                tone.write_text(
                    json.dumps(self.tone_report_payload(frequency, scope_vpp, adc_vpp)),
                    encoding="utf-8",
                )
                entries.append({"analysis": tone.name, "class": point_class})
            points = root / "points.json"
            points.write_text(json.dumps({"points": entries}), encoding="utf-8")

            report = analyze.analyze_sweep(
                SimpleNamespace(
                    points=points,
                    reference_frequency_hz=100_000.0,
                    max_amplitude_error_v=0.005,
                    max_passband_ripple_db=0.1,
                    min_stopband_attenuation_db=50.0,
                )
            )

            self.assertTrue(report["pass"], report["failures"])
            stopband = report["points"][-1]
            self.assertEqual(stopband["measured_adc_vpp_codes"], 0.0)
            self.assertEqual(stopband["adc_vpp_codes"], 1e-12)
            self.assertEqual(stopband["gain_upper_bound"], 5e-12)

    def test_sweep_rejects_out_of_profile_classes_and_unbound_high_stopband(self):
        cases = (
            (600_000.0, "passband", False, "passband point exceeds"),
            (750_000.0, "stopband", False, "stopband point is below"),
            (5_000_000.0, "stopband", False, "response-only is required"),
        )
        for frequency_hz, point_class, response_only, message in cases:
            with self.subTest(frequency_hz=frequency_hz, point_class=point_class):
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    tone = root / "tone.json"
                    tone.write_text(
                        json.dumps(
                            self.tone_report_payload(
                                frequency_hz,
                                0.2,
                                0.4,
                                response_only=response_only,
                            )
                        ),
                        encoding="utf-8",
                    )
                    points = root / "points.json"
                    points.write_text(
                        json.dumps(
                            {"points": [{"analysis": tone.name, "class": point_class}]}
                        ),
                        encoding="utf-8",
                    )
                    args = SimpleNamespace(
                        points=points,
                        reference_frequency_hz=100_000.0,
                        max_amplitude_error_v=0.005,
                        max_passband_ripple_db=0.1,
                        min_stopband_attenuation_db=50.0,
                    )
                    with self.assertRaisesRegex(analyze.AnalysisError, message):
                        analyze.analyze_sweep(args)

    def test_sweep_requires_multiple_passband_and_one_stopband_point(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            point = root / "point.json"
            point.write_text(
                json.dumps(self.tone_report_payload(100_000.0, 0.2, 400.0)),
                encoding="utf-8",
            )
            points = root / "points.json"
            args = SimpleNamespace(
                points=points,
                reference_frequency_hz=100_000.0,
                max_amplitude_error_v=0.005,
                max_passband_ripple_db=0.1,
                min_stopband_attenuation_db=50.0,
            )
            one_passband = {"points": [{"analysis": point.name, "class": "passband"}]}
            points.write_text(json.dumps(one_passband), encoding="utf-8")
            with self.assertRaisesRegex(analyze.AnalysisError, "two passing passband"):
                analyze.analyze_sweep(args)

            two_passband = {
                "points": [
                    {"analysis": point.name, "class": "passband"},
                    {"analysis": point.name, "class": "passband"},
                ]
            }
            points.write_text(json.dumps(two_passband), encoding="utf-8")
            with self.assertRaisesRegex(analyze.AnalysisError, "one passing stopband"):
                analyze.analyze_sweep(args)

    def test_report_destination_rejects_existing_and_every_input_kind(self):
        np = analyze.np
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = self.write_capture(root, [np.zeros(analyze.FRAME_SAMPLES)])
            lan_report = self.write_lan_report(root, capture, 1)
            scope_npy = root / "scope.npy"
            scope_time = np.arange(64, dtype=np.float64) / 1_000_000.0
            np.save(scope_npy, np.column_stack((scope_time, np.zeros(64))))
            frame_path = next(capture.glob("*.s16le"))
            capture_args = SimpleNamespace(
                command="tone",
                capture=capture,
                lan_report=lan_report,
                scope_npy=scope_npy,
                report=root / "unused.json",
            )
            for input_path in (
                lan_report,
                capture,
                capture / "capture.json",
                frame_path,
                scope_npy,
            ):
                with self.subTest(input_path=input_path):
                    capture_args.report = input_path
                    with self.assertRaises(analyze.UnsafeReportPath):
                        analyze.validate_report_destination(capture_args)

            tone_report = root / "tone.json"
            tone_report.write_text("{}", encoding="utf-8")
            points = root / "points.json"
            points.write_text(
                json.dumps({"points": [{"analysis": tone_report.name}]}),
                encoding="utf-8",
            )
            sweep_args = SimpleNamespace(
                command="sweep", points=points, report=root / "unused.json"
            )
            for input_path in (points, tone_report):
                with self.subTest(input_path=input_path):
                    sweep_args.report = input_path
                    with self.assertRaises(analyze.UnsafeReportPath):
                        analyze.validate_report_destination(sweep_args)

            existing = root / "existing.json"
            existing.write_text("keep-me", encoding="utf-8")
            capture_args.report = existing
            with self.assertRaisesRegex(analyze.UnsafeReportPath, "already exists"):
                analyze.validate_report_destination(capture_args)

            with redirect_stderr(StringIO()), redirect_stdout(StringIO()):
                return_code = analyze.main(
                    [
                        "zero",
                        "--capture",
                        str(capture),
                        "--lan-report",
                        str(lan_report),
                        "--report",
                        str(existing),
                    ]
                )
            self.assertEqual(return_code, 1)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep-me")

    def test_report_destination_rejects_missing_declared_frame_alias(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "adc"
            capture.mkdir()
            report = capture / "future-frame.s16le"
            (capture / "capture.json").write_text(
                json.dumps({"frames": [{"file": report.name}]}), encoding="utf-8"
            )
            args = SimpleNamespace(
                command="zero",
                capture=capture,
                lan_report=root / "lan.json",
                scope_npy=None,
                report=report,
            )
            with self.assertRaisesRegex(analyze.UnsafeReportPath, "input evidence"):
                analyze.validate_report_destination(args)
            self.assertFalse(report.exists())

    def test_cli_rejects_nan_and_infinity_for_every_float_argument(self):
        common = ["--capture", "capture", "--lan-report", "lan", "--report", "out"]
        tone = ["tone", *common, "--scope-npy", "scope", "--expected-frequency-hz", "1"]
        square = [
            "square",
            *common,
            "--scope-npy",
            "scope",
            "--expected-frequency-hz",
            "1",
        ]
        cases = [
            (["zero", *common], "--scale-uv-per-lsb"),
            (["zero", *common], "--max-abs-mean-code"),
            (["zero", *common], "--max-rms-code"),
            (tone, "--expected-frequency-hz"),
            (tone, "--scale-uv-per-lsb"),
            (tone, "--max-frequency-error-hz"),
            (tone, "--max-amplitude-error-v"),
            (square, "--expected-frequency-hz"),
            (square, "--polarity-margin"),
            (square, "--max-frequency-error-hz"),
            (square, "--max-duty-error"),
            (square, "--min-level-span-codes"),
            (
                ["sweep", "--points", "points", "--report", "out"],
                "--reference-frequency-hz",
            ),
            (
                ["sweep", "--points", "points", "--report", "out"],
                "--max-amplitude-error-v",
            ),
            (
                ["sweep", "--points", "points", "--report", "out"],
                "--max-passband-ripple-db",
            ),
            (
                ["sweep", "--points", "points", "--report", "out"],
                "--min-stopband-attenuation-db",
            ),
        ]
        for arguments, option in cases:
            for invalid in ("nan", "inf", "-inf"):
                with self.subTest(option=option, invalid=invalid):
                    with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                        analyze.parse_args([*arguments, option, invalid])

    def test_scope_time_axis_must_be_strictly_monotonic_and_uniform(self):
        np = analyze.np
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = np.zeros(64, dtype=np.float64)
            uniform = np.arange(64, dtype=np.float64) / 1_000_000.0

            duplicate = uniform.copy()
            duplicate[32] = duplicate[31]
            duplicate_path = root / "duplicate.npy"
            np.save(duplicate_path, np.column_stack((duplicate, values)))
            with self.assertRaisesRegex(analyze.AnalysisError, "time order"):
                analyze.load_scope_trace(duplicate_path)

            nonuniform = uniform.copy()
            nonuniform[32:] += 0.01 / 1_000_000.0
            nonuniform_path = root / "nonuniform.npy"
            np.save(nonuniform_path, np.column_stack((nonuniform, values)))
            with self.assertRaisesRegex(analyze.AnalysisError, "not uniformly"):
                analyze.load_scope_trace(nonuniform_path)

    def test_sweep_rejects_nonfinite_tone_metrics(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            tone = root / "tone.json"
            payload = self.tone_report_payload(100_000.0, 0.2, 400.0)
            payload["scope"]["fundamental_vpp"] = float("nan")
            tone.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            points = root / "points.json"
            points.write_text(
                json.dumps(
                    {
                        "points": [
                            {"analysis": tone.name, "class": "passband"},
                            {"analysis": tone.name, "class": "passband"},
                            {"analysis": tone.name, "class": "stopband"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                points=points,
                reference_frequency_hz=100_000.0,
                max_amplitude_error_v=0.005,
                max_passband_ripple_db=0.1,
                min_stopband_attenuation_db=50.0,
            )
            with self.assertRaisesRegex(analyze.AnalysisError, "finite and positive"):
                analyze.analyze_sweep(args)


if __name__ == "__main__":
    unittest.main()
