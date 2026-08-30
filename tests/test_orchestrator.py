from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from witty_agent.dispatch import Allocation
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.orchestrator import JobSpec, Orchestrator, list_jobs
from witty_agent.schedule import ScheduleDefinition, write_schedule
from witty_agent.session import create_agent
from witty_agent.types import AgentContext, AgentMessage


class EchoLLM:
    async def __call__(self, context: AgentContext) -> AgentMessage:
        last = ""
        for message in reversed(context.messages):
            source = str(message.source or "")
            if message.role == "user" and not source.startswith("plugin:"):
                last = message.text()
                break
        if not last and context.messages:
            last = context.messages[-1].text()
        return text_reply(f"ok:{last[:40]}")


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_chat_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            create_agent("grid-base", "coder", root=root)
            orch = Orchestrator(root, EchoLLM())
            result = await orch.dispatch(
                JobSpec(
                    prompt="hello world",
                    kind="chat",
                    project_id="grid-base",
                    agent_id="coder",
                    workspace=workspace,
                )
            )
            self.assertEqual(result.status, "completed")
            self.assertIn("hello", result.text)
            self.assertTrue(result.session_id)
            jobs = list_jobs("grid-base", root=root)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["job_id"], result.job_id)

    async def test_fanout_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            create_agent("grid-base", "coder", root=root)
            orch = Orchestrator(root, EchoLLM())
            result = await orch.fanout(
                ["inspect A", "inspect B", "inspect C"],
                JobSpec(prompt="", project_id="grid-base", agent_id="coder", workspace=workspace),
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(result.children), 3)
            self.assertIn("inspect A", result.text)
            self.assertIn("inspect B", result.text)

    async def test_fanout_refuses_cheap_lookups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            create_agent("grid-base", "coder", root=root)
            orch = Orchestrator(root, EchoLLM())
            result = await orch.fanout(
                ["read a.py", "ls src", "read a.py"],
                JobSpec(prompt="", project_id="grid-base", agent_id="coder", workspace=workspace),
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error, "all_cheap")
            # 拒绝话术在 prompts.toml，测试从同一份配置取，别抄字面。
            self.assertEqual(result.text, Allocation("serial", False, "all_cheap").message)
            self.assertEqual(result.children, [])

    async def test_plan_then_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            create_agent("grid-base", "coder", root=root)
            llm = ScriptedLLM(
                [
                    tool_reply("plan_write", {"body": "- read\n- write"}, call_id="p1"),
                    text_reply("planned"),
                    text_reply("executed"),
                ]
            )
            orch = Orchestrator(root, llm)

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            orch.approve = allow
            result = await orch.dispatch(
                JobSpec(
                    prompt="ship the report",
                    kind="plan",
                    project_id="grid-base",
                    agent_id="coder",
                    workspace=workspace,
                )
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.rounds, 2)
            self.assertIn("executed", result.text)

    async def test_schedule_tick_runs_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            create_agent("grid-base", "coder", root=root)
            start = datetime.now(timezone.utc) + timedelta(seconds=2)
            write_schedule(
                ScheduleDefinition(
                    name="once",
                    prompt="nightly check",
                    enabled=True,
                    start_at=start.isoformat(),
                    start_at_ms=int(start.timestamp() * 1000),
                    workspace=str(workspace),
                ),
                "grid-base",
                "coder",
                root=root,
            )
            clock = {"now": int((start - timedelta(minutes=1)).timestamp() * 1000)}
            from witty_agent.schedule import Scheduler

            Scheduler(root, now_ms=lambda: clock["now"]).tick()
            clock["now"] = int((start + timedelta(seconds=1)).timestamp() * 1000)
            orch = Orchestrator(root, EchoLLM())
            # reuse same clock by constructing scheduler inside tick_and_run — it uses wall clock.
            # Drive fires through dispatch after a second scheduler tick with injected clock.
            fires = Scheduler(root, now_ms=lambda: clock["now"]).tick()
            self.assertEqual(len(fires), 1)
            result = await orch.dispatch(
                JobSpec(
                    kind="schedule",
                    prompt=fires[0].prompt,
                    project_id="grid-base",
                    agent_id="coder",
                    workspace=workspace,
                )
            )
            self.assertEqual(result.status, "completed")
            self.assertTrue(result.text.startswith("ok:"))
            self.assertIn("scheduled_task", fires[0].prompt)


if __name__ == "__main__":
    unittest.main()
