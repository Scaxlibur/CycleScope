import importlib.util
import ipaddress
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "cslp_pcap_analyze.py"
SPEC = importlib.util.spec_from_file_location("cslp_pcap_analyze", MODULE_PATH)
pcap = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pcap
SPEC.loader.exec_module(pcap)


SOURCE_IP = ipaddress.IPv4Address("192.168.10.2")
DESTINATION_IP = ipaddress.IPv4Address("192.168.10.4")


def checksum(data):
    total = pcap.checksum_sum(data)
    value = (~total) & 0xFFFF
    return value or 0xFFFF


def build_udp_frame(*, udp_checksum="valid", fragment_field=0, message_type=0x20):
    payload = b"CSLP" + bytes((1, message_type)) + bytes(26)
    udp_length = 8 + len(payload)
    udp = bytearray(struct.pack(">HHHH", 50000, 50001, udp_length, 0) + payload)
    pseudo = (
        SOURCE_IP.packed
        + DESTINATION_IP.packed
        + bytes((0, 17))
        + struct.pack(">H", udp_length)
    )
    if udp_checksum == "valid":
        struct.pack_into(">H", udp, 6, checksum(pseudo + udp))
    elif udp_checksum == "bad":
        struct.pack_into(">H", udp, 6, 1)
    elif udp_checksum != "zero":
        raise ValueError(udp_checksum)

    total_length = 20 + len(udp)
    ip_header = bytearray(
        struct.pack(
            ">BBHHHBBH4s4s",
            0x45,
            0,
            total_length,
            7,
            fragment_field,
            64,
            17,
            0,
            SOURCE_IP.packed,
            DESTINATION_IP.packed,
        )
    )
    struct.pack_into(">H", ip_header, 10, checksum(ip_header))
    ethernet = bytes.fromhex("00112233445566778899aabb0800")
    return ethernet + ip_header + udp


def write_pcap(path, frames):
    data = bytearray()
    data += struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
    for index, frame in enumerate(frames):
        data += struct.pack("<IIII", 1 + index, 500, len(frame), len(frame))
        data += frame
    path.write_bytes(data)


def write_lan_report(path, wave_packets=1, passed=True):
    path.write_text(
        json.dumps({"pass": passed, "counters": {"wave_packets": wave_packets}}),
        encoding="utf-8",
    )


def write_tcpdump_log(path, dropped=0):
    path.write_text(
        f"1 packets captured\n1 packets received by filter\n"
        f"{dropped} packets dropped by kernel\n",
        encoding="utf-8",
    )


class PcapAnalyzeTests(unittest.TestCase):
    def analyze(self, root, frame, *, expected_packets=1, dropped=0):
        capture = root / "capture.pcap"
        lan = root / "lan.json"
        tcpdump = root / "tcpdump.log"
        write_pcap(capture, [frame])
        write_lan_report(lan, expected_packets)
        write_tcpdump_log(tcpdump, dropped)
        return pcap.analyze(
            capture,
            SOURCE_IP,
            DESTINATION_IP,
            50000,
            50001,
            lan,
            tcpdump,
        )

    def test_valid_nonzero_checksum_and_matching_wave_count_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.analyze(Path(temporary), build_udp_frame())
        self.assertTrue(report["pass"])
        self.assertEqual(report["counts"]["target_udp_checksum_valid"], 1)
        self.assertEqual(report["counts"]["target_wave_packets"], 1)
        self.assertEqual(report["counts"]["source_udp_fragments"], 0)

    def test_zero_and_bad_udp_checksums_fail(self):
        for mode, counter in (
            ("zero", "target_udp_checksum_zero"),
            ("bad", "target_udp_checksum_bad"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                report = self.analyze(
                    Path(temporary), build_udp_frame(udp_checksum=mode)
                )
            self.assertFalse(report["pass"])
            self.assertEqual(report["counts"][counter], 1)

    def test_ipv4_fragment_fails_even_when_udp_header_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.analyze(
                Path(temporary), build_udp_frame(fragment_field=0x2000)
            )
        self.assertFalse(report["pass"])
        self.assertEqual(report["counts"]["source_udp_fragments"], 1)

    def test_lan_wave_count_mismatch_and_tcpdump_drop_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.analyze(
                Path(temporary), build_udp_frame(), expected_packets=2, dropped=1
            )
        self.assertFalse(report["pass"])
        self.assertTrue(
            any("pcap/LAN WAVE packet mismatch" in item for item in report["failures"])
        )
        self.assertTrue(
            any("packets_dropped_by_kernel=1" in item for item in report["failures"])
        )

    def test_truncated_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture.pcap"
            frame = build_udp_frame()
            data = bytearray(
                struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
            )
            data += struct.pack("<IIII", 1, 0, len(frame) - 1, len(frame))
            data += frame[:-1]
            capture.write_bytes(data)
            with self.assertRaisesRegex(pcap.PcapError, "truncated by snaplen"):
                list(pcap.pcap_packets(capture))


if __name__ == "__main__":
    unittest.main()
