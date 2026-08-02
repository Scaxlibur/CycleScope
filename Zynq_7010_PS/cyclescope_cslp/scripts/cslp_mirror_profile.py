"""Parse and freeze the compile-time CSLP diagnostic mirror configuration."""

from dataclasses import dataclass
from typing import Mapping


LOCAL_IPV4_LAST_OCTET = 2
LOCAL_UDP_PORT = 50000
PRIMARY_UDP_PORT = 50001


@dataclass(frozen=True)
class MirrorProfile:
    enabled: bool
    ipv4_last_octet: int
    udp_port: int

    def compile_definitions(self) -> str:
        return (
            f"CSLP_MIRROR_ENABLED={1 if self.enabled else 0};"
            f"CSLP_MIRROR_IPV4_LAST_OCTET={self.ipv4_last_octet};"
            f"CSLP_MIRROR_UDP_PORT={self.udp_port}U"
        )

    def log_value(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return (
            f"{state}:{1 if self.enabled else 0} "
            f"destination:192.168.10.{self.ipv4_last_octet}:{self.udp_port}"
        )


def _decimal(environment: Mapping[str, str], name: str, default: str) -> int:
    value = environment.get(name, default)
    try:
        return int(value, 10)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be a decimal integer") from error


def load_mirror_profile(
    environment: Mapping[str, str], peer_ipv4_last_octet: int
) -> MirrorProfile:
    enabled_text = environment.get("CSLP_MIRROR_ENABLED", "0")
    if enabled_text not in ("0", "1"):
        raise RuntimeError("CSLP_MIRROR_ENABLED must be 0 or 1")

    ipv4_last_octet = _decimal(
        environment, "CSLP_MIRROR_IPV4_LAST_OCTET", "4"
    )
    udp_port = _decimal(environment, "CSLP_MIRROR_UDP_PORT", "50002")
    if not 1 <= ipv4_last_octet <= 254:
        raise RuntimeError("CSLP_MIRROR_IPV4_LAST_OCTET must be in 1..254")
    if not 1 <= udp_port <= 65535:
        raise RuntimeError("CSLP_MIRROR_UDP_PORT must be in 1..65535")

    enabled = enabled_text == "1"
    if enabled and ipv4_last_octet in (
        LOCAL_IPV4_LAST_OCTET,
        peer_ipv4_last_octet,
    ):
        raise RuntimeError(
            "enabled mirror IPv4 must differ from local and primary peer"
        )
    if enabled and udp_port in (LOCAL_UDP_PORT, PRIMARY_UDP_PORT):
        raise RuntimeError(
            "enabled mirror UDP port must differ from 50000 and 50001"
        )
    return MirrorProfile(enabled, ipv4_last_octet, udp_port)
