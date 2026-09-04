#!/usr/bin/env python3
"""Check that the hand-maintained HTML report acknowledges the Markdown source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


META_RE = re.compile(r'(<meta name="cyclescope-source-sha256" content=")([0-9a-f]{64})(">)')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def abbreviated(value: str) -> str:
    return f"{value[:8]}…{value[-8:]}"


def abbreviation_variants(value: str) -> tuple[str, str]:
    return abbreviated(value), f"{value[:8]}…{value[-7:]}"


def expected_markers(root: Path) -> tuple[tuple[str, ...], ...]:
    calibration = json.loads((root / "docs/evidence/最终标定摘要/calibration.json").read_text(encoding="utf-8"))
    m12 = json.loads(
        (root / "docs/evidence/M12验收摘要/m12-final-completion-audit.json").read_text(encoding="utf-8")
    )
    m8m9 = json.loads(
        (root / "docs/evidence/M8M9最终审计摘要/combined-audit.json").read_text(encoding="utf-8")
    )
    f0 = json.loads(
        (root / "docs/evidence/F0显示版构建烧录证明/release-record.json").read_text(encoding="utf-8")
    )
    max_vpp = round(float(calibration["supported_source_max_vpp_v"]) * 1000)
    return (
        (f"{max_vpp} mVpp",),
        (f'{m12["observed_case_count"]} 个 P4 工程联调观测点',),
        abbreviation_variants(m8m9["build_and_flash"]["app_bin_sha256"]),
        abbreviation_variants(f0["build"]["application_bin_sha256"]),
        ("docs/协议与接口/CSLP-G题采样与处理-Profile-v0.1.md",),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--update", action="store_true", help="update the source hash after manually syncing HTML")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    markdown = root / "final_doc/设计报告.md"
    html = root / "final_doc/设计报告.html"

    try:
        markdown_text = markdown.read_text(encoding="utf-8")
        html_text = html.read_text(encoding="utf-8")
        markers = expected_markers(root)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"ERROR: unable to check report: {error}", file=sys.stderr)
        return 2

    missing = [
        " or ".join(group)
        for group in markers
        if not any(marker in markdown_text and marker in html_text for marker in group)
    ]
    if "public/CSLP-G题采样与处理-Profile-v0.1.md" in html_text:
        missing.append("stale public/ Profile path must be removed")
    if missing:
        for marker in missing:
            print(f"ERROR: report identity marker is not synchronized: {marker}")
        return 1

    expected = digest(markdown)
    matches = list(META_RE.finditer(html_text))
    if len(matches) != 1:
        print("ERROR: HTML must contain exactly one cyclescope-source-sha256 meta tag")
        return 1
    actual = matches[0].group(2)
    if args.update:
        html.write_text(META_RE.sub(rf"\g<1>{expected}\g<3>", html_text), encoding="utf-8")
        print(f"UPDATED: {html.relative_to(root)} source sha256={expected}")
        return 0
    if actual != expected:
        print(f"ERROR: report HTML is stale: source sha256 {actual} != {expected}")
        return 1
    print(f"REPORT_SYNC_PASS: source sha256={expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
