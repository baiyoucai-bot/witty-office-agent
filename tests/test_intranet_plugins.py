from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.approval import is_dangerous
from witty_agent.diary import append_diary, harvest_diary, read_diary
from witty_agent.http_api import configure_api, handle_request
from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.links import extract_urls, harvest_links, resolve_mention, search_links
from witty_agent.net_policy import host_allowed
from witty_agent.plan_mode import MUTATING_TOOLS
from witty_agent.plugins import list_plugins
from witty_agent.plugins import mail as mail_plugin
from witty_agent.prompts import get_prompt
from witty_agent.runtime import clear_runtime_cache
from witty_agent.skills import list_skills
from witty_agent.system_prompt import build_system_prompt
from witty_agent.plugins.pptx import pptx_add_slide, pptx_create, pptx_edit_slide, pptx_outline
from witty_agent.tools.registry import list_tools
from witty_agent.tool_surface import select_advertised_names


class IntranetPluginTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_and_store_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "links.jsonl"
            with patch.dict(os.environ, {"WITTY_LINKS_FILE": str(path)}):
                urls = extract_urls("打开 http://192.168.0.10/oa 和 http://192.168.0.10/oa/flow")
                self.assertEqual(len(urls), 2)
                rows = harvest_links("今天打开了 http://192.168.0.10/oa 报周报")
                self.assertEqual(len(rows), 1)
                found = search_links("周报")
                self.assertEqual(found[0]["host"], "192.168.0.10")
                self.assertIn("周报", found[0]["intent"])
                harvest_links("打开OA系统 http://192.168.0.10/oa 填周报")
                hit = resolve_mention("OA")
                self.assertTrue(hit)
                self.assertEqual(hit[0]["host"], "192.168.0.10")

    def test_diary_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "diary"
            memory = Path(tmp) / "user"
            memory.mkdir()
            with patch.dict(os.environ, {"WITTY_DIARY_DIR": str(folder)}, clear=False):
                harvest_diary("今天下午开了验收会。无关", memory_dir=memory)
                # 传了 memory_dir 就以它为准：日记跟着 agent 的记忆目录走，不看环境变量。
                # 反过来（环境变量赢）意味着日记跟着进程的 cwd 跑，多 agent 会串，
                # 跑测试还会写进用户真实日记。
                self.assertIn("验收会", read_diary(memory_dir=memory))
                self.assertFalse(folder.exists())
                path = append_diary("把周报发出去了", kind="note", memory_dir=memory)
                self.assertTrue(Path(path).is_file())
                self.assertEqual(Path(path).parent, memory / "diary")
                timeline = (memory / "timeline.md").read_text(encoding="utf-8")
                self.assertIn("验收会", timeline)

    def test_private_host_allowed_public_denied(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "1"
        clear_runtime_cache()
        try:
            self.assertTrue(host_allowed("http://127.0.0.1:8765/health"))
            self.assertFalse(host_allowed("https://example.com/x"))
        finally:
            os.environ.pop("WITTY_WEB_DENY_PUBLIC", None)
            clear_runtime_cache()

    def test_new_skills_and_tools_are_pluggable(self) -> None:
        names = {item.name for item in list_skills()}
        self.assertTrue({"mail-desk", "link-box", "daily-diary", "witty-ppt-skills", "llm-wiki"} <= names)
        tools = {item.name for item in list_tools()}
        self.assertTrue(
            {
                "mail_list",
                "mail_save",
                "mail_reply",
                "link_search",
                "link_resolve",
                "diary_write",
                "pptx_create",
                "pptx_edit_slide",
                "pptx_add_picture",
                "web_fetch",
                "wiki_search",
                "wiki_init",
            }
            <= tools
        )
        catalog = list_plugins()
        self.assertTrue(catalog["protected"])
        plugin_names = {item["name"] for item in catalog["plugins"]}
        self.assertIn("mail", plugin_names)
        self.assertIn("links", plugin_names)
        self.assertIn("diary", plugin_names)
        self.assertIn("pptx", plugin_names)
        self.assertIn("llmwiki", plugin_names)
        self.assertTrue(KERNEL_TOOLS.isdisjoint(set().union(*(item["tools"] for item in catalog["plugins"]))))

    def test_mail_memory_backend_analyze_reply_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp) / "drafts"
            dest = Path(tmp) / "out" / "note.txt"
            with patch.dict(os.environ, {"WITTY_MAIL_DRAFTS": str(drafts)}):
                clear_runtime_cache()
                box = mail_plugin.MemoryMailbox()
                box.add(
                    uid="9",
                    **{
                        "from": "lead@grid.local",
                        "to": ["me@grid.local"],
                        "subject": "请尽快确认周报",
                        "body": "请尽快回复验收材料。",
                        "attachments": [{"name": "note.txt", "bytes": b"hello-grid"}],
                    },
                )
                mail_plugin.set_mail_backend(box)
                try:
                    listed = mail_plugin.email_list()
                    self.assertIn("请尽快确认周报", listed)
                    viewed = mail_plugin.email_read("9")
                    self.assertIn("验收材料", viewed)
                    analyzed = mail_plugin.email_analyze(uid="9")
                    self.assertIn("request", analyzed)
                    saved = mail_plugin.email_save_attachment("9", str(dest), name="note.txt")
                    self.assertTrue(dest.is_file())
                    self.assertEqual(dest.read_bytes(), b"hello-grid")
                    self.assertIn(str(dest), saved)
                    reply = mail_plugin.email_reply("9", "已收到，下午发出。")
                    self.assertIn("d-", reply)
                    self.assertIn("Re:", reply)
                finally:
                    mail_plugin.set_mail_backend(None)

    def test_pptx_offline_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "deck.pptx")
            pptx_create(path, "封面", "内网")
            pptx_add_slide(path, "要点", "一条\n两条")
            pptx_edit_slide(path, 2, title="修订", bullets="新一条")
            outline = pptx_outline(path)
            self.assertIn("封面", outline)
            self.assertIn("修订", outline)
            self.assertIn("新一条", outline)

    def test_surface_unlocks_and_prompt_explains(self) -> None:
        names = select_advertised_names(
            "看一下收件箱并回复附件",
            sorted(
                {
                    *KERNEL_TOOLS,
                    "mail_status",
                    "mail_list",
                    "mail_read",
                    "mail_reply",
                    "mail_save",
                    "link_resolve",
                    "pptx_create",
                }
            ),
        )
        self.assertIn("mail_list", names)
        self.assertIn("mail_reply", names)
        text = build_system_prompt(
            ".",
            tool_names=["mail_list", "mail_reply", "link_search", "diary_write", "pptx_create"],
            skills=[],
            context_files=[],
            list_snippets=False,
            prompt="看一下收件箱",
        )
        self.assertIn("mail_status", text)
        self.assertIn("link_resolve", text)
        self.assertIn("diary_write", text)
        # PPT 指引正文是配置（prompts.toml），别在测试里抄工具名——主路径改过一次就漂了。
        self.assertIn(get_prompt("pptx_capability").strip(), text)
        self.assertIn("mail_send", MUTATING_TOOLS)
        self.assertTrue(is_dangerous("mail_send"))
        self.assertTrue(is_dangerous("mail_save"))

    async def test_http_plugin_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            links = root / "links.jsonl"
            diary = root / "diary"
            configure_api(root=root)
            with patch.dict(os.environ, {"WITTY_LINKS_FILE": str(links), "WITTY_DIARY_DIR": str(diary)}):
                status, plugins = await handle_request("GET", "/v1/plugins")
                self.assertEqual(status, 200)
                self.assertTrue(plugins["protected"])
                status, mail = await handle_request("GET", "/v1/mail")
                self.assertEqual(status, 200)
                self.assertIn("drafts", mail)
                self.assertNotIn("secret", str(mail).lower())
                self.assertFalse(mail.get("imap_password"))
                status, created = await handle_request(
                    "POST",
                    "/v1/links",
                    {"url": "http://192.168.0.10/oa", "title": "OA", "intent": "报周报", "alias": "审批网"},
                )
                self.assertEqual(status, 200)
                self.assertIn("审批网", created["link"].get("aliases") or [])
                await handle_request(
                    "POST",
                    "/v1/links",
                    {"url": "http://192.168.0.10/rare", "title": "冷门", "intent": "偶尔"},
                )
                status, listed = await handle_request("GET", "/v1/links?q=OA")
                self.assertEqual(status, 200)
                self.assertEqual(listed["links"][0]["title"], "OA")
                self.assertIn("审批网", listed["links"][0].get("aliases") or [])
                status, ranked = await handle_request("GET", "/v1/links")
                self.assertEqual(status, 200)
                self.assertEqual(ranked["links"][0]["title"], "OA")
                self.assertIn("OA", ranked.get("habits") or "")
                status, wrote = await handle_request("POST", "/v1/diary", {"text": "今天下午开了验收会"})
                self.assertEqual(status, 200)
                status, body = await handle_request("GET", "/v1/diary")
                self.assertEqual(status, 200)
                self.assertIn("验收会", body["body"])


if __name__ == "__main__":
    unittest.main()
