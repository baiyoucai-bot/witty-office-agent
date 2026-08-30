from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from witty_agent.capability import CapabilityRegistry
from witty_agent.guard import (
    AnswerNowReminder,
    AskGate,
    EvidenceGate,
    FailStrategyReminder,
    PlanPresentGate,
    ProgressGuard,
    RepeatToolReminder,
    TodoGate,
    needs_choice,
    is_choice_only,
    poses_choice,
    questions_from_assistant_text,
    needs_evidence,
    needs_memory_browse,
    needs_todo,
    recalled_answer_hint,
    recalled_relocations,
    recalled_verify_hint,
    recalled_verify_paths,
    autoload_recalled_verify,
    autoload_browse_read,
    browse_read_hits,
    relevant_browse_rows,
    excerpt_paths,
)
from witty_agent.trace import collect_turn_evidence
from witty_agent.tools.fs import bind_workspace
from witty_agent.memory import (
    apply_relocated_hits,
    resolve_session_memory,
    rewrite_relocated_paths,
    topic_body,
    write_topic,
)
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.loop import LoopConfig, run_agent_loop
from witty_agent.plan_mode import (
    PlanModeController,
    first_heading,
    maybe_auto_enter,
    needs_plan,
    plan_steps,
)
from witty_agent.prompts import get_prompt
from witty_agent.invariants import run_invariants
from witty_agent.session import create_agent, create_session
from witty_agent.session_log import (
    SessionLog,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    derive_messages,
    fold_plan_mode,
    fold_todos,
    interrupted_turn_closers,
    project_todos,
    repair_session_log,
    unpaired_call_results,
)
from witty_agent.store import append_session_event, load_messages, load_session_events, write_header
from witty_agent.time_context import maybe_inject
from witty_agent.system_prompt import build_system_prompt
from witty_agent.todo import (
    apply_todo_write,
    completed_titles,
    current_todos,
    format_todo_section,
    has_open_todos,
    to_todo_list,
)
from witty_agent.tools import list_tools
from witty_agent.tools.registry import ToolSpec
from witty_agent.types import AgentContext, AgentMessage, ModelRef, ToolCallBlock
from witty_agent.user_questions import (
    AskUserAnswer,
    AskUserAnswerItem,
    UserQuestionError,
    UserQuestionService,
)


def _context(tools: list | None = None) -> AgentContext:
    return AgentContext(
        system_prompt="sys",
        messages=[],
        tools=tools or [],
        workspace_dir=".",
        model=ModelRef(provider="openai", model_id="test"),
        project_id="grid-base",
        agent_id="coder",
        session_id="s1",
    )


class SessionLogTests(unittest.IsolatedAsyncioTestCase):
    def test_fold_todos_last_write_wins(self) -> None:
        log = SessionLog()
        log.append("todo/write", {"todos": [{"content": "a", "status": "pending"}]})
        log.append("todo/write", {"todos": [{"content": "b", "status": "completed"}]})
        self.assertEqual(fold_todos(log.events)[0]["content"], "b")

    def test_project_todos_clears_on_turn_start(self) -> None:
        log = SessionLog()
        log.append("todo/write", {"todos": [{"content": "a", "status": "pending"}]})
        log.append("turn/start", {"turn": 2})
        self.assertIsNone(project_todos(log.events))
        self.assertEqual(fold_todos(log.events)[0]["content"], "a")

    def test_fold_plan_mode(self) -> None:
        log = SessionLog()
        self.assertFalse(fold_plan_mode(log.events))
        log.append("plan/mode", {"active": True})
        log.append("plan/mode", {"active": False})
        self.assertFalse(fold_plan_mode(log.events))
        log.append("plan/mode", {"active": True})
        self.assertTrue(fold_plan_mode(log.events))

    def test_derive_messages_skips_empty_assistant(self) -> None:
        log = SessionLog()
        log.append("user/message", {"text": "hi", "source": "user"})
        log.append("assistant/message", {"text": "", "tool_calls": []})
        log.append("assistant/message", {"text": "hello", "tool_calls": []})
        messages = derive_messages(log.events)
        self.assertEqual([item.role for item in messages], ["user", "assistant"])
        self.assertEqual(messages[1].text(), "hello")

    def test_interrupted_turn_not_started_and_unknown(self) -> None:
        log = SessionLog()
        log.append("turn/start", {"turn": 1})
        log.append("step/start", {"turn": 1, "step": 1})
        log.append(
            "assistant/message",
            {
                "text": "",
                "tool_calls": [{"id": "c1", "name": "bash", "arguments": {"command": "pwd"}}],
            },
        )
        closers = interrupted_turn_closers(log.events)
        self.assertEqual(closers[0].type, "tool/result")
        self.assertEqual(closers[0].data["code"], TOOL_NOT_STARTED)
        self.assertIn("中断", closers[0].data["text"])
        self.assertEqual(closers[-2].type, "step/end")
        self.assertEqual(closers[-1].type, "turn/end")
        self.assertEqual(closers[-1].data["reason"], "interrupted")
        log.append("tool/call", {"id": "c1", "name": "bash"})
        closers = interrupted_turn_closers(log.events)
        self.assertEqual(closers[0].data["code"], TOOL_OUTCOME_UNKNOWN)
        self.assertIn("未知", closers[0].data["text"])

    def test_closed_turn_still_fills_unpaired_result(self) -> None:
        log = SessionLog()
        log.append("turn/start", {"turn": 1})
        log.append(
            "assistant/message",
            {"text": "", "tool_calls": [{"id": "c2", "name": "read", "arguments": {}}]},
        )
        log.append("turn/end", {"turn": 1})
        self.assertEqual(interrupted_turn_closers(log.events), [])
        self.assertEqual(unpaired_call_results(log.events)[0].data["code"], TOOL_NOT_STARTED)
        added = repair_session_log(log)
        self.assertEqual(added[0].type, "tool/result")
        derived = derive_messages(log.events)
        self.assertEqual(derived[-1].role, "toolResult")
        self.assertTrue(derived[-1].is_error)
        self.assertEqual(derived[-1].tool_call_id, "c2")
        self.assertEqual(repair_session_log(log), [])

    def test_balanced_log_needs_no_repair(self) -> None:
        log = SessionLog()
        log.append("turn/start", {"turn": 1})
        log.append("assistant/message", {"text": "ok", "tool_calls": []})
        log.append("turn/end", {"turn": 1})
        self.assertEqual(interrupted_turn_closers(log.events), [])
        self.assertEqual(repair_session_log(log), [])

    async def test_hydrate_repairs_open_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="repair-open")
            session.log.append("turn/start", {"turn": 1})
            session.log.append("step/start", {"turn": 1, "step": 1})
            session.log.append(
                "assistant/message",
                {
                    "text": "",
                    "tool_calls": [{"id": "c9", "name": "write", "arguments": {"path": "a"}}],
                },
            )
            path = session._store_path()
            write_header(path, session.session_id, str(session.workspace_dir), None)
            for event in session.log.events:
                append_session_event(path, event)
            reloaded = create_session(agent, workspace_dir=workspace, session_id="repair-open")
            reloaded._hydrate_log()
            self.assertFalse(reloaded.log.has_open_turn())
            derived = derive_messages(reloaded.log.events)
            hit = next(item for item in derived if item.role == "toolResult")
            self.assertEqual(hit.tool_call_id, "c9")
            self.assertTrue(hit.is_error)
            stored = load_messages(path)
            self.assertTrue(any(item.role == "toolResult" and item.tool_call_id == "c9" for item in stored))

    async def test_abort_after_tool_request_repairs_result(self) -> None:
        log = SessionLog()
        seen = {"n": 0}

        async def stream(_ctx):
            seen["n"] += 1
            return tool_reply("read", {"path": "note.txt"}, "c-abort")

        async def aborted() -> bool:
            return seen["n"] >= 1

        result = await run_agent_loop(
            [AgentMessage(role="user", content="read note")],
            _context(),
            stream,
            LoopConfig(session_log=log, is_aborted=aborted, max_turns=2),
        )
        results = [item for item in result.messages if item.role == "toolResult"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_error)
        self.assertEqual(results[0].source, "plugin:session-repair")
        self.assertEqual(results[0].tool_call_id, "c-abort")
        self.assertFalse(log.has_open_turn())
        self.assertTrue(
            any(
                item.type == "tool/result" and item.data.get("code") == TOOL_NOT_STARTED
                for item in log.events
            )
        )


class TodoTests(unittest.TestCase):
    def test_rejects_empty_duplicate_and_extra_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "非空"):
            to_todo_list([{"content": "  ", "status": "pending"}])
        with self.assertRaisesRegex(ValueError, "重复"):
            to_todo_list(
                [
                    {"content": "same", "status": "pending"},
                    {"content": "same", "status": "completed"},
                ]
            )
        with self.assertRaisesRegex(ValueError, "未知"):
            to_todo_list([{"content": "x", "status": "pending", "id": "1"}])

    def test_single_active_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "最多一条"):
            to_todo_list(
                [
                    {"content": "a", "status": "in_progress"},
                    {"content": "b", "status": "in_progress"},
                ],
                allow_parallel=False,
            )
        items = to_todo_list(
            [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ],
            allow_parallel=True,
        )
        self.assertEqual(len(items), 2)

    def test_write_needs_session(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "所属"):
            apply_todo_write(None, [{"content": "a", "status": "pending"}])

    def test_open_todos_survive_turn_start_in_prompt(self) -> None:
        log = SessionLog()
        apply_todo_write(log, [{"content": "read note", "status": "in_progress"}])
        log.append("turn/start", {"turn": 2})
        self.assertIsNone(project_todos(log.events))
        folded = current_todos(log)
        self.assertEqual(folded[0]["content"], "read note")
        section = format_todo_section(folded)
        self.assertIn("read note", section)
        text = build_system_prompt(".", tool_names=["read", "todo_write"], todo_section=section)
        self.assertIn("当前待办", text)
        self.assertIn("read note", text)
        self.assertEqual(format_todo_section(None), "")

    def test_completed_only_list_stays_off_prompt(self) -> None:
        log = SessionLog()
        apply_todo_write(
            log,
            [
                {"content": "read note", "status": "completed"},
                {"content": "write summary", "status": "completed"},
            ],
        )
        log.append("turn/start", {"turn": 2})
        folded = current_todos(log)
        self.assertIsNotNone(folded)
        self.assertEqual(len(folded), 2)
        self.assertFalse(has_open_todos(folded))
        section = format_todo_section(folded)
        self.assertIn("read note", section)
        self.assertIn("write summary", section)
        self.assertNotIn("- [completed]", section)
        self.assertIn("刚做完", section)
        self.assertEqual(completed_titles(folded), "read note; write summary")
        text = build_system_prompt(".", tool_names=["read", "todo_write"], todo_section=section)
        self.assertIn("当前待办", text)
        self.assertIn("read note", text)
        self.assertNotIn("- [completed] read note", text)
        self.assertEqual(format_todo_section([]), "")
        mixed = format_todo_section(
            [
                {"content": "read note", "status": "completed"},
                {"content": "write summary", "status": "pending"},
            ]
        )
        self.assertIn("read note", mixed)
        self.assertIn("write summary", mixed)

    def test_tools_include_harness_names(self) -> None:
        names = {item.name for item in list_tools()}
        self.assertIn("todo_write", names)
        self.assertIn("exit_plan_mode", names)
        self.assertIn("ask_user_question", names)


