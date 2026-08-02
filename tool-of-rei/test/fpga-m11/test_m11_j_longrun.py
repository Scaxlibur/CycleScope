from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import m11_j_longrun as j


class M11JLongrunTests(unittest.TestCase):
    @staticmethod
    def _gem_text(
        *,
        tx_frames: int = 100,
        nwcfg: int = 0x00000003,
        overrides: dict[int, int] | None = None,
    ) -> str:
        values = {address: 0 for address in j.GEM_REGISTER_NAMES}
        values[0xF8000140] = 0x00500801
        values[0xE000B004] = nwcfg
        values[0xE000B108] = tx_frames
        values.update(overrides or {})
        lines = ["M11_GEM_READONLY_BEGIN"]
        lines.extend(
            f"{address:08X}: {values[address]:08X}"
            for address in j.GEM_REGISTER_NAMES
        )
        lines.append("M11_GEM_READONLY_END")
        return "\n".join(lines)

    def test_frozen_h_selection_uses_largest_safe_b_edge_stimulus(self):
        selected = j.select_longrun_case()
        self.assertTrue(selected["pass"])
        self.assertEqual(selected["case_id"], "h-b-edge-j-3e+06Hz")
        self.assertEqual(selected["u_j_frequency_hz"], 3e6)
        self.assertLessEqual(selected["source_vpp_v"], 0.45)

    def test_upper_frequency_gate_requires_exact_passing_seven_point_summary(self):
        valid = {
            "pass": True,
            "target_pass": True,
            "point_count": 7,
            "sine_point_count": 5,
            "arb_point_count": 2,
            "minimum_attenuation_lower_bound_db": 72.0,
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertTrue(j.upper_frequency_gate(path)["pass"])
            valid["point_count"] = 6
            path.write_text(json.dumps(valid), encoding="utf-8")
            with self.assertRaisesRegex(j.M11JError, "seven points"):
                j.upper_frequency_gate(path)

    def test_response_gate_has_closed_250_ms_boundary_and_rejects_missing(self):
        self.assertTrue(
            j.response_gate({"first_complete_frame_latency_us": 250_000.0})["pass"]
        )
        self.assertFalse(
            j.response_gate({"first_complete_frame_latency_us": 250_000.001})["pass"]
        )
        self.assertFalse(j.response_gate({})["pass"])
        self.assertFalse(
            j.response_gate({"first_complete_frame_latency_us": math.nan})["pass"]
        )

    def test_exact_longrun_gate_uses_10000_request_plus_one_terminal_frame(self):
        report = {
            "pass": True,
            "counters": {
                "frames_completed": j.EXPECTED_FRAMES,
                "wave_packets": j.EXPECTED_WAVE_PACKETS,
            },
            "capture": {"frame_count": j.EXPECTED_FRAMES},
            "expected_frames_after_deferred_disable": j.EXPECTED_FRAMES,
            "status_delta": {
                "frames_sent": j.EXPECTED_FRAMES,
                "packets_sent": j.EXPECTED_WAVE_PACKETS,
            },
        }
        result = j.exact_longrun_gate(report)
        self.assertTrue(result["pass"])
        self.assertEqual(result["requested_frames"], 10_000)
        self.assertEqual(result["expected_frames"], 10_001)
        report["counters"]["frames_completed"] = 10_002
        self.assertFalse(j.exact_longrun_gate(report)["pass"])

    def test_progress_parser_returns_latest_complete_progress_record(self):
        text = "noise\nPROGRESS frames=100 packets=1200\nPROGRESS frames=5000 packets=60000\n"
        self.assertEqual(j.progress_frame_count(text), 5000)
        self.assertIsNone(j.progress_frame_count("no progress yet"))

    def test_gem_snapshot_parser_requires_complete_read_only_register_set(self):
        parsed = j.parse_gem_snapshot(self._gem_text())
        self.assertTrue(parsed["pass"])
        self.assertEqual(parsed["error_counters_nonzero"], {})
        self.assertEqual(parsed["registers"]["TX_FRAMES"]["value"], 100)

        unsafe = j.parse_gem_snapshot(
            self._gem_text(nwcfg=0, overrides={0xE000B190: 2})
        )
        self.assertFalse(unsafe["pass"])
        self.assertEqual(unsafe["error_counters_nonzero"], {"RX_FCS": 2})

        missing = "\n".join(self._gem_text().splitlines()[:-2] + ["M11_GEM_READONLY_END"])
        with self.assertRaisesRegex(j.M11JError, "missing"):
            j.parse_gem_snapshot(missing)

    def test_gem_snapshot_parser_rejects_duplicate_register(self):
        text = self._gem_text().replace(
            "M11_GEM_READONLY_END",
            "E000B108: 00000064\nM11_GEM_READONLY_END",
        )
        with self.assertRaisesRegex(j.M11JError, "duplicate"):
            j.parse_gem_snapshot(text)

    def test_gem_delta_accepts_counter_wrap_and_exact_minimum(self):
        before_value = 0xFFFFFF00
        after_value = (before_value + j.EXPECTED_WAVE_PACKETS) & 0xFFFFFFFF
        before = j.parse_gem_snapshot(self._gem_text(tx_frames=before_value))
        after = j.parse_gem_snapshot(self._gem_text(tx_frames=after_value))
        result = j.gem_delta(before, after)
        self.assertTrue(result["pass"])
        self.assertEqual(result["tx_frames_delta"], j.EXPECTED_WAVE_PACKETS)

    def test_live_acknowledgement_is_exact(self):
        j.require_live_acknowledgement(j.J_LIVE_ACK)
        with self.assertRaisesRegex(j.M11JError, "requires --acknowledge"):
            j.require_live_acknowledgement("almost")

    def test_gem_tcl_contains_only_target_selection_and_memory_reads(self):
        source = j.GEM_TCL.read_text(encoding="utf-8")
        self.assertIn("puts [mrd $address]", source)
        for forbidden in ("mwr ", "rst ", "stop ", "dow ", "con "):
            self.assertNotIn(forbidden, source)

    def test_gain_drift_uses_ten_1000_frame_blocks_and_excludes_terminal(self):
        frames = []
        for index in range(j.EXPECTED_FRAMES):
            block = min(index // j.GAIN_BLOCK_FRAMES, 9)
            gain_db = block * 0.004
            frames.append(
                {"recovered_band_vpp_v": 0.25 * 10.0 ** (gain_db / 20.0)}
            )
        result = j.gain_drift_summary({"frames": frames})
        self.assertTrue(result["pass"])
        self.assertEqual(len(result["blocks"]), 10)
        self.assertTrue(result["terminal_frame_excluded_from_drift"])
        for item in frames[9_000:10_000]:
            item["recovered_band_vpp_v"] = 0.25 * 10.0 ** (0.051 / 20.0)
        self.assertFalse(j.gain_drift_summary({"frames": frames})["pass"])

    def test_j_module_contains_no_dp800_write_entrypoint(self):
        source = Path(j.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "power.set_voltage_current_limit(",
            "power.set_protection(",
            "power.set_output(",
            "power.output",
            "power.set",
        ):
            self.assertNotIn(forbidden, source)

    def test_recorded_gem_only_failure_is_safe_to_resume_without_arb_reupload(self):
        prior = (
            j.EVIDENCE_ROOT
            / "points"
            / "20260801_021631_239904+0800_j-response-longrun"
        )
        payload = j.load_json(prior / "point.json")
        current = j.load_json(Path(payload["final_preflight_evidence"]))
        result = j.validate_preoutput_resume(
            prior,
            j.select_longrun_case(),
            current,
        )
        self.assertTrue(result["pass"])
        self.assertFalse(result["prior_source_output_on"])
        self.assertFalse(result["repeat_arb_upload_performed_during_resume"])

    def test_fresh_source_off_discards_old_transport_and_writes_at_most_once(self):
        session = Mock()
        service = Mock()
        service.open_session.return_value = session
        service.status.side_effect = [
            SimpleNamespace(output="ON", as_dict=lambda: {"output": "ON"}),
            SimpleNamespace(output="OFF", as_dict=lambda: {"output": "OFF"}),
        ]
        service.set_output.return_value = SimpleNamespace(
            output="OFF", as_dict=lambda: {"output": "OFF"}
        )
        with patch.object(j, "SourceService", return_value=service):
            result = j.fresh_source_output_off(Mock(), Mock())
        self.assertTrue(result["pass"])
        self.assertTrue(result["off_write_performed"])
        service.set_output.assert_called_once_with(1, False)
        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
