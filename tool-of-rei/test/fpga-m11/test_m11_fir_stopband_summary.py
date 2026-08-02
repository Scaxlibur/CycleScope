from __future__ import annotations

import math
import unittest

import m11_fir_stopband_summary as summary


class FirStopbandSummaryTests(unittest.TestCase):
    def test_matrix_has_the_exact_f_stage_shape(self):
        records = summary.f_cases()
        self.assertEqual(len(records), 23)
        self.assertEqual(
            sum(record["case_id"].startswith("f-fixed-") for record in records),
            15,
        )
        self.assertEqual(
            sum(record["case_id"].startswith("f-worst-") for record in records),
            8,
        )

    def test_empirical_p95_is_an_upper_per_frame_residual(self):
        values = [float(index) for index in range(22)]
        result = summary.empirical_p95(values)
        self.assertGreater(result, 19.0)
        self.assertLess(result, 21.0)

    def test_attenuation_lower_bound_uses_conservative_passband_and_residual(self):
        result = summary.attenuation_lower_bound_db(
            ch2_vpp_v=1.0,
            kadc_lower_code_per_v=400.0,
            residual_vpp_upper_code=1.0,
        )
        self.assertTrue(math.isclose(result, 20.0 * math.log10(400.0)))
        self.assertGreater(result, 50.0)

    def test_f_scope_crosscheck_limit_distinguishes_coherent_points(self):
        self.assertEqual(summary.f_scope_crosscheck_limit(0.0), 0.02)
        self.assertEqual(summary.f_scope_crosscheck_limit(0.05), 0.02)
        self.assertEqual(summary.f_scope_crosscheck_limit(0.050001), 0.10)
        self.assertEqual(summary.f_scope_crosscheck_limit(0.5), 0.10)

    def test_f_scope_crosscheck_rejects_peak_beyond_half_bin(self):
        with self.assertRaises(summary.FirStopbandSummaryError):
            summary.f_scope_crosscheck_limit(0.5001)

    def test_reference_kadc_uses_minimum_of_33_nontrivial_anchors(self):
        training = []
        for index in range(33):
            training.append(
                {
                    "case_id": f"case-{index}",
                    "source_vpp_v": 0.05,
                    "frequency_hz": 10_000.0 + index,
                    "adc_fundamental_vpp_code": 400.0 + index,
                    "wavebench": {
                        "channels": {"ch2": {"fundamental_vpp_v": 1.0}}
                    },
                }
            )
        result = summary.reference_kadc({"training_records": training})
        self.assertEqual(result["anchor_count"], 33)
        self.assertEqual(result["minimum_code_per_v"], 400.0)
        self.assertEqual(result["maximum_code_per_v"], 432.0)


if __name__ == "__main__":
    unittest.main()
