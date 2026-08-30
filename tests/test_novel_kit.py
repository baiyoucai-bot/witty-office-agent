from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from witty_agent.kernel_surface import KERNEL_COMMANDS, KERNEL_TOOLS
from witty_agent.plugins.novel_kit import (
    BookPaths,
    RecordError,
    Thresholds,
    append_records,
    build_index,
    context_pack,
    coverage,
    expand,
    fold,
    load_records,
    normalize,
    run_checks,
    save_dismissal,
    story_ch,
    truncate_records,
    validate,
)
from witty_agent.plugins.novel_kit import cli
from witty_agent.runtime import clear_runtime_cache, novel_settings
from witty_agent.tools import list_tools

SEED = [
    {"type": "chapter_digest", "ch": 1, "title": "云台镇", "summary": "沈砚初到云台镇"},
    {"type": "scene", "ch": 1, "location": "云台镇口", "participants": ["沈砚", "柳沉舟"]},
    {
        "type": "character_state",
        "ch": 1,
        "who": "沈砚",
        "status": "alive",
        "location": "云台镇",
        "unknowns": ["兄长未死"],
    },
    {"type": "character_state", "ch": 1, "who": "柳沉舟", "status": "alive"},
    {"type": "thread", "ch": 1, "id": "t-yupei", "role": "setup", "due_ch": 4},
    {"type": "chapter_digest", "ch": 2, "title": "夜访", "summary": "沈砚夜探柳宅"},
    {"type": "character_state", "ch": 2, "who": "沈砚", "knows": ["兄长未死"]},
]


def _book(tmp: str, records: list[dict] | None = None, *, prose: bool = True) -> BookPaths:
    book = BookPaths.at(tmp)
    book.ensure()
    rows = SEED if records is None else records
    append_records(book.records, rows)
    if prose:
        for number in {int(item["ch"]) for item in rows}:
            book.chapter_file(number).write_text("正文\n", encoding="utf-8")
    return book


class RecordSchemaTests(unittest.TestCase):
    def test_rejects_unknown_type_and_field(self) -> None:
        self.assertTrue(validate({"type": "nope", "ch": 1}))
        errors = validate({"type": "thread", "ch": 1, "id": "t", "dua_ch": 4})
        self.assertTrue(any("dua_ch" in item for item in errors))

    def test_typo_in_field_is_loud_not_silent(self) -> None:
        """字段名打错必须报错。静默丢一条记录，几百章后才会以穿帮的形式暴露。"""
        with self.assertRaises(RecordError):
            normalize({"type": "character_state", "ch": 3, "who": "沈砚", "staus": "dead"})

    def test_rejects_bad_chapter_and_choice(self) -> None:
        self.assertTrue(validate({"type": "thread", "ch": 0, "id": "t"}))
        self.assertTrue(validate({"type": "thread", "ch": "1", "id": "t"}))
        self.assertTrue(validate({"type": "character_state", "ch": 1, "who": "甲", "status": "zombie"}))

    def test_relationship_needs_exactly_two(self) -> None:
        self.assertTrue(validate({"type": "relationship", "ch": 1, "pair": ["甲"]}))
        self.assertTrue(validate({"type": "relationship", "ch": 1, "pair": ["甲", "乙", "丙"]}))

    def test_normalize_defaults_and_sorts(self) -> None:
        item = normalize({"type": "relationship", "ch": 7, "pair": ["乙", "甲"]})
        self.assertEqual(item["valid_from"], 7)
        self.assertEqual(item["pair"], ["乙", "甲"] if "乙" < "甲" else ["甲", "乙"])
        self.assertEqual(item["pair"], sorted(item["pair"]))

    def test_scalar_list_field_is_wrapped(self) -> None:
        item = normalize({"type": "character_state", "ch": 1, "who": "甲", "knows": "某事"})
        self.assertEqual(item["knows"], ["某事"])


