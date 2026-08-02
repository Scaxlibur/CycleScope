from __future__ import annotations

import unittest

import m11_multitone_summary as summary


class M11MultitoneSummaryTests(unittest.TestCase):
    def test_matrix_has_exactly_ten_g_cases(self):
        records = summary.g_cases()
        self.assertEqual(len(records), 10)
        self.assertEqual(len({record["case_id"] for record in records}), 10)

    def test_g_cases_cover_low_edge_and_weak_line_variants(self):
        case_ids = {record["case_id"] for record in summary.g_cases()}
        self.assertIn("g-a-low-low-crest", case_ids)
        self.assertIn("g-a-edge-high-crest", case_ids)
        self.assertIn("g-b-low-high-crest", case_ids)
        self.assertIn("g-b-edge-low-crest", case_ids)
        self.assertIn("g-weak-line-high-crest", case_ids)

    def test_maximum_line_error_does_not_depend_on_line_order(self):
        analysis = {
            "adc_recovery": {
                "line_results": [
                    {"absolute_error_v": 0.0002},
                    {"absolute_error_v": 0.0011},
                    {"absolute_error_v": 0.0004},
                ]
            }
        }
        self.assertEqual(summary._maximum_line_error(analysis), 0.0011)

    def test_maximum_line_error_rejects_missing_lines(self):
        with self.assertRaises(summary.MultitoneSummaryError):
            summary._maximum_line_error(
                {"adc_recovery": {"line_results": []}}
            )


if __name__ == "__main__":
    unittest.main()
