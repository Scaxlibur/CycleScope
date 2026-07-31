import importlib.util
import inspect
import io
import ipaddress
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "tools" / "cslp_lan_stress.py"
SPEC = importlib.util.spec_from_file_location("cslp_lan_stress", MODULE_PATH)
stress = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = stress
SPEC.loader.exec_module(stress)


def replace_flags(packet, flags):
    message = bytearray(packet)
    struct.pack_into(">H", message, 26, flags)
    struct.pack_into(">I", message, 28, stress.crc32_datagram(message))
    return stress.parse_datagram(bytes(message))


def build_profile_wave(
    *,
    sequence,
    payload,
    frame_id=1,
    chunk_index=0,
    extra_flags=0,
    timestamp_us=1234567,
    scale_uv_per_lsb=488,
    offset_uv=0,
    calibration_id=0,
    test_pattern=True,
):
    samples_in_chunk = min(
        stress.FULL_CHUNK_SAMPLES,
        stress.FRAME_SAMPLES - chunk_index * stress.FULL_CHUNK_SAMPLES,
    )
    if len(payload) != samples_in_chunk * 2:
        raise ValueError("payload does not match chunk layout")
    flags = stress.FLAG_FILTERED | extra_flags
    if test_pattern:
        flags |= stress.FLAG_TEST_PATTERN
    if chunk_index == 0:
        flags |= stress.FLAG_FIRST_CHUNK
    if chunk_index + 1 == stress.CHUNK_COUNT:
        flags |= stress.FLAG_LAST_CHUNK
    extension = stress.WAVE_EXTENSION.pack(
        frame_id,
        chunk_index,
        stress.CHUNK_COUNT,
        chunk_index * stress.FULL_CHUNK_SAMPLES,
        samples_in_chunk,
        stress.SAMPLE_FORMAT_S16_LE,
        stress.CHANNEL_COUNT,
        stress.SAMPLE_RATE_HZ,
        stress.FRAME_SAMPLES,
        scale_uv_per_lsb,
        offset_uv,
        7,
        stress.FILTER_PROFILE,
        calibration_id,
    )
    header = stress.COMMON_HEADER.pack(
        stress.MAGIC,
        stress.VERSION,
        stress.MSG_WAVE_DATA,
        stress.WAVE_HEADER_BYTES,
        0xAABBCCDD,
        sequence,
        timestamp_us,
        len(payload),
        flags,
        0,
    )
    message = bytearray(header + extension + payload)
    struct.pack_into(">I", message, 28, stress.crc32_datagram(message))
    return stress.parse_datagram(bytes(message))


def build_frame_packets(
    *,
    frame_id,
    timestamp_us,
    sequence_start,
    test_pattern=True,
    extra_flags=0,
    scale_uv_per_lsb=488,
    offset_uv=0,
    calibration_id=0,
):
    samples = [(index % 4096) - 2048 for index in range(stress.FRAME_SAMPLES)]
    packets = []
    for chunk_index in range(stress.CHUNK_COUNT):
        offset = chunk_index * stress.FULL_CHUNK_SAMPLES
        count = min(
            stress.FULL_CHUNK_SAMPLES,
            stress.FRAME_SAMPLES - offset,
        )
        payload = struct.pack(f"<{count}h", *samples[offset : offset + count])
        packets.append(
            build_profile_wave(
                sequence=sequence_start + chunk_index,
                payload=payload,
                frame_id=frame_id,
                chunk_index=chunk_index,
                timestamp_us=timestamp_us,
                test_pattern=test_pattern,
                extra_flags=extra_flags,
                scale_uv_per_lsb=scale_uv_per_lsb,
                offset_uv=offset_uv,
                calibration_id=calibration_id,
            )
        )
    return packets


def build_status(*, sequence, flags=0, reserved=0):
    payload = stress.STATUS_PAYLOAD.pack(
        stress.DEVICE_IDLE,
        0,
        0,
        99,
        10,
        120,
        0,
        0,
        0,
        1234,
        reserved,
    )
    message = stress.build_message(
        stress.MSG_STATUS,
        0xAABBCCDD,
        sequence,
        payload,
        timestamp_us=123,
    )
    return replace_flags(message, flags)


def build_ack(message_type, sequence, payload, session_id=0xAABBCCDD):
    return stress.parse_datagram(
        stress.build_message(
            message_type,
            session_id,
            sequence,
            payload,
            timestamp_us=123,
        )
    )


