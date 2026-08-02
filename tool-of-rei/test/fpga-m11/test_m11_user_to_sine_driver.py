from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from wavebench.errors import DataError, InstrumentError
from wavebench_rigol_dg4000.driver import DG4202Source

import m11_user_to_sine_driver as transition


class M11UserToSineDriverTests(unittest.TestCase):
    def test_installed_plugin_identity_and_source_hash_are_frozen(self):
        audit = transition.verify_installed_driver()
        self.assertEqual(audit["distribution"], "wavebench-rigol-dg4000")
        self.assertEqual(audit["version"], "1.1.0")
        self.assertEqual(
            audit["driver_source_sha256"], transition.EXPECTED_DRIVER_SHA256
        )

    def test_user_snapshot_requires_narrow_authorization_and_output_off(self):
        driver = transition.M11UserToSineDG4202Source(transport=Mock())
        off = SimpleNamespace(function="USER", output="OFF")
        driver.get_status = Mock(return_value=off)
        with self.assertRaisesRegex(DataError, "authorized only"):
            driver._snapshot_basic_status(1)

        driver._m11_user_to_sine_authorized = True
        self.assertIs(driver._snapshot_basic_status(1), off)
        driver.get_status = Mock(return_value=SimpleNamespace(function="USER", output="ON"))
        with self.assertRaisesRegex(DataError, "requires USER output to be OFF"):
            driver._snapshot_basic_status(1)

    def test_non_user_snapshot_still_delegates_to_installed_policy(self):
        driver = transition.M11UserToSineDG4202Source(transport=Mock())
        driver.get_status = Mock(return_value=SimpleNamespace(function="SIN", output="OFF"))
        marker = SimpleNamespace(function="SIN", output="OFF")
        with patch.object(DG4202Source, "_snapshot_basic_status", return_value=marker) as base:
            result = driver._snapshot_basic_status(1)
        self.assertIs(result, marker)
        base.assert_called_once_with(1)

    def test_failed_user_transition_forces_off_without_restoring_user(self):
        driver = transition.M11UserToSineDG4202Source(transport=Mock())
        driver._m11_user_to_sine_authorized = True
        driver._force_output_off = Mock()
        driver.get_status = Mock(return_value=SimpleNamespace(output="OFF"))
        snapshot = SimpleNamespace(function="USER", output="OFF", channel=1)
        with self.assertRaisesRegex(InstrumentError, "old USER waveform was not restored"):
            driver._recover_configuration_failure(
                snapshot=snapshot,
                original_error=InstrumentError("ambiguous test write"),
            )
        driver._force_output_off.assert_called_once_with(1)
        self.assertTrue(driver._configuration_writes_blocked)

    def test_checked_plan_binding_rejects_wrong_function_before_instrument_io(self):
        text = '''[experiment]
name = "test"
label = "test"

[safety]
scope_guard_channel = 1
require_scope_coupling_not = ["DC", "AC"]
allow_50ohm = false

[[steps]]
kind = "source.status"
channel = 1
[[steps]]
kind = "power.status"
channel = 1
[[steps]]
kind = "source.output"
channel = 1
state = "off"
[[steps]]
kind = "source.set_func"
channel = 1
function = "SQU"
[[steps]]
kind = "source.set_vpp"
channel = 1
value_vpp = 0.2
[[steps]]
kind = "source.set_freq"
channel = 1
frequency_hz = 4000000
[[steps]]
kind = "source.status"
channel = 1
[[steps]]
kind = "power.status"
channel = 1
'''
        with TemporaryDirectory() as temporary:
            plan = Path(temporary) / "plan.toml"
            plan.write_text(text, encoding="utf-8")
            checked = {
                "path": str(plan.resolve()),
                "sha256": transition.sha256_file(plan),
                "steps": transition.EXPECTED_PLAN_STEPS,
            }
            with patch.object(transition.SourceService, "open_session") as open_session:
                with self.assertRaisesRegex(
                    transition.UserToSineTransitionError,
                    "exactly one SIN",
                ):
                    transition.validate_checked_plan_binding(
                        config=Mock(),
                        plan_path=plan,
                        checked_plan=checked,
                        frequency_hz=4_000_000.0,
                        source_vpp_v=0.2,
                    )
            open_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
