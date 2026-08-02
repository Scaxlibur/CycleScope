#!/usr/bin/env python3

from __future__ import annotations

import csv
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

import calibration_core as core


class CalibrationCoreTests(unittest.TestCase):
    def test_catalog_is_exact_and_holdouts_are_separate(self) -> None:
        self.assertEqual(len(core.CASES), 44)
        self.assertEqual(len(core.M3_CASE_IDS), 24)
        self.assertEqual(len(core.M4_CASE_IDS), 9)
        self.assertEqual(len(core.HOLDOUT_CASE_IDS), 7)
        self.assertTrue(set(core.TRAINING_CASE_IDS).isdisjoint(core.HOLDOUT_CASE_IDS))
        self.assertTrue(
            set(core.EXCLUDED_COMPRESSED_CASE_IDS).isdisjoint(core.CASES_BY_ID)
        )
        self.assertEqual(core.SUPPORTED_SOURCE_MAX_VPP_V, 0.25)
        self.assertLessEqual(
            max(case.source_vpp_v for case in core.CASES),
            core.SUPPORTED_SOURCE_MAX_VPP_V,
        )

    def test_catalog_v2_records_compression_exclusions(self) -> None:
        catalog = core.catalog_payload()
        self.assertEqual(catalog["counts"], {"all": 44, "m2": 4, "m3": 24, "m4": 9, "m6": 7})
        self.assertEqual(
            catalog["excluded_historical_case_ids"],
            list(core.EXCLUDED_COMPRESSED_CASE_IDS),
        )

    def test_tone_fit_recovers_noncoherent_amplitude_and_phase(self) -> None:
        sample_rate = 4_062_500.0
        frequency = 123_456.0
        samples = 8192
        peak = 91.25
        phase = 0.73
        time_s = np.arange(samples) / sample_rate
        values = 13.0 + peak * np.sin(2.0 * math.pi * frequency * time_s + phase)
        values += 2.5 * np.sin(2.0 * math.pi * 2.0 * frequency * time_s - 0.2)
        result = core.tone_fit(values, sample_rate, frequency)
        self.assertAlmostEqual(result["fundamental_peak"], peak, places=9)
        self.assertAlmostEqual(result["fundamental_phase_rad"], phase, places=9)
        self.assertAlmostEqual(result["fit_offset"], 13.0, places=9)

    def test_linear_interpolation_forbids_extrapolation_and_duplicates(self) -> None:
        rows = [
            {"frequency_hz": 10_000.0, "value": 500.0},
            {"frequency_hz": 20_000.0, "value": 520.0},
        ]
        self.assertEqual(core.linear_interpolate(rows, "value", 15_000.0), 510.0)
        with self.assertRaises(core.CalibrationError):
            core.linear_interpolate(rows, "value", 9_999.0)
        duplicate = [rows[0], dict(rows[0])]
        with self.assertRaises(core.CalibrationError):
            core.linear_interpolate(duplicate, "value", 10_000.0)

    def test_identity_gate_rejects_overrange_and_noncontiguous_frames(self) -> None:
        records = []
        for index in range(64):
            records.append(
                {
                    **core.UPSTREAM_IDENTITY,
                    "core_flags": 0x000C,
                    "frame_id": 100 + index,
                }
            )
        self.assertTrue(core.validate_mirror_metadata(records)["contiguous"])
        bad_flags = [dict(item) for item in records]
        bad_flags[10]["core_flags"] = 0x001C
        with self.assertRaises(core.CalibrationError):
            core.validate_mirror_metadata(bad_flags)
        bad_ids = [dict(item) for item in records]
        bad_ids[20]["frame_id"] += 1
        with self.assertRaises(core.CalibrationError):
            core.validate_mirror_metadata(bad_ids)

    def test_response_csv_rejects_nonfinite_value(self) -> None:
        rows = [
            {"frequency_hz": 10_000.0, "value": 500.0},
            {"frequency_hz": 20_000.0, "value": math.nan},
        ]
        with self.assertRaises(core.CalibrationError):
            core.linear_interpolate(rows, "value", 15_000.0)

    def test_profile_id_is_stable_for_canonical_payload(self) -> None:
        left = core.canonical_sha256({"b": 2, "a": 1})
        right = core.canonical_sha256({"a": 1, "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)

    def test_scope_amendment_preserves_stop_and_binds_active_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core.write_json(root / "campaign.json", {"format": "legacy campaign"})
            core.write_json(root / "case-catalog.json", {"format": "legacy catalog"})
            core.write_json(
                root / "campaign-stop.json",
                {
                    "campaign_status": "STOPPED_M4_AMPLITUDE_DEPENDENCE",
                    "failed_case_id": core.EXCLUDED_COMPRESSED_CASE_IDS[0],
                    "m5_fit_created": False,
                    "m6_holdouts_read": False,
                    "end_to_end_deviation_percent": -3.1224,
                },
            )
            original_manifest = core.write_sha256s(root)
            original_manifest_sha = core.sha256_file(original_manifest)
            output = root / core.SCOPE_AMENDMENT_DIRECTORY
            result = core.build_scope_amendment(root, output)
            binding = core.scope_amendment_binding(root)
            self.assertEqual(result["binding"], binding)
            self.assertEqual(core.sha256_file(original_manifest), original_manifest_sha)
            self.assertEqual(binding["excluded_case_ids"], list(core.EXCLUDED_COMPRESSED_CASE_IDS))
            self.assertEqual(core.verify_sha256s(output)["files_verified"], 2)

    def test_tone_analysis_saturation_gate_scans_all_frames(self) -> None:
        records = [
            {"minimum_code": -20.0, "maximum_code": 10.0},
            {"minimum_code": -15.0, "maximum_code": 32767.0},
        ]
        maximum = max(
            max(abs(item["minimum_code"]), abs(item["maximum_code"])) for item in records
        )
        self.assertEqual(maximum, 32767.0)


if __name__ == "__main__":
    unittest.main()
