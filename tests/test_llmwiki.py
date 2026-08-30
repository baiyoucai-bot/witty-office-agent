from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plan_mode import MUTATING_TOOLS
from witty_agent.plugins import list_plugins
from witty_agent.http_api import configure_api, handle_request
from witty_agent.plugins.llmwiki import (
    public_wiki,
    wiki_add,
    wiki_init,
    wiki_lint,
    wiki_remove,
    wiki_search,
    wiki_sources,
    wiki_stats,
)
from witty_agent.runtime import clear_runtime_cache
from witty_agent.skills import list_skills, match_relevant_skills
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class LlmWikiPluginTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_cache()

    def test_not_a_kernel_tool(self) -> None:
        names = {item.name for item in list_tools()}
        for name in (
            "wiki_init",
            "wiki_search",
            "wiki_lint",
            "wiki_stats",
            "wiki_add",
            "wiki_remove",
            "wiki_sources",
        ):
            self.assertNotIn(name, KERNEL_TOOLS)
            self.assertIn(name, names)
        self.assertIn("wiki_search", READONLY_TOOLS)
        self.assertIn("wiki_lint", READONLY_TOOLS)
        self.assertIn("wiki_stats", READONLY_TOOLS)
        self.assertIn("wiki_sources", READONLY_TOOLS)
        self.assertIn("wiki_init", MUTATING_TOOLS)
        self.assertIn("wiki_add", MUTATING_TOOLS)
        self.assertIn("wiki_remove", MUTATING_TOOLS)
        self.assertNotIn("wiki_init", READONLY_TOOLS)
        self.assertEqual(
            next(item.network for item in list_skills() if item.name == "llm-wiki"),
            "public",
        )
        plugins = {item["name"] for item in list_plugins()["plugins"]}
        self.assertIn("llmwiki", plugins)
        self.assertIn("llm-wiki", {item.name for item in list_skills()})

    def test_init_search_lint_and_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = wiki_init(str(root))
            self.assertIn("ready", first)
            self.assertTrue((root / "wiki" / "SCHEMA.md").is_file())
            self.assertTrue((root / "raw" / "assets").is_dir())
            again = wiki_init(str(root))
            self.assertIn("已有", again)

            page = root / "wiki" / "concepts" / "diffusion.md"
            page.write_text(
                "---\ntype: concept\ntags: [gen]\nsources: []\nupdated: 2026-08-18\n---\n"
                "# Diffusion\n\n扩散模型见 [[missing-page]]。\n",
                encoding="utf-8",
            )
            hit = wiki_search("扩散模型", root=str(root))
            self.assertIn("concepts/diffusion", hit)
            self.assertIn("扩散", hit)
            miss = wiki_search("完全无关的量子引力术语", root=str(root))
            self.assertIn("没有命中", miss)

            lint = wiki_lint(str(root))
            self.assertIn("断链", lint)
            self.assertIn("孤儿", lint)
            stats = wiki_stats(str(root))
            self.assertIn("页：", stats)
            self.assertIn("concepts", stats.lower())

            with patch.dict(os.environ, {"WITTY_LLMWIKI_ENABLED": "0"}):
                clear_runtime_cache()
                self.assertIn("已关闭", wiki_search("扩散", root=str(root)))
                self.assertIn("已关闭", wiki_init(str(root)))

    def test_add_list_and_remove_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# 标题\n\n正文。\n", encoding="utf-8")
            added = wiki_add(str(paper), root=str(root))
            self.assertIn("已收入原文", added)
            listing = wiki_sources(root=str(root))
            self.assertIn("paper", listing)
            self.assertTrue(list((root / "raw").glob("*.md")))
            with patch(
                "witty_agent.tools.web.web_fetch",
                return_value="# 公网页\nhello wiki",
            ):
                remote = wiki_add("https://example.com/notes/hello", root=str(root))
            self.assertIn("已收入原文", remote)
            self.assertIn("example.com", wiki_sources(root=str(root)))
            source_id = next(
                line.split(" · ", 1)[0].lstrip("- ").strip()
                for line in wiki_sources(root=str(root)).splitlines()
                if "hello" in line
            )
            removed = wiki_remove(source_id, root=str(root))
            self.assertIn("已删除原文", removed)
            self.assertNotIn("hello", wiki_sources(root=str(root)))
            paper_id = next(
                line.split(" · ", 1)[0].lstrip("- ").strip()
                for line in wiki_sources(root=str(root)).splitlines()
                if "paper" in line
            )
            snap = public_wiki(str(root))
            self.assertTrue(snap["enabled"])
            self.assertEqual(snap["pending"], 1)
            self.assertTrue(any(item["id"] == paper_id for item in snap["sources"]))


class WikiApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_needs_workspace_then_add(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            status, body = await handle_request("GET", "/v1/wiki")
            self.assertEqual(status, 400)
            paper = Path(tmp) / "note.md"
            paper.write_text("# n\n", encoding="utf-8")
            status, body = await handle_request(
                "POST",
                "/v1/wiki",
                {"source": str(paper), "workspace_dir": tmp},
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["sources"])
            status, listed = await handle_request("GET", f"/v1/wiki?workspace_dir={tmp}")
            self.assertEqual(status, 200)
            self.assertEqual(listed["pending"], 1)

    def test_skill_matches_wiki_prompt(self) -> None:
        names = [item.name for item in match_relevant_skills("wiki 里怎么说扩散")]
        self.assertEqual(names[:1], ["llm-wiki"])
        self.assertNotIn(
            "llm-wiki",
            [item.name for item in match_relevant_skills("帮我做表格质检")],
        )

    def test_advertised_on_wiki_prompt(self) -> None:
        names = select_advertised_names(
            "lint the wiki",
            ["wiki_search", "wiki_lint", "table_qa", "read"],
        )
        self.assertIn("wiki_lint", names)
        idle = select_advertised_names("帮我做表格质检", ["wiki_search", "table_qa"])
        self.assertNotIn("wiki_search", idle)
        self.assertIn("table_qa", idle)
