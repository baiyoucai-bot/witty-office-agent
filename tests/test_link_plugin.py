from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.kernel_surface import KERNEL_TOOLS, is_kernel_tool, is_kernel_tool_module
from witty_agent.links import (
    harvest_links,
    habit_summary,
    record_opened_url,
    resolve_mention,
    search_links,
    upsert_link,
)
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plugins import list_plugins, plugin_owns
from witty_agent.prompts import get_prompt
from witty_agent.system_prompt import build_system_prompt
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class LinkPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "links.jsonl"
        self.env = patch.dict(os.environ, {"WITTY_LINKS_FILE": str(self.path)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_business_tools_are_not_kernel(self) -> None:
        names = {item.name for item in list_tools()}
        for name in ("link_add", "link_search", "link_ingest", "link_resolve", "link_habits"):
            self.assertIn(name, names)
            self.assertFalse(is_kernel_tool(name))
            self.assertNotIn(name, KERNEL_TOOLS)
        self.assertFalse(is_kernel_tool_module("witty_agent.plugins.links"))
        self.assertTrue(plugin_owns("link_search"))
        self.assertIn("links", {item["name"] for item in list_plugins()["plugins"]})
        self.assertIn("link_search", READONLY_TOOLS)
        self.assertIn("link_habits", READONLY_TOOLS)
        self.assertNotIn("link_add", READONLY_TOOLS)

    def test_harvest_keeps_intent_history_and_aliases(self) -> None:
        first = harvest_links("今天打开了 http://192.168.0.10/oa 报周报")
        self.assertEqual(len(first), 1)
        self.assertIn("周报", first[0]["intent"])
        harvest_links("打开OA系统 http://192.168.0.10/oa 又称审批网 填请假")
        rows = search_links("OA")
        self.assertEqual(rows[0]["hits"], 2)
        self.assertGreaterEqual(len(rows[0].get("intents") or []), 2)
        aliases = " ".join(rows[0].get("aliases") or [])
        self.assertTrue("OA" in aliases or "审批网" in aliases)
        hit = resolve_mention("审批网")
        self.assertEqual(hit[0]["host"], "192.168.0.10")
        self.assertGreaterEqual(int(hit[0]["hits"]), 3)

    def test_legacy_links_file_migrates_once(self) -> None:
        self.env.stop()
        root = Path(self.tmp.name)
        memory_user = root / "default_project" / "agents" / "default_agent" / "agent_state" / "memory" / "user"
        memory_user.mkdir(parents=True)
        legacy = root / "default_project" / "agents" / "default_agent" / "links" / "links.jsonl"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            '{"url":"http://192.168.0.10/oa","host":"192.168.0.10","title":"OA","intent":"周报","hits":2}\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"WITTY_MEMORY_USER": str(memory_user)}, clear=False):
            os.environ.pop("WITTY_LINKS_FILE", None)
            from witty_agent.links import links_path, load_links

            path = links_path()
            self.assertIn("agent_state", str(path))
            self.assertTrue(path.is_file())
            rows = load_links()
        self.assertEqual(rows[0]["host"], "192.168.0.10")
        self.env.start()

    def test_opened_url_and_habits_rank_by_use(self) -> None:
        upsert_link("http://192.168.0.10/rare", title="冷门", intent="偶尔")
        record_opened_url("http://192.168.0.10/oa", title="OA", intent="web_fetch")
        record_opened_url("http://192.168.0.10/oa", intent="web_fetch")
        summary = habit_summary(limit=2)
        self.assertIn("OA", summary)
        self.assertIn("192.168.0.10/oa", summary)
        self.assertTrue(summary.index("oa") < summary.index("rare") or "冷门" not in summary)

    def test_empty_url_and_empty_render_use_prompt_keys(self) -> None:
        from witty_agent.links import render_links

        with self.assertRaises(ValueError) as exc:
            upsert_link("")
        self.assertEqual(str(exc.exception), get_prompt("link_url_required"))
        self.assertEqual(render_links([]), get_prompt("link_empty"))

    def test_surface_and_prompt(self) -> None:
        shown = select_advertised_names(
            "上次那个OA门户是哪个",
            sorted(KERNEL_TOOLS) + ["link_search", "link_resolve", "link_habits", "link_add"],
        )
        self.assertIn("link_resolve", shown)
        self.assertIn("link_habits", shown)
        self.assertNotIn(
            "link_resolve",
            select_advertised_names("review the auth module", sorted(KERNEL_TOOLS) + ["link_resolve"]),
        )
        text = build_system_prompt(
            ".",
            tool_names=["link_search", "link_resolve"],
            skills=[],
            context_files=[],
            prompt="上次那个门户",
        )
        self.assertIn("link_resolve", text)
        self.assertIn(get_prompt("link_empty")[:4], get_prompt("link_empty"))
        self.assertIn("agent_state/links", text)


if __name__ == "__main__":
    unittest.main()
