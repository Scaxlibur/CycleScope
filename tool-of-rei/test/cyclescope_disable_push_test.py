#!/usr/bin/env python3
"""Exercise an exact DISABLE -> CONFIG -> ENABLE lifecycle against ESP32-P4."""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import struct
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ESP32-P4" / "tools"))

import cslp_fpga_emulator as cslp  # noqa: E402


BIND_IP = "192.168.10.5"
BIND_PORT = 50000
PEER_IP = "192.168.10.3"
PEER_PORT = 50001
CHUNK_GAP_US = 500
CONTROL_TIMEOUT_SECONDS = 5.0
HOLD_SECONDS = 35.0
OVERLAP_ACK_DELAY_SECONDS = 0.005
OVERLAP_POST_ENABLE_FRAMES = 600


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("retry", "overlap"),
        default="retry",
        help=(
            "retry checks byte-identical DISABLE retries; overlap holds an "
            "old-config FFT across a fast same-session reconfiguration"
        ),
    )
    args = parser.parse_args()

    cslp.self_test()
    frame_samples = cslp.synthesize_multitone(
        cslp.DEFAULT_FUNDAMENTAL_HZ,
        cslp.DEFAULT_TONES,
        cslp.DEFAULT_SCALE_UV_PER_LSB,
        cslp.DEFAULT_OFFSET_UV,
    )
    emulator = cslp.CslpFpgaEmulator(
        BIND_IP,
        BIND_PORT,
        PEER_IP,
        PEER_PORT,
        frame_samples,
        cslp.DEFAULT_SCALE_UV_PER_LSB,
        cslp.DEFAULT_OFFSET_UV,
        cslp.DEFAULT_CALIBRATION_ID,
    )
    wave_sequence = secrets.randbits(32)
    status_sequence = secrets.randbits(32)
    wave_packets_sent = 0
    source_frames_sent = 0
    next_frame_start = time.monotonic()

    def send_frame(
        frame_id: int,
        config_id: int,
        label: str,
        *,
        count_as_source_frame: bool = True,
    ) -> None:
        nonlocal wave_sequence
        nonlocal wave_packets_sent
        nonlocal source_frames_sent
        nonlocal next_frame_start

        now = time.monotonic()
        if now < next_frame_start:
            time.sleep(next_frame_start - now)
        frame_start = time.monotonic()
        frame_timestamp_us = cslp.monotonic_us(emulator.boot_start_ns)
        for chunk_index in range(cslp.CHUNK_COUNT):
            wave_sequence = (wave_sequence + 1) & 0xFFFFFFFF
            packet = cslp.build_wave_packet(
                emulator.session_id,
                wave_sequence,
                frame_id,
                frame_timestamp_us,
                chunk_index,
                config_id,
                emulator.frame_samples,
                emulator.scale_uv_per_lsb,
                emulator.offset_uv,
                emulator.calibration_id,
            )
            emulator.socket.sendto(packet, emulator.peer_address)
            wave_packets_sent += 1
            if chunk_index + 1 < cslp.CHUNK_COUNT:
                time.sleep(CHUNK_GAP_US / 1_000_000)
        if count_as_source_frame:
            source_frames_sent += 1
        next_frame_start = frame_start + cslp.FRAME_PERIOD_US / 1_000_000
        print(
            f"{label}: frame={frame_id} config=0x{config_id:08X} "
            f"wave_packets={wave_packets_sent}",
            flush=True,
        )

    def receive_expected_control(
        expected_type: int,
        timeout_seconds: float = CONTROL_TIMEOUT_SECONDS,
    ) -> tuple[bytes, cslp.Message]:
        packet, request, address = emulator.receive_request(
            time.monotonic() + timeout_seconds
        )
        emulator.peer_address = address
        if request.session_id != emulator.session_id:
            raise RuntimeError(
                f"control request changed session: expected "
                f"0x{emulator.session_id:08X}, got 0x{request.session_id:08X}"
            )
        if request.message_type != expected_type:
            raise RuntimeError(
                f"expected control type 0x{expected_type:02X}, got "
                f"0x{request.message_type:02X}"
            )
        return packet, request

    print(
        f"listening on {BIND_IP}:{BIND_PORT}; expecting {PEER_IP}:{PEER_PORT}; "
        f"mode={args.mode}",
        flush=True,
    )
    try:
        emulator.handshake(15.0)
        if emulator.device_state != cslp.DEVICE_STATE_PUSH_ENABLED:
            raise RuntimeError("initial handshake did not enter PUSH_ENABLED")
        session_id = emulator.session_id
        old_config_id = emulator.config_id

        for frame_id in range(1, 9):
            send_frame(frame_id, old_config_id, "pre-disable")

        first_disable_packet, first_disable = receive_expected_control(
            cslp.DISABLE_PUSH
        )
        if first_disable.payload_bytes != 0 or first_disable.payload:
            raise RuntimeError("DISABLE_PUSH request did not have an empty payload")
        print(
            f"DISABLE_PUSH first request: session=0x{session_id:08X} "
            f"seq={first_disable.message_seq}; injecting frame 9 before ACK",
            flush=True,
        )

        send_frame(9, old_config_id, "pre-ACK drain")
        if args.mode == "retry":
            if not emulator.handle_control_request(
                first_disable,
                transmit_response=False,
            ):
                raise RuntimeError("first DISABLE_PUSH request was not handled")
            if emulator.device_state != cslp.DEVICE_STATE_READY:
                raise RuntimeError("DISABLE_PUSH did not move the emulator to READY")
            cached_disable_ack = emulator.response_cache[
                (session_id, cslp.DISABLE_PUSH, first_disable.message_seq)
            ][1]
            parsed_disable_ack = cslp.parse_message(cached_disable_ack)
            if (
                parsed_disable_ack.message_type != cslp.DISABLE_PUSH_ACK
                or struct.unpack("!HH", parsed_disable_ack.payload)
                != (cslp.STATUS_OK, 0)
            ):
                raise RuntimeError("cached DISABLE_PUSH_ACK was malformed")
            print("intentionally dropped first DISABLE_PUSH_ACK", flush=True)

            retry_packet, retry_disable = receive_expected_control(
                cslp.DISABLE_PUSH
            )
            if retry_packet != first_disable_packet:
                raise RuntimeError("DISABLE_PUSH retry was not byte-identical")
            if retry_disable.message_seq != first_disable.message_seq:
                raise RuntimeError("DISABLE_PUSH retry changed message_seq")
            if not emulator.handle_control_request(retry_disable):
                raise RuntimeError("DISABLE_PUSH retry was not handled")
            if emulator.device_state != cslp.DEVICE_STATE_READY:
                raise RuntimeError("cached DISABLE_PUSH retry changed READY state")
            print(
                "replayed byte-identical DISABLE_PUSH_ACK "
                f"seq={retry_disable.message_seq}",
                flush=True,
            )

            send_frame(
                10,
                old_config_id,
                "post-ACK old-config probe",
                count_as_source_frame=False,
            )
        else:
            time.sleep(OVERLAP_ACK_DELAY_SECONDS)
            if not emulator.handle_control_request(first_disable):
                raise RuntimeError("fast DISABLE_PUSH request was not handled")
            if emulator.device_state != cslp.DEVICE_STATE_READY:
                raise RuntimeError("DISABLE_PUSH did not move the emulator to READY")
            print(
                "sent first DISABLE_PUSH_ACK after 5 ms analysis overlap window",
                flush=True,
            )

        _config_packet, config_request = receive_expected_control(cslp.CONFIG_SET)
        if config_request.payload_bytes != 20:
            raise RuntimeError("post-DISABLE CONFIG_SET had the wrong payload size")
        if struct.unpack("!IIIBBHI", config_request.payload) != cslp.PROFILE_CONFIG:
            raise RuntimeError("post-DISABLE CONFIG_SET changed the frozen profile")
        if not emulator.handle_control_request(config_request):
            raise RuntimeError("post-DISABLE CONFIG_SET was not handled")
        new_config_id = emulator.config_id
        if new_config_id == 0 or new_config_id == old_config_id:
            raise RuntimeError("reconfiguration did not allocate a new config_id")

        _enable_packet, enable_request = receive_expected_control(cslp.ENABLE_PUSH)
        if enable_request.payload_bytes != 0 or enable_request.payload:
            raise RuntimeError("post-CONFIG ENABLE_PUSH did not have an empty payload")
        if not emulator.handle_control_request(enable_request):
            raise RuntimeError("post-CONFIG ENABLE_PUSH was not handled")
        if emulator.device_state != cslp.DEVICE_STATE_PUSH_ENABLED:
            raise RuntimeError("reconfiguration did not restore PUSH_ENABLED")
        if emulator.session_id != session_id:
            raise RuntimeError("DISABLE lifecycle unexpectedly changed session_id")

        post_enable_start = 11 if args.mode == "retry" else 10
        post_enable_count = (
            8 if args.mode == "retry" else OVERLAP_POST_ENABLE_FRAMES
        )
        for frame_id in range(
            post_enable_start,
            post_enable_start + post_enable_count,
        ):
            send_frame(frame_id, new_config_id, "post-enable")

        expected_wave_frames = (
            18 if args.mode == "retry" else 9 + post_enable_count
        )
        expected_wave_packets = expected_wave_frames * cslp.CHUNK_COUNT
        expected_source_frames = 9 + post_enable_count
        if wave_packets_sent != expected_wave_packets:
            raise RuntimeError(
                f"expected {expected_wave_packets} WAVE packets, sent "
                f"{wave_packets_sent}"
            )
        if source_frames_sent != expected_source_frames:
            raise RuntimeError(
                f"expected {expected_source_frames} source frames, counted "
                f"{source_frames_sent}"
            )

        last_frame_id = post_enable_start + post_enable_count - 1
        hold_seconds = HOLD_SECONDS if args.mode == "retry" else 3.0
        hold_deadline = time.monotonic() + hold_seconds
        statuses_sent = 0
        while time.monotonic() < hold_deadline:
            status_sequence = (status_sequence + 1) & 0xFFFFFFFF
            emulator.send_status(
                status_sequence,
                last_frame_id,
                source_frames_sent,
                wave_packets_sent,
            )
            statuses_sent += 1
            time.sleep(min(0.5, max(0.0, hold_deadline - time.monotonic())))

        print(
            "completed DISABLE lifecycle: "
            f"mode={args.mode} "
            f"disable_requests={2 if args.mode == 'retry' else 1} "
            f"first_ack_dropped={1 if args.mode == 'retry' else 0} "
            f"byte_identical_retry={1 if args.mode == 'retry' else 0} "
            f"session=0x{session_id:08X} "
            f"config=0x{old_config_id:08X}->0x{new_config_id:08X} "
            "pre_ack_frames=9 "
            f"post_ack_old_config_probes={1 if args.mode == 'retry' else 0} "
            f"post_enable_frames={post_enable_count} "
            f"source_frames={source_frames_sent} "
            f"expected_p4_completed={expected_source_frames} "
            f"expected_min_overlap_stale={1 if args.mode == 'overlap' else 0} "
            f"queued_stale_filter_expected={1 if args.mode == 'overlap' else 0} "
            f"wave_packets={wave_packets_sent} "
            f"statuses={statuses_sent}",
            flush=True,
        )
    finally:
        emulator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
