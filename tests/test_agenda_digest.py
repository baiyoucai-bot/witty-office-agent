from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from witty_agent import hooks
from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plugins.agenda_digest import agenda_digest
from witty_agent.schedule import ScheduleDefinition, write_schedule
from witty_agent.session import create_agent
from witty_agent.skills import match_relevant_skills
from witty_agent.tools import list_tools
from witty_agent.tool_surface import select_advertised_names


class AgendaDigestPluginTests(unittest.TestCase):
    def tearDown(self) -> None:
        hooks.current_project_id = ""
        hooks.current_agent_id = ""
        hooks.current_root = None

    def test_not_a_kernel_tool(self) -> None:
        self.assertNotIn("agenda_digest", KERNEL_TOOLS)
        names = {item.name for item in list_tools()}
        self.assertIn("agenda_digest", names)
        self.assertIn("agenda_digest", READONLY_TOOLS)

    def test_empty_and_next_fire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "coder", root=root)
            hooks.current_project_id = "grid-base"
            hooks.current_agent_id = "coder"
            hooks.current_root = root
            self.assertIn("没有定时任务", agenda_digest())
            start = datetime.now(timezone.utc) + timedelta(hours=2)
            write_schedule(
                ScheduleDefinition(
                    name="standup",
                    prompt="站会摘要",
                    enabled=True,
                    start_at=start.isoformat(),
                    start_at_ms=int(start.timestamp() * 1000),
                    period="1d",
                    period_ms=86_400_000,
                ),
                "grid-base",
                "coder",
                root=root,
            )
            write_schedule(
                ScheduleDefinition(
                    name="paused",
                    prompt="先不开",
                    enabled=False,
                    start_at=start.isoformat(),
                    start_at_ms=int(start.timestamp() * 1000),
                ),
                "grid-base",
                "coder",
                root=root,
            )
            text = agenda_digest()
            self.assertIn("standup", text)
            self.assertIn("站会摘要", text)
            self.assertIn("已暂停", text)
            self.assertIn("启用 1", text)
            self.assertIn("暂停 1", text)

    def test_skill_matches_digest_prompt(self) -> None:
        names = [item.name for item in match_relevant_skills("看一下今日日程摘要")]
        self.assertEqual(names[:1], ["agenda-digest"])
        self.assertNotIn("agenda-digest", [item.name for item in match_relevant_skills("创建定时任务")])

    def test_advertised_on_digest_prompt(self) -> None:
        names = select_advertised_names("今日日程摘要", ["agenda_digest", "schedule_list", "read"])
        self.assertIn("agenda_digest", names)
        idle = select_advertised_names("review the auth module", ["agenda_digest", "read"])
        self.assertNotIn("agenda_digest", idle)
