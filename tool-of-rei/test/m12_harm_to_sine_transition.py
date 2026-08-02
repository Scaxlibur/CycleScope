#!/usr/bin/env python3
"""One-way, fail-closed WaveBench transition from HARM/OFF to SIN/OFF.

This exists solely because the public DG4000 plugin refuses fixed-wave
transactions from a manually configured HARM profile.  It uses the plugin's
own transaction implementation, never sends a raw SCPI command, never changes
the configured 50 ohm load, and never turns the output on.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wavebench.config import load_config
from wavebench.errors import DataError, InstrumentError
from wavebench.instruments.registry import resolve_instrument_descriptor
from wavebench.logging import CommandLogger
from wavebench.services.source_service import SourceService
from wavebench_rigol_dg4000.driver import DG4202Source


class HarmToSineError(RuntimeError):
    """The source is not eligible for the one-way safety transition."""


class HarmOffToSineDriver(DG4202Source):
    """Permit one non-restoring HARM/OFF -> SIN/OFF driver transaction."""

    _m12_transition_authorized = False

    def _snapshot_harmonic_transaction(self, channel: int):
        """Replace only the plugin's un-restorable HARM snapshot for this exit."""
        if not self._m12_transition_authorized:
            return super()._snapshot_harmonic_transaction(channel)
        context = self.get_channel_profile(channel)
        if context.status.function != "HARM" or context.status.output != "OFF":
            raise DataError("M12 HARM-to-SIN transition requires current HARM/OFF state")
        # The inherited set_function passes this object only to our recovery
        # override below.  Deliberately do not query or attempt to restore the
        # manual harmonic profile whose load semantics the plugin rejects.
        return SimpleNamespace(context=context)

    def _recover_harmonic_failure(self, *, snapshot: Any, original_error: Exception) -> None:
        if not self._m12_transition_authorized:
            return super()._recover_harmonic_failure(
                snapshot=snapshot,
                original_error=original_error,
            )
        self._configuration_writes_blocked = True
        try:
            self._force_output_off(snapshot.context.status.channel)
            observed = self.get_status(snapshot.context.status.channel)
            if observed.output != "OFF":
                raise InstrumentError("M12 HARM-to-SIN OFF recovery readback mismatch")
        except Exception as recovery_error:
            raise InstrumentError(
                "M12 HARM-to-SIN failed and source OFF recovery could not be verified"
            ) from recovery_error
        raise InstrumentError(
            "M12 HARM-to-SIN failed; output is confirmed OFF, old HARM settings were not restored, "
            "and configuration writes are blocked until a new driver session"
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
        if current.function != "HARM" or current.output != "OFF":
            raise DataError("M12 one-way transition requires current HARM/OFF state")
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
    if status.function != "HARM":
        failures.append("DG function is not HARM")
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
        raise HarmToSineError("M12 transition requires a max_source_vpp safety limit no greater than 0.5")

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
            raise HarmToSineError("configured source is not the reviewed DG4202 plugin driver")
        descriptor = resolve_instrument_descriptor("rigol.dg4202", expected_kind="source")
        driver = HarmOffToSineDriver(
            transport=base_driver.transport,
            check_errors_after_ops=base_driver.check_errors_after_ops,
        )
        source = SourceService(config=config, logger=logger, session=driver, descriptor=descriptor)
        before = source.channel_profile(1)
        failures = profile_failures(before)
        if failures:
            raise HarmToSineError("; ".join(failures))
        status = source.set_function(1, "SIN")
        after = source.channel_profile(1)
        if status.output != "OFF" or status.function != "SIN":
            raise HarmToSineError("SIN transition return value is not OFF/SIN")
        if after.status.output != "OFF" or after.status.function != "SIN":
            raise HarmToSineError("SIN transition final readback is not OFF/SIN")
        if after.load_ohm != 50.0:
            raise HarmToSineError("SIN transition changed the DG 50 ohm load")
        for field, tolerance in (("frequency_hz", 0.01), ("amplitude", 1.0e-6), ("offset_v", 1.0e-9)):
            if not math.isclose(
                float(getattr(after.status, field)),
                float(getattr(before.status, field)),
                rel_tol=0.0,
                abs_tol=tolerance,
            ):
                raise HarmToSineError(f"SIN transition unexpectedly changed DG {field}")
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
        "format": "CycleScope M12 HARM/OFF to SIN/OFF one-way transaction v1",
        "raw_scpi_entrypoint_used": False,
        "source_output_on_writes": 0,
        "dp800_writes": 0,
        "old_harmonic_profile_restored": False,
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
