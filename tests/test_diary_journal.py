"""日记：agent 工作日志、主动小结、路径归属、测试隔离。

这些都是「今天做了什么日记里没记」那次报障翻出来的：日记只收用户说的话，助手干的活
一个字不记；路径跟着进程 cwd 跑，跑测试会写进用户真实日记；时间戳切在时区偏移上。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.diary import (
    append_diary,
    clock_hhmmss,
    day_sections,
    days_needing_summary,
    diary_dir,
    harvest_diary,
    note_work,
    read_diary,
    summarize_day,
    today_excerpt,
    today_stamp,
    turn_actions,
)
from witty_agent.types import AgentMessage, ToolCallBlock


def _call(name: str, **args) -> ToolCallBlock:
    return ToolCallBlock(id=f"c-{name}", name=name, arguments=dict(args))


def _turn(*calls: ToolCallBlock) -> list[AgentMessage]:
    return [
        AgentMessage(role="user", content="改一下"),
        AgentMessage(role="assistant", content=list(calls)),
    ]


class DiaryHomeTests(unittest.TestCase):
    """日记归 agent 的记忆目录，不归进程当前目录。"""

    def test_memory_dir_beats_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            stray = Path(tmp) / "stray"
            with patch.dict(
                os.environ,
                {"WITTY_DIARY_DIR": str(stray), "WITTY_MEMORY_USER": str(stray)},
                clear=False,
            ):
                self.assertEqual(diary_dir(home), home / "diary")
                note_work("改 a.py", memory_dir=home)
                self.assertIn("a.py", read_diary(memory_dir=home))
                # 环境变量输了：日记没落到 stray 去。
                self.assertFalse(stray.exists())

    def test_falls_back_to_environment_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "d"
            with patch.dict(os.environ, {"WITTY_DIARY_DIR": str(folder)}, clear=False):
                self.assertEqual(diary_dir(), folder)


class ClockTests(unittest.TestCase):
    def test_timestamp_is_a_time_not_a_timezone(self) -> None:
        """`iso[-8:]` 在 +08:00 下切出来是「秒+时区」，落盘全是 `55+08:00`。"""
        stamp = clock_hhmmss()
        self.assertRegex(stamp, r"^\d{2}:\d{2}:\d{2}$")
        self.assertNotIn("+", stamp)


class WorkLogTests(unittest.TestCase):
    """agent 干的活要进日记——这是「今天做了什么」查得到的前提。"""

    def test_reads_writes_and_commands_are_recorded(self) -> None:
        line = turn_actions(
            _turn(
                _call("read", path="src/witty_agent/memory.py"),
                _call("edit", path="src/witty_agent/session.py"),
                _call("bash", command="uv run pytest"),
            )
        )
        self.assertIn("memory.py", line)
        self.assertIn("session.py", line)
        self.assertIn("bash", line)

    def test_only_this_turn_counts(self) -> None:
        """整段历史都算的话，同一件事会被每轮重记一遍。"""
        messages = [
            AgentMessage(role="user", content="第一件事"),
            AgentMessage(role="assistant", content=[_call("edit", path="old.py")]),
            AgentMessage(role="user", content="第二件事"),
            AgentMessage(role="assistant", content=[_call("edit", path="new.py")]),
        ]
        line = turn_actions(messages)
        self.assertIn("new.py", line)
        self.assertNotIn("old.py", line)

    def test_pure_chat_records_nothing(self) -> None:
        self.assertEqual(turn_actions([AgentMessage(role="assistant", content="你好")]), "")

    def test_work_and_chat_land_in_separate_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            note_work("改 memory.py", memory_dir=home)
            harvest_diary("今天下午开了验收会", memory_dir=home)
            parsed = day_sections(memory_dir=home)
            self.assertTrue(any("memory.py" in row for row in parsed["work"]))
            self.assertTrue(any("验收会" in row for row in parsed["chat"]))
            self.assertFalse(any("验收会" in row for row in parsed["work"]))

    def test_same_line_is_not_stacked_up(self) -> None:
        """去重比正文，不比整行——带时间戳一起比，隔一秒的同一句就成了新条目。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            for _ in range(4):
                note_work("跑 bash×1", memory_dir=home)
            self.assertEqual(len(day_sections(memory_dir=home)["work"]), 1)

    def test_pasted_link_is_not_a_diary_entry(self) -> None:
        """原来 `_worth` 里有条 http 分支，粘个链接就整条进日记。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            harvest_diary("https://example.com/报表.xlsx", memory_dir=home)
            self.assertEqual(day_sections(memory_dir=home)["chat"], [])


class SummaryTests(unittest.TestCase):
    def test_yesterday_with_entries_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            append_diary("改 a.py", day="2020-01-01", kind="work", memory_dir=home)
            self.assertIn("2020-01-01", days_needing_summary(home))

    def test_summary_is_written_once_and_then_settles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            append_diary("改 a.py", day="2020-01-01", kind="work", memory_dir=home)
            summarize_day(
                "2020-01-01",
                memory_dir=home,
                write_fn=lambda day, work, chat: "今天改了 a.py。",
            )
            self.assertIn("今天改了 a.py。", read_diary("2020-01-01", memory_dir=home))
            # 写过就不该再排队，否则每轮都白喊一次模型。
            self.assertNotIn("2020-01-01", days_needing_summary(home))

    def test_today_waits_until_enough_piled_up(self) -> None:
        """今天还在过，攒够了才总结；不然长会话每轮都要喊一次模型。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            note_work("改 a.py", memory_dir=home)
            self.assertNotIn(today_stamp(), days_needing_summary(home, min_entries=3))
            note_work("改 b.py", memory_dir=home)
            note_work("改 c.py", memory_dir=home)
            self.assertIn(today_stamp(), days_needing_summary(home, min_entries=3))

    def test_local_fallback_when_model_is_unavailable(self) -> None:
        """内网没配 key 也得有小结，不能又是一片空白。"""

        def broken(day, work, chat):
            raise RuntimeError("no api key")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            note_work("改 a.py", memory_dir=home)
            text = summarize_day(today_stamp(), memory_dir=home, write_fn=broken)
            self.assertTrue(text)
            self.assertIn(text.splitlines()[0][:6], read_diary(memory_dir=home))

    def test_excerpt_leads_with_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            note_work("改 a.py", memory_dir=home)
            summarize_day(
                today_stamp(),
                memory_dir=home,
                write_fn=lambda day, work, chat: "主要在改记忆模块。",
            )
            excerpt = today_excerpt(memory_dir=home)
            self.assertIn("主要在改记忆模块。", excerpt)
            self.assertLess(excerpt.index("主要在改记忆模块。"), excerpt.index("a.py"))


class LegacyFormatTests(unittest.TestCase):
    def test_old_flat_list_still_reads_back(self) -> None:
        """旧文件是一条平铺列表，没有小节，不能因为换格式就读不出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user"
            folder = home / "diary"
            folder.mkdir(parents=True)
            (folder / "2020-02-02.md").write_text(
                "# 2020-02-02\n\n- 10:00:00 · chat · 开了验收会\n",
                encoding="utf-8",
            )
            parsed = day_sections("2020-02-02", memory_dir=home)
            self.assertTrue(any("验收会" in row for row in parsed["chat"]))


if __name__ == "__main__":
    unittest.main()