class RecordStoreTests(unittest.TestCase):
    def test_through_is_a_causal_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp)
            self.assertEqual(len(load_records(book.records, through=1)), 5)
            self.assertEqual(len(load_records(book.records)), 7)

    def test_truncate_drops_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp)
            removed = truncate_records(book.records, 1)
            self.assertEqual(removed, 2)
            self.assertEqual({item["ch"] for item in load_records(book.records)}, {1})

    def test_bad_line_reports_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text('{"type":"thread","ch":1,"id":"t"}\nnot json\n', encoding="utf-8")
            with self.assertRaises(RecordError) as ctx:
                load_records(path)
            self.assertIn(":2", str(ctx.exception))


class RegistryFoldTests(unittest.TestCase):
    def test_knows_records_the_chapter_it_became_known(self) -> None:
        reg = fold([normalize(item) for item in SEED])
        self.assertEqual(reg.characters["沈砚"]["knows"], {"兄长未死": 2})
        self.assertEqual(reg.characters["沈砚"]["unknowns"], {})

    def test_fold_through_hides_the_future(self) -> None:
        reg = fold([normalize(item) for item in SEED], through=1)
        self.assertEqual(reg.characters["沈砚"]["knows"], {})
        self.assertIn("兄长未死", reg.characters["沈砚"]["unknowns"])

    def test_payoff_closes_a_thread(self) -> None:
        rows = [normalize(item) for item in SEED]
        rows.append(normalize({"type": "thread", "ch": 3, "id": "t-yupei", "role": "payoff"}))
        reg = fold(rows)
        self.assertEqual(reg.threads["t-yupei"]["status"], "closed")
        self.assertEqual(reg.threads["t-yupei"]["closed_ch"], 3)
        self.assertEqual(reg.open_threads(), [])

    def test_registry_is_a_pure_derivative(self) -> None:
        """同样的记录折叠两次结果一致——registry 删了随时能重建。"""
        rows = [normalize(item) for item in SEED]
        self.assertEqual(fold(rows).to_json(), fold(list(reversed(rows))).to_json())


class ContextPackTests(unittest.TestCase):
    def test_pack_is_chapter_safe(self) -> None:
        """第 1 章的包里不能出现第 2 章才知道的事。未来泄底是长篇最贵的错。"""
        rows = [normalize(item) for item in SEED]
        early = context_pack(rows, chapter=1, query="沈砚")
        later = context_pack(rows, chapter=2, query="沈砚")
        self.assertIn("尚不知：兄长未死", early)
        self.assertNotIn("已知：兄长未死", early)
        self.assertIn("已知：兄长未死", later)

    def test_pack_respects_budget(self) -> None:
        rows = [normalize(item) for item in SEED]
        small = context_pack(rows, chapter=2, query="沈砚", budget=400)
        self.assertLessEqual(len(small), 900)
        self.assertIn("省略", small)

    def test_one_hop_pulls_in_the_counterpart(self) -> None:
        rows = [normalize(item) for item in SEED]
        rows.append(
            normalize({"type": "relationship", "ch": 1, "pair": ["沈砚", "柳沉舟"], "polarity": "hostile"})
        )
        packed = context_pack(rows, chapter=2, query="沈砚")
        self.assertIn("柳沉舟", packed)

    def test_empty_store_says_so(self) -> None:
        self.assertIn("空", context_pack([], chapter=1, query="任何"))


