from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.evolution.cases import freeze_benchmark, list_cases, score_artifacts, write_case
from witty_agent.evolution.optimize import run_optimize_loop
from witty_agent.evolution.protocol import parse_eval_report
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.loop import LoopConfig, run_agent_loop
from witty_agent.session import create_agent, create_session
from witty_agent.store import (
    append_message,
    append_title,
    list_trace_summaries,
    read_session_meta,
    write_header,
)
from witty_agent.types import AgentContext, AgentMessage, ModelRef


class EvolutionCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_eval_protocol_and_rubric(self) -> None:
        report = parse_eval_report("status: ok\ncase_id: CASE-001-file-summary\nrun: 1\nscore: 80\n")
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.score, 80)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "summary.md").write_text("hello 120ms\n", encoding="utf-8")
            score = score_artifacts(
                workspace,
                "- 50 pts: `summary.md` exists\n- 50 pts: `summary.md` contains: 120ms\n",
            )
            self.assertEqual(score, 100)

    async def test_optimize_keeps_strictly_higher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = create_agent("grid-base", "evolving", root=root)
            write_case(
                agent.record,
                "cap",
                "CASE-001-file-summary",
                statement="write summary.md",
                rubric="- 100 pts: `summary.md` exists\n",
                root=root,
            )
            freeze_benchmark(agent.record, "cap", root=root)
            hits = {"n": 0}

            async def runner(case, workspace: Path) -> None:
                hits["n"] += 1
                if hits["n"] > 1:
                    (workspace / "summary.md").write_text("ok\n", encoding="utf-8")

            async def mutate() -> None:
                (agent.record.state_dir / "AGENTS.md").write_text("be precise\n", encoding="utf-8")

            result = await run_optimize_loop(
                agent.record,
                "cap",
                workspace=root / "eval",
                runner=runner,
                mutate=mutate,
                hypothesis="writing the file will raise the score",
                root=root,
            )
            self.assertTrue(result.keep)
            self.assertGreater(result.after, result.before)
            self.assertTrue((agent.record.state_dir / "evolution_log.md").is_file())
            self.assertEqual(len(list_cases(agent.record, "cap", root=root)), 1)

    async def test_input_subagent_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            create_agent("grid-base", "worker", root=root)
            parent = create_agent("grid-base", "planner", root=root)
            session = create_session(parent, workspace_dir=workspace)
            llm = ScriptedLLM(
                [
                    tool_reply("run_subagent", {"agent_id": "worker", "prompt": "first"}, call_id="a"),
                    text_reply("child-one"),
                    text_reply("parent-one"),
                    tool_reply(
                        "input_subagent",
                        {"subagent_id": "PLACEHOLDER", "prompt": "again"},
                        call_id="b",
                    ),
                    text_reply("child-two"),
                    text_reply("parent-two"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                if name == "input_subagent" and args.get("subagent_id") == "PLACEHOLDER":
                    from witty_agent import hooks

                    sid = next(iter(hooks.subagent_sessions))
                    args["subagent_id"] = sid
                return "allow"

            first = await session.run("go", stream_fn=llm, approve=allow)
            from witty_agent import hooks

            tools = [item for item in first.messages if item.role == "toolResult"]
            self.assertTrue(any("child-one" in item.text() for item in tools))
            self.assertTrue(hooks.subagent_sessions)
            sid = next(iter(hooks.subagent_sessions))
            llm2 = ScriptedLLM(
                [
                    tool_reply("input_subagent", {"subagent_id": sid, "prompt": "again"}, call_id="c"),
                    text_reply("child-two"),
                    text_reply("done"),
                ]
            )
            second = await session.run("continue", stream_fn=llm2, approve=allow)
            follow = [item.text() for item in second.messages if item.role == "toolResult"]
            self.assertTrue(any("child-two" in text for text in follow), follow)

    async def test_max_turns_and_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            context = AgentContext(
                system_prompt="s",
                messages=[],
                tools=[],
                workspace_dir=str(workspace),
                model=ModelRef("openai", "x"),
                project_id="p",
                agent_id="a",
                session_id="s",
            )
            result = await run_agent_loop(
                [AgentMessage(role="user", content="hi")],
                context,
                ScriptedLLM([text_reply("one"), text_reply("two")]),
                LoopConfig(max_turns=1, retry_attempts=1),
            )
            assistants = [item for item in result.messages if item.role == "assistant"]
            self.assertEqual(len(assistants), 1)
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            await session.run("summarize the notes today", stream_fn=ScriptedLLM([text_reply("ok")]))
            self.assertEqual(session.title, "summarize the notes today")
            summaries = list_trace_summaries(session._store_path().parent)
            self.assertTrue(any(item["title"] == "summarize the notes today" for item in summaries))

    def test_default_agent_gets_example_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = create_agent("grid-base", "default_agent", root=Path(tmp))
            cases = list_cases(agent.record, "example-benchmark", root=Path(tmp))
            self.assertGreaterEqual(len(cases), 2)


class SessionTopicTests(unittest.TestCase):
    def test_untitled_uses_first_user_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abc.jsonl"
            write_header(path, "abc", str(tmp))
            append_message(path, AgentMessage(role="user", content="第一句主题\n第二行"))
            append_message(path, AgentMessage(role="assistant", content="好的"))
            meta = read_session_meta(path)
            self.assertEqual(meta["title"], "第一句主题")

    def test_stored_title_wins_over_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abc.jsonl"
            write_header(path, "abc", str(tmp))
            append_message(path, AgentMessage(role="user", content="第一句"))
            append_title(path, "指定主题")
            meta = read_session_meta(path)
            self.assertEqual(meta["title"], "指定主题")


if __name__ == "__main__":
    unittest.main()
