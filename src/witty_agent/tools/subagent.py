"""run_subagent / input_subagent：深度 1，结束后会话可续问。"""

from __future__ import annotations

from witty_agent import hooks
from witty_agent.approval import DANGEROUS_TOOLS
from witty_agent.dispatch import guard_spawn
from witty_agent.logging import get_logger
from witty_agent.tools.registry import tool

logger = get_logger("tools.subagent")


@tool
async def run_subagent(agent_id: str, prompt: str) -> str:
    """在同一项目里拉起另一个 Agent 跑一轮。结束后可用 input_subagent 续问。危险操作，必须先批准。

    Args:
        agent_id: 目标 Agent id
        prompt: 交给子 Agent 的任务
    """
    decision = guard_spawn(prompt)
    if not decision.ok:
        logger.info("拒绝平凡子代理 code=%s agent=%s", decision.code, agent_id)
        return decision.message
    if hooks.subagent_depth >= 1:
        return "subagent depth exceeded (max 1)"
    if hooks.subagent_stream_fn is None:
        raise RuntimeError("未绑定子代理 stream_fn")
    from witty_agent.session import create_agent, create_session

    hooks.subagent_depth += 1
    try:
        child = create_agent(
            hooks.current_project_id or "default_project",
            agent_id,
            root=hooks.current_root,
        )
        session = create_session(
            child,
            workspace_dir=hooks.current_workspace or None,
        )
        result = await session.run(
            prompt,
            stream_fn=hooks.subagent_stream_fn,
            approve=hooks.subagent_approve,
            approval_mode="allow-all" if hooks.subagent_approve is None else None,
        )
        hooks.subagent_sessions[session.session_id] = session
        last = result.messages[-1].text() if result.messages else ""
        logger.info("子代理完成 agent=%s id=%s", agent_id, session.session_id)
        return f"[subagent_id {session.session_id}]\n{last or '(empty subagent result)'}"
    finally:
        hooks.subagent_depth = max(0, hooks.subagent_depth - 1)


@tool
async def input_subagent(subagent_id: str, prompt: str = "") -> str:
    """轮询或续问已结束但仍保留的子代理会话。prompt 为空只返回最近回复。

    Args:
        subagent_id: run_subagent 返回的子会话 id
        prompt: 续问正文；空则只读最近结果
    """
    session = hooks.subagent_sessions.get(subagent_id)
    if session is None:
        return f"unknown subagent_id {subagent_id}"
    if not prompt:
        path = session._store_path()
        from witty_agent.store import load_messages

        messages = load_messages(path)
        last = messages[-1].text() if messages else ""
        return last or "(empty)"
    if hooks.subagent_stream_fn is None:
        raise RuntimeError("未绑定子代理 stream_fn")
    result = await session.run(
        prompt,
        stream_fn=hooks.subagent_stream_fn,
        approve=hooks.subagent_approve,
        approval_mode="allow-all" if hooks.subagent_approve is None else None,
    )
    last = result.messages[-1].text() if result.messages else ""
    return last or "(empty)"


# input_subagent 续问会让子代理再跑工具，按危险工具处理
assert "run_subagent" in DANGEROUS_TOOLS
