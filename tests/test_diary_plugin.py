from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.kernel_surface import KERNEL_TOOLS, is_kernel_tool, is_kernel_tool_module
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plan_mode import MUTATING_TOOLS
from witty_agent.plugins import list_plugins, plugin_owns
from witty_agent.plugins.diary import diary_list, diary_read, diary_write
from witty_agent.prompts import get_prompt
from witty_agent.system_prompt import build_system_prompt
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class DiaryPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "diary"
        self.memory = Path(self.tmp.name) / "user"
        self.memory.mkdir()
        self.env = patch.dict(
            os.environ,
            {"WITTY_DIARY_DIR": str(self.folder), "WITTY_MEMORY_USER": str(self.memory)},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_business_tools_are_not_kernel(self) -> None:
        names = {item.name for item in list_tools()}
        for name in ("diary_write", "diary_read", "diary_list"):
            self.assertIn(name, names)
            self.assertFalse(is_kernel_tool(name))
            self.assertNotIn(name, KERNEL_TOOLS)
        self.assertFalse(is_kernel_tool_module("witty_agent.plugins.diary"))
        self.assertTrue(plugin_owns("diary_write"))
        self.assertIn("diary", {item["name"] for item in list_plugins()["plugins"]})
        self.assertIn("diary_read", READONLY_TOOLS)
        self.assertIn("diary_list", READONLY_TOOLS)
        self.assertNotIn("diary_write", READONLY_TOOLS)
        self.assertNotIn("diary_write", MUTATING_TOOLS)

    def test_write_read_list_and_skip_short(self) -> None:
        self.assertIn(get_prompt("diary_skipped"), diary_write("ok"))
        saved = diary_write("今天下午把周报发出去了")
        self.assertTrue(Path(saved).is_file())
        body = diary_read()
        self.assertIn("周报", body)
        listed = diary_list()
        self.assertRegex(listed, r"\d{4}-\d{2}-\d{2}")
        self.assertIn(get_prompt("diary_empty_day"), diary_read("1999-01-01"))

    def test_surface_and_prompt(self) -> None:
        shown = select_advertised_names(
            "我今天干了啥",
            sorted(KERNEL_TOOLS) + ["diary_write", "diary_read", "diary_list"],
        )
        self.assertIn("diary_read", shown)
        self.assertNotIn(
            "diary_write",
            select_advertised_names("review the auth module", sorted(KERNEL_TOOLS) + ["diary_write"]),
        )
        text = build_system_prompt(
            ".",
            tool_names=["diary_write", "diary_read"],
            skills=[],
            context_files=[],
            prompt="我今天干了啥",
        )
        self.assertIn("diary_write", text)
        self.assertIn("diary_read", text)
        self.assertIn("不要编行程", text)


if __name__ == "__main__":
    unittest.main()
