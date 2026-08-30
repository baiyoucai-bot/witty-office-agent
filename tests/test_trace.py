from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from witty_agent.http_api import configure_api, handle_request
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.memory import resolve_session_memory, write_topic
from witty_agent.prompts import get_prompt
from witty_agent.trace import attach_turn_evidence, collect_turn_evidence
from witty_agent.types import AgentMessage


class TraceEvidenceTests(unittest.TestCase):
    def test_collects_tool_source_and_locator(self) -> None:
        messages = [
            AgentMessage(role="user", content="read the file"),
            tool_reply("read", {"path": "foo.py"}, call_id="c1"),
            AgentMessage(
                role="toolResult",
                content="print(1)\n",
                tool_call_id="c1",
                tool_name="read",
            ),
            text_reply("it prints 1"),
        ]
        items, reason = collect_turn_evidence(messages)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "read")
        self.assertEqual(items[0]["locator"], "foo.py")
        self.assertTrue(items[0]["ok"])
        self.assertIn("print(1)", items[0]["excerpt"])
        self.assertIn("read", reason)
        self.assertEqual(reason, get_prompt("trace_reason_tools", tools="read", count="1"))

    def test_no_tools_marks_unverified(self) -> None:
        messages = [
            AgentMessage(role="user", content="hi"),
            AgentMessage(role="user", content="clock", source="plugin:time-context"),
            text_reply("hello"),
        ]
        items, reason = collect_turn_evidence(messages)
        self.assertEqual(items, [])
        self.assertEqual(reason, get_prompt("trace_reason_none"))

    def test_empty_lookup_is_not_a_source(self) -> None:
        messages = [
            AgentMessage(role="user", content="note.txt 里写了什么？"),
            tool_reply("grep", {"pattern": "secret", "path": "."}, call_id="g1"),
            AgentMessage(
                role="toolResult",
                content="(no matches)",
                tool_call_id="g1",
                tool_name="grep",
            ),
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
        items, reason = collect_turn_evidence(messages)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "grep")
        self.assertFalse(items[0]["ok"])
        self.assertEqual(reason, get_prompt("trace_reason_none"))

    def test_recalled_memory_is_evidence(self) -> None:
        messages = [
            AgentMessage(role="user", content="我喜欢什么样的回复"),
            text_reply("简短回复"),
        ]
        hits = [{"slug": "prefs", "title": "个人偏好", "text": "我喜欢简短回复", "score": 7}]
        items, reason = collect_turn_evidence(messages, memory_hits=hits)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "memory")
        self.assertEqual(items[0]["source"], "memory_read")
        self.assertEqual(items[0]["locator"], "prefs")
        self.assertEqual(items[0]["score"], 7)
        self.assertIn("简短", items[0]["excerpt"])
        self.assertNotIn("relocated", items[0])
        self.assertEqual(reason, get_prompt("trace_reason_memory", slugs="prefs", count="1"))
        archived = collect_turn_evidence(
            messages,
            memory_hits=[
                {
                    "slug": "archive/prefs",
                    "text": "喜欢吃桃子",
                    "score": 6,
                    "layer": "archive",
                }
            ],
        )[0]
        self.assertEqual(archived[0]["layer"], "archive")
        self.assertEqual(archived[0]["locator"], "archive/prefs")
        mixed_hits, _ = collect_turn_evidence(
            messages,
            memory_hits=[
                {
                    "slug": "archive/prefs",
                    "text": "喜欢吃桃子",
                    "score": 6,
                    "layer": "archive",
                },
                {"slug": "prefs", "text": "我喜欢简短回复", "score": 7, "layer": "working"},
            ],
        )
        self.assertEqual(
            [item["locator"] for item in mixed_hits if item["kind"] == "memory"],
            ["prefs", "archive/prefs"],
        )
        loaded_archive, _ = collect_turn_evidence(
            [
                AgentMessage(role="user", content="偏好是什么？"),
                tool_reply("memory_read", {"slug": "archive/prefs"}, call_id="a1"),
                AgentMessage(
                    role="toolResult",
                    content="- 喜欢吃桃子",
                    tool_call_id="a1",
                    tool_name="memory_read",
                ),
                text_reply("简短"),
            ],
            memory_hits=[{"slug": "prefs", "text": "我喜欢简短回复", "score": 7}],
        )
        self.assertEqual(
            [item["locator"] for item in loaded_archive if item["kind"] == "memory"],
            ["prefs", "archive/prefs"],
        )
        moved = collect_turn_evidence(
            messages,
            memory_hits=[
                {
                    "slug": "solo-txt",
                    "text": "read notes/solo.txt: unique-body",
                    "score": 3,
                    "relocated": [{"from": "solo.txt", "to": "notes/solo.txt"}],
                }
            ],
        )[0]
        self.assertEqual(moved[0]["relocated"], [{"from": "solo.txt", "to": "notes/solo.txt"}])
        self.assertEqual(moved[0]["locator"], "solo-txt")
        mixed = [
            AgentMessage(role="user", content="check"),
            tool_reply("read", {"path": "foo.py"}, call_id="c1"),
            AgentMessage(role="toolResult", content="print(1)", tool_call_id="c1", tool_name="read"),
            text_reply("ok"),
        ]
        both, both_reason = collect_turn_evidence(mixed, memory_hits=hits)
        self.assertEqual([item["kind"] for item in both], ["tool", "memory"])
        self.assertIn("read", both_reason)
        self.assertIn("prefs", both_reason)

    def test_memory_hits_keep_scope(self) -> None:
        messages = [
            AgentMessage(role="user", content="OAuth2 怎么定的"),
            text_reply("工作区里记过"),
        ]
        hits = [
            {"slug": "decisions", "title": "已做决定", "text": "用户拍板用简体", "scope": "user"},
            {"slug": "decisions", "title": "已做决定", "text": "已决定采用 OAuth2", "scope": "workspace"},
        ]
        items, reason = collect_turn_evidence(messages, memory_hits=hits)
        self.assertEqual(len(items), 2)
        scopes = {item["scope"] for item in items}
        self.assertEqual(scopes, {"user", "workspace"})
        self.assertTrue(all(item["locator"] == "decisions" for item in items))
        self.assertIn("decisions", reason)

    def test_empty_recalled_browse_hints_are_not_sources(self) -> None:
        messages = [
            AgentMessage(role="user", content="量子纠缠超导"),
            text_reply("不清楚"),
        ]
        empty = {
            "reason": "no_overlap",
            "tokens": ["量子纠缠"],
            "populated": [
                {"id": "prefs", "title": "个人偏好", "count": 1, "scope": "user"},
                {"id": "decisions", "title": "已做决定", "count": 1, "scope": "workspace"},
            ],
            "archive_count": 0,
        }
        items, reason = collect_turn_evidence(messages, memory_empty=empty)
        self.assertEqual([item["kind"] for item in items], ["browse", "browse"])
        self.assertEqual(items[0]["source"], "memory_status")
        self.assertEqual(items[0]["locator"], "prefs")
        self.assertNotEqual(reason, get_prompt("trace_reason_none"))
        self.assertEqual(
            reason,
            get_prompt("trace_reason_browse", slugs="prefs, decisions", count="2"),
        )
        generic, generic_reason = collect_turn_evidence(
            messages,
            memory_empty={"reason": "too_generic", "populated": empty["populated"]},
        )
        self.assertEqual(generic, [])
        self.assertEqual(generic_reason, get_prompt("trace_reason_none"))
        hit, hit_reason = collect_turn_evidence(
            messages,
            memory_hits=[{"slug": "prefs", "text": "我喜欢简短回复"}],
            memory_empty=empty,
        )
        self.assertEqual([item["kind"] for item in hit], ["memory"])
        self.assertIn("prefs", hit_reason)
        self.assertNotIn("browse hints", hit_reason)
        share, share_reason = collect_turn_evidence(
            [AgentMessage(role="user", content="我爱吃冰淇淋"), text_reply("好的")],
            memory_empty=empty,
        )
        self.assertEqual(share, [])
        self.assertEqual(share_reason, get_prompt("trace_reason_none"))
        archived, archived_reason = collect_turn_evidence(
            messages,
            memory_empty={
                "reason": "no_overlap",
                "populated": [],
                "archive": [{"id": "archive/domain", "title": "归档·domain", "count": 2, "scope": "user"}],
            },
        )
        self.assertEqual([item["locator"] for item in archived], ["archive/domain"])
        self.assertIn("archive/domain", archived_reason)
        loaded, loaded_reason = collect_turn_evidence(
            [
                AgentMessage(role="user", content="个人偏好是什么？"),
                tool_reply("memory_read", {"slug": "prefs", "scope": "user"}, call_id="browse-1"),
                AgentMessage(
                    role="toolResult",
                    content="- 我喜欢简短回复",
                    tool_call_id="browse-1",
                    tool_name="memory_read",
                ),
                text_reply("简短回复"),
            ],
            memory_empty=empty,
        )
        self.assertEqual([item["kind"] for item in loaded], ["memory"])
        self.assertEqual(loaded[0]["locator"], "prefs")
        self.assertTrue(loaded[0].get("loaded"))
        self.assertEqual(loaded_reason, get_prompt("trace_reason_memory", slugs="prefs", count="1"))

    def test_loaded_skill_is_evidence(self) -> None:
        messages = [
            AgentMessage(role="user", content="做一份幻灯片"),
            AgentMessage(
                role="user",
                content='<skill name="slides" location="skills/slides/SKILL.md">\n做汇报/评审用的幻灯片。\n</skill>',
                source="plugin:skill-invocation",
            ),
            text_reply("按技能做一页封面"),
        ]
        items, reason = collect_turn_evidence(messages)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "skill")
        self.assertEqual(items[0]["source"], "skill")
        self.assertEqual(items[0]["locator"], "slides")
        self.assertIn("汇报", items[0]["excerpt"])
        self.assertEqual(reason, get_prompt("trace_reason_skill", skills="slides", count="1"))

    def test_attach_lands_on_final_assistant(self) -> None:
        final = text_reply("done")
        messages = [
            AgentMessage(role="user", content="go"),
            tool_reply("ls", {"path": "."}, call_id="c1"),
            AgentMessage(role="toolResult", content="a.py", tool_call_id="c1", tool_name="ls"),
            final,
        ]
        items, reason = attach_turn_evidence(messages)
        self.assertEqual(final.evidence, items)
        self.assertEqual(final.trace_reason, reason)
        self.assertEqual(messages[1].evidence, [])


class TraceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_and_messages_expose_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            note = workspace / "note.txt"
            note.write_text("alpha-source\n", encoding="utf-8")
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [
                        tool_reply("read", {"path": "note.txt"}, call_id="r1"),
                        text_reply("the note says alpha-source"),
                    ]
                ),
            )
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, started = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "what is in note.txt", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            self.assertTrue(done["evidence"], done)
            self.assertEqual(done["evidence"][0]["source"], "read")
            self.assertIn("note.txt", done["evidence"][0]["locator"])
            self.assertIn("alpha-source", done["evidence"][0]["excerpt"])
            self.assertIn("read", done["trace_reason"])
            status, body = await handle_request("GET", f"/v1/sessions/{sid}/messages")
            self.assertEqual(status, 200)
            assistant = [item for item in body["messages"] if item["role"] == "assistant" and item.get("text")]
            self.assertTrue(assistant)
            self.assertEqual(assistant[-1]["evidence"][0]["source"], "read")
            self.assertTrue(assistant[-1]["trace_reason"])

    async def test_run_exposes_recalled_memory_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
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
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM([text_reply("按偏好用简短回复")]),
            )
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, started = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "简短回复偏好是什么", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            memory_items = [item for item in done["evidence"] if item.get("kind") == "memory"]
            self.assertTrue(memory_items, done)
            self.assertEqual(memory_items[0]["source"], "memory_read")
            self.assertEqual(memory_items[0]["locator"], "prefs")
            self.assertIn("简短", memory_items[0]["excerpt"])
            self.assertIn("prefs", done["trace_reason"])

    async def test_run_exposes_seal_without_replacing_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [text_reply("里面是 42"), text_reply("还是 42")]
                ),
            )
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, started = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "note.txt 里写了什么？", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["text"], "还是 42")
            self.assertIn("未核实", done["sealed"])
            self.assertNotEqual(done["sealed"], done["text"])
            ends = [
                item
                for item in done["timeline"]
                if item.get("type") == "message_end" and item.get("source") == "plugin:evidence-seal"
            ]
            self.assertTrue(ends)
            self.assertIn("未核实", ends[0].get("text") or "")
            status, body = await handle_request("GET", f"/v1/sessions/{sid}/messages")
            self.assertEqual(status, 200)
            visible = [item for item in body["messages"] if item.get("role") == "assistant" and item.get("text")]
            self.assertGreaterEqual(len(visible), 2)
            self.assertEqual(visible[-1]["source"], "plugin:evidence-seal")
            self.assertEqual(visible[-2]["text"], "还是 42")


if __name__ == "__main__":
    unittest.main()
