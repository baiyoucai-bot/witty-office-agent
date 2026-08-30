"""pdf-extract 技能元数据、校验与缺失文件。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from witty_agent.paths import project_root
from witty_agent.skills import list_skills, load_skill, match_relevant_skills

SKILL_NAME = "pdf-extract"
SKILL_DIR = project_root() / "skills" / SKILL_NAME


def _load(script: str) -> ModuleType:
    path = SKILL_DIR / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"pdf_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


extract = _load("extract.py")
tables = _load("tables.py")
ocr = _load("ocr.py")
compose = _load("compose.py")


def _load_office(script: str) -> ModuleType:
    path = project_root() / "skills" / "office-document" / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"office_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


convert_legacy = _load_office("convert_legacy.py")


class SkillMetadataTests(unittest.TestCase):
    def test_discoverable(self) -> None:
        by_name = {item.name: item for item in list_skills()}
        self.assertIn(SKILL_NAME, by_name)
        self.assertEqual(by_name[SKILL_NAME].network, "intranet")
        skill = load_skill(SKILL_NAME)
        self.assertIn("文字层", skill.body)
        scripts = {path.name for path in skill.scripts_dir.glob("*.py")}
        self.assertTrue({"extract.py", "tables.py", "ocr.py", "compose.py"} <= scripts)

    def test_matches_extract_prompts(self) -> None:
        for prompt in ("把这份 pdf 抽成文本", "用 pdftotext 读一下扫描件有没有文字层"):
            names = [item.name for item in match_relevant_skills(prompt, limit=3)]
            self.assertIn(SKILL_NAME, names, prompt)


class ExtractTests(unittest.TestCase):
    def test_missing_file(self) -> None:
        self.assertEqual(extract.main(["--input", "/no/such.pdf", "--check"]), 2)

    def test_encrypt_header_fails_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.pdf"
            path.write_bytes(b"%PDF-1.4\n/Encrypt 1 0 R\n%%EOF\n")
            self.assertEqual(extract.main(["--input", str(path), "--check"]), 1)

    def test_parse_pages(self) -> None:
        self.assertEqual(extract.parse_pages("1-3"), (1, 3))
        self.assertEqual(extract.parse_pages("2"), (2, 2))
        self.assertEqual(extract.parse_pages(""), (1, 0))


class TablesOcrComposeTests(unittest.TestCase):
    def test_tables_markdown_and_missing(self) -> None:
        body = tables.to_markdown(
            [(1, [[["项目", "金额"], ["甲", "1"]]])]
        )
        self.assertIn("第1页 表1", body)
        self.assertIn("| 项目 | 金额 |", body)
        self.assertEqual(tables.main(["--input", "/no/such.pdf"]), 2)
        self.assertEqual(tables.parse_pages("2-4"), (2, 4))

    def test_ocr_order_and_missing(self) -> None:
        lines = ocr.order_lines(
            [[[[10, 20], [30, 20], [30, 40], [10, 40]], "下", 0.9],
             [[[10, 1], [30, 1], [30, 10], [10, 10]], "上", 0.9]]
        )
        self.assertEqual(lines, ["上", "下"])
        self.assertEqual(ocr.main(["--input", "/no/such.pdf"]), 2)
        self.assertEqual(ocr.parse_pages("3"), (3, 3))

    def test_compose_help_and_missing(self) -> None:
        self.assertEqual(compose.main(["--help-spec"]), 0)
        self.assertEqual(compose.main(["--list", "/no/such.pdf"]), 2)
        self.assertEqual(compose.main(["--merge", "/no/a.pdf", "--output", "/tmp/out.pdf"]), 2)

    def test_compose_merge_if_pypdf(self) -> None:
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf 未装")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.pdf"
            second = root / "b.pdf"
            dest = root / "out.pdf"
            for path in (first, second):
                writer = PdfWriter()
                writer.add_blank_page(width=72, height=72)
                writer.write(str(path))
            self.assertEqual(
                compose.main(["--merge", str(first), str(second), "--output", str(dest)]),
                0,
            )
            self.assertTrue(dest.is_file())


class ConvertLegacyTests(unittest.TestCase):
    def test_missing_and_unknown_suffix(self) -> None:
        self.assertEqual(convert_legacy.main(["--input", "/no/such.doc"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            path.write_text("x", encoding="utf-8")
            self.assertEqual(convert_legacy.main(["--input", str(path)]), 2)


if __name__ == "__main__":
    unittest.main()
