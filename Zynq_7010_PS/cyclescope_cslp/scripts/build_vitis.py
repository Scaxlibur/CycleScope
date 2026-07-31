#!/usr/bin/env python3
"""Rebuild the CycleScope standalone platform and application with Vitis 2025.1."""

from pathlib import Path
import os
import re
import shutil
import subprocess

import vitis

from cslp_calibration_profile import load_calibration_profile
from cslp_mirror_profile import MirrorProfile, load_mirror_profile


ROOT = Path(__file__).resolve().parents[1]
FPGA_ROOT = ROOT.parents[1]
XSA = FPGA_ROOT / "Zynq_7010_PL" / "build" / "system" / "hardware" / "cyclescope_system.xsa"
BUILD = ROOT / "build" / "vitis"
WORKSPACE = BUILD / "workspace"
PLATFORM_NAME = "cyclescope_platform"
DOMAIN_NAME = "standalone_ps7_cortexa9_0"
APP_NAME = "cyclescope_cslp_app"


def calibration_definitions():
    manifest_text = os.environ.get("CSLP_CALIBRATION_MANIFEST")
    if not manifest_text:
        print("VITIS_CALIBRATION=uncalibrated id:0 scale_uv_per_lsb:488 offset_uv:0")
        return (
            "CSLP_WAVE_SCALE_UV_PER_LSB=488U;"
            "CSLP_WAVE_OFFSET_UV=0;"
            "CSLP_WAVE_CALIBRATION_ID=0U"
        )

    manifest = Path(manifest_text).expanduser()
    if not manifest.is_absolute():
        manifest = ROOT / manifest
    profile = load_calibration_profile(manifest)
    print(
        "VITIS_CALIBRATION="
        f"validated id:{profile.calibration_id} "
        f"scale_uv_per_lsb:{profile.scale_uv_per_lsb} "
        f"offset_uv:{profile.offset_uv} "
        f"manifest_sha256:{profile.manifest_sha256}"
    )
    for artifact in profile.artifact_records:
        print(
            "VITIS_CALIBRATION_ARTIFACT="
            f"{artifact['name']}:{artifact['sha256']}:{artifact['size']}"
        )
    return profile.compile_definitions()


def diagnostic_definitions() -> tuple[str, MirrorProfile]:
    test_pattern = os.environ.get("CSLP_TEST_PATTERN", "0")
    if test_pattern not in ("0", "1"):
        raise RuntimeError("CSLP_TEST_PATTERN must be 0 or 1")

    peer_octet_text = os.environ.get("CSLP_PEER_IPV4_LAST_OCTET", "3")
    try:
        peer_octet = int(peer_octet_text, 10)
    except ValueError as error:
        raise RuntimeError(
            "CSLP_PEER_IPV4_LAST_OCTET must be an integer"
        ) from error
    if not 1 <= peer_octet <= 254:
        raise RuntimeError("CSLP_PEER_IPV4_LAST_OCTET must be in 1..254")
    mirror = load_mirror_profile(os.environ, peer_octet)

    mode_name = os.environ.get("CSLP_TEST_MODE", "ramp").lower()
    modes = {"ramp": 0, "sine": 1, "multitone": 2}
    if mode_name not in modes:
        raise RuntimeError(
            "CSLP_TEST_MODE must be ramp, sine, or multitone"
        )
    mode = modes[mode_name]

    try:
        amplitude = int(os.environ.get("CSLP_TEST_AMPLITUDE", "2047"), 10)
    except ValueError as error:
        raise RuntimeError("CSLP_TEST_AMPLITUDE must be an integer") from error
    if not 0 <= amplitude <= 2047:
        raise RuntimeError("CSLP_TEST_AMPLITUDE must be in 0..2047")

    try:
        coherent_bin = int(os.environ.get("CSLP_TEST_BIN", "256"), 10)
    except ValueError as error:
        raise RuntimeError("CSLP_TEST_BIN must be an integer") from error
    if not 1 <= coherent_bin <= 1008:
        raise RuntimeError("CSLP_TEST_BIN must be in 1..1008 (<=500 kHz)")
    phase_increment = coherent_bin * 32768

    try:
        fault_mask = int(os.environ.get("CSLP_TEST_FAULTS", "0"), 0)
    except ValueError as error:
        raise RuntimeError("CSLP_TEST_FAULTS must be an integer mask") from error
    if not 0 <= fault_mask <= 7:
        raise RuntimeError("CSLP_TEST_FAULTS must use only bits 0..2")

    print(f"VITIS_TEST_PATTERN={test_pattern}")
    print(
        f"VITIS_TEST_SOURCE=mode:{mode_name} amplitude:{amplitude} "
        f"bin:{coherent_bin} phase_increment:{phase_increment} "
        f"faults:0x{fault_mask:02x}"
    )
    print(f"VITIS_PEER_IPV4=192.168.10.{peer_octet}")
    print(f"VITIS_MIRROR={mirror.log_value()}")
    definitions = (
        f"CSLP_DEFAULT_TEST_PATTERN={test_pattern};"
        f"CSLP_DEFAULT_TEST_MODE={mode};"
        f"CSLP_DEFAULT_TEST_AMPLITUDE={amplitude};"
        f"CSLP_DEFAULT_TEST_PHASE_INCREMENT={phase_increment}U;"
        f"CSLP_DEFAULT_TEST_FAULTS={fault_mask}U;"
        f"CSLP_PEER_IPV4_LAST_OCTET={peer_octet};"
        f"{mirror.compile_definitions()}"
    )
    return definitions, mirror


