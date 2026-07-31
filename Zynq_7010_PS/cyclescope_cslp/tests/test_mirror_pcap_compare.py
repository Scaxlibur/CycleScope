import importlib.util
from pathlib import Path
import sys
import unittest


TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
MODULE_PATH = TOOLS / "cslp_mirror_pcap_compare.py"
SPEC = importlib.util.spec_from_file_location("cslp_mirror_pcap_compare", MODULE_PATH)
compare = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = compare
SPEC.loader.exec_module(compare)


def message(sequence: int) -> bytes:
    return compare.stress.build_message(
        compare.stress.MSG_STATUS,
        0x12345678,
        sequence,
        bytes(40),
        timestamp_us=sequence,
    )


class MirrorPcapCompareTests(unittest.TestCase):
    def test_identical_payload_sequences_pass_with_equal_digest(self):
        primary = [message(1), message(2)]
        result = compare.compare_payload_sequences(primary, list(primary))
        self.assertTrue(result["pass"])
        self.assertEqual(result["primary_sha256"], result["mirror_sha256"])
        self.assertEqual(result["primary_message_types"], {"0x10": 2})

    def test_count_and_byte_mismatches_fail_closed(self):
        first = message(1)
        second = message(2)
        count = compare.compare_payload_sequences([first, second], [first])
        self.assertFalse(count["pass"])
        self.assertIn("payload count mismatch", count["failures"][0])

        changed = compare.compare_payload_sequences([first], [second])
        self.assertFalse(changed["pass"])
        self.assertEqual(changed["first_mismatch_index"], 0)

    def test_empty_sequences_fail(self):
        result = compare.compare_payload_sequences([], [])
        self.assertFalse(result["pass"])
        self.assertEqual(len(result["failures"]), 2)


if __name__ == "__main__":
    unittest.main()