class ContinuityRuleTests(unittest.TestCase):
    def _findings(self, rows: list[dict], **kwargs) -> dict[str, list]:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            out: dict[str, list] = {}
            for item in run_checks(book, **kwargs):
                out.setdefault(item.rule, []).append(item)
            return out

    def test_dead_character_walking(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "甲死了"},
            {"type": "character_state", "ch": 1, "who": "甲", "status": "dead"},
            {"type": "chapter_digest", "ch": 2, "summary": "甲又出现"},
            {"type": "scene", "ch": 2, "location": "街", "participants": ["甲"]},
        ]
        found = self._findings(rows)
        self.assertIn("dead_character_active", found)
        self.assertEqual(found["dead_character_active"][0].severity, "critical")

    def test_knowledge_regression_only_fires_backwards(self) -> None:
        forward = [
            {"type": "chapter_digest", "ch": 1, "summary": "不知"},
            {"type": "character_state", "ch": 1, "who": "甲", "unknowns": ["秘密"]},
            {"type": "chapter_digest", "ch": 2, "summary": "知道了"},
            {"type": "character_state", "ch": 2, "who": "甲", "knows": ["秘密"]},
        ]
        self.assertNotIn("knowledge_regression", self._findings(forward))
        backward = forward + [
            {"type": "chapter_digest", "ch": 3, "summary": "又不知道了"},
            {"type": "character_state", "ch": 3, "who": "甲", "unknowns": ["秘密"]},
        ]
        self.assertIn("knowledge_regression", self._findings(backward))

    def test_overdue_and_dormant_threads(self) -> None:
        rows = [{"type": "chapter_digest", "ch": n, "summary": "x"} for n in range(1, 9)]
        rows.append({"type": "thread", "ch": 1, "id": "t-a", "role": "setup", "due_ch": 3})
        rows.append({"type": "thread", "ch": 1, "id": "t-b", "role": "setup"})
        found = self._findings(rows)
        self.assertEqual([item.key for item in found["overdue_thread"]], ["overdue_thread:t-a"])
        self.assertEqual([item.key for item in found["dormant_thread"]], ["dormant_thread:t-b"])

    def test_absent_character_ignores_walk_ons(self) -> None:
        rows: list[dict] = [{"type": "chapter_digest", "ch": n, "summary": "x"} for n in range(1, 12)]
        for chapter in (1, 2, 3):
            rows.append(
                {"type": "scene", "ch": chapter, "location": "街", "participants": ["主角", "路人"]}
                if chapter == 1
                else {"type": "scene", "ch": chapter, "location": "街", "participants": ["主角"]}
            )
        found = self._findings(rows)
        self.assertEqual([item.key for item in found["absent_character"]], ["absent_character:主角"])

    def test_relationship_anachronism_and_dangling(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "scene", "ch": 1, "location": "街", "participants": ["甲"]},
            {"type": "chapter_digest", "ch": 5, "summary": "x"},
            {"type": "scene", "ch": 5, "location": "街", "participants": ["乙"]},
            {"type": "relationship", "ch": 5, "pair": ["甲", "乙"], "valid_from": 2},
            {"type": "relationship", "ch": 5, "pair": ["甲", "丙"]},
        ]
        found = self._findings(rows)
        self.assertIn("relationship_anachronism", found)
        self.assertIn("dangling_relationship", found)

    def test_stalled_middle_is_measured_not_guessed(self) -> None:
        rows = [{"type": "chapter_digest", "ch": n, "summary": "什么也没发生"} for n in range(1, 5)]
        found = self._findings(rows)
        self.assertIn("stalled_run", found)
        rows.append({"type": "thread", "ch": 3, "id": "t-x", "role": "advance"})
        self.assertNotIn("stalled_run", self._findings(rows))

    def test_missing_chapter_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, SEED, prose=False)
            rules = {item.rule for item in run_checks(book)}
            self.assertIn("missing_chapter_file", rules)

    def test_thresholds_come_from_config(self) -> None:
        rows = [{"type": "chapter_digest", "ch": n, "summary": "x"} for n in range(1, 9)]
        rows.append({"type": "thread", "ch": 1, "id": "t-b", "role": "setup"})
        loose = self._findings(rows, limits=Thresholds(dormant_thread_chapters=99))
        self.assertNotIn("dormant_thread", loose)


