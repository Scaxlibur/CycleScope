#!/usr/bin/env python3
"""Inject valid WAVE_DATA before ENABLE_PUSH_ACK, then one frame after ACK."""

from __future__ import annotations

from pathlib import Path
import secrets
import struct
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ESP32-P4" / "tools"))

import cslp_fpga_emulator as cslp  # noqa: E402


def main() -> int:
    frame_samples = cslp.synthesize_multitone(
        cslp.DEFAULT_FUNDAMENTAL_HZ,
        cslp.DEFAULT_TONES,
        cslp.DEFAULT_SCALE_UV_PER_LSB,
        cslp.DEFAULT_OFFSET_UV,
    )
    emulator = cslp.CslpFpgaEmulator(
        "192.168.10.5",
        50000,
        "192.168.10.3",
        50001,
        frame_samples,
        cslp.DEFAULT_SCALE_UV_PER_LSB,
        cslp.DEFAULT_OFFSET_UV,
        cslp.DEFAULT_CALIBRATION_ID,
    )
    wave_sequence = secrets.randbits(32)
    packets_sent = 0
    injected = False

    def send_frame(frame_id: int, label: str) -> None:
        nonlocal wave_sequence, packets_sent
        frame_timestamp_us = cslp.monotonic_us(emulator.boot_start_ns)
        for chunk_index in range(cslp.CHUNK_COUNT):
            wave_sequence = (wave_sequence + 1) & 0xFFFFFFFF
            packet = cslp.build_wave_packet(
                emulator.session_id,
                wave_sequence,
                frame_id,
                frame_timestamp_us,
                chunk_index,
                emulator.config_id,
                emulator.frame_samples,
                emulator.scale_uv_per_lsb,
                emulator.offset_uv,
                emulator.calibration_id,
            )
            emulator.socket.sendto(packet, emulator.peer_address)
            packets_sent += 1
            if chunk_index + 1 < cslp.CHUNK_COUNT:
                time.sleep(500 / 1_000_000)
        print(
            f"{label} frame={frame_id} packets_total={packets_sent}",
            flush=True,
        )

    original_send_response = emulator.send_response

    def send_response_with_pre_enable_frames(
        request: cslp.Message,
        response_type: int,
        payload: bytes,
    ) -> None:
        nonlocal injected
        if response_type == cslp.ENABLE_PUSH_ACK and not injected:
            status, reserved = struct.unpack("!HH", payload)
            if status != cslp.STATUS_OK or reserved != 0:
                raise RuntimeError("ENABLE_PUSH_ACK was not the expected successful ACK")
            print(
                "injecting 8 complete WAVE frames before ENABLE_PUSH_ACK",
                flush=True,
            )
            injection_started = time.monotonic()
            for frame_id in range(1, 9):
                send_frame(frame_id, "pre-enable")
                if frame_id < 8:
                    time.sleep(0.001)
            injected = True
            elapsed_ms = (time.monotonic() - injection_started) * 1000.0
            print(
                f"sending delayed ENABLE_PUSH_ACK after {elapsed_ms:.3f} ms",
                flush=True,
            )
        original_send_response(request, response_type, payload)

    emulator.send_response = send_response_with_pre_enable_frames
    print(
        "listening on 192.168.10.5:50000; expecting 192.168.10.3:50001",
        flush=True,
    )
    try:
        emulator.handshake(15.0)
        if not injected:
            raise RuntimeError("pre-enable frame injection did not run")
        time.sleep(0.2)
        send_frame(9, "post-enable")

        status_sequence = secrets.randbits(32)
        hold_deadline = time.monotonic() + 35.0
        while time.monotonic() < hold_deadline:
            status_sequence = (status_sequence + 1) & 0xFFFFFFFF
            emulator.send_status(status_sequence, 9, 9, packets_sent)
            time.sleep(min(0.5, max(0.0, hold_deadline - time.monotonic())))
        print(
            f"completed pre_enable_frames=8 post_enable_frames=1 "
            f"wave_packets={packets_sent}",
            flush=True,
        )
    finally:
        emulator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
