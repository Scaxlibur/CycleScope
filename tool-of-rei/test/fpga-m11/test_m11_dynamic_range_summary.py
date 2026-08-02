from __future__ import annotations

import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import warnings

import numpy as np

import m11_dynamic_range_summary as summary


class DynamicRangeSummaryTests(unittest.TestCase):
    def test_case_ids_are_canonical(self):
        self.assertEqual(summary.case_id(10_000, 0.01), "d-10000Hz-010mVpp")
        self.assertEqual(summary.case_id(500_000, 0.45), "d-500000Hz-450mVpp")

    def test_robust_sine_outliers_report_without_replacing(self):
        sample_rate_hz = 4_062_500.0
        frequency_hz = 100_000.0
        time_s = np.arange(8192, dtype=np.float64) / sample_rate_hz
        values = 400.0 * np.sin(2.0 * math.pi * frequency_hz * time_s)
        values[1234] += 20.0
        result = summary.robust_sine_outliers(
            values, sample_rate_hz, frequency_hz
        )
        self.assertEqual(result["outlier_count"], 1)
        self.assertEqual(result["samples"], 8192)
        self.assertGreaterEqual(result["threshold_code"], 8.0)
        self.assertAlmostEqual(values[1234], 400.0 * math.sin(
            2.0 * math.pi * frequency_hz * time_s[1234]
        ) + 20.0)

    def test_frequency_summary_distinguishes_engineering_and_hard_limits(self):
        records = []
        for amplitude, adc_vpp, gamp in (
            (0.01, 19.4, 4.62),
            (0.05, 97.0, 4.62),
            (0.10, 194.0, 4.62),
            (0.25, 485.0, 4.62),
            (0.45, 873.0, 4.59228),
        ):
            records.append(
                {
                    "source_vpp_v": amplitude,
                    "ratios": {"gamp_v_per_v": gamp},
                    "ch1": {"thd_ratio": 0.003, "raw_vpp_v": 0.52},
                    "ch2": {"thd_ratio": 0.0025, "raw_vpp_v": 2.18},
                    "adc": {
                        "fundamental_vpp_code": adc_vpp,
                        "thd_ratio": 0.0005,
                        "sfdr_db": {"median": 70.0},
                        "outliers": {"count": 0},
                        "minimum_code": -430.0,
                        "maximum_code": 450.0,
                    },
                }
            )
        result = summary.frequency_summary(records)
        self.assertFalse(
            result["compression"]["engineering_target_0_5_percent_pass"]
        )
        self.assertTrue(result["compression"]["hard_limit_1_percent_pass"])
        self.assertTrue(result["large_signal"]["thd_gate_pass"])

    def test_adc_supplemental_analysis_promotes_int16_before_square(self):
        sample_rate_hz = 4_062_500.0
        frequency_hz = 100_000.0
        time_s = np.arange(8192, dtype=np.float64) / sample_rate_hz
        values = np.rint(
            400.0 * np.sin(2.0 * math.pi * frequency_hz * time_s)
        ).astype("<i2")
        with TemporaryDirectory() as temporary:
            capture = Path(temporary)
            for index in range(22):
                values.tofile(capture / f"frame_{index:05d}.s16le")
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                result = summary._supplemental_adc_metrics(
                    capture, frequency_hz
                )
        self.assertEqual(result["frame_count"], 22)
        self.assertGreater(result["sfdr_db"]["median"], 40.0)


if __name__ == "__main__":
    unittest.main()
