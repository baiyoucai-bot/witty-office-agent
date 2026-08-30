from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plugins.table_qa import table_qa
from witty_agent.skills import match_relevant_skills
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class TableQaPluginTests(unittest.TestCase):
    def test_not_a_kernel_tool(self) -> None:
        self.assertNotIn("table_qa", KERNEL_TOOLS)
        names = {item.name for item in list_tools()}
        self.assertIn("table_qa", names)
        self.assertIn("table_qa", READONLY_TOOLS)

    def test_findings_and_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "sheet.csv"
            bad.write_text("name,name,age,\na,1\nb,TODO,2,x\n", encoding="utf-8")
            text = table_qa(str(bad))
            self.assertIn("重复表头", text)
            self.assertIn("空表头", text)
            self.assertIn("列数", text)
            self.assertIn("占位格", text)
            good = root / "ok.tsv"
            good.write_text("name\tage\nann\t3\n", encoding="utf-8")
            self.assertIn("通过", table_qa(str(good)))
            self.assertIn("找不到", table_qa(str(root / "missing.csv")))
            other = root / "deck.xlsx"
            other.write_bytes(b"PK")
            self.assertIn("暂不质检", table_qa(str(other)))

    def test_skill_matches_table_prompt(self) -> None:
        names = [item.name for item in match_relevant_skills("帮我做表格质检")]
        self.assertEqual(names[:1], ["table-qa"])
        self.assertNotIn("table-qa", [item.name for item in match_relevant_skills("检查PPT版式")])

    def test_advertised_on_table_prompt(self) -> None:
        names = select_advertised_names("CSV质检", ["table_qa", "doc_qa", "write"])
        self.assertIn("table_qa", names)
        deck = select_advertised_names("检查PPT版式", ["table_qa", "doc_qa"])
        self.assertNotIn("table_qa", deck)
