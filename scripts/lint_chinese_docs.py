#!/usr/bin/env python3
"""Report a small deterministic subset of Chinese documentation style issues."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from audit_docs import discover_markdown, repository_root, visible_markdown_lines


INLINE_CODE_RE = re.compile(r"`[^`]*`")
HTML_TAG_RE = re.compile(r"<[^>]+>")
LINK_TARGET_RE = re.compile(r"(\[[^\]]*\])\([^)]+\)")
CURVED_QUOTES_RE = re.compile(r"[“”]")
SECOND_PERSON_RE = re.compile(r"(?<![A-Za-z])(?:你|您|同学)(?![A-Za-z])")
TERMS = {
    "json": "JSON",
    "http": "HTTP",
    "url": "URL",
    "api": "API",
    "ai": "AI",
}


def visible_prose(line: str) -> str:
    line = LINK_TARGET_RE.sub(r"\1", line)
    line = INLINE_CODE_RE.sub("", line)
    return HTML_TAG_RE.sub("", line)


def lint_line(line: str) -> list[str]:
    prose = visible_prose(line)
    messages: list[str] = []
    if CURVED_QUOTES_RE.search(prose):
        messages.append("中文正文使用弯引号；改用直角引号「」")
    if SECOND_PERSON_RE.search(prose):
        messages.append("面向读者的正文使用第二人称")
    for source, target in TERMS.items():
        if re.search(rf"(?<![A-Za-z]){source}(?![A-Za-z])", prose):
            messages.append(f"可见术语 {source!r} 建议写为 {target!r}")
    return messages


def main() -> int:
    try:
        root = repository_root(Path.cwd())
        findings: list[tuple[str, int, str]] = []
        for path in discover_markdown(root):
            lines = tuple(path.read_text(encoding="utf-8").splitlines())
            for number, line in visible_markdown_lines(lines):
                findings.extend(
                    (path.relative_to(root).as_posix(), number, message)
                    for message in lint_line(line)
                )
    except (OSError, UnicodeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: unable to lint Chinese documentation: {error}", file=sys.stderr)
        return 2

    for path, line, message in findings:
        print(f"WARNING: {path}:{line}: {message}")
    print(f"SUMMARY: Chinese copy warnings={len(findings)} (non-blocking)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
