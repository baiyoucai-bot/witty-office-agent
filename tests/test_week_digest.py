from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plugins.week_digest import week_digest
from witty_agent.skills import match_relevant_skills
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class WeekDigestPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "diary"
        self.folder.mkdir()
        self.env = patch.dict(os.environ, {"WITTY_DIARY_DIR": str(self.folder)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_not_a_kernel_tool(self) -> None:
        self.assertNotIn("week_digest", KERNEL_TOOLS)
        names = {item.name for item in list_tools()}
        self.assertIn("week_digest", names)
        self.assertIn("week_digest", READONLY_TOOLS)

    def test_empty_and_days(self) -> None:
        self.assertIn("没有日记", week_digest())
        (self.folder / "2026-08-17.md").write_text(
            " # 2026-08-17\n\n- 13:00 · note · 今天下午开了验收会\n- 16:00 · note · 把周报发出去了\n",
            encoding="utf-8",
        )
        (self.folder / "2026-08-16.md").write_text(
            "# 2026-08-16\n\n- 09:00 · note · 看了方案\n",
            encoding="utf-8",
        )
        text = week_digest()
        self.assertIn("2026-08-17", text)
        self.assertIn("验收会", text)
        self.assertIn("2 条", text)
        self.assertIn("2026-08-16", text)
        self.assertIn("条目：3", text)

    def test_skill_matches_digest_prompt(self) -> None:
        names = [item.name for item in match_relevant_skills("出一份本周摘要")]
        self.assertEqual(names[:1], ["week-digest"])
        self.assertNotIn("week-digest", [item.name for item in match_relevant_skills("今天下午开了验收会")])

    def test_advertised_on_digest_prompt(self) -> None:
        names = select_advertised_names("周报摘要", ["week_digest", "diary_write", "read"])
        self.assertIn("week_digest", names)
        self.assertNotIn("diary_write", names)
        idle = select_advertised_names("今天做了啥", ["week_digest", "diary_write", "diary_read"])
        self.assertNotIn("week_digest", idle)
