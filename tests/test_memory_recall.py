"""记忆召回打分：数命中的「词」，不数同一个词的 n-gram 碎片。

`_query_tokens` 对中文出滑窗 2/3-gram，所以一个 3 字词命中时它自己的两个 2-gram
也必然命中。旧算法给这三笔各记一次（2+1+1=4），而两个真词只有 1+1=2——于是
阈值 3 卡在中间：**一个词碰巧撞上就够格，两个实词重叠反而够不到**。实测电网调度
侧的九宫格，两个 2 字实词重叠的问句一条都召不回来。

分数量级不是自由参数：`recalled_cover_min` / `budget_hits` / `archive_min_score`
都读同一把尺子，所以权重定成「长词 3、短词 2」，让 floor=3 恰好等于
「一个长词，或两个短词」，单个短词重叠仍在门外（字面上分不开
`长篇小说` 撞 `长篇铺垫` 和真命中）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from witty_agent.memory import (
    _overlap_score,
    _query_tokens,
    ensure_lattice,
    retrieve_hits,
    write_topic,
)
from witty_agent.memory_config import load_memory_settings

# 一个调度自动化专责攒了一阵子的九宫格，句子都是能自动收割出来的那种
STORE = {
    "who": ["用户是地区调度中心的自动化专责，负责变电站远动信息核对。"],
    "prefs": ["回答先给结论再给依据，不要长篇铺垫。", "表格一律用 Markdown，不要贴截图。"],
    "constraints": [
        "生产环境的开关操作必须先问，不许自己动手。",
        "外网抓取一律不做，内网白名单以外的主机不要试。",
    ],
    "domain": [
        "220kV 城东变的远动点表在 2024 年换过一次，旧点号不能直接用。",
        "五防校验失败通常是遥信双位置不一致导致的。",
    ],
    "assets": ["调度日报的模板在 ~/work/templates/daily.docx。", "点表台账放在共享盘 //nas/dispatch/points/ 下面。"],
    "people": ["运维班的联系人是老陈，检修计划先过他。"],
    "decisions": ["已决定周报用 week-digest 技能生成，不再手写。"],
    "followups": ["下次要把城东变的点表差异导成 csv 给二次班。"],
    "goals": ["本月要把全地区变电站的远动点表核对一遍。"],
}


def _build(directory: Path) -> None:
    ensure_lattice(directory)
    for slug, bullets in STORE.items():
        write_topic(
            directory,
            slug,
            description=slug,
            body="\n".join(f"- {line}" for line in bullets),
        )


class OverlapScoreTests(unittest.TestCase):
    """打分本身：一个词的碎片不许顶过两个词。"""

    def _score(self, query: str, body: str) -> int:
        settings = load_memory_settings()
        return _overlap_score(body, _query_tokens(query, settings.stopwords))

    def test_one_word_does_not_outrank_two_words(self) -> None:
        # `变电站` 命中时 `变电` / `电站` 只是它自己的切片，不是另外两条证据。
        one = self._score("变电站巡视", "大中型基建：输变电、变电站、线路工程")
        two = self._score("周报还要手写吗", "已决定周报用 week-digest 技能生成，不再手写。")
        self.assertLessEqual(one, two, "一个 3 字词不该比两个 2 字实词更像命中")

    def test_floor_means_one_long_word_or_two_short_words(self) -> None:
        floor = load_memory_settings().retrieve_min_score
        self.assertGreaterEqual(
            self._score("变电站巡视", "输变电、变电站、线路工程"), floor, "一个长词该够格"
        )
        self.assertGreaterEqual(
            self._score("日报模板改一下", "调度日报的模板在 ~/work/templates/daily.docx。"),
            floor,
            "两个 2 字实词该够格",
        )
        self.assertLess(
            self._score("长篇小说推荐", "回答先给结论再给依据，不要长篇铺垫。"),
            floor,
            "只沾一个 2 字词，字面上和真命中分不开，必须在门外",
        )

    def test_short_latin_token_is_not_a_proper_noun(self) -> None:
        # csv / sql / api 这类 3-4 字拉丁泛标识，不能和中文专名同权。
        floor = load_memory_settings().retrieve_min_score
        self.assertLess(
            self._score("python 怎么读 csv 文件", "下次要把城东变的点表差异导成 csv 给二次班。"),
            floor,
        )


class RecallTests(unittest.TestCase):
    """端到端：该召的召回来，不该召的仍在门外。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "mem"
        self.directory.mkdir(parents=True)
        _build(self.directory)
        self.settings = load_memory_settings()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _hit(self, query: str, slug: str, needle: str) -> bool:
        return any(
            item["slug"] == slug and needle in str(item["text"])
            for item in retrieve_hits(self.directory, query, self.settings)
        )

    def test_two_short_word_overlap_is_recalled(self) -> None:
        """旧打分下这两条一律 2 分，够不到 floor 3——整类问句召不回来。"""
        self.assertTrue(self._hit("周报还要手写吗", "decisions", "week-digest"))
        self.assertTrue(self._hit("日报模板改一下", "assets", "daily.docx"))

    def test_long_word_recall_not_regressed(self) -> None:
        self.assertTrue(self._hit("城东变的点表还能用旧点号吗", "domain", "旧点号"))
        self.assertTrue(self._hit("五防校验失败一般是什么原因", "domain", "遥信双位置"))
        self.assertTrue(self._hit("检修计划找谁", "people", "老陈"))
        self.assertTrue(self._hit("台账放在哪个共享盘", "assets", "//nas/dispatch/points/"))

    def test_unrelated_turns_recall_nothing(self) -> None:
        for query in ("你好", "1+1 等于几", "讲个笑话", "帮我写个 Python 快排", "现在几点了"):
            self.assertEqual(retrieve_hits(self.directory, query, self.settings), [], query)

    def test_single_word_collision_does_not_drag_in_standing_cells(self) -> None:
        """常驻格（偏好 / 红线）不许被一个撞词拽出来——放宽到「沾一个短词」实测
        会让 `生产者消费者` 撞出「生产环境」红线，假命中 2/32 → 11/32。"""
        for query in (
            "生产者消费者模型讲一下",
            "长篇小说推荐",
            "截图工具用哪个好",
            "内网穿透工具推荐一个",
            "论文结论怎么写",
            "环境变量怎么配",
        ):
            self.assertEqual(retrieve_hits(self.directory, query, self.settings), [], query)


class StandingDecayTests(unittest.TestCase):
    """常驻格的真正职责是**不被衰减扣出召回**，不是把门槛放低。此前无测试保护。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "mem"
        self.directory.mkdir(parents=True)
        ensure_lattice(self.directory)
        self.settings = load_memory_settings()
        stale = (date.today() - timedelta(days=self.settings.retrieve_decay_days * 8)).isoformat()
        for slug in ("prefs", "domain"):
            write_topic(
                self.directory,
                slug,
                description=slug,
                body=f"- {stale} 日报模板一律用 Markdown 表格。",
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stale_preference_still_recalled(self) -> None:
        hits = retrieve_hits(self.directory, "日报模板要用什么格式", self.settings)
        self.assertTrue(any(item["slug"] == "prefs" for item in hits), hits)

    def test_stale_non_standing_bullet_still_decays_out(self) -> None:
        hits = retrieve_hits(self.directory, "日报模板要用什么格式", self.settings)
        self.assertFalse(any(item["slug"] == "domain" for item in hits), hits)


if __name__ == "__main__":
    unittest.main()