class DismissalTests(unittest.TestCase):
    def test_dismissal_survives_rewording(self) -> None:
        """豁免绑在 key 上而不是措辞上。改几个字就复活的告警，很快会连真错一起被无视。"""
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "甲", "status": "dead"},
            {"type": "chapter_digest", "ch": 2, "summary": "x"},
            {"type": "scene", "ch": 2, "location": "街", "participants": ["甲"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            before = [item for item in run_checks(book) if item.rule == "dead_character_active"]
            self.assertTrue(before)
            save_dismissal(book.dismissals, before[0].key, "他是诈死")
            after = [item for item in run_checks(book) if item.rule == "dead_character_active"]
            self.assertEqual(after, [])


class CliTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_cache()

    def _run(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def test_gate_exit_codes(self) -> None:
        """goal 的 GateSpec 看的是退出码，所以 0/1/2 的语义必须稳。"""
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp)
            code, _ = self._run("--book", tmp, "check")
            self.assertEqual(code, cli.EXIT_OK)

            append_records(
                book.records,
                [
                    {"type": "chapter_digest", "ch": 3, "summary": "甲复活"},
                    {"type": "character_state", "ch": 3, "who": "沈砚", "status": "dead"},
                    {"type": "chapter_digest", "ch": 4, "summary": "还在走动"},
                    {"type": "scene", "ch": 4, "location": "街", "participants": ["沈砚"]},
                ],
            )
            for number in (3, 4):
                book.chapter_file(number).write_text("正文\n", encoding="utf-8")
            code, out = self._run("--book", tmp, "check")
            self.assertEqual(code, cli.EXIT_FINDINGS)
            self.assertIn("dead_character_active", out)

    def test_strict_promotes_warnings(self) -> None:
        rows = [{"type": "chapter_digest", "ch": n, "summary": "静"} for n in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            _book(tmp, rows)
            self.assertEqual(self._run("--book", tmp, "check")[0], cli.EXIT_OK)
            self.assertEqual(self._run("--book", tmp, "check", "--strict")[0], cli.EXIT_FINDINGS)

    def test_not_a_book_is_an_error_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run("--book", tmp, "check")
            self.assertEqual(code, cli.EXIT_ERROR)

    def test_json_output_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _book(tmp, SEED, prose=False)
            code, out = self._run("--book", tmp, "check", "--json")
            self.assertEqual(code, cli.EXIT_FINDINGS)
            self.assertTrue({item["rule"] for item in json.loads(out)})

    def test_truncate_then_rebuild_clears_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _book(tmp)
            self._run("--book", tmp, "truncate", "--through", "1")
            _, out = self._run("--book", tmp, "stats")
            self.assertIn("章 1", out)

    def test_registry_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp)
            self._run("--book", tmp, "rebuild")
            data = json.loads(book.registry.read_text(encoding="utf-8"))
            self.assertEqual(data["through"], 2)
            self.assertIn("沈砚", data["characters"])


class BiTemporalTests(unittest.TestCase):
    """双时间轴：`ch` 是叙述章（决定可见性），`occurred_ch` 是故事时间（决定谁覆盖谁）。"""

    FLASHBACK = [
        {"type": "chapter_digest", "ch": 1, "summary": "甲还活着"},
        {"type": "character_state", "ch": 1, "who": "甲", "status": "alive"},
        {"type": "chapter_digest", "ch": 10, "summary": "甲战死"},
        {"type": "character_state", "ch": 10, "who": "甲", "status": "dead"},
        {"type": "chapter_digest", "ch": 20, "summary": "乙回忆起当年"},
        # 第 20 章的一段倒叙，讲的是故事时间第 5 章的事
        {"type": "scene", "ch": 20, "occurred_ch": 5, "location": "旧宅", "participants": ["甲", "乙"]},
    ]

    def test_flashback_does_not_resurrect_the_dead(self) -> None:
        """第 20 章写一段他生前的回忆，不该报「死人复活」。单时间轴一定误报。"""
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, self.FLASHBACK)
            rules = {item.rule for item in run_checks(book)}
            self.assertNotIn("dead_character_active", rules)

    def test_a_real_resurrection_still_fires(self) -> None:
        """倒叙豁免不能顺手把真错也放过：故事时间在死后就还是要报。"""
        rows = self.FLASHBACK + [
            {"type": "chapter_digest", "ch": 21, "summary": "甲又出现"},
            {"type": "scene", "ch": 21, "location": "街", "participants": ["甲"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            rules = {item.rule for item in run_checks(book)}
            self.assertIn("dead_character_active", rules)

    def test_flashback_does_not_override_later_state(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "甲", "location": "京城"},
            {"type": "chapter_digest", "ch": 9, "summary": "x"},
            {"type": "character_state", "ch": 9, "who": "甲", "location": "边关"},
            {"type": "chapter_digest", "ch": 12, "summary": "回忆"},
            {"type": "character_state", "ch": 12, "occurred_ch": 2, "who": "甲", "location": "旧宅"},
        ]
        reg = fold([normalize(item) for item in rows])
        self.assertEqual(reg.characters["甲"]["location"], "边关")

    def test_visibility_still_keys_on_narration_chapter(self) -> None:
        """倒叙的故事时间再早，也要等它被写出来才看得见——否则就是提前泄底。"""
        rows = [normalize(item) for item in self.FLASHBACK]
        early = context_pack(rows, chapter=6, query="乙")
        self.assertNotIn("旧宅", early)
        later = context_pack(rows, chapter=20, query="乙")
        self.assertIn("旧宅", later)

    def test_occurred_ch_defaults_to_narration_without_being_stored(self) -> None:
        """派生值不落盘：省 18% 体积，也让 git diff 里每行只剩真正写了的东西。"""
        row = normalize({"type": "thread", "ch": 4, "id": "t"})
        self.assertNotIn("occurred_ch", row)
        self.assertEqual(story_ch(row), 4)

    def test_redundant_occurred_ch_is_dropped_on_write(self) -> None:
        self.assertNotIn("occurred_ch", normalize({"type": "thread", "ch": 4, "occurred_ch": 4, "id": "t"}))


class Bm25Tests(unittest.TestCase):
    def test_length_normalisation_beats_naive_overlap(self) -> None:
        """短而精准的记录要赢过又长又只顺带提一句的记录。IDF 简单叠加会让两者打平。"""
        rows = [
            normalize({"type": "thread", "ch": 1, "id": "t-yupei", "summary": "玉佩"}),
            normalize(
                {
                    "type": "chapter_digest",
                    "ch": 1,
                    "summary": "玉佩只被顺带提了一句，" + "此外还讲了许多别的闲事，" * 20,
                }
            ),
        ]
        ranked = build_index(rows).rank("玉佩")
        self.assertEqual(ranked[0][1]["type"], "thread")

    def test_term_frequency_saturates(self) -> None:
        """出现五次不该是出现一次的五倍分，否则关键词堆砌就能刷榜。"""
        once = normalize({"type": "world_fact", "ch": 1, "fact": "封城"})
        many = normalize({"type": "world_fact", "ch": 2, "fact": "封城封城封城封城封城"})
        index = build_index([once, many])
        scores = {item["ch"]: score for score, item in index.rank("封城")}
        self.assertLess(scores[2], scores[1] * 5)

    def test_no_query_returns_nothing(self) -> None:
        self.assertEqual(build_index([normalize({"type": "thread", "ch": 1, "id": "t"})]).rank(""), [])


class ExpandTests(unittest.TestCase):
    ROWS = [
        {"type": "chapter_digest", "ch": 1, "summary": "x"},
        {"type": "scene", "ch": 1, "location": "街", "participants": ["甲", "乙", "丙"]},
        {"type": "relationship", "ch": 1, "pair": ["甲", "乙"], "polarity": "ally"},
        {"type": "relationship", "ch": 1, "pair": ["乙", "丙"], "polarity": "hostile"},
    ]

    def _reg(self):
        return fold([normalize(item) for item in self.ROWS])

    def test_one_hop_stops_at_direct_neighbours(self) -> None:
        _, added = expand(self._reg(), ["甲"], hops=1)
        self.assertEqual(added, ["乙"])

    def test_two_hops_reach_the_far_side(self) -> None:
        _, added = expand(self._reg(), ["甲"], hops=2)
        self.assertEqual(added, ["乙", "丙"])

    def test_zero_hops_pulls_nothing(self) -> None:
        edges, added = expand(self._reg(), ["甲"], hops=0)
        self.assertEqual((edges, added), ([], []))


class NoiseBudgetTests(unittest.TestCase):
    def test_hostile_co_present_does_not_block_strict(self) -> None:
        """宿敌同场是「你确定要这么写吗」，不是「这里错了」。1000 章合成书上实测 78 条，
        按 warning 算会让 --strict 永远红。"""
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "scene", "ch": 1, "location": "街", "participants": ["甲", "乙"]},
            {"type": "relationship", "ch": 1, "pair": ["甲", "乙"], "polarity": "hostile"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            hits = [item for item in run_checks(book) if item.rule == "hostile_co_present"]
            self.assertTrue(hits)
            self.assertEqual(hits[0].severity, "info")
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                code = cli.main(["--book", tmp, "check", "--strict"])
            self.assertEqual(code, cli.EXIT_OK)


class ScaleTests(unittest.TestCase):
    """算法复杂度的哨兵。界限放得很宽，只为了挡住不小心写出 O(n²) 的改动。"""

    CHAPTERS = 200

    def _rows(self) -> list[dict]:
        rows: list[dict] = []
        cast = [f"角色{index:02d}" for index in range(40)]
        for chapter in range(1, self.CHAPTERS + 1):
            party = [cast[(chapter + offset) % len(cast)] for offset in range(4)]
            rows.append({"type": "chapter_digest", "ch": chapter, "summary": f"第{chapter}章的事"})
            rows.append({"type": "scene", "ch": chapter, "location": "街", "participants": party})
            for who in party:
                rows.append({"type": "character_state", "ch": chapter, "who": who, "location": "街"})
            rows.append({"type": "thread", "ch": chapter, "id": f"t-{chapter}", "role": "setup"})
        return rows

    def test_whole_book_operations_stay_fast(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, self._rows(), prose=False)
            start = time.perf_counter()
            records = load_records(book.records)
            fold(records)
            context_pack(records, chapter=self.CHAPTERS, query="角色07")
            run_checks(book, records=records)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 5.0, f"200 章全套耗时 {elapsed:.2f}s，疑似复杂度回退")

    def test_state_stays_small(self) -> None:
        """状态库要一直小到能整个读进内存——这是「不上外部数据库」的前提。"""
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, self._rows(), prose=False)
            per_chapter = book.records.stat().st_size / self.CHAPTERS
            self.assertLess(per_chapter, 2048, "每章状态超过 2KB，百万字规模要重新评估存储")


class HollowGateTests(unittest.TestCase):
    """「查过了，干净」和「压根没东西可查」必须能区分开。

    改这条之前：三章正文 + 空状态库，`check --strict` 直接绿灯退出 0。
    goal 模式下客观门只看退出码，于是整套质量系统一次没跑，循环还以为一切正常。
    """

    def _hollow_book(self, tmp: str, chapters: int = 3) -> BookPaths:
        book = BookPaths.at(tmp)
        book.ensure()
        for number in range(1, chapters + 1):
            book.chapter_file(number).write_text("正文若干\n", encoding="utf-8")
        return book

    def test_prose_without_records_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._hollow_book(tmp)
            for argv in (["check"], ["check", "--strict"]):
                with self.subTest(argv=argv):
                    buffer = io.StringIO()
                    with redirect_stdout(buffer), redirect_stderr(buffer):
                        code = cli.main(["--book", tmp, *argv])
                    self.assertEqual(code, cli.EXIT_FINDINGS)

    def test_每章都点名未入库(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = self._hollow_book(tmp)
            hits = [item for item in run_checks(book) if item.rule == "unindexed_chapter"]
            self.assertEqual([item.chapter for item in hits], [1, 2, 3])

    def test_empty_book_says_not_evaluated_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            BookPaths.at(tmp).ensure()
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                code = cli.main(["--book", tmp, "check"])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertIn("没有执行", buffer.getvalue())
            self.assertNotIn("校验通过", buffer.getvalue())

    def test_indexed_book_still_reports_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _book(tmp)
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                cli.main(["--book", tmp, "check"])
            self.assertIn("通过", buffer.getvalue())

    def test_coverage_counts_the_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp)
            book.chapter_file(99).write_text("孤儿章\n", encoding="utf-8")
            seen = coverage(book, load_records(book.records))
            self.assertTrue(seen.evaluated)
            self.assertEqual(seen.gap, 1)


class AliasTests(unittest.TestCase):
    """中文长篇躲不开称谓：本名、字、号、绰号、尊称，同一个人五六种叫法。

    不归一的话「沈砚」和「沈公子」在库里就是两个人，而且**不报错**，
    只是把知道的事、出场次数各记一半，后面所有规则一起失准。
    """

    ROWS = [
        {"type": "chapter_digest", "ch": 1, "summary": "x"},
        {
            "type": "character_state",
            "ch": 1,
            "who": "沈砚",
            "aka": ["沈公子", "砚哥儿"],
            "knows": ["兄长未死"],
        },
        {"type": "chapter_digest", "ch": 2, "summary": "x"},
        {"type": "scene", "ch": 2, "location": "柳宅", "participants": ["沈公子", "柳沉舟"]},
        {"type": "character_state", "ch": 2, "who": "柳沉舟"},
    ]

    def test_aliases_fold_into_one_character(self) -> None:
        reg = fold([normalize(item) for item in self.ROWS])
        self.assertNotIn("沈公子", reg.characters)
        self.assertIn("兄长未死", reg.characters["沈砚"]["knows"])
        self.assertEqual(reg.characters["沈砚"]["last_ch"], 2)

    def test_alias_declared_later_still_unifies_earlier_chapters(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "scene", "ch": 1, "location": "街", "participants": ["沈公子"]},
            {"type": "chapter_digest", "ch": 50, "summary": "揭破身份"},
            {"type": "character_state", "ch": 50, "who": "沈砚", "aka": ["沈公子"]},
        ]
        reg = fold([normalize(item) for item in rows])
        self.assertNotIn("沈公子", reg.characters)
        self.assertEqual(reg.characters["沈砚"]["first_ch"], 1)

    def test_alias_chains_resolve_to_one_head(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "乙", "aka": ["丙"]},
            {"type": "character_state", "ch": 2, "who": "甲", "aka": ["乙"]},
            {"type": "chapter_digest", "ch": 2, "summary": "x"},
            {"type": "scene", "ch": 2, "location": "街", "participants": ["丙"]},
        ]
        reg = fold([normalize(item) for item in rows])
        self.assertEqual(sorted(reg.characters), ["甲"])

    def test_alias_pointing_at_two_people_is_critical(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "沈砚", "aka": ["公子"]},
            {"type": "character_state", "ch": 2, "who": "柳沉舟", "aka": ["公子"]},
            {"type": "chapter_digest", "ch": 2, "summary": "x"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            hits = [item for item in run_checks(book) if item.rule == "alias_collision"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].severity, "critical")

    def test_aka_cannot_contain_who(self) -> None:
        with self.assertRaises(RecordError):
            normalize({"type": "character_state", "ch": 1, "who": "沈砚", "aka": ["沈砚"]})

    def test_relationships_and_owners_are_canonicalised_too(self) -> None:
        """归一漏掉任何一个人名字段，都等于状态照样分裂。"""
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "沈砚", "aka": ["沈公子"]},
            {"type": "relationship", "ch": 1, "pair": ["沈公子", "柳沉舟"], "polarity": "ally"},
            {"type": "object_state", "ch": 1, "object": "玉佩", "owner": "沈公子"},
        ]
        reg = fold([normalize(item) for item in rows])
        self.assertEqual([sorted(key) for key in reg.relationships], [sorted(["沈砚", "柳沉舟"])])
        self.assertEqual(reg.objects["玉佩"]["owner"], "沈砚")


