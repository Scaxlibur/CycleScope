#!/usr/bin/env python3
"""Drive RT1-RT4 recovery sessions for the ESP32-P4 private fault image."""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import select
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ESP32-P4" / "tools"))

import cslp_fpga_emulator as cslp  # noqa: E402


REPEATED_FATAL_CYCLES = 32
RECOVERY_FRAMES = 100


class RuntimeFaultDriver:
    def __init__(self, emulator: cslp.CslpFpgaEmulator, chunk_gap_us: int) -> None:
        self.emulator = emulator
        self.chunk_gap_us = chunk_gap_us
        self.wave_sequence = secrets.randbits(32)
        self.status_sequence = secrets.randbits(32)
        self.frames_sent = 0
        self.packets_sent = 0

    def service_control(self, timeout_seconds: float) -> bool:
        readable, _, _ = select.select(
            [self.emulator.socket], [], [], max(0.0, timeout_seconds)
        )
        if not readable:
            return False
        packet, address = self.emulator.socket.recvfrom(
            cslp.MAX_UDP_PAYLOAD_BYTES + 1
        )
        if address != self.emulator.expected_peer:
            print(
                f"ignored unexpected source {address[0]}:{address[1]}",
                flush=True,
            )
            return True
        try:
            request = cslp.parse_message(packet)
        except ValueError as error:
            print(f"ignored invalid control request: {error}", flush=True)
            return True
        self.emulator.peer_address = address
        if not self.emulator.handle_control_request(request):
            print(
                f"ignored unsupported control type=0x{request.message_type:02X}",
                flush=True,
            )
        return True

    def send_frame(
        self,
        session_id: int,
        config_id: int,
        frame_id: int,
        *,
        label: str,
    ) -> None:
        frame_timestamp_us = cslp.monotonic_us(self.emulator.boot_start_ns)
        for chunk_index in range(cslp.CHUNK_COUNT):
            self.wave_sequence = (self.wave_sequence + 1) & 0xFFFFFFFF
            packet = cslp.build_wave_packet(
                session_id,
                self.wave_sequence,
                frame_id,
                frame_timestamp_us,
                chunk_index,
                config_id,
                self.emulator.frame_samples,
                self.emulator.scale_uv_per_lsb,
                self.emulator.offset_uv,
                self.emulator.calibration_id,
            )
            self.emulator.socket.sendto(packet, self.emulator.peer_address)
            self.packets_sent += 1
            if chunk_index + 1 < cslp.CHUNK_COUNT:
                time.sleep(self.chunk_gap_us / 1_000_000)
        self.frames_sent += 1
        if frame_id == 1 or frame_id % 25 == 0:
            print(
                f"{label}: frame={frame_id} packets_total={self.packets_sent}",
                flush=True,
            )

    def send_status(self) -> None:
        self.status_sequence = (self.status_sequence + 1) & 0xFFFFFFFF
        self.emulator.send_status(
            self.status_sequence,
            self.frames_sent,
            self.frames_sent,
            self.packets_sent,
        )

    def wait_for_new_session(
        self,
        old_session: int,
        timeout_seconds: float,
        *,
        stream_old_frames: bool,
        old_config: int = 0,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + timeout_seconds
        next_frame = time.monotonic()
        next_status = time.monotonic() + 0.8
        old_frame_id = 1
        while time.monotonic() < deadline:
            if (
                self.emulator.session_id != old_session
                and self.emulator.device_state == cslp.DEVICE_STATE_PUSH_ENABLED
            ):
                return self.emulator.session_id, self.emulator.config_id

            now = time.monotonic()
            if (
                stream_old_frames
                and self.emulator.session_id == old_session
                and now >= next_frame
            ):
                self.send_frame(
                    old_session,
                    old_config,
                    old_frame_id,
                    label="S1 old stream",
                )
                old_frame_id += 1
                next_frame = now + cslp.FRAME_PERIOD_US / 1_000_000
            if now >= next_status and self.emulator.session_id == old_session:
                self.send_status()
                next_status = now + 0.8

            wake_at = min(next_frame if stream_old_frames else deadline, next_status)
            self.service_control(min(0.02, max(0.0, wake_at - time.monotonic())))
        raise TimeoutError(
            f"timed out waiting for a session after 0x{old_session:08X}"
        )

    def send_recovery_frames(self, session_id: int, config_id: int, label: str) -> None:
        next_frame = time.monotonic()
        for frame_id in range(1, RECOVERY_FRAMES + 1):
            now = time.monotonic()
            if now < next_frame:
                time.sleep(next_frame - now)
            frame_started = time.monotonic()
            if (
                self.emulator.session_id != session_id
                or self.emulator.config_id != config_id
                or self.emulator.device_state != cslp.DEVICE_STATE_PUSH_ENABLED
            ):
                raise RuntimeError(f"{label} lost its active session")
            self.send_frame(
                session_id,
                config_id,
                frame_id,
                label=label,
            )
            next_frame = frame_started + cslp.FRAME_PERIOD_US / 1_000_000

    def hold_active(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        next_status = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_status:
                self.send_status()
                next_status = now + 0.8
            self.service_control(min(0.02, max(0.0, deadline - now)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-ip", default="192.168.10.5")
    parser.add_argument("--port", type=int, default=50000)
    parser.add_argument("--peer-ip", default="192.168.10.3")
    parser.add_argument("--peer-port", type=int, default=50001)
    parser.add_argument("--chunk-gap-us", type=int, default=150)
    # Serial access can wait behind automatic approval while the board remains
    # in the bootloader. Keep the pre-listener alive across that control-plane
    # delay; active-session handshakes still complete in milliseconds.
    parser.add_argument("--handshake-timeout", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.chunk_gap_us < 0 or args.handshake_timeout <= 0:
        raise SystemExit("chunk gap must be non-negative and timeout must be positive")
    cslp.self_test()
    frame_samples = cslp.synthesize_multitone(
        cslp.DEFAULT_FUNDAMENTAL_HZ,
        cslp.DEFAULT_TONES,
        cslp.DEFAULT_SCALE_UV_PER_LSB,
        cslp.DEFAULT_OFFSET_UV,
    )
    emulator = cslp.CslpFpgaEmulator(
        args.bind_ip,
        args.port,
        args.peer_ip,
        args.peer_port,
        frame_samples,
        cslp.DEFAULT_SCALE_UV_PER_LSB,
        cslp.DEFAULT_OFFSET_UV,
        cslp.DEFAULT_CALIBRATION_ID,
    )
    driver = RuntimeFaultDriver(emulator, args.chunk_gap_us)
    print(
        f"runtime-fault fixture listening on {args.bind_ip}:{args.port}; "
        f"expecting {args.peer_ip}:{args.peer_port}",
        flush=True,
    )
    try:
        emulator.handshake(args.handshake_timeout)
        s1_session = emulator.session_id
        s1_config = emulator.config_id
        print(
            f"S1 ready: session=0x{s1_session:08X} config=0x{s1_config:08X}",
            flush=True,
        )

        s2_session, s2_config = driver.wait_for_new_session(
            s1_session,
            args.handshake_timeout,
            stream_old_frames=True,
            old_config=s1_config,
        )
        print(
            f"S2 recovered: session=0x{s2_session:08X} config=0x{s2_config:08X}",
            flush=True,
        )

        # Give the board time to capture its post-handshake bad-session baseline,
        # then inject exactly one complete S1 frame into S2.
        time.sleep(0.6)
        driver.send_frame(
            s1_session,
            s1_config,
            0x40000001,
            label="explicit old-S1 rejection",
        )
        print("explicit old-S1 frame sent: packets=12", flush=True)
        time.sleep(0.4)
        driver.send_recovery_frames(s2_session, s2_config, "S2 recovery")
        print("S2 recovery frames sent: 100", flush=True)

        current_session = s2_session
        current_config = s2_config
        for cycle in range(1, REPEATED_FATAL_CYCLES + 1):
            current_session, current_config = driver.wait_for_new_session(
                current_session,
                args.handshake_timeout,
                stream_old_frames=False,
            )
            if cycle == 1 or cycle % 8 == 0 or cycle == REPEATED_FATAL_CYCLES:
                print(
                    f"repeat recovery {cycle}/{REPEATED_FATAL_CYCLES}: "
                    f"session=0x{current_session:08X}",
                    flush=True,
                )

        time.sleep(0.6)
        driver.send_recovery_frames(
            current_session,
            current_config,
            "final recovery",
        )
        print("final recovery frames sent: 100", flush=True)
        driver.hold_active(5.0)
        print(
            f"runtime-fault fixture PASS: sessions={REPEATED_FATAL_CYCLES + 2} "
            f"repeated_fatal={REPEATED_FATAL_CYCLES} "
            f"packets_sent={driver.packets_sent}",
            flush=True,
        )
    finally:
        emulator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
