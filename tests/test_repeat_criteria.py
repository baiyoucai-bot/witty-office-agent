"""转圈检测的判据：什么算「同一次调用又来一次」，以及什么能为重复开脱。

改前 `RepeatToolReminder` 只有一个 key 槽，数的是**连续完全相同**的调用：
  交替 —— A-B-A-B 打转、被一次 `ls` 隔开的重试，计数每次都归 1，一次都不触发。
  拼法 —— `a.py` / `./a.py` / 绝对写法 / `dir/` 是四把不同的 key，模型换个拼法重试
          就重新从 1 开始数。

生产形状实测（`/tmp/probe_spin.py`，一次调用算一轮，阈值读真配置 [3,5,8]；两处**单独归因**）：
    改前（连续 + 不归一）        调参漏挡 7/8　留出漏挡 7/7
    只改计数（路径仍不归一）      调参漏挡 2/8　留出漏挡 3/7
    只归一路径（仍数连续）        调参漏挡 5/8　留出漏挡 5/7
    两者都改                    调参漏挡 1/8　留出漏挡 1/7
四种配置下误停、误提醒都是 0。两处互不覆盖，谁也到不了终点，所以同一刀落。

放宽方向才是危险方向（放宽 = 把正在推进的活当成转圈停掉），所以每条否决单独压住：
只读调用不为重复开脱、会改世界的调用才开脱、非路径参数一律原样参与、key 表有上限。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.guard import _KEY_WINDOW, RepeatToolReminder, changes_state
from witty_agent.loop import READONLY_TOOLS
from witty_agent.prompts import get_prompt

# 这些插在两次相同调用中间**不能**为重复开脱：它们没改变任何东西，上次结果照样作数。
# 名单取自真 `READONLY_TOOLS`（判据本体），不在这里另抄一份含义。
MUST_NOT_EXCUSE = ("read", "grep", "ls", "find", "memory_read", "memory_status")
# 这些插在中间**必须**开脱：世界变了，同一次调用的结果可能不一样了。
MUST_EXCUSE = ("edit", "write", "bash", "apply_patch", "memory_write")


def top_count(
    reminder: RepeatToolReminder,
    trace: list[tuple[str, dict]],
) -> tuple[int, bool]:
    """喂一串调用，返回 (最高计数, 提醒过没有)。`session.on_tool_result` 就是这么调的。"""
    top = 0
    nudged = False
    for name, args in trace:
        if reminder.observe(name, args) is not None:
            nudged = True
        top = max(top, reminder._count)
    return top, nudged


class ChangesStateTests(unittest.TestCase):
    """判据的那半张表：谁能让「上次结果还作数」失效。"""

    def test_readonly_tools_never_excuse_a_repeat(self) -> None:
        for name in MUST_NOT_EXCUSE:
            with self.subTest(name=name):
                self.assertIn(name, READONLY_TOOLS, "判据表本身漏了这个只读工具")
                self.assertFalse(changes_state(name))

    def test_mutating_tools_excuse_a_repeat(self) -> None:
        for name in MUST_EXCUSE:
            with self.subTest(name=name):
                self.assertTrue(changes_state(name))

    def test_criterion_is_the_complement_of_readonly_tools(self) -> None:
        """不另立一张表：`changes_state` 必须逐项等于 `READONLY_TOOLS` 的补集。

        另立一张表就会两处对不上而静默失效——证伪账本的判据表就是这么错过最常见那种失败的。
        """
        for name in sorted(READONLY_TOOLS):
            with self.subTest(name=name):
                self.assertFalse(changes_state(name))

    def test_unknown_tool_is_treated_as_world_changing(self) -> None:
        """名字不认识就算改了世界——保守方向是少停轮，不是多停轮。

        插件工具、MCP 工具、以后新加的工具都走这条路。反过来（不认识就当只读）会把
        「插件工具推进了一步」当成没推进，误停正在干活的循环。
        """
        for name in ("some_plugin_tool", "mcp__x__y", "", "未来的新工具"):
            with self.subTest(name=name):
                self.assertTrue(changes_state(name))


class RepeatCountingTests(unittest.TestCase):
    """计数那一半：不要求连续，要求「中间没有改变结果的事情发生」。"""

    def test_alternating_two_calls_still_counts(self) -> None:
        """A-B-A-B 交替打转。改前两把 key 互相清零，计数永远是 1。"""
        reminder = RepeatToolReminder(thresholds=[3])
        trace = [("read", {"path": "a.py"}), ("read", {"path": "b.py"})] * 3
        top, nudged = top_count(reminder, trace)
        self.assertEqual(top, 3)
        self.assertTrue(nudged)

    def test_readonly_call_in_between_does_not_reset(self) -> None:
        reminder = RepeatToolReminder(thresholds=[3])
        trace = [
            ("read", {"path": "notes.md"}),
            ("ls", {"path": "docs"}),
            ("read", {"path": "notes.md"}),
            ("ls", {"path": "docs"}),
            ("read", {"path": "notes.md"}),
        ]
        top, nudged = top_count(reminder, trace)
        self.assertEqual(top, 3)
        self.assertTrue(nudged)

    def test_state_change_clears_other_keys(self) -> None:
        """「改一处跑一次测试」来回三轮：`bash` 参数完全一样，但不是转圈。

        这是最危险的那条误停：`edit` 改了文件，同一条测试命令的结果可能就不一样了。
        """
        reminder = RepeatToolReminder(thresholds=[3])
        test_cmd = "uv run python -m unittest discover -s tests -q"
        trace = [
            ("edit", {"path": "src/x.py", "old_text": "a", "new_text": "b"}),
            ("bash", {"command": test_cmd}),
            ("edit", {"path": "src/x.py", "old_text": "b", "new_text": "c"}),
            ("bash", {"command": test_cmd}),
            ("edit", {"path": "src/y.py", "old_text": "d", "new_text": "e"}),
            ("bash", {"command": test_cmd}),
        ]
        top, nudged = top_count(reminder, trace)
        self.assertEqual(top, 1)
        self.assertFalse(nudged)

    def test_state_change_keeps_its_own_count(self) -> None:
        """同一条命令连着跑，中间什么都没改——那本身就是在转圈，不许自己给自己开脱。

        `observe` 里清空别人的计数时必须**留下自己那把**。清光了这条就永远数不到 2。
        """
        reminder = RepeatToolReminder(thresholds=[3])
        trace = [("bash", {"command": "ls -la"})] * 3
        top, nudged = top_count(reminder, trace)
        self.assertEqual(top, 3)
        self.assertTrue(nudged)

    def test_read_edit_read_edit_is_progress(self) -> None:
        """读—改—读—改—读：改到对为止，不是转圈。"""
        reminder = RepeatToolReminder(thresholds=[3])
        trace = [
            ("read", {"path": "src/x.py"}),
            ("edit", {"path": "src/x.py", "old_text": "a", "new_text": "b"}),
            ("read", {"path": "src/x.py"}),
            ("edit", {"path": "src/x.py", "old_text": "b", "new_text": "c"}),
            ("read", {"path": "src/x.py"}),
        ]
        top, nudged = top_count(reminder, trace)
        self.assertEqual(top, 1)
        self.assertFalse(nudged)

    def test_paging_is_not_repeating(self) -> None:
        """翻页读一个长文件：`path` 相同，`offset` 递进。非路径参数原样参与指纹。"""
        reminder = RepeatToolReminder(thresholds=[3])
        path = "src/witty_agent/guard.py"
        trace = [
            ("read", {"path": path}),
            ("read", {"path": path, "offset": 400}),
            ("read", {"path": path, "offset": 800}),
            ("read", {"path": path, "offset": 1200}),
        ]
        top, nudged = top_count(reminder, trace)
        self.assertEqual(top, 1)
        self.assertFalse(nudged)

    def test_excluded_tool_is_not_counted(self) -> None:
        reminder = RepeatToolReminder(thresholds=[2], exclude=["read"])
        top, nudged = top_count(reminder, [("read", {"path": "a.py"})] * 4)
        self.assertEqual(top, 0)
        self.assertFalse(nudged)

    def test_key_table_is_bounded(self) -> None:
        """一轮几百次调用不能让 key 表无界增长。挤掉旧的方向是漏挡，不会凭空多挡。"""
        reminder = RepeatToolReminder(thresholds=[3])
        for index in range(_KEY_WINDOW * 3):
            reminder.observe("read", {"path": f"f{index}.py"})
        self.assertLessEqual(len(reminder._counts), _KEY_WINDOW)

    def test_evicted_key_starts_over(self) -> None:
        """被挤掉的 key 回来时从 1 数起——漏挡，不是误挡。"""
        reminder = RepeatToolReminder(thresholds=[2])
        first = {"path": "old.py"}
        reminder.observe("read", first)
        for index in range(_KEY_WINDOW + 1):
            reminder.observe("read", {"path": f"f{index}.py"})
        self.assertIsNone(reminder.observe("read", first))
        self.assertEqual(reminder._count, 1)

    def test_reset_clears_every_key(self) -> None:
        reminder = RepeatToolReminder(thresholds=[2])
        reminder.observe("read", {"path": "a.py"})
        reminder.observe("read", {"path": "b.py"})
        reminder.reset()
        self.assertEqual(reminder._counts, {})
        self.assertIsNone(reminder.observe("read", {"path": "a.py"}))
        self.assertEqual(reminder._count, 1)


class FingerprintTests(unittest.TestCase):
    """指纹那一半：同一个文件的不同拼法算同一次调用。"""

    def test_spelling_variants_collapse_without_workspace(self) -> None:
        """没有工作区可谈时只做纯文本归一：`./` 前缀、末尾斜杠、反斜杠。"""
        reminder = RepeatToolReminder(thresholds=[3])
        keys = {
            reminder.fingerprint("read", {"path": spelling})
            for spelling in ("src/x.py", "./src/x.py", "src/x.py/", ".//src/x.py", "src\\x.py")
        }
        self.assertEqual(len(keys), 1)

    def test_absolute_and_relative_collapse_with_workspace(self) -> None:
        """给了工作区就按工具真去看的那个文件算。

        `find`/`grep` 回来的是绝对路径，模型下一步常改回相对写法——那是同一次调用。
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            reminder = RepeatToolReminder(thresholds=[3], workspace=str(workspace))
            relative = reminder.fingerprint("read", {"path": "src/x.py"})
            absolute = reminder.fingerprint("read", {"path": str(workspace / "src" / "x.py")})
            self.assertEqual(relative, absolute)

    def test_path_canonicalization_agrees_with_the_ledger(self) -> None:
        """两处对「哪个文件」的看法必须一致：都走 `sandbox.fingerprint_target`。

        对不上就会一处认得出、另一处认不出同一个文件——证伪账本此前自己拼路径，盯的是
        沙箱映射之外的幽灵路径。
        """
        from witty_agent.sandbox import fingerprint_target

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            reminder = RepeatToolReminder(thresholds=[3], workspace=str(workspace))
            for raw in ("src/x.py", "./src/x.py", "sandbox/y.py", str(workspace / "src" / "x.py")):
                with self.subTest(raw=raw):
                    self.assertEqual(
                        reminder._canonical_path(raw),
                        str(fingerprint_target(str(workspace), raw)),
                    )

    def test_non_path_arguments_survive_canonicalization(self) -> None:
        """只归一 `path`，别的参数一个字都不许动——动了就会把翻页当成重复。"""
        reminder = RepeatToolReminder(thresholds=[3])
        keys = {
            reminder.fingerprint("read", {"path": "a.py", "offset": offset})
            for offset in (1, 400, 800)
        }
        self.assertEqual(len(keys), 3)
        self.assertNotEqual(
            reminder.fingerprint("grep", {"pattern": "x", "path": "src"}),
            reminder.fingerprint("grep", {"pattern": "y", "path": "src"}),
        )

    def test_tool_name_is_part_of_the_key(self) -> None:
        reminder = RepeatToolReminder(thresholds=[3])
        self.assertNotEqual(
            reminder.fingerprint("read", {"path": "a.py"}),
            reminder.fingerprint("ls", {"path": "a.py"}),
        )

    def test_argument_order_does_not_matter(self) -> None:
        reminder = RepeatToolReminder(thresholds=[3])
        self.assertEqual(
            reminder.fingerprint("grep", {"pattern": "x", "path": "src"}),
            reminder.fingerprint("grep", {"path": "src", "pattern": "x"}),
        )

    def test_non_dict_and_empty_path_do_not_crash(self) -> None:
        reminder = RepeatToolReminder(thresholds=[3])
        for arguments in (None, "text", ["a"], {"path": ""}, {"path": "   "}, {"path": 3}):
            with self.subTest(arguments=arguments):
                self.assertIsInstance(reminder.fingerprint("read", arguments), str)


class RepeatPromptTests(unittest.TestCase):
    """文案是配置：措辞得跟判据说同一件事，别再说「连续」。"""

    def test_prompts_do_not_claim_consecutive(self) -> None:
        """判据不要求连续，文案就不能说连续——说了模型会以为插一次 `ls` 就洗白了。"""
        for name in ("repeat_gentle", "repeat_detailed", "repeat_stop"):
            with self.subTest(name=name):
                text = get_prompt(
                    name,
                    tool_name="read",
                    count="3",
                    arguments="{}",
                )
                self.assertNotIn("连续", text)
                self.assertIn("重复", text)

    def test_stop_notice_reports_the_repeated_tool(self) -> None:
        reminder = RepeatToolReminder(thresholds=[2], stop_at=2)
        trace = [("read", {"path": "a.py"}), ("ls", {"path": "."}), ("read", {"path": "a.py"})]
        top_count(reminder, trace)
        notice = reminder.stop_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.text(), get_prompt("repeat_stop", tool_name="read", count="2"))


if __name__ == "__main__":
    unittest.main()
