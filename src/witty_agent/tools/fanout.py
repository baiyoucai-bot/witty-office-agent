"""DeepSeek-TUI / CodeWhale RLM 式并行子任务。"""

from __future__ import annotations

import json

from witty_agent import hooks
from witty_agent.dispatch import guard_fanout
from witty_agent.logging import get_logger
from witty_agent.tools.registry import tool

logger = get_logger("tools.fanout")


@tool
async def run_fanout(prompts_json: str) -> str:
    """并行跑多条独立子任务并汇总结果。对照 DeepSeek 社区 RLM fanout。危险操作，必须先批准。

    Args:
        prompts_json: JSON 字符串数组，例如 ["查 A","查 B"]，最多 8 条
    """
    if hooks.subagent_depth >= 1:
        return "fanout not allowed inside a subagent"
    try:
        parsed = json.loads(prompts_json)
    except json.JSONDecodeError as exc:
        raise ValueError("prompts_json 必须是 JSON 数组") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("prompts_json 必须是非空数组")
    decision = guard_fanout([str(item) for item in parsed])
    if not decision.ok:
        logger.info("拒绝平凡 fanout code=%s", decision.code)
        return decision.message
    if hooks.subagent_stream_fn is None:
        raise RuntimeError("未绑定 stream_fn")
    from witty_agent.orchestrator import JobSpec, Orchestrator

    orch = Orchestrator(
        hooks.current_root,
        hooks.subagent_stream_fn,
        approve=hooks.subagent_approve,
    )
    result = await orch.fanout(
        list(decision.tasks),
        JobSpec(
            prompt="",
            kind="fanout",
            project_id=hooks.current_project_id or "default_project",
            agent_id=hooks.current_agent_id or "default_agent",
            workspace=hooks.current_workspace or None,
        ),
    )
    logger.info("fanout 完成 children=%s", len(result.children))
    return result.text or "(empty fanout)"


@tool
def plan_write(body: str) -> str:
    """把编排计划写入当前会话 scratchpad/PLAN.md。

    Args:
        body: 计划正文
    """
    import os
    from pathlib import Path

    scratch = os.environ.get("WITTY_SCRATCHPAD")
    if not scratch:
        raise RuntimeError("未绑定 scratchpad")
    path = Path(scratch) / "PLAN.md"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return f"wrote {path}"


@tool
def plan_read() -> str:
    """读取当前会话 scratchpad/PLAN.md。"""
    import os
    from pathlib import Path

    scratch = os.environ.get("WITTY_SCRATCHPAD")
    if not scratch:
        raise RuntimeError("未绑定 scratchpad")
    path = Path(scratch) / "PLAN.md"
    if not path.is_file():
        return "(no PLAN.md)"
    return path.read_text(encoding="utf-8")
