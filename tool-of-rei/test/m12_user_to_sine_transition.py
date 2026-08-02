#!/usr/bin/env python3
"""One-way, fail-closed WaveBench transition from USER/OFF to SIN/OFF.

The public DG4202 driver intentionally refuses fixed-wave transactions when
the current waveform is USER because the volatile arbitrary waveform is not a
restorable basic snapshot.  M12 only needs to replace that USER waveform while
the output is OFF.  This helper permits exactly that one-way exit through the
driver's own transaction implementation; it never emits raw SCPI, changes the
configured 50 ohm load, or enables the source output.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
from typing import Any

from wavebench.config import load_config
from wavebench.errors import DataError, InstrumentError
from wavebench.instruments.registry import resolve_instrument_descriptor
from wavebench.logging import CommandLogger
from wavebench.services.source_service import SourceService
from wavebench_rigol_dg4000.driver import DG4202Source


class UserToSineError(RuntimeError):
    """The source is not eligible for the one-way M12 USER exit."""


class UserOffToSineDriver(DG4202Source):
    """Permit one non-restoring USER/OFF -> SIN/OFF driver transaction."""

    _m12_transition_authorized = False

    def _snapshot_basic_status(self, channel: int):
        if not self._m12_transition_authorized:
            return super()._snapshot_basic_status(channel)
        current = self.get_status(channel)
        if current.function != "USER" or current.output != "OFF":
            raise DataError("M12 USER-to-SIN transition requires current USER/OFF state")
        # Do not claim a USER waveform can be restored.  If the inherited
        # transaction later fails, our recovery below leaves the source OFF
        # and blocks further configuration writes in this session.
        return replace(current, function="SIN")

    def _recover_configuration_failure(self, *, snapshot: Any, original_error: Exception) -> None:
        if not self._m12_transition_authorized:
            return super()._recover_configuration_failure(
                snapshot=snapshot,
                original_error=original_error,
            )
        self._configuration_writes_blocked = True
        try:
            self._force_output_off(snapshot.channel)
            observed = self.get_status(snapshot.channel)
            if observed.output != "OFF":
                raise InstrumentError("M12 USER-to-SIN OFF recovery readback mismatch")
        except Exception as recovery_error:
            raise InstrumentError(
                "M12 USER-to-SIN failed and source OFF recovery could not be verified"
            ) from recovery_error
        raise InstrumentError(
            "M12 USER-to-SIN failed; output is confirmed OFF, the old USER waveform was not "
            "restored, and configuration writes are blocked until a new driver session"
        ) from original_error

    def set_function(
        self,
        channel: int,
        function: str,
        *,
        check_errors: bool | None = None,
    ):
        if function.strip().upper() != "SIN":
            raise DataError("M12 one-way transition permits only SIN")
        current = self.get_status(channel)
        if current.function != "USER" or current.output != "OFF":
            raise DataError("M12 USER-to-SIN transition requires current USER/OFF state")
        self._m12_transition_authorized = True
        try:
            return super().set_function(channel, function, check_errors=check_errors)
        finally:
            self._m12_transition_authorized = False


def profile_failures(profile: Any) -> list[str]:
    status = profile.status
    failures: list[str] = []
    if status.output != "OFF":
        failures.append("DG output is not OFF")
    if status.function != "USER":
        failures.append("DG function is not USER")
    if profile.load_ohm != 50.0:
        failures.append("DG load is not the required 50 ohm")
    if status.amplitude_unit != "VPP" or not 0.0 < status.amplitude <= 0.5:
        failures.append("DG amplitude is not a safe positive VPP value")
    if not math.isclose(status.offset_v, 0.0, abs_tol=1.0e-9):
        failures.append("DG offset is not 0 V")
    if status.frequency_mode != "FIX" or status.sweep_enabled != "OFF":
        failures.append("DG is not FIX with sweep OFF")
    if profile.noise_enabled or profile.burst_enabled or profile.modulation_enabled:
        failures.append("DG noise, burst, or modulation is active")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    if config.safety_limits.max_source_vpp is None or config.safety_limits.max_source_vpp > 0.5:
        raise UserToSineError("M12 transition requires a max_source_vpp safety limit no greater than 0.5")

    logger = CommandLogger()
    base = SourceService(config=config, logger=logger)
    base_driver = None
    driver = None
    before = None
    after = None
    error_text: str | None = None
    try:
        base_driver = base.open_session()
        if not isinstance(base_driver, DG4202Source):
            raise UserToSineError("configured source is not the reviewed DG4202 plugin driver")
        descriptor = resolve_instrument_descriptor("rigol.dg4202", expected_kind="source")
        driver = UserOffToSineDriver(
            transport=base_driver.transport,
            check_errors_after_ops=base_driver.check_errors_after_ops,
        )
        source = SourceService(config=config, logger=logger, session=driver, descriptor=descriptor)
        before = source.channel_profile(1)
        failures = profile_failures(before)
        if failures:
            raise UserToSineError("; ".join(failures))
        status = source.set_function(1, "SIN")
        after = source.channel_profile(1)
        if status.output != "OFF" or status.function != "SIN":
            raise UserToSineError("SIN transition return value is not OFF/SIN")
        if after.status.output != "OFF" or after.status.function != "SIN":
            raise UserToSineError("SIN transition final readback is not OFF/SIN")
        if after.load_ohm != 50.0:
            raise UserToSineError("SIN transition changed the DG 50 ohm load")
        for field, tolerance in (("frequency_hz", 0.01), ("amplitude", 1.0e-6), ("offset_v", 1.0e-9)):
            if not math.isclose(
                float(getattr(after.status, field)),
                float(getattr(before.status, field)),
                rel_tol=0.0,
                abs_tol=tolerance,
            ):
                raise UserToSineError(f"SIN transition unexpectedly changed DG {field}")
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
    finally:
        try:
            if driver is not None:
                driver.close()
            elif base_driver is not None:
                base_driver.close()
        except Exception as error:
            error_text = error_text or f"source close: {type(error).__name__}: {error}"

    result = {
        "format": "CycleScope M12 USER/OFF to SIN/OFF one-way transaction v1",
        "raw_scpi_entrypoint_used": False,
        "source_output_on_writes": 0,
        "dp800_writes": 0,
        "old_user_waveform_restored": False,
        "before": None if before is None else before.as_dict(),
        "after": None if after is None else after.as_dict(),
        "error": error_text,
        "pass": error_text is None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
