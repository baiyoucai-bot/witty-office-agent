"""窗口会计的三条守卫：出去的参数也要裁、不值得裁就别裁、能问到真 token 就别猜。

改前 compaction 只裁「回来的东西」（toolResult），而且是能裁就裁，token 数一律按字符估。
三处各自漏一块：

1. **出去的东西没人管**：`write` 的正文、`apply_patch` 的差异留在 assistant 消息里，
   调用已经跑完了，结果就在下一条，可正文之后每一次请求都照收窗口费。
2. **裁一点也照裁**：每次改写都让 provider 从该条起的 prompt cache 作废。缓存输入比
   未缓存便宜一个数量级，省几百字符不够抵这个代价，是净亏。
3. **只会猜**：字符估算看不到系统提示，也没有 provider 的分词器，一律低估。压缩因此
   偏晚触发，而偏晚的那次就是超窗那次。

三条都是「宁可多留，不可乱裁」的方向：本文件压住的是反方向——裁掉不该裁的（当前步、
待办清单）、把真读数换回估算值。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from witty_agent.compaction import (
    COMPACTION_CHECKPOINT_SOURCE,
    CompactionSettings,
    estimate_tokens,
    measured_prefix,
    prune_tool_call_args,
    prune_tool_results,
    settings_from_runtime,
    total_tokens,
)
from witty_agent.prompts import get_prompt
from witty_agent.runtime import compaction_settings
from witty_agent.types import AgentMessage, TextBlock, ToolCallBlock, Usage

BIG = "x" * 9000


def call(name: str, **arguments) -> AgentMessage:
    return AgentMessage(
        role="assistant",
        content=[TextBlock(text="ok"), ToolCallBlock(id="c1", name=name, arguments=arguments)],
    )


def result(name: str, body: str) -> AgentMessage:
    return AgentMessage(role="toolResult", content=body, tool_name=name, tool_call_id="c1")


def user(text: str = "go") -> AgentMessage:
    return AgentMessage(role="user", content=text)


def args_of(message: AgentMessage) -> dict:
    return message.tool_calls()[0].arguments


class ToolCallArgPruneTests(unittest.TestCase):
    cfg = CompactionSettings(tool_call_arg_threshold=1000, tool_call_arg_head=100, tool_call_arg_tail=50)

    def test_older_oversize_argument_is_trimmed(self) -> None:
        messages = [call("write", path="a.py", content=BIG), result("write", "ok"), call("read", path="a.py")]
        out = prune_tool_call_args(messages, self.cfg)
        self.assertLess(len(args_of(out[0])["content"]), 1000)
        self.assertIn(get_prompt("tool_call_arg_pruned"), args_of(out[0])["content"])

    def test_short_arguments_survive_intact(self) -> None:
        """路径和短参数必须原样留着：摘要窗口和「碰过哪些文件」全靠它们认。"""
        messages = [call("write", path="a.py", content=BIG), result("write", "ok"), call("read", path="a.py")]
        out = prune_tool_call_args(messages, self.cfg)
        self.assertEqual(args_of(out[0])["path"], "a.py")

    def test_current_step_is_never_trimmed(self) -> None:
        """当前步的参数是模型正在等结果的那次调用，裁了它就是把手上的活裁掉。"""
        messages = [user(), call("write", path="a.py", content=BIG)]
        out = prune_tool_call_args(messages, self.cfg)
        self.assertEqual(args_of(out[-1])["content"], BIG)

    def test_excluded_tools_are_left_alone(self) -> None:
        cfg = replace(self.cfg, prune_exclude_tools=("todo_write",))
        messages = [call("todo_write", todos=BIG), result("todo_write", "ok"), call("read", path="a.py")]
        self.assertIs(prune_tool_call_args(messages, cfg), messages)

    def test_non_string_arguments_are_ignored(self) -> None:
        messages = [call("bash", timeout=30, background=True), result("bash", "ok"), call("read", path="a")]
        self.assertIs(prune_tool_call_args(messages, self.cfg), messages)

    def test_threshold_zero_disables(self) -> None:
        messages = [call("write", path="a", content=BIG), result("write", "ok"), call("read", path="a")]
        self.assertIs(prune_tool_call_args(messages, replace(self.cfg, tool_call_arg_threshold=0)), messages)

    def test_user_and_tool_result_messages_are_untouched(self) -> None:
        messages = [user(BIG), result("read", BIG), call("read", path="a")]
        self.assertIs(prune_tool_call_args(messages, self.cfg), messages)


class ClearAtLeastTests(unittest.TestCase):
    """省得太少就整体不裁：一次改写换掉的是从该条起的整段 prompt cache。"""

    def test_a_trim_below_the_floor_is_abandoned(self) -> None:
        cfg = CompactionSettings(
            tool_result_threshold=1000,
            tool_result_head=900,
            tool_result_tail=90,
            clear_at_least_chars=5000,
        )
        messages = [result("read", "y" * 1100), user()]
        self.assertIs(prune_tool_results(messages, cfg), messages)

    def test_a_trim_above_the_floor_goes_through(self) -> None:
        cfg = CompactionSettings(
            tool_result_threshold=1000,
            tool_result_head=100,
            tool_result_tail=50,
            clear_at_least_chars=5000,
        )
        messages = [result("read", "y" * 9000), user()]
        out = prune_tool_results(messages, cfg)
        self.assertIsNot(out, messages)
        self.assertLess(len(out[0].text()), 1000)

    def test_savings_add_up_across_messages(self) -> None:
        """判据是这一次改写省下的总量，不是单条。三条各省一点，够了就一起裁。"""
        cfg = CompactionSettings(
            tool_result_threshold=1000,
            tool_result_head=100,
            tool_result_tail=50,
            clear_at_least_chars=3000,
        )
        messages = [result("read", "y" * 1400) for _ in range(3)] + [user()]
        self.assertIsNot(prune_tool_results(messages, cfg), messages)

    def test_zero_floor_keeps_the_old_always_prune_behaviour(self) -> None:
        cfg = CompactionSettings(tool_result_threshold=1000, tool_result_head=100, tool_result_tail=50)
        messages = [result("read", "y" * 1100), user()]
        self.assertIsNot(prune_tool_results(messages, cfg), messages)


class ExcludeToolsTests(unittest.TestCase):
    def test_excluded_result_is_not_pruned(self) -> None:
        """待办清单被裁成头尾是纯负收益：中间那几条待办就是它存在的理由。"""
        cfg = CompactionSettings(tool_result_threshold=1000, prune_exclude_tools=("todo_write",))
        messages = [result("todo_write", BIG), user()]
        self.assertIs(prune_tool_results(messages, cfg), messages)

    def test_other_tools_in_the_same_list_still_prune(self) -> None:
        cfg = CompactionSettings(
            tool_result_threshold=1000,
            tool_result_head=100,
            tool_result_tail=50,
            prune_exclude_tools=("todo_write",),
        )
        messages = [result("todo_write", BIG), result("read", BIG), user()]
        out = prune_tool_results(messages, cfg)
        self.assertEqual(out[0].text(), BIG)
        self.assertLess(len(out[1].text()), 1000)

    def test_shipped_config_protects_the_todo_list(self) -> None:
        """这条是配置的判据，不是代码的：默认配置漏掉待办清单，守卫等于没接。"""
        cfg = settings_from_runtime(compaction_settings())
        self.assertIn("todo_write", cfg.prune_exclude_tools)


class MeasuredPrefixTests(unittest.TestCase):
    """能问到 provider 真读数就不许退回猜。唯一不能信的是压缩检查点之前的读数。"""

    def assistant_with_usage(self, text: str, input_tokens: int, output_tokens: int = 10) -> AgentMessage:
        return AgentMessage(
            role="assistant",
            content=text,
            usage=Usage(input=input_tokens, output=output_tokens),
        )

    def test_no_usage_falls_back_to_the_estimate(self) -> None:
        """从盘上恢复的历史没有 usage（store 把用量单独记行），这条路必须还在。"""
        messages = [user("a" * 400), self.assistant_with_usage("b" * 400, 0, 0)]
        self.assertEqual(measured_prefix(messages), (0, 0))
        self.assertEqual(total_tokens(messages), sum(estimate_tokens(item) for item in messages))

    def test_provider_reading_covers_the_whole_prefix(self) -> None:
        """真读数把系统提示也算进去了，字符估算看不到系统提示，所以一律低估。"""
        messages = [user("a" * 400), self.assistant_with_usage("b" * 400, 50000, 200)]
        self.assertEqual(measured_prefix(messages), (50200, 2))
        self.assertEqual(total_tokens(messages), 50200)

    def test_tail_after_the_reading_is_estimated(self) -> None:
        tail = [user("c" * 800), result("read", "d" * 800)]
        messages = [user("a" * 400), self.assistant_with_usage("b" * 400, 50000, 200), *tail]
        self.assertEqual(total_tokens(messages), 50200 + sum(estimate_tokens(item) for item in tail))

    def test_newest_reading_wins(self) -> None:
        messages = [
            self.assistant_with_usage("a", 10000, 100),
            user("go"),
            self.assistant_with_usage("b", 30000, 100),
        ]
        self.assertEqual(total_tokens(messages), 30100)

    def test_reading_from_before_a_checkpoint_is_not_trusted(self) -> None:
        """压缩改写了头部，检查点之前的读数描述的是压缩前那份记录。

        信它的后果不是估错一点，而是每一轮都报出压缩前的大小，于是每一轮都再压一次。
        所以搜索下界是最近一个检查点，找不到读数就整份按字符估——多估会早压，早压只花钱。
        """
        stale = self.assistant_with_usage("huge history", 90000, 500)
        checkpoint = AgentMessage(role="user", content="summary", source=COMPACTION_CHECKPOINT_SOURCE)
        messages = [stale, checkpoint, user("go"), AgentMessage(role="assistant", content="after")]
        self.assertEqual(measured_prefix(messages), (0, 2))
        self.assertEqual(total_tokens(messages), sum(estimate_tokens(item) for item in messages))
        self.assertLess(total_tokens(messages), 90000)

    def test_a_reading_after_the_checkpoint_is_trusted_again(self) -> None:
        stale = self.assistant_with_usage("huge history", 90000, 500)
        checkpoint = AgentMessage(role="user", content="summary", source=COMPACTION_CHECKPOINT_SOURCE)
        fresh = self.assistant_with_usage("after", 12000, 100)
        self.assertEqual(total_tokens([stale, checkpoint, user(), fresh]), 12100)

    def test_reading_never_regresses_to_a_smaller_estimate(self) -> None:
        """守的就是「不可回退」：一条真读数在手，同一段前缀不许再按字符猜。"""
        messages = [user("a" * 40), self.assistant_with_usage("b" * 40, 60000, 100)]
        self.assertGreater(total_tokens(messages), sum(estimate_tokens(item) for item in messages))


if __name__ == "__main__":
    unittest.main()
