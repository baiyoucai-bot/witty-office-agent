"""压缩 × 分叉 × 回滚 × 恢复：历史只追加（tombstone），投影按标记算。

压缩落盘一直是「只追加 + 读时投影」：检查点之后的内容才算数，之前的原始消息还在盘上。
tombstone 改造把分叉和回滚也拉到同一条纪律上：

* `rollback_session` 不再重写文件，而是追加一条回滚标记；`load_messages` 读时按文件顺序
  逐条应用标记再折叠。默认 `keep` 仍数折叠后的列表（和用户看到的一致），`raw=True` 数原始
  序列——能回到压缩检查点之前。
* `fork_session` 拷的是父会话的**原始序列**，`keep` 用标记表达；子会话文件里保留原始前缀。

于是「分叉回压缩之前」「回滚不丢历史」「子会话可审计」三件事都成立。原先钉住旧行为的
几条断言在这里翻转成新契约，并带着理由。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from witty_agent.compaction import COMPACTION_CHECKPOINT_SOURCE
from witty_agent.session_tree import fork_session, list_session_ids, read_parent, rollback_session
from witty_agent.store import (
    ROLLBACK_MARKER_SOURCE,
    append_message,
    load_messages,
    load_raw_messages,
    session_path,
    write_header,
)
from witty_agent.types import AgentMessage


def msg(role: str, text: str, **kwargs) -> AgentMessage:
    return AgentMessage(role=role, content=text, **kwargs)


def checkpoint(text: str = "summary so far", *, keep_before: int = 0) -> AgentMessage:
    return msg("user", text, source=COMPACTION_CHECKPOINT_SOURCE, meta={"keep_before": keep_before})


def texts(messages: list[AgentMessage]) -> list[str]:
    return [item.text() for item in messages]


class SessionFileBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def build(self, session_id: str, messages: list[AgentMessage]) -> Path:
        path = session_path(self.root, session_id)
        write_header(path, session_id, str(self.root), None)
        for message in messages:
            append_message(path, message)
        return path

    def raw_rows(self, path: Path) -> list[dict]:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def raw_message_texts(self, path: Path) -> list[str]:
        return [row.get("content", "") for row in self.raw_rows(path) if row.get("type") == "message"]


class CompactionIsAProjectionTests(SessionFileBase):
    """压缩不改写盘上的东西，它只是让读的时候从检查点起算。"""

    def test_raw_prefix_survives_on_disk(self) -> None:
        path = self.build("s1", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("assistant", "new-1")]:
            append_message(path, message)
        self.assertEqual(
            self.raw_message_texts(path),
            ["old-1", "old-2", "summary so far", "new-1"],
            "压缩是追加，不是重写：原始前缀必须还在盘上",
        )

    def test_load_starts_at_the_checkpoint(self) -> None:
        path = self.build("s1", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("assistant", "new-1")]:
            append_message(path, message)
        self.assertEqual(texts(load_messages(path)), ["summary so far", "new-1"])

    def test_keep_before_widens_the_window(self) -> None:
        """区间压缩：检查点前面可以按 meta 留几条，用来接住被摘要切断的那半个步骤。"""
        path = self.build("s1", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(keep_before=1), msg("assistant", "new-1")]:
            append_message(path, message)
        self.assertEqual(texts(load_messages(path)), ["old-2", "summary so far", "new-1"])

    def test_the_newest_checkpoint_wins(self) -> None:
        path = self.build("s1", [msg("user", "old-1")])
        for message in [checkpoint("first"), msg("assistant", "mid"), checkpoint("second")]:
            append_message(path, message)
        self.assertEqual(texts(load_messages(path)), ["second"])

    def test_a_leading_checkpoint_does_not_swallow_the_session(self) -> None:
        """检查点落在第 0 条时不折叠：否则一份只有摘要的会话读出来是空的。"""
        path = self.build("s1", [checkpoint("only"), msg("assistant", "after")])
        self.assertEqual(texts(load_messages(path)), ["only", "after"])


class ForkAfterCompactionTests(SessionFileBase):
    """分叉拷原始序列：子会话看到的投影与父一致，盘上却一条历史不少。"""

    def make_compacted(self) -> Path:
        path = self.build("parent", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("user", "new-1"), msg("assistant", "new-2")]:
            append_message(path, message)
        return path

    def test_fork_inherits_the_projection(self) -> None:
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root))
        self.assertEqual(texts(forked), ["summary so far", "new-1", "new-2"])

    def test_the_child_file_keeps_the_raw_prefix_on_disk(self) -> None:
        """翻转：子会话文件里保留 old-1/old-2。分叉不再是有损投影，支线可审计、可再回溯。"""
        self.make_compacted()
        fork_session(self.root, "parent", "child", cwd=str(self.root))
        child_rows = self.raw_message_texts(session_path(self.root, "child"))
        self.assertIn("old-1", child_rows)
        self.assertEqual(texts(load_messages(session_path(self.root, "child"))), ["summary so far", "new-1", "new-2"])

    def test_keep_indexes_the_folded_list_by_default(self) -> None:
        """keep=1 留下的是折叠后的第一条（摘要）——默认语义与用户看到的列表对齐，没变。"""
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root), keep=1)
        self.assertEqual(texts(forked), ["summary so far"])
        # 但盘上原始前缀仍在，keep 是靠标记表达的
        child_raw = load_raw_messages(session_path(self.root, "child"))
        self.assertIn("old-1", texts(child_raw))
        self.assertEqual(child_raw[-1].source, ROLLBACK_MARKER_SOURCE)

    def test_folded_keep_beyond_window_does_not_resurrect_the_prefix(self) -> None:
        """默认语义下 keep 再大也只是「全留」，不会把压缩前的 old-1 顺手捞回来。"""
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root), keep=99)
        self.assertEqual(texts(forked), ["summary so far", "new-1", "new-2"])

    def test_raw_keep_can_fork_back_to_before_the_checkpoint(self) -> None:
        """翻转：raw=True 按原始下标截，检查点之前的消息重新可寻址。"""
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root), keep=2, raw=True)
        self.assertEqual(texts(forked), ["old-1", "old-2"])
        self.assertEqual(self.raw_message_texts(session_path(self.root, "child")), ["old-1", "old-2"])

    def test_fork_records_the_parent(self) -> None:
        self.make_compacted()
        fork_session(self.root, "parent", "child", cwd=str(self.root))
        self.assertEqual(read_parent(session_path(self.root, "child")), "parent")
        self.assertEqual(list_session_ids(self.root), ["child", "parent"])

    def test_forking_a_fork_keeps_narrowing(self) -> None:
        """两次分叉叠加：每一次都以上一次的折叠视图为源，可见范围只会更窄。"""
        self.make_compacted()
        fork_session(self.root, "parent", "child", cwd=str(self.root), keep=2)
        grand = fork_session(self.root, "child", "grand", cwd=str(self.root))
        self.assertEqual(texts(grand), ["summary so far", "new-1"])


class RollbackAfterCompactionTests(SessionFileBase):
    """回滚是追加一条标记：盘上历史一条不少，投影按标记算。"""

    def make_compacted(self) -> Path:
        path = self.build("s1", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("user", "new-1"), msg("assistant", "new-2")]:
            append_message(path, message)
        return path

    def test_rollback_appends_a_marker_and_keeps_the_raw_prefix(self) -> None:
        """翻转：回滚不再重写文件。原始前缀、被回滚掉的消息都还在盘上，只是不进投影。"""
        path = self.make_compacted()
        before = self.raw_message_texts(path)
        rollback_session(self.root, "s1", keep=2)
        after = self.raw_message_texts(path)
        self.assertEqual(after[: len(before)], before)
        self.assertIn("old-1", after)
        self.assertIn("new-2", after, "被回滚掉的 new-2 仍在盘上")
        rows = self.raw_rows(path)
        self.assertEqual(rows[-1]["source"], ROLLBACK_MARKER_SOURCE)
        self.assertEqual(rows[-1]["meta"], {"keep": 2, "raw": False})

    def test_rollback_keeps_the_folded_head(self) -> None:
        path = self.make_compacted()
        kept = rollback_session(self.root, "s1", keep=2)
        self.assertEqual(texts(kept), ["summary so far", "new-1"])
        self.assertEqual(texts(load_messages(path)), ["summary so far", "new-1"])

    def test_rollback_preserves_the_parent_link(self) -> None:
        self.build("root", [msg("user", "a")])
        fork_session(self.root, "root", "s1", cwd=str(self.root))
        append_message(session_path(self.root, "s1"), msg("assistant", "b"))
        rollback_session(self.root, "s1", keep=1)
        self.assertEqual(read_parent(session_path(self.root, "s1")), "root")

    def test_folded_rollback_to_zero_is_empty_not_the_original(self) -> None:
        """默认语义 keep=0 = 空会话（与用户看到的列表一致），不会偷偷回到压缩前。"""
        path = self.make_compacted()
        self.assertEqual(rollback_session(self.root, "s1", keep=0), [])
        self.assertEqual(load_messages(path), [])
        self.assertIn("old-1", self.raw_message_texts(path), "空的只是投影，历史还在")

    def test_raw_rollback_reaches_before_the_checkpoint(self) -> None:
        """翻转：raw=True 按原始下标回滚，检查点被截掉，压缩前的 old-1/old-2 重新成为可见会话。"""
        path = self.make_compacted()
        kept = rollback_session(self.root, "s1", keep=2, raw=True)
        self.assertEqual(texts(kept), ["old-1", "old-2"])
        self.assertEqual(texts(load_messages(path)), ["old-1", "old-2"])
        self.assertIn("summary so far", self.raw_message_texts(path), "检查点仍在盘上，只是不再生效")

    def test_appending_after_rollback_continues_from_the_kept_head(self) -> None:
        path = self.make_compacted()
        rollback_session(self.root, "s1", keep=1)
        append_message(path, msg("user", "again"))
        self.assertEqual(texts(load_messages(path)), ["summary so far", "again"])

    def test_multiple_rollbacks_apply_in_file_order(self) -> None:
        """两次回滚逐条应用：第二次的 keep 数的是第一次回滚之后（含后续追加）的列表。"""
        path = self.build("s1", [msg("user", "a"), msg("assistant", "b"), msg("user", "c")])
        rollback_session(self.root, "s1", keep=2)  # [a, b]
        append_message(path, msg("assistant", "d"))  # [a, b, d]
        kept = rollback_session(self.root, "s1", keep=1)  # [a]
        self.assertEqual(texts(kept), ["a"])
        append_message(path, msg("assistant", "e"))
        self.assertEqual(texts(load_messages(path)), ["a", "e"])
        self.assertEqual(
            self.raw_message_texts(path),
            ["a", "b", "c", "", "d", "", "e"],
            "七条原始记录（含两条空正文的标记）一条都没删",
        )

    def test_fork_of_a_rolled_back_session_inherits_the_marker(self) -> None:
        path = self.make_compacted()
        rollback_session(self.root, "s1", keep=2)
        forked = fork_session(self.root, "s1", "child", cwd=str(self.root))
        self.assertEqual(texts(forked), ["summary so far", "new-1"])
        self.assertEqual(len(load_raw_messages(session_path(self.root, "child"))), len(load_raw_messages(path)))


class ResumeAfterCompactionTests(SessionFileBase):
    """恢复走的也是 `load_messages`，所以重启前后看到的窗口必须一模一样。"""

    def test_resume_matches_what_the_live_session_saw(self) -> None:
        path = self.build("s1", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("user", "new-1")]:
            append_message(path, message)
        first = texts(load_messages(path))
        self.assertEqual(first, texts(load_messages(path)))
        self.assertEqual(first, ["summary so far", "new-1"])

    def test_appending_after_resume_does_not_resurrect_the_prefix(self) -> None:
        path = self.build("s1", [msg("user", "old-1")])
        for message in [checkpoint(), msg("user", "new-1")]:
            append_message(path, message)
        append_message(path, msg("assistant", "new-2"))
        self.assertEqual(texts(load_messages(path)), ["summary so far", "new-1", "new-2"])

    def test_a_second_compaction_narrows_again(self) -> None:
        path = self.build("s1", [msg("user", "old-1")])
        for message in [checkpoint("first"), msg("user", "mid-1"), msg("assistant", "mid-2")]:
            append_message(path, message)
        self.assertEqual(len(load_messages(path)), 3)
        for message in [checkpoint("second"), msg("user", "late")]:
            append_message(path, message)
        self.assertEqual(texts(load_messages(path)), ["second", "late"])
        self.assertEqual(len(self.raw_message_texts(path)), 6, "六条原始记录一条都没删")


if __name__ == "__main__":
    unittest.main()
