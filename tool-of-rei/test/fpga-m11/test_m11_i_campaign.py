from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import m11_i_campaign as campaign


class M11ICampaignTests(unittest.TestCase):
    def test_frozen_i_order_is_five_sines_then_two_combinations(self):
        records = campaign.ordered_cases()
        self.assertEqual(
            [item["case_id"] for item in records],
            [
                "i-formal-4e+06Hz",
                "i-formal-5e+06Hz",
                "i-formal-7.2e+06Hz",
                "i-formal-7.5e+06Hz",
                "i-formal-1e+07Hz",
                "i-b-edge-j-5e+06Hz",
                "i-b-edge-j-1e+07Hz",
            ],
        )
        self.assertEqual([item["kind"] for item in records], ["sine"] * 5 + ["arb"] * 2)

    def test_attempt_classifier_skips_one_pass_and_blocks_blind_retry(self):
        self.assertEqual(campaign.classify_attempts("case", [])["state"], "pending")
        passing = {"state": "pass", "directory": "/pass"}
        failed = {"state": "failed", "directory": "/failed"}
        audited = {"state": "audited_nonpoint", "directory": "/pre-output"}
        self.assertEqual(
            campaign.classify_attempts("case", [audited])["state"], "pending"
        )
        self.assertEqual(
            campaign.classify_attempts("case", [passing, failed])["state"], "done"
        )
        blocked = campaign.classify_attempts("case", [failed])
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("automatic retry is forbidden", blocked["failures"][0])
        duplicate = campaign.classify_attempts("case", [passing, passing])
        self.assertEqual(duplicate["state"], "blocked")

    def test_current_evidence_has_no_blocked_or_duplicate_i_point(self):
        status = campaign.campaign_status()
        self.assertEqual(status["point_count"], 7)
        self.assertEqual(status["done_count"] + status["pending_count"], 7)
        self.assertEqual(status["failures"], [])
        self.assertNotIn("blocked", {item["state"] for item in status["cases"]})
        self.assertTrue(
            all(
                len(
                    [
                        attempt
                        for attempt in item["attempts"]
                        if attempt.get("state") == "pass"
                    ]
                )
                <= 1
                for item in status["cases"]
            )
        )

    def test_live_ack_is_checked_before_status_or_instrument_path(self):
        with patch.object(campaign, "campaign_status") as status:
            with self.assertRaisesRegex(campaign.M11ICampaignError, "requires --acknowledge"):
                campaign.run_missing("wrong")
        status.assert_not_called()

    def test_arb_prime_is_required_only_from_sin(self):
        self.assertTrue(campaign.arb_prime_required("SIN"))
        self.assertFalse(campaign.arb_prime_required("USER"))
        with self.assertRaisesRegex(campaign.M11ICampaignError, "SIN/OFF or USER/OFF"):
            campaign.arb_prime_required("SQU")

    def test_arb_prime_record_is_enriched_and_hash_checked_before_live_path(self):
        record = next(item for item in campaign.ordered_cases() if item["kind"] == "arb")
        self.assertNotIn("waveform_path", record)
        loaded = campaign.resolve_arb_prime_record(record)
        self.assertEqual(loaded["case_id"], record["case_id"])
        self.assertTrue(Path(loaded["waveform_path"]).is_file())

    def test_all_passing_points_are_skipped_without_live_calls(self):
        records = [
            {"case_id": "s", "kind": "sine", "minimum_frames": 22},
            {"case_id": "a", "kind": "arb", "minimum_frames": 64},
        ]
        status = {
            "failures": [],
            "cases": [
                {"case_id": "s", "state": "done"},
                {"case_id": "a", "state": "done"},
            ],
            "points_complete": True,
            "pass": True,
        }
        with (
            patch.object(campaign, "ordered_cases", return_value=records),
            patch.object(campaign, "campaign_status", return_value=status),
            patch.object(campaign, "_write_summary_if_ready", return_value={"pass": True}),
            patch.object(campaign.sine, "run_live") as sine_live,
            patch.object(campaign.arb, "run_live") as arb_live,
        ):
            result = campaign.run_missing(campaign.I_CAMPAIGN_ACK)
        self.assertTrue(result["pass"])
        self.assertEqual(result["executed"], [])
        sine_live.assert_not_called()
        arb_live.assert_not_called()

    def test_campaign_contains_no_dp832_write_entrypoint(self):
        source = Path(campaign.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "power.set_voltage_current_limit(",
            "power.set_protection(",
            "power.set_output(",
            "power.output",
            "power.set",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
