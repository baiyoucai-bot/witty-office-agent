"""摸底：压缩 × 分叉 × 恢复 三样撞在一起时，会话记录到底还剩什么。

这组测试不主张现状是对的，它把现状**钉住**，好让 tombstone 改造有一条能证伪的基线。
压缩落盘的方式已经是「只追加 + 读时投影」：检查点那条消息之后的内容才算数，之前的原始
消息还在盘上，只是 `load_messages` 不再返回。所以「压缩会原地改写历史」这个担心是错的。

真正的窟窿在另一处——投影只有一份，而且是有损的：

* `fork_session(keep=n)` / `rollback_session(keep=n)` 的 n 数的是**折叠后**的列表，
  于是检查点之前的任何一条都不可寻址。压缩过的会话没法分叉回压缩之前。
* `rollback_session` 会整份重写文件，盘上那段原始前缀就此永久消失。分叉出去的子会话
  文件里也只有折叠后的那几条，连盘上都没有原始前缀。

下面每条测试对应上面一条事实。改造 tombstone 时它们要么继续过，要么带着理由一起改。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from witty_agent.compaction import COMPACTION_CHECKPOINT_SOURCE
from witty_agent.session_tree import fork_session, list_session_ids, read_parent, rollback_session
from witty_agent.store import append_message, load_messages, session_path, write_header
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
    """分叉是拿折叠后的视图去建新文件，所以压缩之前的历史对子会话不存在。"""

    def make_compacted(self) -> Path:
        path = self.build("parent", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("user", "new-1"), msg("assistant", "new-2")]:
            append_message(path, message)
        return path

    def test_fork_inherits_only_the_projection(self) -> None:
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root))
        self.assertEqual(texts(forked), ["summary so far", "new-1", "new-2"])

    def test_the_child_file_has_no_raw_prefix_even_on_disk(self) -> None:
        """父会话盘上还留着 old-1/old-2，子会话文件里连盘上都没有。

        这是「投影有损」最硬的一处：分叉一次，压缩前的原始记录就在这条支线上彻底没了。
        """
        self.make_compacted()
        fork_session(self.root, "parent", "child", cwd=str(self.root))
        child_rows = self.raw_message_texts(session_path(self.root, "child"))
        self.assertNotIn("old-1", child_rows)
        self.assertIn("old-1", self.raw_message_texts(session_path(self.root, "parent")))

    def test_keep_indexes_the_folded_list_not_the_file(self) -> None:
        """keep=1 留下的是折叠后的第一条（摘要），不是文件里的第一条（old-1）。"""
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root), keep=1)
        self.assertEqual(texts(forked), ["summary so far"])

    def test_cannot_fork_back_to_before_the_checkpoint(self) -> None:
        """现状记账：压缩之前的任何一条都不可寻址，keep 再大也回不去。

        tombstone 改造要动的就是这一条——要让分叉能落在压缩之前，就得让检查点变成可跨越
        的标记，而不是读取下界。
        """
        self.make_compacted()
        forked = fork_session(self.root, "parent", "child", cwd=str(self.root), keep=99)
        self.assertNotIn("old-1", texts(forked))
        self.assertEqual(len(forked), 3)

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
    """回滚是原地重写，它会把盘上那段原始前缀真删掉——压缩本身不会。"""

    def make_compacted(self) -> Path:
        path = self.build("s1", [msg("user", "old-1"), msg("assistant", "old-2")])
        for message in [checkpoint(), msg("user", "new-1"), msg("assistant", "new-2")]:
            append_message(path, message)
        return path

    def test_rollback_destroys_the_raw_prefix(self) -> None:
        path = self.make_compacted()
        self.assertIn("old-1", self.raw_message_texts(path))
        rollback_session(self.root, "s1", keep=2)
        self.assertNotIn("old-1", self.raw_message_texts(path))

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

    def test_rollback_past_the_checkpoint_cannot_reach_the_original(self) -> None:
        """keep=0 之后会话是空的，而不是回到压缩前的 old-1/old-2。

        也就是说压缩之后，回滚只能在摘要之后的那一小段里挑位置。这条和分叉那条同源，
        改造时一起看。
        """
        path = self.make_compacted()
        self.assertEqual(rollback_session(self.root, "s1", keep=0), [])
        self.assertEqual(load_messages(path), [])


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
