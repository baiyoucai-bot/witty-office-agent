from __future__ import annotations

import asyncio
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path

from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.plugins import file_classify as fc
from witty_agent.tools import list_tools

_UNIT_ID = re.compile(r"### unit_id: (\S+)")
_GROUP_ID = re.compile(r"### group_id: (\S+)")

TAXONOMY = [
    {"id": "C01", "name": "合同", "description": "正式签订的施工、采购、服务合同"},
    {"id": "C02", "name": "发票", "description": "增值税发票及收据"},
    {"id": "B01", "name": "投标文件", "children": [
        {"id": "B0101", "name": "投标文件-商务文件"},
        {"id": "B0102", "name": "投标文件-业绩证明"},
    ]},
    {"id": "A01", "name": "招标文件"},
]

# 路径决定用途：投标目录下的合同扫描件应落 B0102，不是 C01
TREE = {
    "某变电站工程/二标段/投标文件/业绩证明/合同扫描件.txt": "施工合同 甲方 乙方 签章",
    "某变电站工程/二标段/投标文件/业绩证明/1.txt": "业绩证明材料 第 1 页 项目概况",
    "某变电站工程/二标段/投标文件/业绩证明/2.txt": "第 2 页 承接以下工程",
    "某变电站工程/二标段/投标文件/业绩证明/3.txt": "第 3 页 累计合同额 签章",
    "某变电站工程/二标段/合同/施工总承包合同.txt": "施工总承包合同 编号 HT-2024-011",
    "某变电站工程/招标文件/合同模板.txt": "合同格式范本 由投标人填写",
}


def _build(root: Path) -> None:
    for rel, body in TREE.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


class FakeModel:
    """按提示词里的 unit_id / group_id 回填结果，模拟一个守规矩的模型。"""

    def __init__(self, *, merge: bool = True, low_confidence: set[str] | None = None, delay: float = 0.0) -> None:
        self.merge = merge
        self.low_confidence = low_confidence or set()
        self.delay = delay
        self.calls: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, system: str, user: str, **_kw: object) -> str:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._reply(system, user)
        finally:
            self.in_flight -= 1

    def _reply(self, system: str, user: str) -> str:
        if "同一目录下的一批文件" in system:
            self.calls.append("group")
            groups = _GROUP_ID.findall(user)
            return json.dumps({"groups": [
                {
                    "group_id": gid,
                    "merge": self.merge,
                    "members": [],
                    "split": [] if self.merge else [[name] for name in re.findall(r"--- (\S+) ---", user)],
                    "confidence": 0.9,
                    "reason": "页码连续",
                }
                for gid in groups
            ]}, ensure_ascii=False)
        stage_two = "正文摘录" in user
        self.calls.append("pass2" if stage_two else "pass1")
        results = []
        for uid in _UNIT_ID.findall(user):
            needs = (not stage_two) and uid in self.low_confidence
            results.append({
                "unit_id": uid,
                "category_id": "B0102" if not needs else "C01",
                "category_name": "投标文件-业绩证明" if not needs else "合同",
                "confidence": 0.3 if needs else 0.92,
                "need_content": needs,
                "evidence": ["path:投标文件"],
                "reasoning": "位于投标文件目录，用途是证明业绩",
                "group_conflict": "",
            })
        return json.dumps({"results": results}, ensure_ascii=False)


class TaxonomyTests(unittest.TestCase):
    def test_array_with_nested_children(self) -> None:
        cats = fc.normalize_taxonomy(json.dumps(TAXONOMY, ensure_ascii=False))
        ids = [cat.id for cat in cats]
        self.assertEqual(ids, ["C01", "C02", "B01", "B0101", "B0102", "A01"])
        child = [cat for cat in cats if cat.id == "B0102"][0]
        self.assertIn("投标文件", child.desc)

    def test_plain_dict_and_string_list(self) -> None:
        self.assertEqual(
            [(c.id, c.name) for c in fc.normalize_taxonomy({"C01": "合同", "C02": "发票"})],
            [("C01", "合同"), ("C02", "发票")],
        )
        self.assertEqual([c.id for c in fc.normalize_taxonomy(["合同", "发票"])], ["合同", "发票"])

    def test_alias_keys_and_wrapper(self) -> None:
        raw = {"categories": [{"编号": "X1", "名称": "签证", "说明": "现场签证单"}]}
        cats = fc.normalize_taxonomy(raw)
        self.assertEqual((cats[0].id, cats[0].name, cats[0].desc), ("X1", "签证", "现场签证单"))

    def test_bad_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            fc.normalize_taxonomy("{not json")


class GroupingTests(unittest.TestCase):
    def test_numbered_siblings_become_one_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build(root)
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            self.assertEqual(len(groups), 1)
            members = next(iter(groups.values()))
            self.assertEqual([rec.name for rec in members], ["1.txt", "2.txt", "3.txt"])
            self.assertNotIn("1.txt", [rec.name for rec in loose])

    def test_prefixed_and_scattered_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("扫描件_01.txt", "扫描件_02.txt", "报告.txt", "第1页.txt", "第2页.txt"):
                (root / name).write_text("x", encoding="utf-8")
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            self.assertEqual(len(groups), 2)
            self.assertEqual([rec.name for rec in loose], ["报告.txt"])

    def test_far_apart_numbers_are_not_grouped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("a1.txt", "a900.txt"):
                (root / name).write_text("x", encoding="utf-8")
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            self.assertEqual(groups, {})
            self.assertEqual(len(loose), 2)


class ClassifyFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_split_files_get_one_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fake = FakeModel()
            fc._ask = fake
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertEqual(summary["files"], 6)
            self.assertEqual(summary["units"], 4)
            self.assertEqual(summary["merged"], 1)
            rows = [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]
            merged = [row for row in rows if len(row["members"]) > 1]
            self.assertEqual(len(merged), 1)
            self.assertEqual(len(merged[0]["members"]), 3)
            self.assertEqual(merged[0]["category_id"], "B0102")

    def test_model_can_split_a_wrong_candidate_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel(merge=False)
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertEqual(summary["units"], 6)
            self.assertEqual(summary["merged"], 0)

    def test_low_confidence_escalates_to_second_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            target = fc._digest("u", [[r for r in loose if r.name == "合同扫描件.txt"][0].rel])
            fake = FakeModel(low_confidence={target})
            fc._ask = fake
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertIn("pass2", fake.calls)
            self.assertEqual(summary["pass2"], 1)
            rows = {r["unit_id"]: r for r in
                    [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]}
            self.assertEqual(rows[target]["stage"], "pass2")
            self.assertEqual(rows[target]["category_id"], "B0102")

    def test_resume_skips_finished_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            second = FakeModel()
            fc._ask = second
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertEqual(second.calls, [])
            self.assertEqual(summary["units"], 4)
            self.assertTrue((out / "report.md").is_file())

    def test_crash_midway_keeps_finished_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            flaky = FakeModel()
            calls = {"n": 0}

            async def blow_up_on_second_batch(system: str, user: str, **kw: object) -> str:
                if "资料分类专家" in system:
                    calls["n"] += 1
                    if calls["n"] == 2:
                        # 必须用不可重试的错误：网关 5xx / 超时会一直等资源池，不算崩
                        raise RuntimeError("401 invalid api key")
                return await flaky(system, user, **kw)

            fc._ask = blow_up_on_second_batch
            with self.assertRaises(RuntimeError):
                fc.classify_directory(root, TAXONOMY, out_dir=out, pass1_batch=2, concurrency=1)
            saved = (out / "results.jsonl").read_text("utf-8").splitlines()
            self.assertEqual(len(saved), 2)

            fc._ask = FakeModel()
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out, pass1_batch=2)
            self.assertEqual(summary["units"], 4)
            rows = [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]
            self.assertEqual(len({row["unit_id"] for row in rows}), 4)

    def test_no_resume_clears_previous_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            again = FakeModel()
            fc._ask = again
            fc.classify_directory(root, TAXONOMY, out_dir=out, resume=False)
            self.assertIn("pass1", again.calls)
            lines = (out / "results.jsonl").read_text("utf-8").splitlines()
            self.assertEqual(len(lines), 4)

    def test_on_result_only_delivers_settled_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            slow = fc._digest("u", [[r for r in loose if r.name == "合同扫描件.txt"][0].rel])
            fc._ask = FakeModel(low_confidence={slow})
            batches: list[list[dict]] = []
            fc.classify_directory(
                root, TAXONOMY, out_dir=out, pass1_batch=2, concurrency=1, on_result=batches.append
            )
            # 4 个单元、每个恰好一行：低置信那个不在第一轮出行，定论才出
            rows = [row for batch in batches for row in batch]
            self.assertEqual(len(rows), 4)
            self.assertEqual(len({row["unit_id"] for row in rows}), 4)
            # 中间结论绝不外泄——初判给了真实类型（C01）也一样，出行的只有终局
            self.assertTrue(all(row["status"] in ("ok", "failed") for row in rows))
            self.assertEqual(batches[-1][0]["unit_id"], slow)
            self.assertEqual(batches[-1][0]["status"], "ok")
            self.assertIsNone(batches[-1][0]["error"])

    def test_on_result_failure_does_not_abort_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()

            def explode(_rows: list[dict]) -> None:
                raise RuntimeError("下游数据库炸了")

            summary = fc.classify_directory(root, TAXONOMY, out_dir=out, on_result=explode)
            self.assertEqual(summary["units"], 4)
            self.assertEqual(len((out / "results.jsonl").read_text("utf-8").splitlines()), 4)

    def test_report_lists_categories_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            report = (out / "report.md").read_text("utf-8")
            self.assertIn("投标文件-业绩证明", report)
            self.assertIn("path:投标文件", report)
            self.assertIn("等 3 个文件合并为一份", report)

    def test_rejects_empty_taxonomy_and_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            _build(root)
            with self.assertRaises(ValueError):
                fc.classify_directory(root, [])
            with self.assertRaises(FileNotFoundError):
                fc.classify_directory(Path(tmp) / "nope", TAXONOMY)


class AsyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_batches_run_concurrently_up_to_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            # 文件名不能带尾号，否则会被认成同一份资料的拆分件、合并成一个单元
            for letter in "abcdefghijkl":
                target = root / "投标文件" / f"材料{letter}.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"材料 {letter}", encoding="utf-8")
            fake = FakeModel(delay=0.02)
            fc._ask = fake
            fc.classify_directory(root, TAXONOMY, out_dir=out, pass1_batch=1, concurrency=4)
            self.assertEqual(fake.peak, 4)

    def test_concurrency_one_is_serial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fake = FakeModel(delay=0.01)
            fc._ask = fake
            fc.classify_directory(root, TAXONOMY, out_dir=out, pass1_batch=1, concurrency=1)
            self.assertEqual(fake.peak, 1)

    def test_async_entry_awaitable_from_running_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()

            async def drive() -> dict:
                return await fc.aclassify_directory(root, TAXONOMY, out_dir=out)

            summary = asyncio.run(drive())
            self.assertEqual(summary["units"], 4)

    def test_sync_wrapper_refuses_inside_running_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            _build(root)
            fc._ask = FakeModel()

            async def drive() -> None:
                fc.classify_directory(root, TAXONOMY)

            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(drive())
            self.assertIn("aclassify_directory", str(caught.exception))


