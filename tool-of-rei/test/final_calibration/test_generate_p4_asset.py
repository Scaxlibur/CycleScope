#!/usr/bin/env python3

from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

import calibration_core as core
import generate_p4_asset as generator


class GenerateP4AssetTests(unittest.TestCase):
    def create_frozen_artifacts(self, root: Path, *, holdout_pass: bool = True) -> tuple[Path, Path]:
        fit_dir = root / "fit"
        holdout_dir = root / "holdout"
        fit_dir.mkdir()
        holdout_dir.mkdir()
        response = fit_dir / "response.csv"
        with response.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=("frequency_hz", "input_uv_per_code"),
            )
            writer.writeheader()
            for index, frequency in enumerate(core.TRAIN_FREQUENCIES_HZ):
                writer.writerow(
                    {
                        "frequency_hz": float(frequency),
                        "input_uv_per_code": 260.0 + index * 0.5,
                    }
                )
        identity_sha = "1" * 64
        profile_id = 0x12345678
        scope_binding = {
            "version": 2,
            "manifest_sha256": "2" * 64,
            "amendment_sha256": "3" * 64,
            "case_catalog_sha256": "4" * 64,
            "supported_source_max_vpp_v": core.SUPPORTED_SOURCE_MAX_VPP_V,
            "excluded_case_ids": list(core.EXCLUDED_COMPRESSED_CASE_IDS),
        }
        core.write_json(
            fit_dir / "calibration.json",
            {
                "status": "fit-frozen-before-holdout",
                "p4_response_profile_id": profile_id,
                "identity_sha256": identity_sha,
                "upstream_identity": dict(core.UPSTREAM_IDENTITY),
                "scope_amendment": scope_binding,
            },
        )
        core.write_json(
            fit_dir / "calibration-build-manifest.json",
            {
                "p4_response_profile_id": profile_id,
                "identity_sha256": identity_sha,
                "upstream_identity": dict(core.UPSTREAM_IDENTITY),
                "scope_amendment": scope_binding,
                "response_csv": {
                    "path": "response.csv",
                    "sha256": core.sha256_file(response),
                },
            },
        )
        core.write_sha256s(fit_dir)
        core.write_json(
            holdout_dir / "holdout-report.json",
            {
                "pass": holdout_pass,
                "hard_5mv_1khz_pass": holdout_pass,
                "target_3mv_500hz_pass": holdout_pass,
                "fit_frozen_before_holdout": True,
                "holdout_refit_performed": False,
                "p4_response_profile_id": profile_id,
                "scope_amendment": scope_binding,
            },
        )
        core.write_sha256s(holdout_dir)
        return fit_dir, holdout_dir

    def test_generator_emits_bound_deterministic_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fit_dir, holdout_dir = self.create_frozen_artifacts(root)
            output_dir = root / "asset"
            manifest = generator.generate_asset(fit_dir, holdout_dir, output_dir)
            header = (output_dir / generator.HEADER_NAME).read_text(encoding="utf-8")
            self.assertEqual(manifest["p4_response_profile_id"], 0x12345678)
            self.assertIn(".profile_id = 0x12345678U", header)
            self.assertIn(".calibration_id = 25030U", header)
            self.assertIn(".scale_uv_per_lsb = 516U", header)
            self.assertIn(".anchor_count = kResponseAnchors.size()", header)
            self.assertEqual(core.verify_sha256s(output_dir)["files_verified"], 2)

    def test_generator_rejects_failed_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fit_dir, holdout_dir = self.create_frozen_artifacts(
                root, holdout_pass=False
            )
            with self.assertRaises(core.CalibrationError):
                generator.generate_asset(
                    fit_dir, holdout_dir, root / "asset"
                )


if __name__ == "__main__":
    unittest.main()
