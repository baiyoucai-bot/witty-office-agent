"""excel-xlsx 技能：生成 / 改格子 / 公式保全校验。脚本是技能资产，不进 witty_agent 包。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from witty_agent.paths import project_root
from witty_agent.skills import list_skills, load_skill, match_relevant_skills

SKILL_NAME = "excel-xlsx"
SKILL_DIR = project_root() / "skills" / SKILL_NAME
SCRIPT_DIR = SKILL_DIR / "scripts"


def _load(script: str, *, name: str | None = None) -> ModuleType:
    path = SCRIPT_DIR / script
    mod_name = name or f"excel_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


xlsx_parts = _load("xlsx_parts.py", name="xlsx_parts")
write = _load("write.py")
inspect = _load("inspect.py")
apply = _load("apply.py")
check_xlsx = _load("check_xlsx.py")
recalc = _load("recalc.py")
chart = _load("chart.py")


def _write_book(root: Path, name: str = "book.xlsx") -> Path:
    spec = {
        "sheets": [
            {
                "name": "汇总",
                "rows": [
                    ["项目", "数量", "单价", "金额"],
                    ["甲", 2, 10, "=B2*C2"],
                ],
            }
        ]
    }
    spec_path = root / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = root / name
    assert write.main(["--spec", str(spec_path), "--output", str(out)]) == 0
    return out


class SkillMetadataTests(unittest.TestCase):
    def test_discoverable(self) -> None:
        by_name = {item.name: item for item in list_skills()}
        self.assertIn(SKILL_NAME, by_name)
        self.assertEqual(by_name[SKILL_NAME].network, "intranet")
        skill = load_skill(SKILL_NAME)
        self.assertEqual(skill.name, SKILL_DIR.name)
        scripts = {path.name for path in skill.scripts_dir.glob("*.py")}
        self.assertTrue(
            {"write.py", "apply.py", "inspect.py", "check_xlsx.py", "recalc.py", "xlsx_parts.py", "chart.py"}
            <= scripts
        )

    def test_matches_workbook_prompts(self) -> None:
        for prompt in ("改这个工作簿的格子，公式要保全", "帮我填报一份 xlsx", "用 excel-xlsx 出表"):
            names = [item.name for item in match_relevant_skills(prompt, limit=3)]
            self.assertIn(SKILL_NAME, names, prompt)

    def test_does_not_steal_csv_analysis(self) -> None:
        names = [item.name for item in match_relevant_skills("分析一下这个 csv 文件")]
        self.assertEqual(names, ["data-analysis"])
        qa = [item.name for item in match_relevant_skills("帮我做表格质检")]
        self.assertEqual(qa, ["table-qa"])


class WriteInspectTests(unittest.TestCase):
    def test_write_keeps_formula(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_book(Path(tmp))
            book, _, _ = xlsx_parts.load_workbook(path)
            self.assertEqual([sheet.name for sheet in book.sheets], ["汇总"])
            cell = book.sheets[0].cells["D2"]
            self.assertEqual(cell.formula, "B2*C2")
            self.assertEqual(book.sheets[0].cells["A2"].value, "甲")
            self.assertEqual(inspect.main(["--input", str(path)]), 0)

    def test_help_spec(self) -> None:
        self.assertEqual(write.main(["--help-spec"]), 0)
        self.assertEqual(apply.main(["--help-spec"]), 0)


class ApplyAndCheckTests(unittest.TestCase):
    def test_refuses_to_overwrite_formula(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _write_book(root)
            spec = root / "bad.json"
            spec.write_text(
                json.dumps({"sheet": "汇总", "sets": [{"cell": "D2", "value": 99}]}),
                encoding="utf-8",
            )
            self.assertEqual(
                apply.main(["--input", str(src), "--output", str(root / "out.xlsx"), "--spec", str(spec)]),
                1,
            )

    def test_updates_input_keeps_formula(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _write_book(root)
            spec = root / "ok.json"
            spec.write_text(
                json.dumps({"sheet": "汇总", "sets": [{"cell": "B2", "value": 5}]}),
                encoding="utf-8",
            )
            dest = root / "out.xlsx"
            self.assertEqual(
                apply.main(["--input", str(src), "--output", str(dest), "--spec", str(spec)]),
                0,
            )
            book, _, _ = xlsx_parts.load_workbook(dest)
            self.assertEqual(book.sheets[0].cells["B2"].value, "5")
            self.assertEqual(book.sheets[0].cells["D2"].formula, "B2*C2")
            self.assertEqual(
                check_xlsx.main(["--input", str(dest), "--original", str(src)]),
                0,
            )

    def test_detects_formula_flattened_to_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _write_book(root)
            spec = root / "flat.json"
            spec.write_text(
                json.dumps(
                    {
                        "sheet": "汇总",
                        "sets": [{"cell": "D2", "value": 20, "overwrite_formula": True}],
                    }
                ),
                encoding="utf-8",
            )
            dest = root / "flat.xlsx"
            self.assertEqual(
                apply.main(["--input", str(src), "--output", str(dest), "--spec", str(spec)]),
                0,
            )
            self.assertEqual(check_xlsx.main(["--input", str(dest)]), 0)
            self.assertEqual(
                check_xlsx.main(["--input", str(dest), "--original", str(src)]),
                1,
            )

    def test_missing_file_exit_2(self) -> None:
        self.assertEqual(inspect.main(["--input", "/no/such.xlsx"]), 2)
        self.assertEqual(check_xlsx.main(["--input", "/no/such.xlsx"]), 2)
        self.assertEqual(recalc.main(["--input", "/no/such.xlsx", "--output", "/tmp/x.xlsx"]), 2)


class ChartTests(unittest.TestCase):
    def test_help_and_missing(self) -> None:
        self.assertEqual(chart.main(["--help-spec"]), 0)
        self.assertEqual(
            chart.main(["--input", "/no/such.xlsx", "--output", "/tmp/x.xlsx", "--spec", "/no/spec.json"]),
            2,
        )

    def test_refuses_existing_drawing_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drawn.xlsx"
            import zipfile

            with zipfile.ZipFile(path, "w") as zout:
                zout.writestr("xl/charts/chart1.xml", "<c/>")
            self.assertTrue(chart.has_existing_drawing(path))
            spec = Path(tmp) / "chart.json"
            spec.write_text('{"charts":[{"sheet":"汇总","type":"bar","series":[{"values":"A1:A2"}]}]}', encoding="utf-8")
            self.assertEqual(
                chart.main(["--input", str(path), "--output", str(Path(tmp) / "out.xlsx"), "--spec", str(spec)]),
                2,
            )


if __name__ == "__main__":
    unittest.main()
