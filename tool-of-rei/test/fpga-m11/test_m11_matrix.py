from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np


SCRIPT = Path(__file__).with_name("m11_matrix.py")
SPEC = importlib.util.spec_from_file_location("m11_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


class M11MatrixTests(unittest.TestCase):
    def test_fir_coefficients_are_the_actual_q17_sets(self):
        stages = matrix.parse_fir_stages()
        self.assertEqual([len(stage) for stage in stages], [21, 31, 79])
        self.assertEqual([sum(stage) for stage in stages], [1 << 17] * 3)

    def test_worst_points_are_unique_and_inside_formal_core_band(self):
        points = matrix.worst_stopband_points(matrix.parse_fir_stages())
        self.assertEqual(len(points), 8)
        frequencies = [point["frequency_hz"] for point in points]
        self.assertEqual(len(set(frequencies)), 8)
        self.assertTrue(all(1_000_000 <= value <= 3_000_000 for value in frequencies))

    def test_generated_matrix_covers_formal_10mhz_and_safe_amplitudes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "matrix"
            manifest = matrix.build_manifest(root)
        sine = manifest["sine_points"]
        formal = [point for point in sine if point["stage"] == "I"]
        self.assertEqual(
            {point["frequency_hz"] for point in formal},
            {4_000_000.0, 5_000_000.0, 7_200_000.0, 7_500_000.0, 10_000_000.0},
        )
        self.assertTrue(all(0 < point["source_vpp_v"] <= 0.5 for point in sine))

    def test_arbs_obey_harmonic_grid_and_weak_line_requirement(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "matrix"
            root.mkdir()
            g_records = matrix.generate_g_arbs(root)
            h_records = matrix.generate_h_arbs(root, g_records)
            i_records = matrix.generate_i_arbs(root, g_records)
            matrix.validate_arb_records(g_records + h_records + i_records)
            self.assertEqual(len(g_records), 10)
            self.assertEqual(len(h_records), 15)
            self.assertEqual(len(i_records), 2)
            for record in g_records + h_records + i_records:
                waveform = np.load(root / record["file"], allow_pickle=False)
                self.assertEqual(waveform.shape, (16_384,))
                self.assertLessEqual(float(np.max(waveform)), 1.0 + 1e-12)
                self.assertGreaterEqual(float(np.min(waveform)), -1.0 - 1e-12)

    def test_generate_refuses_overwrite_and_writes_hash_manifest(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "matrix"
            result = matrix.generate(root)
            self.assertTrue(result["pass"])
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "SHA256SUMS").is_file())
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                matrix.generate(root)


if __name__ == "__main__":
    unittest.main()
