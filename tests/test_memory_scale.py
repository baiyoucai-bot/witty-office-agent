"""记忆的规模行为：召回随语料变准，条目越界有去处，空目录不留。

这一组盯的是「记忆越攒越多」之后会不会退化：
  * 召回：常见词随语料变多自动降权，稀有专名反而召得回（`_distinctive` / `_idf_factor`）
  * 索引：内容变了当轮就得重建，不能因为指纹精度不够读到旧内容
  * 越界：归档满了不许静默丢，进 retired/
  * 巩固：碎片合并前先留底
  * 空壳：一次性 cwd 留下的空工作区目录要清，有内容的一律不动
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from witty_agent.memory import (
    RETIRED_DIR,
    append_unique_bullets,
    clear_memory_index_cache,
    ensure_lattice,
    gc_workspace_memory,
    memory_budget,
    retrieve_hits,
    topic_body,
    workspace_has_content,
    write_topic,
)
from witty_agent.memory_config import load_memory_settings
from witty_agent.memory_consolidate import consolidate
from witty_agent.memory_harvest import harvest_user_text, judge_pending_leftover


def _mem() -> Path:
    directory = Path(tempfile.mkdtemp()) / "mem"
    directory.mkdir(parents=True)
    ensure_lattice(directory)
    return directory


class RareTermRecallTests(unittest.TestCase):
    """中文实词多是两个字，一律 2 分够不到门槛 3——精确报出一个专名也召不回。

    分开 `老王`（专名）和 `回复`（常见词）的是 df，不是词长；而 df 要有统计意义得先
    有语料。所以语料小的时候照旧一起挡，语料大了才放专名过去。
    """

    def setUp(self) -> None:
        clear_memory_index_cache()
        self.settings = load_memory_settings()

    def _corpus(self, filler: int) -> Path:
        directory = _mem()
        rows = [f"这次回复内容需要整理一下第{index}版" for index in range(filler)]
        write_topic(
            directory,
            "domain",
            description="领域要点",
            body="\n".join(f"- {line}" for line in [*rows, "老王负责城东变的远动点表"]),
        )
        return directory

    def test_small_corpus_keeps_the_strict_rule(self) -> None:
        directory = self._corpus(3)
        self.assertEqual(retrieve_hits(directory, "老王", self.settings), [])

    def test_large_corpus_recalls_a_rare_proper_noun(self) -> None:
        directory = self._corpus(self.settings.retrieve_rare_corpus_min + 5)
        hits = retrieve_hits(directory, "老王管什么", self.settings)
        self.assertTrue(any(item["slug"] == "domain" for item in hits), hits)

    def test_common_word_stays_out_however_big_the_corpus(self) -> None:
        directory = self._corpus(self.settings.retrieve_rare_corpus_min + 5)
        for query in ("回复", "内容", "整理"):
            self.assertEqual(retrieve_hits(directory, query, self.settings), [], query)


class IndexFreshnessTests(unittest.TestCase):
    """收割是「写完立刻检索」的节奏，指纹精度不够就会读到旧内容。"""

    def setUp(self) -> None:
        clear_memory_index_cache()
        self.settings = load_memory_settings()

    def test_same_length_rewrite_is_picked_up(self) -> None:
        directory = _mem()
        write_topic(directory, "domain", description="领域要点", body="- 城东变的点表是甲版")
        self.assertTrue(retrieve_hits(directory, "城东变的点表", self.settings))
        # 等长改写：文件大小不变，浮点秒截到毫秒也可能不变，只有 ns 级 mtime 认得出。
        write_topic(directory, "domain", description="领域要点", body="- 城南变的点表是乙版")
        hits = retrieve_hits(directory, "城南变的点表", self.settings)
        self.assertTrue(hits, hits)
        self.assertIn("城南变", str(hits[0]["text"]))


class RetireTests(unittest.TestCase):
    """归档满了以前是 `existing[-cap:]`，多出来的直接消失。记忆是用户数据。"""

    def test_archive_overflow_lands_in_retired(self) -> None:
        directory = _mem()
        settings = load_memory_settings()
        overflow = settings.working_set + settings.archive_cap + 5
        append_unique_bullets(
            directory,
            "domain",
            description="领域要点",
            lines=[f"第{index}条互不相同的领域事实编号{index}" for index in range(overflow)],
        )
        retired = topic_body(directory / RETIRED_DIR, "domain")
        self.assertTrue(retired.strip(), "溢出的条目必须留在 retired/，不能静默消失")

    def test_budget_counts_every_layer(self) -> None:
        directory = _mem()
        append_unique_bullets(
            directory, "domain", description="领域要点", lines=["城东变的点表换过一次"]
        )
        budget = memory_budget(directory)
        self.assertEqual(int(budget["total"]), 1)
        self.assertFalse(budget["over_budget"])


class ConsolidateTests(unittest.TestCase):
    """巩固是有损的，所以合并前必须留底，形状不对宁可不合。"""

    def setUp(self) -> None:
        self.directory = _mem()
        append_unique_bullets(
            self.directory,
            "assets",
            description="项目与资产",
            lines=[
                "2026-08-01 点表在共享盘",
                "2026-08-02 点表台账放 //nas/dispatch/points/ 下面",
                "2026-08-03 日报模板在 ~/work/templates/daily.docx",
            ],
        )

    def test_merge_keeps_a_copy_in_retired(self) -> None:
        def merge(lines, _slug, _settings):
            return [
                "2026-08-02 点表台账放 //nas/dispatch/points/ 下面",
                "2026-08-03 日报模板在 ~/work/templates/daily.docx",
            ]

        report = consolidate(self.directory, ["assets"], merge_fn=merge)
        self.assertEqual(report["cells"], ["assets"])
        self.assertEqual(int(report["removed"]), 1)
        self.assertIn("共享盘", topic_body(self.directory / RETIRED_DIR, "assets"))
        self.assertNotIn("共享盘", topic_body(self.directory, "assets"))

    def test_rubbish_merge_is_refused(self) -> None:
        before = topic_body(self.directory, "assets")
        for bad in ([], ["a", "b", "c", "d", "e"], "not a list"):
            report = consolidate(self.directory, ["assets"], merge_fn=lambda *_a: bad)
            self.assertEqual(report["cells"], [], bad)
        self.assertEqual(topic_body(self.directory, "assets"), before)

    def test_merge_failure_leaves_the_cell_alone(self) -> None:
        before = topic_body(self.directory, "assets")

        def boom(*_args):
            raise RuntimeError("端点挂了")

        self.assertEqual(consolidate(self.directory, ["assets"], merge_fn=boom)["cells"], [])
        self.assertEqual(topic_body(self.directory, "assets"), before)


class DeferredJudgeTests(unittest.TestCase):
    """判官是收割路上唯一的网络调用，不能跑在每轮的关键路径上。"""

    def test_deferred_leftover_is_handed_back_not_dropped(self) -> None:
        directory = _mem()
        text = "遥信抖动一般是接点接触不良。"
        report = harvest_user_text(directory, text, defer_judge=True)
        # 这台机器没有端点，判官跑不起来 → 仍旧当场走结构判据，不推迟。
        pending = list(report.get("pending_judge") or [])
        if pending:
            self.assertNotIn("接点接触不良", topic_body(directory, "domain"))
            judge_pending_leftover(
                directory, pending, text, settings=load_memory_settings()
            )
        else:
            self.assertIn("接点接触不良", topic_body(directory, "domain"))

    def test_defer_never_double_writes(self) -> None:
        directory = _mem()
        text = "点表台账放在 //nas/dispatch/points/ 下面。"
        harvest_user_text(directory, text, defer_judge=True)
        body = topic_body(directory, "assets")
        self.assertEqual(body.count("//nas/dispatch/points/"), 1, body)


class WorkspaceGcTests(unittest.TestCase):
    """一次性 cwd 留下的空壳只增不减；有内容的目录一律不动。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "memory"
        self.root.mkdir(parents=True)
        (self.root / "user").mkdir()

    def _workspace(self, name: str, *, cwd: Path | None, bullets: list[str]) -> Path:
        directory = self.root / name
        directory.mkdir()
        (directory / "MEMORY.md").write_text("", encoding="utf-8")
        if cwd is not None:
            (directory / ".workspace").write_text(f"{cwd}\n", encoding="utf-8")
        if bullets:
            write_topic(
                directory,
                "decisions",
                description="已做决定",
                body="\n".join(f"- {line}" for line in bullets),
            )
        return directory

    def test_empty_dir_with_a_dead_cwd_is_swept(self) -> None:
        gone = Path(tempfile.mkdtemp()) / "already-removed"
        self._workspace("tmp-dead", cwd=gone, bullets=[])
        self.assertEqual(gc_workspace_memory(self.root), ["tmp-dead"])
        self.assertFalse((self.root / "tmp-dead").exists())

    def test_dir_with_memories_survives_a_dead_cwd(self) -> None:
        gone = Path(tempfile.mkdtemp()) / "already-removed"
        self._workspace("live-notes", cwd=gone, bullets=["就按 OAuth2 定"])
        self.assertEqual(gc_workspace_memory(self.root), [])
        self.assertTrue((self.root / "live-notes").exists())

    def test_live_cwd_and_fresh_dir_survives(self) -> None:
        here = Path(tempfile.mkdtemp())
        self._workspace("fresh", cwd=here, bullets=[])
        self.assertEqual(gc_workspace_memory(self.root), [])

    def test_current_workspace_is_never_swept(self) -> None:
        gone = Path(tempfile.mkdtemp()) / "already-removed"
        self._workspace("mine", cwd=gone, bullets=[])
        self.assertEqual(gc_workspace_memory(self.root, keep="mine"), [])
        self.assertTrue((self.root / "mine").exists())

    def test_user_lattice_is_never_swept(self) -> None:
        gc_workspace_memory(self.root)
        self.assertTrue((self.root / "user").exists())

    def test_content_probe_ignores_the_scaffold(self) -> None:
        empty = self._workspace("scaffold", cwd=None, bullets=[])
        self.assertFalse(workspace_has_content(empty))
        filled = self._workspace("filled", cwd=None, bullets=["记一笔"])
        self.assertTrue(workspace_has_content(filled))


