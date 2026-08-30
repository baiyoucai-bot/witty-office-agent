"""long-document 工程 + word-docx 长文导出 / 大纲抽取。"""

from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from types import ModuleType

from witty_agent.paths import project_root
from witty_agent.skills import list_skills, load_skill, match_relevant_skills

LONG = "long-document"
LONG_DIR = project_root() / "skills" / LONG
WORD_DIR = project_root() / "skills" / "word-docx"


def _load(skill_dir: Path, script: str) -> ModuleType:
    path = skill_dir / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"{skill_dir.name}_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


init_project = _load(LONG_DIR, "init_project.py")
check_doc = _load(LONG_DIR, "check_doc.py")
import_source = _load(LONG_DIR, "import_source.py")
report = _load(WORD_DIR, "report.py")
outline = _load(WORD_DIR, "outline.py")
extract_text = _load(WORD_DIR, "extract_text.py")


def _tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


class SkillMetadataTests(unittest.TestCase):
    def test_discoverable(self) -> None:
        by_name = {item.name: item for item in list_skills()}
        self.assertIn(LONG, by_name)
        self.assertEqual(by_name[LONG].network, "general")
        skill = load_skill(LONG)
        self.assertEqual(skill.name, LONG_DIR.name)
        profiles = {path.name for path in skill.references_dir.joinpath("profiles").glob("*.json")}
        self.assertEqual(
            profiles,
            {"generic.json", "feasibility.json", "outline-design.json", "detailed-design.json"},
        )

    def test_matches_long_doc_not_memo(self) -> None:
        for prompt in ("写一份可研报告，分章来", "帮我做详设目录再往下写", "按初步设计分章编写"):
            names = [item.name for item in match_relevant_skills(prompt, limit=3)]
            self.assertIn(LONG, names, prompt)
        self.assertEqual(
            [item.name for item in match_relevant_skills("写一份报告")],
            ["office-document"],
        )


