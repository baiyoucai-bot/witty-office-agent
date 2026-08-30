"""危险任务必须批准。读类默认可自动放行。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from witty_agent.logging import get_logger

ApprovalDecision = Literal["allow", "deny"]
ApprovalMode = Literal["always-ask", "allow-all", "deny-all", "read-only"]
APPROVAL_MODES = frozenset({"always-ask", "allow-all", "deny-all", "read-only"})
ApproveFn = Callable[[str, str, dict], Awaitable[ApprovalDecision]]

DANGEROUS_TOOLS = frozenset(
    {
        "apply_patch",
        "write",
        "edit",
        "bash",
        "exec_command",
        "python_repl",
        "run_subagent",
        "input_subagent",
        "input_command",
        "schedule_write",
        "schedule_delete",
        "run_fanout",
        "web_fetch",
        "job_kill",
        "mail_send",
        "mail_save",
        "sql_export",
    }
)

logger = get_logger("approval")


def is_dangerous(tool_name: str) -> bool:
    return tool_name in DANGEROUS_TOOLS


async def decide_approval(
    mode: ApprovalMode | str,
    tool_name: str,
    tool_call_id: str,
    args: dict,
    approve: ApproveFn | None,
) -> ApprovalDecision:
    if mode == "allow-all":
        return "allow"
    if mode == "deny-all":
        return "deny"
    if mode == "read-only" and is_dangerous(tool_name):
        return "deny"
    if not is_dangerous(tool_name):
        return "allow"
    if approve is None:
        logger.warning("危险工具无审批回调，已拒绝 name=%s", tool_name)
        return "deny"
    decision = await approve(tool_name, tool_call_id, args)
    logger.info(
        "审批结果 name=%s id=%s decision=%s", tool_name, tool_call_id, decision
    )
    return decision
