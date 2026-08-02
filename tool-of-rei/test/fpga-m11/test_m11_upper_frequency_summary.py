from __future__ import annotations

from pathlib import Path
import unittest

import m11_upper_frequency_summary as summary


class M11UpperFrequencySummaryTests(unittest.TestCase):
    def test_matrix_has_five_sines_and_two_combinations(self):
        sine_records, arb_records = summary.i_cases()
        self.assertEqual(len(sine_records), 5)
        self.assertEqual(len(arb_records), 2)
        self.assertEqual(
            {record["frequency_hz"] for record in sine_records},
            {4e6, 5e6, 7.2e6, 7.5e6, 10e6},
        )
        self.assertEqual(
            {record["u_j_frequency_hz"] for record in arb_records},
            {5e6, 10e6},
        )

    def test_every_i_case_is_marked_formal_coverage(self):
        sine_records, arb_records = summary.i_cases()
        self.assertTrue(
            all(record.get("formal_10mhz_coverage") is True for record in sine_records)
        )
        self.assertTrue(
            all(record.get("formal_10mhz_coverage") is True for record in arb_records)
        )

    def test_ten_mhz_sine_and_combinations_require_64_frames(self):
        sine_records, arb_records = summary.i_cases()
        ten_mhz = next(
            record for record in sine_records if record["frequency_hz"] == 10e6
        )
        self.assertEqual(ten_mhz["minimum_frames"], 64)
        self.assertTrue(all(record["minimum_frames"] == 64 for record in arb_records))

    def test_scope_crosscheck_limit_remains_two_percent(self):
        self.assertEqual(summary.SCOPE_CROSSCHECK_LIMIT, 0.02)
        self.assertEqual(summary.FFT_MAX_GRID_OFFSET_BINS, 0.5)

    def test_7p2mhz_failed_live_verdict_has_one_reproducible_offline_correction(self):
        point = (
            summary.safety.EVIDENCE_ROOT
            / "points"
            / "20260801_015524_644597+0800_i-formal-7.2e+06Hz"
        )
        self.assertTrue(Path(point).is_dir())
        validated = summary.validated_sine_analysis(
            point,
            "i-formal-7.2e+06Hz",
        )
        self.assertTrue(validated["pass"])
        self.assertEqual(validated["mode"], "validated_offline_analysis_correction")
        self.assertFalse(validated["point_repeated"])
        self.assertFalse(validated["raw_samples_modified"])


if __name__ == "__main__":
    unittest.main()