def configure_lwip(domain):
    domain.set_lib("lwip220")
    settings = {
        "lwip220_api_mode": "RAW_API",
        "lwip220_dhcp": "false",
        "lwip220_ipv6_enable": "false",
        "lwip220_ip_frag": "0",
        "lwip220_ip_reassembly": "0",
        "lwip220_tcp": "false",
        "lwip220_udp": "true",
        "lwip220_udp_block_tx": "false",
        "lwip220_temac_phy_link_speed": "CONFIG_LINKSPEED100",
        "lwip220_mem_size": "262144",
        "lwip220_memp_n_pbuf": "128",
        "lwip220_pbuf_pool_bufsize": "1700",
        "lwip220_pbuf_pool_size": "128",
        "lwip220_memp_n_udp_pcb": "4",
    }
    for key, value in settings.items():
        domain.set_config("lib", key, value, "lwip220")


def patch_lwip_zero_option_template():
    """Make CMake emit explicit zeroes instead of falling back to lwIP defaults."""
    candidates = list(WORKSPACE.rglob("lwipopts.h.in"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one generated lwipopts.h.in, found {len(candidates)}"
        )

    template = candidates[0]
    contents = template.read_text(encoding="utf-8")
    for name in ("IP_FRAG", "IP_REASSEMBLY"):
        old = f"#cmakedefine {name} @{name}@"
        new = f"#define {name} @{name}@"
        if contents.count(old) != 1:
            raise RuntimeError(
                f"unexpected AMD lwipopts template for {name}: {template}"
            )
        contents = contents.replace(old, new)
    template.write_text(contents, encoding="utf-8")
    print(f"VITIS_LWIP_TEMPLATE_PATCHED={template}")


def patch_lwip_gem_frame_limit():
    """Allow a standard 1514-byte Ethernet frame before hardware adds FCS."""
    candidates = list(WORKSPACE.rglob("xemacpsif_dma.c"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one generated xemacpsif_dma.c, found {len(candidates)}"
        )

    source = candidates[0]
    contents = source.read_text(encoding="utf-8")
    replacements = {
        "max_fr_size = MAX_FRAME_SIZE_JUMBO - 18;":
            "max_fr_size = MAX_FRAME_SIZE_JUMBO - XEMACPS_TRL_SIZE;",
        "max_fr_size = XEMACPS_MAX_FRAME_SIZE - 18;":
            "max_fr_size = XEMACPS_MAX_FRAME_SIZE - XEMACPS_TRL_SIZE;",
    }
    for old, new in replacements.items():
        if contents.count(old) != 1:
            raise RuntimeError(
                f"unexpected AMD GEM frame limit in {source}: {old}"
            )
        contents = contents.replace(old, new)
    source.write_text(contents, encoding="utf-8")
    print(f"VITIS_LWIP_GEM_FRAME_LIMIT_PATCHED={source}")


def assert_lwip_configuration():
    candidates = [
        path
        for path in WORKSPACE.rglob("lwipopts.h")
        if path.parent.name == "include" and path.parent.parent.name == "bsp"
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one generated BSP lwipopts.h, found {len(candidates)}"
        )

    options = candidates[0].read_text(encoding="utf-8")
    required = {
        "CONFIG_LINKSPEED100": "1",
        "LWIP_UDP": "1",
        "LWIP_DHCP": "0",
        "LWIP_IPV6": "0",
        "LWIP_FULL_CSUM_OFFLOAD_RX": "1",
        "LWIP_FULL_CSUM_OFFLOAD_TX": "1",
        "IP_FRAG": "0",
        "IP_REASSEMBLY": "0",
    }
    for name, value in required.items():
        pattern = rf"(?m)^\s*#define\s+{re.escape(name)}\s+{value}\s*$"
        if re.search(pattern, options) is None:
            raise RuntimeError(f"generated lwIP option mismatch: {name} != {value}")
    if re.search(
        r"(?m)^\s*#define\s+CONFIG_LINKSPEED_AUTODETECT\b", options
    ) is not None:
        raise RuntimeError("generated lwIP unexpectedly enables PHY speed autodetect")

    print(f"VITIS_LWIP_OPTIONS={candidates[0]}")
    print("VITIS_LWIP_CONFIG_PASS")


def import_sources(app):
    app.import_files(
        from_loc=str(ROOT / "include"),
        files=[
            "cslp_protocol.h",
            "cslp_control.h",
            "cslp_frame_pool.h",
            "cslp_mirror_policy.h",
            "cslp_time.h",
        ],
    )
    app.import_files(
        from_loc=str(ROOT / "src"),
        files=["cslp_protocol.c", "cslp_control.c", "cslp_frame_pool.c"],
    )
    app.import_files(
        from_loc=str(ROOT / "target"),
        files=[
            "main.c",
            "cslp_dma_zynq.c",
            "cslp_dma_zynq.h",
            "cslp_phy_rtl8211f.c",
            "platform.h",
            "platform_config.h",
        ],
    )

    vitis_root = Path(os.environ["XILINX_VITIS"])
    template = (
        vitis_root
        / "data"
        / "embeddedsw"
        / "lib"
        / "sw_apps"
        / "lwip_udp_perf_server"
        / "src"
    )
    if not template.is_dir():
        raise RuntimeError(f"Vitis lwIP template not found: {template}")
    app.import_files(
        from_loc=str(template),
        files=["platform.c", "platform_zynq.c"],
    )


def main():
    if not XSA.is_file():
        raise RuntimeError(f"M4 XSA missing; run 'make -C Zynq_7010_PL system': {XSA}")
    if BUILD.exists():
        shutil.rmtree(BUILD)
    WORKSPACE.mkdir(parents=True)

    client = vitis.create_client()
    try:
        client.set_workspace(str(WORKSPACE))
        platform = client.create_platform_component(
            name=PLATFORM_NAME,
            hw_design=str(XSA),
            os="standalone",
            cpu="ps7_cortexa9_0",
            domain_name=DOMAIN_NAME,
            no_boot_bsp=True,
            generate_dtb=False,
        )
        domain = platform.get_domain(DOMAIN_NAME)
        configure_lwip(domain)
        patch_lwip_zero_option_template()
        patch_lwip_gem_frame_limit()
        platform.build()
        assert_lwip_configuration()

        platform_xpfm = client.find_platform_in_repos(PLATFORM_NAME)
        app = client.create_app_component(
            name=APP_NAME,
            platform=platform_xpfm,
            domain=DOMAIN_NAME,
            template="empty_application",
        )
        import_sources(app)
        diagnostic_compile_definitions, mirror = diagnostic_definitions()
        app.set_app_config(
            key="USER_COMPILE_DEFINITIONS",
            values=(
                f"{diagnostic_compile_definitions};{calibration_definitions()}"
            ),
        )
        app.set_app_config(
            key="USER_LINK_LIBRARIES", values="lwip220;xiltimer"
        )
        app.build()

        elf_candidates = list(Path(app.component_location).rglob("*.elf"))
        if not elf_candidates:
            raise RuntimeError("Vitis reported success but no ELF was produced")
        elf = elf_candidates[0]
        nm = (
            Path(os.environ["XILINX_VITIS"])
            / "gnu"
            / "aarch32"
            / "lin"
            / "gcc-arm-none-eabi"
            / "bin"
            / "arm-none-eabi-nm"
        )
        symbols = subprocess.run(
            [str(nm), "-a", str(elf)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        required_symbols = (
            "cyclescope_rtl8211f_force_100_full",
            "cyclescope_rtl8211f_link_is_ready",
        )
        for required in required_symbols:
            if required not in symbols:
                raise RuntimeError(
                    f"RTL8211F fixed-100 PHY gate is missing from ELF: {required}"
                )
        for forbidden in (
            "get_Realtek_phy_speed",
            "configure_IEEE_phy_speed",
            "rtl8211f_hardware_reset",
        ):
            if forbidden in symbols:
                raise RuntimeError(
                    f"generic lwIP PHY backend unexpectedly linked: {forbidden}"
                )
        mirror_marker = b"CYCLESCOPE_MIRROR enabled"
        marker_present = mirror_marker in elf.read_bytes()
        if marker_present != mirror.enabled:
            raise RuntimeError(
                "ELF mirror identity does not match the compile-time profile"
            )
        print(
            "VITIS_MIRROR_ELF_IDENTITY_PASS="
            f"{'enabled' if marker_present else 'disabled'}"
        )
        print("VITIS_RTL8211F_FIXED_100_BACKEND_PASS")
        print(f"VITIS_APP_ELF={elf}")
        print("VITIS_BUILD_PASS")
    finally:
        vitis.dispose()


if __name__ == "__main__":
    main()
