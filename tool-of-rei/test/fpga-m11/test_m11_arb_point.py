from __future__ import annotations

import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

import m11_arb_point as arb
import m11_sine_point as sine


CALIBRATION_MANIFEST = (
    sine.EVIDENCE_ROOT
    / "offline"
    / "calibration-v1"
    / "calibration-build-manifest.json"
)


class M11ArbPointTests(unittest.TestCase):
    def test_matrix_has_expected_g_h_i_shape(self):
        manifest = sine.load_json(arb.MATRIX_ROOT / "manifest.json")
        stages = [record["stage"] for record in manifest["arb_points"]]
        self.assertEqual(stages.count("G"), 10)
        self.assertEqual(stages.count("H"), 15)
        self.assertEqual(stages.count("I"), 2)

    def test_load_case_binds_waveform_and_manifest(self):
        record = arb.load_arb_case("g-b-low-low-crest")
        self.assertEqual(record["stage"], "G")
        self.assertTrue(Path(record["waveform_path"]).is_file())
        self.assertEqual(len(record["matrix_manifest_sha256"]), 64)

    def test_stage_acknowledgement_and_ceiling_are_fail_closed(self):
        record = arb.load_arb_case("g-b-low-low-crest")
        with self.assertRaisesRegex(arb.M11ArbPointError, "stage G requires"):
            arb.validate_stage(record, "wrong")
        arb.validate_stage(record, arb.G_STAGE_ACK)
        over = {**record, "source_vpp_v": 0.250001}
        with self.assertRaisesRegex(arb.M11ArbPointError, "must not exceed"):
            arb.validate_stage(over, arb.G_STAGE_ACK)

    def test_configuration_plan_uploads_off_and_never_writes_power(self):
        record = arb.load_arb_case("g-b-low-low-crest")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.toml"
            path.write_text(arb.plan_text(record), encoding="utf-8")
            result = arb.validate_configuration_plan(
                path, record, arb.safety.derived_config()
            )
        self.assertFalse(result["output_on_during_upload"])
        self.assertEqual(
            result["steps"],
            [
                "source.status",
                "power.status",
                "source.arb_load",
                "source.status",
                "power.status",
            ],
        )

    def test_user_function_exception_is_only_accepted_while_off(self):
        generic = {
            "pass": False,
            "evidence_path": "/tmp/generic.json",
            "failures": ["DG CH1 function is not safely restorable: 'USER'"],
            "source": {"profile": {"status": {"function": "USER", "output": "OFF"}}},
        }
        accepted = arb.filter_arb_preflight(generic)
        self.assertTrue(accepted["pass"])
        self.assertIsNotNone(accepted["accepted_arb_exception"])

        unsafe = {
            **generic,
            "source": {"profile": {"status": {"function": "USER", "output": "ON"}}},
        }
        rejected = arb.filter_arb_preflight(unsafe)
        self.assertFalse(rejected["pass"])

    def test_known_component_fit_does_not_assume_largest_line_is_fundamental(self):
        sample_rate_hz = 2_000_000.0
        samples = 8_000
        time_s = np.arange(samples, dtype=np.float64) / sample_rate_hz
        values = (
            0.005 * np.sin(2.0 * math.pi * 50_000.0 * time_s + 0.2)
            + 0.025 * np.sin(2.0 * math.pi * 150_000.0 * time_s - 0.4)
            + 0.015 * np.sin(2.0 * math.pi * 250_000.0 * time_s + 0.7)
        )
        components, residual, _fit = arb._joint_fit(
            values, sample_rate_hz, [50_000.0, 150_000.0, 250_000.0]
        )
        self.assertTrue(math.isclose(math.hypot(*components[50_000.0]), 0.005, abs_tol=1e-12))
        self.assertTrue(math.isclose(math.hypot(*components[150_000.0]), 0.025, abs_tol=1e-12))
        self.assertTrue(math.isclose(math.hypot(*components[250_000.0]), 0.015, abs_tol=1e-12))
        self.assertLess(float(np.max(np.abs(residual))), 1e-11)

    def test_missing_calibration_is_rejected_before_instrument_preflight(self):
        with patch.object(arb.safety, "readonly_preflight") as preflight:
            with self.assertRaisesRegex(arb.M11ArbPointError, "calibration-manifest"):
                arb.run_live(
                    case_id="g-b-low-low-crest",
                    frames=64,
                    acknowledgement=arb.LIVE_ACK,
                    stage_acknowledgement=arb.G_STAGE_ACK,
                    calibration_manifest=None,
                )
        preflight.assert_not_called()

    def test_validated_manifest_has_nonzero_identity(self):
        identity = arb.require_nonzero_calibration(CALIBRATION_MANIFEST)
        self.assertEqual(identity["calibration_id"], 25030)
        self.assertEqual(identity["scale_uv_per_lsb"], 516)
        self.assertEqual(identity["offset_uv"], -6761)

    def test_all_arb_stages_enforce_at_least_64_frames(self):
        for stage in ("G", "H", "I"):
            self.assertEqual(
                arb._minimum_frames({"stage": stage, "minimum_frames": 22}, 22),
                64,
            )


if __name__ == "__main__":
    unittest.main()
