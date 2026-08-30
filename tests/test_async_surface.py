"""异步面：会喊模型的入口都得是协程，同步版不许在事件循环里堵住循环。

这一组盯的是 `AGENTS.md`「并发：能异步就异步」立下的几条硬规则，尤其是曾经真实存在过
的那个坑——同步函数发现自己在事件循环里，就改调 `llm._request()` 这个私有同步方法。它
不报错，只是把整个 agent 卡住，现场根本看不出原因。

  * 桥：`run_sync` 在循环里必须抛，且要指名换哪个异步入口
  * 三个后台慢活（判官 / 巩固 / 日记小结）都有 `afoo` / `foo` 双入口
  * 注入点（`merge_fn` / `write_fn`）同步异步都得收，测试给的多是普通 lambda
  * 收割热路径在循环里不许同步等模型，得退回结构判定
  * 谁都不许再写 `llm._request(context)`
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
import warnings
from pathlib import Path

from witty_agent import diary, memory_consolidate, memory_harvest
from witty_agent.async_bridge import in_event_loop, run_sync
from witty_agent.memory import append_unique_bullets, ensure_lattice, topic_body
from witty_agent.paths import project_root
from witty_agent.plugins import file_classify


def _mem() -> Path:
    directory = Path(tempfile.mkdtemp()) / "mem"
    directory.mkdir(parents=True)
    ensure_lattice(directory)
    return directory


class BridgeTests(unittest.TestCase):
    def test_run_sync_works_outside_a_loop(self) -> None:
        async def answer() -> int:
            return 7

        self.assertEqual(run_sync(answer(), entry="answer"), 7)

    def test_run_sync_refuses_inside_a_loop_and_names_the_entry(self) -> None:
        async def answer() -> int:
            return 7

        async def drive() -> None:
            run_sync(answer(), entry="aanswer")

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(drive())
        self.assertIn("aanswer", str(caught.exception))

    def test_refusal_does_not_leak_a_pending_coroutine(self) -> None:
        """不 close 掉协程会多一条 never awaited 警告，把真正的报错埋掉。"""

        async def answer() -> int:
            return 7

        async def drive() -> None:
            run_sync(answer(), entry="aanswer")

        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            with self.assertRaises(RuntimeError):
                asyncio.run(drive())
        self.assertEqual([w for w in seen if "never awaited" in str(w.message)], [])

    def test_in_event_loop_reports_both_ways(self) -> None:
        self.assertFalse(in_event_loop())
        self.assertTrue(asyncio.run(_report_in_loop()))


async def _report_in_loop() -> bool:
    return in_event_loop()


class DoubleEntryTests(unittest.TestCase):
    """每个会喊模型的入口都得有 afoo / foo 两个门，且 afoo 是真协程。"""

    def test_model_entries_are_coroutines(self) -> None:
        for func in (
            diary.asummarize_day,
            diary._model_summary,
            diary._ask,
            memory_consolidate.aconsolidate,
            memory_consolidate._model_merge,
            memory_consolidate._ask,
            memory_harvest.ajudge_pending_leftover,
            memory_harvest._amodel_judge,
            memory_harvest._ask_judge,
            file_classify.aclassify_directory,
            file_classify._ask,
        ):
            self.assertTrue(inspect.iscoroutinefunction(func), func.__name__)

    def test_sync_wrappers_stay_plain_functions(self) -> None:
        for func in (
            diary.summarize_day,
            memory_consolidate.consolidate,
            memory_harvest.judge_pending_leftover,
            file_classify.classify_directory,
        ):
            self.assertFalse(inspect.iscoroutinefunction(func), func.__name__)

    def test_sync_wrappers_refuse_inside_a_loop(self) -> None:
        directory = _mem()

        async def drive(call) -> None:
            call()

        for call in (
            lambda: diary.summarize_day("2026-08-26", memory_dir=directory),
            lambda: memory_consolidate.consolidate(directory, ["assets"]),
            lambda: memory_harvest.judge_pending_leftover(directory, ["随便一句"], "问句"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(drive(call))
            self.assertIn("await", str(caught.exception))


class InjectionTests(unittest.TestCase):
    """merge_fn / write_fn 是测试和脚本的注入点，同步异步都得收。"""

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

    def test_consolidate_takes_an_async_merge_fn(self) -> None:
        async def merge(_lines, _slug, _settings):
            return [
                "2026-08-02 点表台账放 //nas/dispatch/points/ 下面",
                "2026-08-03 日报模板在 ~/work/templates/daily.docx",
            ]

        report = asyncio.run(memory_consolidate.aconsolidate(self.directory, ["assets"], merge_fn=merge))
        self.assertEqual(report["cells"], ["assets"])
        self.assertNotIn("共享盘", topic_body(self.directory, "assets"))

    def test_consolidate_still_takes_a_sync_merge_fn(self) -> None:
        report = memory_consolidate.consolidate(
            self.directory,
            ["assets"],
            merge_fn=lambda _lines, _slug, _settings: [
                "2026-08-02 点表台账放 //nas/dispatch/points/ 下面",
                "2026-08-03 日报模板在 ~/work/templates/daily.docx",
            ],
        )
        self.assertEqual(report["cells"], ["assets"])

    def test_summarize_day_takes_either_kind_of_write_fn(self) -> None:
        from witty_agent.diary import note_work, today_stamp

        day = today_stamp()
        note_work("改了 a.py", memory_dir=self.directory)

        async def compose(_day, _work, _chat) -> str:
            return "异步写的小结。"

        self.assertEqual(
            asyncio.run(diary.asummarize_day(day, memory_dir=self.directory, write_fn=compose)),
            "异步写的小结。",
        )
        self.assertEqual(
            diary.summarize_day(
                day,
                memory_dir=self.directory,
                write_fn=lambda _day, _work, _chat: "同步写的小结。",
            ),
            "同步写的小结。",
        )


class HotPathTests(unittest.TestCase):
    def test_leftover_falls_back_to_structural_inside_a_loop(self) -> None:
        """收割跑在事件循环里（`Session._harvest_memory`），那儿不许同步等模型。

        判官这一段本来就该靠 `defer_judge=True` 挪到后台任务；循环里撞上就退回结构判据，
        而不是把循环堵死。
        """
        from dataclasses import replace

        from witty_agent.memory_config import load_memory_settings

        settings = replace(load_memory_settings(), judge_leftover=True)
        called = {"n": 0}

        def tripwire(*_args, **_kwargs):
            called["n"] += 1
            raise AssertionError("事件循环里不该同步喊模型")

        original_judge, original_live = memory_harvest._model_judge, memory_harvest._live_judge_allowed
        memory_harvest._model_judge = tripwire
        memory_harvest._live_judge_allowed = lambda: True
        try:

            async def drive():
                return memory_harvest._decide_leftover(
                    ["遥信抖动一般是接点接触不良。"], "遥信抖动", settings, None
                )

            asyncio.run(drive())
        finally:
            memory_harvest._model_judge = original_judge
            memory_harvest._live_judge_allowed = original_live
        self.assertEqual(called["n"], 0)


class NoBlockingBypassTests(unittest.TestCase):
    def test_nobody_calls_the_private_sync_request(self) -> None:
        """`llm._request` 是同步的，在事件循环里调会卡死整个 agent。"""
        offenders = []
        for path in (project_root() / "src" / "witty_agent").rglob("*.py"):
            if path.name == "llm.py":
                continue
            for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
                if "_request(context)" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
