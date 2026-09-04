from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = load_script("audit_docs")
REPORT = load_script("check_report_sync")
COPY = load_script("lint_chinese_docs")


class DocumentationToolTests(unittest.TestCase):
    def test_markdown_parser_ignores_fences_and_builds_duplicate_anchors(self):
        lines = (
            "# 文档 API",
            "[有效](guide.md#运行-check)",
            "```markdown",
            "[忽略](missing.md)",
            "# 也忽略",
            "```",
            "## 运行 `check`",
            "## 运行 `check`",
        )
        visible = AUDIT.visible_markdown_lines(lines)
        headings, anchors = AUDIT.markdown_structure(visible)
        document = AUDIT.Document(Path("README.md"), Path("README.md"), lines, visible, headings, anchors)

        self.assertEqual(
            [heading.title for heading in headings],
            ["文档 API", "运行 `check`", "运行 `check`"],
        )
        self.assertLessEqual({"文档-api", "运行-check", "运行-check-1"}, anchors)
        self.assertEqual(AUDIT.markdown_links(document), [(2, "guide.md#运行-check")])

    def test_link_check_reports_missing_target_escape_and_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            guide = root / "guide.md"
            readme.write_text(
                "# Index\n[missing](missing.md)\n[escape](../outside.md)\n[bad](guide.md#missing)\n[good](guide.md#section)\n",
                encoding="utf-8",
            )
            guide.write_text("# Guide\n## Section\n", encoding="utf-8")
            documents = [AUDIT.load_document(path, root) for path in (readme, guide)]

            findings, inbound = AUDIT.check_links(documents, root)

            self.assertEqual(
                [(item.level, item.line) for item in findings],
                [("error", 2), ("error", 3), ("warning", 4)],
            )
            self.assertEqual(inbound[guide.resolve()], {readme.resolve()})

    def test_structure_checks_h1_orphan_and_long_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "README.md"
            orphan = root / "orphan.md"
            entry.write_text("# Entry\n", encoding="utf-8")
            orphan.write_text("plain\nextra\n", encoding="utf-8")
            documents = [AUDIT.load_document(path, root) for path in (entry, orphan)]

            findings = AUDIT.check_structure(documents, {}, max_lines=1)

            self.assertTrue(any(item.path == "orphan.md" and "no ATX H1" in item.message for item in findings))
            self.assertTrue(any(item.path == "orphan.md" and "orphan" in item.message for item in findings))
            self.assertTrue(any(item.path == "orphan.md" and "long page" in item.message for item in findings))
            self.assertFalse(any(item.path == "README.md" and "orphan" in item.message for item in findings))

    def test_report_digest_and_abbreviation_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.md"
            source.write_text("# Source\n", encoding="utf-8")
            self.assertEqual(REPORT.digest(source), hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(REPORT.abbreviated("1234567890abcdef"), "12345678…90abcdef")
            self.assertEqual(
                REPORT.abbreviation_variants("1234567890abcdef"),
                ("12345678…90abcdef", "12345678…0abcdef"),
            )

    def test_chinese_lint_ignores_html_attributes_code_and_harmonic_h5(self):
        self.assertEqual(COPY.lint_line('<a href="x">入口</a> `json` H5 /dev/serial/by-id'), [])
        self.assertEqual(
            COPY.lint_line("使用“json”接口"),
            [
                "中文正文使用弯引号；改用直角引号「」",
                "可见术语 'json' 建议写为 'JSON'",
            ],
        )


if __name__ == "__main__":
    unittest.main()
