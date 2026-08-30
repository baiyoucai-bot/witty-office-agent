from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.http_api import configure_api, handle_request
from witty_agent.kernel_surface import KERNEL_COMMANDS
from witty_agent.llm import ScriptedLLM, text_reply
from witty_agent.session import create_agent, create_session
from witty_agent.skill_scaffold import (
    create_skill_from_brief,
    parse_create_skill_args,
    slug_from_brief,
)
from witty_agent.skills import list_skills, load_skill


class SkillScaffoldTests(unittest.IsolatedAsyncioTestCase):
    def test_slug_and_args(self) -> None:
        self.assertEqual(slug_from_brief("检查发票抬头和税号"), "check-invoice-title-tax")
        self.assertEqual(slug_from_brief("check invoice format"), "check-invoice-format")
        self.assertEqual(slug_from_brief("检查发票", "invoice-check"), "invoice-check")
        self.assertTrue(slug_from_brief("随便聊聊天气").startswith("user-skill-"))
        brief, name, overwrite = parse_create_skill_args(
            "--overwrite invoice-check 检查发票抬头"
        )
        self.assertEqual(brief, "检查发票抬头")
        self.assertEqual(name, "invoice-check")
        self.assertTrue(overwrite)

    def test_create_from_one_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = create_skill_from_brief(
                "检查发票抬头和税号",
                project_id="grid-base",
                agent_id="coder",
                root=root,
            )
            self.assertEqual(meta.name, "check-invoice-title-tax")
            self.assertEqual(meta.origin, "user")
            loaded = load_skill("check-invoice-title-tax", "grid-base", "coder", root=root)
            self.assertIn("检查发票抬头和税号", loaded.body)
            self.assertTrue(any(item.name == meta.name for item in list_skills("grid-base", "coder", root=root)))
            with self.assertRaises(FileExistsError):
                create_skill_from_brief(
                    "检查发票抬头和税号",
                    project_id="grid-base",
                    agent_id="coder",
                    root=root,
                )
            again = create_skill_from_brief(
                "检查发票抬头和税号",
                overwrite=True,
                project_id="grid-base",
                agent_id="coder",
                root=root,
            )
            self.assertEqual(again.name, meta.name)

    async def test_slash_command_installs_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            names = {item["name"] for item in session.slash_commands()}
            self.assertIn("create-skill", names)
            self.assertNotIn("create-skill", KERNEL_COMMANDS)
            result = await session.run(
                "/create-skill 检查发票抬头和税号",
                stream_fn=ScriptedLLM([text_reply("should-not-run")]),
            )
            self.assertIn("check-invoice-title-tax", result.messages[0].text())
            self.assertTrue(
                any(
                    item.name == "check-invoice-title-tax"
                    for item in list_skills("grid-base", "coder", root=root)
                )
            )
            empty = await session.run(
                "/create-skill",
                stream_fn=ScriptedLLM([text_reply("should-not-run")]),
            )
            self.assertIn("用法", empty.messages[0].text())

    async def test_http_brief_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            status, body = await handle_request(
                "POST",
                "/v1/skills",
                {
                    "brief": "检查发票抬头和税号",
                    "project_id": "grid-base",
                    "agent_id": "coder",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["name"], "check-invoice-title-tax")
            self.assertEqual(body["origin"], "user")
            status, conflict = await handle_request(
                "POST",
                "/v1/skills",
                {
                    "brief": "检查发票抬头和税号",
                    "project_id": "grid-base",
                    "agent_id": "coder",
                },
            )
            self.assertEqual(status, 409)
            self.assertEqual(conflict["name"], "check-invoice-title-tax")