class DynamicContentTests(unittest.TestCase):
    """读正文的深度由模型逐轮决定：不够就 need_content=true 换更长摘录，读完全文强制定论。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    @staticmethod
    def _verdict(uid: str, *, need: bool, category: str = "B0102") -> dict:
        return {
            "unit_id": uid,
            "category_id": category,
            "category_name": "投标文件-业绩证明",
            "confidence": 0.4 if need else 0.9,
            "need_content": need,
            "evidence": ["content:关键线索"],
            "reasoning": "测试",
            "group_conflict": "",
        }

    def _content_model(self, record: dict):
        """pass1 一律要正文；正文轮里只要还标着「正文未完」就继续索取。"""

        async def reply(system: str, user: str, **_kw: object) -> str:
            uids = _UNIT_ID.findall(user)
            if "正文摘录" in user:
                record["pass2_users"].append(user)
                # 只认单元级标注，不认策略段里顺带出现的字样
                done = "正文摘录（已含全文" in user
                return json.dumps(
                    {"results": [self._verdict(uid, need=not done) for uid in uids]},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"results": [self._verdict(uid, need=True, category="C01") for uid in uids]},
                ensure_ascii=False,
            )

        return reply

    def test_model_requests_more_and_next_round_gives_deeper_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            target = root / "投标文件" / "资料.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            # 前 100 字全是 A，关键线索 B 在 100 字之后：首轮摘录读不到
            target.write_text("A" * 100 + "B" * 290, encoding="utf-8")
            record: dict = {"pass2_users": []}
            fc._ask = self._content_model(record)
            fc.classify_directory(
                root, TAXONOMY, out_dir=out,
                excerpt_chars=100, content_rounds=3, max_excerpt_chars=1000,
            )
            self.assertEqual(len(record["pass2_users"]), 2)
            self.assertIn("正文摘录（每文件至多前 100 字", record["pass2_users"][0])
            self.assertNotIn("B" * 10, record["pass2_users"][0])
            # 第二轮给 4 倍（400 字），390 字全文都在，且标注已含全文
            self.assertIn("正文摘录（已含全文", record["pass2_users"][1])
            self.assertIn("B" * 290, record["pass2_users"][1])
            rows = [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]
            # 没定论的轮次不落行：全程只在定论那一刻写一行
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[-1]["status"], "ok")
            self.assertEqual(rows[-1]["category_id"], "B0102")

    def test_full_content_forces_verdict_even_if_model_keeps_asking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            target = root / "投标文件" / "资料.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("短文件，一轮就能读完", encoding="utf-8")
            record: dict = {"pass2_users": []}

            async def greedy(system: str, user: str, **_kw: object) -> str:
                uids = _UNIT_ID.findall(user)
                if "正文摘录" in user:
                    record["pass2_users"].append(user)
                # 无论哪轮都嚷着要更多正文
                return json.dumps(
                    {"results": [self._verdict(uid, need=True) for uid in uids]}, ensure_ascii=False
                )

            fc._ask = greedy
            fc.classify_directory(root, TAXONOMY, out_dir=out, excerpt_chars=100, content_rounds=3)
            # 全文已给完，模型再要也不给：只跑一轮正文，结果强制终态
            self.assertEqual(len(record["pass2_users"]), 1)
            self.assertIn("正文摘录（已含全文", record["pass2_users"][0])
            rows = [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]
            self.assertIn(rows[-1]["status"], ("ok", "failed"))

    def test_round_cap_forces_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            target = root / "投标文件" / "资料.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("C" * 100000, encoding="utf-8")
            record: dict = {"pass2_users": []}
            fc._ask = self._content_model(record)
            fc.classify_directory(
                root, TAXONOMY, out_dir=out,
                excerpt_chars=100, content_rounds=2, max_excerpt_chars=1000000,
            )
            # 正文永远读不完，但轮次耗尽：第二轮带「最后一轮」策略并强制终态
            self.assertEqual(len(record["pass2_users"]), 2)
            self.assertIn("最后一轮", record["pass2_users"][1])
            rows = [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]
            self.assertIn(rows[-1]["status"], ("ok", "failed"))


class JsonRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_bad_json_is_retried_within_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            inner = FakeModel()
            state = {"n": 0}

            async def flaky(system: str, user: str, **kw: object) -> str:
                state["n"] += 1
                if state["n"] == 1:
                    return "抱歉，我这次没法输出 JSON"
                return await inner(system, user, **kw)

            fc._ask = flaky
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertEqual(summary["units"], 4)
            self.assertEqual(summary["units_failed"], 0)
            # 失败的那次尝试也要进转录，ok=false 且带错误
            calls = [json.loads(line) for line in (out / "calls.jsonl").read_text("utf-8").splitlines()]
            failed = [c for c in calls if not c["ok"]]
            self.assertEqual(len(failed), 1)
            self.assertIn("error", failed[0])
            self.assertEqual(failed[0]["reply"], "抱歉，我这次没法输出 JSON")

    def test_bad_json_exhausts_retries_then_breaker_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            state = {"n": 0}

            async def broken(system: str, user: str, **_kw: object) -> str:
                state["n"] += 1
                return "永远不是 JSON"

            fc._ask = broken
            with self.assertRaises(ValueError) as ctx:
                fc.classify_directory(root, TAXONOMY, out_dir=out, concurrency=1)
            # 单批降级续跑，连续 _BREAKER_LIMIT 批全废才中止；错误信息要说清是熔断
            self.assertIn("中止", str(ctx.exception))
            self.assertGreaterEqual(state["n"], (1 + fc._JSON_RETRIES) * fc._BREAKER_LIMIT)


class UnresolvedRowTests(unittest.TestCase):
    """判不出来的行也要能自证：终局理由不能是过期的「转入下一轮」，名称不能空着。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    @staticmethod
    def _rows(out: Path) -> dict[str, dict]:
        return fc._load_jsonl(out / "results.jsonl")

    def test_unassigned_rows_carry_a_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)

            async def undecidable(system: str, user: str, **_kw: object) -> str:
                ids = re.findall(r"(u_[0-9a-f]{10})", user)
                return json.dumps({
                    "results": [
                        {"unit_id": uid, "category_id": "_待分类", "confidence": 0.2,
                         "need_content": False, "evidence": ["path:新建文件夹"],
                         "reasoning": "目录无业务含义，缺少能定论的用途证据"}
                        for uid in dict.fromkeys(ids)
                    ]
                })

            fc._ask = undecidable
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out, concurrency=1)
            self.assertGreater(summary["units_failed"], 0)
            for row in self._rows(out).values():
                self.assertEqual(row["category_id"], "_待分类")
                # 模型没给名称也要有可显示的值，否则调用方界面上一片空白
                self.assertEqual(row["category_name"], "待分类")

    def test_status_is_machine_readable_and_never_counted_as_success(self) -> None:
        """失败必须有字段可判，且不能进成功统计；尤其是带着真实类型 id 的那种。"""
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)

            async def low_then_bad(system: str, user: str, **_kw: object) -> str:
                if "未提供完整正文" in user:
                    ids = dict.fromkeys(re.findall(r"(u_[0-9a-f]{10})", user))
                    return json.dumps({
                        "results": [
                            {"unit_id": uid, "category_id": "01", "category_name": "投标文件",
                             "confidence": 0.4, "need_content": True, "evidence": ["path:x"],
                             "reasoning": "仅凭路径不足以定论"}
                            for uid in ids
                        ]
                    })
                return "服务器繁忙，请稍后再试。"

            fc._ask = low_then_bad
            summary = fc.classify_directory(
                root, TAXONOMY, out_dir=out, concurrency=1, content_rounds=1, group_check=False
            )
            rows = list(self._rows(out).values())
            self.assertTrue(rows)
            for row in rows:
                # 结论保留了初判的真实类型，光看 category_id 会以为成功了
                self.assertNotEqual(row["category_id"], "_待分类")
                self.assertEqual(row["status"], "failed")
                self.assertIn("非法 JSON", row["error"])
            self.assertEqual(summary["units_ok"], 0)
            self.assertEqual(summary["units_failed"], len(rows))
            # 类型分布只数成功的行：没判成的不许出现在 tally 里
            self.assertEqual(summary["tally"], {})

    def test_normal_rows_are_all_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out, concurrency=1)
            self.assertEqual(summary["units_failed"], 0)
            self.assertEqual(summary["units_ok"], summary["units"])
            self.assertEqual(sum(summary["tally"].values()), summary["units"])
            for row in self._rows(out).values():
                self.assertEqual(row["status"], "ok")
                # 成功行的 error 必须是 null，不能是空串之类的歧义值
                self.assertIsNone(row["error"])

    def test_pass1_undecided_is_forced_into_content_round(self) -> None:
        """待分类必须是最后手段：光凭路径就放弃的单元要被拽去读正文，读完能判就判。"""
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)

            async def lazy_then_decisive(system: str, user: str, **_kw: object) -> str:
                ids = dict.fromkeys(re.findall(r"(u_[0-9a-f]{10})", user))
                if "正文摘录" in user:
                    rows = [
                        {"unit_id": uid, "category_id": "B0102", "category_name": "投标文件-业绩证明",
                         "confidence": 0.85, "need_content": False,
                         "evidence": ["content:业绩证明材料"], "reasoning": "正文表明是业绩材料"}
                        for uid in ids
                    ]
                else:
                    # 第一轮就很自信地放弃：confidence 高、也不要正文
                    rows = [
                        {"unit_id": uid, "category_id": "_待分类", "confidence": 0.9,
                         "need_content": False, "evidence": [], "reasoning": "看不出来"}
                        for uid in ids
                    ]
                return json.dumps({"results": rows}, ensure_ascii=False)

            fc._ask = lazy_then_decisive
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out, concurrency=1)
            # 全部被拽进正文轮并在那里判出了具体类型，一个待分类都不剩
            self.assertEqual(summary["units_failed"], 0)
            self.assertEqual(summary["units_ok"], summary["units"])
            self.assertEqual(summary["pass2"], summary["units"])
            for row in self._rows(out).values():
                self.assertEqual(row["category_id"], "B0102")

    def test_model_said_undecided_is_flagged_apart_from_no_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)

            async def undecidable(system: str, user: str, **_kw: object) -> str:
                ids = dict.fromkeys(re.findall(r"(u_[0-9a-f]{10})", user))
                return json.dumps({
                    "results": [
                        {"unit_id": uid, "category_id": "_待分类", "confidence": 0.2,
                         "need_content": False, "evidence": ["path:新建文件夹"],
                         "reasoning": "缺少能定论的用途证据"}
                        for uid in ids
                    ]
                })

            fc._ask = undecidable
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out, concurrency=1)
            # 模型答了但判不出来，和模型压根没答要分开——error 文案不同，处置动作不同
            self.assertEqual(summary["units_ok"], 0)
            self.assertEqual(summary["units_failed"], summary["units"])
            for row in self._rows(out).values():
                self.assertEqual(row["status"], "failed")
                self.assertIn("证据不足", row["error"])
                self.assertNotIn("未返回", row["error"])

    def test_always_missed_unit_gets_real_final_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)

            async def wrong_ids(system: str, user: str, **_kw: object) -> str:
                # 每轮都答，但 unit_id 对不上任何真实单元（连模糊匹配也救不回）
                return json.dumps({"results": [{"unit_id": "完全不存在的编号", "category_id": "01",
                                                "confidence": 0.9, "need_content": False,
                                                "evidence": ["x"], "reasoning": "y"}]})

            fc._ask = wrong_ids
            fc.classify_directory(root, TAXONOMY, out_dir=out, concurrency=1, content_rounds=1)
            finals = list(self._rows(out).values())
            self.assertTrue(finals)
            for row in finals:
                # 终局行不能只留「已转入下一轮复判」——到这儿没有下一轮了
                self.assertIn("【终局】", row["reasoning"])
                self.assertIn("模型始终未返回该单元的判定", row["reasoning"])
                self.assertEqual(row["status"], "failed")
                self.assertIn("未返回该单元的判定", row["error"])

    def test_last_round_bad_json_reason_reaches_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)

            async def low_then_bad(system: str, user: str, **_kw: object) -> str:
                if "未提供完整正文" in user:
                    ids = re.findall(r"(u_[0-9a-f]{10})", user)
                    return json.dumps({
                        "results": [
                            {"unit_id": uid, "category_id": "01", "category_name": "投标文件",
                             "confidence": 0.4, "need_content": True, "evidence": ["path:x"],
                             "reasoning": "仅凭路径不足以定论"}
                            for uid in dict.fromkeys(ids)
                        ]
                    })
                return "服务器繁忙，请稍后再试。"

            fc._ask = low_then_bad
            summary = fc.classify_directory(
                root, TAXONOMY, out_dir=out, concurrency=1, content_rounds=1, group_check=False
            )
            self.assertGreaterEqual(summary["degraded_batches"], 1)
            finals = list(self._rows(out).values())
            self.assertTrue(finals)
            for row in finals:
                # 初判结论保留，同时写明为什么没能复判成功
                self.assertIn("仅凭路径不足以定论", row["reasoning"])
                self.assertIn("非法 JSON", row["reasoning"])


