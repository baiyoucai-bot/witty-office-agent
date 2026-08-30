from __future__ import annotations

import json
import unittest

from witty_agent.dispatch import (
    Allocation,
    allocation_hint,
    assess_fanout,
    assess_subagent,
    guard_fanout,
    guard_spawn,
    is_cheap_lookup,
    is_chat_turn,
    is_idle_prompt,
    is_share_prompt,
    recommend,
)
from witty_agent.prompts import get_prompt
from witty_agent.tools.fanout import run_fanout
from witty_agent.tools.subagent import run_subagent


class DispatchPolicyTests(unittest.TestCase):
    def test_refuses_cheap_lookup_and_empty(self) -> None:
        empty = assess_subagent("   ")
        self.assertFalse(empty.ok)
        self.assertEqual(empty.action, "serial")
        self.assertEqual(empty.code, "empty")
        cheap = assess_subagent("read foo.py")
        self.assertFalse(cheap.ok)
        self.assertEqual(cheap.code, "cheap_lookup")
        self.assertIn("串行一步", cheap.message)
        listed = assess_subagent("ls src")
        self.assertEqual(listed.code, "cheap_lookup")
        path = assess_subagent("./src/witty_agent/loop.py")
        self.assertEqual(path.code, "cheap_lookup")
        zh = assess_subagent("读一下 config/runtime.toml")
        self.assertEqual(zh.code, "cheap_lookup")

    def test_read_plus_words_is_not_cheap(self) -> None:
        self.assertTrue(assess_subagent("read the loop and summarize").ok)
        self.assertTrue(assess_subagent("read carefully").ok)
        self.assertTrue(assess_subagent("type hint for the allocator").ok)
        self.assertTrue(assess_subagent("看一下 整个认证模块并出风险").ok)
        self.assertEqual(assess_subagent("read foo.py").code, "cheap_lookup")
        self.assertEqual(assess_subagent("type notes.md").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read README").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read LICENSE").code, "cheap_lookup")
        self.assertEqual(assess_subagent("读一下 README").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read TODO").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read CONTRIBUTING").code, "cheap_lookup")
        self.assertEqual(assess_subagent("读一下 NOTICE").code, "cheap_lookup")
        self.assertEqual(assess_subagent("再读一下 README").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read foo.py again").code, "cheap_lookup")
        self.assertEqual(assess_subagent("then ls src").code, "cheap_lookup")
        self.assertEqual(assess_subagent("继续列出 src").code, "cheap_lookup")
        self.assertTrue(assess_subagent("再看一下 整个认证模块并出风险").ok)
        self.assertTrue(assess_subagent("read TODO list and prioritize").ok)
        self.assertEqual(assess_subagent("read foo.py and summarize").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read foo.py then summarize").code, "cheap_lookup")
        self.assertEqual(assess_subagent("读一下 note.txt 并总结").code, "cheap_lookup")
        self.assertTrue(assess_subagent("read foo.py and write summary.md").ok)
        cheap_again = allocation_hint("再读一下 README")
        self.assertIsNotNone(cheap_again)
        self.assertIn("查找", cheap_again.text())

    def test_batch_paths_are_cheap_and_block_spawn(self) -> None:
        self.assertEqual(assess_subagent("read README.md and LICENSE").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read foo.py and bar.py").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read foo.py bar.py").code, "cheap_lookup")
        self.assertEqual(assess_subagent("读一下 a.txt 和 b.txt").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read foo.py, bar.py, and baz.py").code, "cheap_lookup")
        self.assertEqual(assess_subagent("read foo.py and summarize").code, "cheap_lookup")
        self.assertTrue(assess_subagent("read the loop and summarize").ok)
        hint = allocation_hint("read README.md and LICENSE")
        self.assertIsNotNone(hint)
        self.assertEqual(hint.source, "plugin:dispatch-hint")
        self.assertEqual(
            hint.text(),
            get_prompt(
                "dispatch_hint_serial_batch",
                count="2",
                paths="README.md, LICENSE",
            ),
        )
        self.assertIn("批量查找", hint.text())
        blocked = guard_spawn(
            "review the auth module and report risks",
            parent_prompt="read README.md and LICENSE",
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.code, "stay_serial")
        fan = assess_fanout(["read a.py and b.py", "read c.py and d.py"])
        self.assertFalse(fan.ok)
        self.assertEqual(fan.code, "all_cheap")

    def test_refuses_echo_of_parent_prompt(self) -> None:
        decision = assess_subagent(
            "please review the auth module",
            parent_prompt="Please review the auth module",
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "echo_parent")
        self.assertIn("复述", decision.message)
        near = assess_subagent(
            "review auth and list risks",
            parent_prompt="review the auth module and report risks",
        )
        self.assertFalse(near.ok)
        self.assertEqual(near.code, "echo_parent")

    def test_cheap_parent_blocks_spawn_even_for_isolated_job(self) -> None:
        decision = guard_spawn(
            "review the auth module and report risks",
            parent_prompt="read foo.py",
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "stay_serial")
        fan = guard_fanout(
            ["review auth and list risks", "review billing and list risks"],
            parent_prompt="read foo.py",
        )
        self.assertFalse(fan.ok)
        self.assertEqual(fan.code, "stay_serial")
        isolated = guard_spawn("review the auth module and report risks", parent_prompt="delegate")
        self.assertTrue(isolated.ok)
        split = guard_fanout(
            ["review auth and list risks", "review billing and list risks"],
            parent_prompt="review auth and list risks\nreview billing and list risks",
        )
        self.assertTrue(split.ok)

    def test_allows_isolated_multi_step_job(self) -> None:
        decision = assess_subagent("review the auth module and report risks")
        self.assertTrue(decision.ok)
        self.assertEqual(decision.action, "subagent")
        self.assertEqual(decision.tasks, ("review the auth module and report risks",))
        # existing session tests use short but non-lookup prompts
        self.assertTrue(assess_subagent("say hi").ok)
        self.assertTrue(assess_subagent("first").ok)

    def test_fanout_needs_two_distinct_nontrivial_jobs(self) -> None:
        ok = assess_fanout(["inspect A", "inspect B", "inspect C"])
        self.assertTrue(ok.ok)
        self.assertEqual(ok.action, "fanout")
        self.assertEqual(len(ok.tasks), 3)
        dups = assess_fanout(["inspect A", "inspect A", "  inspect A  "])
        self.assertFalse(dups.ok)
        self.assertEqual(dups.code, "too_few")
        cheap = assess_fanout(["read a.py", "ls src", "grep foo"])
        self.assertFalse(cheap.ok)
        self.assertEqual(cheap.code, "all_cheap")
        mixed = assess_fanout(["read a.py", "review auth and list risks"])
        self.assertFalse(mixed.ok)
        self.assertEqual(mixed.code, "too_few")

    def test_recommend_defaults_to_serial(self) -> None:
        serial = recommend("read the loop and summarize")
        self.assertEqual(serial.action, "serial")
        self.assertTrue(serial.ok)
        fan = recommend(
            "split work",
            tasks=["review auth and list risks", "review billing and list risks"],
        )
        self.assertEqual(fan.action, "fanout")
        self.assertTrue(fan.ok)

    def test_allocation_hint_serial_cheap_and_fanout(self) -> None:
        self.assertTrue(is_idle_prompt("  "))
        self.assertTrue(is_idle_prompt("你好"))
        self.assertTrue(is_idle_prompt("say hi"))
        self.assertTrue(is_idle_prompt("好的，谢谢"))
        self.assertTrue(is_idle_prompt("谢谢你"))
        self.assertTrue(is_idle_prompt("明白了"))
        self.assertTrue(is_idle_prompt("got it"))
        self.assertTrue(is_idle_prompt("ok thanks!"))
        self.assertFalse(is_idle_prompt("做一份幻灯片"))
        self.assertFalse(is_idle_prompt("read foo.py"))
        self.assertFalse(is_idle_prompt("好的，帮我读 note.txt"))
        self.assertFalse(is_idle_prompt("谢谢，继续改 session.py"))
        self.assertTrue(is_share_prompt("我爱吃冰淇淋"))
        self.assertTrue(is_share_prompt("我喜欢简短回复"))
        self.assertTrue(is_chat_turn("我爱吃冰淇淋"))
        self.assertFalse(is_share_prompt("我是谁"))
        self.assertFalse(is_share_prompt("做一份幻灯片"))
        self.assertIsNone(allocation_hint("我爱吃冰淇淋"))
        self.assertTrue(is_cheap_lookup("read foo.py"))
        self.assertTrue(is_cheap_lookup("read note.txt"))
        self.assertTrue(is_cheap_lookup("read foo.py then summarize"))
        self.assertTrue(is_cheap_lookup("读一下 note.txt 并总结"))
        self.assertFalse(is_cheap_lookup("read foo.py and write summary.md"))
        self.assertFalse(is_cheap_lookup("做一份幻灯片"))
        self.assertFalse(is_cheap_lookup("review the auth module and report risks"))
        self.assertIsNone(allocation_hint("  "))
        self.assertIsNone(allocation_hint("你好"))
        self.assertIsNone(allocation_hint("thanks"))
        self.assertIsNone(allocation_hint("say hi"))
        self.assertIsNone(allocation_hint("好的，谢谢"))
        cheap = allocation_hint("read foo.py")
        self.assertIsNotNone(cheap)
        self.assertEqual(cheap.source, "plugin:dispatch-hint")
        self.assertIn("查找", cheap.text())
        serial = allocation_hint("review the auth module and report risks")
        self.assertEqual(serial.source, "plugin:dispatch-hint")
        self.assertIn("自己完成", serial.text())
        self.assertNotIn("查找", serial.text())
        fan = allocation_hint(
            "review auth and list risks\nreview billing and list risks"
        )
        self.assertIn("并行派发", fan.text())
        self.assertIn("review auth and list risks", fan.text())
        self.assertIn("review billing and list risks", fan.text())

    def test_refuse_messages_come_from_prompts(self) -> None:
        text = Allocation("serial", False, "cheap_lookup").message
        self.assertEqual(
            text,
            get_prompt(
                "dispatch_refuse_trivial",
                reason=get_prompt("dispatch_reason_cheap_lookup"),
            ),
        )


class DispatchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_subagent_returns_refuse_without_spawn(self) -> None:
        from witty_agent import hooks

        hooks.reset()
        text = await run_subagent("worker", "read foo.py")
        self.assertIn("拒绝派发", text)
        self.assertFalse(hooks.subagent_sessions)
        batch = await run_subagent("worker", "read README.md and LICENSE")
        self.assertIn("拒绝派发", batch)
        self.assertFalse(hooks.subagent_sessions)

    async def test_run_fanout_returns_refuse_without_spawn(self) -> None:
        from witty_agent import hooks

        hooks.reset()
        text = await run_fanout(json.dumps(["read a.py", "ls src"]))
        self.assertIn("拒绝并行派发", text)
        self.assertFalse(hooks.subagent_sessions)

    async def test_session_injects_allocation_hint_once(self) -> None:
        import tempfile
        from pathlib import Path

        from witty_agent.llm import ScriptedLLM, text_reply
        from witty_agent.session import create_agent, create_session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            chat = await session.run(
                "你好",
                stream_fn=ScriptedLLM([text_reply("hi")]),
                approval_mode="allow-all",
            )
            self.assertEqual(
                [item for item in chat.messages if item.source == "plugin:dispatch-hint"],
                [],
            )
            result = await session.run(
                "review the auth module and report risks",
                stream_fn=ScriptedLLM([text_reply("ok")]),
                approval_mode="allow-all",
            )
            hints = [item for item in result.messages if item.source == "plugin:dispatch-hint"]
            self.assertEqual(len(hints), 1)
            self.assertIn("自己完成", hints[0].text())
            kinds = [event.type for event in session.log.events]
            self.assertIn("turn/dispatch-hint", kinds)


if __name__ == "__main__":
    unittest.main()
