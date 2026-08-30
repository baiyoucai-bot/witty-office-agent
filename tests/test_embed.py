from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.embed import Witty, result_text
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.paths import bundled_root, project_root
from witty_agent.permission import PermissionPolicy, normalize_level
from witty_agent.prompts import get_prompt
from witty_agent.tomlcompat import tomllib


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EmbedLibraryTests(unittest.IsolatedAsyncioTestCase):
    def test_tomlcompat_loads_runtime(self) -> None:
        path = project_root() / "config" / "runtime.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertIn("model", data)

    def test_project_root_finds_prompts(self) -> None:
        root = project_root()
        self.assertTrue((root / "config" / "prompts.toml").is_file())
        self.assertTrue(get_prompt("library_approval_pending"))

    def test_bundled_data_tracks_repo(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        bundled = bundled_root()
        if not (bundled / "config" / "prompts.toml").is_file():
            self.skipTest("包内 data/ 未同步，发 wheel 前跑 scripts/sync_package_data.py")
        for name in ("prompts.toml", "runtime.toml", "memory.toml"):
            self.assertEqual(
                _digest(repo / "config" / name),
                _digest(bundled / "config" / name),
                f"data/config/{name} 与仓库不同步，请跑 scripts/sync_package_data.py",
            )

    def test_permission_aliases(self) -> None:
        self.assertEqual(normalize_level("allow-all"), "allow")
        self.assertEqual(normalize_level("always-ask"), "ask")
        self.assertEqual(PermissionPolicy(level="allow").approval_mode(), "allow-all")
        self.assertEqual(PermissionPolicy(level="ask").approval_mode(), "always-ask")

    def test_workspace_defaults_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "cwd"
            workspace.mkdir()
            root = Path(tmp) / "home"
            llm = ScriptedLLM([text_reply("pong")])
            here = Path.cwd()
            try:
                os.chdir(workspace)
                witty = Witty(root=root, llm=llm, permission="allow")
            finally:
                os.chdir(here)
            self.assertEqual(witty.workspace, workspace.resolve())

    def test_api_key_can_be_passed_in_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            witty = Witty(
                workspace=tmp,
                root=Path(tmp) / "home",
                permission="allow",
                api_key="from-code",
                base_url="http://127.0.0.1:9/v1",
                model_id="demo-model",
            )
            self.assertEqual(witty.llm.api_key, "from-code")
            self.assertEqual(witty.llm.base_url, "http://127.0.0.1:9/v1")
            self.assertEqual(witty.llm.model_id, "demo-model")

    def test_library_log_default_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Witty(
                workspace=tmp,
                root=Path(tmp) / "home",
                llm=ScriptedLLM([text_reply("x")]),
                permission="allow",
            )
        self.assertEqual(logging.getLogger("witty_agent").level, logging.WARNING)

    async def test_timeout_default_allows_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            inbox = Path(tmp) / "inbox"
            llm = ScriptedLLM(
                [
                    tool_reply("write", {"path": "out.txt", "content": "ok\n"}, call_id="w1"),
                    text_reply("wrote"),
                ]
            )
            witty = Witty(
                workspace=workspace,
                root=Path(tmp) / "home",
                llm=llm,
                permission="ask",
                timeout_sec=0.05,
                on_timeout="allow",
                inbox=inbox,
            )
            result = await witty.arun("写文件")
            self.assertEqual(result_text(result), "wrote")
            self.assertEqual(result.text, "wrote")
            self.assertEqual(str(result), "wrote")
            self.assertTrue(result.ok)
            self.assertEqual(result.tools[0]["name"], "write")
            self.assertTrue(result.tools[0]["ok"])
            self.assertIn("tool_start", {item["type"] for item in result.steps})
            self.assertIn("text", {item["type"] for item in result.steps})
            self.assertEqual((workspace / "out.txt").read_text(encoding="utf-8"), "ok\n")
            pending = inbox / witty.session.session_id / "w1.json"
            self.assertTrue(pending.is_file())

    async def test_timeout_deny_blocks_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            llm = ScriptedLLM(
                [
                    tool_reply("write", {"path": "nope.txt", "content": "x\n"}, call_id="w2"),
                    text_reply("stopped"),
                ]
            )
            witty = Witty(
                workspace=workspace,
                root=Path(tmp) / "home",
                llm=llm,
                permission="ask",
                timeout_sec=0.05,
                on_timeout="deny",
                inbox=Path(tmp) / "inbox",
            )
            result = await witty.arun("写文件")
            self.assertEqual(result_text(result), "stopped")
            self.assertFalse((workspace / "nope.txt").exists())

    async def test_ask_callback_allow(self) -> None:
        seen: list[str] = []

        async def ask(name: str, call_id: str, args: dict) -> str:
            seen.append(name)
            return "allow"

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            llm = ScriptedLLM(
                [
                    tool_reply("write", {"path": "ok.txt", "content": "yes\n"}, call_id="w3"),
                    text_reply("done"),
                ]
            )
            witty = Witty(
                workspace=workspace,
                root=Path(tmp) / "home",
                llm=llm,
                permission="ask",
                timeout_sec=2,
                ask=ask,
                inbox=Path(tmp) / "inbox",
            )
            result = await witty.arun("写")
            self.assertEqual(seen, ["write"])
            self.assertEqual(result_text(result), "done")
            self.assertEqual((workspace / "ok.txt").read_text(encoding="utf-8"), "yes\n")

    async def test_reply_file_denies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            inbox = Path(tmp) / "inbox"

            async def ask(name: str, call_id: str, args: dict) -> None:
                folder = inbox / witty.session.session_id
                (folder / f"{call_id}.reply").write_text("deny\n", encoding="utf-8")

            llm = ScriptedLLM(
                [
                    tool_reply("write", {"path": "blocked.txt", "content": "no\n"}, call_id="w4"),
                    text_reply("denied"),
                ]
            )
            witty = Witty(
                workspace=workspace,
                root=Path(tmp) / "home",
                llm=llm,
                permission="ask",
                timeout_sec=2,
                ask=ask,
                inbox=inbox,
            )
            result = await witty.arun("写")
            self.assertEqual(result_text(result), "denied")
            self.assertFalse((workspace / "blocked.txt").exists())

    async def test_on_event_optional_stream(self) -> None:
        seen: list[str] = []

        def on_event(payload: dict) -> None:
            seen.append(str(payload.get("type") or ""))

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            llm = ScriptedLLM(
                [
                    tool_reply("write", {"path": "n.txt", "content": "1\n"}, call_id="w5"),
                    text_reply("done"),
                ]
            )
            witty = Witty(
                workspace=workspace,
                root=Path(tmp) / "home",
                llm=llm,
                permission="allow",
                on_event=on_event,
            )
            result = await witty.arun("写")
            self.assertEqual(result.text, "done")
            self.assertIn("tool_start", seen)
            self.assertIn("tool_end", seen)
            self.assertIn("done", seen)