class PoolRetryTests(unittest.TestCase):
    """资源池瞬时故障（超时/429/5xx）持续重试；鉴权、配额类立即失败。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_timeout_keeps_retrying_until_pool_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            inner = FakeModel()
            state = {"n": 0}

            async def flaky(system: str, user: str, **kw: object) -> str:
                state["n"] += 1
                if state["n"] <= 3:
                    raise RuntimeError("504 gateway timeout")
                return await inner(system, user, **kw)

            fc._ask = flaky
            waits: list[str] = []
            events: list[dict] = []
            summary = fc.classify_directory(
                root, TAXONOMY, out_dir=out, concurrency=1,
                retry_interval=0.2, progress=waits.append, on_retry=events.append,
            )
            # 三次超时后仍跑完，任务不失败
            self.assertEqual(summary["units_failed"], 0)
            self.assertEqual(summary["pool_waits"], 3)
            self.assertGreater(summary["pool_wait_sec"], 0)
            # 等待情况经 progress 报给调用方
            self.assertTrue(any("模型资源暂不可用" in text for text in waits))
            # 结构化事件：三次等待 + 一次恢复，字段够调用方直接展示
            kinds = [item["event"] for item in events]
            self.assertEqual(kinds, ["pool_wait", "pool_wait", "pool_wait", "pool_recovered"])
            first = events[0]
            self.assertEqual(first["attempt"], 1)
            self.assertEqual(first["delay_sec"], 0.2)
            self.assertIn("504", first["error"])
            self.assertIn("模型资源暂不可用", first["message"])
            self.assertEqual(events[2]["waits"], 3)
            self.assertGreater(events[2]["total_wait_sec"], 0)

    def test_retry_event_callback_failure_does_not_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            inner = FakeModel()
            state = {"n": 0}

            async def flaky(system: str, user: str, **kw: object) -> str:
                state["n"] += 1
                if state["n"] == 1:
                    raise RuntimeError("connection reset")
                return await inner(system, user, **kw)

            def boom(_event: dict) -> None:
                raise RuntimeError("上报通道自己炸了")

            fc._ask = flaky
            summary = fc.classify_directory(
                root, TAXONOMY, out_dir=out, concurrency=1, retry_interval=0.01, on_retry=boom
            )
            self.assertEqual(summary["units_failed"], 0)

    def test_auth_failure_fails_fast_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            state = {"n": 0}

            async def unauthorized(system: str, user: str, **_kw: object) -> str:
                state["n"] += 1
                raise RuntimeError("401 invalid api key")

            fc._ask = unauthorized
            with self.assertRaises(RuntimeError):
                fc.classify_directory(
                    root, TAXONOMY, out_dir=out, concurrency=1, retry_interval=0.01
                )
            # 鉴权错误重试一万次也是错：只调一次就抛
            self.assertEqual(state["n"], 1)

    def test_retry_max_attempts_caps_the_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            state = {"n": 0}

            async def always_down(system: str, user: str, **_kw: object) -> str:
                state["n"] += 1
                raise RuntimeError("connection refused")

            fc._ask = always_down
            with self.assertRaises(RuntimeError):
                fc.classify_directory(
                    root, TAXONOMY, out_dir=out, concurrency=1,
                    retry_interval=0.01, retry_max_attempts=3,
                )
            self.assertEqual(state["n"], 3)

    def test_gate_makes_concurrent_batches_back_off_together(self) -> None:
        gate = fc._PoolGate(0.05)
        delay = gate.penalize()
        self.assertAlmostEqual(delay, 0.05, places=3)

        async def scenario() -> float:
            started = asyncio.get_running_loop().time()
            # 一个批次撞墙后，其他在飞批次也要一起等，而不是立刻冲进去
            await asyncio.gather(*(gate.wait_turn() for _ in range(4)))
            return asyncio.get_running_loop().time() - started

        waited = asyncio.run(scenario())
        self.assertGreaterEqual(waited, 0.04)
        self.assertEqual(gate.waits, 1)

    def test_gate_honours_retry_after_longer_than_interval(self) -> None:
        # 池子明确要求等更久就听它的；要求得比配置间隔短则按配置来，不早于间隔冲击
        self.assertEqual(fc._retry_after_sec("429 too many requests retry-after: 90s"), 90.0)
        self.assertEqual(fc._retry_after_sec("504 gateway timeout"), 0.0)
        gate = fc._PoolGate(10.0)
        self.assertEqual(gate.penalize(90.0), 90.0)
        self.assertEqual(fc._PoolGate(10.0).penalize(2.0), 10.0)


class JsonFallbackTests(unittest.TestCase):
    """坏 JSON 三层兜底：修复笔误、抢救完整记录、整批降级但不杀整轮。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_repair_unescaped_inner_quotes(self) -> None:
        # qwq 实测输出：evidence 里引用原文时套未转义的内层引号
        raw = '{"results": [{"unit_id": "u_1", "evidence": ["content: "工程 施工图 设计阶段""], "confidence": 0.9}]}'
        payload, mode = fc._parse_lenient(raw)
        self.assertEqual(mode, "repaired")
        self.assertEqual(payload["results"][0]["evidence"], ['content: "工程 施工图 设计阶段"'])

    def test_repair_literal_newline_in_string(self) -> None:
        raw = '{"results": [{"unit_id": "u_1", "reasoning": "第一行\n第二行"}]}'
        payload, mode = fc._parse_lenient(raw)
        self.assertEqual(mode, "repaired")
        self.assertEqual(payload["results"][0]["reasoning"], "第一行\n第二行")

    def test_salvage_recovers_complete_records_from_truncated_reply(self) -> None:
        raw = (
            '{"results": [{"unit_id": "u_1", "category_id": "C01"}, '
            '{"unit_id": "u_2", "category_id": "C02"}, {"unit_id": "u_3", "category_id": '
        )
        payload, mode = fc._parse_lenient(raw)
        self.assertEqual(mode, "salvaged")
        self.assertEqual([r["unit_id"] for r in payload["results"]], ["u_1", "u_2"])

    def test_unsalvageable_still_raises(self) -> None:
        with self.assertRaises(ValueError):
            fc._parse_lenient("抱歉，我只会写散文")

    def test_degraded_pass1_batch_recovers_in_pass2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            inner = FakeModel()

            async def broken_pass1(system: str, user: str, **kw: object) -> str:
                reply = await inner(system, user, **kw)
                if inner.calls[-1] == "pass1":
                    return "这批我拒绝输出 JSON"
                return reply

            fc._ask = broken_pass1
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            # 第一轮整批降级但任务不失败，单元全部转正文轮判出
            self.assertEqual(summary["degraded_batches"], 1)
            self.assertEqual(summary["pass2"], summary["units"])
            self.assertEqual(summary["units_failed"], 0)
            rows = [json.loads(line) for line in (out / "results.jsonl").read_text("utf-8").splitlines()]
            # 降级只是中间态，不落行；正文轮救回来后每单元恰好一条成功的终局行
            self.assertEqual(len(rows), summary["units"])
            self.assertTrue(all(r["status"] == "ok" for r in rows))


