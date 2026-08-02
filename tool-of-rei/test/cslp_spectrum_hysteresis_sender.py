#!/usr/bin/env python3
"""Send the fixed 80-frame spectrum-scale hysteresis schedule to ESP32-P4.

The fixture owns only the test schedule and timing.  CSLP synthesis, packet
serialization, control handshake, and status messages are imported from the
project emulator so this file cannot silently grow a second protocol
implementation.

``--self-test-only`` is deliberately offline: it validates every generated
frame and packet without constructing ``CslpFpgaEmulator`` (and therefore
without creating or binding a socket).
"""

from __future__ import annotations

import argparse
import secrets
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ESP32-P4" / "tools"))

import cslp_fpga_emulator as cslp  # noqa: E402


BIND_IP = "192.168.10.5"
BIND_PORT = 50_000
PEER_IP = "192.168.10.3"
PEER_PORT = 50_001

FRAME_COUNT = 80
CHUNK_GAP_US = 250
HOLD_SECONDS = 2.0
HANDSHAKE_TIMEOUT_SECONDS = 15.0

FUNDAMENTAL_HZ = 40_750.0
SCALE_UV_PER_LSB = 100
OFFSET_UV = 500
CALIBRATION_ID = 1

H1_AMPLITUDE_VOLTS_PEAK = 0.025
H1_PHASE_RADIANS = 0.17
H4_AMPLITUDE_VOLTS_PEAK = 0.010
H4_PHASE_RADIANS = -0.51
H3_PHASE_RADIANS = 0.92

SCALE_TIERS_MVPK = (20.0, 50.0, 100.0, 200.0, 300.0, 500.0)
INITIAL_HEADROOM = 1.20
UPSHIFT_TRIGGER = 1.15
DOWNSHIFT_HEADROOM = 1.25


@dataclass(frozen=True)
class Stage:
    name: str
    first_frame: int
    last_frame: int
    h3_amplitudes_mvpk: tuple[float, ...]
    expected_scale_mvpk: float


STAGES = (
    Stage("threshold-alternation", 1, 40, (83.2, 83.5), 100.0),
    Stage("upshift", 41, 50, (90.0,), 200.0),
    Stage("upper-hold", 51, 60, (83.5,), 200.0),
    Stage("downshift", 61, 80, (79.0,), 100.0),
)


def h3_amplitude_mvpk(frame_id: int) -> float:
    """Return the prescribed H3 amplitude for one 1-based frame ID."""
    if 1 <= frame_id <= 40:
        return 83.2 if frame_id % 2 == 1 else 83.5
    if 41 <= frame_id <= 50:
        return 90.0
    if 51 <= frame_id <= 60:
        return 83.5
    if 61 <= frame_id <= FRAME_COUNT:
        return 79.0
    raise ValueError(f"frame_id must be in 1..{FRAME_COUNT}")


def tones_for_h3(h3_mvpk: float) -> tuple[tuple[int, float, float], ...]:
    return (
        (1, H1_AMPLITUDE_VOLTS_PEAK, H1_PHASE_RADIANS),
        (3, h3_mvpk / 1000.0, H3_PHASE_RADIANS),
        (4, H4_AMPLITUDE_VOLTS_PEAK, H4_PHASE_RADIANS),
    )


def select_scale_tier(required_mvpk: float) -> float:
    for tier in SCALE_TIERS_MVPK:
        if tier >= required_mvpk:
            return tier
    raise ValueError(f"required spectrum scale exceeds the fixture tiers: {required_mvpk}")


def stateless_scale_mvpk(maximum_mvpk: float) -> float:
    return select_scale_tier(maximum_mvpk * INITIAL_HEADROOM)


def hysteretic_scale_mvpk(maximum_mvpk: float, previous_mvpk: float) -> float:
    if previous_mvpk == 0.0:
        return stateless_scale_mvpk(maximum_mvpk)
    if maximum_mvpk * UPSHIFT_TRIGGER > previous_mvpk:
        return stateless_scale_mvpk(maximum_mvpk)
    downshift_candidate = select_scale_tier(
        maximum_mvpk * DOWNSHIFT_HEADROOM
    )
    return min(previous_mvpk, downshift_candidate)