class ProjectTests(unittest.TestCase):
    def test_scaffold_alias_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = ["--name", "示范可研", "--root", tmp, "--profile", "可研"]
            self.assertEqual(init_project.main(args), 0)
            project = Path(tmp) / "示范可研"
            self.assertTrue((project / "outline.md").is_file())
            self.assertTrue((project / "ledger.toml").is_file())
            self.assertTrue((project / "continuity.md").is_file())
            self.assertTrue((project / "chapters" / "05-投资.md").is_file())
            card = (project / "项目卡.md").read_text(encoding="utf-8")
            self.assertIn("feasibility", card)
            (project / "chapters" / "01-概述.md").write_text("# 概述\n\n手写的要点。\n", encoding="utf-8")
            self.assertEqual(init_project.main(args), 0)
            self.assertIn("手写的要点", (project / "chapters" / "01-概述.md").read_text(encoding="utf-8"))

    def test_bad_profile_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(init_project.main(["--name", "x", "--root", tmp, "--profile", "审计专用"]), 2)
            self.assertEqual(init_project.main(["--name", "x", "--root", str(Path(tmp) / "missing")]), 2)

    def test_missing_chapter_and_dangling_cite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            (project / "chapters" / "01-概述.md").unlink()
            self.assertEqual(check_doc.main(["--project", str(project)]), 1)
            init_project.main(["--name", "稿2", "--root", tmp, "--profile", "generic"])
            other = Path(tmp) / "稿2"
            body = other / "chapters" / "01-概述.md"
            body.write_text("# 概述\n\n投资见 [cite:ghost]。\n", encoding="utf-8")
            self.assertEqual(check_doc.main(["--project", str(other)]), 1)

    def test_clean_written_chapter_no_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            budgets = {
                slug: budget
                for slug, _title, budget, _tables in check_doc.parse_outline(
                    (project / "outline.md").read_text(encoding="utf-8")
                )
            }
            for path in (project / "chapters").glob("*.md"):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                # 得真写满预算才算「写好的一章」。原来这里只写「已写正文。」五个字就断言通过，
                # 等于把「欠一大截」当合格——线上那份可研正是这么以 0 FAIL 交出去的。
                body = "正文内容。" * ((budgets.get(path.stem) or 0) // 5 + 1)
                path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
            self.assertEqual(check_doc.main(["--project", str(project)]), 0)

    def test_underwritten_chapter_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            for path in (project / "chapters").glob("*.md"):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                path.write_text(f"# {title}\n\n只写一句就收工。\n", encoding="utf-8")
            self.assertEqual(check_doc.main(["--project", str(project)]), 1)

    def test_missing_required_table_fails(self) -> None:
        """提纲写了 `+N表`，正文没表就得 FAIL。

        可研的投资、效益、风险靠叙述说不清口径，工具链一直支持表格，
        但没人要求过，于是导出的报告一张表都没有。
        """
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            body = "正文内容。" * 300
            for path in (project / "chapters").glob("*.md"):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
            outline_path = project / "outline.md"
            first = check_doc.parse_outline(outline_path.read_text(encoding="utf-8"))[0]
            outline_path.write_text(
                f"# 提纲\n\n- [{first[0]}] {first[1]} ~{first[2]}字 +1表\n", encoding="utf-8"
            )
            self.assertEqual(check_doc.main(["--project", str(project)]), 1)

            chapter = project / "chapters" / f"{first[0]}.md"
            table = "| 科目 | 金额 |\n|---|---|\n| 硬件 | 480 |\n"
            chapter.write_text(
                f"# {first[1]}\n\n{body}\n\n{table}\n", encoding="utf-8"
            )
            self.assertEqual(check_doc.main(["--project", str(project)]), 0)

    def test_outline_annotations_not_swallowed_into_title(self) -> None:
        """`~N字` `+N表` 是预算标注，不能被非贪婪的标题组吞进去。

        report.py 少认 `+N表` 时，导出的章标题会变成「投资估算 ~2000字 +2表」。
        """
        for module in (check_doc, report):
            match = module.OUTLINE_LINE.match("- [05-投资] 投资估算 ~2000字 +2表")
            assert match is not None
            self.assertEqual(match.group(2), "投资估算", module.__name__)

    def test_chapter_scope_gates_sample_before_mass_production(self) -> None:
        """--chapter 是样章门：其余章还是桩时，只判样章本身。

        没有这个口子，样章写完跑全工程校验会被未写章的字数 FAIL 淹没，
        「样章过了再量产」根本执行不起来。
        """
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            sample = project / "chapters" / "03-方案.md"
            sample.write_text("# 方案\n\n" + "论证内容。" * 900 + "\n", encoding="utf-8")
            # 其余章仍是桩：全工程有 FAIL，单章没有
            self.assertEqual(check_doc.main(["--project", str(project)]), 1)
            self.assertEqual(
                check_doc.main(["--project", str(project), "--chapter", "03-方案"]), 0
            )
            # 样章自身的悬空引用照样拦住
            sample.write_text(
                "# 方案\n\n" + "论证内容。" * 900 + "依据 [cite:ghost]。\n", encoding="utf-8"
            )
            self.assertEqual(
                check_doc.main(["--project", str(project), "--chapter", "03-方案"]), 1
            )
            # slug 不在提纲里：退出码 2
            self.assertEqual(
                check_doc.main(["--project", str(project), "--chapter", "99-不存在"]), 2
            )

    def test_dangling_num_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            for path in (project / "chapters").glob("*.md"):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                path.write_text(f"# {title}\n\n投资 [num:ghost]。\n", encoding="utf-8")
            self.assertEqual(check_doc.main(["--project", str(project)]), 1)

    def test_import_source_registers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            src = Path(tmp) / "旧稿.md"
            src.write_text("# 旧稿\n\n原文。\n", encoding="utf-8")
            self.assertEqual(
                import_source.main(
                    ["--project", str(project), "--input", str(src), "--id", "draft", "--title", "用户旧稿"]
                ),
                0,
            )
            ledger = (project / "sources.toml").read_text(encoding="utf-8")
            self.assertIn("[draft]", ledger)
            self.assertTrue((project / "sources" / "draft.md").is_file())
            self.assertEqual(
                import_source.main(
                    ["--project", str(project), "--input", str(src), "--id", "draft", "--title", "用户旧稿"]
                ),
                2,
            )
            binary = Path(tmp) / "old.docx"
            binary.write_bytes(b"PK")
            self.assertEqual(
                import_source.main(["--project", str(project), "--input", str(binary), "--id", "bin"]),
                2,
            )


class ExportTests(unittest.TestCase):
    def test_report_then_outline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            for path in (project / "chapters").glob("*.md"):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                path.write_text(f"# {title}\n\n## 小节\n\n已写正文。\n", encoding="utf-8")
            dest = Path(tmp) / "稿.docx"
            self.assertEqual(report.main(["--project", str(project), "--output", str(dest)]), 0)
            self.assertTrue(dest.is_file())
            self.assertEqual(outline.main(["--input", str(dest)]), 0)
            items = outline.outline(dest)
            titles = [item["title"] for item in items]
            self.assertIn("概述", titles)
            self.assertIn("小节", titles)
            self.assertTrue(any(item["level"] == 1 for item in items if item["title"] == "概述"))
            self.assertTrue(any(item["level"] == 2 for item in items if item["title"] == "小节"))

    def test_outline_missing_file(self) -> None:
        self.assertEqual(outline.main(["--input", "/no/such.docx"]), 2)

    def test_report_toc_image_num_and_extract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            (project / "assets").mkdir(exist_ok=True)
            (project / "assets" / "图.png").write_bytes(_tiny_png())
            (project / "ledger.toml").write_text(
                '[inv]\nlabel = "总投资"\ntext = "1.2亿元"\n',
                encoding="utf-8",
            )
            chapters = sorted((project / "chapters").glob("*.md"))
            first = True
            for path in chapters:
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                if first:
                    path.write_text(
                        f"# {title}\n\n"
                        f"总投资 [num:inv]，见图 [@fig:site]。\n\n"
                        f"![厂址示意](assets/图.png){{#fig:site}}\n\n"
                        f"表: 投资构成 {{#tbl:cost}}\n\n"
                        f"| 项 | 金额 |\n| --- | --- |\n| 合计 | [num:inv] |\n",
                        encoding="utf-8",
                    )
                    first = False
                else:
                    path.write_text(f"# {title}\n\n见概述，总投资 [num:inv]。\n", encoding="utf-8")
            dest = Path(tmp) / "稿.docx"
            self.assertEqual(
                report.main(["--project", str(project), "--output", str(dest), "--toc"]),
                0,
            )
            with zipfile.ZipFile(dest) as zin:
                xml = zin.read("word/document.xml").decode("utf-8")
                names = set(zin.namelist())
            self.assertIn("TOC", xml)
            self.assertIn("SEQ", xml)
            self.assertIn("1.2亿元", xml)
            self.assertNotIn("[num:inv]", xml)
            self.assertTrue(any(name.startswith("word/media/") for name in names))
            md_path = Path(tmp) / "抽出.md"
            self.assertEqual(
                extract_text.main(["--input", str(dest), "--output", str(md_path)]),
                0,
            )
            body = md_path.read_text(encoding="utf-8")
            self.assertIn("概述", body)
            self.assertIn("1.2亿元", body)

    def test_table_cell_expands_ref_and_num(self) -> None:
        """单元格里的 [@tbl:] / [num:] 也要展开成域和数值。

        number_pass 本来就扫单元格里的 [@…]，渲染却只扫段落：校验说没问题，
        Word 里却原样印着 `[@tbl:x]`。表格成了硬要求之后，表引用表是常态。
        """
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            (project / "ledger.toml").write_text(
                '[inv]\nlabel = "总投资"\ntext = "1850 万元"\n', encoding="utf-8"
            )
            chapters = sorted((project / "chapters").glob("*.md"))
            for index, path in enumerate(chapters):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                if index == 0:
                    path.write_text(
                        f"# {title}\n\n"
                        "表: 投资分项 {#tbl:invest}\n\n"
                        "| 科目 | 金额 |\n|---|---|\n| 硬件 | 480 |\n\n"
                        "表: 附件清单 {#tbl:attach}\n\n"
                        "| 附件 | 正文对应 | 金额 |\n|---|---|---|\n"
                        "| 附件C | [@tbl:invest] | [num:inv] |\n",
                        encoding="utf-8",
                    )
                else:
                    path.write_text(f"# {title}\n\n正文。\n", encoding="utf-8")
            dest = Path(tmp) / "稿.docx"
            self.assertEqual(
                report.main(["--project", str(project), "--output", str(dest)]), 0
            )
            with zipfile.ZipFile(dest) as zin:
                xml = zin.read("word/document.xml").decode("utf-8")
            self.assertNotIn("[@tbl:invest]", xml)
            self.assertNotIn("[num:inv]", xml)
            self.assertIn("REF _ref_tbl_invest", xml)
            self.assertIn("1850 万元", xml)

    def test_report_dangling_ref_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "稿", "--root", tmp, "--profile", "generic"])
            project = Path(tmp) / "稿"
            for path in (project / "chapters").glob("*.md"):
                title = path.read_text(encoding="utf-8").splitlines()[0][2:].strip()
                path.write_text(f"# {title}\n\n见图 [@fig:missing]。\n", encoding="utf-8")
            self.assertEqual(
                report.main(["--project", str(project), "--output", str(Path(tmp) / "x.docx")]),
                2,
            )


if __name__ == "__main__":
    unittest.main()
