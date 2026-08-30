from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.http_api import configure_api, handle_request
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.session import create_agent, create_session
from witty_agent.system_prompt import build_system_prompt
from witty_agent.vault import load_vault, mask_vault, set_vault_entry


class BackendProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_vault_keys_in_prompt_not_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            set_vault_entry("GRID_TOKEN", "super-secret", "grid-base", "coder", root=root)
            vault = load_vault("grid-base", "coder", root=root)
            self.assertEqual(vault["GRID_TOKEN"], "super-secret")
            self.assertEqual(mask_vault(vault)["GRID_TOKEN"], "***")
            prompt = build_system_prompt(".", tool_names=["bash"], vault_keys=list(vault))
            self.assertIn("GRID_TOKEN", prompt)
            self.assertNotIn("super-secret", prompt)

    async def test_vault_injected_into_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            set_vault_entry("GRID_TOKEN", "secret-value", "grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            llm = ScriptedLLM(
                [
                    tool_reply("bash", {"command": "printf %s \"$GRID_TOKEN\""}, call_id="b1"),
                    text_reply("done"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            result = await session.run("echo token", stream_fn=llm, approve=allow)
            tools = [item for item in result.messages if item.role == "toolResult"]
            self.assertTrue(tools)
            self.assertIn("secret-value", tools[0].text())
            self.assertTrue(session.scratchpad and session.scratchpad.is_dir())

    async def test_abort_before_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            started = asyncio.Event()

            async def hold(_context) -> AgentMessage:
                started.set()
                await asyncio.sleep(0.2)
                return text_reply("should-not-run")

            task = asyncio.create_task(session.run("hi", stream_fn=hold))
            await started.wait()
            session.abort()
            result = await task
            self.assertEqual(result.messages[-1].stop_reason, "aborted")

    async def test_new_turn_after_abort_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.abort()
            result = await session.run("hi", stream_fn=ScriptedLLM([text_reply("still-ok")]))
            self.assertEqual(result.messages[-1].text(), "still-ok")
            self.assertEqual(result.messages[-1].stop_reason, "end_turn")

    async def test_steer_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            session.steer("extra hint")
            result = await session.run("hi", stream_fn=ScriptedLLM([text_reply("ok")]))
            texts = [item.text() for item in result.messages if item.role == "user"]
            self.assertIn("extra hint", texts)

    async def test_http_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("from-api")]))
            status, created = await handle_request(
                "POST",
                "/v1/agents",
                {"project_id": "grid-base", "agent_id": "coder"},
            )
            self.assertEqual(status, 200)
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
            status, reply = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "hello", "approval_mode": "allow-all"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(reply["text"], "from-api")
            status, steered = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/steer",
                {"text": "extra hint"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(steered.get("ok"))
            status, empty = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/steer",
                {"text": "   "},
            )
            self.assertEqual(status, 400)
            status, planned = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "/plan", "approval_mode": "allow-all"},
            )
            self.assertEqual(status, 200)
            status, msgs = await handle_request("GET", f"/v1/sessions/{sid}/messages")
            self.assertEqual(status, 200)
            self.assertTrue((msgs.get("plan") or {}).get("active"))
            status, health = await handle_request("GET", "/v1/health")
            self.assertTrue(health["ok"])

    async def test_http_async_run_and_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            os.environ["WITTY_HOME"] = tmp
            os.environ["WITTY_APPROVAL_TIMEOUT_SEC"] = "8"
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [
                        tool_reply("write", {"path": "ok.txt", "content": "yes"}),
                        text_reply("approved-ok"),
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
                {"prompt": "write it", "approval_mode": "always-ask", "wait": False},
            )
            self.assertEqual(status, 202)
            self.assertIn(started["status"], {"running", "awaiting_approval"})
            pending = started.get("pending") if started.get("status") == "awaiting_approval" else None
            for _ in range(200):
                if pending:
                    break
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] == "awaiting_approval":
                    pending = run["pending"]
                    break
                if run["status"] in {"done", "error"}:
                    self.fail(f"run finished before approval: {run}")
                await asyncio.sleep(0.05)
            self.assertIsNotNone(pending)
            self.assertEqual(pending["tool_name"], "write")
            status, ack = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/approval",
                {"tool_call_id": pending["tool_call_id"], "decision": "allow"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(ack["ok"])
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["text"], "approved-ok")
            self.assertTrue((workspace / "ok.txt").is_file())
            # write 补尾换行（契约见 test_fs_observe.test_write_and_edit_add_trailing_newline）
            self.assertEqual((workspace / "ok.txt").read_text(encoding="utf-8"), "yes\n")

    async def test_http_async_run_and_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            os.environ["WITTY_HOME"] = tmp
            os.environ["WITTY_QUESTION_TIMEOUT_SEC"] = "8"
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [
                        tool_reply(
                            "ask_user_question",
                            {
                                "questions": [
                                    {
                                        "id": "auth",
                                        "question": "OAuth2 还是 JWT？",
                                        "options": [{"label": "OAuth2"}, {"label": "JWT"}],
                                    }
                                ]
                            },
                            call_id="q1",
                        ),
                        text_reply("采用 OAuth2"),
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
                {"prompt": "用 OAuth2 还是 JWT？", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            question = started.get("question") if started.get("status") == "awaiting_question" else None
            for _ in range(200):
                if question:
                    break
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] == "awaiting_question":
                    question = run["question"]
                    break
                if run["status"] in {"done", "error"}:
                    self.fail(f"run finished before question: {run}")
                await asyncio.sleep(0.05)
            self.assertIsNotNone(question)
            self.assertEqual(question["questions"][0]["id"], "auth")
            status, ack = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/answer",
                {"answers": [{"id": "auth", "selected": ["OAuth2"]}]},
            )
            self.assertEqual(status, 200)
            self.assertTrue(ack["ok"])
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["text"], "采用 OAuth2")

    async def test_http_posed_choice_opens_question_without_second_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            os.environ["WITTY_HOME"] = tmp
            os.environ["WITTY_QUESTION_TIMEOUT_SEC"] = "8"
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [
                        text_reply("用青绿模板还是简约风？"),
                        text_reply("SHOULD_NOT_RUN_BEFORE_ANSWER"),
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
                {"prompt": "帮我写一份季度报告", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            question = started.get("question") if started.get("status") == "awaiting_question" else None
            timeline_kinds = []
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                timeline_kinds = [item.get("type") for item in (run.get("timeline") or [])]
                if run["status"] == "awaiting_question":
                    question = run["question"]
                    break
                if run["status"] in {"done", "error"}:
                    self.fail(f"run finished before question: {run}")
                await asyncio.sleep(0.05)
            self.assertIsNotNone(question)
            labels = [
                str(opt.get("label") or "")
                for item in (question.get("questions") or [])
                for opt in (item.get("options") or [])
            ]
            self.assertIn("青绿模板", labels)
            self.assertIn("简约风", labels)
            self.assertNotIn("SHOULD_NOT_RUN_BEFORE_ANSWER", str(run.get("text") or ""))
            self.assertIn("text_delta", timeline_kinds)

    async def test_http_tool_preparing_emitted_before_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            os.environ["WITTY_HOME"] = tmp
            os.environ["WITTY_QUESTION_TIMEOUT_SEC"] = "8"
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [
                        tool_reply(
                            "ask_user_question",
                            {
                                "questions": [
                                    {
                                        "id": "auth",
                                        "question": "OAuth2 还是 JWT？",
                                        "options": [{"label": "OAuth2"}, {"label": "JWT"}],
                                    }
                                ]
                            },
                            call_id="q1",
                        ),
                        text_reply("采用 OAuth2"),
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
            await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "OAuth2 还是 JWT？", "approval_mode": "allow-all", "wait": False},
            )
            kinds = []
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                kinds = [item.get("type") for item in (run.get("timeline") or [])]
                if run["status"] == "awaiting_question":
                    break
                if run["status"] in {"done", "error"}:
                    self.fail(f"run finished before question: {run}")
                await asyncio.sleep(0.05)
            self.assertIn("tool_preparing", kinds)
            self.assertLess(kinds.index("tool_preparing"), kinds.index("question_required"))


if __name__ == "__main__":
    unittest.main()
