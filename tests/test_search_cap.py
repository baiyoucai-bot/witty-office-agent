from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.tools.fs import bind_workspace
from witty_agent.tools.search import find, grep, ls


class SearchCapTests(unittest.TestCase):
    def test_find_footer_reports_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for index in range(5):
                (workspace / f"n{index}.txt").write_text("x\n", encoding="utf-8")
            bind_workspace(str(workspace))
            page = find("n*.txt", ".", limit=2)
            self.assertEqual(page.count(".txt"), 2)
            self.assertIn(get_prompt("search_footer_capped", shown="2", total="5"), page)
            full = find("n*.txt", ".", limit=20)
            self.assertNotIn("已截断", full)
            self.assertEqual(full.count(".txt"), 5)

    def test_grep_footer_when_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for index in range(4):
                (workspace / f"g{index}.py").write_text(f"token-{index}\n", encoding="utf-8")
            bind_workspace(str(workspace))
            page = grep("token", ".", "*.py", limit=2)
            self.assertIn("token-", page)
            self.assertIn("已截断", page)
            self.assertIn("显示 2 条", page)
            self.assertNotIn("[truncated]", page)

    def test_ls_footer_reports_total(self) -> None:
        flag = os.environ.get("WITTY_SANDBOX_ENABLED")
        os.environ["WITTY_SANDBOX_ENABLED"] = "0"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                for index in range(5):
                    (workspace / f"n{index}.txt").write_text("x\n", encoding="utf-8")
                bind_workspace(str(workspace))
                page = ls(".", limit=2)
                self.assertEqual(sum(1 for line in page.splitlines() if line.endswith(".txt")), 2)
                self.assertIn(get_prompt("search_footer_capped", shown="2", total="5"), page)
                self.assertNotIn("more]", page)
                full = ls(".", limit=20)
                self.assertNotIn("已截断", full)
                self.assertEqual(sum(1 for line in full.splitlines() if line.endswith(".txt")), 5)
        finally:
            if flag is None:
                os.environ.pop("WITTY_SANDBOX_ENABLED", None)
            else:
                os.environ["WITTY_SANDBOX_ENABLED"] = flag
