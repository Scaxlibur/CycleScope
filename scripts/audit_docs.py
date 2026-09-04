#!/usr/bin/env python3
"""Check deterministic Markdown links and structure with the standard library."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


DOCUMENT_ROOTS = ("docs", "final_doc", "Zynq_7010_PL/docs")
ROOT_DOCUMENTS = (
    "README.md",
    "Zynq_7010_PL/README.md",
    "Zynq_7010_PS/cyclescope_cslp/README.md",
    "Zynq_7010_PS/cyclescope_cslp/scripts/JTAG_DOWNLOAD.md",
)
EXCLUDED_PREFIXES = (
    "docs/evidence/",
    "docs/主办方答疑/",
    "docs/G题_周期信号测量分析装置/",
    "docs/AD9226_2CH模块使用手册_V1.0/",
    "docs/ESP32-P4-Function-EV-Board v1.4 - ESP32-P4 - — esp-dev-kits latest 文档-0e31eb87-16e0-4fc4-9f05-805a80640e07/",
    "docs/Z7-Nano 用户手册 — 微相科技FPGA用户手册 V1.0 文档-11c1ee4a-297f-4d5d-85f3-7d32378c2531/",
)
LONG_PAGE_LINES = 600

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HTML_ID_RE = re.compile(r"<(?:a\s+name|[^>]+\sid)=[\"']([^\"']+)[\"']", re.IGNORECASE)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)


@dataclass(frozen=True)
class Heading:
    level: int
    title: str
    line: int


@dataclass(frozen=True)
class Document:
    path: Path
    relative: Path
    lines: tuple[str, ...]
    visible_lines: tuple[tuple[int, str], ...]
    headings: tuple[Heading, ...]
    anchors: frozenset[str]


@dataclass(frozen=True)
class Finding:
    level: str
    path: str
    line: int
    message: str


def repository_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def is_excluded(relative: Path) -> bool:
    value = relative.as_posix()
    return any(value.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def discover_markdown(root: Path) -> list[Path]:
    paths = {root / name for name in ROOT_DOCUMENTS if (root / name).is_file()}
    for directory in DOCUMENT_ROOTS:
        base = root / directory
        if base.is_dir():
            paths.update(base.rglob("*.md"))
    return sorted(path for path in paths if path.is_file() and not is_excluded(path.relative_to(root)))


def visible_markdown_lines(lines: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    visible: list[tuple[int, str]] = []
    fence_char = ""
    fence_length = 0
    for number, line in enumerate(lines, 1):
        match = FENCE_RE.match(line)
        if fence_char:
            if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_length:
                fence_char = ""
                fence_length = 0
            continue
        if match:
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            continue
        visible.append((number, line))
    return tuple(visible)


def heading_slug(title: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", title.casefold())
    text = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", text)
    slug: list[str] = []
    for character in text:
        if character.isspace():
            slug.append("-")
        elif character in {"-", "_"} or not unicodedata.category(character).startswith(("P", "S")):
            slug.append(character)
    return re.sub(r"-+", "-", "".join(slug)).strip("-")


def markdown_structure(
    visible_lines: tuple[tuple[int, str], ...],
) -> tuple[tuple[Heading, ...], frozenset[str]]:
    headings: list[Heading] = []
    anchors: set[str] = set()
    seen: defaultdict[str, int] = defaultdict(int)
    for number, line in visible_lines:
        match = HEADING_RE.match(line)
        if match:
            title = match.group(2).strip()
            headings.append(Heading(len(match.group(1)), title, number))
            base = heading_slug(title)
            suffix = seen[base]
            anchors.add(base if suffix == 0 else f"{base}-{suffix}")
            seen[base] += 1
        anchors.update(unquote(value).casefold() for value in HTML_ID_RE.findall(line))
    return tuple(headings), frozenset(anchors)


def load_document(path: Path, root: Path) -> Document:
    lines = tuple(path.read_text(encoding="utf-8").splitlines())
    visible = visible_markdown_lines(lines)
    headings, anchors = markdown_structure(visible)
    return Document(path, path.relative_to(root), lines, visible, headings, anchors)


def markdown_links(document: Document) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for number, line in document.visible_lines:
        line = INLINE_CODE_RE.sub("", line)
        links.extend((number, match.group(1).strip("<>")) for match in LINK_RE.finditer(line))
    return links


def inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def check_links(
    documents: list[Document], root: Path
) -> tuple[list[Finding], dict[Path, set[Path]]]:
    findings: list[Finding] = []
    inbound: dict[Path, set[Path]] = defaultdict(set)
    by_path = {document.path.resolve(): document for document in documents}

    for document in documents:
        for line, destination in markdown_links(document):
            parsed = urlsplit(destination)
            if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
                continue
            target = document.path if not parsed.path else document.path.parent / unquote(parsed.path)
            target = target.resolve()
            if not inside(target, root):
                findings.append(
                    Finding("error", document.relative.as_posix(), line, f"relative link escapes repository: {destination}")
                )
                continue
            if not target.exists():
                findings.append(
                    Finding("error", document.relative.as_posix(), line, f"relative link target does not exist: {destination}")
                )
                continue
            target_document = by_path.get(target)
            if target_document:
                inbound[target].add(document.path.resolve())
                if parsed.fragment and unquote(parsed.fragment).casefold() not in target_document.anchors:
                    findings.append(
                        Finding("warning", document.relative.as_posix(), line, f"anchor was not found: {destination}")
                    )
    return findings, inbound


def check_structure(
    documents: list[Document], inbound: dict[Path, set[Path]], max_lines: int
) -> list[Finding]:
    findings: list[Finding] = []
    for document in documents:
        h1 = [heading for heading in document.headings if heading.level == 1]
        if not h1:
            findings.append(Finding("warning", document.relative.as_posix(), 1, "page has no ATX H1"))
        elif len(h1) > 1:
            findings.append(Finding("warning", document.relative.as_posix(), h1[1].line, "page has more than one H1"))

        if len(document.lines) > max_lines and "history" not in document.relative.parts:
            findings.append(
                Finding(
                    "warning",
                    document.relative.as_posix(),
                    1,
                    f"long page: {len(document.lines)} lines (threshold {max_lines})",
                )
            )

        is_entry = document.relative.name == "README.md"
        is_compatibility = any(
            heading.level == 1 and "旧入口" in heading.title for heading in document.headings
        )
        if not is_entry and not is_compatibility and not inbound.get(document.path.resolve()):
            findings.append(Finding("warning", document.relative.as_posix(), 1, "orphan Markdown page: no inbound link"))
    return findings


def audit(root: Path, max_lines: int = LONG_PAGE_LINES) -> tuple[list[Document], list[Finding]]:
    documents = [load_document(path, root) for path in discover_markdown(root)]
    link_findings, inbound = check_links(documents, root)
    findings = [*link_findings, *check_structure(documents, inbound, max_lines)]
    findings.sort(key=lambda item: (item.path.casefold(), item.line, item.level, item.message))
    return documents, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="repository root; defaults to the current Git worktree")
    parser.add_argument("--max-lines", type=int, default=LONG_PAGE_LINES)
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--quiet-warnings", action="store_true")
    args = parser.parse_args(argv)

    try:
        root = args.root.resolve() if args.root else repository_root(Path.cwd())
        documents, findings = audit(root, args.max_lines)
    except (OSError, UnicodeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: unable to audit documentation: {error}", file=sys.stderr)
        return 2

    for finding in findings:
        if not (args.quiet_warnings and finding.level == "warning"):
            print(f"{finding.level.upper()}: {finding.path}:{finding.line}: {finding.message}")
    errors = sum(item.level == "error" for item in findings)
    warnings = sum(item.level == "warning" for item in findings)
    print(f"SUMMARY: {len(documents)} Markdown files, {errors} errors, {warnings} warnings")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
