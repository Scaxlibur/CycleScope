from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wavebench.errors import DataError
from wavebench_rigol_dg4000.driver import DG4202Source

import m11_repeat_arb_driver as repeat


class M11RepeatArbDriverTests(unittest.TestCase):
    def test_installed_plugin_identity_and_source_hash_are_frozen(self):
        audit = repeat.verify_installed_driver()
        self.assertEqual(audit["distribution"], "wavebench-rigol-dg4000")
        self.assertEqual(audit["version"], "1.1.0")
        self.assertEqual(audit["driver_source_sha256"], repeat.EXPECTED_DRIVER_SHA256)

    def test_user_snapshot_is_accepted_only_when_output_is_off(self):
        driver = repeat.M11RepeatArbDG4202Source(transport=Mock())
        off = SimpleNamespace(function="USER", output="OFF")
        driver.get_status = Mock(return_value=off)
        self.assertIs(driver._snapshot_basic_status(1), off)

        driver.get_status = Mock(
            return_value=SimpleNamespace(function="USER", output="ON")
        )
        with self.assertRaisesRegex(DataError, "requires USER output to be OFF"):
            driver._snapshot_basic_status(1)

    def test_basic_function_still_delegates_to_installed_policy(self):
        driver = repeat.M11RepeatArbDG4202Source(transport=Mock())
        driver.get_status = Mock(return_value=SimpleNamespace(function="SIN", output="OFF"))
        marker = SimpleNamespace(function="SIN", output="OFF")
        with patch.object(DG4202Source, "_snapshot_basic_status", return_value=marker) as base:
            result = driver._snapshot_basic_status(1)
        self.assertIs(result, marker)
        base.assert_called_once_with(1)

    def test_repeat_upload_guards_reject_bad_amplitude_before_opening_source(self):
        with patch.object(repeat.SourceService, "open_session") as open_session:
            with self.assertRaisesRegex(repeat.RepeatArbDriverError, "waveform/amplitude"):
                repeat.upload_repeated_arb(
                    config=Mock(),
                    logger=Mock(),
                    waveform=Path("/does/not/exist"),
                    playback_frequency_hz=500.0,
                    amplitude_vpp=0.46,
                    points=16_384,
                )
        open_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