def bare_status_client():
    client = stress.StressClient.__new__(stress.StressClient)
    client.counters = stress.Counters()
    client.status_snapshots = []
    client.status_intervals_ms = []
    client.last_status_arrival_ns = None
    client.last_status_seq = None
    client.baseline_status = None
    return client


def bare_wave_client():
    client = stress.StressClient.__new__(stress.StressClient)
    client.args = SimpleNamespace(
        progress_every=100,
        source_mode="test-pattern",
        expected_test_faults=0,
        expected_calibration_id=0,
        expected_scale_uv_per_lsb=None,
        expected_offset_uv=None,
    )
    client.counters = stress.Counters()
    client.disable_ack_ns = None
    client.disable_trigger_frame_id = None
    client.packet_sizes = {}
    client.last_wave_seq = None
    client.last_wave_arrival_ns = None
    client.wave_intervals_us = []
    client.config_id = 7
    client.assemblies = {}
    client.active_frame_id = None
    client.completed_frame_ids = set()
    client.last_frame_timestamp_us = None
    client.last_frame_id = None
    client.sample_min = 32767
    client.sample_max = -32768
    client.sample_values = set()
    client.wave_metadata_identity = None
    client.first_wave_ns = None
    client.first_complete_ns = None
    client.last_complete_ns = None
    client.frame_intervals_ms = []
    client.frame_timestamp_intervals_ms = []
    client.run_started_ns = 0
    return client