def collapsed(values: tuple[float, ...]) -> tuple[float, ...]:
    result: list[float] = []
    for value in values:
        if not result or value != result[-1]:
            result.append(value)
    return tuple(result)


def build_sample_variants() -> dict[float, tuple[int, ...]]:
    variants: dict[float, tuple[int, ...]] = {}
    for h3_mvpk in (83.2, 83.5, 90.0, 79.0):
        variants[h3_mvpk] = cslp.synthesize_multitone(
            FUNDAMENTAL_HZ,
            tones_for_h3(h3_mvpk),
            SCALE_UV_PER_LSB,
            OFFSET_UV,
        )
    return variants


def validate_fixture_profile() -> None:
    expected_stages = (
        ("threshold-alternation", 1, 40, (83.2, 83.5), 100.0),
        ("upshift", 41, 50, (90.0,), 200.0),
        ("upper-hold", 51, 60, (83.5,), 200.0),
        ("downshift", 61, 80, (79.0,), 100.0),
    )
    actual_stages = tuple(
        (
            stage.name,
            stage.first_frame,
            stage.last_frame,
            stage.h3_amplitudes_mvpk,
            stage.expected_scale_mvpk,
        )
        for stage in STAGES
    )
    if actual_stages != expected_stages:
        raise RuntimeError("the four logged hysteresis stages changed")
    if (BIND_IP, BIND_PORT, PEER_IP, PEER_PORT) != (
        "192.168.10.5",
        50_000,
        "192.168.10.3",
        50_001,
    ):
        raise RuntimeError("the fixed PC-to-P4 route changed")
    if cslp.FRAME_PERIOD_US != 50_000:
        raise RuntimeError("the imported profile no longer sends at 20 Hz")
    if HOLD_SECONDS != 2.0:
        raise RuntimeError("the post-stream hold is no longer exactly 2 seconds")
    if FRAME_COUNT * cslp.CHUNK_COUNT != 960:
        raise RuntimeError("the fixed schedule no longer contains 960 WAVE packets")


