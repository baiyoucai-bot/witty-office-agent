from __future__ import annotations

import os
import sys
import tempfile
from witty_agent.tomlcompat import tomllib
import unittest
from pathlib import Path

from witty_agent import hooks
from unittest.mock import patch

from witty_agent.compaction import (
    COMPACTION_CHECKPOINT_SOURCE,
    CompactionSettings,
    compact_messages,
    compact_messages_async,
    should_compact,
)
from witty_agent.prompts import get_prompt
from witty_agent.goal import run_goal_loop, write_goal
from witty_agent.kernel import apply_kernel_update
from witty_agent.llm import ScriptedLLM, accumulate_stream, parse_sse_line, text_reply, tool_reply
from witty_agent.llm import _from_openai_choice, _openai_chat_body, _to_openai_messages
from witty_agent.loop import LoopConfig, run_agent_loop, should_parallelize
from witty_agent.mcp import McpClient, McpServerSpec, reset_mcp_cache
from witty_agent.retry import RetryableLLMError, retry_assistant_call, retry_call, should_retry_message
from witty_agent.runtime import clear_runtime_cache
from witty_agent.session import create_agent, create_session
from witty_agent.session_tree import list_session_ids, read_parent, rollback_session
from witty_agent.store import append_message, load_messages, write_header
from witty_agent.types import AgentContext, AgentMessage, ModelRef, ToolCallBlock


def _context(workspace: str, tools: list | None = None) -> AgentContext:
    return AgentContext(
        system_prompt="sys",
        messages=[],
        tools=tools or [],
        workspace_dir=workspace,
        model=ModelRef(provider="openai", model_id="test"),
        project_id="grid-base",
        agent_id="coder",
        session_id="s1",
    )


class OpenAIBodyTests(unittest.TestCase):
    def test_empty_tools_are_omitted(self) -> None:
        ctx = _context("/tmp")
        ctx.messages = [AgentMessage(role="user", content="你好")]
        body = _openai_chat_body(
            ctx,
            model_id="m",
            max_tokens=16,
            stream=True,
            think_level="off",
        )
        self.assertNotIn("tools", body)

    def test_blank_assistant_rows_are_dropped(self) -> None:
        ctx = _context("/tmp")
        ctx.messages = [
            AgentMessage(role="user", content="我是谁"),
            AgentMessage(role="assistant", content=""),
            AgentMessage(role="user", content="你好"),
        ]
        roles = [row["role"] for row in _to_openai_messages(ctx)]
        self.assertEqual(roles, ["system", "user", "user"])


class RetryStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_call_recovers(self) -> None:
        hits = {"n": 0}

        async def flaky() -> str:
            hits["n"] += 1
            if hits["n"] < 3:
                raise RetryableLLMError("503 unavailable")
            return "ok"

        self.assertEqual(await retry_call(flaky, attempts=3, base_delay=0), "ok")
        self.assertEqual(hits["n"], 3)

    async def test_auth_error_does_not_retry(self) -> None:
        hits = {"n": 0}

        async def boom() -> AgentMessage:
            hits["n"] += 1
            return AgentMessage(role="assistant", content="401 unauthorized", stop_reason="error")

        message = await retry_assistant_call(boom, attempts=3, base_delay=0)
        self.assertEqual(hits["n"], 1)
        self.assertFalse(should_retry_message(message))

    def test_retry_after_header_travels_in_error_text(self) -> None:
        """错误跨 AgentMessage 边界只剩字符串，Retry-After 不带进文本就丢了。"""
        import urllib.error

        from witty_agent.llm import _retry_after_note

        class _Headers(dict):
            pass

        def _error(value: str | None) -> urllib.error.HTTPError:
            headers = _Headers({"Retry-After": value} if value is not None else {})
            return urllib.error.HTTPError("http://x", 429, "too many", headers, None)

        self.assertEqual(_retry_after_note(_error("90")), " retry-after: 90s")
        self.assertEqual(_retry_after_note(_error(None)), "")
        # HTTP 日期形式解析不了，忽略而不是猜
        self.assertEqual(_retry_after_note(_error("Wed, 21 Oct 2026 07:28:00 GMT")), "")

    def test_sse_accumulator(self) -> None:
        self.assertIsNone(parse_sse_line("data: [DONE]"))
        chunks = [
            {"choices": [{"delta": {"content": "hel"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "content": "lo",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "ls", "arguments": "{"},
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        message = _from_openai_choice(accumulate_stream(chunks))
        self.assertEqual(message.text(), "hello")
        self.assertEqual(message.tool_calls()[0].name, "ls")
        self.assertEqual(message.stop_reason, "toolUse")
        names: list[str] = []
        lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1"}]}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"ask_user_question","arguments":"{"}}]}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]}\n',
            "data: [DONE]\n",
        ]

        class _Stream:
            def __init__(self) -> None:
                self._i = 0

            def readline(self) -> bytes:
                if self._i >= len(lines):
                    return b""
                item = lines[self._i]
                self._i += 1
                return item.encode("utf-8")

            def read(self) -> bytes:
                return b""

        from witty_agent.llm import _read_response

        payload = _read_response(
            _Stream(),
            stream=True,
            on_tool_delta=names.append,
        )
        self.assertEqual(names, ["", "ask_user_question"])
        self.assertEqual(payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "ask_user_question")

    def test_sse_reasoning_and_think_tags(self) -> None:
        from witty_agent.llm import _thinking_body, split_think_tags

        self.assertEqual(_thinking_body("off"), {"thinking": {"type": "disabled"}})
        self.assertEqual(_thinking_body("short"), {})
        self.assertEqual(_thinking_body("long")["reasoning_effort"], "high")
        tagged, visible = split_think_tags("<think>why</think>\nanswer")
        self.assertEqual(tagged, "why")
        self.assertEqual(visible, "answer")
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "step-"}}]},
            {"choices": [{"delta": {"reasoning_content": "one", "content": "<think>in"}}]},
            {"choices": [{"delta": {"content": "-tag</think>ok"}}]},
        ]
        message = _from_openai_choice(accumulate_stream(chunks))
        self.assertIn("step-one", message.reasoning)
        self.assertIn("in-tag", message.reasoning)
        self.assertEqual(message.text(), "ok")

    async def test_llm_retry_resets_stream(self) -> None:
        from witty_agent.llm import OpenAICompatLLM
        from witty_agent.retry import RetryableLLMError

        llm = OpenAICompatLLM(
            model_id="x",
            api_key="k",
            base_url="http://127.0.0.1",
            timeout=5,
            max_tokens=16,
            retry_attempts=2,
        )
        hits = {"n": 0, "resets": 0}
        llm.on_stream_reset = lambda: hits.__setitem__("resets", hits["resets"] + 1)

        def flaky(_context: AgentContext) -> AgentMessage:
            hits["n"] += 1
            if hits["n"] == 1:
                raise RetryableLLMError("503 unavailable")
            return text_reply("ok")

        llm._request = flaky  # type: ignore[method-assign]
        message = await llm(_context("/tmp"))
        self.assertEqual(message.text(), "ok")
        self.assertEqual(hits["n"], 2)
        self.assertEqual(hits["resets"], 1)


class ParallelAndSessionTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_tools(self) -> None:
        from witty_agent.tools import get_tool

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("alpha = 1\n", encoding="utf-8")
            os.environ["WITTY_WORKSPACE"] = tmp
            assistant = AgentMessage(
                role="assistant",
                content=[
                    ToolCallBlock(id="c1", name="ls", arguments={"path": ".", "limit": 20}),
                    ToolCallBlock(
                        id="c2",
                        name="grep",
                        arguments={"pattern": "alpha", "path": ".", "glob": "*.py"},
                    ),
                ],
                stop_reason="toolUse",
            )
            result = await run_agent_loop(
                [AgentMessage(role="user", content="look")],
                _context(tmp, tools=[get_tool("ls"), get_tool("grep")]),
                ScriptedLLM([assistant, text_reply("done")]),
                LoopConfig(approval_mode="allow-all", tool_execution="parallel", retry_attempts=1),
            )
            tools = [item for item in result.messages if item.role == "toolResult"]
            self.assertEqual(len(tools), 2)
            self.assertTrue(any("a.py" in item.text() for item in tools))
            self.assertTrue(any("alpha" in item.text() for item in tools))

    def test_should_parallelize_readonly_batch(self) -> None:
        reads = [
            ToolCallBlock(id="c1", name="read", arguments={"path": "a.py"}),
            ToolCallBlock(id="c2", name="grep", arguments={"pattern": "x", "path": "."}),
        ]
        self.assertTrue(should_parallelize(reads, mode="sequential"))
        self.assertTrue(should_parallelize(reads, mode="parallel"))
        self.assertFalse(should_parallelize(reads, mode="sequential", enabled=False))
        self.assertFalse(should_parallelize(reads[:1], mode="sequential"))
        mixed = [
            ToolCallBlock(id="c1", name="read", arguments={"path": "a.py"}),
            ToolCallBlock(id="c2", name="write", arguments={"path": "b.py", "content": "x"}),
        ]
        self.assertFalse(should_parallelize(mixed, mode="sequential"))
        self.assertTrue(should_parallelize(mixed, mode="parallel"))

    async def test_sequential_mode_batches_readonly_reads(self) -> None:
        from witty_agent.tools import get_tool

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("alpha = 1\n", encoding="utf-8")
            os.environ["WITTY_WORKSPACE"] = tmp
            assistant = AgentMessage(
                role="assistant",
                content=[
                    ToolCallBlock(id="c1", name="ls", arguments={"path": ".", "limit": 20}),
                    ToolCallBlock(
                        id="c2",
                        name="grep",
                        arguments={"pattern": "alpha", "path": ".", "glob": "*.py"},
                    ),
                ],
                stop_reason="toolUse",
            )
            result = await run_agent_loop(
                [AgentMessage(role="user", content="look")],
                _context(tmp, tools=[get_tool("ls"), get_tool("grep")]),
                ScriptedLLM([assistant, text_reply("done")]),
                LoopConfig(approval_mode="allow-all", tool_execution="sequential", retry_attempts=1),
            )
            tools = [item for item in result.messages if item.role == "toolResult"]
            self.assertEqual(len(tools), 2)
            self.assertTrue(any("a.py" in item.text() for item in tools))
            self.assertTrue(any("alpha" in item.text() for item in tools))

    async def test_fork_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="root1")
            await session.run("hi", stream_fn=ScriptedLLM([text_reply("hello")]))
            child = session.fork(session_id="child1")
            self.assertEqual(child.parent_id, "root1")
            self.assertEqual(read_parent(child._store_path()), "root1")
            await child.run("next", stream_fn=ScriptedLLM([text_reply("again")]))
            ids = list_session_ids(session._store_path().parent)
            self.assertEqual(set(ids), {"root1", "child1"})
            kept = rollback_session(session._store_path().parent, "child1", 2)
            self.assertEqual(len(kept), 2)
            self.assertEqual(len(load_messages(child._store_path())), 2)


class SubagentMcpGoalKernelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        hooks.reset()
        reset_mcp_cache()
        clear_runtime_cache()

    async def test_nested_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            os.environ["WITTY_HOME"] = tmp
            create_agent("grid-base", "worker", root=root)
            parent = create_agent("grid-base", "planner", root=root)
            session = create_session(parent, workspace_dir=workspace)
            llm = ScriptedLLM(
                [
                    tool_reply(
                        "run_subagent",
                        {"agent_id": "worker", "prompt": "say hi"},
                        call_id="sub1",
                    ),
                    text_reply("child-ok"),
                    text_reply("parent-ok"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            result = await session.run("delegate", stream_fn=llm, approve=allow)
            tools = [item for item in result.messages if item.role == "toolResult"]
            self.assertTrue(tools)
            self.assertIn("child-ok", tools[0].text())
            self.assertEqual(result.messages[-1].text(), "parent-ok")

    def test_mcp_stdio_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "server.py"
            script.write_text(
                "import json, sys\n"
                "while True:\n"
                "    line = sys.stdin.readline()\n"
                "    if not line:\n"
                "        break\n"
                "    req = json.loads(line)\n"
                "    if 'id' not in req:\n"
                "        continue\n"
                "    method = req.get('method')\n"
                "    mid = req['id']\n"
                "    if method == 'initialize':\n"
                "        result = {'protocolVersion': '2024-11-05', 'capabilities': {}, "
                "'serverInfo': {'name': 'fake', 'version': '0'}}\n"
                "    elif method == 'tools/list':\n"
                "        result = {'tools': [{'name': 'ping', 'description': 'ping', "
                "'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}]}\n"
                "    else:\n"
                "        result = {'content': [{'type': 'text', 'text': 'pong'}]}\n"
                "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': result}) + '\\n')\n"
                "    sys.stdout.flush()\n",
                encoding="utf-8",
            )
            client = McpClient(McpServerSpec(name="fake", command=sys.executable, args=[str(script)]))
            try:
                tools = client.connect()
                self.assertEqual(tools[0].name, "mcp__fake__ping")
                self.assertIn("pong", tools[0].func(q="hi"))
            finally:
                client.close()

    async def test_goal_loop_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)

            async def runner(prompt: str) -> None:
                self.assertIn("[goal]", prompt)
                self.assertIn("ship it", prompt)
                write_goal(scratch / "GOAL.yaml", "ship it", status="complete")

            state = await run_goal_loop(objective="ship it", scratch=scratch, runner=runner)
            self.assertEqual(state.status, "complete")
            self.assertEqual(state.round, 1)

    def test_kernel_keeps_user_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "system_config.toml"
            path.write_text(
                'name = "writer"\napproval_mode = "allow-all"\nversion = 3\n',
                encoding="utf-8",
            )
            result = apply_kernel_update(path)
            self.assertIn("approval_mode", result.kept)
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["approval_mode"], "allow-all")
            self.assertEqual(data["name"], "writer")
            self.assertEqual(data["version"], 3)
            self.assertEqual(data["kernel_version"], "2026-08-13")


class ModelCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_summary_and_fallback(self) -> None:
        messages = [AgentMessage(role="user", content="x" * 80) for _ in range(40)]
        settings = CompactionSettings(
            enabled=True, context_window=100, reserve_tokens=10, keep_recent_tokens=20
        )

        async def summarizer(context: AgentContext) -> AgentMessage:
            self.assertIn("摘要", context.system_prompt)
            self.assertIn("摘要", context.messages[0].text())
            return text_reply("MODEL SUMMARY")

        compacted = await compact_messages_async(messages, settings, stream_fn=summarizer)
        self.assertIn("MODEL SUMMARY", compacted[0].text())
        self.assertLess(len(compacted), len(messages))

        async def broken(context: AgentContext) -> AgentMessage:
            raise RuntimeError("model down")

        fallback = await compact_messages_async(messages, settings, stream_fn=broken)
        self.assertIn("[compaction]", fallback[0].text())
        self.assertNotIn("MODEL SUMMARY", fallback[0].text())

        wide = CompactionSettings(
            enabled=True, context_window=100000, reserve_tokens=10, keep_recent_tokens=20
        )
        self.assertFalse(should_compact(messages, wide))
        forced = await compact_messages_async(messages, wide, stream_fn=summarizer, force=True)
        self.assertIn("MODEL SUMMARY", forced[0].text())

    async def test_loop_compacts_between_tool_rounds(self) -> None:
        from witty_agent.tools.registry import ToolSpec

        settings = CompactionSettings(
            enabled=True,
            use_model=False,
            context_window=40,
            reserve_tokens=8,
            keep_recent_tokens=16,
        )

        def bulky(text: str = "Z" * 400) -> str:
            return text

        spec = ToolSpec(
            name="bulky",
            description="return a long string",
            parameters={"type": "object", "properties": {}},
            func=bulky,
        )
        seen: list[list[str]] = []

        async def stream_fn(context: AgentContext) -> AgentMessage:
            seen.append([item.role for item in context.messages])
            if len(seen) == 1:
                return tool_reply("bulky", {}, call_id="c1")
            return text_reply("done")

        async def transform(messages: list[AgentMessage]) -> list[AgentMessage]:
            return compact_messages(messages, settings)

        context = _context(".", tools=[spec])
        context.messages = [AgentMessage(role="user", content="seed " + ("x" * 120))]
        result = await run_agent_loop(
            [AgentMessage(role="user", content="now")],
            context,
            stream_fn,
            LoopConfig(
                approval_mode="allow-all",
                transform_context=transform,
                retry_attempts=1,
            ),
        )
        self.assertEqual(result.messages[-1].text(), "done")
        self.assertEqual(len(seen), 2)
        self.assertIn("[compaction]", context.messages[0].text())
        self.assertLess(len(context.messages), 6)

    async def test_session_compacts_mid_run_and_logs(self) -> None:
        tiny = {
            "enabled": True,
            "use_model": False,
            "context_window": 40,
            "reserve_tokens": 8,
            "keep_recent_tokens": 16,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "blob.txt").write_text("Z" * 400, encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="compact1")
            seen_second: list[bool] = []

            class Probe(ScriptedLLM):
                async def __call__(self, context: AgentContext) -> AgentMessage:
                    if not self._replies:
                        return text_reply("done")
                    if len(self._replies) == 1:
                        seen_second.append(
                            any("[compaction]" in item.text() for item in context.messages)
                        )
                    return await super().__call__(context)

            llm = Probe(
                [
                    tool_reply("read", {"path": "blob.txt"}, call_id="r1"),
                    text_reply("ok"),
                ]
            )
            with patch("witty_agent.session.compaction_settings", return_value=tiny):
                result = await session.run("look", stream_fn=llm, approval_mode="allow-all")
            self.assertEqual(result.messages[-1].text(), "ok")
            self.assertTrue(seen_second)
            self.assertTrue(seen_second[0])
            self.assertTrue(
                any(item.type == "compaction/result" for item in session.log.events)
            )
            stored = load_messages(session._store_path())
            self.assertEqual(stored[0].source, COMPACTION_CHECKPOINT_SOURCE)
            self.assertTrue(any(item.text() == "ok" for item in stored))

    async def test_slash_compact_writes_checkpoint_and_busy(self) -> None:
        tiny = {
            "enabled": True,
            "use_model": False,
            "context_window": 100000,
            "reserve_tokens": 10,
            "keep_recent_tokens": 20,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="compact-now")
            path = session._store_path()
            write_header(path, session.session_id, str(workspace))
            for index in range(24):
                append_message(path, AgentMessage(role="user", content=f"seed {index} " + ("x" * 80)))
                append_message(path, AgentMessage(role="assistant", content="ok"))
            before = load_messages(path)
            self.assertGreater(len(before), 16)
            with patch("witty_agent.session.compaction_settings", return_value=tiny):
                result = await session.run("/compact", stream_fn=ScriptedLLM([text_reply("should-not-run")]))
            self.assertIn("已压缩", result.messages[-1].text())
            self.assertNotIn("should-not-run", result.messages[-1].text())
            stored = load_messages(session._store_path())
            self.assertLess(len(stored), len(before) + 2)
            self.assertEqual(stored[0].source, COMPACTION_CHECKPOINT_SOURCE)
            self.assertTrue(
                any(
                    item.type == "compaction/result" and item.data.get("manual")
                    for item in session.log.events
                )
            )
            session._run_active = True
            with patch("witty_agent.session.compaction_settings", return_value=tiny):
                busy = session.compact_now()
            self.assertEqual(busy.kind, "error")
            self.assertEqual(busy.text, get_prompt("compaction_busy"))
            session._run_active = False
            session._compact_lock.acquire()
            with patch("witty_agent.session.compaction_settings", return_value=tiny):
                locked = session.compact_now()
            self.assertEqual(locked.kind, "error")
            session._compact_lock.release()

    async def test_slash_compact_uses_model_summary(self) -> None:
        tiny = {
            "enabled": True,
            "use_model": True,
            "context_window": 100000,
            "reserve_tokens": 10,
            "keep_recent_tokens": 20,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="compact-model")
            path = session._store_path()
            write_header(path, session.session_id, str(workspace))
            for index in range(24):
                append_message(path, AgentMessage(role="user", content=f"seed {index} " + ("x" * 80)))
                append_message(path, AgentMessage(role="assistant", content="ok"))
            with patch("witty_agent.session.compaction_settings", return_value=tiny):
                result = await session.run(
                    "/compact",
                    stream_fn=ScriptedLLM([text_reply("MODEL SUMMARY")]),
                )
            self.assertIn("已压缩", result.messages[-1].text())
            stored = load_messages(path)
            self.assertEqual(stored[0].source, COMPACTION_CHECKPOINT_SOURCE)
            self.assertIn("MODEL SUMMARY", stored[0].text())

    async def test_slash_compact_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="compact-region")
            path = session._store_path()
            write_header(path, session.session_id, str(workspace))
            append_message(path, AgentMessage(role="user", content="HEAD-KEEP"))
            for index in range(6):
                append_message(path, AgentMessage(role="user", content=f"mid {index} " + ("y" * 40)))
                append_message(path, AgentMessage(role="assistant", content="ok"))
            append_message(path, AgentMessage(role="user", content="TAIL-KEEP"))
            before = load_messages(path)
            self.assertGreater(len(before), 8)
            result = await session.run("/compact 1-12", stream_fn=ScriptedLLM([text_reply("unused")]))
            self.assertIn("第 1–12", result.messages[-1].text())
            stored = load_messages(path)
            self.assertEqual(stored[0].text(), "HEAD-KEEP")
            self.assertEqual(stored[1].source, COMPACTION_CHECKPOINT_SOURCE)
            self.assertTrue(any(item.text() == "TAIL-KEEP" for item in stored))
            self.assertLess(len(stored), len(before))
            bad = session.compact_now("nope")
            self.assertEqual(bad.kind, "error")
            self.assertIn("/compact", bad.text)

    async def test_auto_compact_writes_checkpoint(self) -> None:
        tiny = {
            "enabled": True,
            "use_model": False,
            "context_window": 80,
            "reserve_tokens": 10,
            "keep_recent_tokens": 20,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="auto-ckpt")
            path = session._store_path()
            write_header(path, session.session_id, str(workspace))
            for index in range(24):
                append_message(path, AgentMessage(role="user", content=f"seed {index} " + ("x" * 80)))
                append_message(path, AgentMessage(role="assistant", content="ok"))
            before = load_messages(path)
            self.assertGreater(len(before), 16)
            with patch("witty_agent.session.compaction_settings", return_value=tiny):
                result = await session.run("continue", stream_fn=ScriptedLLM([text_reply("next")]))
            self.assertEqual(result.messages[-1].text(), "next")
            stored = load_messages(path)
            self.assertLess(len(stored), len(before))
            self.assertEqual(stored[0].source, COMPACTION_CHECKPOINT_SOURCE)
            self.assertTrue(any(item.text() == "next" for item in stored))
            self.assertTrue(
                any(
                    item.type == "compaction/result" and not item.data.get("manual")
                    for item in session.log.events
                )
            )
            resumed = create_session(agent, workspace_dir=workspace, session_id="auto-ckpt")
            again = load_messages(resumed._store_path())
            self.assertEqual(again[0].source, COMPACTION_CHECKPOINT_SOURCE)
            self.assertLess(len(again), len(before))


if __name__ == "__main__":
    unittest.main()
