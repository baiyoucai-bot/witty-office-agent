from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.approval import is_dangerous
from witty_agent.kernel_surface import KERNEL_TOOLS, is_kernel_tool, is_kernel_tool_module
from witty_agent.plan_mode import MUTATING_TOOLS
from witty_agent.plugins.mail import (
    MemoryMailbox,
    email_analyze,
    probe_live,
    email_attach,
    email_draft,
    email_list,
    email_read,
    email_send,
    email_status,
    set_mail_backend,
)
from witty_agent.prompts import get_prompt
from witty_agent.skills import match_relevant_skills
from witty_agent.system_prompt import build_system_prompt
from witty_agent.tools import list_tools


class EmailPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.drafts = self.home / "drafts"
        self.drafts.mkdir()
        os.environ["WITTY_HOME"] = str(self.home)
        os.environ["WITTY_MAIL_DRAFTS"] = str(self.drafts)
        self.box = MemoryMailbox()
        self.box.add(
            uid="12",
            mailbox="INBOX",
            **{
                "from": "lead@intranet.grid",
                "to": ["me@intranet.grid"],
                "subject": "请于周五前提交周报",
                "date": "2026-08-17",
                "body": "请尽快把本周周报发到办公室。截止日期周五。",
                "attachments": [{"name": "template.docx", "size": 10}],
            },
        )
        set_mail_backend(self.box)

    def tearDown(self) -> None:
        set_mail_backend(None)
        os.environ.pop("WITTY_HOME", None)
        os.environ.pop("WITTY_MAIL_DRAFTS", None)
        self.tmp.cleanup()

    def test_business_tools_are_not_kernel(self) -> None:
        names = {item.name for item in list_tools()}
        for name in (
            "mail_status",
            "mail_list",
            "mail_read",
            "mail_analyze",
            "mail_draft",
            "mail_attach",
            "mail_send",
        ):
            self.assertIn(name, names)
            self.assertFalse(is_kernel_tool(name))
            self.assertNotIn(name, KERNEL_TOOLS)
        self.assertFalse(is_kernel_tool_module("witty_agent.plugins.mail"))
        self.assertEqual([item.name for item in match_relevant_skills("看一下收件箱")], ["mail-desk"])
        self.assertEqual([item.name for item in match_relevant_skills("帮我回一封邮件")], ["mail-desk"])
        self.assertTrue(is_dangerous("mail_send"))
        self.assertFalse(is_dangerous("mail_list"))
        self.assertIn("mail_send", MUTATING_TOOLS)
        self.assertNotIn("mail_read", MUTATING_TOOLS)

    def test_status_hides_password(self) -> None:
        os.environ["WITTY_IMAP_PASSWORD"] = "secret-token"
        try:
            text = email_status()
        finally:
            os.environ.pop("WITTY_IMAP_PASSWORD", None)
        self.assertIn("IMAP", text)
        self.assertIn("已设", text)

    def test_probe_live_exits_when_unconfigured(self) -> None:
        from witty_agent.runtime import clear_runtime_cache

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "WITTY_HOME": tmp,
                "WITTY_IMAP_HOST": "",
                "WITTY_SMTP_HOST": "",
                "WITTY_MAIL_USER": "",
                "WITTY_IMAP_PASSWORD": "",
            }
            from unittest.mock import patch

            with patch.dict(os.environ, env, clear=False):
                clear_runtime_cache()
                code = probe_live()
        self.assertEqual(code, 2)

    def test_list_read_analyze_without_network(self) -> None:
        listed = email_list(query="周报")
        self.assertIn("12", listed)
        self.assertIn("请于周五前提交周报", listed)
        body = email_read("12")
        self.assertIn("lead@intranet.grid", body)
        self.assertIn("template.docx", body)
        analysis = email_analyze(uid="12")
        self.assertIn("request", analysis)
        self.assertIn("截止日期周五", analysis)
        pasted = email_analyze(text="请确认明日会议议程")
        self.assertIn("meeting", pasted)
        self.assertIn(get_prompt("email_uid_missing", uid="99"), email_read("99"))

    def test_draft_attach_and_send(self) -> None:
        saved = email_draft(
            to="office@intranet.grid",
            subject="本周周报",
            body="附件是周报。",
        )
        self.assertIn("d-", saved)
        draft_id = saved.split()[1]
        attach = self.home / "week.txt"
        attach.write_text("done", encoding="utf-8")
        self.assertIn(draft_id, email_attach(draft_id, str(attach)))
        self.assertIn("本周周报", email_analyze(draft_id=draft_id))
        sent = email_send(draft_id)
        self.assertIn("本周周报", sent)
        self.assertEqual(self.box.sent[0]["to"], ["office@intranet.grid"])
        self.assertEqual(self.box.sent[0]["attachments"][0]["name"], "week.txt")
        self.assertIn(get_prompt("email_send_need_to"), email_send(email_draft().split()[1]))

    def test_unconfigured_stdlib_explains(self) -> None:
        set_mail_backend(None)
        text = email_list()
        self.assertIn("未配置", text)
        self.assertNotIn("gmail", text.casefold())

    def test_system_prompt_explains_mail_chain(self) -> None:
        text = build_system_prompt(".", tool_names=["mail_list", "mail_send"], prompt="看一下收件箱")
        self.assertIn("mail_analyze", text)
        self.assertIn("WITTY_IMAP_PASSWORD", text)
        self.assertIn("不要假设", text)


if __name__ == "__main__":
    unittest.main()