def validate_schedule(
    sample_variants: dict[float, tuple[int, ...]],
) -> tuple[tuple[int, ...], ...]:
    expected_h3 = (
        tuple(83.2 if frame_id % 2 == 1 else 83.5 for frame_id in range(1, 41))
        + (90.0,) * 10
        + (83.5,) * 10
        + (79.0,) * 20
    )
    actual_h3 = tuple(
        h3_amplitude_mvpk(frame_id)
        for frame_id in range(1, FRAME_COUNT + 1)
    )
    if actual_h3 != expected_h3 or len(actual_h3) != FRAME_COUNT:
        raise RuntimeError("80-frame H3 schedule or a stage boundary changed")
    boundary_expectations = {
        1: 83.2,
        2: 83.5,
        39: 83.2,
        40: 83.5,
        41: 90.0,
        50: 90.0,
        51: 83.5,
        60: 83.5,
        61: 79.0,
        80: 79.0,
    }
    if any(actual_h3[frame_id - 1] != expected
           for frame_id, expected in boundary_expectations.items()):
        raise RuntimeError("an exact hysteresis schedule boundary changed")

    if set(sample_variants) != {79.0, 83.2, 83.5, 90.0}:
        raise RuntimeError("the generated H3 sample variants are incomplete")
    if len(set(sample_variants.values())) != len(sample_variants):
        raise RuntimeError("different H3 truths produced identical sample vectors")

    frame_samples = tuple(sample_variants[h3] for h3 in actual_h3)
    for left_frame, right_frame in ((1, 2), (40, 41), (50, 51), (60, 61)):
        if frame_samples[left_frame - 1] == frame_samples[right_frame - 1]:
            raise RuntimeError(
                f"samples did not change across fixture boundary "
                f"{left_frame}->{right_frame}"
            )

    # The phases are intentionally fixed.  Every vector is a legal G-problem
    # three-tone input (and in fact remains inside the narrower u_a overlap).
    for h3_mvpk in sample_variants:
        tones = tones_for_h3(h3_mvpk)
        harmonics = tuple(harmonic for harmonic, _, _ in tones)
        frequencies = tuple(harmonic * FUNDAMENTAL_HZ for harmonic in harmonics)
        amplitudes = tuple(amplitude for _, amplitude, _ in tones)
        vpp_volts, _rms_volts = cslp.expected_multitone_metrics(tones)
        if harmonics != (1, 3, 4) or len(set(harmonics)) != 3:
            raise RuntimeError("fixture must contain H1 plus exactly two harmonics")
        if min(amplitudes) < 0.005:
            raise RuntimeError("a fixture component fell below 5 mVpk")
        if min(frequencies) < 10_000.0 or max(frequencies) > 200_000.0:
            raise RuntimeError("a fixture component left the u_a frequency band")
        if not (0.050 <= vpp_volts <= 0.250):
            raise RuntimeError("fixed-phase total Vpp left 50..250 mV")
        if not (0.100 <= vpp_volts <= 0.250):
            raise RuntimeError("fixture unexpectedly left the u_a voltage overlap")
        if len(sample_variants[h3_mvpk]) != cslp.FRAME_SAMPLE_COUNT:
            raise RuntimeError("synthesis did not return 8192 samples")

    hysteretic: list[float] = []
    previous = 0.0
    for maximum in actual_h3:
        previous = hysteretic_scale_mvpk(maximum, previous)
        hysteretic.append(previous)
    hysteretic_tuple = tuple(hysteretic)
    if collapsed(hysteretic_tuple) != (100.0, 200.0, 100.0):
        raise RuntimeError("reference hysteresis did not produce 100->200->100")
    if (
        hysteretic_tuple[:40] != (100.0,) * 40
        or hysteretic_tuple[40:60] != (200.0,) * 20
        or hysteretic_tuple[60:] != (100.0,) * 20
    ):
        raise RuntimeError("reference scale changed at an unexpected frame")
    stateless_first_stage = tuple(
        stateless_scale_mvpk(maximum) for maximum in actual_h3[:40]
    )
    expected_stateless = tuple(
        100.0 if frame_id % 2 == 1 else 200.0
        for frame_id in range(1, 41)
    )
    if stateless_first_stage != expected_stateless:
        raise RuntimeError("the first stage no longer provokes stateless scale jitter")
    if hysteretic_tuple[0] != 100.0:
        raise RuntimeError("the first frame no longer starts on the 100 mVpk tier")
    return frame_samples