class MangledIdTests(unittest.TestCase):
    """弱模型回填 unit_id 常抄错个别字符，实测 u_82fd436192 会答成 u_82rd436192。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_match_replies_recovers_one_char_typo(self) -> None:
        expected = ["u_82fd436192", "u_a9bf7f0bc7"]
        items = [
            {"unit_id": "u_82rd436192", "category_id": "C01"},
            {"unit_id": "u_a9bf7f0bc7", "category_id": "C02"},
        ]
        found = fc._match_replies(expected, items, "unit_id")
        self.assertEqual(found["u_82fd436192"]["category_id"], "C01")
        self.assertEqual(found["u_a9bf7f0bc7"]["category_id"], "C02")

    def test_match_replies_prefers_exact_over_fuzzy(self) -> None:
        # 精确命中的行不能被后来的错 id 行抢走
        expected = ["u_aaaaaaaaa1", "u_aaaaaaaaa2"]
        items = [
            {"unit_id": "u_aaaaaaaaa1", "category_id": "right"},
            {"unit_id": "u_aaaaaaaax2", "category_id": "fuzzy"},
        ]
        found = fc._match_replies(expected, items, "unit_id")
        self.assertEqual(found["u_aaaaaaaaa1"]["category_id"], "right")
        self.assertEqual(found["u_aaaaaaaaa2"]["category_id"], "fuzzy")

    def test_match_replies_keeps_garbage_as_missing(self) -> None:
        found = fc._match_replies(["u_82fd436192"], [{"unit_id": "u_zzzzzzzzzz"}], "unit_id")
        self.assertEqual(found, {})

    def test_mangled_pass1_ids_do_not_trigger_second_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            inner = FakeModel()

            async def sloppy(system: str, user: str, **kw: object) -> str:
                reply = await inner(system, user, **kw)
                # 把每个回填的 unit_id 末位字符改掉，模拟抄错
                return re.sub(
                    r'"unit_id": "(u_\w{9})\w"',
                    lambda m: f'"unit_id": "{m.group(1)}x"',
                    reply,
                )

            fc._ask = sloppy
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertEqual(summary["pass2"], 0)
            self.assertEqual(summary["units_failed"], 0)
            self.assertNotIn("pass2", inner.calls)


class TitleLineTests(unittest.TestCase):
    """第一轮附标题行（正文开头一小段）核验文件名：文件名写错也能被感知，不再是孤证定案。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def _spy(self, users: list[str]):
        inner = FakeModel()

        async def spy(system: str, user: str, **kw: object) -> str:
            if "资料分类专家" in system:
                users.append(user)
            return await inner(system, user, **kw)

        return spy

    def test_pass1_carries_title_line_to_expose_misnamed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            # 文件名写错：名为「合同」，正文却是验收单，还躺在无业务含义的容器目录里
            target = root / "待整理" / "合同.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("工程竣工验收单 验收日期 2024-01-01", encoding="utf-8")
            users: list[str] = []
            fc._ask = self._spy(users)
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertEqual(len(users), 1)
            self.assertIn("标题行: 工程竣工验收单", users[0])
            # 「正文摘录」是第二轮的标志字样，第一轮的提示词里不许出现
            self.assertNotIn("正文摘录", users[0])

    def test_title_chars_zero_disables_title_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            users: list[str] = []
            fc._ask = self._spy(users)
            fc.classify_directory(root, TAXONOMY, out_dir=out, title_chars=0)
            self.assertTrue(users)
            for user in users:
                self.assertNotIn("标题行:", user)

    def test_binary_title_says_text_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            target = root / "待整理" / "凭证.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\n")
            users: list[str] = []
            fc._ask = self._spy(users)
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertIn("无法抽取文本", users[0])

    def test_title_line_flattens_to_single_line(self) -> None:
        self.assertEqual(fc._title_line("第一行\n\n 第二行 "), "标题行: 第一行 / 第二行\n")
        self.assertEqual(fc._title_line(""), "")
        self.assertEqual(fc._title_line("\n \n"), "")


