import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = (
    Path(__file__).parents[1] / "scripts" / "cslp_mirror_profile.py"
)
SPEC = importlib.util.spec_from_file_location("cslp_mirror_profile", MODULE_PATH)
mirror = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = mirror
SPEC.loader.exec_module(mirror)


class MirrorProfileTests(unittest.TestCase):
    def test_defaults_are_disabled_but_fully_defined(self):
        profile = mirror.load_mirror_profile({}, 3)
        self.assertFalse(profile.enabled)
        self.assertEqual(profile.ipv4_last_octet, 4)
        self.assertEqual(profile.udp_port, 50002)
        self.assertEqual(
            profile.compile_definitions(),
            "CSLP_MIRROR_ENABLED=0;"
            "CSLP_MIRROR_IPV4_LAST_OCTET=4;"
            "CSLP_MIRROR_UDP_PORT=50002U",
        )

    def test_enabled_profile_is_hashable_and_logged_explicitly(self):
        profile = mirror.load_mirror_profile(
            {
                "CSLP_MIRROR_ENABLED": "1",
                "CSLP_MIRROR_IPV4_LAST_OCTET": "4",
                "CSLP_MIRROR_UDP_PORT": "50002",
            },
            3,
        )
        self.assertTrue(profile.enabled)
        self.assertEqual(
            profile.log_value(),
            "enabled:1 destination:192.168.10.4:50002",
        )
        self.assertEqual(len({profile}), 1)

    def test_boolean_is_exact_and_empty_is_rejected(self):
        for value in ("", "true", "01", "2"):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "CSLP_MIRROR_ENABLED"
            ):
                mirror.load_mirror_profile({"CSLP_MIRROR_ENABLED": value}, 3)

    def test_numeric_fields_are_strict_decimal_and_bounded(self):
        cases = (
            ("CSLP_MIRROR_IPV4_LAST_OCTET", "0"),
            ("CSLP_MIRROR_IPV4_LAST_OCTET", "255"),
            ("CSLP_MIRROR_IPV4_LAST_OCTET", "0x04"),
            ("CSLP_MIRROR_UDP_PORT", "0"),
            ("CSLP_MIRROR_UDP_PORT", "65536"),
            ("CSLP_MIRROR_UDP_PORT", "0xc352"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(RuntimeError):
                mirror.load_mirror_profile({name: value}, 3)

    def test_enabled_address_and_port_conflicts_are_rejected(self):
        for name, value in (
            ("CSLP_MIRROR_IPV4_LAST_OCTET", "2"),
            ("CSLP_MIRROR_IPV4_LAST_OCTET", "3"),
            ("CSLP_MIRROR_UDP_PORT", "50000"),
            ("CSLP_MIRROR_UDP_PORT", "50001"),
        ):
            environment = {"CSLP_MIRROR_ENABLED": "1", name: value}
            with self.subTest(name=name, value=value), self.assertRaises(RuntimeError):
                mirror.load_mirror_profile(environment, 3)

    def test_disabled_profile_does_not_block_alternate_peer_builds(self):
        profile = mirror.load_mirror_profile(
            {
                "CSLP_MIRROR_ENABLED": "0",
                "CSLP_MIRROR_IPV4_LAST_OCTET": "4",
                "CSLP_MIRROR_UDP_PORT": "50001",
            },
            4,
        )
        self.assertFalse(profile.enabled)


if __name__ == "__main__":
    unittest.main()