def validate_packetization(frame_samples: tuple[tuple[int, ...], ...]) -> None:
    if cslp.CHUNK_COUNT != 12 or cslp.FRAME_SAMPLE_COUNT * 2 != 16_384:
        raise RuntimeError("the frozen 12-chunk/16384-byte frame profile changed")

    for frame_id, samples in enumerate(frame_samples, start=1):
        reconstructed: list[int] = []
        payload_bytes_in_frame = 0
        chunks_in_frame = 0
        for chunk_index in range(cslp.CHUNK_COUNT):
            message_seq = (frame_id - 1) * cslp.CHUNK_COUNT + chunk_index + 1
            packet = cslp.build_wave_packet(
                0x11223344,
                message_seq,
                frame_id,
                frame_id * cslp.FRAME_PERIOD_US,
                chunk_index,
                0x55667788,
                samples,
                SCALE_UV_PER_LSB,
                OFFSET_UV,
                CALIBRATION_ID,
            )
            (
                magic,
                version,
                message_type,
                header_bytes,
                session_id,
                parsed_sequence,
                _timestamp_us,
                payload_bytes,
                flags,
                crc,
            ) = cslp.COMMON_HEADER.unpack_from(packet)
            if (
                magic != cslp.MAGIC
                or version != cslp.VERSION
                or message_type != cslp.WAVE_DATA
                or header_bytes != cslp.WAVE_HEADER_BYTES
                or session_id != 0x11223344
                or parsed_sequence != message_seq
                or len(packet) != header_bytes + payload_bytes
                or crc != cslp.packet_crc32(packet)
            ):
                raise RuntimeError(f"frame {frame_id} chunk {chunk_index} header failed")

            expected_sample_offset = chunk_index * cslp.SAMPLES_PER_CHUNK
            expected_sample_count = min(
                cslp.SAMPLES_PER_CHUNK,
                cslp.FRAME_SAMPLE_COUNT - expected_sample_offset,
            )
            wave_header = cslp.WAVE_HEADER.unpack_from(
                packet, cslp.COMMON_HEADER_BYTES
            )
            expected_wave_header = (
                frame_id,
                chunk_index,
                cslp.CHUNK_COUNT,
                expected_sample_offset,
                expected_sample_count,
                cslp.SAMPLE_FORMAT_S16_LE,
                cslp.CHANNEL_COUNT,
                cslp.SAMPLE_RATE_HZ,
                cslp.FRAME_SAMPLE_COUNT,
                SCALE_UV_PER_LSB,
                OFFSET_UV,
                0x55667788,
                cslp.FILTER_PROFILE,
                CALIBRATION_ID,
            )
            if wave_header != expected_wave_header:
                raise RuntimeError(f"frame {frame_id} chunk {chunk_index} metadata failed")
            expected_flags = (
                cslp.FLAG_FILTERED
                | cslp.FLAG_CALIBRATED
                | cslp.FLAG_TEST_PATTERN
            )
            if chunk_index == 0:
                expected_flags |= cslp.FLAG_FIRST_CHUNK
            if chunk_index + 1 == cslp.CHUNK_COUNT:
                expected_flags |= cslp.FLAG_LAST_CHUNK
            if flags != expected_flags or payload_bytes != expected_sample_count * 2:
                raise RuntimeError(f"frame {frame_id} chunk {chunk_index} shape failed")

            reconstructed.extend(
                struct.unpack_from(
                    f"<{expected_sample_count}h", packet, header_bytes
                )
            )
            payload_bytes_in_frame += payload_bytes
            chunks_in_frame += 1

        if chunks_in_frame != 12 or payload_bytes_in_frame != 16_384:
            raise RuntimeError(
                f"frame {frame_id} was not exactly 12 chunks/16384 payload bytes"
            )
        if tuple(reconstructed) != samples:
            raise RuntimeError(f"frame {frame_id} samples changed during packetization")


def self_test() -> dict[float, tuple[int, ...]]:
    cslp.self_test()
    validate_fixture_profile()
    sample_variants = build_sample_variants()
    frame_samples = validate_schedule(sample_variants)
    validate_packetization(frame_samples)
    return sample_variants


def print_stage_truths() -> None:
    print(
        f"fixed route {BIND_IP}:{BIND_PORT} -> {PEER_IP}:{PEER_PORT}; "
        f"F0={FUNDAMENTAL_HZ:.3f}Hz H1=25.0mVpk@{H1_PHASE_RADIANS:.2f}rad "
        f"H4=10.0mVpk@{H4_PHASE_RADIANS:.2f}rad",
        flush=True,
    )
    for stage in STAGES:
        truths: list[str] = []
        for h3_mvpk in stage.h3_amplitudes_mvpk:
            vpp, rms = cslp.expected_multitone_metrics(tones_for_h3(h3_mvpk))
            truths.append(
                f"H3={h3_mvpk:.1f}mVpk@{H3_PHASE_RADIANS:.2f}rad "
                f"Vpp={vpp * 1000.0:.6f}mV RMS={rms * 1000.0:.6f}mV"
            )
        print(
            f"stage={stage.name} frames={stage.first_frame}-{stage.last_frame} "
            f"expected_Amax={stage.expected_scale_mvpk:.0f}mVpk truth: "
            + " | ".join(truths),
            flush=True,
        )


