from __future__ import annotations

import unittest

import m11_combination_summary as summary


class M11CombinationSummaryTests(unittest.TestCase):
    def test_matrix_has_exact_fifteen_case_h_grid(self):
        records = summary.h_cases()
        self.assertEqual(len(records), 15)
        self.assertEqual(len({record["case_id"] for record in records}), 15)

    def test_h_grid_covers_three_band_sources_and_five_interferences(self):
        records = summary.h_cases()
        self.assertEqual(len({record["u_b_source_case"] for record in records}), 3)
        self.assertEqual(
            {record["u_j_frequency_hz"] for record in records},
            {1e6, 1.5e6, 2e6, 2.5e6, 3e6},
        )

    def test_every_h_case_has_one_explicit_u_j_component(self):
        for record in summary.h_cases():
            interference = [
                item for item in record["components"] if item.get("role") == "u_J"
            ]
            self.assertEqual(len(interference), 1)
            self.assertEqual(
                interference[0]["frequency_hz"], record["u_j_frequency_hz"]
            )
            self.assertEqual(2.0 * interference[0]["peak_v"], record["u_j_vpp_v"])


if __name__ == "__main__":
    unittest.main()
