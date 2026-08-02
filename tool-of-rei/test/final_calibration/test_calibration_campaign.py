#!/usr/bin/env python3

from __future__ import annotations

from types import SimpleNamespace
import unittest

import calibration_campaign as campaign


def profile(**overrides):
    status = {
        "output": "OFF",
        "offset_v": 0.0,
        "amplitude_unit": "VPP",
        "amplitude": 0.1,
        "frequency_mode": "FIX",
        "sweep_enabled": "OFF",
        "function": "SIN",
        "frequency_hz": 100_000.0,
    }
    status.update(overrides.pop("status", {}))
    values = {
        "status": SimpleNamespace(**status),
        "load_ohm": 50.0,
        "polarity": "NORMAL",
        "noise_enabled": False,
        "burst_enabled": False,
        "modulation_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CalibrationCampaignTests(unittest.TestCase):
    def test_source_profile_accepts_only_frozen_safe_state(self) -> None:
        campaign.assert_source_profile(
            profile(),
            expected_frequency_hz=100_000.0,
            expected_vpp=0.1,
            expected_function="SIN",
        )
        for unsafe in (
            profile(status={"output": "ON"}),
            profile(load_ohm=1_000_000.0),
            profile(status={"offset_v": 0.1}),
            profile(modulation_enabled=True),
            profile(status={"amplitude": 0.51}),
        ):
            with self.assertRaises(campaign.CampaignError):
                campaign.assert_source_profile(unsafe)

    def test_groups_exclude_unsupported_450mvpp_cases(self) -> None:
        low = campaign.group_case_ids("m4-cross-low")
        self.assertEqual(len(low), 6)
        self.assertTrue(all("450mVpp" not in case_id for case_id in low))
        with self.assertRaises(campaign.CampaignError):
            campaign.group_case_ids("m4-cross-450")
        self.assertTrue(
            set(campaign.core.EXCLUDED_COMPRESSED_CASE_IDS).isdisjoint(
                campaign.core.CASES_BY_ID
            )
        )

    def test_vertical_scale_covers_observed_ten_x_setting_to_ch2_gain(self) -> None:
        self.assertEqual(campaign.choose_vertical_scale(0.01), 0.02)
        self.assertEqual(campaign.choose_vertical_scale(0.1), 0.2)
        self.assertEqual(campaign.choose_vertical_scale(0.25), 0.5)


if __name__ == "__main__":
    unittest.main()