def send_schedule(
    emulator: cslp.CslpFpgaEmulator,
    sample_variants: dict[float, tuple[int, ...]],
    chunk_gap_us: int,
) -> None:
    wave_sequence = secrets.randbits(32)
    status_sequence = secrets.randbits(32)
    frames_sent = 0
    wave_packets_sent = 0
    next_status = time.monotonic()
    next_frame = time.monotonic()

    for frame_id in range(1, FRAME_COUNT + 1):
        now = time.monotonic()
        if now < next_frame:
            time.sleep(next_frame - now)
        frame_start = time.monotonic()
        frame_timestamp_us = cslp.monotonic_us(emulator.boot_start_ns)

        if frame_start >= next_status:
            status_sequence = (status_sequence + 1) & 0xFFFFFFFF
            emulator.send_status(
                status_sequence,
                frames_sent,
                frames_sent,
                wave_packets_sent,
            )
            next_status = frame_start + 0.5

        samples = sample_variants[h3_amplitude_mvpk(frame_id)]
        for chunk_index in range(cslp.CHUNK_COUNT):
            wave_sequence = (wave_sequence + 1) & 0xFFFFFFFF
            packet = cslp.build_wave_packet(
                emulator.session_id,
                wave_sequence,
                frame_id,
                frame_timestamp_us,
                chunk_index,
                emulator.config_id,
                samples,
                SCALE_UV_PER_LSB,
                OFFSET_UV,
                CALIBRATION_ID,
            )
            emulator.socket.sendto(packet, emulator.peer_address)
            wave_packets_sent += 1
            if chunk_index + 1 < cslp.CHUNK_COUNT:
                time.sleep(chunk_gap_us / 1_000_000.0)

        frames_sent += 1
        next_frame = frame_start + cslp.FRAME_PERIOD_US / 1_000_000.0
        stage = next(
            item for item in STAGES
            if item.first_frame <= frame_id <= item.last_frame
        )
        if frame_id == stage.last_frame:
            print(
                f"sent stage={stage.name} through frame={frame_id} "
                f"wave_packets={wave_packets_sent}",
                flush=True,
            )

    if frames_sent != FRAME_COUNT or wave_packets_sent != FRAME_COUNT * cslp.CHUNK_COUNT:
        raise RuntimeError("sender counters disagree with the fixed 80-frame schedule")

    hold_deadline = time.monotonic() + HOLD_SECONDS
    while True:
        remaining = hold_deadline - time.monotonic()
        if remaining <= 0.0:
            break
        status_sequence = (status_sequence + 1) & 0xFFFFFFFF
        emulator.send_status(
            status_sequence,
            frames_sent,
            frames_sent,
            wave_packets_sent,
        )
        time.sleep(min(0.5, remaining))

    print(
        f"completed frames={frames_sent} wave_packets={wave_packets_sent} "
        "expected_scale=100->200->100 stateless_first40=100/200-jitter",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunk-gap-us",
        type=int,
        default=CHUNK_GAP_US,
        help="delay between WAVE chunks; default: 250 us",
    )
    parser.add_argument(
        "--handshake-timeout",
        type=float,
        default=HANDSHAKE_TIMEOUT_SECONDS,
        help="seconds to wait for the fixed ESP32-P4 peer; default: 15",
    )
    parser.add_argument(
        "--self-test-only",
        action="store_true",
        help="validate all 80 frames/960 packets offline and do not bind a socket",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.chunk_gap_us < 0 or args.handshake_timeout <= 0.0:
        raise SystemExit("chunk gap must be non-negative and timeout must be positive")

    sample_variants = self_test()
    if args.self_test_only:
        print(
            "CSLP spectrum hysteresis sender self-test passed: "
            "frames=80 chunks=960 payload=16384B/frame "
            "scale=100->200->100 stateless_first40=100/200-jitter",
            flush=True,
        )
        return 0

    print_stage_truths()
    emulator = cslp.CslpFpgaEmulator(
        BIND_IP,
        BIND_PORT,
        PEER_IP,
        PEER_PORT,
        sample_variants[83.2],
        SCALE_UV_PER_LSB,
        OFFSET_UV,
        CALIBRATION_ID,
    )
    print(
        f"listening on {BIND_IP}:{BIND_PORT}; expecting {PEER_IP}:{PEER_PORT}; "
        f"frames={FRAME_COUNT} chunk_gap_us={args.chunk_gap_us} "
        f"hold_seconds={HOLD_SECONDS:.1f}",
        flush=True,
    )
    try:
        emulator.handshake(args.handshake_timeout)
        if emulator.device_state != cslp.DEVICE_STATE_PUSH_ENABLED:
            raise RuntimeError("handshake did not enter PUSH_ENABLED")
        send_schedule(emulator, sample_variants, args.chunk_gap_us)
    finally:
        emulator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
