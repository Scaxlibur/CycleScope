from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


SCRIPT = Path(__file__).with_name("m11_arb_dry_run.py")
SPEC = importlib.util.spec_from_file_location("m11_arb_dry_run", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dry_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dry_run)


class M11ArbDryRunTests(unittest.TestCase):
    def _record(self, root: Path, **changes):
        waveform = root / "arb" / "case.npy"
        waveform.parent.mkdir(parents=True)
        waveform.write_bytes(b"waveform")
        values = {
            "case_id": "g-case",
            "kind": "arb",
            "stage": "G",
            "file": "arb/case.npy",
            "sha256": hashlib.sha256(b"waveform").hexdigest(),
            "points": 4,
            "source_vpp_v": 0.5,
            "playback_frequency_hz": 500.0,
        }
        values.update(changes)
        return values, waveform

    def test_record_validation_enforces_hash_path_and_amplitude(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, waveform = self._record(root)
            self.assertEqual(dry_run.validate_record(root, record), waveform.resolve())
            with self.assertRaisesRegex(RuntimeError, "0.5 Vpp"):
                dry_run.validate_record(root, {**record, "source_vpp_v": 0.5001})
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                dry_run.validate_record(root, {**record, "file": "../outside.npy"})

    def test_command_is_dry_run_without_output_on(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, waveform = self._record(root)
            command = dry_run.build_command(
                record=record,
                waveform=waveform,
                arb_name="M11G01",
                payload_path=root / "payload.json",
            )
        self.assertIn("--dry-run", command)
        self.assertNotIn("--output-on", command)
        self.assertEqual(command[1:3], ["source", "arb-load"])
        payload_argument = Path(command[command.index("--export-payload") + 1])
        self.assertTrue(payload_argument.is_absolute())

    def test_matrix_source_hashes_must_match_current_facts(self):
        expected = dry_run.expected_source_hashes()
        self.assertEqual(dry_run.validate_source_hashes({"source_hashes": expected}), expected)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            dry_run.validate_source_hashes(
                {"source_hashes": {**expected, "m11_plan": "0" * 64}}
            )

    def test_payload_validation_accepts_bounded_dac14(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, waveform = self._record(root)
            payload = {
                "format": "wavebench.arbitrary.v1",
                "target": {
                    "channel": 1,
                    "name": "M11G01",
                    "amplitude_vpp": 0.5,
                    "offset_v": 0.0,
                },
                "source": {"source_path": str(waveform), "points": 4},
                "payload": {
                    "encoding": "dac14_unsigned_integer",
                    "values": [0, 8191, 8192, 16383],
                },
            }
            dry_run.validate_payload(
                record=record,
                waveform=waveform,
                arb_name="M11G01",
                payload=payload,
            )


if __name__ == "__main__":
    unittest.main()