class TreeOverviewTests(unittest.TestCase):
    """每批提示词都带整个项目的目录总览，模型先有全局观再判个体。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_render_tree_lists_every_folder_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build(root)
            tree = fc._render_tree(fc.scan_files(root))
            self.assertIn("某变电站工程/二标段/投标文件/业绩证明（4 个文件）", tree)
            self.assertIn("某变电站工程/招标文件（1 个文件）", tree)

    def test_render_tree_truncates_huge_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in range(fc._TREE_MAX_DIRS + 30):
                target = root / f"目录{idx:04d}" / "文件.txt"
                target.parent.mkdir(parents=True)
                target.write_text("x", encoding="utf-8")
            tree = fc._render_tree(fc.scan_files(root))
            self.assertIn("另有 30 个目录未列出", tree)
            self.assertEqual(len(tree.splitlines()), fc._TREE_MAX_DIRS + 1)

    def test_both_passes_carry_the_overview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            slow = fc._digest("u", [[r for r in loose if r.name == "合同扫描件.txt"][0].rel])
            users: list[str] = []
            inner = FakeModel(low_confidence={slow})

            async def spy(system: str, user: str, **kw: object) -> str:
                if "资料分类专家" in system:
                    users.append(user)
                return await inner(system, user, **kw)

            fc._ask = spy
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            self.assertTrue(users)
            for user in users:
                self.assertIn("项目目录总览", user)
                self.assertIn("某变电站工程/二标段/合同（1 个文件）", user)


class ProjectContextTests(unittest.TestCase):
    """项目背景（名称/编号/分类）进提示词，让模型分得清本项目文件与引用的外部材料。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_render_lists_given_fields_in_fixed_order(self) -> None:
        text = fc._render_project({"code": "XJ-2026-007", "name": "新疆某220kV变电站", "category": "输变电工程"})
        self.assertIn("- 项目名称：新疆某220kV变电站", text)
        self.assertIn("- 项目编号：XJ-2026-007", text)
        self.assertIn("- 项目分类：输变电工程", text)
        # 顺序固定为名称、编号、分类，不随调用方传入顺序变
        self.assertLess(text.index("项目名称"), text.index("项目编号"))
        self.assertLess(text.index("项目编号"), text.index("项目分类"))
        # 用法说明必须在场：这段的价值就是教模型怎么用项目名
        self.assertIn("其它", text)
        self.assertIn("以后者为准", text)

    def test_render_tolerates_chinese_keys_and_partial_input(self) -> None:
        text = fc._render_project({"项目名称": "某工程", "项目编号": "  ", "无关键": "x"})
        self.assertIn("- 项目名称：某工程", text)
        self.assertNotIn("项目编号", text)
        self.assertNotIn("项目分类", text)

    def test_render_returns_empty_when_nothing_useful(self) -> None:
        for value in (None, {}, {"name": ""}, {"name": None}, {"其它": "x"}):
            self.assertEqual(fc._render_project(value), "")

    def test_long_values_are_clipped(self) -> None:
        text = fc._render_project({"name": "长" * 500})
        self.assertIn("长" * fc._PROJECT_VALUE_MAX, text)
        self.assertNotIn("长" * (fc._PROJECT_VALUE_MAX + 1), text)

    def test_both_passes_carry_project_and_omit_it_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            slow = fc._digest("u", [[r for r in loose if r.name == "合同扫描件.txt"][0].rel])
            users: list[str] = []
            inner = FakeModel(low_confidence={slow})

            async def spy(system: str, user: str, **kw: object) -> str:
                if "资料分类专家" in system:
                    users.append(user)
                return await inner(system, user, **kw)

            fc._ask = spy
            fc.classify_directory(
                root, TAXONOMY, out_dir=out,
                project={"name": "新疆某220kV变电站", "code": "XJ-2026-007", "category": "输变电工程"},
            )
            self.assertTrue(users)
            for user in users:
                self.assertIn("本项目背景", user)
                self.assertIn("XJ-2026-007", user)

            users.clear()
            fc._EXCERPT_CACHE.clear()
            fc.classify_directory(root, TAXONOMY, out_dir=Path(tmp) / "out2")
            self.assertTrue(users)
            for user in users:
                # 不传就整段消失，不留空标题
                self.assertNotIn("本项目背景", user)
                self.assertTrue(user.lstrip().startswith("## 类型表"))


