import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "cslp_test_pattern_analyze.py"
SPEC = importlib.util.spec_from_file_location("cslp_test_pattern_analyze", MODULE_PATH)
pattern = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pattern
SPEC.loader.exec_module(pattern)


@unittest.skipIf(pattern.np is None, "WaveBench NumPy environment is not active")
class TestPatternAnalysisTests(unittest.TestCase):
    def write_evidence(self, root, frames):
        capture = root / "capture"
        capture.mkdir()
        records = []
        for index, values in enumerate(frames):
            raw = pattern.np.asarray(values, dtype="<i2").tobytes()
            name = f"frame_{index:05d}_{index + 1:08x}.s16le"
            (capture / name).write_bytes(raw)
            records.append(
                {
                    "frame_index": index,
                    "frame_id": index + 1,
                    "timestamp_us": 1_000_000 + index * 50_000,
                    "file": name,
                    "frame_bytes": len(raw),
                    "sample_count": pattern.FRAME_SAMPLES,
                    "sample_rate_hz": pattern.SAMPLE_RATE_HZ,
                    "scale_uv_per_lsb": 488,
                    "offset_uv": 0,
                    "config_id": 7,
                    "filter_profile": 1,
                    "calibration_id": 0,
                    "frame_flags": pattern.FLAG_FILTERED | pattern.FLAG_TEST_PATTERN,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        manifest = {
            "format": "CycleScope CSLP independent complete frames v1",
            "sample_encoding": "S16_LE",
            "frame_count": len(records),
            "frame_samples": pattern.FRAME_SAMPLES,
            "sample_rate_hz": pattern.SAMPLE_RATE_HZ,
            "source_mode": "test-pattern",
            "activity_policy": "require",
            "overrange_policy": "reject",
            "expected_test_faults": 0,
            "session_id": 9,
            "device_boot_id": 10,
            "config_id": 7,
            "frames": records,
            "partial": False,
        }
        (capture / "capture.json").write_text(json.dumps(manifest), encoding="utf-8")
        lan = root / "lan.json"
        lan.write_text(
            json.dumps(
                {
                    "pass": True,
                    "source_mode": "test-pattern",
                    "expected_test_faults": 0,
                    "session_id": 9,
                    "device_boot_id": 10,
                    "config_id": 7,
                    "capture": {
                        "directory": str(capture.resolve()),
                        "frame_count": len(records),
                        "partial": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return capture, lan

    def analyze(self, root, mode, frames, amplitude=1600, coherent_bin=256):
        capture, lan = self.write_evidence(root, frames)
        return pattern.analyze(
            SimpleNamespace(
                mode=mode,
                amplitude=amplitude,
                coherent_bin=coherent_bin,
                capture=capture,
                lan_report=lan,
            )
        )

    def test_ramp_full_range_stride_and_wrap_pass(self):
        np = pattern.np
        base = np.arange(pattern.FRAME_SAMPLES)
        frames = [((base * 16 + phase + 2048) % 4096) - 2048 for phase in (0, 7)]
        with TemporaryDirectory() as temporary:
            report = self.analyze(Path(temporary), "ramp", frames, amplitude=2047)
        self.assertTrue(report["pass"], report["failures"])
        self.assertGreaterEqual(report["metrics"]["wrap_count"], 2)

    def test_coherent_sine_bin_and_amplitude_pass(self):
        np = pattern.np
        index = np.arange(pattern.FRAME_SAMPLES)
        frames = [
            np.rint(1600 * np.sin(2 * np.pi * 256 * index / pattern.FRAME_SAMPLES + phase))
            for phase in (0.0, 0.3, 1.1)
        ]
        with TemporaryDirectory() as temporary:
            report = self.analyze(Path(temporary), "sine", frames)
        self.assertTrue(report["pass"], report["failures"])
        self.assertEqual(report["metrics"]["strongest_bin"], 256)

    def test_fixed_multitone_bins_and_weights_pass(self):
        np = pattern.np
        index = np.arange(pattern.FRAME_SAMPLES)
        values = sum(
            1600 * weight * np.sin(2 * np.pi * bin_index * index / pattern.FRAME_SAMPLES)
            for bin_index, weight in zip(
                pattern.MULTITONE_BINS, pattern.MULTITONE_WEIGHTS, strict=True
            )
        )
        frames = [np.rint(values)] * 3
        with TemporaryDirectory() as temporary:
            report = self.analyze(Path(temporary), "multitone", frames)
        self.assertTrue(report["pass"], report["failures"])
        self.assertEqual(tuple(report["metrics"]["strongest_bins"]), pattern.MULTITONE_BINS)

    def test_wrong_sine_bin_fails_closed(self):
        np = pattern.np
        index = np.arange(pattern.FRAME_SAMPLES)
        frames = [
            np.rint(1600 * np.sin(2 * np.pi * 255 * index / pattern.FRAME_SAMPLES))
        ] * 2
        with TemporaryDirectory() as temporary:
            report = self.analyze(Path(temporary), "sine", frames)
        self.assertFalse(report["pass"])
        self.assertTrue(any("strongest bin" in item for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