def default_args(**overrides):
    values = {
        "passive_mirror": False,
        "local_ip": "192.168.10.4",
        "local_port": 50001,
        "remote_ip": "192.168.10.2",
        "remote_port": 50000,
        "interface": "enp2s0",
        "source_mode": "test-pattern",
        "activity_policy": "require",
        "overrange_policy": "reject",
        "expected_test_faults": 0,
        "expected_calibration_id": 0,
        "expected_scale_uv_per_lsb": None,
        "expected_offset_uv": None,
        "capture_dir": None,
        "frames": 10,
        "run_timeout": 2.0,
        "control_timeout": 0.1,
        "control_retries": 3,
        "baseline_status_timeout": 1.6,
        "final_status_timeout": 1.6,
        "post_disable_observe": 0.0,
        "receive_buffer": 4 * 1024 * 1024,
        "progress_every": 100,
        "report": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LanStressProtocolTests(unittest.TestCase):
    def test_monotonic_response_latency_is_explicit_and_fail_closed(self):
        self.assertEqual(stress.monotonic_latency_us(1_000, 251_000), 250.0)
        self.assertIsNone(stress.monotonic_latency_us(None, 251_000))
        self.assertIsNone(stress.monotonic_latency_us(1_000, None))
        with self.assertRaisesRegex(stress.ProtocolError, "moved backwards"):
            stress.monotonic_latency_us(2_000, 1_000)

    def test_golden_wave_packet(self):
        golden = bytes.fromhex(
            "43 53 4C 50 01 20 00 48 11 22 33 44 01 02 03 04 "
            "00 00 00 00 00 12 D6 87 00 0A 00 0F 69 DB 20 4C "
            "00 00 00 2A 00 00 00 01 00 00 00 00 00 05 01 01 "
            "00 3D FD 24 00 00 00 05 00 00 01 E8 00 00 00 00 "
            "00 00 00 07 00 01 00 03 00 80 FF FF 00 00 01 00 "
            "FF 7F"
        )
        packet = stress.parse_datagram(golden)
        self.assertEqual(stress.crc32_datagram(golden), 0x69DB204C)
        self.assertEqual(packet.header.message_type, stress.MSG_WAVE_DATA)
        self.assertEqual(packet.header.session_id, 0x11223344)
        self.assertEqual(packet.header.timestamp_us, 1_234_567)
        chunk = stress.parse_wave(packet)
        self.assertEqual(chunk.frame_id, 42)
        self.assertEqual(chunk.samples_in_chunk, 5)
        self.assertEqual(chunk.samples, bytes.fromhex("00 80 FF FF 00 00 01 00 FF 7F"))

    def test_passive_mirror_control_replay_and_seq_conflict_are_distinct(self):
        client = stress.StressClient.__new__(stress.StressClient)
        client.args = SimpleNamespace(passive_mirror=True)
        client.config_id = 0
        client.device_boot_id = 0
        client.mirror_control_responses = {}
        client.mirror_control_replays = 0
        client.mirror_control_seq_conflicts = 0
        client.mirror_disable_ack_seen = False
        client.mirror_handshake_types = set()
        client.disable_ack_ns = None

        hello_payload = struct.pack(
            ">HBBIII",
            stress.STATUS_OK,
            stress.VERSION,
            0,
            stress.REQUIRED_CAPS,
            stress.FRAME_SAMPLES,
            0x10203040,
        )
        hello = build_ack(stress.MSG_HELLO_ACK, 1, hello_payload)
        client.consume_mirrored_control(hello, 1000)
        self.assertEqual(client.device_boot_id, 0x10203040)

        config_payload = struct.pack(
            ">HHIIIIBBHI",
            stress.STATUS_OK,
            0,
            7,
            stress.SAMPLE_RATE_HZ,
            stress.FRAME_SAMPLES,
            stress.FRAME_PERIOD_US,
            stress.SAMPLE_FORMAT_S16_LE,
            stress.CHANNEL_COUNT,
            stress.FILTER_PROFILE,
            stress.FRAME_SAMPLES,
        )
        config = build_ack(stress.MSG_CONFIG_ACK, 2, config_payload)
        client.consume_mirrored_control(config, 2000)
        client.consume_mirrored_control(config, 3000)
        self.assertEqual(client.config_id, 7)
        self.assertEqual(client.mirror_control_replays, 1)

        conflict_payload = struct.pack(">H", stress.STATUS_SEQ_CONFLICT) + bytes(26)
        conflict = build_ack(stress.MSG_CONFIG_ACK, 2, conflict_payload)
        client.consume_mirrored_control(conflict, 4000)
        self.assertEqual(client.mirror_control_seq_conflicts, 1)

        disable = build_ack(
            stress.MSG_DISABLE_PUSH_ACK,
            4,
            struct.pack(">HH", stress.STATUS_OK, 0),
        )
        client.consume_mirrored_control(disable, 5000)
        self.assertTrue(client.mirror_disable_ack_seen)
        self.assertEqual(client.disable_ack_ns, 5000)

    def test_passive_mirror_late_join_locks_config_from_wave(self):
        client = bare_wave_client()
        client.args.passive_mirror = True
        client.config_id = 0
        packet = build_frame_packets(
            frame_id=1, timestamp_us=1234567, sequence_start=100
        )[0]
        client.consume(packet, 1000)
        self.assertEqual(client.config_id, 7)

    def test_passive_mirror_entrypoint_contains_no_network_send(self):
        source = inspect.getsource(stress.StressClient.run_passive_mirror)
        self.assertNotIn("sendto(", source)
        with mock.patch.object(
            sys,
            "argv",
            ["cslp_lan_stress.py", "--passive-mirror", "--frames", "2"],
        ):
            args = stress.parse_args()
        self.assertTrue(args.passive_mirror)
        self.assertEqual(args.local_port, 50002)

    def test_network_write_counter_tracks_control_retries(self):
        client = stress.StressClient.__new__(stress.StressClient)
        client.args = SimpleNamespace(control_retries=2, control_timeout=0.01)
        client.counters = stress.Counters()
        client.network_writes = 0
        client.socket = mock.Mock()
        client.remote = ("192.168.10.2", 50000)
        client.receive = mock.Mock(side_effect=stress.socket.timeout)

        with self.assertRaises(TimeoutError):
            client.exchange(b"request", stress.MSG_HELLO_ACK, 1)
        self.assertEqual(client.network_writes, 2)
        self.assertEqual(client.socket.sendto.call_count, 2)

    def test_wave_requires_nonzero_frame_id_and_scale(self):
        payload = bytes(stress.FULL_CHUNK_SAMPLES * 2)
        zero_frame = build_profile_wave(
            sequence=1,
            payload=payload,
            frame_id=0,
        )
        with self.assertRaisesRegex(stress.ProtocolError, "frame_id"):
            stress.parse_wave(zero_frame)

        zero_scale = build_profile_wave(
            sequence=2,
            payload=payload,
            scale_uv_per_lsb=0,
        )
        with self.assertRaisesRegex(stress.ProtocolError, "scale_uV_per_lsb"):
            stress.parse_wave(zero_scale)

    def test_newer_frame_abandons_incomplete_frame_and_still_completes(self):
        client = bare_wave_client()
        client.consume_wave(
            build_frame_packets(
                frame_id=10,
                timestamp_us=1_000_000,
                sequence_start=1,
            )[0],
            1_000_000,
        )
        packets = build_frame_packets(
            frame_id=11,
            timestamp_us=1_050_000,
            sequence_start=2,
        )
        for index, packet in enumerate(packets, start=2):
            client.consume_wave(packet, index * 1_000_000)

        self.assertEqual(client.counters.frame_interleaves, 1)
        self.assertEqual(client.counters.incomplete_frames, 1)
        self.assertEqual(client.counters.frames_completed, 1)
        self.assertIn(11, client.completed_frame_ids)
        self.assertNotIn(10, client.assemblies)
        self.assertIsNone(client.active_frame_id)

    def test_late_old_frame_chunk_is_dropped_without_disturbing_new_frame(self):
        client = bare_wave_client()
        old_packets = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
        )
        new_packets = build_frame_packets(
            frame_id=11,
            timestamp_us=1_050_000,
            sequence_start=2,
        )

        client.consume_wave(old_packets[0], 1_000_000)
        client.consume_wave(new_packets[0], 2_000_000)
        self.assertIsNone(client.consume_wave(old_packets[1], 3_000_000))
        for index, packet in enumerate(new_packets[1:], start=4):
            client.consume_wave(packet, index * 1_000_000)

        self.assertEqual(client.counters.frame_id_reordered, 1)
        self.assertEqual(client.counters.frames_completed, 1)
        self.assertIn(11, client.completed_frame_ids)

    def test_completed_frame_id_replay_is_counted_and_dropped(self):
        client = bare_wave_client()
        packets = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
        )
        for index, packet in enumerate(packets, start=1):
            client.consume_wave(packet, index * 1_000_000)

        replay = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=100,
        )[0]
        self.assertIsNone(client.consume_wave(replay, 20_000_000))
        self.assertEqual(client.counters.frame_id_duplicates, 1)
        self.assertEqual(client.counters.frames_completed, 1)
        self.assertFalse(client.assemblies)

    def test_older_unseen_frame_is_dropped_against_completed_high_water(self):
        client = bare_wave_client()
        for index, packet in enumerate(
            build_frame_packets(
                frame_id=11,
                timestamp_us=1_050_000,
                sequence_start=20,
            ),
            start=1,
        ):
            client.consume_wave(packet, index * 1_000_000)

        old = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=100,
        )[0]
        self.assertIsNone(client.consume_wave(old, 20_000_000))
        self.assertEqual(client.counters.frame_id_reordered, 1)
        self.assertEqual(client.counters.frames_completed, 1)
        self.assertFalse(client.assemblies)

    def test_frame_id_wrap_skips_zero_without_a_false_gap(self):
        client = bare_wave_client()
        arrival = 0
        for packet in build_frame_packets(
            frame_id=0xFFFFFFFF,
            timestamp_us=1_000_000,
            sequence_start=1,
        ) + build_frame_packets(
            frame_id=1,
            timestamp_us=1_050_000,
            sequence_start=20,
        ):
            arrival += 1_000_000
            client.consume_wave(packet, arrival)

        self.assertEqual(client.counters.frames_completed, 2)
        self.assertEqual(client.counters.frame_id_gaps, 0)
        self.assertEqual(client.counters.frame_id_reordered, 0)
        self.assertEqual(client.last_frame_id, 1)

    def test_frame_timestamp_must_advance(self):
        client = bare_wave_client()
        packets = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
        )
        for index, packet in enumerate(packets, start=1):
            client.consume_wave(packet, index * 1_000_000)

        with self.assertRaisesRegex(stress.TimestampOrderError, "did not advance"):
            client.consume_wave(
                build_frame_packets(
                    frame_id=11,
                    timestamp_us=1_000_000,
                    sequence_start=100,
                )[0],
                20_000_000,
            )
        self.assertEqual(client.counters.timestamp_order_errors, 1)

    def test_control_message_round_trip(self):
        message = stress.build_message(
            stress.MSG_ENABLE_PUSH,
            0xAABBCCDD,
            7,
            timestamp_us=123,
        )
        packet = stress.parse_datagram(message)
        self.assertEqual(packet.header.header_bytes, stress.COMMON_HEADER_BYTES)
        self.assertEqual(packet.header.payload_bytes, 0)
        self.assertEqual(packet.payload, b"")

    def test_crc_corruption_is_rejected(self):
        message = bytearray(
            stress.build_message(stress.MSG_DISABLE_PUSH, 1, 2, timestamp_us=3)
        )
        message[12] ^= 1
        with self.assertRaisesRegex(stress.ProtocolError, "CRC"):
            stress.parse_datagram(bytes(message))

    def test_idle_status_is_saved_as_counter_baseline(self):
        packet = build_status(sequence=1)
        client = bare_status_client()
        client.args = SimpleNamespace(baseline_status_timeout=0.1)
        client.receive = lambda _timeout: (packet, 1_000_000)

        client.wait_for_idle_status()

        self.assertIsNotNone(client.baseline_status)
        self.assertEqual(client.baseline_status["device_state"], stress.DEVICE_IDLE)
        self.assertEqual(client.baseline_status["frames_sent"], 10)
        self.assertEqual(client.baseline_status["packets_sent"], 120)

    def test_conflicting_duplicate_has_its_own_counter(self):
        first_payload = bytes(stress.FULL_CHUNK_SAMPLES * 2)
        second_payload = b"\x01\x00" + first_payload[2:]
        first = build_profile_wave(sequence=1, payload=first_payload)
        conflict = build_profile_wave(sequence=2, payload=second_payload)
        client = bare_wave_client()

        client.consume_wave(first, 1_000_000)
        with self.assertRaises(stress.ChunkConflictError):
            client.consume_wave(conflict, 2_000_000)

        self.assertEqual(client.counters.chunk_conflicts, 1)
        self.assertEqual(client.counters.metadata_errors, 0)

    def test_status_sequence_gap_duplicate_and_reorder_are_distinct(self):
        client = bare_status_client()
        for index, sequence in enumerate((10, 11, 11, 13, 12, 14), start=1):
            client.consume_status(build_status(sequence=sequence), index * 1_000_000)

        self.assertEqual(client.counters.status_sequence_gaps, 1)
        self.assertEqual(client.counters.status_sequence_duplicates, 1)
        self.assertEqual(client.counters.status_sequence_reordered, 1)
        self.assertEqual(client.last_status_seq, 14)

    def test_status_flags_and_payload_reserved_are_rejected(self):
        client = bare_status_client()
        with self.assertRaisesRegex(stress.ProtocolError, "STATUS header/flags"):
            client.consume_status(build_status(sequence=1, flags=0x0001), 1)
        with self.assertRaisesRegex(stress.ProtocolError, "reserved"):
            client.consume_status(build_status(sequence=2, reserved=1), 2)
        self.assertEqual(client.counters.status_format_errors, 2)

    def test_wave_reserved_flags_are_rejected(self):
        packet = build_profile_wave(
            sequence=1,
            payload=bytes(stress.FULL_CHUNK_SAMPLES * 2),
            extra_flags=0x8000,
        )
        with self.assertRaisesRegex(stress.WaveFlagError, "reserved flag"):
            stress.parse_wave(packet)

    def test_real_adc_mode_collects_samples_before_reporting_overflow(self):
        client = bare_wave_client()
        client.args = default_args(source_mode="real-adc")
        packets = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
            test_pattern=False,
            extra_flags=stress.FLAG_FIFO_OVERFLOW,
        )

        for index, packet in enumerate(packets, start=1):
            client.consume_wave(packet, index * 1_000_000)

        self.assertEqual(client.counters.frames_completed, 1)
        self.assertEqual(client.counters.fifo_overflow_wave_frames, 1)
        self.assertGreater(len(client.sample_values), 1)

    def test_real_adc_mode_rejects_test_pattern_flag(self):
        client = bare_wave_client()
        client.args = default_args(source_mode="real-adc")
        packet = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
        )[0]

        with self.assertRaisesRegex(stress.WaveFlagError, "real ADC mode"):
            client.consume_wave(packet, 1_000_000)

    def test_calibrated_real_adc_requires_exact_flag_id_scale_and_offset(self):
        client = bare_wave_client()
        client.args = default_args(
            source_mode="real-adc",
            expected_calibration_id=17,
            expected_scale_uv_per_lsb=516,
            expected_offset_uv=-6708,
        )
        packets = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
            test_pattern=False,
            extra_flags=stress.FLAG_CALIBRATED,
            calibration_id=17,
            scale_uv_per_lsb=516,
            offset_uv=-6708,
        )
        for index, packet in enumerate(packets, start=1):
            client.consume_wave(packet, index * 1_000_000)

        self.assertEqual(client.counters.frames_completed, 1)
        self.assertEqual(client.wave_metadata_identity, (17, 516, -6708))

    def test_calibrated_real_adc_metadata_mismatch_fails_closed(self):
        client = bare_wave_client()
        client.args = default_args(
            source_mode="real-adc",
            expected_calibration_id=17,
            expected_scale_uv_per_lsb=516,
            expected_offset_uv=-6708,
        )
        packet = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
            test_pattern=False,
            extra_flags=stress.FLAG_CALIBRATED,
            calibration_id=18,
            scale_uv_per_lsb=516,
            offset_uv=-6708,
        )[0]
        with self.assertRaisesRegex(stress.ProtocolError, "metadata mismatch"):
            client.consume_wave(packet, 1_000_000)

    def test_expected_test_fault_flags_are_accepted_and_counted(self):
        for fault, flag, counter_name in (
            (
                stress.TEST_FAULT_OTR,
                stress.FLAG_ADC_OVERRANGE,
                "adc_overrange_wave_frames",
            ),
            (
                stress.TEST_FAULT_OVERFLOW,
                stress.FLAG_FIFO_OVERFLOW,
                "fifo_overflow_wave_frames",
            ),
        ):
            with self.subTest(fault=fault):
                client = bare_wave_client()
                client.args.expected_test_faults = fault
                packets = build_frame_packets(
                    frame_id=10,
                    timestamp_us=1_000_000,
                    sequence_start=1,
                    extra_flags=flag,
                )
                for index, packet in enumerate(packets, start=1):
                    client.consume_wave(packet, index * 1_000_000)
                self.assertEqual(getattr(client.counters, counter_name), 1)

    def test_fault_policy_accepts_deterministic_combined_observation(self):
        failures = stress.test_fault_policy_failures(
            stress.TEST_FAULT_ALL,
            frames_completed=11,
            wave_overrange=1,
            wave_overflow=10,
            device_overrange=1,
            device_overflow=10,
            device_drops=1,
            timestamp_intervals_ms=[50.0, 100.0] + [50.0] * 8,
        )
        self.assertEqual(failures, [])

    def test_fault_policy_rejects_missing_or_extra_injections(self):
        failures = stress.test_fault_policy_failures(
            stress.TEST_FAULT_FRAME_DROP,
            frames_completed=11,
            wave_overrange=0,
            wave_overflow=1,
            device_overrange=0,
            device_overflow=1,
            device_drops=0,
            timestamp_intervals_ms=[50.0] * 10,
        )
        self.assertTrue(any("overflow" in failure for failure in failures))
        self.assertTrue(any("frame-drop count" in failure for failure in failures))
        self.assertTrue(any("100 ms" in failure for failure in failures))

    def test_frame_timestamp_gap_is_taken_from_cslp_metadata(self):
        client = bare_wave_client()
        client.args.expected_test_faults = stress.TEST_FAULT_FRAME_DROP
        packets = build_frame_packets(
            frame_id=10,
            timestamp_us=1_000_000,
            sequence_start=1,
        ) + build_frame_packets(
            frame_id=11,
            timestamp_us=1_100_000,
            sequence_start=20,
        )
        for index, packet in enumerate(packets, start=1):
            client.consume_wave(packet, index * 1_000_000)
        self.assertEqual(client.frame_timestamp_intervals_ms, [100.0])

    def test_control_ack_header_flags_payload_are_strict(self):
        valid = stress.build_message(
            stress.MSG_ENABLE_PUSH_ACK,
            0xAABBCCDD,
            7,
            struct.pack(">HH", stress.STATUS_OK, 0),
            timestamp_us=123,
        )
        packet = stress.parse_datagram(valid)
        self.assertEqual(
            stress.validate_control_ack(packet, stress.MSG_ENABLE_PUSH_ACK, 7),
            stress.STATUS_OK,
        )

        bad_flags = replace_flags(valid, 1)
        with self.assertRaisesRegex(stress.ProtocolError, "header/flags/length"):
            stress.validate_control_ack(bad_flags, stress.MSG_ENABLE_PUSH_ACK, 7)

        bad_reserved = stress.parse_datagram(
            stress.build_message(
                stress.MSG_ENABLE_PUSH_ACK,
                0xAABBCCDD,
                7,
                struct.pack(">HH", stress.STATUS_OK, 1),
                timestamp_us=123,
            )
        )
        with self.assertRaisesRegex(stress.ProtocolError, "reserved"):
            stress.validate_control_ack(bad_reserved, stress.MSG_ENABLE_PUSH_ACK, 7)

        bad_length = stress.parse_datagram(
            stress.build_message(
                stress.MSG_ENABLE_PUSH_ACK,
                0xAABBCCDD,
                7,
                b"\x00\x00",
                timestamp_us=123,
            )
        )
        with self.assertRaisesRegex(stress.ProtocolError, "header/flags/length"):
            stress.validate_control_ack(bad_length, stress.MSG_ENABLE_PUSH_ACK, 7)

    def test_handshake_probes_same_key_different_payload_conflict(self):
        client = stress.StressClient.__new__(stress.StressClient)
        client.args = default_args()
        client.session_id = 0xAABBCCDD
        client.control_seq = 1
        client.config_id = 0
        client.device_boot_id = 0
        client.enabled = False
        client.counters = stress.Counters()
        client.wait_for_idle_status = mock.Mock()
        config_requests = []

        hello_ack = stress.parse_datagram(
            stress.build_message(
                stress.MSG_HELLO_ACK,
                client.session_id,
                1,
                struct.pack(
                    ">HBBIII",
                    stress.STATUS_OK,
                    stress.VERSION,
                    0,
                    stress.REQUIRED_CAPS,
                    stress.FRAME_SAMPLES,
                    0x12345678,
                ),
                timestamp_us=1,
            )
        )
        config_ack = stress.parse_datagram(
            stress.build_message(
                stress.MSG_CONFIG_ACK,
                client.session_id,
                2,
                struct.pack(
                    ">HHIIIIBBHI",
                    stress.STATUS_OK,
                    0,
                    7,
                    stress.SAMPLE_RATE_HZ,
                    stress.FRAME_SAMPLES,
                    stress.FRAME_PERIOD_US,
                    stress.SAMPLE_FORMAT_S16_LE,
                    stress.CHANNEL_COUNT,
                    stress.FILTER_PROFILE,
                    stress.FRAME_SAMPLES,
                ),
                timestamp_us=2,
            )
        )
        conflict_ack = stress.parse_datagram(
            stress.build_message(
                stress.MSG_CONFIG_ACK,
                client.session_id,
                2,
                struct.pack(">H", stress.STATUS_SEQ_CONFLICT) + bytes(26),
                timestamp_us=3,
            )
        )
        enable_ack = stress.parse_datagram(
            stress.build_message(
                stress.MSG_ENABLE_PUSH_ACK,
                client.session_id,
                3,
                struct.pack(">HH", stress.STATUS_OK, 0),
                timestamp_us=4,
            )
        )

        def exchange(request, ack_type, sequence):
            packet = stress.parse_datagram(request)
            if ack_type == stress.MSG_HELLO_ACK:
                return hello_ack
            if ack_type == stress.MSG_CONFIG_ACK:
                config_requests.append(packet)
                return conflict_ack if len(config_requests) == 3 else config_ack
            if ack_type == stress.MSG_ENABLE_PUSH_ACK:
                return enable_ack
            raise AssertionError((ack_type, sequence))

        client.exchange = exchange
        client.handshake()

        self.assertEqual(len(config_requests), 3)
        self.assertEqual(
            [packet.header.message_seq for packet in config_requests],
            [2, 2, 2],
        )
        self.assertEqual(config_requests[0].raw, config_requests[1].raw)
        self.assertNotEqual(config_requests[0].payload, config_requests[2].payload)
        self.assertEqual(client.counters.control_replays, 1)
        self.assertEqual(client.counters.control_seq_conflicts, 1)
        self.assertTrue(client.enabled)
        self.assertIsInstance(client.enable_request_started_ns, int)
        self.assertIsInstance(client.enable_ack_ns, int)
        self.assertGreaterEqual(client.enable_ack_ns, client.enable_request_started_ns)

    def test_deferred_disable_requires_exactly_one_terminal_frame(self):
        self.assertEqual(
            stress.deferred_disable_count_failures(10, 11, 132),
            [],
        )
        failures = stress.deferred_disable_count_failures(10, 10, 120)
        self.assertTrue(any("expected 11 frames" in failure for failure in failures))

    def test_late_terminal_chunk_after_disable_ack_is_not_a_new_frame(self):
        payload = bytes(stress.FULL_CHUNK_SAMPLES * 2)
        terminal = build_profile_wave(
            sequence=1,
            payload=payload,
            frame_id=11,
            timestamp_us=1_000_000,
        )
        illegal_new = build_profile_wave(
            sequence=2,
            payload=payload,
            frame_id=12,
            timestamp_us=1_050_000,
        )
        client = bare_wave_client()
        client.disable_ack_ns = 100
        client.disable_trigger_frame_id = 11

        client.consume_wave(terminal, 200)
        self.assertEqual(client.counters.post_disable_wave_packets, 0)
        client.consume_wave(illegal_new, 300)
        self.assertEqual(client.counters.post_disable_wave_packets, 1)
        self.assertEqual(client.counters.frame_interleaves, 1)
        self.assertEqual(client.counters.incomplete_frames, 1)

    def test_network_preflight_requires_local_ip_on_selected_interface(self):
        args = SimpleNamespace(
            local_ip="192.168.10.4",
            remote_ip="192.168.10.2",
            interface="enp2s0",
        )
        configured = [(ipaddress.IPv4Address("192.168.10.4"), 24)]
        with mock.patch.object(stress, "read_interface_ipv4", return_value=configured):
            stress.validate_network_configuration(args)

        configured = [(ipaddress.IPv4Address("192.168.10.3"), 24)]
        with mock.patch.object(stress, "read_interface_ipv4", return_value=configured):
            with self.assertRaisesRegex(RuntimeError, "is not assigned"):
                stress.validate_network_configuration(args)

    def test_runtime_error_still_returns_partial_report_with_actual_rcvbuf(self):
        args = default_args()
        nic_stats = {
            "rx_packets": 10,
            "rx_bytes": 1000,
            "rx_dropped": 0,
            "rx_errors": 0,
        }
        fake_socket = mock.Mock()
        fake_socket.getsockopt.return_value = 425984
        with (
            mock.patch.object(stress, "validate_network_configuration"),
            mock.patch.object(stress, "read_nic_stats", return_value=nic_stats),
            mock.patch.object(stress.socket, "socket", return_value=fake_socket),
        ):
            client = stress.StressClient(args)
            client.handshake = mock.Mock(
                side_effect=TimeoutError("synthetic handshake timeout")
            )
            with (
                mock.patch("sys.stdout", new=io.StringIO()),
                mock.patch("sys.stderr", new=io.StringIO()),
            ):
                report = client.run()
            client.close()

        self.assertFalse(report["pass"])
        self.assertFalse(report["handshake_complete"])
        self.assertEqual(report["requested_receive_buffer"], 4 * 1024 * 1024)
        self.assertEqual(report["actual_receive_buffer"], 425984)
        self.assertTrue(
            any("synthetic handshake timeout" in item for item in report["failures"])
        )
        self.assertTrue(
            any(
                "pcap" in item for item in report["scope"]["requires_external_evidence"]
            )
        )

    def test_missing_interface_is_rejected_without_network_changes(self):
        with mock.patch.object(stress.socket, "if_nametoindex", side_effect=OSError):
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                stress.read_interface_ipv4("missing0")

    def test_invalid_cli_parameters_fail_before_opening_a_socket(self):
        argv = ["cslp_lan_stress.py", "--local-port", "0"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("sys.stderr", new=io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                stress.parse_args()

    def test_nonzero_calibration_cli_requires_real_adc_scale_and_offset(self):
        invalid_commands = (
            ["cslp_lan_stress.py", "--expected-calibration-id", "17"],
            [
                "cslp_lan_stress.py",
                "--source-mode",
                "real-adc",
                "--expected-calibration-id",
                "17",
            ],
        )
        for argv in invalid_commands:
            with self.subTest(argv=argv):
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch("sys.stderr", new=io.StringIO()),
                ):
                    with self.assertRaises(SystemExit):
                        stress.parse_args()


if __name__ == "__main__":
    unittest.main()
