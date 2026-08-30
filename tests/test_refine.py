"""/refine 的判据：哪条路径能把「编造的经验」写进 Agent 状态。

自进化最贵的失败不是学不到，是**错学**（misevolution）：一条幻觉进了系统提示，之后
每轮都在放大。所以这里盯的全是放宽方向——证据对不上必须丢、义务红着必须拒、role 帽
必须挡、undo 必须真的把三类资产一起拉回去。收紧方向（多丢一条、多拒一次）只损失一次
沉淀机会，不单独立测。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import witty_agent.refine as refine_mod
from witty_agent.layout import criteria_dir, snapshots_dir
from witty_agent.memory import workspace_memory_key
from witty_agent.prompts import get_prompt
from witty_agent.refine import (
    RefinePlan,
    apply_refinements,
    parse_refine_reply,
    run_refine,
    undo_refine,
)
from witty_agent.session import create_agent, create_session
from witty_agent.state.agent_state import ROLE_MAX_CHARS, load_agent_state
from witty_agent.types import AgentMessage, ModelRef
from witty_agent.verify import Obligation, ObligationLedger

TRANSCRIPT = "assistant: 统一用 uv run 跑测试，直接 python 会缺依赖 [calls: bash]"


def item_block(
    kind: str = "memory",
    title: str = "跑测试的正确姿势",
    name: str = "uv-run-tests",
    evidence: str = "统一用 uv run 跑测试",
    body: str = "测试一律 uv run python -m unittest，别用系统 python。",
) -> str:
    return f"== item\nkind: {kind}\ntitle: {title}\nname: {name}\nevidence: {evidence}\nbody:\n{body}\n"


def run(coro):
    return asyncio.run(coro)


class ParserTests(unittest.TestCase):
    """复盘员的回复是自由文本。读不懂或对不上的每一种形状都必须落到「不沉淀」。"""

    def test_three_kinds_parse(self) -> None:
        reply = (
            item_block(kind="role", name="")
            + item_block(kind="memory", name="uv-tips")
            + item_block(kind="skill", name="uv-test-flow")
        )
        plan = parse_refine_reply(reply, TRANSCRIPT)
        self.assertEqual([item.kind for item in plan.items], ["role", "memory", "skill"])
        self.assertFalse(plan.dropped)

    def test_hallucinated_evidence_is_dropped(self) -> None:
        plan = parse_refine_reply(item_block(evidence="这段话轨迹里根本没有出现过"), TRANSCRIPT)
        self.assertFalse(plan.items)
        self.assertEqual(plan.dropped[0][1], get_prompt("refine_skip_evidence"))

    def test_short_evidence_is_dropped(self) -> None:
        # "uv" 在轨迹里出现，但两个字符在任何轨迹里都能撞上，等于没验。
        plan = parse_refine_reply(item_block(evidence="uv"), TRANSCRIPT)
        self.assertFalse(plan.items)
        self.assertEqual(plan.dropped[0][1], get_prompt("refine_skip_evidence"))

    def test_evidence_matches_across_whitespace(self) -> None:
        plan = parse_refine_reply(item_block(evidence="统一用  uv run\t跑测试"), TRANSCRIPT)
        self.assertEqual(len(plan.items), 1)

    def test_nothing_reply(self) -> None:
        self.assertTrue(parse_refine_reply("NOTHING", TRANSCRIPT).nothing)
        self.assertTrue(parse_refine_reply("```\nnothing\n```", TRANSCRIPT).nothing)

    def test_garbage_reply_fails_parse(self) -> None:
        self.assertFalse(parse_refine_reply("我觉得这段轨迹很好。", TRANSCRIPT).parsed)
        self.assertFalse(parse_refine_reply("", TRANSCRIPT).parsed)

    def test_code_fenced_reply_parses(self) -> None:
        plan = parse_refine_reply(f"```\n{item_block()}```", TRANSCRIPT)
        self.assertEqual(len(plan.items), 1)

    def test_unknown_kind_is_dropped(self) -> None:
        plan = parse_refine_reply(item_block(kind="prompt"), TRANSCRIPT)
        self.assertEqual(plan.dropped[0][1], get_prompt("refine_skip_kind"))

    def test_empty_body_is_dropped(self) -> None:
        plan = parse_refine_reply(item_block(body=""), TRANSCRIPT)
        self.assertEqual(plan.dropped[0][1], get_prompt("refine_skip_body"))

    def test_memory_without_name_is_dropped(self) -> None:
        plan = parse_refine_reply(item_block(kind="memory", name=""), TRANSCRIPT)
        self.assertEqual(plan.dropped[0][1], get_prompt("refine_skip_slug"))

    def test_role_without_name_is_fine(self) -> None:
        plan = parse_refine_reply(item_block(kind="role", name=""), TRANSCRIPT)
        self.assertEqual(len(plan.items), 1)


def plan_of(*blocks: str) -> RefinePlan:
    return parse_refine_reply("".join(blocks), TRANSCRIPT)


class ApplyTests(unittest.TestCase):
    """先快照后落笔；每类资产落到它声明的位置；applied 为零不留假档。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.agent = create_agent("default_project", "refiner", root=self.root)
        self.record = self.agent.record

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _apply(self, plan: RefinePlan):
        return apply_refinements(
            plan,
            record=self.record,
            workspace_dir=self.workspace,
            root=self.root,
            session_id="s1",
        )

    def _agents_md(self) -> str:
        return (self.record.state_dir / "AGENTS.md").read_text(encoding="utf-8")

    def test_role_lands_in_agents_md_and_replaces_seed(self) -> None:
        result = self._apply(plan_of(item_block(kind="role", name="", body="测试一律 uv run。")))
        self.assertEqual(result.kind, "success")
        text = self._agents_md()
        self.assertIn("测试一律 uv run。", text)
        self.assertNotIn(get_prompt("agent_role_seed").strip(), text)
        from witty_agent.state.agent_state import agent_role_text

        # 注入线也要通：agent_role_text 是系统提示实际读的那条路。
        self.assertIn("测试一律 uv run。", agent_role_text("default_project", "refiner", root=self.root))

    def test_memory_lands_in_workspace_grid(self) -> None:
        result = self._apply(plan_of(item_block(kind="memory", name="uv-tips")))
        self.assertEqual(result.kind, "success")
        from witty_agent.layout import memory_workspace_dir

        path = (
            memory_workspace_dir(
                workspace_memory_key(self.workspace), "default_project", "refiner", root=self.root
            )
            / "uv-tips.md"
        )
        self.assertIn("uv run python -m unittest", path.read_text(encoding="utf-8"))

    def test_skill_becomes_a_draft_on_disk(self) -> None:
        result = self._apply(plan_of(item_block(kind="skill", name="uv-test-flow")))
        self.assertEqual(result.kind, "success")
        from witty_agent.skills import user_skills_dir

        path = user_skills_dir("default_project", "refiner", root=self.root) / "uv-test-flow" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("name: uv-test-flow", text)
        self.assertIn("uv run python -m unittest", text)

    def test_role_cap_refuses_overgrowth(self) -> None:
        (self.record.state_dir / "AGENTS.md").write_text("x" * (ROLE_MAX_CHARS - 10), encoding="utf-8")
        result = self._apply(plan_of(item_block(kind="role", name="", body="很长的新守则" * 10)))
        self.assertEqual(result.kind, "error")
        self.assertEqual(self._agents_md(), "x" * (ROLE_MAX_CHARS - 10))

    def test_existing_skill_is_not_overwritten(self) -> None:
        first = self._apply(plan_of(item_block(kind="skill", name="uv-test-flow", body="第一版")))
        self.assertEqual(first.kind, "success")
        second = self._apply(plan_of(item_block(kind="skill", name="uv-test-flow", body="要覆盖")))
        self.assertEqual(second.kind, "error")
        from witty_agent.skills import user_skills_dir

        path = user_skills_dir("default_project", "refiner", root=self.root) / "uv-test-flow" / "SKILL.md"
        self.assertIn("第一版", path.read_text(encoding="utf-8"))

    def test_max_items_is_enforced(self) -> None:
        original = refine_mod.refine_settings

        def capped():
            table = dict(original())
            table["max_items"] = 1
            return table

        refine_mod.refine_settings = capped
        try:
            result = self._apply(
                plan_of(
                    item_block(kind="memory", name="one"),
                    item_block(kind="memory", name="two"),
                )
            )
        finally:
            refine_mod.refine_settings = original
        self.assertEqual(result.kind, "success")
        self.assertEqual(result.text.count("+ ["), 1)
        self.assertIn(get_prompt("refine_skip_limit", limit="1"), result.text)

    def test_undo_restores_all_three_assets(self) -> None:
        before = self._agents_md()
        result = self._apply(
            plan_of(
                item_block(kind="role", name="", body="新守则"),
                item_block(kind="memory", name="uv-tips"),
                item_block(kind="skill", name="uv-test-flow"),
            )
        )
        self.assertEqual(result.kind, "success")
        self.assertEqual(load_agent_state("default_project", "refiner", root=self.root).version, 2)
        undone = undo_refine(self.record, root=self.root)
        self.assertEqual(undone.kind, "success")
        self.assertEqual(self._agents_md(), before)
        from witty_agent.layout import memory_workspace_dir
        from witty_agent.skills import user_skills_dir

        memory_path = (
            memory_workspace_dir(
                workspace_memory_key(self.workspace), "default_project", "refiner", root=self.root
            )
            / "uv-tips.md"
        )
        skill_path = user_skills_dir("default_project", "refiner", root=self.root) / "uv-test-flow"
        self.assertFalse(memory_path.exists())
        self.assertFalse(skill_path.exists())
        self.assertEqual(load_agent_state("default_project", "refiner", root=self.root).version, 1)

    def test_second_undo_has_nothing_to_undo(self) -> None:
        self._apply(plan_of(item_block(kind="role", name="", body="新守则")))
        undo_refine(self.record, root=self.root)
        again = undo_refine(self.record, root=self.root)
        self.assertEqual(again.kind, "error")
        self.assertEqual(again.text, get_prompt("refine_undo_none"))

    def test_all_skipped_run_does_not_disturb_the_previous_refine(self) -> None:
        """第二次全军覆没的沉淀不许动版本，也不许弄坏第一次的回滚档。"""
        import json

        first = self._apply(plan_of(item_block(kind="skill", name="uv-test-flow")))
        self.assertEqual(first.kind, "success")
        version_before = load_agent_state("default_project", "refiner", root=self.root).version
        result = self._apply(plan_of(item_block(kind="skill", name="uv-test-flow", body="重复名字")))
        self.assertEqual(result.kind, "error")
        self.assertEqual(
            load_agent_state("default_project", "refiner", root=self.root).version, version_before
        )
        snaps = snapshots_dir("default_project", "refiner", root=self.root)
        self.assertFalse((snaps / f"v{version_before}.tar.gz").exists(), "失败那次的快照不该留假档")
        marker = json.loads((snaps / "refine_last.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], 1, "第一次沉淀的回滚档必须还在")
        self.assertEqual(undo_refine(self.record, root=self.root).kind, "success")

    def test_fresh_all_skipped_leaves_no_marker(self) -> None:
        result = self._apply(plan_of(item_block(evidence="轨迹里没有的话")))
        self.assertEqual(result.kind, "error")
        snaps = snapshots_dir("default_project", "refiner", root=self.root)
        self.assertFalse((snaps / "refine_last.json").exists())
        self.assertEqual(load_agent_state("default_project", "refiner", root=self.root).version, 1)


def reviewer(reply: str):
    """记录调用次数的复盘员桩。"""
    calls: list[int] = []

    async def stream(_context) -> AgentMessage:
        calls.append(1)
        return AgentMessage(role="assistant", content=reply, stop_reason="end_turn")

    return stream, calls


HISTORY = [
    AgentMessage(role="user", content="帮我跑测试"),
    AgentMessage(role="assistant", content="统一用 uv run 跑测试，直接 python 会缺依赖"),
]

MODEL = ModelRef(provider="openai", model_id="refine-test")


class RunRefineTests(unittest.TestCase):
    """编排层的否决线：空轨迹、红义务、坏回复，每条都不许碰 Agent 状态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.agent = create_agent("default_project", "refiner", root=self.root)
        self.record = self.agent.record

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _refine(self, stream, history=None):
        return run(
            run_refine(
                stream,
                model=MODEL,
                record=self.record,
                workspace_dir=self.workspace,
                history=HISTORY if history is None else history,
                root=self.root,
                session_id="s1",
            )
        )

    def _ledger(self) -> ObligationLedger:
        return ObligationLedger(
            criteria_dir(
                workspace_memory_key(self.workspace), "default_project", "refiner", root=self.root
            )
        )

    def test_empty_history_refuses(self) -> None:
        stream, calls = reviewer("NOTHING")
        result = self._refine(stream, history=[])
        self.assertEqual(result.text, get_prompt("refine_empty"))
        self.assertEqual(calls, [])

    def test_red_obligation_blocks_distillation(self) -> None:
        self._ledger().record(Obligation(name="always-red", command="exit 1"))
        stream, calls = reviewer(item_block())
        result = self._refine(stream)
        self.assertEqual(result.kind, "error")
        self.assertIn(get_prompt("refine_gate_red", failures="").splitlines()[0], result.text)
        self.assertEqual(calls, [], "义务红着连复盘员都不该请")

    def test_green_obligation_lets_refine_proceed(self) -> None:
        self._ledger().record(Obligation(name="always-green", command="exit 0"))
        stream, calls = reviewer("NOTHING")
        result = self._refine(stream)
        self.assertEqual(result.text, get_prompt("refine_nothing"))
        self.assertEqual(calls, [1])

    def test_happy_path_writes_and_marks(self) -> None:
        stream, _calls = reviewer(item_block(kind="role", name="", body="测试一律 uv run。"))
        result = self._refine(stream)
        self.assertEqual(result.kind, "success")
        text = (self.record.state_dir / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("测试一律 uv run。", text)
        marker = snapshots_dir("default_project", "refiner", root=self.root) / "refine_last.json"
        self.assertTrue(marker.is_file())

    def test_reviewer_error_reply_changes_nothing(self) -> None:
        async def broken(_context) -> AgentMessage:
            return AgentMessage(role="assistant", content="", stop_reason="error")

        before = (self.record.state_dir / "AGENTS.md").read_text(encoding="utf-8")
        result = self._refine(broken)
        self.assertEqual(result.text, get_prompt("refine_unparsed"))
        self.assertEqual((self.record.state_dir / "AGENTS.md").read_text(encoding="utf-8"), before)

    def test_reviewer_exception_changes_nothing(self) -> None:
        async def boom(_context) -> AgentMessage:
            raise RuntimeError("网关挂了")

        result = self._refine(boom)
        self.assertEqual(result.text, get_prompt("refine_unparsed"))

    def test_all_dropped_reports_reasons(self) -> None:
        stream, _calls = reviewer(item_block(evidence="轨迹里没有这句"))
        result = self._refine(stream)
        self.assertEqual(result.kind, "error")
        self.assertIn(get_prompt("refine_skip_evidence"), result.text)


class SessionRefineTests(unittest.TestCase):
    """/refine 从会话入口走通：命令特例、目录里可见、undo 同步可用。"""

    def test_slash_refine_end_to_end(self) -> None:
        from witty_agent.llm import ScriptedLLM, text_reply

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "ws"
                workspace.mkdir()
                agent = create_agent("default_project", "refiner", root=root)
                session = create_session(agent, workspace_dir=workspace)
                await session.run(
                    "你好",
                    stream_fn=ScriptedLLM([text_reply("你好，我在。有活直接说。")]),
                    approval_mode="allow-all",
                )
                reply = item_block(
                    kind="role",
                    name="",
                    evidence="你好，我在。有活直接说",
                    body="收到问候直接应答，不空跑工具。",
                )
                result = await session.run(
                    "/refine",
                    stream_fn=ScriptedLLM([text_reply(reply)]),
                    approval_mode="allow-all",
                )
                self.assertIn("收到问候直接应答", (agent.record.state_dir / "AGENTS.md").read_text(encoding="utf-8"))
                self.assertTrue(result.messages)
                undone = await session.run(
                    "/refine undo",
                    stream_fn=ScriptedLLM([text_reply("不该请模型")]),
                    approval_mode="allow-all",
                )
                self.assertIn(
                    get_prompt("refine_undo_ok", version="1"),
                    "".join(item.text() for item in undone.messages),
                )
                self.assertNotIn(
                    "收到问候直接应答",
                    (agent.record.state_dir / "AGENTS.md").read_text(encoding="utf-8"),
                )

        run(scenario())

    def test_refine_shows_in_command_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("default_project", "refiner", root=root)
            session = create_session(agent, workspace_dir=workspace)
            names = [item["name"] for item in session.slash_commands()]
            self.assertIn("refine", names)

    def test_sync_dispatch_only_handles_undo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("default_project", "refiner", root=root)
            session = create_session(agent, workspace_dir=workspace)
            hint = session.dispatch_command("/refine")
            self.assertEqual(hint.text, get_prompt("refine_needs_model"))
            none = session.dispatch_command("/refine undo")
            self.assertEqual(none.text, get_prompt("refine_undo_none"))


if __name__ == "__main__":
    unittest.main()