class PlanModeTests(unittest.IsolatedAsyncioTestCase):
    def test_set_commits_between_turns(self) -> None:
        log = SessionLog()
        controller = PlanModeController()
        self.assertEqual(controller.set(log, True), "committed")
        self.assertTrue(controller.get(log).active)
        self.assertEqual(controller.set(log, True), "noop")

    def test_pending_appended_on_pre_step(self) -> None:
        log = SessionLog()
        log.append("turn/start", {"turn": 1})
        controller = PlanModeController()
        self.assertEqual(controller.set(log, True), "queued")
        self.assertTrue(controller.get(log).pending)
        extras = controller.apply_pre_step(log)
        self.assertTrue(fold_plan_mode(log.events))
        self.assertEqual(extras[0].source, "plugin:plan-mode")

    def test_first_heading(self) -> None:
        self.assertEqual(first_heading("# Ship it\nbody"), "Ship it")
        self.assertIsNone(first_heading("no heading"))

    def test_plan_steps_from_bullets(self) -> None:
        self.assertEqual(plan_steps("# Auth rewrite\n- split tokens\n- add tests"), ["split tokens", "add tests"])
        self.assertEqual(plan_steps("# Ship\n1. do it\n2. check it"), ["do it", "check it"])
        self.assertEqual(plan_steps("# Empty\njust prose"), [])

    def test_blocks_mutating_only_when_active(self) -> None:
        from witty_agent.plan_mode import blocks_tool

        self.assertFalse(blocks_tool("write", active=False))
        self.assertTrue(blocks_tool("write", active=True))
        self.assertTrue(blocks_tool("bash", active=True))
        self.assertFalse(blocks_tool("read", active=True))
        self.assertFalse(blocks_tool("exit_plan_mode", active=True))

    def test_needs_plan_large_mutate_only(self) -> None:
        self.assertTrue(needs_plan("refactor the auth module"))
        self.assertTrue(needs_plan("rewrite session store and migrate callers"))
        self.assertTrue(needs_plan("重构认证模块并补测试"))
        self.assertTrue(needs_plan("实现登录模块的 OAuth2"))
        self.assertTrue(needs_plan("implement OAuth2 across the API"))
        self.assertTrue(needs_plan("update src/a.py and src/b.py"))
        self.assertFalse(needs_plan("write hello.py"))
        self.assertFalse(needs_plan("写一个 hello.py"))
        self.assertFalse(needs_plan("review the auth module and report risks"))
        self.assertFalse(needs_plan("你好"))
        self.assertFalse(needs_plan("OAuth2 还是 JWT？"))
        self.assertFalse(needs_plan("/plan off"))

    def test_maybe_auto_enter_once(self) -> None:
        log = SessionLog()
        controller = PlanModeController()
        notice = maybe_auto_enter(controller, log, "refactor the auth module")
        self.assertIsNotNone(notice)
        self.assertEqual(notice.source, "plugin:auto-plan")
        self.assertEqual(notice.text(), get_prompt("plan_auto_enter"))
        self.assertTrue(controller.get(log).active)
        self.assertIsNone(maybe_auto_enter(controller, log, "refactor the auth module"))
        self.assertIsNone(maybe_auto_enter(controller, log, "write hello.py"))
        skipped = PlanModeController()
        self.assertIsNone(maybe_auto_enter(skipped, SessionLog(), "refactor the auth module", enabled=False))

    async def test_exit_plan_mode_asks_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.plan.set(session.log, True)

            async def approve(_questions):
                return AskUserAnswer(
                    answers=[
                        AskUserAnswerItem(
                            id="plan-review",
                            selected=[get_prompt("plan_review_approve")],
                        )
                    ]
                )

            llm = ScriptedLLM(
                [
                    tool_reply("exit_plan_mode", {"plan": "# Ship\n- do it"}, call_id="e1"),
                    text_reply("done"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            result = await session.run(
                "finish",
                stream_fn=llm,
                approve=allow,
                approval_mode="allow-all",
                ask_user=approve,
            )
            self.assertFalse(session.plan.get(session.log).active)
            texts = [item.text() for item in result.messages if item.role == "toolResult"]
            self.assertTrue(any("approved" in text for text in texts))

    async def test_plan_approve_unlocks_write_and_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.plan.set(session.log, True)
            seen: list[set[str]] = []
            prompts: list[str] = []
            emitted: list[object] = []

            async def approve(_questions):
                return AskUserAnswer(
                    answers=[
                        AskUserAnswerItem(
                            id="plan-review",
                            selected=[get_prompt("plan_review_approve")],
                        )
                    ]
                )

            async def stream(ctx):
                seen.append({getattr(item, "name", "") for item in ctx.tools})
                prompts.append(ctx.system_prompt)
                if len(seen) == 1:
                    return tool_reply("exit_plan_mode", {"plan": "# Auth rewrite\n- split tokens"}, call_id="e1")
                return text_reply("executing")

            async def emit(event):
                emitted.append(event)

            result = await session.run(
                "refactor the auth module",
                stream_fn=stream,
                approval_mode="allow-all",
                ask_user=approve,
                emit=emit,
            )
            self.assertFalse(session.plan.get(session.log).active)
            self.assertGreaterEqual(len(seen), 2)
            self.assertNotIn("write", seen[0])
            self.assertIn("write", seen[1])
            self.assertIn("edit", seen[1])
            self.assertIn("bash", seen[1])
            sources = [item.source for item in result.messages]
            self.assertIn("plugin:plan-approved", sources)
            self.assertIn("plugin:skill-invocation", sources)
            self.assertTrue(any(item.type == "turn/plan-approved" for item in session.log.events))
            approved_plan = "# Auth rewrite\n- split tokens"
            self.assertEqual(
                [item for item in result.messages if item.source == "plugin:plan-approved"][0].text(),
                get_prompt("plan_approved", plan=approved_plan),
            )
            self.assertIn("<plan:policy>", prompts[0])
            self.assertNotIn("<plan:policy>", prompts[1])
            self.assertNotIn(get_prompt("skills_plan"), prompts[1])
            self.assertIn(get_prompt("guideline_dispatch"), prompts[1])
            todos = current_todos(session.log)
            self.assertIsNotNone(todos)
            self.assertEqual(todos[0]["content"], "split tokens")
            self.assertEqual(todos[0]["status"], "in_progress")
            self.assertIn("split tokens", prompts[1])
            self.assertTrue(any(item.type == "turn/plan-todos" for item in session.log.events))
            todo_events = [item for item in emitted if getattr(item, "type", "") == "todos"]
            self.assertEqual(len(todo_events), 1)
            self.assertEqual(todo_events[0].args["todos"][0]["content"], "split tokens")

    async def test_plan_mode_blocks_write_allows_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("alpha-source", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.plan.set(session.log, True)
            llm = ScriptedLLM(
                [
                    tool_reply("read", {"path": "note.txt"}, call_id="r1"),
                    tool_reply("write", {"path": "oops.txt", "content": "no"}, call_id="w1"),
                    text_reply("noted"),
                ]
            )
            result = await session.run(
                "research the note",
                stream_fn=llm,
                approval_mode="allow-all",
            )
            by_id = {
                item.tool_call_id: item
                for item in result.messages
                if item.role == "toolResult"
            }
            self.assertIn("alpha-source", by_id["r1"].text())
            self.assertFalse(by_id["r1"].is_error)
            self.assertTrue(by_id["w1"].is_error)
            self.assertIn("计划模式", by_id["w1"].text())
            self.assertFalse((workspace / "oops.txt").exists())
            self.assertTrue(session.plan.get(session.log).active)

    async def test_plan_mode_hides_mutating_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.plan.set(session.log, True)
            seen: list[list[str]] = []

            async def stream(ctx):
                seen.append([getattr(item, "name", "") for item in ctx.tools])
                return text_reply("planning")

            await session.run("research only", stream_fn=stream, approval_mode="allow-all")
            self.assertTrue(seen)
            names = set(seen[0])
            self.assertIn("read", names)
            self.assertIn("exit_plan_mode", names)
            self.assertNotIn("write", names)
            self.assertNotIn("bash", names)

    async def test_session_auto_enters_plan_for_refactor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            seen: list[list[str]] = []
            prompts: list[str] = []

            async def stream(ctx):
                seen.append([getattr(item, "name", "") for item in ctx.tools])
                prompts.append(ctx.system_prompt)
                return text_reply("planning")

            result = await session.run(
                "refactor the auth module",
                stream_fn=stream,
                approval_mode="allow-all",
            )
            self.assertTrue(session.plan.get(session.log).active)
            self.assertTrue(any(item.source == "plugin:auto-plan" for item in result.messages))
            self.assertTrue(any(item.type == "turn/auto-plan" for item in session.log.events))
            self.assertTrue(seen)
            names = set(seen[0])
            self.assertIn("exit_plan_mode", names)
            self.assertNotIn("write", names)
            self.assertTrue(prompts)
            self.assertIn("<plan:policy>", prompts[0])
            self.assertIn(get_prompt("skills_plan"), prompts[0])
            self.assertNotIn(get_prompt("guideline_dispatch"), prompts[0])
            self.assertNotIn(get_prompt("guideline_stop"), prompts[0])
            self.assertFalse(any(item.source == "plugin:skill-invocation" for item in result.messages))
            self.assertNotIn("skill", names)
            self.assertNotIn("list_available_skills", names)

            small = create_session(agent, workspace_dir=workspace)
            await small.run("write hello.py", stream_fn=ScriptedLLM([text_reply("ok")]), approval_mode="allow-all")
            self.assertFalse(small.plan.get(small.log).active)

    async def test_plan_present_gate_asks_for_exit_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.plan.set(session.log, True)
            result = await session.run(
                "refactor the auth module",
                stream_fn=ScriptedLLM(
                    [
                        text_reply("# Auth rewrite\n- read current flow\n- split tokens"),
                        text_reply("waiting"),
                    ]
                ),
                approval_mode="allow-all",
            )
            sources = [item.source for item in result.messages]
            self.assertIn("plugin:plan-present-gate", sources)
            self.assertNotIn("plugin:todo-gate", sources)
            self.assertTrue(any(item.type == "turn/plan-present-gate" for item in session.log.events))

    def test_plan_present_gate_once_and_todo_skipped(self) -> None:
        drafted = [
            AgentMessage(role="user", content="refactor the auth module"),
            text_reply("# Auth rewrite\n- read\n- split"),
        ]
        gate = PlanPresentGate(enabled=True)
        self.assertIsNone(gate.maybe_nudge(drafted, plan_active=False))
        nudge = gate.maybe_nudge(drafted, plan_active=True)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source, "plugin:plan-present-gate")
        self.assertEqual(nudge.text(), get_prompt("plan_present_gate"))
        self.assertIsNone(gate.maybe_nudge(drafted + [nudge, text_reply("# still")], plan_active=True))
        self.assertIsNone(
            TodoGate(enabled=True).maybe_nudge(
                drafted,
                plan_active=True,
            )
        )
        used = [
            AgentMessage(role="user", content="refactor the auth module"),
            tool_reply("exit_plan_mode", {"plan": "# Auth rewrite\n- do it"}, call_id="e1"),
            AgentMessage(role="toolResult", content="{}", tool_call_id="e1", tool_name="exit_plan_mode"),
            text_reply("waiting"),
        ]
        self.assertIsNone(PlanPresentGate(enabled=True).maybe_nudge(used, plan_active=True))


class TimeContextTests(unittest.TestCase):
    def test_opt_in_and_interval(self) -> None:
        log = SessionLog()
        log.append("turn/start", {"turn": 1})
        log.append("step/start", {"turn": 1, "step": 1})
        zone = "Asia/Shanghai"
        now = datetime(2026, 8, 13, 22, 0, tzinfo=ZoneInfo(zone))
        first = maybe_inject(log, now=now, interval_ms=60_000, time_zone=zone)
        self.assertIsNotNone(first)
        self.assertEqual(first.source, "plugin:time-context")
        self.assertIn("2026-08-13", first.text())

    def test_inject_when_forced(self) -> None:
        from witty_agent.runtime import clear_runtime_cache
        import os

        log = SessionLog()
        log.append("user/message", {"text": "hi", "source": "user"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.toml"
            path.write_text(
                "[time_context]\nenabled = true\ntime_zone = \"Asia/Shanghai\"\nrefresh_interval_ms = 60000\n",
                encoding="utf-8",
            )
            os.environ["WITTY_RUNTIME_FILE"] = str(path)
            clear_runtime_cache()
            try:
                now = datetime(2026, 8, 13, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
                first = maybe_inject(log, now=now)
                self.assertIsNotNone(first)
                self.assertEqual(first.source, "plugin:time-context")
                self.assertIn("第 1 步", first.text())
                log.append("user/message", {"text": first.text(), "source": first.source}, time_ms=int(now.timestamp() * 1000))
                again = maybe_inject(log, now=now + timedelta(seconds=10))
                self.assertIsNone(again)
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                clear_runtime_cache()


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def test_repeat_gentle_then_detailed(self) -> None:
        from witty_agent.prompts import get_prompt

        reminder = RepeatToolReminder(thresholds=[3, 5])
        args = {"path": "a.txt"}
        self.assertIsNone(reminder.observe("read", args))
        self.assertIsNone(reminder.observe("read", args))
        gentle = reminder.observe("read", args)
        self.assertIsNotNone(gentle)
        self.assertEqual(gentle.text(), get_prompt("repeat_gentle"))
        # 阈值**之间**必须闭嘴。丢掉这个返回值，「每轮都刷提醒」的退化就没人管了
        # （变异测试实测：`not in thresholds` 换成 `>= thresholds[0]` 原先一条都不炸）。
        self.assertIsNone(reminder.observe("read", args))
        detailed = reminder.observe("read", args)
        # 文案照配置比，不在测试里复述字面：`repeat_detailed` 的措辞属于配置。
        self.assertEqual(
            detailed.text(),
            get_prompt("repeat_detailed", tool_name="read", count="5", arguments='{"path":"a.txt"}'),
        )
        reminder.reset()
        self.assertIsNone(reminder.observe("read", args))

    def test_repeat_stops_at_last_threshold(self) -> None:
        reminder = RepeatToolReminder(thresholds=[2, 3])
        args = {"path": "a.txt"}
        self.assertIsNone(reminder.stop_notice())
        reminder.observe("read", args)
        self.assertIsNone(reminder.stop_notice())
        reminder.observe("read", args)
        self.assertIsNone(reminder.stop_notice())
        reminder.observe("read", args)
        notice = reminder.stop_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.source, "plugin:repeat-tool-stop")
        self.assertIn("3", notice.text())
        reminder.reset()
        self.assertIsNone(reminder.stop_notice())
        off = RepeatToolReminder(thresholds=[2], stop_at=0)
        off.observe("read", args)
        off.observe("read", args)
        self.assertIsNone(off.stop_notice())

    def test_progress_guard_stops_after_consecutive_errors(self) -> None:
        guard = ProgressGuard(stall_limit=3)
        fail = AgentMessage(
            role="toolResult", content="boom", tool_call_id="c1", tool_name="read", is_error=True
        )
        ok = AgentMessage(role="toolResult", content="ok", tool_call_id="c1", tool_name="read")
        assistant = AgentMessage(
            role="assistant",
            content=[ToolCallBlock(id="c1", name="read", arguments={"path": "a"})],
        )
        self.assertIsNone(guard.observe_turn(assistant, [fail]))
        self.assertIsNone(guard.observe_turn(assistant, [fail]))
        notice = guard.observe_turn(assistant, [fail])
        self.assertIsNotNone(notice)
        self.assertEqual(notice.source, "plugin:progress-guard")
        self.assertIn("3", notice.text())
        guard.reset()
        self.assertIsNone(guard.observe_turn(assistant, [fail]))
        self.assertIsNone(guard.observe_turn(assistant, [ok]))
        self.assertIsNone(guard.observe_turn(assistant, [fail]))
        self.assertIsNone(ProgressGuard(stall_limit=0).observe_turn(assistant, [fail]))

    def test_fail_strategy_once_skips_plan_block(self) -> None:
        rem = FailStrategyReminder(enabled=True)
        fail = AgentMessage(
            role="toolResult", content="nope", tool_name="read", is_error=True
        )
        first = rem.observe("read", fail)
        self.assertIsNotNone(first)
        self.assertEqual(first.source, "plugin:fail-strategy")
        self.assertEqual(first.text(), get_prompt("fail_strategy", tool_name="read"))
        self.assertIsNone(rem.observe("grep", fail))
        ok = AgentMessage(role="toolResult", content="ok", tool_name="read")
        self.assertIsNone(FailStrategyReminder(enabled=True).observe("read", ok))
        blocked = AgentMessage(
            role="toolResult",
            content=get_prompt("plan_block_mutating", tool_name="write"),
            tool_name="write",
            is_error=True,
        )
        self.assertIsNone(FailStrategyReminder(enabled=True).observe("write", blocked))
        denied = AgentMessage(
            role="toolResult",
            content=get_prompt("sandbox_denied_outside", path="../secret.txt"),
            tool_name="write",
            is_error=True,
        )
        sandbox_hint = FailStrategyReminder(enabled=True).observe("write", denied)
        self.assertIsNotNone(sandbox_hint)
        self.assertEqual(
            sandbox_hint.text(),
            get_prompt("fail_strategy_sandbox", tool_name="write"),
        )
        self.assertNotIn("换路径或换工具", sandbox_hint.text())
        self.assertIn("which", sandbox_hint.text())

    def test_answer_now_after_first_usable_lookup(self) -> None:
        rem = AnswerNowReminder(enabled=True)
        hit = AgentMessage(role="toolResult", content="1|hello", tool_name="read")
        first = rem.observe("read", hit, prompt="what does note.txt contain?")
        self.assertIsNotNone(first)
        self.assertEqual(first.source, "plugin:answer-now")
        self.assertEqual(first.text(), get_prompt("answer_now", tool_name="read"))
        self.assertIsNone(rem.observe("grep", hit, prompt="what does note.txt contain?"))
        miss = AgentMessage(
            role="toolResult", content="(no matches)", tool_name="grep", is_error=False
        )
        self.assertIsNone(
            AnswerNowReminder(enabled=True).observe(
                "grep", miss, prompt="what does note.txt contain?"
            )
        )
        self.assertIsNone(
            AnswerNowReminder(enabled=True).observe(
                "read", hit, prompt="what do a.py and b.py contain?"
            )
        )
        self.assertIsNone(
            AnswerNowReminder(enabled=True).observe(
                "read", hit, prompt="review the auth module and report risks"
            )
        )
        todo = AgentMessage(role="toolResult", content="ok", tool_name="todo_write")
        self.assertIsNone(
            AnswerNowReminder(enabled=True).observe(
                "todo_write", todo, prompt="what does note.txt contain?"
            )
        )

    def test_recalled_answer_hint_skips_file_and_empty(self) -> None:
        hits = [{"slug": "prefs", "text": "我喜欢简短回复", "scope": "user", "score": 7}]
        first = recalled_answer_hint("简短回复偏好是什么？", hits)
        self.assertIsNotNone(first)
        self.assertEqual(first.source, "plugin:recalled-answer")
        self.assertEqual(
            first.text(),
            get_prompt("recalled_answer", count="1", slugs="prefs"),
        )
        self.assertIsNone(recalled_answer_hint("简短回复偏好是什么？", hits, enabled=False))
        self.assertIsNone(recalled_answer_hint("简短回复偏好是什么？", []))
        weak = [{"slug": "prefs", "text": "我喜欢简短回复", "scope": "user", "score": 3}]
        self.assertIsNone(recalled_answer_hint("简短回复偏好是什么？", weak))
        self.assertIsNone(recalled_answer_hint("what does note.txt contain?", hits))
        self.assertIsNone(recalled_answer_hint("review the auth module and report risks", hits))
        self.assertIsNone(recalled_answer_hint("OAuth2 还是 JWT？", hits))
        self.assertIsNone(recalled_answer_hint("你好", hits))
        archived = [
            {
                "slug": "archive/prefs",
                "text": "喜欢吃桃子",
                "layer": "archive",
                "score": 6,
            }
        ]
        old = recalled_answer_hint("喜欢吃桃子的偏好是什么？", archived)
        self.assertIsNotNone(old)
        self.assertEqual(
            old.text(),
            get_prompt("recalled_answer_archive", count="1", slugs="archive/prefs"),
        )
        mixed = recalled_answer_hint(
            "简短回复偏好是什么？",
            [
                {"slug": "prefs", "text": "我喜欢简短回复", "score": 7},
                {
                    "slug": "archive/prefs",
                    "text": "喜欢吃桃子",
                    "layer": "archive",
                    "score": 6,
                },
            ],
        )
        self.assertIsNotNone(mixed)
        self.assertEqual(
            mixed.text(),
            get_prompt("recalled_answer_mixed", count="2", slugs="prefs, archive/prefs"),
        )
        self.assertNotIn("现在按这些笔记回答", mixed.text())
        self.assertIn("不要当当前偏好", mixed.text())
        scopes = recalled_answer_hint(
            "缩进偏好是什么？",
            [
                {
                    "slug": "decisions",
                    "text": "本目录用空格",
                    "scope": "workspace",
                    "score": 8,
                },
                {"slug": "prefs", "text": "我喜欢 tab", "scope": "user", "score": 6},
            ],
        )
        self.assertIsNotNone(scopes)
        self.assertEqual(
            scopes.text(),
            get_prompt("recalled_answer_scopes", count="2", slugs="decisions, prefs"),
        )
        self.assertIn("不要盖过用户偏好", scopes.text())

    def test_recalled_verify_hint_reads_excerpt_paths(self) -> None:
        self.assertEqual(excerpt_paths("2026-08-17 read note.txt: alpha-source-line"), ["note.txt"])
        self.assertEqual(excerpt_paths("OAuth2 采用决定"), [])
        weak = [
            {
                "slug": "note-txt",
                "text": "2026-08-17 read note.txt: alpha-source-line",
                "scope": "workspace",
                "score": 3,
            }
        ]
        first = recalled_verify_hint("alpha 是什么？", weak)
        self.assertIsNotNone(first)
        self.assertEqual(first.source, "plugin:recalled-verify")
        self.assertEqual(
            first.text(),
            get_prompt("recalled_verify", count="1", paths="note.txt"),
        )
        strong = [{"slug": "note-txt", "text": "read note.txt: alpha-source-line", "score": 7}]
        self.assertIsNone(recalled_verify_hint("alpha 是什么？", strong))
        self.assertIsNone(recalled_verify_hint("alpha 是什么？", [{"slug": "prefs", "text": "我喜欢简短回复", "score": 3}]))
        self.assertIsNone(recalled_verify_hint("what does note.txt contain?", weak))
        self.assertIsNone(recalled_verify_hint("alpha 是什么？", weak, enabled=False))
        self.assertIsNone(recalled_verify_hint("你好", weak))
        batch = [
            {
                "slug": "notes",
                "text": "2026-08-17 read note.txt: alpha and read other.py: leftover",
                "score": 3,
            }
        ]
        multi = recalled_verify_hint("alpha 是什么？", batch)
        self.assertIsNotNone(multi)
        self.assertEqual(
            multi.text(),
            get_prompt("recalled_verify_batch", count="2", paths="note.txt, other.py"),
        )
        self.assertIn("同一步", multi.text())
        self.assertEqual(recalled_verify_paths("alpha 是什么？", batch), ["note.txt", "other.py"])
        self.assertEqual(recalled_verify_paths("alpha 是什么？", weak), ["note.txt"])

    def test_autoload_recalled_verify_reads_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("alpha-source-line\n", encoding="utf-8")
            (workspace / "other.py").write_text("leftover-token\n", encoding="utf-8")
            bind_workspace(str(workspace))
            loaded = autoload_recalled_verify(["note.txt", "other.py"])
            self.assertEqual(len(loaded), 4)
            self.assertEqual(loaded[0].source, "plugin:recalled-verify-read")
            self.assertEqual([call.arguments.get("path") for call in loaded[0].tool_calls()], ["note.txt", "other.py"])
            self.assertIn("alpha-source-line", loaded[1].text())
            self.assertIn("leftover-token", loaded[2].text())
            self.assertFalse(loaded[1].is_error)
            self.assertFalse(loaded[2].is_error)
            self.assertEqual(loaded[3].source, "plugin:recalled-verify")
            self.assertEqual(
                loaded[3].text(),
                get_prompt("recalled_verify_loaded", count="2", paths="note.txt, other.py"),
            )
            items, _reason = collect_turn_evidence(loaded)
            locators = {item["locator"] for item in items if item.get("kind") == "tool"}
            self.assertEqual(locators, {"note.txt", "other.py"})
            single = autoload_recalled_verify(["note.txt"])
            self.assertEqual(len(single), 3)
            self.assertEqual(single[0].source, "plugin:recalled-verify-read")
            self.assertEqual([call.arguments.get("path") for call in single[0].tool_calls()], ["note.txt"])
            self.assertIn("alpha-source-line", single[1].text())
            self.assertEqual(
                single[2].text(),
                get_prompt("recalled_verify_loaded", count="1", paths="note.txt"),
            )
            self.assertEqual(autoload_recalled_verify([]), [])
            self.assertEqual(autoload_recalled_verify(["note.txt", "other.py"], enabled=False), [])
            missed = autoload_recalled_verify(["missing.txt"])
            self.assertEqual(len(missed), 4)
            self.assertTrue(missed[1].is_error)
            self.assertEqual(missed[2].tool_name, "find")
            self.assertEqual(
                missed[3].text(),
                get_prompt("recalled_verify_missed", count="1", paths="missing.txt"),
            )
            self.assertNotIn("Already read", missed[3].text())
            mixed = autoload_recalled_verify(["note.txt", "missing.txt"])
            self.assertFalse(mixed[1].is_error)
            self.assertTrue(mixed[2].is_error)
            self.assertEqual(mixed[3].tool_name, "find")
            self.assertEqual(
                mixed[4].text(),
                get_prompt(
                    "recalled_verify_partial",
                    ok_count="1",
                    ok_paths="note.txt",
                    bad_count="1",
                    bad_paths="missing.txt",
                ),
            )
            nest = workspace / "notes"
            nest.mkdir()
            extra = workspace / "extra"
            extra.mkdir()
            (nest / "ghost.txt").write_text("relocated-body\n", encoding="utf-8")
            (extra / "ghost.txt").write_text("other-ghost\n", encoding="utf-8")
            (nest / "solo.txt").write_text("unique-body\n", encoding="utf-8")
            located = autoload_recalled_verify(["ghost.txt"])
            finds = [item for item in located if item.tool_name == "find"]
            self.assertEqual(len(finds), 1)
            self.assertIn("ghost.txt", finds[0].text())
            self.assertEqual(
                located[-1].text(),
                get_prompt("recalled_verify_located", bad_paths="ghost.txt", patterns="ghost.txt"),
            )
            self.assertFalse(any("unique-body" in (item.text() or "") and item.tool_name == "read" for item in located))
            relocated = autoload_recalled_verify(["solo.txt"])
            reads = [item for item in relocated if item.tool_name == "read"]
            self.assertEqual(len(reads), 2)
            self.assertTrue(reads[0].is_error)
            self.assertFalse(reads[1].is_error)
            self.assertIn("unique-body", reads[1].text())
            self.assertEqual(
                relocated[-1].text(),
                get_prompt("recalled_verify_relocated", bad_paths="solo.txt", found="notes/solo.txt"),
            )
            self.assertEqual(recalled_relocations(relocated), [("solo.txt", "notes/solo.txt")])
            self.assertEqual(recalled_relocations(located), [])
            write_topic(
                workspace,
                "solo-txt",
                description="solo-txt",
                body="- 2026-08-17 read solo.txt: unique-body",
            )
            self.assertEqual(rewrite_relocated_paths(workspace, [("solo.txt", "notes/solo.txt")]), 1)
            self.assertIn("notes/solo.txt", topic_body(workspace, "solo-txt"))
            self.assertNotIn("read solo.txt:", topic_body(workspace, "solo-txt"))
            hits = [
                {"slug": "solo-txt", "text": "read solo.txt: unique-body", "score": 3},
                {"slug": "prefs", "text": "简短回复", "score": 4},
            ]
            self.assertEqual(apply_relocated_hits(hits, [("solo.txt", "notes/solo.txt")]), 1)
            self.assertEqual(hits[0]["text"], "read notes/solo.txt: unique-body")
            self.assertEqual(hits[0]["relocated"], [{"from": "solo.txt", "to": "notes/solo.txt"}])
            self.assertNotIn("relocated", hits[1])

    def test_autoload_browse_read_unique_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_topic(root, "prefs", description="个人偏好", body="- 我喜欢简短回复")
            prev = os.environ.get("WITTY_MEMORY_USER")
            os.environ["WITTY_MEMORY_USER"] = str(root)
            try:
                rows = relevant_browse_rows(
                    "个人偏好是什么？",
                    {
                        "reason": "no_overlap",
                        "populated": [{"id": "prefs", "title": "个人偏好", "count": 1, "scope": "user"}],
                    },
                )
                self.assertEqual(rows, [{"slug": "prefs", "scope": "user"}])
                loaded = autoload_browse_read(rows)
                self.assertEqual(len(loaded), 3)
                self.assertEqual(loaded[0].source, "plugin:browse-read")
                self.assertEqual(loaded[1].tool_name, "memory_read")
                self.assertFalse(loaded[1].is_error)
                self.assertIn("简短", loaded[1].text())
                self.assertEqual(
                    loaded[2].text(),
                    get_prompt("evidence_gate_loaded", slug="prefs", scope="user"),
                )
                hits = browse_read_hits(loaded)
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["slug"], "prefs")
                self.assertEqual(hits[0]["scope"], "user")
                self.assertTrue(hits[0]["loaded"])
                self.assertIn("简短", str(hits[0]["text"]))
                items, reason = collect_turn_evidence(
                    [
                        AgentMessage(role="user", content="个人偏好是什么？"),
                        *loaded,
                        text_reply("简短"),
                    ],
                    memory_empty={
                        "reason": "no_overlap",
                        "populated": [
                            {"id": "prefs", "title": "个人偏好", "count": 1, "scope": "user"},
                            {"id": "decisions", "title": "已做决定", "count": 1, "scope": "user"},
                        ],
                    },
                )
                self.assertEqual([item["kind"] for item in items], ["memory"])
                self.assertEqual(items[0]["locator"], "prefs")
                self.assertTrue(items[0].get("loaded"))
                self.assertEqual(reason, get_prompt("trace_reason_memory", slugs="prefs", count="1"))
                self.assertEqual(autoload_browse_read(rows, enabled=False), [])
                found = autoload_browse_read([{"slug": "missing-cell", "scope": "user"}])
                reads = [item for item in found if item.tool_name == "memory_read"]
                self.assertEqual(len(reads), 2)
                self.assertTrue(reads[0].is_error)
                self.assertFalse(reads[1].is_error)
                self.assertIn("简短", reads[1].text())
                self.assertEqual(
                    found[-1].text(),
                    get_prompt(
                        "evidence_gate_found",
                        bad_slugs="missing-cell (user)",
                        found="prefs (user)",
                    ),
                )
                write_topic(root, "decisions", description="已做决定", body="- 已决定采用 OAuth2")
                batch = autoload_browse_read(
                    [
                        {"slug": "prefs", "scope": "user"},
                        {"slug": "decisions", "scope": "user"},
                    ]
                )
                self.assertEqual(len(batch), 4)
                self.assertEqual(len(batch[0].tool_calls()), 2)
                self.assertIn("简短", batch[1].text())
                self.assertIn("OAuth2", batch[2].text())
                self.assertEqual(
                    batch[3].text(),
                    get_prompt(
                        "evidence_gate_loaded_batch",
                        count="2",
                        slugs="prefs (user), decisions (user)",
                    ),
                )
                missed = autoload_browse_read([{"slug": "missing-cell", "scope": "user"}])
                self.assertTrue(missed[1].is_error)
                statuses = [item for item in missed if item.tool_name == "memory_status"]
                self.assertEqual(len(statuses), 1)
                self.assertFalse(statuses[0].is_error)
                self.assertIn("prefs", statuses[0].text())
                self.assertEqual(
                    missed[-1].text(),
                    get_prompt(
                        "evidence_gate_status",
                        bad_slugs="missing-cell (user)",
                        scopes="user",
                    ),
                )
                overlap = autoload_browse_read(
                    [{"slug": "missing-cell", "scope": "user"}],
                    prompt="个人偏好是什么？",
                )
                overlap_reads = [item for item in overlap if item.tool_name == "memory_read"]
                self.assertEqual(len(overlap_reads), 2)
                self.assertTrue(overlap_reads[0].is_error)
                self.assertFalse(overlap_reads[1].is_error)
                self.assertIn("简短", overlap_reads[1].text())
                self.assertNotIn("OAuth2", overlap_reads[1].text())
                self.assertEqual(
                    overlap[-1].text(),
                    get_prompt(
                        "evidence_gate_found",
                        bad_slugs="missing-cell (user)",
                        found="prefs (user)",
                    ),
                )
                both = autoload_browse_read(
                    [{"slug": "missing-cell", "scope": "user"}],
                    prompt="个人偏好和已做决定是什么？",
                )
                both_reads = [item for item in both if item.tool_name == "memory_read"]
                self.assertEqual(len(both_reads), 3)
                self.assertTrue(both_reads[0].is_error)
                self.assertTrue(any("简短" in item.text() and not item.is_error for item in both_reads))
                self.assertTrue(any("OAuth2" in item.text() and not item.is_error for item in both_reads))
                self.assertEqual(
                    both[-1].text(),
                    get_prompt(
                        "evidence_gate_found_batch",
                        bad_slugs="missing-cell (user)",
                        count="2",
                        found="prefs (user), decisions (user)",
                    ),
                )
            finally:
                if prev is None:
                    os.environ.pop("WITTY_MEMORY_USER", None)
                else:
                    os.environ["WITTY_MEMORY_USER"] = prev

    async def test_session_injects_recalled_answer_instead_of_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 我喜欢简短回复",
            )
            result = await session.run(
                "简短回复偏好是什么？",
                stream_fn=ScriptedLLM([text_reply("简短")]),
                approval_mode="allow-all",
            )
            recalled = [item for item in result.messages if item.source == "plugin:recalled-answer"]
            self.assertEqual(len(recalled), 1)
            self.assertEqual(
                recalled[0].text(),
                get_prompt("recalled_answer", count="1", slugs="prefs"),
            )
            self.assertFalse(any(item.source == "plugin:dispatch-hint" for item in result.messages))
            self.assertTrue(any(item.type == "turn/recalled-answer" for item in session.log.events))

    async def test_session_injects_recalled_verify_instead_of_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("alpha-source-line\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.workspace_dir,
                "note-txt",
                description="note-txt",
                body="- 2026-08-17 read note.txt: alpha-source-line",
            )
            result = await session.run(
                "alpha 是什么？",
                stream_fn=ScriptedLLM([text_reply("alpha-source-line")]),
                approval_mode="allow-all",
            )
            reads = [item for item in result.messages if item.source == "plugin:recalled-verify-read"]
            self.assertEqual(len(reads), 1)
            self.assertEqual([call.arguments.get("path") for call in reads[0].tool_calls()], ["note.txt"])
            verify = [item for item in result.messages if item.source == "plugin:recalled-verify"]
            self.assertEqual(len(verify), 1)
            self.assertEqual(
                verify[0].text(),
                get_prompt("recalled_verify_loaded", count="1", paths="note.txt"),
            )
            bodies = [item.text() for item in result.messages if item.role == "toolResult" and item.tool_name == "read"]
            self.assertTrue(any("alpha-source-line" in text for text in bodies))
            self.assertFalse(any(item.source == "plugin:dispatch-hint" for item in result.messages))
            self.assertFalse(any(item.source == "plugin:recalled-answer" for item in result.messages))
            self.assertTrue(
                any(
                    item.type == "turn/recalled-verify"
                    and (item.data or {}).get("source") == "plugin:recalled-verify-read"
                    for item in session.log.events
                )
            )
            derived = derive_messages(session.log.events)
            self.assertTrue(
                any(item.role == "toolResult" and "alpha-source-line" in item.text() for item in derived)
            )
            failures = run_invariants(session.log, result.messages)
            self.assertFalse(any("not in derived history" in item for item in failures))
            self.assertTrue(
                any(
                    item.type == "tool_execution_end" and item.tool_name == "read"
                    for item in result.events
                )
            )

    async def test_session_autoloads_recalled_verify_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("alpha-source-line\n", encoding="utf-8")
            (workspace / "other.py").write_text("leftover-token\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.workspace_dir,
                "notes",
                description="notes",
                body="- 2026-08-17 read note.txt: alpha and read other.py: leftover",
            )
            result = await session.run(
                "alpha 是什么？",
                stream_fn=ScriptedLLM([text_reply("alpha-source-line")]),
                approval_mode="allow-all",
            )
            reads = [item for item in result.messages if item.source == "plugin:recalled-verify-read"]
            self.assertEqual(len(reads), 1)
            self.assertEqual(len(reads[0].tool_calls()), 2)
            notes = [item for item in result.messages if item.source == "plugin:recalled-verify"]
            self.assertEqual(len(notes), 1)
            self.assertEqual(
                notes[0].text(),
                get_prompt("recalled_verify_loaded", count="2", paths="note.txt, other.py"),
            )
            self.assertNotIn("same step", notes[0].text())
            bodies = [item.text() for item in result.messages if item.role == "toolResult" and item.tool_name == "read"]
            self.assertTrue(any("alpha-source-line" in text for text in bodies))
            self.assertTrue(any("leftover-token" in text for text in bodies))
            self.assertFalse(any(item.source == "plugin:dispatch-hint" for item in result.messages))
            self.assertTrue(
                any(
                    item.type == "turn/recalled-verify"
                    and (item.data or {}).get("source") == "plugin:recalled-verify-read"
                    for item in session.log.events
                )
            )
            derived = derive_messages(session.log.events)
            self.assertTrue(any(item.role == "toolResult" and "alpha-source-line" in item.text() for item in derived))
            self.assertTrue(any(item.role == "toolResult" and "leftover-token" in item.text() for item in derived))
            failures = run_invariants(session.log, result.messages)
            self.assertFalse(any("not in derived history" in item for item in failures))
            ends = [item for item in result.events if item.type == "tool_execution_end" and item.tool_name == "read"]
            self.assertEqual(len(ends), 2)

    async def test_session_rewrites_relocated_recalled_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "notes").mkdir()
            (workspace / "notes" / "solo.txt").write_text("unique-body\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.workspace_dir,
                "solo-txt",
                description="solo-txt",
                body="- 2026-08-17 read solo.txt: unique-body",
            )
            result = await session.run(
                "unique 是什么？",
                stream_fn=ScriptedLLM([text_reply("unique-body")]),
                approval_mode="allow-all",
            )
            note = [item for item in result.messages if item.source == "plugin:recalled-verify"]
            self.assertEqual(len(note), 1)
            self.assertEqual(
                note[0].text(),
                get_prompt("recalled_verify_relocated", bad_paths="solo.txt", found="notes/solo.txt"),
            )
            body = topic_body(memory.workspace_dir, "solo-txt")
            self.assertIn("notes/solo.txt", body)
            self.assertNotIn("read solo.txt:", body)
            self.assertTrue(
                any(
                    item.type == "turn/recalled-verify"
                    and (item.data or {}).get("relocated") == [{"from": "solo.txt", "to": "notes/solo.txt"}]
                    for item in session.log.events
                )
            )

    async def test_session_autoloads_unique_browse_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 我喜欢简短回复",
            )
            result = await session.run(
                "个人偏好是什么？",
                stream_fn=ScriptedLLM([text_reply("简短")]),
                approval_mode="allow-all",
            )
            reads = [item for item in result.messages if item.source == "plugin:browse-read"]
            self.assertTrue(any(item.role == "assistant" for item in reads))
            notes = [item for item in result.messages if item.source == "plugin:browse-read" and item.role == "user"]
            self.assertEqual(len(notes), 1)
            self.assertEqual(
                notes[0].text(),
                get_prompt("evidence_gate_loaded", slug="prefs", scope="user"),
            )
            bodies = [
                item.text()
                for item in result.messages
                if item.role == "toolResult" and item.tool_name == "memory_read"
            ]
            self.assertTrue(any("简短" in text for text in bodies))
            self.assertFalse(any(item.source == "plugin:dispatch-hint" for item in result.messages))
            self.assertTrue(
                any(
                    item.type == "turn/browse-read" and (item.data or {}).get("loaded") == ["prefs"]
                    for item in session.log.events
                )
            )
            final = [item for item in result.messages if item.role == "assistant" and item.text()][-1]
            self.assertTrue(
                any(row.get("kind") == "memory" and row.get("locator") == "prefs" for row in final.evidence)
            )
            self.assertFalse(any(row.get("kind") == "browse" for row in final.evidence))
            self.assertIn("prefs", final.trace_reason)

    async def test_session_autoloads_browse_slug_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 我喜欢简短回复",
            )
            write_topic(
                memory.user_dir,
                "decisions",
                description="已做决定",
                body="- 助手记录：采用令牌拆分",
            )
            result = await session.run(
                "个人偏好和已做决定是什么？",
                stream_fn=ScriptedLLM([text_reply("简短")]),
                approval_mode="allow-all",
            )
            reads = [item for item in result.messages if item.source == "plugin:browse-read" and item.role == "assistant"]
            self.assertEqual(len(reads), 1)
            self.assertEqual(len(reads[0].tool_calls()), 2)
            notes = [item for item in result.messages if item.source == "plugin:browse-read" and item.role == "user"]
            self.assertEqual(
                notes[0].text(),
                get_prompt(
                    "evidence_gate_loaded_batch",
                    count="2",
                    slugs="prefs (user), decisions (user)",
                ),
            )
            bodies = [
                item.text()
                for item in result.messages
                if item.role == "toolResult" and item.tool_name == "memory_read"
            ]
            self.assertTrue(any("简短" in text for text in bodies))
            self.assertTrue(any("令牌拆分" in text for text in bodies))

    def test_empty_lookup_not_evidence_and_hints_once(self) -> None:
        rem = FailStrategyReminder(enabled=True)
        miss = AgentMessage(
            role="toolResult", content="(no matches)", tool_name="grep", is_error=False
        )
        first = rem.observe("grep", miss)
        self.assertIsNotNone(first)
        self.assertEqual(first.source, "plugin:fail-strategy")
        self.assertEqual(first.text(), get_prompt("empty_lookup", tool_name="grep"))
        self.assertIsNone(rem.observe("find", miss))
        self.assertIsNone(
            FailStrategyReminder(enabled=True).observe(
                "read",
                AgentMessage(role="toolResult", content="1|hello", tool_name="read"),
            )
        )
        invented = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            tool_reply("grep", {"pattern": "secret", "path": "."}, call_id="g1"),
            AgentMessage(
                role="toolResult",
                content="(no matches)",
                tool_call_id="g1",
                tool_name="grep",
            ),
            text_reply("里面是 42"),
        ]
        nudge = EvidenceGate(enabled=True).maybe_nudge(invented)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source, "plugin:evidence-gate")
        todo_only = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            tool_reply(
                "todo_write",
                {"todos": [{"content": "read note", "status": "in_progress"}]},
                call_id="t1",
            ),
            AgentMessage(
                role="toolResult",
                content="todos updated",
                tool_call_id="t1",
                tool_name="todo_write",
            ),
            text_reply("里面是 42"),
        ]
        self.assertIsNotNone(EvidenceGate(enabled=True).maybe_nudge(todo_only))
        read_ok = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            tool_reply("read", {"path": "note.txt"}, call_id="r1"),
            AgentMessage(
                role="toolResult", content="1|42", tool_call_id="r1", tool_name="read"
            ),
            text_reply("42"),
        ]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(read_ok))

    async def test_session_injects_fail_strategy_after_bad_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "what does missing.txt contain?",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "missing.txt"}, call_id="r1"),
                        text_reply("not there"),
                    ]
                ),
                approval_mode="allow-all",
            )
            self.assertTrue(any(item.source == "plugin:fail-strategy" for item in result.messages))
            self.assertTrue(any(item.type == "turn/fail-strategy" for item in session.log.events))

    async def test_session_injects_answer_now_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("hello", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "what does note.txt contain?",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "note.txt"}, call_id="r1"),
                        text_reply("hello"),
                    ]
                ),
                approval_mode="allow-all",
            )
            hints = [item for item in result.messages if item.source == "plugin:answer-now"]
            self.assertEqual(len(hints), 1)
            self.assertEqual(hints[0].text(), get_prompt("answer_now", tool_name="read"))
            self.assertTrue(any(item.type == "turn/answer-now" for item in session.log.events))

    async def test_session_injects_empty_lookup_after_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("hello", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "what does note.txt contain?",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("grep", {"pattern": "zzz-no-such", "path": "."}, call_id="g1"),
                        text_reply("里面是 42"),
                    ]
                ),
                approval_mode="allow-all",
            )
            hints = [item for item in result.messages if item.source == "plugin:fail-strategy"]
            self.assertEqual(len(hints), 1)
            self.assertEqual(hints[0].text(), get_prompt("empty_lookup", tool_name="grep"))
            self.assertTrue(any(item.source == "plugin:evidence-gate" for item in result.messages))
            self.assertTrue(any(item.type == "turn/fail-strategy" for item in session.log.events))

    def test_needs_evidence_and_gate_once(self) -> None:
        self.assertTrue(needs_evidence("note.txt 里写了什么？"))
        self.assertTrue(needs_evidence("what does the file say"))
        self.assertFalse(needs_evidence("你好"))
        self.assertFalse(needs_evidence("/plan"))
        self.assertFalse(needs_evidence("写一个 hello.py"))
        self.assertFalse(needs_evidence("怎么改得更短"))
        self.assertFalse(needs_evidence("我喜欢简短不要每次问依据"))
        self.assertFalse(needs_evidence("什么是数字化审计"))
        self.assertFalse(needs_evidence("数字化审计是什么"))
        self.assertTrue(needs_evidence("简短回复偏好是什么"))
        concept = [AgentMessage(role="user", content="什么是数字化审计"), text_reply("用数据做审计")]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(concept))
        gate = EvidenceGate(enabled=True)
        invented = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            text_reply("里面是 42"),
        ]
        nudge = gate.maybe_nudge(invented)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source, "plugin:evidence-gate")
        self.assertIsNone(gate.maybe_nudge(invented + [nudge, text_reply("未核实")]))
        self.assertIsNone(
            EvidenceGate(enabled=True).maybe_nudge(invented, has_memory=True)
        )
        with_tool = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            tool_reply("read", {"path": "note.txt"}, call_id="r1"),
            AgentMessage(role="toolResult", content="42", tool_call_id="r1", tool_name="read"),
            text_reply("42"),
        ]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(with_tool))
        with_skill = [
            AgentMessage(role="user", content="幻灯片规范是什么？"),
            AgentMessage(
                role="user",
                content='<skill name="slides" location="skills/slides/SKILL.md">body</skill>',
                source="plugin:skill-invocation",
            ),
            text_reply("一页一个观点"),
        ]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(with_skill))
        hello = [AgentMessage(role="user", content="你好"), text_reply("hi")]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(hello))
        admitted = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            text_reply("未核实，我没有读这个文件"),
        ]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(admitted))

    def test_evidence_gate_points_at_populated_slugs(self) -> None:
        empty = {
            "reason": "no_overlap",
            "populated": [
                {"id": "prefs", "title": "个人偏好", "count": 1},
                {"id": "decisions", "title": "已做决定", "count": 1},
            ],
        }
        asked = [
            AgentMessage(role="user", content="简短回复偏好是什么？"),
            text_reply("应该喜欢长文"),
        ]
        nudge = EvidenceGate(enabled=True).maybe_nudge(asked, memory_empty=empty)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source, "plugin:evidence-gate")
        self.assertEqual(
            nudge.text(),
            get_prompt("evidence_gate_browse", slugs="prefs"),
        )
        self.assertIn("memory_read", nudge.text())
        self.assertNotIn("decisions", nudge.text())
        stray = [
            AgentMessage(role="user", content="量子纠缠超导是什么？"),
            text_reply("大概是超导"),
        ]
        stray_nudge = EvidenceGate(enabled=True).maybe_nudge(stray, memory_empty=empty)
        self.assertIsNone(stray_nudge)
        file_ask = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            text_reply("里面是 42"),
        ]
        file_nudge = EvidenceGate(enabled=True).maybe_nudge(file_ask, memory_empty=empty)
        self.assertEqual(file_nudge.text(), get_prompt("evidence_gate"))
        self.assertNotIn("prefs", file_nudge.text())
        generic = EvidenceGate(enabled=True).maybe_nudge(
            asked,
            memory_empty={"reason": "too_generic", "populated": empty["populated"]},
        )
        self.assertEqual(generic.text(), get_prompt("evidence_gate"))

    def test_evidence_gate_points_at_archive_slugs(self) -> None:
        empty = {
            "reason": "no_overlap",
            "populated": [{"id": "prefs", "count": 1}],
            "archive": [{"id": "archive/domain", "title": "归档·domain", "count": 2}],
        }
        asked = [
            AgentMessage(role="user", content="旧施工图在哪里？"),
            text_reply("在柜子里"),
        ]
        nudge = EvidenceGate(enabled=True).maybe_nudge(asked, memory_empty=empty)
        self.assertIsNotNone(nudge)
        self.assertIn("archive/domain", nudge.text())
        self.assertNotIn("prefs", nudge.text())
        self.assertEqual(
            nudge.text(),
            get_prompt("evidence_gate_browse", slugs="archive/domain"),
        )
        self.assertTrue(needs_memory_browse("旧施工图在哪里？", empty))
        self.assertFalse(needs_memory_browse("review the auth module", empty))
        self.assertFalse(needs_memory_browse("note.txt 里写了什么？", empty))
        self.assertFalse(needs_memory_browse("旧施工图在哪里？", {"reason": "too_generic"}))
        self.assertFalse(
            needs_memory_browse(
                "量子纠缠超导是什么？",
                {"reason": "no_overlap", "populated": [{"id": "prefs", "title": "个人偏好", "count": 1}]},
            )
        )
        self.assertFalse(
            needs_memory_browse(
                "旧施工图在哪里？",
                {"reason": "no_overlap", "populated": [{"id": "prefs", "count": 1}]},
            )
        )
        self.assertTrue(
            needs_memory_browse(
                "施工图在哪里？",
                {
                    "reason": "no_overlap",
                    "archive": [
                        {
                            "id": "archive/domain",
                            "title": "归档·domain",
                            "count": 2,
                            "kind": "archive",
                            "overlap": True,
                            "excerpt": "2025-01-01 旧施工图在柜里",
                        }
                    ],
                },
            )
        )
        self.assertFalse(
            needs_memory_browse(
                "施工图在哪里？",
                {
                    "reason": "no_overlap",
                    "archive": [{"id": "archive/domain", "title": "归档·domain", "count": 2, "kind": "archive"}],
                },
            )
        )

    def test_needs_choice_and_ask_gate_once(self) -> None:
        self.assertTrue(needs_choice("OAuth2 还是 JWT？"))
        self.assertTrue(needs_choice("dark or light?"))
        self.assertTrue(needs_choice("选一个方案：同步还是异步"))
        self.assertTrue(needs_choice("帮我写一份报告，用青绿模板还是简约风？"))
        self.assertTrue(needs_choice("用哪个模板？"))
        self.assertTrue(is_choice_only("OAuth2 还是 JWT？"))
        self.assertFalse(is_choice_only("帮我写一份报告，用青绿模板还是简约风？"))
        self.assertFalse(needs_choice("note.txt 里写了什么？"))
        self.assertFalse(needs_choice("你好"))
        self.assertFalse(needs_evidence("OAuth2 还是 JWT？"))
        self.assertTrue(poses_choice("用青绿模板还是简约风？"))
        self.assertTrue(poses_choice("请选择：\n1. 同步\n2. 异步"))
        self.assertFalse(poses_choice("建议用 OAuth2，不改协议。"))
        self.assertFalse(poses_choice("报告已经写好了。"))
        or_items = questions_from_assistant_text("用青绿模板还是简约风？")
        self.assertEqual(len(or_items), 1)
        self.assertEqual([opt.label for opt in or_items[0].options], ["青绿模板", "简约风"])
        listed = questions_from_assistant_text("请选择：\n1. 同步\n2. 异步")
        self.assertEqual([opt.label for opt in listed[0].options], ["同步", "异步"])
        guessed = [
            AgentMessage(role="user", content="OAuth2 还是 JWT？"),
            text_reply("用 OAuth2"),
        ]
        self.assertIsNone(EvidenceGate(enabled=True).maybe_nudge(guessed))
        gate = AskGate(enabled=True)
        nudge = gate.maybe_nudge(guessed)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source, "plugin:ask-gate")
        self.assertEqual(nudge.text(), get_prompt("ask_gate"))
        self.assertIsNone(gate.maybe_nudge(guessed + [nudge, text_reply("还是 OAuth2")]))
        self.assertIsNone(AskGate(enabled=True).maybe_nudge(guessed, has_memory=True))
        posed = [
            AgentMessage(role="user", content="帮我写一份季度报告"),
            tool_reply("read", {"path": "README.md"}, call_id="r1"),
            AgentMessage(
                role="toolResult",
                content="ok",
                tool_call_id="r1",
                tool_name="read",
            ),
            text_reply("用青绿模板还是简约风？"),
        ]
        posed_nudge = AskGate(enabled=True).maybe_nudge(posed)
        self.assertIsNotNone(posed_nudge)
        self.assertEqual(posed_nudge.text(), get_prompt("ask_gate_posed"))
        asked = [
            AgentMessage(role="user", content="OAuth2 还是 JWT？"),
            tool_reply(
                "ask_user_question",
                {"questions": [{"id": "auth", "question": "OAuth2 还是 JWT？"}]},
                call_id="a1",
            ),
            AgentMessage(
                role="toolResult",
                content='{"answers":[{"id":"auth","selected":["OAuth2"]}]}',
                tool_call_id="a1",
                tool_name="ask_user_question",
            ),
            text_reply("采用 OAuth2"),
        ]
        self.assertIsNone(AskGate(enabled=True).maybe_nudge(asked))

    def test_needs_todo_and_gate_once(self) -> None:
        self.assertTrue(needs_todo("review the auth module and report risks"))
        self.assertTrue(needs_todo("先读 README 再出摘要"))
        self.assertTrue(needs_todo("看一下整个认证模块并出风险"))
        self.assertTrue(needs_todo("1. 读配置\n2. 改超时\n3. 补测试"))
        self.assertFalse(needs_todo("你好"))
        self.assertFalse(needs_todo("read foo.py"))
        self.assertFalse(needs_todo("note.txt 里写了什么？"))
        self.assertFalse(needs_todo("OAuth2 还是 JWT？"))
        guessed = [
            AgentMessage(role="user", content="review the auth module and report risks"),
            text_reply("auth looks fine"),
        ]
        gate = TodoGate(enabled=True)
        nudge = gate.maybe_nudge(guessed)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source, "plugin:todo-gate")
        self.assertEqual(nudge.text(), get_prompt("todo_gate"))
        self.assertIsNone(gate.maybe_nudge(guessed + [nudge, text_reply("still fine")]))
        self.assertIsNone(TodoGate(enabled=True).maybe_nudge(guessed, has_todos=True))
        with_todo = [
            AgentMessage(role="user", content="review the auth module and report risks"),
            tool_reply("todo_write", {"todos": [{"content": "read auth", "status": "in_progress"}]}, call_id="t1"),
            AgentMessage(role="toolResult", content="updated", tool_call_id="t1", tool_name="todo_write"),
            text_reply("started"),
        ]
        self.assertIsNone(TodoGate(enabled=True).maybe_nudge(with_todo))
        hello = [AgentMessage(role="user", content="你好"), text_reply("hi")]
        self.assertIsNone(TodoGate(enabled=True).maybe_nudge(hello))

    def test_seal_after_second_invention(self) -> None:
        gate = EvidenceGate(enabled=True)
        invented = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            text_reply("里面是 42"),
        ]
        self.assertIsNone(gate.maybe_seal(invented))
        nudge = gate.maybe_nudge(invented)
        self.assertIsNotNone(nudge)
        again = invented + [nudge, text_reply("还是 42")]
        seal = gate.maybe_seal(again)
        self.assertIsNotNone(seal)
        self.assertEqual(seal.source, "plugin:evidence-seal")
        self.assertIn("未核实", seal.text())
        self.assertIsNone(gate.maybe_seal(again + [seal]))
        admitted = invented + [nudge, text_reply("未核实，我没有读这个文件")]
        other = EvidenceGate(enabled=True)
        other.maybe_nudge(invented)
        self.assertIsNone(other.maybe_seal(admitted))

    async def test_loop_nudges_then_continues(self) -> None:
        gate = EvidenceGate(enabled=True)

        async def follow_up():
            extra = gate.maybe_nudge(ctx.messages)
            return [extra] if extra is not None else []

        spec = ToolSpec(name="read", description="read", parameters={"type": "object"}, func=lambda path="": "ok")
        ctx = _context([spec])
        llm = ScriptedLLM(
            [
                text_reply("the file says 42"),
                text_reply("unverified, I did not read it"),
            ]
        )
        result = await run_agent_loop(
            [AgentMessage(role="user", content="what does note.txt contain?")],
            ctx,
            llm,
            LoopConfig(approval_mode="allow-all", get_follow_up_messages=follow_up),
        )
        sources = [item.source for item in result.messages]
        self.assertIn("plugin:evidence-gate", sources)
        self.assertEqual(result.messages[-1].text(), "unverified, I did not read it")

    async def test_session_seals_second_invention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "note.txt 里写了什么？",
                stream_fn=ScriptedLLM([text_reply("里面是 42"), text_reply("还是 42")]),
                approval_mode="allow-all",
            )
            seals = [item for item in result.messages if item.source == "plugin:evidence-seal"]
            self.assertEqual(len(seals), 1)
            self.assertIn("未核实", seals[0].text())
            from witty_agent.memory_harvest import last_assistant_text

            self.assertEqual(last_assistant_text(result.messages), "还是 42")
            derived = derive_messages(session.log.events)
            self.assertTrue(any(item.source == "plugin:evidence-seal" for item in derived))
            self.assertTrue(any("未核实" in item.text() for item in derived))

    async def test_session_nudges_choice_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "OAuth2 还是 JWT？",
                stream_fn=ScriptedLLM([text_reply("用 OAuth2"), text_reply("好的我来问你")]),
                approval_mode="allow-all",
            )
            sources = [item.source for item in result.messages]
            self.assertIn("plugin:ask-gate", sources)
            self.assertNotIn("plugin:evidence-gate", sources)

    async def test_session_nudges_posed_choice_after_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "README.md").write_text("ok\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "帮我写一份季度报告",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "README.md"}, call_id="r1"),
                        text_reply("用青绿模板还是简约风？"),
                        text_reply("好的我来问你"),
                    ]
                ),
                approval_mode="allow-all",
            )
            sources = [item.source for item in result.messages]
            self.assertIn("plugin:ask-gate", sources)
            asked = next(item for item in result.messages if item.source == "plugin:ask-gate")
            self.assertEqual(asked.text(), get_prompt("ask_gate_posed"))

    async def test_loop_stops_on_stalled_tool_errors(self) -> None:
        def boom() -> str:
            raise RuntimeError("nope")

        spec = ToolSpec(
            name="boom",
            description="fail",
            parameters={"type": "object"},
            func=boom,
        )
        guard = ProgressGuard(stall_limit=2)

        async def should_stop(assistant, new_messages):
            notice = guard.observe_turn(assistant, new_messages)
            if notice is None:
                return False
            new_messages.append(notice)
            return True

        llm = ScriptedLLM(
            [
                tool_reply("boom", {}, call_id="b1"),
                tool_reply("boom", {}, call_id="b2"),
                text_reply("should-not-run"),
            ]
        )
        result = await run_agent_loop(
            [AgentMessage(role="user", content="go")],
            _context([spec]),
            llm,
            LoopConfig(
                approval_mode="allow-all",
                retry_attempts=1,
                should_stop_after_turn=should_stop,
            ),
        )
        assistants = [item for item in result.messages if item.role == "assistant"]
        self.assertEqual(len(assistants), 3)
        self.assertEqual(assistants[-1].source, "plugin:progress-guard")
        self.assertTrue(any(item.role == "toolResult" and item.is_error for item in result.messages))

    async def test_loop_stops_on_repeated_same_tool(self) -> None:
        reminder = RepeatToolReminder(thresholds=[2], stop_at=2)

        def on_tool_result(call, _result):
            extra = reminder.observe(call.name, call.arguments)
            return [extra] if extra is not None else []

        async def should_stop(_assistant, new_messages):
            notice = reminder.stop_notice()
            if notice is None:
                return False
            new_messages.append(notice)
            return True

        spec = ToolSpec(
            name="read",
            description="read",
            parameters={"type": "object"},
            func=lambda path="": "ok",
        )
        llm = ScriptedLLM(
            [
                tool_reply("read", {"path": "a.txt"}, call_id="r1"),
                tool_reply("read", {"path": "a.txt"}, call_id="r2"),
                text_reply("should-not-run"),
            ]
        )
        result = await run_agent_loop(
            [AgentMessage(role="user", content="read it")],
            _context([spec]),
            llm,
            LoopConfig(
                approval_mode="allow-all",
                on_tool_result=on_tool_result,
                should_stop_after_turn=should_stop,
            ),
        )
        stops = [item for item in result.messages if item.source == "plugin:repeat-tool-stop"]
        self.assertEqual(len(stops), 1)
        self.assertFalse(any(item.text() == "should-not-run" for item in result.messages))

    async def test_tool_timeout(self) -> None:
        async def hang() -> str:
            await asyncio.sleep(1)
            return "late"

        spec = ToolSpec(name="hang", description="hang", parameters={"type": "object"}, func=hang)
        llm = ScriptedLLM(
            [
                tool_reply("hang", {}, call_id="h1"),
                text_reply("stopped"),
            ]
        )
        result = await run_agent_loop(
            [AgentMessage(role="user", content="go")],
            _context([spec]),
            llm,
            LoopConfig(approval_mode="allow-all", tool_timeout_ms=20, retry_attempts=1),
        )
        errors = [item for item in result.messages if item.role == "toolResult"]
        self.assertTrue(errors[0].is_error)
        self.assertIn("超时", errors[0].text())

    async def test_declared_timeout_beats_global_zero(self) -> None:
        def hang() -> str:
            time.sleep(1)
            return "late"

        spec = ToolSpec(
            name="hang",
            description="hang",
            parameters={"type": "object"},
            func=hang,
            timeout_ms=40,
        )
        llm = ScriptedLLM(
            [
                tool_reply("hang", {}, call_id="h2"),
                text_reply("stopped"),
            ]
        )
        result = await run_agent_loop(
            [AgentMessage(role="user", content="go")],
            _context([spec]),
            llm,
            LoopConfig(approval_mode="allow-all", tool_timeout_ms=0, retry_attempts=1),
        )
        errors = [item for item in result.messages if item.role == "toolResult"]
        self.assertTrue(errors[0].is_error)
        self.assertIn("超时", errors[0].text())
        from witty_agent.tools import list_tools

        fetch = next(item for item in list_tools() if item.name == "web_fetch")
        self.assertGreater(int(fetch.timeout_ms or 0), 0)

    async def test_bash_timeout_is_standard_error(self) -> None:
        from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
        from witty_agent.tools.fs import bash

        previous_home = os.environ.get("WITTY_HOME")
        previous_pkgs = os.environ.get("WITTY_SANDBOX_PACKAGES")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["WITTY_HOME"] = tmp
                os.environ["WITTY_SANDBOX_PACKAGES"] = ""
                bind_workspace(tmp)
                with self.assertRaises(TimeoutError) as ctx:
                    bash("sleep 2", timeout=1)
                self.assertIn("超时", str(ctx.exception))
                self.assertIn("1000", str(ctx.exception))
                spec = next(item for item in list_tools() if item.name == "bash")
                llm = ScriptedLLM(
                    [
                        tool_reply("bash", {"command": "sleep 2", "timeout": 1}, call_id="b-to"),
                        text_reply("stopped"),
                    ]
                )
                result = await run_agent_loop(
                    [AgentMessage(role="user", content="run")],
                    _context([spec]),
                    llm,
                    LoopConfig(approval_mode="allow-all", tool_timeout_ms=0, retry_attempts=1),
                )
                errors = [item for item in result.messages if item.role == "toolResult"]
                self.assertTrue(errors[0].is_error)
                self.assertIn("超时", errors[0].text())
                self.assertNotIn("timed out after", errors[0].text())
        finally:
            if previous_home is None:
                os.environ.pop("WITTY_HOME", None)
            else:
                os.environ["WITTY_HOME"] = previous_home
            if previous_pkgs is None:
                os.environ.pop("WITTY_SANDBOX_PACKAGES", None)
            else:
                os.environ["WITTY_SANDBOX_PACKAGES"] = previous_pkgs


class AskUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_provider(self) -> None:
        service = UserQuestionService()
        with self.assertRaises(UserQuestionError) as ctx:
            await service.ask([])
        self.assertEqual(ctx.exception.code, "EMPTY_QUESTIONS")
        with self.assertRaises(UserQuestionError) as ctx:
            from witty_agent.user_questions import AskUserQuestionItem

            await service.ask([AskUserQuestionItem(id="q", question="ok?")])
        self.assertEqual(ctx.exception.code, "NO_PROVIDER")


class CapabilityAndSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_registry(self) -> None:
        registry = CapabilityRegistry()
        registry.provide("planMode", object())
        self.assertTrue(registry.has("planMode"))
        self.assertIn("planMode", registry.names())

    async def test_session_persists_todo_and_plan_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="sess1")
            session.plan.set(session.log, True)
            llm = ScriptedLLM(
                [
                    tool_reply(
                        "todo_write",
                        {"todos": [{"content": "read spec", "status": "in_progress"}]},
                        call_id="t1",
                    ),
                    text_reply("listed"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            await session.run("work", stream_fn=llm, approve=allow, approval_mode="allow-all")
            events = load_session_events(session._store_path())
            types = [item.type for item in events]
            self.assertIn("turn/start", types)
            self.assertIn("plan/mode", types)
            self.assertIn("todo/write", types)
            self.assertIn("user/message", types)
            self.assertIn("assistant/message", types)
            self.assertTrue(fold_plan_mode(events))
            self.assertEqual(fold_todos(events)[0]["content"], "read spec")
            self.assertIn("planMode", session.capabilities.names())
            derived = derive_messages(events)
            self.assertGreaterEqual(len(derived), 2)

    async def test_todo_write_refreshes_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="todo-prompt")
            seen: list[str] = []

            class Probe(ScriptedLLM):
                async def __call__(self, context):  # type: ignore[no-untyped-def]
                    seen.append(context.system_prompt)
                    return await super().__call__(context)

            llm = Probe(
                [
                    tool_reply(
                        "todo_write",
                        {"todos": [{"content": "read spec", "status": "in_progress"}]},
                        call_id="t1",
                    ),
                    text_reply("started"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            await session.run("review the spec and report risks", stream_fn=llm, approve=allow, approval_mode="allow-all")
            self.assertGreaterEqual(len(seen), 2)
            self.assertNotIn("- [in_progress] read spec", seen[0])
            self.assertIn("- [in_progress] read spec", seen[1])
            self.assertIn("当前待办", seen[1])


if __name__ == "__main__":
    unittest.main()
