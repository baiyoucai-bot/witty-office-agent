from __future__ import annotations

import unittest

from witty_agent import hooks
from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plugins.session_health import session_health
from witty_agent.session_log import SessionLog
from witty_agent.skills import match_relevant_skills
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class SessionHealthPluginTests(unittest.TestCase):
    def tearDown(self) -> None:
        hooks.session_log = None

    def test_not_a_kernel_tool(self) -> None:
        self.assertNotIn("session_health", KERNEL_TOOLS)
        names = {item.name for item in list_tools()}
        self.assertIn("session_health", names)
        self.assertIn("session_health", READONLY_TOOLS)

    def test_needs_session(self) -> None:
        hooks.session_log = None
        self.assertIn("需要所属会话", session_health())

    def test_reports_unpaired_and_plan(self) -> None:
        log = SessionLog()
        log.append("turn/start", {"turn": 1})
        log.append(
            "assistant/message",
            {"text": "", "tool_calls": [{"id": "c1", "name": "write", "arguments": {}}]},
        )
        log.append("plan/mode", {"active": True})
        log.append("todo/write", {"todos": [{"content": "写完再停", "status": "pending"}]})
        hooks.session_log = log
        text = session_health()
        self.assertIn("未收口", text)
        self.assertIn("write#c1", text)
        self.assertIn("写完再停", text)
        self.assertIn("开", text)

    def test_skill_matches_health_prompt(self) -> None:
        names = [item.name for item in match_relevant_skills("检查一下会话健康")]
        self.assertEqual(names, ["session-health"])
        self.assertNotIn(
            "session-health",
            [item.name for item in match_relevant_skills("review the auth module")],
        )

    def test_advertised_on_health_prompt(self) -> None:
        names = select_advertised_names("检查一下会话健康", ["session_query", "session_health", "read"])
        self.assertIn("session_health", names)
        self.assertIn("session_query", names)
        idle = select_advertised_names("review the auth module", ["session_query", "session_health", "read"])
        self.assertNotIn("session_health", idle)