class RecallCacheTests(unittest.TestCase):
    """召回按话题缓存，可缓存只在检索时写、从来没人作废——刚记的东西整段对话召不回。"""

    def test_harvest_clears_the_cached_hits(self) -> None:
        from witty_agent.session import Session

        session = Session.__new__(Session)
        session._last_memory_query = "配网台账"
        session._last_memory_hits = ({"slug": "domain", "text": "旧的"},)
        session._last_memory_retrieved = "旧的"
        session._invalidate_recall()
        self.assertEqual(session._last_memory_query, "")
        self.assertEqual(session._last_memory_hits, ())
        self.assertEqual(session._last_memory_retrieved, "")


class DecayStillAppliesTests(unittest.TestCase):
    """IDF 只管词的稀有度，不许把时间衰减顶掉。"""

    def test_stale_domain_bullet_still_decays_out(self) -> None:
        clear_memory_index_cache()
        settings = load_memory_settings()
        directory = _mem()
        stale = (date.today() - timedelta(days=settings.retrieve_decay_days * 10)).isoformat()
        write_topic(
            directory,
            "domain",
            description="领域要点",
            body=f"- {stale} 城东变的远动点表在 2024 年换过一次",
        )
        hits = retrieve_hits(directory, "城东变的远动点表还能用吗", settings)
        self.assertEqual(hits, [], hits)


if __name__ == "__main__":
    unittest.main()