class CallTranscriptTests(unittest.TestCase):
    """每次模型调用都要转录进 calls.jsonl：说了什么、答了什么、花了多久。"""

    def setUp(self) -> None:
        self._real_ask = fc._ask
        fc._EXCERPT_CACHE.clear()

    def tearDown(self) -> None:
        fc._ask = self._real_ask

    def test_every_model_call_is_transcribed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            groups, loose = fc.candidate_groups(fc.scan_files(root))
            slow = fc._digest("u", [[r for r in loose if r.name == "合同扫描件.txt"][0].rel])
            fake = FakeModel(low_confidence={slow})
            fc._ask = fake
            summary = fc.classify_directory(root, TAXONOMY, out_dir=out)
            calls = [json.loads(line) for line in (out / "calls.jsonl").read_text("utf-8").splitlines()]
            # 拆分件确认 + 第一轮 + 正文轮，一次调用一行，seq 连续
            self.assertEqual(len(calls), len(fake.calls))
            self.assertEqual(summary["model_calls"], len(calls))
            self.assertEqual(sorted(c["seq"] for c in calls), list(range(1, len(calls) + 1)))
            stages = {c["stage"] for c in calls}
            self.assertEqual(stages, {"group", "pass1", "pass2"})
            pass2 = [c for c in calls if c["stage"] == "pass2"]
            self.assertEqual(pass2[0]["round"], 1)
            for entry in calls:
                self.assertTrue(entry["ok"])
                self.assertIn("类型表" if entry["stage"] != "group" else "候选组", entry["user"])
                self.assertTrue(entry["reply"].startswith("{"))
                self.assertGreaterEqual(entry["duration_ms"], 0)
            self.assertEqual(summary["calls_file"], str(out / "calls.jsonl"))

    def test_no_resume_clears_old_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()
            fc.classify_directory(root, TAXONOMY, out_dir=out)
            first = (out / "calls.jsonl").read_text("utf-8").splitlines()
            fc._ask = FakeModel()
            fc.classify_directory(root, TAXONOMY, out_dir=out, resume=False)
            second = (out / "calls.jsonl").read_text("utf-8").splitlines()
            # 重跑从 1 重新计数，不在旧转录后面续写
            self.assertEqual(len(second), len(first))
            self.assertEqual(json.loads(second[0])["seq"], 1)


