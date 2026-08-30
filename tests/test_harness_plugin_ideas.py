from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.compaction import CompactionSettings, compact_now, triage_messages
from witty_agent.focus_board import (
    archive_focus,
    load_focus,
    missing_anchors,
    parse_focus,
    render_focus,
    save_focus,
    save_focus_text,
)
from witty_agent.handoff_note import fold_handoff, git_branch, handoff_notice, load_handoff
from witty_agent.memory import budget_hits, cite_tag, topic_switched
from witty_agent.negative_ledger import gate_attempt, record_failure
from witty_agent.prompts import get_prompt
from witty_agent.spill import apply_spill, resolve_spill, spill_locator
from witty_agent.types import AgentMessage, ToolCallBlock


def _call(name: str, **arguments: object) -> ToolCallBlock:
    return ToolCallBlock(id="c1", name=name, arguments=arguments)


class FocusBoardTests(unittest.TestCase):
    def test_roundtrip_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            save_focus_text(
                directory,
                "## 目标\n修好压缩丢路径\n## 约束\n- 不改内核名\n## 决定\n- 接到 spill\n## 锚点\n- src/witty_agent/spill.py",
            )
            board = load_focus(directory)
            self.assertIn("压缩", board.objective)
            self.assertEqual(board.constraints, ["不改内核名"])
            self.assertIn("src/witty_agent/spill.py", board.anchors)
            text = render_focus(board)
            self.assertIn("工作板", text)
            archived = archive_focus(directory)
            self.assertIsNotNone(archived)
            self.assertTrue(load_focus(directory).empty())

    def test_missing_anchors(self) -> None:
        board = parse_focus("## 锚点\n- secret-path.py\n- 保留")
        missed = missing_anchors("摘要里只有 保留", board)
        self.assertEqual(missed, ["secret-path.py"])


class CompactionTriageTests(unittest.TestCase):
    def test_failed_and_duplicate_are_stubbed(self) -> None:
        failed = AgentMessage(role="toolResult", content="exit=1\nbad", tool_name="bash", is_error=False)
        ok = AgentMessage(role="toolResult", content="hello-output", tool_name="read")
        dup = AgentMessage(role="toolResult", content="hello-output", tool_name="read")
        out = triage_messages([failed, ok, dup])
        self.assertIn("失败", out[0].text())
        self.assertEqual(out[1].text(), "hello-output")
        self.assertIn("重复", out[2].text())

    def test_summary_keeps_commands(self) -> None:
        messages = [
            AgentMessage(
                role="assistant",
                content=[_call("bash", command="python -m compileall src")],
            ),
            AgentMessage(role="toolResult", content="exit=1\nboom", tool_name="bash", is_error=True),
            AgentMessage(role="user", content="x" * 80),
        ]
        compacted = compact_now(messages, CompactionSettings(keep_recent_tokens=1, use_model=False))
        self.assertIsNotNone(compacted)
        text = compacted[0].text()
        self.assertIn("python -m compileall", text)


class SpillLocatorTests(unittest.TestCase):
    def test_spill_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            spilled = apply_spill(
                AgentMessage(role="toolResult", content="Z" * 5000, tool_name="bash"),
                _call("bash", command="echo"),
                scratchpad=directory,
                session_id="sid1",
                max_inline_bytes=800,
            )
            locator = spill_locator("sid1", "c1")
            self.assertIn(locator, spilled.text())
            body = resolve_spill(directory, locator)
            self.assertIsNotNone(body)
            self.assertTrue(body.startswith("Z"))


class NegativeLedgerTests(unittest.TestCase):
    def test_blocks_same_failed_path_until_evidence_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            missing = workspace / "gone.txt"
            memory = workspace / "mem"
            memory.mkdir()
            call = _call("read", path=str(missing))
            record_failure(
                memory,
                call,
                # 用 `get_prompt` 而不是手写「文件不存在」：账本按**工具真会抛的那条文案**
                # 认失败，手写的字骗得过判据，于是缺陷被藏住了（改前实测真文案只挡住 2/8）。
                AgentMessage(
                    role="toolResult",
                    content=get_prompt("read_not_found", path="gone.txt"),
                    is_error=True,
                    tool_name="read",
                ),
                workspace=workspace,
            )
            blocked = gate_attempt(memory, call, workspace=workspace)
            self.assertIsNotNone(blocked)
            self.assertTrue(blocked.is_error)
            missing.write_text("now", encoding="utf-8")
            self.assertIsNone(gate_attempt(memory, call, workspace=workspace))

    def test_bash_and_grep_misses_are_not_ledgered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            memory = workspace / "mem"
            memory.mkdir()
            bash = _call("bash", command="pytest")
            record_failure(
                memory,
                bash,
                AgentMessage(role="toolResult", content="exit=1\nfailed", tool_name="bash"),
                workspace=workspace,
            )
            self.assertIsNone(gate_attempt(memory, bash, workspace=workspace))
            grep = _call("grep", pattern="not found", path=".")
            record_failure(
                memory,
                grep,
                AgentMessage(role="toolResult", content="src/a.py:1:not found", tool_name="grep"),
                workspace=workspace,
            )
            self.assertIsNone(gate_attempt(memory, grep, workspace=workspace))
            denied = _call("write", path=str(workspace / "gone.txt"))
            record_failure(
                memory,
                denied,
                AgentMessage(
                    role="toolResult",
                    content="用户拒绝了危险工具 write。",
                    is_error=True,
                    tool_name="write",
                ),
                workspace=workspace,
            )
            self.assertIsNone(gate_attempt(memory, denied, workspace=workspace))


class HandoffTests(unittest.TestCase):
    def test_fold_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            cwd = Path(raw)
            fold_handoff(directory, cwd, user_text="继续做交接", assistant_text="已记下分支")
            body = load_handoff(directory, cwd)
            self.assertIn("继续做交接", body)
            self.assertTrue(git_branch(cwd))
            self.assertIn("不是当前权限", handoff_notice(directory, cwd))
            fold_handoff(
                directory,
                cwd,
                user_text="给我升级一下本机那个编辑器",
                assistant_text="当前沙箱策略（workspace-write）把我 bash 的读写范围锁死在工作区和 sandbox/",
            )
            cleaned = load_handoff(directory, cwd)
            self.assertIn("继续做交接", cleaned)
            self.assertNotIn("workspace-write", cleaned)
            self.assertNotIn("锁死在工作区", cleaned)


class MemoryBudgetTests(unittest.TestCase):
    def test_budget_caps_and_topic_switch(self) -> None:
        hits = [
            {"slug": "a", "title": "A", "text": "alpha " * 20, "score": 9},
            {"slug": "b", "title": "B", "text": "beta " * 20, "score": 8},
            {"slug": "c", "title": "C", "text": "gamma " * 20, "score": 4},
            {"slug": "d", "title": "D", "text": "delta " * 80, "score": 3},
        ]
        decided = budget_hits(hits)
        self.assertEqual(decided[0]["decision"], "use")
        self.assertIn(decided[-1]["decision"], {"ignore", "verify"})
        self.assertTrue(topic_switched("施工图在哪", "量子纠缠是什么"))
        self.assertFalse(topic_switched("施工图在哪个目录", "施工图目录在哪"))
        self.assertEqual(cite_tag("abc-1", 12), "[cite:abc-1#12]")


if __name__ == "__main__":
    unittest.main()