class TraitTests(unittest.TestCase):
    """外貌前后不一是 ConStory-Bench 的主导失败模式之一，原来我连字段都没有。"""

    def _rows(self, second: str) -> list[dict]:
        return [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "沈砚", "traits": {"眼睛": "褐色"}},
            {"type": "chapter_digest", "ch": 12, "summary": "x"},
            {"type": "character_state", "ch": 12, "who": "沈砚", "traits": {"眼睛": second}},
        ]

    def test_changed_trait_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, self._rows("灰色"))
            hits = [item for item in run_checks(book) if item.rule == "trait_contradiction"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].chapter, 12)
            self.assertIn("褐色", hits[0].message)

    def test_repeating_the_same_trait_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, self._rows("褐色"))
            self.assertEqual([item for item in run_checks(book) if item.rule == "trait_contradiction"], [])

    def test_intentional_change_can_be_dismissed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, self._rows("灰色"))
            save_dismissal(book.dismissals, "trait_contradiction:沈砚:眼睛", "换瞳术，正文有交代")
            self.assertEqual([item for item in run_checks(book) if item.rule == "trait_contradiction"], [])

    def test_traits_reach_the_context_pack(self) -> None:
        rows = [normalize(item) for item in self._rows("褐色")]
        self.assertIn("眼睛=褐色", context_pack(rows, chapter=12, query="沈砚"))

    def test_objects_get_the_same_check(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "object_state", "ch": 1, "object": "玉佩", "traits": {"材质": "羊脂玉"}},
            {"type": "chapter_digest", "ch": 9, "summary": "x"},
            {"type": "object_state", "ch": 9, "object": "玉佩", "traits": {"材质": "青玉"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            self.assertTrue([item for item in run_checks(book) if item.rule == "trait_contradiction"])

    def test_traits_must_be_a_mapping(self) -> None:
        with self.assertRaises(RecordError):
            normalize({"type": "character_state", "ch": 1, "who": "沈砚", "traits": ["褐色"]})


class UndeclaredAndPovTests(unittest.TestCase):
    def test_typo_in_a_name_is_caught(self) -> None:
        """名字打错一个字，现在会静默多出一个角色——库不报错，只是给出错的答案。"""
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "柳沉舟"},
            {"type": "scene", "ch": 1, "location": "街", "participants": ["柳沉舟"]},
            {"type": "chapter_digest", "ch": 2, "summary": "x"},
            {"type": "scene", "ch": 2, "location": "街", "participants": ["柳沈舟"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            hits = [item for item in run_checks(book) if item.rule == "undeclared_character"]
            self.assertEqual([item.chapter for item in hits], [2])
            self.assertIn("柳沈舟", hits[0].message)

    def test_declared_alias_is_not_reported_as_undeclared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, AliasTests.ROWS)
            self.assertEqual([item for item in run_checks(book) if item.rule == "undeclared_character"], [])

    def test_pov_character_must_be_in_the_scene(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "沈砚"},
            {"type": "character_state", "ch": 1, "who": "柳沉舟"},
            {"type": "scene", "ch": 1, "location": "街", "participants": ["柳沉舟"], "pov": "沈砚"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            hits = [item for item in run_checks(book) if item.rule == "pov_not_present"]
            self.assertEqual(len(hits), 1)

    def test_pov_inside_the_scene_is_fine(self) -> None:
        rows = [
            {"type": "chapter_digest", "ch": 1, "summary": "x"},
            {"type": "character_state", "ch": 1, "who": "沈砚"},
            {"type": "scene", "ch": 1, "location": "街", "participants": ["沈砚"], "pov": "沈砚"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            book = _book(tmp, rows)
            self.assertEqual([item for item in run_checks(book) if item.rule == "pov_not_present"], [])


class SurfaceTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_cache()

    def test_library_registers_no_tools_and_no_kernel_names(self) -> None:
        """M0 是库层：不注册工具，更不许占内核名或内核命令名。"""
        names = {item.name for item in list_tools()}
        self.assertFalse({name for name in names if name.startswith("novel_")})
        self.assertNotIn("novel", KERNEL_COMMANDS)
        self.assertFalse({name for name in KERNEL_TOOLS if name.startswith("novel_")})

    def test_settings_defaults_match_shipped_config(self) -> None:
        settings = novel_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(
            Thresholds.from_settings(settings),
            Thresholds(
                dormant_thread_chapters=3,
                absent_character_chapters=5,
                main_character_min_appearances=3,
                stalled_run_chapters=3,
            ),
        )


if __name__ == "__main__":
    unittest.main()
