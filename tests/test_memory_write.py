"""记忆写入侧：同一件事只占一个槽，且留最新那条。

`memory_harvest._stamp` 给每条子弹盖上当天日期和 `[cite:…]`，而去重此前比的是整串，
所以**同一句话隔一天再说就是一个新字符串**，永远撞不上。实测 20 天的说话里用户只讲了
8 件事，`who` 格 12 个槽被同一件事吃掉 8 个，3 件事被按新鲜度挤出工作集——用得越久
越钝。`prefs` 更糟：它是常驻格、不衰减，攒进去的副本永远不会自己走。

这里锁住的取舍：
- 只认**整句相同**（去日期/cite/收尾标点后）。试过把「短句是长句子串」也当同一件事，
  同类 12 对里 8 对判错，而复核后一半的「错」其实是对的——字面包含分不开
  「补充说明 / 反转作废 / 碰巧撞上」，所以不做。
- 同一件事换个说法仍会占两个槽（要语义相似度，不是字符串操作）。
- 时间线例外：那里行首日期是事件日期，一句话可以挂两个日期，只能比整串。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from witty_agent.memory import (
    _bullets,
    append_unique_bullets,
    ensure_lattice,
    retrieve_hits,
    topic_body,
)
from witty_agent.memory_config import load_memory_settings
from witty_agent.memory_prefs import upsert_pref_bullets

ROLE = "用户是地区调度中心的自动化专责。"
ONCE = (
    "点表台账放在共享盘 //nas/dispatch/points/ 下面。",
    "运维班的联系人是老陈。",
    "五防校验失败通常是遥信双位置不一致。",
)


class _Dir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "mem"
        self.directory.mkdir(parents=True)
        ensure_lattice(self.directory)
        self.settings = load_memory_settings()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _say(self, slug: str, day: int, text: str) -> int:
        return append_unique_bullets(
            self.directory,
            slug,
            description=slug,
            lines=[f"2026-08-{day:02d} {text} [cite:s{day}#1]"],
        )

    def _rows(self, slug: str) -> list[str]:
        return _bullets(topic_body(self.directory, slug))


class RepeatedFactTests(_Dir):
    def test_restated_fact_keeps_one_slot(self) -> None:
        for day in range(1, 15):
            self._say("who", day, ROLE)
        rows = self._rows("who")
        self.assertEqual([row for row in rows if ROLE in row].__len__(), 1, rows)

    def test_restated_fact_does_not_evict_facts_said_once(self) -> None:
        """旧实现：14 天后 who 格 11/12 条是同一件事，3 条只说过一次的只剩 1 条。"""
        for day in range(1, 15):
            self._say("who", day, ROLE)
            if day <= len(ONCE):
                self._say("who", day, ONCE[day - 1])
        rows = self._rows("who")
        for fact in ONCE:
            self.assertTrue(any(fact in row for row in rows), f"{fact} 被挤出工作集：{rows}")
        self.assertEqual(len(rows), len(ONCE) + 1, rows)

    def test_refresh_carries_the_newest_date(self) -> None:
        """日期跟着换新，否则 `_decay_score` 会把用户今天刚重申的事扣出召回。"""
        self._say("who", 1, ROLE)
        self._say("who", 9, ROLE)
        self.assertEqual([row.split(" ", 1)[0] for row in self._rows("who")], ["2026-08-09"])

    def test_refresh_moves_to_tail_so_recency_does_not_evict_it(self) -> None:
        """填到刚好满格再重申——此时它**还在**工作集里，刷新必须把它挪到队尾。

        填过头就不算：那样重申的是一条已经溢出的事实，走的是追加，原地替换也照样活。
        """
        cap = self.settings.working_set
        self._say("who", 1, ROLE)
        for index in range(cap - 1):
            self._say("who", 2, f"第 {index} 件互不相同的事，先把格子填满。")
        self.assertEqual(len(self._rows("who")), cap, "前置条件：刚好满格、且 ROLE 在队首")
        self._say("who", 3, ROLE)
        for index in range(cap - 1):
            self._say("who", 4, f"第 {index} 件后来才发生的事，继续挤。")
        self.assertTrue(any(ROLE in row for row in self._rows("who")), self._rows("who"))

    def test_restating_a_stale_fact_restores_recall(self) -> None:
        """写入侧和召回侧连起来的收益：重申能把已被衰减扣掉的事实拉回 Recalled。"""
        stale = (date.today() - timedelta(days=self.settings.retrieve_decay_days * 8)).isoformat()
        fact = "五防校验失败通常是遥信双位置不一致。"
        append_unique_bullets(self.directory, "domain", description="领域", lines=[f"{stale} {fact}"])
        query = "五防校验失败一般什么原因"
        self.assertEqual(retrieve_hits(self.directory, query, self.settings), [])
        append_unique_bullets(
            self.directory, "domain", description="领域", lines=[f"{date.today().isoformat()} {fact}"]
        )
        hits = retrieve_hits(self.directory, query, self.settings)
        self.assertTrue(any(item["slug"] == "domain" for item in hits), hits)
        self.assertEqual(len(self._rows("domain")), 1, "重申不该攒成两条")


class MergeBoundaryTests(_Dir):
    def test_different_wording_of_one_fact_still_costs_two_slots(self) -> None:
        """有意留下的缺口：字面不同就是两条，要并得靠语义相似度。"""
        self._say("who", 1, "我是地区调度中心的自动化专责。")
        self._say("who", 2, "我这边是地区调度中心的自动化专责，管远动。")
        self.assertEqual(len(self._rows("who")), 2, self._rows("who"))

    def test_containment_is_not_a_merge(self) -> None:
        """短句是长句子串也不并：「留最新那条」是方向盲的，会把更具体的那条丢掉。

        用户先说全了路径、后来又松口提一句，是最常见的形状——按包含关系并掉就等于
        用「放在共享盘」换掉「放在共享盘 //nas/dispatch/points/ 下面」。
        """
        self._say("domain", 1, "点表台账放在共享盘 //nas/dispatch/points/ 下面。")
        self._say("domain", 2, "点表台账放在共享盘。")
        rows = self._rows("domain")
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(any("//nas/dispatch/points/" in row for row in rows), rows)

    def test_added_count_reports_refresh_as_a_write(self) -> None:
        """`cells_hit` 靠返回值连共现边：重申也算这一格本轮被记下了。"""
        self.assertEqual(self._say("who", 1, ROLE), 1)
        self.assertEqual(self._say("who", 2, ROLE), 1)

    def test_byte_identical_reappend_is_a_no_op(self) -> None:
        self.assertEqual(self._say("who", 1, ROLE), 1)
        self.assertEqual(self._say("who", 1, ROLE), 0)


class ArchiveTests(_Dir):
    def test_archive_dedupes_by_fact(self) -> None:
        """同一件事可以「溢出 → 再被说到 → 再溢出」，归档不能因此攒副本。"""
        cap = self.settings.working_set
        self._say("who", 1, ROLE)
        for round_index in range(3):
            for index in range(cap):
                self._say("who", 2 + round_index, f"第 {round_index}-{index} 件填充事实。")
            self._say("who", 5 + round_index, ROLE)
        archive = _bullets(topic_body(self.directory / "archive", "who"))
        self.assertLessEqual(len([row for row in archive if ROLE in row]), 1, archive)


class TimelineTests(_Dir):
    def test_one_sentence_under_two_dates_keeps_both(self) -> None:
        """时间线的行首日期是事件日期，不是收割元数据——不能按「事」并成一条。"""
        from witty_agent.timeline import harvest_timeline, render_timeline

        harvest_timeline(
            self.directory,
            "农配网工程 2024年6月10日批复，2025-01-08 开工。",
            today=date(2026, 8, 14),
        )
        text = render_timeline(self.directory)
        self.assertIn("2024-06-10", text)
        self.assertIn("2025-01-08", text)


class PrefTests(_Dir):
    def _pref(self, day: int, text: str) -> int:
        return upsert_pref_bullets(
            self.directory,
            description="偏好",
            lines=[f"2026-08-{day:02d} {text} [cite:s{day}#1]"],
            settings=self.settings,
        )

    def test_pref_without_slot_cue_restated_daily_keeps_one_slot(self) -> None:
        """`表格一律用 Markdown` 不匹配任何 pref_slots cue（多数偏好都不匹配），
        此前只比整串 → 每天一条；而 prefs 是常驻格不衰减，副本永远不会自己走。"""
        for day in range(1, 9):
            self._pref(day, "表格一律用 Markdown。")
        self.assertEqual(len(self._rows("prefs")), 1, self._rows("prefs"))

    def test_pref_with_slot_cue_restated_daily_keeps_one_slot(self) -> None:
        for day in range(1, 9):
            self._pref(day, "我喜欢简短回复。")
        self.assertEqual(len(self._rows("prefs")), 1, self._rows("prefs"))

    def test_retract_without_slot_cue_drops_the_stored_pref(self) -> None:
        """作废句和被作废句抠出来一样（都是 `我 辣`）却曾判成两回事：作废句 slot 为空，
        而另一条分支比的是原文子串，`replace(cue, ' ')` 留的空格让它永远不匹配。"""
        self._pref(1, "我喜欢吃辣。")
        self._pref(5, "我不吃辣了。")
        self.assertEqual(self._rows("prefs"), [])

    def test_unrelated_retract_keeps_other_prefs(self) -> None:
        self._pref(1, "我喜欢吃辣。")
        self._pref(5, "我不喝咖啡了。")
        self.assertTrue(any("吃辣" in row for row in self._rows("prefs")), self._rows("prefs"))

    def test_slot_replacement_still_wins(self) -> None:
        self._pref(1, "我习惯用 vim。")
        self._pref(5, "我习惯用 vscode。")
        rows = self._rows("prefs")
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("vscode", rows[0])


if __name__ == "__main__":
    unittest.main()
