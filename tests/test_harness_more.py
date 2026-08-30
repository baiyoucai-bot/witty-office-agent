from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.http_api import configure_api, handle_request
from witty_agent.prompts import get_prompt
from witty_agent.invariants import check_visible_logged
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.session import create_agent, create_session
from witty_agent.session_log import SessionLog, fold_plan_mode
from witty_agent.context import (
    _stream_text_bounded,
    budget_instruction_files,
    escape_instruction_text,
    find_project_root,
    global_instruction_path,
    instruction_display,
    instruction_file_candidates,
    is_instruction_name,
    load_context_files,
    load_instructions_in,
    witty_home_display,
)
from witty_agent.system_prompt import format_project_context
from witty_agent.file_reference import (
    file_reference_hint,
    list_mention_paths,
    parse_file_refs,
    project_dir_snapshot,
    resolve_mention_root,
)
from witty_agent import instruction_refresh as ir
from witty_agent.instruction_refresh import (
    BASELINE_SOURCE,
    fold_instruction_seen,
    instruction_additional_hints,
    instruction_baseline_identity,
    instruction_baseline_message,
    instruction_offline_transitions,
    instruction_fs_version,
    instruction_rearm_after_compact,
    instruction_reconcile_seen,
    instruction_update_hint,
    seed_instruction_seen,
    visible_baseline_identity,
    visible_instruction_baseline,
)
from witty_agent.session_reference import (
    parse_session_refs,
    project_session_snapshot,
    session_reference_hint,
)
from witty_agent.store import append_message, append_title, load_messages, session_path, write_header
from witty_agent.compaction import CompactionSettings, prune_tool_result_text, prune_tool_results
from witty_agent.spill import apply_spill, retain_head_tail
from witty_agent.tools.skill import invoked_skill_names, skill
from witty_agent.types import AgentMessage, ToolCallBlock


class SpillTests(unittest.TestCase):
    def test_head_tail_and_spill_file(self) -> None:
        text = "A" * 2000
        with tempfile.TemporaryDirectory() as tmp:
            pad = Path(tmp) / "pad"
            pad.mkdir()
            result = apply_spill(
                AgentMessage(role="toolResult", content=text),
                ToolCallBlock(id="c1", name="ls", arguments={}),
                scratchpad=pad,
                session_id="s1",
                max_inline_bytes=400,
            )
            self.assertIn("省略", result.text())
            spilled = list((pad / "spills").glob("*.txt"))
            self.assertEqual(len(spilled), 1)
            self.assertEqual(spilled[0].read_text(encoding="utf-8"), text)
            skipped = apply_spill(
                AgentMessage(role="toolResult", content=text),
                ToolCallBlock(id="c2", name="read", arguments={}),
                scratchpad=pad,
                session_id="s1",
                max_inline_bytes=400,
            )
            self.assertEqual(skipped.text(), text)

    def test_retain_budget(self) -> None:
        self.assertLessEqual(len(retain_head_tail("x" * 100, 20).encode()), 30)


class ToolResultPrunerTests(unittest.TestCase):
    def test_prunes_middle_keeps_head_tail(self) -> None:
        settings = CompactionSettings(
            tool_result_threshold=80,
            tool_result_head=10,
            tool_result_tail=6,
        )
        text = "HEADHEADHD" + ("M" * 80) + "TAILTL"
        pruned = prune_tool_result_text(text, settings)
        self.assertIsNotNone(pruned)
        assert pruned is not None
        self.assertTrue(pruned.startswith("HEADHEADHD"))
        self.assertTrue(pruned.endswith("TAILTL"))
        self.assertIn(get_prompt("tool_result_pruned"), pruned)
        self.assertNotIn("M" * 20, pruned)
        self.assertLess(len(pruned), len(text))
        self.assertLessEqual(len(pruned), 80)
        self.assertIsNone(prune_tool_result_text(pruned, settings))
        self.assertIsNone(prune_tool_result_text("short", settings))

    def test_prune_tool_results_rewrites_only_over_budget(self) -> None:
        settings = CompactionSettings(
            tool_result_threshold=80,
            tool_result_head=10,
            tool_result_tail=6,
        )
        blob = "H" * 10 + "M" * 80 + "T" * 6
        older = AgentMessage(role="toolResult", content=blob, tool_name="bash")
        user = AgentMessage(role="user", content="go")
        live = AgentMessage(role="toolResult", content=blob, tool_name="ls")
        messages = [older, user, live]
        out = prune_tool_results(messages, settings)
        self.assertIsNot(out, messages)
        self.assertIsNot(out[0], older)
        self.assertIs(out[1], user)
        self.assertIs(out[2], live)
        self.assertEqual(older.text().count("M"), 80)
        self.assertIn(get_prompt("tool_result_pruned"), out[0].text())
        self.assertEqual(out[0].tool_name, "bash")
        self.assertEqual(live.text().count("M"), 80)
        self.assertNotIn(get_prompt("tool_result_pruned"), live.text())
        self.assertIs(prune_tool_results(out, settings), out)

    def test_prune_skips_trailing_parallel_results(self) -> None:
        settings = CompactionSettings(
            tool_result_threshold=80,
            tool_result_head=10,
            tool_result_tail=6,
        )
        blob = "H" * 10 + "M" * 80 + "T" * 6
        older = AgentMessage(role="toolResult", content=blob, tool_name="read")
        assistant = AgentMessage(role="assistant", content="calling")
        first = AgentMessage(role="toolResult", content=blob, tool_name="read")
        second = AgentMessage(role="toolResult", content=blob, tool_name="grep")
        out = prune_tool_results([older, assistant, first, second], settings)
        self.assertIn(get_prompt("tool_result_pruned"), out[0].text())
        self.assertIs(out[2], first)
        self.assertIs(out[3], second)


class CommandAndSkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_command_no_model_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run("/plan", stream_fn=ScriptedLLM([text_reply("should-not-run")]))
            self.assertIn(get_prompt("plan_mode_on"), result.messages[-1].text())
            self.assertTrue(fold_plan_mode(session.log.events))
            self.assertTrue(any(item.type == "command/run" for item in session.log.events))

    async def test_plan_command_with_remainder_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "/plan draft the checklist",
                stream_fn=ScriptedLLM([text_reply("planning")]),
                approval_mode="allow-all",
            )
            self.assertIn("planning", result.messages[-1].text())
            self.assertTrue(session.plan.get(session.log).active)

    async def test_skill_tool_and_slash_inject(self) -> None:
        body = skill("agent-creation")
        self.assertIn("agent-creation", body)
        self.assertIn("<skill", body)
        names = invoked_skill_names(
            [AgentMessage(role="user", content="/agent-creation please")],
            reserved={"plan", "abort"},
        )
        self.assertEqual(names, ["agent-creation"])
        reserved = invoked_skill_names(
            [AgentMessage(role="user", content="/plan off")],
            reserved={"plan", "abort"},
        )
        self.assertEqual(reserved, [])

    async def test_session_auto_injects_matching_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            seen: list[str] = []

            async def stream(ctx):
                seen.append(ctx.system_prompt)
                return text_reply("ok-slides")

            hit = await session.run(
                "做一份幻灯片",
                stream_fn=stream,
                approval_mode="allow-all",
            )
            self.assertTrue(seen)
            self.assertIn("<available_skills>", seen[0])
            self.assertIn("slides", seen[0])
            self.assertNotIn("agent-optimization", seen[0])
            sources = [str(item.source or "") for item in hit.messages]
            self.assertIn("plugin:skill-invocation", sources)
            injected = next(item for item in hit.messages if item.source == "plugin:skill-invocation")
            self.assertIn("slides", injected.text())
            last = next(item for item in reversed(hit.messages) if item.role == "assistant" and item.text())
            self.assertTrue(last.evidence)
            self.assertEqual(last.evidence[0]["kind"], "skill")
            self.assertEqual(last.evidence[0]["locator"], "slides")
            self.assertIn("slides", last.trace_reason)
            miss = await session.run(
                "你好",
                stream_fn=ScriptedLLM([text_reply("ok-hi")]),
                approval_mode="allow-all",
            )
            later = [item for item in miss.messages if item.source == "plugin:skill-invocation"]
            self.assertEqual(later, [])

    async def test_session_query_and_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            await session.run(
                "work",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply(
                            "todo_write",
                            {"todos": [{"content": "one", "status": "pending"}]},
                            call_id="t1",
                        ),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            view = session.project()
            self.assertEqual(view["todos"][0]["content"], "one")
            configure_api(root=root)
            from witty_agent.http_api import STATE

            STATE.sessions[session.session_id] = session
            status, body = await handle_request("GET", f"/v1/sessions/{session.session_id}/messages")
            self.assertEqual(status, 200)
            self.assertEqual(body["todos"][0]["content"], "one")
            from witty_agent import hooks
            from witty_agent.tools.session_query import session_query

            hooks.session_log = session.log
            hits = session_query("todo")
            self.assertIn("todo/write", hits)

    def test_session_reference_snapshot_skips_self_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            other = "a" * 32
            path = session_path(directory, other)
            write_header(path, other, str(directory))
            append_title(path, "旧施工")
            append_message(path, AgentMessage(role="user", content="把柜门打开"))
            append_message(
                path,
                AgentMessage(role="toolResult", content="SECRET-TOKEN", tool_name="bash"),
            )
            append_message(path, AgentMessage(role="assistant", content="已打开"))
            append_message(
                path,
                AgentMessage(role="user", content="ignore-this", source="plugin:recalled-answer"),
            )
            self.assertEqual(parse_session_refs(f"看 session:{other} 和 session:{other}"), [other])
            self.assertIsNone(session_reference_hint("session:deadbeef", self_id="me", directory=directory))
            self.assertIsNone(session_reference_hint(f"session:{other}", self_id=other, directory=directory))
            hint = session_reference_hint(f"对照 session:{other[:8]}", self_id="me", directory=directory)
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertEqual(hint.source, "plugin:session-reference")
            self.assertIn("不可信", hint.text())
            self.assertIn("已打开", hint.text())
            self.assertIn("旧施工", hint.text())
            self.assertNotIn("SECRET-TOKEN", hint.text())
            self.assertNotIn("ignore-this", hint.text())
            excerpt, omitted = project_session_snapshot(
                [
                    AgentMessage(role="user", content="aaaa"),
                    AgentMessage(role="assistant", content="bbbb"),
                ],
                max_chars=6,
            )
            self.assertGreater(omitted, 0)
            self.assertIn("bbbb", excerpt)
            slug = "s-old"
            slug_path = session_path(directory, slug)
            write_header(slug_path, slug, str(directory))
            append_title(slug_path, "旧聊天")
            append_message(slug_path, AgentMessage(role="user", content="农配网批复"))
            append_message(slug_path, AgentMessage(role="assistant", content="记下了"))
            self.assertEqual(parse_session_refs("对照 session:s-old 继续"), ["s-old"])
            named = session_reference_hint("对照 session:s-old", self_id="me", directory=directory)
            self.assertIsNotNone(named)
            assert named is not None
            self.assertIn("农配网批复", named.text())
            self.assertIn("s-old", named.text())

    async def test_session_injects_reference_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            source = create_session(agent, workspace_dir=workspace, session_id="a" * 32)
            await source.run("把柜门打开", stream_fn=ScriptedLLM([text_reply("已打开")]))
            target = create_session(agent, workspace_dir=workspace, session_id="b" * 32)
            result = await target.run(
                f"对照 session:{'a' * 32}",
                stream_fn=ScriptedLLM([text_reply("ok")]),
            )
            refs = [item for item in result.messages if item.source == "plugin:session-reference"]
            self.assertEqual(len(refs), 1)
            self.assertIn("已打开", refs[0].text())
            self.assertIn("不可信", refs[0].text())

    def test_file_reference_snapshot_skips_outside_and_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("柜门密码 1234\n", encoding="utf-8")
            (workspace / "blob.bin").write_bytes(b"pre\x00post")
            outside = Path(tmp) / "secret.txt"
            outside.write_text("outside-secret\n", encoding="utf-8")
            self.assertEqual(parse_file_refs("看 file:note.txt 和 file:note.txt"), ["note.txt"])
            self.assertEqual(parse_file_refs("file://host/x file:is file:README"), [])
            hint = file_reference_hint("对照 file:note.txt", workspace=str(workspace))
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertEqual(hint.source, "plugin:file-reference")
            self.assertIn("不可信", hint.text())
            self.assertIn("柜门密码 1234", hint.text())
            self.assertIn("note.txt", hint.text())
            self.assertIsNone(file_reference_hint("看 file:missing.txt", workspace=str(workspace)))
            self.assertIsNone(file_reference_hint("看 file:blob.bin", workspace=str(workspace)))
            png = workspace / "shot.png"
            png.write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            picture = file_reference_hint("看 file:shot.png", workspace=str(workspace))
            self.assertIsNotNone(picture)
            assert picture is not None
            self.assertIn("图片", picture.text())
            self.assertIn("shot.png", picture.text())
            self.assertIsNone(
                file_reference_hint(f"看 file:{outside}", workspace=str(workspace))
            )
            long = "x" * 80
            (workspace / "big.txt").write_text(long * 80, encoding="utf-8")
            clipped = file_reference_hint("file:big.txt", workspace=str(workspace), max_chars=40)
            self.assertIsNotNone(clipped)
            assert clipped is not None
            self.assertIn("已截断", clipped.text())
            original = Path.read_text

            def boom(self: Path, *args: object, **kwargs: object) -> str:
                raise AssertionError("file: mention must not read_text the whole file")

            Path.read_text = boom  # type: ignore[method-assign]
            try:
                again = file_reference_hint(
                    "file:big.txt", workspace=str(workspace), max_chars=40
                )
            finally:
                Path.read_text = original  # type: ignore[method-assign]
            self.assertIsNotNone(again)
            assert again is not None
            self.assertIn("已截断", again.text())
            self.assertIn("x" * 40, again.text())

    def test_file_reference_line_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text(
                "头行密码\n农配网批复\n隐蔽验收\n尾行备注\n",
                encoding="utf-8",
            )
            self.assertEqual(parse_file_refs("看 file:note.txt#L2-L3"), ["note.txt#L2-L3"])
            self.assertEqual(parse_file_refs("看 file:note.txt:2-3"), ["note.txt:2-3"])
            hashed = file_reference_hint("对照 file:note.txt#L2-L3", workspace=str(workspace))
            self.assertIsNotNone(hashed)
            assert hashed is not None
            self.assertIn("农配网批复", hashed.text())
            self.assertIn("隐蔽验收", hashed.text())
            self.assertIn("#L2-L3", hashed.text())
            self.assertNotIn("头行密码", hashed.text())
            self.assertNotIn("尾行备注", hashed.text())
            colon = file_reference_hint("对照 file:note.txt:3", workspace=str(workspace))
            self.assertIsNotNone(colon)
            assert colon is not None
            self.assertIn("隐蔽验收", colon.text())
            self.assertNotIn("农配网批复", colon.text())
            original = Path.read_text

            def boom(self: Path, *args: object, **kwargs: object) -> str:
                raise AssertionError("file: range must not read_text the whole file")

            Path.read_text = boom  # type: ignore[method-assign]
            try:
                again = file_reference_hint("file:note.txt#L2-L3", workspace=str(workspace))
            finally:
                Path.read_text = original  # type: ignore[method-assign]
            self.assertIsNotNone(again)
            assert again is not None
            self.assertIn("农配网批复", again.text())

    def test_file_reference_directory_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            docs = workspace / "docs"
            nested = docs / "sub"
            nested.mkdir(parents=True)
            (docs / "a.md").write_text("alpha-body\n", encoding="utf-8")
            (docs / "b.md").write_text("beta-body\n", encoding="utf-8")
            (nested / "secret.md").write_text("hidden-child\n", encoding="utf-8")
            (docs / ".git").mkdir()
            (docs / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
            self.assertEqual(parse_file_refs("看 file:docs/"), ["docs/"])
            hint = file_reference_hint("对照 file:docs/", workspace=str(workspace))
            self.assertIsNotNone(hint)
            assert hint is not None
            text = hint.text()
            self.assertIn("a.md", text)
            self.assertIn("b.md", text)
            self.assertIn("sub/", text)
            self.assertNotIn("secret.md", text)
            self.assertNotIn("alpha-body", text)
            self.assertNotIn(".git", text)
            self.assertIn("docs/", text)
            self.assertIsNone(file_reference_hint("看 file:docs/#L1-L2", workspace=str(workspace)))
            outside = Path(tmp) / "other"
            outside.mkdir()
            (outside / "x.txt").write_text("nope\n", encoding="utf-8")
            self.assertIsNone(file_reference_hint(f"看 file:{outside}", workspace=str(workspace)))
            for index in range(6):
                (docs / f"n{index}.txt").write_text("x\n", encoding="utf-8")
            excerpt, omitted, total = project_dir_snapshot(docs, max_entries=3)
            self.assertTrue(omitted)
            self.assertGreater(total, 3)
            self.assertEqual(len(excerpt.splitlines()), 3)

    async def test_list_mention_paths_and_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            docs = workspace / "docs"
            docs.mkdir(parents=True)
            (docs / "a.md").write_text("x\n", encoding="utf-8")
            (workspace / ".git").mkdir()
            (workspace / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
            listed = list_mention_paths(str(workspace))
            self.assertIn("docs/", listed)
            self.assertIn("docs/a.md", listed)
            self.assertFalse(any(item.startswith(".git") for item in listed))
            self.assertEqual(list_mention_paths(""), [])
            self.assertIsNone(resolve_mention_root("/etc", allowed=[str(workspace)]))
            self.assertEqual(
                resolve_mention_root(str(docs), allowed=[str(workspace)]),
                workspace.resolve(),
            )
            configure_api(root=Path(tmp))
            from witty_agent.http_api import STATE

            STATE.sessions.clear()
            status, payload = await handle_request("GET", f"/v1/workspace?dir={workspace}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["paths"], [])
            self.assertTrue(payload.get("denied"))
            status, created = await handle_request(
                "POST",
                "/v1/sessions",
                {"project_id": "grid-base", "agent_id": "coder", "workspace_dir": str(workspace)},
            )
            self.assertEqual(status, 200)
            sid = created["session_id"]
            status, payload = await handle_request(
                "GET",
                f"/v1/workspace?dir={workspace}&session_id={sid}",
            )
            self.assertEqual(status, 200)
            self.assertIn("docs/", payload["paths"])
            self.assertIn("docs/a.md", payload["paths"])
            status, leaked = await handle_request("GET", "/v1/workspace?dir=/etc")
            self.assertEqual(status, 200)
            self.assertEqual(leaked["paths"], [])
            self.assertTrue(leaked.get("denied"))
            escaped = workspace / ".." / ".." / "etc"
            status, escaped_body = await handle_request(
                "GET",
                f"/v1/workspace?dir={escaped}&session_id={sid}",
            )
            self.assertEqual(escaped_body["paths"], [])
            self.assertTrue(escaped_body.get("denied"))

    async def test_inbox_saves_image_under_sandbox(self) -> None:
        import base64

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root)
            blob = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            status, body = await handle_request(
                "POST",
                "/v1/inbox",
                {
                    "workspace_dir": str(workspace),
                    "filename": "paste.png",
                    "mime": "image/png",
                    "content_base64": base64.b64encode(blob).decode("ascii"),
                },
            )
            self.assertEqual(status, 200, body)
            saved = Path(body["path"])
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.read_bytes(), blob)
            self.assertEqual(saved.parent.name, ".witty-inbox")
            hint = file_reference_hint(f"看 file:{body['token']}", workspace=str(workspace))
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertIn("图片", hint.text())

    async def test_inbox_keeps_non_image_suffix(self) -> None:
        """收件箱收任意文件：.md 不能被存成 .png，正文还要能被 file: 引用注入。"""
        import base64

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root)
            blob = "# 会议纪要\n结论：下周上线。\n".encode("utf-8")
            status, body = await handle_request(
                "POST",
                "/v1/inbox",
                {
                    "workspace_dir": str(workspace),
                    "filename": "纪要.md",
                    "mime": "text/markdown",
                    "content_base64": base64.b64encode(blob).decode("ascii"),
                },
            )
            self.assertEqual(status, 200, body)
            saved = Path(body["path"])
            self.assertEqual(saved.suffix, ".md")
            self.assertEqual(saved.read_bytes(), blob)
            hint = file_reference_hint(f"看 file:{body['token']}", workspace=str(workspace))
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertIn("下周上线", hint.text())

    async def test_inbox_unknown_mime_defaults_to_bin(self) -> None:
        import base64

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root)
            status, body = await handle_request(
                "POST",
                "/v1/inbox",
                {
                    "workspace_dir": str(workspace),
                    "filename": "paste",
                    "mime": "application/octet-stream",
                    "content_base64": base64.b64encode(b"\x00\x01\x02").decode("ascii"),
                },
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(Path(body["path"]).suffix, ".bin")

    async def test_file_preview_serves_workspace_image(self) -> None:
        """聊天内联图走 /v1/file-preview：工作区相对路径换回 base64，file: 前缀也认。"""
        import base64
        from urllib.parse import quote

        blob = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            (workspace / "out").mkdir(parents=True)
            (workspace / "out" / "趋势.png").write_bytes(blob)
            configure_api(root=root)
            status, body = await handle_request(
                "GET",
                f"/v1/file-preview?workspace_dir={quote(str(workspace))}&path={quote('out/趋势.png')}",
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["mime"], "image/png")
            self.assertEqual(base64.b64decode(body["content_base64"]), blob)
            self.assertTrue(Path(body["path"]).is_absolute())
            status, body = await handle_request(
                "GET",
                "/v1/file-preview",
                {"workspace_dir": str(workspace), "path": "file:out/趋势.png"},
            )
            self.assertEqual(status, 200, body)

    async def test_file_preview_refuses_escape_and_non_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (root / "outside.png").write_bytes(b"x" * 10)
            (workspace / "notes.txt").write_text("hello", encoding="utf-8")
            configure_api(root=root)
            status, body = await handle_request(
                "GET",
                "/v1/file-preview",
                {"workspace_dir": str(workspace), "path": "../outside.png"},
            )
            self.assertEqual(status, 403, body)
            status, body = await handle_request(
                "GET",
                "/v1/file-preview",
                {"workspace_dir": str(workspace), "path": "notes.txt"},
            )
            self.assertEqual(status, 415, body)
            status, body = await handle_request(
                "GET",
                "/v1/file-preview",
                {"workspace_dir": str(workspace), "path": "missing.png"},
            )
            self.assertEqual(status, 404, body)

    async def test_session_injects_file_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "brief.md").write_text("农配网批复要点\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "对照 file:brief.md",
                stream_fn=ScriptedLLM([text_reply("ok")]),
            )
            refs = [item for item in result.messages if item.source == "plugin:file-reference"]
            self.assertEqual(len(refs), 1)
            self.assertIn("农配网批复要点", refs[0].text())
            self.assertIn("不可信", refs[0].text())

    def test_instruction_update_hint_only_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("必须先审批危险工具\n", encoding="utf-8")
            (workspace / "note.txt").write_text("not-instructions\n", encoding="utf-8")
            self.assertTrue(is_instruction_name("AGENTS.md"))
            self.assertTrue(is_instruction_name("docs/CLAUDE.md"))
            self.assertTrue(is_instruction_name("AGENTS.local.md"))
            self.assertTrue(is_instruction_name("pkg/CLAUDE.local.md"))
            self.assertFalse(is_instruction_name("note.txt"))
            hint = instruction_update_hint(str(workspace), "AGENTS.md")
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertEqual(hint.source, "plugin:instruction-update")
            self.assertIn("必须先审批危险工具", hint.text())
            self.assertIn("替换此前同路径内容", hint.text())
            self.assertIsNone(instruction_update_hint(str(workspace), "note.txt"))
            (workspace / "AGENTS.md").write_text("", encoding="utf-8")
            cleared = instruction_update_hint(str(workspace), "AGENTS.md")
            self.assertIsNotNone(cleared)
            assert cleared is not None
            self.assertIn("指令已移除", cleared.text())
            self.assertNotIn("替换此前同路径内容", cleared.text())
            (workspace / "AGENTS.md").unlink()
            gone = instruction_update_hint(str(workspace), "AGENTS.md")
            self.assertIsNotNone(gone)
            assert gone is not None
            self.assertIn("不再适用", gone.text())

    def test_instruction_update_skips_same_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("必须先审批\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            self.assertIsNone(
                instruction_update_hint(str(workspace), "AGENTS.md", seen=seen)
            )
            (workspace / "AGENTS.md").write_text("改成先跑测试\n", encoding="utf-8")
            changed = instruction_update_hint(str(workspace), "AGENTS.md", seen=seen)
            self.assertIsNotNone(changed)
            assert changed is not None
            self.assertIn("改成先跑测试", changed.text())
            self.assertIsNone(
                instruction_update_hint(str(workspace), "AGENTS.md", seen=seen)
            )

    def test_instruction_reconcile_seen_on_unrelated_touch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("必须先审批\n", encoding="utf-8")
            (workspace / "note.txt").write_text("x\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            self.assertEqual(instruction_reconcile_seen(str(workspace), seen), [])
            (workspace / "AGENTS.md").write_text("改成先跑测试\n", encoding="utf-8")
            changed = instruction_reconcile_seen(str(workspace), seen)
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0].source, "plugin:instruction-update")
            self.assertIn("改成先跑测试", changed[0].text())
            self.assertEqual(instruction_reconcile_seen(str(workspace), seen), [])
            (workspace / "AGENTS.md").unlink()
            gone = instruction_reconcile_seen(str(workspace), seen)
            self.assertEqual(len(gone), 1)
            self.assertIn("不再适用", gone[0].text())

    def test_instruction_reconcile_skips_read_when_version_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            agents = workspace / "AGENTS.md"
            agents.write_text("必须先审批\n", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("Claude 专属：用 plan\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            versions: dict[str, dict[str, str]] = {}
            self.assertEqual(
                instruction_reconcile_seen(str(workspace), seen, versions=versions),
                [],
            )
            self.assertEqual(len(versions), 2)
            root_key = str(agents.resolve())
            self.assertEqual(versions[root_key]["digest"], seen[root_key])
            self.assertEqual(
                versions[root_key]["version"], instruction_fs_version(agents)
            )
            reads: list[str] = []
            orig = ir._read_instruction_text

            def spy(path: Path) -> str:
                reads.append(str(path))
                return orig(path)

            with patch.object(ir, "_read_instruction_text", side_effect=spy):
                self.assertEqual(
                    instruction_reconcile_seen(str(workspace), seen, versions=versions),
                    [],
                )
                self.assertEqual(reads, [])
                agents.write_text("改成先跑测试\n", encoding="utf-8")
                changed = instruction_reconcile_seen(
                    str(workspace), seen, versions=versions
                )
            self.assertEqual(len(changed), 1)
            self.assertIn("改成先跑测试", changed[0].text())
            self.assertTrue(any(Path(item).name == "AGENTS.md" for item in reads))

    def test_instruction_reconcile_sibling_dirty_still_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("基线须审批\n", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("Claude 专属：用 plan\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            versions: dict[str, dict[str, str]] = {}
            self.assertEqual(
                instruction_reconcile_seen(str(workspace), seen, versions=versions),
                [],
            )
            (workspace / "AGENTS.md").write_text("又独立了\n", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("又独立了\n", encoding="utf-8")
            notices = instruction_reconcile_seen(
                str(workspace), seen, versions=versions
            )
            texts = [item.text() for item in notices]
            self.assertTrue(
                any("指令已移除" in text and "CLAUDE.md" in text for text in texts)
            )

    def test_instruction_reconcile_stat_failure_is_not_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            agents = workspace / "AGENTS.md"
            agents.write_text("必须先审批\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            versions: dict[str, dict[str, str]] = {}
            instruction_reconcile_seen(str(workspace), seen, versions=versions)
            agents.unlink()
            with patch(
                "witty_agent.instruction_refresh.instruction_fs_version",
                return_value=None,
            ):
                self.assertEqual(
                    instruction_reconcile_seen(str(workspace), seen, versions=versions),
                    [],
                )
            gone = instruction_reconcile_seen(str(workspace), seen, versions=versions)
            self.assertEqual(len(gone), 1)
            self.assertIn("不再适用", gone[0].text())

    def test_instruction_sibling_duplicate_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("基线须审批\n", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("Claude 专属：用 plan\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            self.assertEqual(len(seen), 2)
            (workspace / "CLAUDE.md").write_text("  基线须审批  \n", encoding="utf-8")
            hint = instruction_update_hint(str(workspace), "CLAUDE.md", seen=seen)
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertIn("指令已移除", hint.text())
            self.assertIn("CLAUDE.md", hint.text())
            self.assertNotIn("替换此前同路径内容", hint.text())
            self.assertEqual(instruction_update_hint(str(workspace), "CLAUDE.md", seen=seen), None)
            (workspace / "CLAUDE.md").write_text("又独立了\n", encoding="utf-8")
            (workspace / "AGENTS.md").write_text("另一段\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            (workspace / "AGENTS.md").write_text("又独立了\n", encoding="utf-8")
            notices = instruction_reconcile_seen(str(workspace), seen)
            texts = [item.text() for item in notices]
            self.assertTrue(any("指令已移除" in text and "CLAUDE.md" in text for text in texts))
            self.assertTrue(any("另一段" not in text and "又独立了" in text for text in texts))

    def test_instruction_rearm_after_compact_nested_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("根须审批\n", encoding="utf-8")
            pkg = workspace / "pkg"
            pkg.mkdir()
            (pkg / "AGENTS.md").write_text("子目录须跑测试\n", encoding="utf-8")
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            extra = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            self.assertEqual(len(extra), 1)
            before = [
                extra[0],
                AgentMessage(role="user", content="later"),
            ]
            after = [
                AgentMessage(role="user", content="[compaction]\n摘要"),
                AgentMessage(role="user", content="later"),
            ]
            rearmed = instruction_rearm_after_compact(before, after, str(workspace), seen)
            self.assertEqual(len(rearmed), 1)
            self.assertEqual(rearmed[0].source, "plugin:instruction-additional")
            self.assertIn("指令仍有效", rearmed[0].text())
            self.assertIn("子目录须跑测试", rearmed[0].text())
            self.assertNotIn("根须审批", rearmed[0].text())
            self.assertEqual(
                instruction_rearm_after_compact(before, before, str(workspace), seen),
                [],
            )

    def test_instruction_rearm_after_compact_per_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("根须审批\n", encoding="utf-8")
            pkg = workspace / "pkg"
            lib = workspace / "lib"
            pkg.mkdir()
            lib.mkdir()
            (pkg / "AGENTS.md").write_text("pkg 须跑测试\n", encoding="utf-8")
            (lib / "AGENTS.md").write_text("lib 须过 lint\n", encoding="utf-8")
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            (lib / "bar.py").write_text("y = 2\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            pkg_hint = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            lib_hint = instruction_additional_hints(str(workspace), "lib/bar.py", seen=seen)
            self.assertEqual(len(pkg_hint), 1)
            self.assertEqual(len(lib_hint), 1)
            before = [pkg_hint[0], lib_hint[0], AgentMessage(role="user", content="later")]
            after = [
                AgentMessage(role="user", content="[compaction]\n摘要"),
                lib_hint[0],
                AgentMessage(role="user", content="later"),
            ]
            rearmed = instruction_rearm_after_compact(before, after, str(workspace), seen)
            self.assertEqual(len(rearmed), 1)
            self.assertIn("pkg 须跑测试", rearmed[0].text())
            self.assertNotIn("lib 须过 lint", rearmed[0].text())

    def test_instruction_rearm_ignores_path_substring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("根须审批\n", encoding="utf-8")
            short = workspace / "pkg"
            nested = workspace / "x" / "pkg"
            short.mkdir()
            nested.mkdir(parents=True)
            (short / "AGENTS.md").write_text("短路径须跑测试\n", encoding="utf-8")
            (nested / "AGENTS.md").write_text("长路径须过 lint\n", encoding="utf-8")
            (short / "foo.py").write_text("x = 1\n", encoding="utf-8")
            (nested / "bar.py").write_text("y = 2\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            short_hint = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            long_hint = instruction_additional_hints(str(workspace), "x/pkg/bar.py", seen=seen)
            self.assertEqual(len(short_hint), 1)
            self.assertEqual(len(long_hint), 1)
            self.assertEqual(short_hint[0].meta.get("action"), "set")
            self.assertTrue(str(short_hint[0].meta.get("path") or "").endswith("pkg/AGENTS.md"))
            self.assertTrue(str(long_hint[0].meta.get("path") or "").endswith("x/pkg/AGENTS.md"))
            before = [short_hint[0], long_hint[0], AgentMessage(role="user", content="later")]
            after = [
                AgentMessage(role="user", content="[compaction]\n摘要"),
                long_hint[0],
                AgentMessage(role="user", content="later"),
            ]
            rearmed = instruction_rearm_after_compact(before, after, str(workspace), seen)
            self.assertEqual(len(rearmed), 1)
            self.assertIn("短路径须跑测试", rearmed[0].text())
            self.assertNotIn("长路径须过 lint", rearmed[0].text())
            self.assertEqual(rearmed[0].meta.get("path"), short_hint[0].meta.get("path"))
            keep_short = [
                AgentMessage(role="user", content="[compaction]\n摘要"),
                short_hint[0],
                AgentMessage(role="user", content="later"),
            ]
            other = instruction_rearm_after_compact(before, keep_short, str(workspace), seen)
            self.assertEqual(len(other), 1)
            self.assertIn("长路径须过 lint", other[0].text())

    def test_instruction_meta_survives_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("根须审批\n", encoding="utf-8")
            pkg = workspace / "pkg"
            pkg.mkdir()
            (pkg / "AGENTS.md").write_text("子目录须跑测试\n", encoding="utf-8")
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            extra = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            self.assertEqual(len(extra), 1)
            path = Path(tmp) / "session.jsonl"
            write_header(path, "sid", str(workspace))
            append_message(path, extra[0])
            loaded = load_messages(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].source, extra[0].source)
            self.assertEqual(loaded[0].meta.get("path"), extra[0].meta.get("path"))
            self.assertEqual(loaded[0].meta.get("action"), "set")
            self.assertEqual(loaded[0].meta.get("digest"), extra[0].meta.get("digest"))
            after = [
                AgentMessage(role="user", content="[compaction]\n摘要"),
                AgentMessage(role="user", content="later"),
            ]
            rearmed = instruction_rearm_after_compact(
                loaded, after, str(workspace), seen
            )
            self.assertEqual(len(rearmed), 1)
            self.assertIn("子目录须跑测试", rearmed[0].text())

    def test_instruction_seen_survives_log_fold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("必须先审批\n", encoding="utf-8")
            pkg = workspace / "pkg"
            pkg.mkdir()
            (pkg / "AGENTS.md").write_text("子目录须跑测试\n", encoding="utf-8")
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            extra = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            self.assertEqual(len(extra), 1)
            log = SessionLog()
            root_key = next(iter(seed_instruction_seen(str(workspace))))
            log.append(
                "turn/instruction-update",
                {"path": root_key, "digest": seen[root_key]},
            )
            nested = {
                key: digest
                for key, digest in seen.items()
                if key != root_key
            }
            log.append(
                "turn/instruction-additional",
                {"path": "pkg/foo.py", "count": 1, "digests": nested},
            )
            restored = fold_instruction_seen(log.events)
            self.assertEqual(restored.get(root_key), seen[root_key])
            self.assertTrue(nested)
            for key, digest in nested.items():
                self.assertEqual(restored.get(key), digest)
            self.assertIsNone(
                instruction_update_hint(str(workspace), "AGENTS.md", seen=restored)
            )
            self.assertEqual(
                instruction_additional_hints(
                    str(workspace), "pkg/foo.py", seen=dict(restored)
                ),
                [],
            )

    def test_instruction_additional_hint_nested_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("根指令须审批\n", encoding="utf-8")
            (workspace / "note.txt").write_text("not-instructions\n", encoding="utf-8")
            pkg = workspace / "pkg"
            pkg.mkdir()
            (pkg / "AGENTS.md").write_text("子目录须跑测试\n", encoding="utf-8")
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            loaded = load_context_files(workspace)
            self.assertTrue(any("根指令须审批" in item["content"] for item in loaded))
            first = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].source, "plugin:instruction-additional")
            self.assertIn("子目录须跑测试", first[0].text())
            self.assertIn("pkg/AGENTS.md", first[0].text())
            self.assertNotIn("根指令须审批", first[0].text())
            self.assertEqual(
                instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen),
                [],
            )
            self.assertEqual(
                instruction_additional_hints(str(workspace), "note.txt", seen=seen),
                [],
            )

    def test_instruction_local_overlay_after_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("基线须审批\n", encoding="utf-8")
            (workspace / "AGENTS.local.md").write_text("本机不要发公网\n", encoding="utf-8")
            (workspace / "CLAUDE.local.md").write_text("  基线须审批  \n", encoding="utf-8")
            loaded = load_context_files(workspace)
            texts = [item["content"] for item in loaded]
            paths = [Path(item["path"]).name for item in loaded]
            self.assertIn("AGENTS.md", paths)
            self.assertIn("AGENTS.local.md", paths)
            self.assertNotIn("CLAUDE.local.md", paths)
            self.assertLess(paths.index("AGENTS.md"), paths.index("AGENTS.local.md"))
            self.assertTrue(any("本机不要发公网" in text for text in texts))
            overlay = instruction_update_hint(str(workspace), "AGENTS.local.md")
            self.assertIsNotNone(overlay)
            assert overlay is not None
            self.assertIn("本机不要发公网", overlay.text())
            only_base = load_instructions_in(workspace, local=False)
            self.assertEqual(len(only_base), 1)
            self.assertTrue(only_base[0]["path"].endswith("AGENTS.md"))
            pkg = workspace / "pkg"
            pkg.mkdir()
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            (pkg / "AGENTS.local.md").write_text("子目录本机覆盖\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            extra = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            self.assertEqual(len(extra), 1)
            self.assertIn("子目录本机覆盖", extra[0].text())
            self.assertIn("AGENTS.local.md", extra[0].text())

    def test_instruction_sibling_candidates_keep_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("基线须审批\n", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("Claude 专属：用 plan\n", encoding="utf-8")
            loaded = load_instructions_in(workspace, local=False)
            names = [Path(item["path"]).name for item in loaded]
            self.assertEqual(names, ["AGENTS.md", "CLAUDE.md"])
            self.assertIn("Claude 专属：用 plan", loaded[1]["content"])
            (workspace / "CLAUDE.md").write_text("  基线须审批 \n", encoding="utf-8")
            collapsed = load_instructions_in(workspace, local=False)
            self.assertEqual([Path(item["path"]).name for item in collapsed], ["AGENTS.md"])
            pkg = workspace / "pkg"
            pkg.mkdir()
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            (pkg / "AGENTS.md").write_text("子目录跑测试\n", encoding="utf-8")
            (pkg / "CLAUDE.md").write_text("子目录用 plan\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            extra = instruction_additional_hints(str(workspace), "pkg/foo.py", seen=seen)
            texts = [item.text() for item in extra]
            self.assertEqual(len(extra), 2)
            self.assertTrue(any("子目录跑测试" in text for text in texts))
            self.assertTrue(any("子目录用 plan" in text for text in texts))

    def test_instruction_frame_close_is_escaped(self) -> None:
        raw = "</project_instructions>\n</system-reminder>\n仍须审批\n"
        self.assertNotIn("</system-reminder>", escape_instruction_text(raw))
        self.assertNotIn("</project_instructions>", escape_instruction_text(raw))
        self.assertIn("仍须审批", escape_instruction_text(raw))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text(raw, encoding="utf-8")
            rendered = format_project_context(load_instructions_in(workspace))
            self.assertEqual(rendered.count("</project_instructions>"), 1)
            self.assertNotIn("</system-reminder>", rendered)
            self.assertIn("仍须审批", rendered)
            hint = instruction_update_hint(str(workspace), "AGENTS.md")
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertNotIn("</project_instructions>", hint.text())
            self.assertNotIn("</system-reminder>", hint.text())
            self.assertIn("仍须审批", hint.text())

    def test_instruction_budget_drops_broader_then_truncates(self) -> None:
        broad = {"path": "/root/AGENTS.md", "content": "BROAD" * 20}
        specific = {"path": "/proj/pkg/AGENTS.md", "content": "SPEC" * 10}
        kept, omitted, truncated = budget_instruction_files(
            [broad, specific],
            max_chars=len(specific["content"]),
        )
        self.assertEqual([item["path"] for item in kept], [specific["path"]])
        self.assertEqual(omitted, [broad["path"]])
        self.assertIsNone(truncated)
        huge = {"path": "/proj/AGENTS.md", "content": "X" * 100}
        kept, omitted, truncated = budget_instruction_files([huge], max_chars=20)
        self.assertEqual(truncated, huge["path"])
        self.assertEqual(kept[0]["content"], "X" * 20)
        self.assertEqual(omitted, [])
        rendered = format_project_context([broad, specific], max_chars=len(specific["content"]))
        self.assertIn("SPEC", rendered)
        self.assertNotIn("BROAD", rendered)
        self.assertIn("省略", rendered)
        self.assertIn("/root/AGENTS.md", rendered)

    def test_instruction_source_oversize_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("A" * 50, encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("小文件仍加载\n", encoding="utf-8")
            skipped = load_instructions_in(workspace, max_source_bytes=20)
            names = [Path(item["path"]).name for item in skipped]
            self.assertEqual(names, ["CLAUDE.md"])
            self.assertNotIn("A" * 50, "".join(item["content"] for item in skipped))
            kept = load_instructions_in(workspace)
            self.assertTrue(any(Path(item["path"]).name == "AGENTS.md" for item in kept))

    def test_instruction_source_oversize_skips_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("A" * 50, encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("小文件仍加载\n", encoding="utf-8")
            opened: list[str] = []
            real_open = Path.open

            def spy(self: Path, *args: object, **kwargs: object):
                opened.append(self.name)
                return real_open(self, *args, **kwargs)

            with patch.object(Path, "open", spy):
                skipped = load_instructions_in(workspace, max_source_bytes=20)
            names = [Path(item["path"]).name for item in skipped]
            self.assertEqual(names, ["CLAUDE.md"])
            self.assertNotIn("AGENTS.md", opened)
            self.assertIn("CLAUDE.md", opened)

    def test_instruction_source_grows_past_cap_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text("A" * 80, encoding="utf-8")
            self.assertIsNone(_stream_text_bounded(path, 20, known_size=10))
            self.assertIsNone(_stream_text_bounded(path, 20, known_size=80))
            self.assertEqual(_stream_text_bounded(path, 200, known_size=80), b"A" * 80)
            self.assertEqual(_stream_text_bounded(path, 0, known_size=80), b"A" * 80)

    def test_instruction_reconcile_oversize_is_unavailable(self) -> None:
        from witty_agent.runtime import clear_runtime_cache

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            agents = workspace / "AGENTS.md"
            agents.write_text("必须先审批\n", encoding="utf-8")
            seen = seed_instruction_seen(str(workspace))
            prior = dict(seen)
            agents.write_text("X" * 80, encoding="utf-8")
            runtime = Path(tmp) / "runtime.toml"
            runtime.write_text("[context]\nmax_source_bytes = 20\n", encoding="utf-8")
            os.environ["WITTY_RUNTIME_FILE"] = str(runtime)
            clear_runtime_cache()
            try:
                self.assertEqual(
                    instruction_reconcile_seen(str(workspace), seen),
                    [],
                )
                self.assertEqual(seen, prior)
                self.assertIsNone(
                    instruction_update_hint(str(workspace), "AGENTS.md", seen=seen)
                )
                self.assertEqual(seen, prior)
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                clear_runtime_cache()
            changed = instruction_reconcile_seen(str(workspace), seen)
            self.assertEqual(len(changed), 1)
            self.assertIn("X" * 80, changed[0].text())
            self.assertNotIn("不再适用", changed[0].text())

    def test_context_settings_from_runtime(self) -> None:
        from witty_agent.runtime import clear_runtime_cache, context_settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runtime.toml"
            path.write_text(
                "[context]\nmax_chars = 12\nmax_source_bytes = 8\n",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text("ABCDEFGHIJ\n", encoding="utf-8")
            os.environ["WITTY_RUNTIME_FILE"] = str(path)
            clear_runtime_cache()
            try:
                self.assertEqual(context_settings()["max_chars"], 12)
                self.assertEqual(context_settings()["max_source_bytes"], 8)
                self.assertEqual(load_instructions_in(root), [])
                rendered = format_project_context(
                    [{"path": "AGENTS.md", "content": "ABCDEFGHIJKLMNOP"}],
                )
                self.assertIn("截断", rendered)
                self.assertNotIn("MNOP", rendered)
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                clear_runtime_cache()

    def test_instruction_candidates_from_runtime(self) -> None:
        from witty_agent.runtime import clear_runtime_cache, context_settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runtime.toml"
            path.write_text(
                "[context]\n"
                "instruction_files = [\"POLICY.md\", \"\", \"../escape.md\", \"AGENTS.md\"]\n"
                "local_instruction_files = [\"POLICY.local.md\"]\n",
                encoding="utf-8",
            )
            (root / "POLICY.md").write_text("先审批危险工具\n", encoding="utf-8")
            (root / "POLICY.local.md").write_text("本机可关审批\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("默认代理人\n", encoding="utf-8")
            os.environ["WITTY_RUNTIME_FILE"] = str(path)
            clear_runtime_cache()
            try:
                self.assertEqual(context_settings()["instruction_files"], ["POLICY.md", "AGENTS.md"])
                self.assertTrue(is_instruction_name("POLICY.md"))
                self.assertTrue(is_instruction_name("POLICY.local.md"))
                self.assertFalse(is_instruction_name("../escape.md"))
                self.assertEqual(
                    instruction_file_candidates(local=False),
                    ("POLICY.md", "AGENTS.md"),
                )
                loaded = load_instructions_in(root)
                names = [Path(item["path"]).name for item in loaded]
                self.assertEqual(names, ["POLICY.md", "AGENTS.md", "POLICY.local.md"])
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                clear_runtime_cache()
            self.assertFalse(is_instruction_name("POLICY.md"))

    def test_user_global_instruction_from_witty_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            home = outer / "home"
            workspace = outer / "ws"
            home.mkdir()
            workspace.mkdir()
            (outer / "AGENTS.md").write_text("父目录不要加载\n", encoding="utf-8")
            (home / "AGENTS.md").write_text("全局先审批\n", encoding="utf-8")
            (home / "AGENTS.local.md").write_text("全局 overlay 不要\n", encoding="utf-8")
            (home / "CLAUDE.md").write_text("全局候选项不要\n", encoding="utf-8")
            (workspace / "AGENTS.md").write_text("项目须跑测试\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                self.assertEqual(witty_home_display(), "$WITTY_HOME")
                self.assertEqual(global_instruction_path(), home.resolve() / "AGENTS.md")
                loaded = load_context_files(workspace)
                texts = [item["content"] for item in loaded]
                displays = [item.get("display") for item in loaded]
                self.assertEqual(texts, ["全局先审批\n", "项目须跑测试\n"])
                self.assertEqual(displays, ["$WITTY_HOME/AGENTS.md", "AGENTS.md"])
                self.assertEqual(
                    instruction_display(home / "AGENTS.md", workspace),
                    "$WITTY_HOME/AGENTS.md",
                )
                rendered = format_project_context(loaded)
                self.assertIn("$WITTY_HOME/AGENTS.md", rendered)
                self.assertNotIn(str(home.resolve()), rendered)
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    def test_project_root_stops_at_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            repo = outer / "repo"
            nested = repo / "pkg"
            home = outer / "home"
            nested.mkdir(parents=True)
            home.mkdir()
            (outer / "AGENTS.md").write_text("仓库外不要\n", encoding="utf-8")
            (repo / ".git").mkdir()
            (repo / "AGENTS.md").write_text("仓库根\n", encoding="utf-8")
            (nested / "AGENTS.md").write_text("子目录\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                self.assertEqual(find_project_root(nested), repo.resolve())
                loaded = load_context_files(nested)
                self.assertEqual(
                    [item["content"] for item in loaded],
                    ["仓库根\n", "子目录\n"],
                )
                self.assertEqual(
                    [item.get("display") for item in loaded],
                    ["AGENTS.md", "pkg/AGENTS.md"],
                )
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    def test_no_marker_uses_cwd_as_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            child = outer / "child"
            home = outer / "home"
            child.mkdir()
            home.mkdir()
            (outer / "AGENTS.md").write_text("无标记父目录不要\n", encoding="utf-8")
            (child / "AGENTS.md").write_text("cwd 指令\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                self.assertEqual(find_project_root(child), child.resolve())
                loaded = load_context_files(child)
                self.assertEqual([item["content"] for item in loaded], ["cwd 指令\n"])
                self.assertEqual([item.get("display") for item in loaded], ["AGENTS.md"])
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    def test_user_global_dedup_when_home_is_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("同一份\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(workspace)
            try:
                loaded = load_context_files(workspace)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0]["content"], "同一份\n")
                self.assertEqual(loaded[0]["display"], "$WITTY_HOME/AGENTS.md")
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    def test_witty_home_display_default(self) -> None:
        previous = os.environ.pop("WITTY_HOME", None)
        try:
            self.assertEqual(witty_home_display(), "~/.witty/data")
            self.assertEqual(
                global_instruction_path(),
                Path.home() / ".witty" / "data" / "AGENTS.md",
            )
        finally:
            if previous is not None:
                os.environ["WITTY_HOME"] = previous

    def test_project_root_markers_from_runtime(self) -> None:
        from witty_agent.runtime import clear_runtime_cache, context_settings

        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            marked = outer / "marked"
            child = marked / "pkg"
            home = outer / "home"
            child.mkdir(parents=True)
            home.mkdir()
            (outer / "AGENTS.md").write_text("标记外不要\n", encoding="utf-8")
            (marked / "PACKAGE").write_text("marker\n", encoding="utf-8")
            (marked / "AGENTS.md").write_text("标记根\n", encoding="utf-8")
            runtime = outer / "runtime.toml"
            runtime.write_text(
                "[context]\n"
                "project_root_markers = [\"PACKAGE\", \"\", \"../escape\", \".git\"]\n",
                encoding="utf-8",
            )
            previous_home = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            os.environ["WITTY_RUNTIME_FILE"] = str(runtime)
            clear_runtime_cache()
            try:
                self.assertEqual(
                    context_settings()["project_root_markers"],
                    ["PACKAGE", ".git"],
                )
                self.assertEqual(find_project_root(child), marked.resolve())
                loaded = load_context_files(child)
                self.assertEqual([item["content"] for item in loaded], ["标记根\n"])
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                if previous_home is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous_home
                clear_runtime_cache()

    def test_instruction_baseline_message_folds_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            home = outer / "home"
            workspace = outer / "ws"
            home.mkdir()
            workspace.mkdir()
            (home / "AGENTS.md").write_text("全局先审批\n", encoding="utf-8")
            (workspace / "AGENTS.md").write_text("项目须跑测试\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                folded = instruction_baseline_message(str(workspace))
                self.assertIsNotNone(folded)
                assert folded is not None
                self.assertEqual(folded.source, BASELINE_SOURCE)
                self.assertEqual(folded.role, "user")
                self.assertEqual((folded.meta or {}).get("baseline"), "true")
                self.assertIn("全局先审批", folded.text())
                self.assertIn("项目须跑测试", folded.text())
                self.assertTrue(visible_instruction_baseline([folded]))
                self.assertFalse(visible_instruction_baseline([]))
                blank = outer / "blank"
                blank.mkdir()
                os.environ["WITTY_HOME"] = str(blank)
                self.assertIsNone(instruction_baseline_message(str(blank)))
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    def test_baseline_identity_is_discovery_not_content(self) -> None:
        from witty_agent.runtime import clear_runtime_cache

        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            home = outer / "home"
            workspace = outer / "ws"
            home.mkdir()
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("先审批\n", encoding="utf-8")
            previous_home = os.environ.get("WITTY_HOME")
            previous_runtime = os.environ.get("WITTY_RUNTIME_FILE")
            os.environ["WITTY_HOME"] = str(home)
            try:
                first = instruction_baseline_identity(str(workspace))
                (workspace / "AGENTS.md").write_text("改成先跑测试\n", encoding="utf-8")
                self.assertEqual(instruction_baseline_identity(str(workspace)), first)
                runtime = outer / "runtime.toml"
                runtime.write_text(
                    "[context]\ninstruction_files = [\"POLICY.md\"]\n",
                    encoding="utf-8",
                )
                os.environ["WITTY_RUNTIME_FILE"] = str(runtime)
                clear_runtime_cache()
                changed = instruction_baseline_identity(str(workspace))
                self.assertNotEqual(changed, first)
                folded = instruction_baseline_message(str(workspace), replace=True)
                self.assertIsNotNone(folded)
                assert folded is not None
                self.assertEqual((folded.meta or {}).get("action"), "replace")
                self.assertIn(get_prompt("instruction_baseline_replace_empty"), folded.text())
                self.assertEqual(visible_baseline_identity([folded]), changed)
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                if previous_runtime is not None:
                    os.environ["WITTY_RUNTIME_FILE"] = previous_runtime
                if previous_home is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous_home
                clear_runtime_cache()

    def test_instruction_offline_transitions_add_edit_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            home = workspace / "home"
            home.mkdir()
            agents = workspace / "AGENTS.md"
            agents.write_text("根须审批\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                seen = seed_instruction_seen(str(workspace))
                self.assertEqual(
                    instruction_offline_transitions(str(workspace), dict(seen)),
                    [],
                )
                (workspace / "CLAUDE.md").write_text("用 plan\n", encoding="utf-8")
                agents.write_text("改成先跑测试\n", encoding="utf-8")
                live = dict(seen)
                changed = instruction_offline_transitions(str(workspace), live)
                actions = {(item.meta or {}).get("action") for item in changed}
                texts = [item.text() for item in changed]
                self.assertEqual(actions, {"set", "replace"})
                self.assertTrue(any("用 plan" in text for text in texts))
                self.assertTrue(any("改成先跑测试" in text for text in texts))
                agents.unlink()
                gone = instruction_offline_transitions(str(workspace), live)
                self.assertTrue(
                    any((item.meta or {}).get("action") == "remove" for item in gone),
                    gone,
                )
                self.assertTrue(any("不再适用" in item.text() for item in gone))
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    def test_instruction_rearm_restores_shadowed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            home = workspace / "home"
            home.mkdir()
            (workspace / "AGENTS.md").write_text("根须审批\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                folded = instruction_baseline_message(str(workspace))
                self.assertIsNotNone(folded)
                assert folded is not None
                before = [folded, AgentMessage(role="user", content="later")]
                after = [
                    AgentMessage(role="user", content="[compaction]\n摘要"),
                    AgentMessage(role="user", content="later"),
                ]
                seen = seed_instruction_seen(str(workspace))
                rearmed = instruction_rearm_after_compact(before, after, str(workspace), seen)
                self.assertEqual(len(rearmed), 1)
                self.assertEqual(rearmed[0].source, BASELINE_SOURCE)
                self.assertIn("根须审批", rearmed[0].text())
                self.assertEqual(
                    instruction_rearm_after_compact(before, before, str(workspace), seen),
                    [],
                )
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    async def test_session_folds_baseline_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace = root / "ws"
            home.mkdir()
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("危险工具必须先批准\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                agent = create_agent("grid-base", "coder", root=root)
                session = create_session(agent, workspace_dir=workspace)
                first = await session.run(
                    "你好",
                    stream_fn=ScriptedLLM([text_reply("好")]),
                    approval_mode="allow-all",
                )
                folded = [item for item in first.messages if item.source == BASELINE_SOURCE]
                self.assertEqual(len(folded), 1)
                self.assertIn("危险工具必须先批准", folded[0].text())
                second = await session.run(
                    "还在吗",
                    stream_fn=ScriptedLLM([text_reply("在")]),
                    approval_mode="allow-all",
                )
                again = [item for item in second.messages if item.source == BASELINE_SOURCE]
                self.assertEqual(again, [])
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    async def test_session_replaces_incompatible_baseline(self) -> None:
        from witty_agent.runtime import clear_runtime_cache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace = root / "ws"
            home.mkdir()
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("危险工具必须先批准\n", encoding="utf-8")
            previous_home = os.environ.get("WITTY_HOME")
            previous_runtime = os.environ.get("WITTY_RUNTIME_FILE")
            os.environ["WITTY_HOME"] = str(home)
            try:
                agent = create_agent("grid-base", "coder", root=root)
                session = create_session(agent, workspace_dir=workspace)
                first = await session.run(
                    "你好",
                    stream_fn=ScriptedLLM([text_reply("好")]),
                    approval_mode="allow-all",
                )
                self.assertTrue(
                    any(item.source == BASELINE_SOURCE for item in first.messages)
                )
                runtime = root / "runtime.toml"
                runtime.write_text(
                    "[context]\ninstruction_files = [\"POLICY.md\"]\n",
                    encoding="utf-8",
                )
                os.environ["WITTY_RUNTIME_FILE"] = str(runtime)
                clear_runtime_cache()
                second = await session.run(
                    "还在吗",
                    stream_fn=ScriptedLLM([text_reply("在")]),
                    approval_mode="allow-all",
                )
                replaced = [item for item in second.messages if item.source == BASELINE_SOURCE]
                self.assertEqual(len(replaced), 1)
                self.assertEqual((replaced[0].meta or {}).get("action"), "replace")
                self.assertIn(get_prompt("instruction_baseline_replace_empty"), replaced[0].text())
            finally:
                os.environ.pop("WITTY_RUNTIME_FILE", None)
                if previous_runtime is not None:
                    os.environ["WITTY_RUNTIME_FILE"] = previous_runtime
                if previous_home is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous_home
                clear_runtime_cache()

    async def test_session_offline_instruction_add(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace = root / "ws"
            home.mkdir()
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("危险工具必须先批准\n", encoding="utf-8")
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = str(home)
            try:
                agent = create_agent("grid-base", "coder", root=root)
                session = create_session(agent, workspace_dir=workspace)
                await session.run(
                    "你好",
                    stream_fn=ScriptedLLM([text_reply("好")]),
                    approval_mode="allow-all",
                )
                (workspace / "CLAUDE.md").write_text("用 plan 模式\n", encoding="utf-8")
                second = await session.run(
                    "还在吗",
                    stream_fn=ScriptedLLM([text_reply("在")]),
                    approval_mode="allow-all",
                )
                added = [
                    item
                    for item in second.messages
                    if item.source == "plugin:instruction-additional"
                ]
                self.assertEqual(len(added), 1)
                self.assertIn("用 plan 模式", added[0].text())
                self.assertFalse(
                    any(item.source == BASELINE_SOURCE for item in second.messages)
                )
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    async def test_session_injects_instruction_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "写一条项目指令",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("write", {"path": "AGENTS.md", "content": "危险工具必须先批准\n"}),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            notes = [item for item in result.messages if item.source == "plugin:instruction-update"]
            self.assertEqual(len(notes), 1)
            self.assertIn("危险工具必须先批准", notes[0].text())
            self.assertEqual((workspace / "AGENTS.md").read_text(encoding="utf-8"), "危险工具必须先批准\n")
            extras = [item for item in result.messages if item.source == "plugin:instruction-additional"]
            self.assertEqual(extras, [])

    async def test_session_injects_nested_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            pkg = workspace / "pkg"
            pkg.mkdir(parents=True)
            (pkg / "AGENTS.md").write_text("子目录须跑测试\n", encoding="utf-8")
            (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "读一下 pkg/foo.py",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "pkg/foo.py"}),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            notes = [
                item for item in result.messages if item.source == "plugin:instruction-additional"
            ]
            self.assertEqual(len(notes), 1)
            self.assertIn("子目录须跑测试", notes[0].text())
            again = await session.run(
                "再读一次",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "pkg/foo.py"}),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            self.assertEqual(
                [item for item in again.messages if item.source == "plugin:instruction-additional"],
                [],
            )

    async def test_session_reconciles_instruction_on_other_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("必须先审批\n", encoding="utf-8")
            (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            first = await session.run(
                "读 note.txt",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "note.txt"}),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            self.assertEqual(
                [item for item in first.messages if item.source == "plugin:instruction-update"],
                [],
            )
            (workspace / "AGENTS.md").write_text("改成先跑测试\n", encoding="utf-8")
            result = await session.run(
                "再读 note.txt",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "note.txt"}),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            notes = [item for item in result.messages if item.source == "plugin:instruction-update"]
            self.assertEqual(len(notes), 1)
            self.assertIn("改成先跑测试", notes[0].text())
            again = await session.run(
                "再读一次",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "note.txt"}),
                        text_reply("ok"),
                    ]
                ),
                approval_mode="allow-all",
            )
            self.assertEqual(
                [item for item in again.messages if item.source == "plugin:instruction-update"],
                [],
            )

    def test_invariant_catches_unlogged(self) -> None:
        log = SessionLog()
        log.append("user/message", {"text": "hi", "source": "user"})
        failures = check_visible_logged(log, [AgentMessage(role="user", content="other")])
        self.assertTrue(failures)


class JobsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_and_projection_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            configure_api(root=root)
            from witty_agent.http_api import STATE

            STATE.sessions[session.session_id] = session
            status, body = await handle_request(
                "POST",
                f"/v1/sessions/{session.session_id}/command",
                {"text": "/plan"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["kind"], "success")
            status, view = await handle_request("GET", f"/v1/sessions/{session.session_id}/projection")
            self.assertEqual(status, 200)
            self.assertTrue(view["plan"]["active"])


if __name__ == "__main__":
    unittest.main()
