#!/usr/bin/env python3
"""Rebuild the CycleScope standalone platform and application with Vitis 2025.1."""

from pathlib import Path
import os
import re
import shutil

import vitis


ROOT = Path(__file__).resolve().parents[1]
FPGA_ROOT = ROOT.parents[1]
XSA = FPGA_ROOT / "Zynq_7010_PL" / "build" / "system" / "hardware" / "cyclescope_system.xsa"
BUILD = ROOT / "build" / "vitis"
WORKSPACE = BUILD / "workspace"
PLATFORM_NAME = "cyclescope_platform"
DOMAIN_NAME = "standalone_ps7_cortexa9_0"
APP_NAME = "cyclescope_cslp_app"


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
        "lwip220_mem_size": "262144",
        "lwip220_memp_n_pbuf": "128",
        "lwip220_pbuf_pool_bufsize": "1700",
        "lwip220_pbuf_pool_size": "128",
        "lwip220_memp_n_udp_pcb": "4",
    }
    for key, value in settings.items():
        domain.set_config("lib", key, value, "lwip220")


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
        "LWIP_UDP": "1",
        "LWIP_DHCP": "0",
        "LWIP_IPV6": "0",
        "LWIP_FULL_CSUM_OFFLOAD_RX": "1",
        "LWIP_FULL_CSUM_OFFLOAD_TX": "1",
    }
    for name, value in required.items():
        pattern = rf"(?m)^\s*#define\s+{re.escape(name)}\s+{value}\s*$"
        if re.search(pattern, options) is None:
            raise RuntimeError(f"generated lwIP option mismatch: {name} != {value}")
    for name in ("IP_FRAG", "IP_REASSEMBLY"):
        if re.search(rf"(?m)^\s*#define\s+{name}\b", options) is not None:
            raise RuntimeError(f"generated lwIP unexpectedly enables {name}")

    print(f"VITIS_LWIP_OPTIONS={candidates[0]}")
    print("VITIS_LWIP_CONFIG_PASS")


def import_sources(app):
    app.import_files(
        from_loc=str(ROOT / "include"),
        files=["cslp_protocol.h", "cslp_control.h", "cslp_frame_pool.h"],
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
        app.set_app_config(
            key="USER_LINK_LIBRARIES", values="lwip220;xiltimer"
        )
        app.build()

        elf_candidates = list(Path(app.component_location).rglob("*.elf"))
        if not elf_candidates:
            raise RuntimeError("Vitis reported success but no ELF was produced")
        print(f"VITIS_APP_ELF={elf_candidates[0]}")
        print("VITIS_BUILD_PASS")
    finally:
        vitis.dispose()


if __name__ == "__main__":
    main()
