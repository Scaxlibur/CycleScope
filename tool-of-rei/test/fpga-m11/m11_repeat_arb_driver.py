"""Hash-bound WaveBench DG4202 extension for repeated OFF/USER ARB uploads."""

from __future__ import annotations

import hashlib
import inspect
import math
from pathlib import Path
from typing import Any

from wavebench.errors import DataError
from wavebench.instruments.registry import resolve_instrument_descriptor
from wavebench.logging import CommandLogger
from wavebench.services.source_service import SourceService
from wavebench_rigol_dg4000.driver import DG4202Source


EXPECTED_DISTRIBUTION = "wavebench-rigol-dg4000"
EXPECTED_VERSION = "1.1.0"
EXPECTED_DRIVER_SHA256 = "aee3944d8ecb1ac69b305becf36a07f2c3dbca831c533a6ca2f9a8fc62585e70"


class RepeatArbDriverError(RuntimeError):
    """The installed WaveBench driver or live source state is not eligible."""


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
        raise RepeatArbDriverError("cannot locate the installed DG4202 driver source")
    path = Path(source).resolve()
    digest = sha256_file(path)
    failures: list[str] = []
    if descriptor.distribution != EXPECTED_DISTRIBUTION:
        failures.append("unexpected DG4202 plugin distribution")
    if descriptor.version != EXPECTED_VERSION:
        failures.append("unexpected DG4202 plugin version")
    if digest != EXPECTED_DRIVER_SHA256:
        failures.append("installed DG4202 driver SHA-256 changed")
    if "source.arbitrary_upload" not in descriptor.capabilities:
        failures.append("DG4202 plugin lacks source.arbitrary_upload")
    if failures:
        raise RepeatArbDriverError("; ".join(failures))
    return {
        "distribution": descriptor.distribution,
        "version": descriptor.version,
        "driver_source": str(path),
        "driver_source_sha256": digest,
        "descriptor_origin": descriptor.origin,
        "capability": "source.arbitrary_upload",
        "extension_scope": (
            "allow the existing upload transaction to snapshot USER only when output is "
            "already OFF; all encoding, writes, readbacks, error recovery and transport "
            "remain the installed WaveBench implementation"
        ),
    }


class M11RepeatArbDG4202Source(DG4202Source):
    """Permit the installed upload transaction to snapshot a verified OFF/USER state."""

    def _snapshot_basic_status(self, channel: int):
        snapshot = self.get_status(channel)
        if snapshot.function == "USER":
            if snapshot.output != "OFF":
                raise DataError("M11 repeated ARB upload requires USER output to be OFF")
            return snapshot
        return super()._snapshot_basic_status(channel)


def upload_repeated_arb(
    *,
    config: Any,
    logger: CommandLogger,
    waveform: Path,
    playback_frequency_hz: float,
    amplitude_vpp: float,
    points: int,
) -> dict[str, Any]:
    audit = verify_installed_driver()
    if not waveform.is_file() or not 0.0 < amplitude_vpp <= 0.45:
        raise RepeatArbDriverError("repeat ARB waveform/amplitude guard failed")
    if not math.isfinite(playback_frequency_hz) or playback_frequency_hz <= 0.0:
        raise RepeatArbDriverError("repeat ARB playback frequency guard failed")
    if not 2 <= points <= 16_384:
        raise RepeatArbDriverError("repeat ARB point-count guard failed")

    base_service = SourceService(config=config, logger=logger)
    base_driver = base_service.open_session()
    if not isinstance(base_driver, DG4202Source):
        base_driver.close()
        raise RepeatArbDriverError("resolved source is not the hash-bound DG4202 driver")
    driver = M11RepeatArbDG4202Source(
        transport=base_driver.transport,
        check_errors_after_ops=base_driver.check_errors_after_ops,
    )
    descriptor = resolve_instrument_descriptor("rigol.dg4202", expected_kind="source")
    try:
        service = SourceService(
            config=config,
            logger=logger,
            session=driver,
            descriptor=descriptor,
        )
        before = service.channel_profile(1)
        if (
            before.status.function != "USER"
            or before.status.output != "OFF"
            or before.load_ohm != 50.0
            or not math.isclose(before.status.offset_v, 0.0, abs_tol=1e-9)
        ):
            raise RepeatArbDriverError(
                "repeat ARB extension requires USER/OFF/50-ohm/0-V before upload"
            )
        status = service.upload_arbitrary_waveform(
            channel=1,
            file_path=str(waveform.resolve()),
            playback_frequency_hz=playback_frequency_hz,
            amplitude_vpp=amplitude_vpp,
            offset_v=0.0,
            max_points=points,
            byte_order="little",
            output_on=False,
        )
        after = service.channel_profile(1)
        if (
            status.function != "USER"
            or status.output != "OFF"
            or after.status.function != "USER"
            or after.status.output != "OFF"
            or after.load_ohm != 50.0
            or not math.isclose(status.frequency_hz, playback_frequency_hz, abs_tol=0.01)
            or not math.isclose(status.amplitude, amplitude_vpp, abs_tol=1e-6)
            or not math.isclose(status.offset_v, 0.0, abs_tol=1e-9)
        ):
            raise RepeatArbDriverError("repeat ARB final USER/OFF readback mismatch")
        return {
            "audit": audit,
            "before": before.as_dict(),
            "status": status.as_dict(),
            "after": after.as_dict(),
            "waveform": str(waveform.resolve()),
            "waveform_sha256": sha256_file(waveform),
            "playback_frequency_hz": playback_frequency_hz,
            "amplitude_vpp": amplitude_vpp,
            "points": points,
            "output_on": False,
            "previous_volatile_waveform_recoverable_after_failed_binary_write": False,
            "failure_policy": (
                "the installed WaveBench arbitrary-upload recovery forces output OFF, "
                "restores the USER context it can verify, reports that old volatile data "
                "cannot be restored, and blocks further writes in that session"
            ),
        }
    finally:
        driver.close()
