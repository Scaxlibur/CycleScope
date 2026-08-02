from __future__ import annotations

import hashlib
import math
import unittest

import m11_calibration as calibration


class M11CalibrationTests(unittest.TestCase):
    def test_training_and_holdout_sets_are_disjoint_and_complete(self):
        self.assertEqual(len(calibration.TRAINING_SWEEP_CASE_IDS), 24)
        self.assertEqual(len(calibration.MINLINE_CASE_IDS), 3)
        self.assertEqual(len(calibration.CROSS_CASE_IDS), 9)
        self.assertEqual(len(calibration.TRAINING_CASE_IDS), 36)
        self.assertEqual(len(calibration.HOLDOUT_CASE_IDS), 7)
        self.assertFalse(
            set(calibration.TRAINING_CASE_IDS) & set(calibration.HOLDOUT_CASE_IDS)
        )
        self.assertFalse(any("holdout" in item for item in calibration.TRAINING_CASE_IDS))

    def test_calibration_id_is_deterministic_nonzero_u16(self):
        digest = hashlib.sha256(b"frozen fit").hexdigest()
        first = calibration.calibration_id_from_identity(digest)
        second = calibration.calibration_id_from_identity(digest)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 1)
        self.assertLessEqual(first, 0xFFFF)
        self.assertEqual(calibration.calibration_id_from_identity("0000" + "a" * 60), 1)

    def test_frequency_response_uses_piecewise_linear_interpolation(self):
        rows = [
            {"frequency_hz": 10_000.0, "ke2e_code_per_v": 2_000.0},
            {"frequency_hz": 20_000.0, "ke2e_code_per_v": 1_900.0},
        ]
        self.assertEqual(
            calibration._linear_interpolate(rows, "ke2e_code_per_v", 15_000.0),
            1_950.0,
        )
        with self.assertRaisesRegex(calibration.CalibrationError, "outside"):
            calibration._linear_interpolate(rows, "ke2e_code_per_v", 9_999.0)

    def test_prediction_inverts_frequency_and_amplitude_model_without_setpoint(self):
        model = {
            "response_rows": [
                {"frequency_hz": 10_000.0, "ke2e_code_per_v": 2_000.0},
                {"frequency_hz": 500_000.0, "ke2e_code_per_v": 1_900.0},
            ],
            "amplitude_rows": [
                {"source_vpp_v": 0.01, "global_gain_factor": 0.99},
                {"source_vpp_v": 0.10, "global_gain_factor": 1.00},
                {"source_vpp_v": 0.45, "global_gain_factor": 1.01},
            ],
        }
        frequency = 255_000.0
        expected_vpp = 0.25
        response = 1_950.0
        factor = 1.0 + (expected_vpp - 0.10) / (0.45 - 0.10) * 0.01
        adc_vpp = expected_vpp * response * factor
        predicted = calibration.predict_source_vpp(model, frequency, adc_vpp)
        self.assertTrue(math.isclose(predicted, expected_vpp, abs_tol=1e-10))

    def test_canonical_identity_hash_ignores_mapping_insertion_order(self):
        self.assertEqual(
            calibration.canonical_sha256({"a": 1, "b": 2}),
            calibration.canonical_sha256({"b": 2, "a": 1}),
        )


if __name__ == "__main__":
    unittest.main()
