"""Hash-bound, one-way WaveBench DG4202 USER/OFF to SIN/OFF transaction."""

from __future__ import annotations

import hashlib
import inspect
import math
from pathlib import Path
from typing import Any

from wavebench.errors import DataError, InstrumentError
from wavebench.instruments.registry import resolve_instrument_descriptor
from wavebench.logging import CommandLogger
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService
from wavebench.services.source_service import SourceService
from wavebench_rigol_dg4000.driver import DG4202Source


EXPECTED_DISTRIBUTION = "wavebench-rigol-dg4000"
EXPECTED_VERSION = "1.1.0"
EXPECTED_DRIVER_SHA256 = "aee3944d8ecb1ac69b305becf36a07f2c3dbca831c533a6ca2f9a8fc62585e70"
REQUIRED_CAPABILITIES = {
    "source.status",
    "source.channel_profile",
    "source.coupling_profile",
    "source.set_function",
    "source.output",
}
EXPECTED_PLAN_STEPS = [
    "source.status",
    "power.status",
    "source.output",
    "source.set_func",
    "source.set_vpp",
    "source.set_freq",
    "source.status",
    "power.status",
]


class UserToSineTransitionError(RuntimeError):
    """The installed driver, checked plan, or source state is ineligible."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_installed_driver() -> dict[str, Any]:
    descriptor = resolve_instrument_descriptor("rigol.dg4202", expected_kind="source")
    source = inspect.getsourcefile(DG4202Source)
    if source is None:
        raise UserToSineTransitionError("cannot locate the installed DG4202 driver source")
    path = Path(source).resolve()
    digest = sha256_file(path)
    failures: list[str] = []
    if descriptor.distribution != EXPECTED_DISTRIBUTION:
        failures.append("unexpected DG4202 plugin distribution")
    if descriptor.version != EXPECTED_VERSION:
        failures.append("unexpected DG4202 plugin version")
    if digest != EXPECTED_DRIVER_SHA256:
        failures.append("installed DG4202 driver SHA-256 changed")
    missing = sorted(REQUIRED_CAPABILITIES - set(descriptor.capabilities))
    if missing:
        failures.append("DG4202 plugin lacks capabilities: " + ", ".join(missing))
    if failures:
        raise UserToSineTransitionError("; ".join(failures))
    extension_path = Path(__file__).resolve()
    return {
        "distribution": descriptor.distribution,
        "version": descriptor.version,
        "driver_source": str(path),
        "driver_source_sha256": digest,
        "descriptor_origin": descriptor.origin,
        "required_capabilities": sorted(REQUIRED_CAPABILITIES),
        "extension_source": str(extension_path),
        "extension_source_sha256": sha256_file(extension_path),
        "extension_scope": (
            "authorize exactly one USER/OFF to SIN/OFF set_function transaction; "
            "do not restore the previous USER waveform; confirm or force output OFF "
            "and stop after any failure"
        ),
    }


def validate_checked_plan_binding(
    *,
    config: Any,
    plan_path: Path,
    checked_plan: dict[str, Any],
    frequency_hz: float,
    source_vpp_v: float,
) -> dict[str, Any]:
    """Bind the one-way exception to the already checked I configuration plan."""

    plan_path = plan_path.resolve()
    if not plan_path.is_file():
        raise UserToSineTransitionError("checked source configuration plan is missing")
    plan = load_run_plan(plan_path)
    kinds = [step.kind for step in plan.steps]
    failures: list[str] = []
    if kinds != EXPECTED_PLAN_STEPS:
        failures.append("checked plan step sequence changed")
    if Path(str(checked_plan.get("path", ""))).resolve() != plan_path:
        failures.append("checked plan path binding changed")
    if checked_plan.get("sha256") != sha256_file(plan_path):
        failures.append("checked plan SHA-256 binding changed")
    if checked_plan.get("steps") != kinds:
        failures.append("checked plan step record changed")
    if plan.restore.source_state:
        failures.append("one-way transition plan must not request source restoration")
    if plan.safety.allow_50ohm or plan.safety.scope_guard_channel != 1:
        failures.append("checked plan scope high-impedance guard changed")
    if any(step.kind in {"power.set", "power.output"} for step in plan.steps):
        failures.append("DP832 writes are forbidden")

    output_steps = [step for step in plan.steps if step.kind == "source.output"]
    function_steps = [step for step in plan.steps if step.kind == "source.set_func"]
    amplitude_steps = [step for step in plan.steps if step.kind == "source.set_vpp"]
    frequency_steps = [step for step in plan.steps if step.kind == "source.set_freq"]
    if len(output_steps) != 1 or output_steps[0].fields.get("state") != "off":
        failures.append("checked plan may contain only one source.output OFF")
    if len(function_steps) != 1 or str(function_steps[0].fields.get("function", "")).upper() != "SIN":
        failures.append("checked plan does not request exactly one SIN function")
    if len(amplitude_steps) != 1 or not math.isclose(
        float(amplitude_steps[0].fields.get("value_vpp", math.nan)),
        source_vpp_v,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        failures.append("checked plan source amplitude binding changed")
    if len(frequency_steps) != 1 or not math.isclose(
        float(frequency_steps[0].fields.get("frequency_hz", math.nan)),
        frequency_hz,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        failures.append("checked plan source frequency binding changed")
    if not 0.0 < source_vpp_v <= 0.2:
        failures.append("M11-I one-way transition amplitude must be within 0.2 Vpp")
    if not 4_000_000.0 <= frequency_hz <= 10_000_000.0:
        failures.append("M11-I one-way transition frequency is outside 4..10 MHz")
    if failures:
        raise UserToSineTransitionError("; ".join(failures))
    RunService(config=config, logger=CommandLogger()).check(plan)
    return {
        "path": str(plan_path),
        "sha256": sha256_file(plan_path),
        "steps": kinds,
        "wavebench_run_check": "pass",
        "requested_function": "SIN",
        "requested_frequency_hz": frequency_hz,
        "requested_source_vpp_v": source_vpp_v,
        "source_output_on_steps": 0,
        "dp832_write_steps": 0,
        "source_restoration_requested": False,
        "pass": True,
    }


class M11UserToSineDG4202Source(DG4202Source):
    """Narrowly permit the installed set_function transaction to leave USER/OFF."""

    _m11_user_to_sine_authorized = False

    def _snapshot_basic_status(self, channel: int):
        snapshot = self.get_status(channel)
        if snapshot.function == "USER":
            if not self._m11_user_to_sine_authorized:
                raise DataError("M11 USER snapshot is authorized only inside USER-to-SIN")
            if snapshot.output != "OFF":
                raise DataError("M11 USER-to-SIN requires USER output to be OFF")
            return snapshot
        return super()._snapshot_basic_status(channel)

    def _recover_configuration_failure(self, *, snapshot: Any, original_error: Exception) -> None:
        if snapshot.function != "USER" or not self._m11_user_to_sine_authorized:
            return super()._recover_configuration_failure(
                snapshot=snapshot,
                original_error=original_error,
            )
        self._configuration_writes_blocked = True
        try:
            self._force_output_off(snapshot.channel)
            recovered = self.get_status(snapshot.channel)
            if recovered.output != "OFF":
                raise InstrumentError("DG4000 USER-to-SIN OFF recovery readback mismatch")
        except Exception as recovery_error:
            raise InstrumentError(
                "DG4000 USER-to-SIN transaction failed and output OFF could not be verified; "
                "configuration writes are blocked"
            ) from recovery_error
        raise InstrumentError(
            "DG4000 USER-to-SIN transaction failed; output is confirmed OFF, the old USER "
            "waveform was not restored, and configuration writes are blocked"
        ) from original_error

    def set_function(
        self,
        channel: int,
        function: str,
        *,
        check_errors: bool | None = None,
    ):
        if function.strip().upper() != "SIN":
            raise DataError("M11 one-way USER transition permits only SIN")
        current = self.get_status(channel)
        if current.function != "USER" or current.output != "OFF":
            raise DataError("M11 one-way transition requires the current state USER/OFF")
        self._m11_user_to_sine_authorized = True
        try:
            return super().set_function(channel, function, check_errors=check_errors)
        finally:
            self._m11_user_to_sine_authorized = False


def _coupling_is_off(coupling: Any) -> bool:
    return not (
        coupling.frequency_enabled
        or coupling.phase_enabled
        or coupling.amplitude_enabled
    )


def _profile_failures(
    profile: Any,
    coupling: Any,
    *,
    expected_function: str,
    reference: Any | None = None,
) -> list[str]:
    status = profile.status
    failures: list[str] = []
    if status.function.upper() != expected_function:
        failures.append(f"DG function is not {expected_function}")
    if status.output != "OFF":
        failures.append("DG output is not OFF")
    if profile.load_ohm != 50.0:
        failures.append("DG load is not 50 ohm")
    if status.amplitude_unit != "VPP":
        failures.append("DG amplitude unit is not VPP")
    if not math.isclose(status.offset_v, 0.0, rel_tol=0.0, abs_tol=1e-9):
        failures.append("DG offset is not 0 V")
    if status.frequency_mode != "FIX" or status.sweep_enabled != "OFF":
        failures.append("DG is not FIX with sweep OFF")
    if profile.noise_enabled or profile.burst_enabled or profile.modulation_enabled:
        failures.append("DG noise/burst/modulation is not fully OFF")
    if not _coupling_is_off(coupling):
        failures.append("DG channel coupling is not fully OFF")
    if not math.isfinite(status.amplitude) or not 0.0 < status.amplitude <= 0.45:
        failures.append("DG existing amplitude is outside the 0..0.45 Vpp transition guard")
    if reference is not None:
        reference_status = reference.status
        for field, tolerance in (
            ("frequency_hz", 0.01),
            ("amplitude", 1e-6),
            ("offset_v", 1e-9),
            ("phase_deg", 1e-6),
        ):
            if not math.isclose(
                float(getattr(status, field)),
                float(getattr(reference_status, field)),
                rel_tol=0.0,
                abs_tol=tolerance,
            ):
                failures.append(f"DG {field} changed during USER-to-SIN")
        if status.amplitude_unit != reference_status.amplitude_unit:
            failures.append("DG amplitude unit changed during USER-to-SIN")
        if status.frequency_mode != reference_status.frequency_mode:
            failures.append("DG frequency mode changed during USER-to-SIN")
    return failures


def transition_user_off_to_sine(
    *,
    config: Any,
    logger: CommandLogger,
    plan_path: Path,
    checked_plan: dict[str, Any],
    frequency_hz: float,
    source_vpp_v: float,
) -> dict[str, Any]:
    """Perform one source-function write through the installed WaveBench driver."""

    audit = verify_installed_driver()
    plan_binding = validate_checked_plan_binding(
        config=config,
        plan_path=plan_path,
        checked_plan=checked_plan,
        frequency_hz=frequency_hz,
        source_vpp_v=source_vpp_v,
    )
    descriptor = resolve_instrument_descriptor("rigol.dg4202", expected_kind="source")
    base_service = SourceService(config=config, logger=logger)
    base_driver = None
    driver = None
    before = None
    before_coupling = None
    set_status = None
    after = None
    after_coupling = None
    recovery: dict[str, Any] | None = None
    failures: list[str] = []
    function_write_attempted = False
    try:
        base_driver = base_service.open_session()
        if not isinstance(base_driver, DG4202Source):
            failures.append("resolved source is not the hash-bound DG4202 driver")
        else:
            driver = M11UserToSineDG4202Source(
                transport=base_driver.transport,
                check_errors_after_ops=base_driver.check_errors_after_ops,
            )
            source = SourceService(
                config=config,
                logger=logger,
                session=driver,
                descriptor=descriptor,
            )
            before = source.channel_profile(1)
            before_coupling = source.coupling_profile()
            failures.extend(
                _profile_failures(
                    before,
                    before_coupling,
                    expected_function="USER",
                )
            )
            if not failures:
                function_write_attempted = True
                try:
                    set_status = source.set_function(1, "SIN")
                    after = source.channel_profile(1)
                    after_coupling = source.coupling_profile()
                    failures.extend(
                        _profile_failures(
                            after,
                            after_coupling,
                            expected_function="SIN",
                            reference=before,
                        )
                    )
                    if set_status.output != "OFF" or set_status.function != "SIN":
                        failures.append("DG set_function return status is not SIN/OFF")
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")

            if function_write_attempted and failures:
                try:
                    observed = source.status(1)
                    if observed.output == "OFF":
                        recovery = {
                            "off_write_performed": False,
                            "reason": "readback already confirmed output OFF",
                            "status": observed.as_dict(),
                            "pass": True,
                        }
                    else:
                        forced = source.set_output(1, False)
                        confirmed = source.status(1)
                        recovery = {
                            "off_write_performed": True,
                            "set_status": forced.as_dict(),
                            "status": confirmed.as_dict(),
                            "pass": confirmed.output == "OFF",
                        }
                        if confirmed.output != "OFF":
                            failures.append("DG output OFF recovery readback failed")
                except Exception as error:
                    recovery = {
                        "off_write_performed": None,
                        "pass": False,
                        "error": f"{type(error).__name__}: {error}",
                    }
                    failures.append("DG output OFF recovery could not be verified")
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
    finally:
        try:
            if driver is not None:
                driver.close()
            elif base_driver is not None:
                base_driver.close()
        except Exception as error:
            failures.append(f"source close: {type(error).__name__}: {error}")

    return {
        "format": "CycleScope M11 DG USER/OFF to SIN/OFF one-way transaction v1",
        "audit": audit,
        "checked_plan": plan_binding,
        "before": None if before is None else before.as_dict(),
        "before_coupling": (
            None if before_coupling is None else before_coupling.as_dict()
        ),
        "set_function_status": None if set_status is None else set_status.as_dict(),
        "after": None if after is None else after.as_dict(),
        "after_coupling": None if after_coupling is None else after_coupling.as_dict(),
        "recovery": recovery,
        "function_write_attempted": function_write_attempted,
        "source_output_on_writes": 0,
        "dp832_writes": 0,
        "raw_scpi_entrypoint_used": False,
        "old_volatile_user_waveform_restored": False,
        "restoration_boundary": (
            "output OFF only; the prior volatile USER waveform is neither restored nor "
            "required by the active user waiver"
        ),
        "failures": failures,
        "pass": not failures,
    }
