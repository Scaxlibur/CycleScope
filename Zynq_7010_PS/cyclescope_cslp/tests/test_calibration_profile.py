from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "cslp_calibration_profile.py"
SPEC = importlib.util.spec_from_file_location("cslp_calibration_profile", MODULE_PATH)
profile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = profile
SPEC.loader.exec_module(profile)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_manifest(root: Path) -> Path:
    artifacts = {}
    for name, file_name in (
        ("calibration_json", "calibration.json"),
        ("response_csv", "response.csv"),
        ("uncertainty_json", "uncertainty.json"),
        ("holdout_report", "holdout-report.json"),
    ):
        path = root / file_name
        path.write_text(f"{name}\n", encoding="utf-8")
        artifacts[name] = {"path": file_name, "sha256": sha256(path)}
    manifest = root / "calibration-build-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "format": profile.FORMAT,
                "status": "validated",
                "calibration_id": 17,
                "scale_uv_per_lsb": 516,
                "offset_uv": -6708,
                "filter_profile": 1,
                "sample_rate_hz": 4_062_500,
                "frame_samples": 8192,
                "matrix_manifest_sha256": "a" * 64,
                "validation": {
                    "holdout_pass": True,
                    "holdout_point_count": 7,
                    "max_absolute_error_v": 0.0049,
                },
                "artifacts": artifacts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


class CalibrationProfileTests(unittest.TestCase):
    def test_valid_profile_binds_artifacts_and_compile_definitions(self):
        with TemporaryDirectory() as temporary:
            manifest = valid_manifest(Path(temporary))
            result = profile.load_calibration_profile(manifest)
        self.assertEqual(result.calibration_id, 17)
        self.assertEqual(result.scale_uv_per_lsb, 516)
        self.assertEqual(result.offset_uv, -6708)
        self.assertEqual(len(result.artifact_records), 4)
        self.assertEqual(
            result.compile_definitions(),
            "CSLP_WAVE_SCALE_UV_PER_LSB=516U;"
            "CSLP_WAVE_OFFSET_UV=-6708;CSLP_WAVE_CALIBRATION_ID=17U",
        )

    def test_zero_id_and_unvalidated_holdout_are_rejected(self):
        with TemporaryDirectory() as temporary:
            manifest = valid_manifest(Path(temporary))
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["calibration_id"] = 0
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(profile.CalibrationProfileError, "calibration_id"):
                profile.load_calibration_profile(manifest)

            payload["calibration_id"] = 17
            payload["validation"]["holdout_pass"] = False
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(profile.CalibrationProfileError, "holdout_pass"):
                profile.load_calibration_profile(manifest)

    def test_holdout_over_5mv_and_tampered_artifact_are_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = valid_manifest(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["validation"]["max_absolute_error_v"] = 0.0050001
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(profile.CalibrationProfileError, "within 5 mV"):
                profile.load_calibration_profile(manifest)

            manifest = valid_manifest(root)
            (root / "response.csv").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(profile.CalibrationProfileError, "SHA-256 mismatch"):
                profile.load_calibration_profile(manifest)

    def test_artifact_escape_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = valid_manifest(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["artifacts"]["response_csv"]["path"] = "../response.csv"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(profile.CalibrationProfileError, "escapes"):
                profile.load_calibration_profile(manifest)


if __name__ == "__main__":
    unittest.main()
