from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.evolution import append_score, ensure_benchmark, restore_snapshot, save_snapshot
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.session import create_agent, create_session, list_project_agents
from witty_agent.skills import list_skills, load_skill, match_relevant_skills
from witty_agent.state.agent_state import load_agent_state


class FusionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_agent_and_text_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["WITTY_HOME"] = tmp
            one = create_agent("grid-base", "planner", root=root)
            two = create_agent("grid-base", "worker", root=root)
            self.assertEqual(set(list_project_agents("grid-base", root=root)), {"planner", "worker"})
            workspace = root / "ws"
            workspace.mkdir()
            session = create_session(one, workspace_dir=workspace)
            result = await session.run("hello", stream_fn=ScriptedLLM([text_reply("ok")]))
            self.assertEqual(result.messages[-1].text(), "ok")
            self.assertTrue(any(event.type == "agent_end" for event in result.events))
            self.assertTrue((root / "grid-base" / "agents" / "planner" / "traces").exists())
            self.assertEqual(two.record.agent_id, "worker")

    async def test_dangerous_tool_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["WITTY_HOME"] = tmp
            os.environ["WITTY_WORKSPACE"] = tmp
            agent = create_agent("grid-base", "writer", root=root)
            workspace = root / "ws"
            workspace.mkdir()
            session = create_session(agent, workspace_dir=workspace)
            llm = ScriptedLLM(
                [
                    tool_reply("write", {"path": "a.txt", "content": "x"}),
                    text_reply("done"),
                ]
            )

            async def deny(name: str, call_id: str, args: dict) -> str:
                return "deny"

            denied = await session.run("w", stream_fn=llm, approve=deny)
            denied_tools = [item for item in denied.messages if item.role == "toolResult"]
            self.assertTrue(denied_tools and denied_tools[0].is_error)
            self.assertFalse((workspace / "a.txt").exists())

            llm2 = ScriptedLLM(
                [
                    tool_reply("write", {"path": "a.txt", "content": "x"}, call_id="c2"),
                    text_reply("done"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            allowed = await session.run("w", stream_fn=llm2, approve=allow)
            self.assertTrue((workspace / "a.txt").is_file())
            wrote = [item for item in allowed.messages if item.role == "toolResult"]
            self.assertTrue(wrote and "wrote" in wrote[0].text())

    def test_snapshot_and_scoreboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "evolving", root=root)
            record = load_agent_state("grid-base", "evolving", root=root)
            snap = save_snapshot(record, root=root)
            self.assertTrue(snap.is_file())
            agents_md = record.state_dir / "AGENTS.md"
            agents_md.write_text("changed\n", encoding="utf-8")
            restore_snapshot(record, 1, root=root)
            self.assertNotEqual(agents_md.read_text(encoding="utf-8"), "changed\n")
            ensure_benchmark(record, "smoke", root=root)
            row = append_score(record, "smoke", score=81.5, summary="baseline", root=root)
            self.assertEqual(row["score"], 81.5)

    def test_optimization_skill_is_loadable(self) -> None:
        names = {item.name for item in list_skills()}
        self.assertIn("agent-optimization", names)
        self.assertIn("skill-optimization", names)
        self.assertIn("agent-evaluation", names)
        self.assertIn("agent-creation", names)
        self.assertIn("benchmark-design", names)
        self.assertIn("data-analysis", names)
        self.assertIn("office-document", names)
        self.assertIn("slides", names)
        self.assertIn("software-engineering", names)
        self.assertIn("skill-porting", names)
        skill = next(item for item in list_skills() if item.name == "skill-optimization")
        self.assertIn("冻结 Benchmark", skill.description)
        self.assertIn("严格决策", load_skill("skill-optimization").body)
        self.assertEqual(
            [item.name for item in match_relevant_skills("优化一个 skill 的路由和正文")],
            ["skill-optimization"],
        )
        self.assertEqual(
            [item.name for item in match_relevant_skills("优化智能体的 AGENTS.md")],
            ["agent-optimization"],
        )


if __name__ == "__main__":
    unittest.main()
