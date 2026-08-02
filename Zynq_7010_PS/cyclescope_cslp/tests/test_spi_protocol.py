import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "cslp_spi_protocol.py"
SPEC = importlib.util.spec_from_file_location("cslp_spi_protocol", MODULE_PATH)
spi = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = spi
SPEC.loader.exec_module(spi)


def info_payload(frame_id=42, status=0, samples=8192):
    return (
        b"CS"
        + bytes((1, status))
        + frame_id.to_bytes(4, "big")
        + samples.to_bytes(2, "big")
    )


def write_udp_capture(root, raw, frame_id=42):
    frame = root / f"frame_00000_{frame_id:08x}.s16le"
    frame.write_bytes(raw)
    manifest = root / "capture.json"
    manifest.write_text(
        json.dumps(
            {
                "format": "CycleScope CSLP independent complete frames v1",
                "frame_samples": 8192,
                "frames": [
                    {
                        "frame_id": frame_id,
                        "file": frame.name,
                        "frame_bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


class SpiProtocolTests(unittest.TestCase):
    def test_get_info_transfer_and_decode(self):
        self.assertEqual(spi.build_get_info_transfer(), b"\xa0" + bytes(10))
        info = spi.parse_get_info_exchange(b"\x00" + info_payload())
        self.assertEqual(info.magic, "CS")
        self.assertEqual(info.version, 1)
        self.assertEqual(info.frame_id, 42)
        self.assertEqual(info.frame_samples, 8192)

    def test_get_info_rejects_bad_identity_and_size(self):
        cases = (
            b"NO" + info_payload()[2:],
            info_payload(frame_id=0),
            info_payload(samples=32),
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(spi.SpiProtocolError):
                spi.parse_info_payload(payload)

    def test_read_transfer_and_s16le_decode(self):
        transfer = spi.build_read_samples_transfer(start=7, count=2)
        self.assertEqual(transfer[:4], bytes((0xA1, 0, 7, 0)))
        raw = bytes.fromhex("0080ff7f")
        received = bytes(4) + raw
        self.assertEqual(spi.parse_read_samples_exchange(received, 2), raw)
        self.assertEqual(spi.decode_s16le_samples(raw), (-32768, 32767))

    def test_read_rejects_frame_wrap(self):
        with self.assertRaisesRegex(ValueError, "must not wrap"):
            spi.build_read_samples_transfer(start=8191, count=2)

    def test_generation_or_error_status_change_is_rejected(self):
        before = spi.parse_info_payload(info_payload(frame_id=42))
        changed = spi.parse_info_payload(info_payload(frame_id=43))
        invalid = spi.parse_info_payload(info_payload(frame_id=42, status=0x04))
        with self.assertRaisesRegex(spi.SpiProtocolError, "generation changed"):
            spi.require_same_generation(before, changed)
        with self.assertRaisesRegex(spi.SpiProtocolError, "OTR/overflow/drop"):
            spi.require_same_generation(before, invalid)

    def test_same_generation_spi_udp_payload_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = bytes(index & 0xFF for index in range(spi.FRAME_BYTES))
            capture = write_udp_capture(root, raw)
            spi_file = root / "spi.s16le"
            spi_file.write_bytes(raw)
            info = spi.parse_info_payload(info_payload())
            report = spi.compare_spi_to_udp(info, info, spi_file, capture)
        self.assertTrue(report["pass"])
        self.assertEqual(report["spi_sha256"], report["udp_sha256"])

    def test_spi_udp_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = bytes(spi.FRAME_BYTES)
            capture = write_udp_capture(root, raw)
            spi_file = root / "spi.s16le"
            spi_file.write_bytes(b"\x01" + raw[1:])
            info = spi.parse_info_payload(info_payload())
            report = spi.compare_spi_to_udp(info, info, spi_file, capture)
        self.assertFalse(report["pass"])
        self.assertTrue(
            any("mismatch at byte 0" in failure for failure in report["failures"])
        )


if __name__ == "__main__":
    unittest.main()