class SurfaceTests(unittest.TestCase):
    def test_registered_but_not_kernel(self) -> None:
        self.assertNotIn("classify_files", KERNEL_TOOLS)
        self.assertIn("classify_files", {item.name for item in list_tools()})

    def test_tool_is_async_so_kernel_loop_awaits_it(self) -> None:
        # loop.py 对没设 timeout_ms 的同步工具会直接在事件循环里调，会卡住循环
        spec = [item for item in list_tools() if item.name == "classify_files"][0]
        self.assertTrue(inspect.iscoroutinefunction(spec.func))

    def test_tool_report_renders_status_counts(self) -> None:
        # 工具汇报模板的占位符跟 summary 字段是硬耦合的，改字段名不跑这条就会 KeyError
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "data", Path(tmp) / "out"
            _build(root)
            fc._ask = FakeModel()
            text = asyncio.run(
                fc.classify_files(str(root), json.dumps(TAXONOMY), out_dir=str(out))
            )
            self.assertIn("状态:", text)
            self.assertIn("成功 4 个", text)
            self.assertIn("失败 0 个", text)

    def test_excerpt_degrades_on_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            rec = fc.FileRec(path=path, rel="scan.png", size=8)
            self.assertIn("无法抽取文本", fc.excerpt(rec, 200))


if __name__ == "__main__":
    unittest.main()
