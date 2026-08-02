import importlib.util
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "cslp_lan_stress.py"
SPEC = importlib.util.spec_from_file_location("cslp_lan_stress_capture", MODULE_PATH)
stress = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = stress
SPEC.loader.exec_module(stress)


class AdcCaptureEvidenceTests(unittest.TestCase):
    def test_complete_frame_is_saved_with_hash_and_metadata(self):
        samples = tuple((index % 4096) - 2048 for index in range(stress.FRAME_SAMPLES))
        raw = struct.pack(f"<{stress.FRAME_SAMPLES}h", *samples)
        metadata = (
            1_234_567,
            stress.CHUNK_COUNT,
            stress.SAMPLE_FORMAT_S16_LE,
            stress.CHANNEL_COUNT,
            stress.SAMPLE_RATE_HZ,
            stress.FRAME_SAMPLES,
            488,
            -12,
            7,
            stress.FILTER_PROFILE,
            3,
            stress.FLAG_FILTERED,
        )
        assembly = stress.FrameAssembly(
            frame_id=42,
            shared_metadata=metadata,
            first_arrival_ns=1,
            last_arrival_ns=2,
        )

        with TemporaryDirectory() as temporary:
            client = stress.StressClient.__new__(stress.StressClient)
            client.capture_dir = Path(temporary)
            client.capture_frames = []

            client.record_capture_frame(assembly, raw)

            self.assertEqual(len(client.capture_frames), 1)
            record = client.capture_frames[0]
            frame_path = Path(temporary) / str(record["file"])
            self.assertEqual(frame_path.read_bytes(), raw)
            self.assertEqual(record["frame_id"], 42)
            self.assertEqual(record["timestamp_us"], 1_234_567)
            self.assertEqual(record["sample_count"], stress.FRAME_SAMPLES)
            self.assertEqual(record["scale_uv_per_lsb"], 488)
            self.assertEqual(record["offset_uv"], -12)
            self.assertEqual(record["calibration_id"], 3)
            self.assertEqual(
                record["sha256"],
                stress.hashlib.sha256(raw).hexdigest(),
            )

    def test_overrange_policy_is_explicit_and_cross_checked(self):
        self.assertEqual(stress.overrange_policy_failures("reject", 0, 0), [])
        self.assertTrue(stress.overrange_policy_failures("reject", 1, 1))
        self.assertTrue(stress.overrange_policy_failures("require", 0, 0))
        self.assertEqual(stress.overrange_policy_failures("require", 2, 2), [])
        self.assertEqual(stress.overrange_policy_failures("allow", 0, 0), [])
        self.assertTrue(stress.overrange_policy_failures("allow", 2, 1))


if __name__ == "__main__":
    unittest.main()
