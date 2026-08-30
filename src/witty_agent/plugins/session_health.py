"""会话只读体检：社区常见的 transcript / session-health，不进内核循环。"""

from __future__ import annotations

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.session_log import fold_plan_mode, fold_todos, unpaired_call_results

_REPAIR_CODES = {"TOOL_NOT_STARTED", "TOOL_OUTCOME_UNKNOWN"}
from witty_agent.tools.registry import ToolSpec, register_tool


def session_health() -> str:
    log = hooks.session_log
    if log is None:
        return get_prompt("session_health_no_session")
    events = list(log.events)
    unpaired = unpaired_call_results(events)
    unpaired_text = (
        ", ".join(
            f"{item.data.get('tool_name') or '?'}#{item.data.get('tool_call_id')}"
            for item in unpaired
        )
        or get_prompt("session_health_none")
    )
    repairs = [
        f"{item.data.get('code')}#{item.data.get('tool_call_id')}"
        for item in events
        if item.type == "tool/result"
        and item.data.get("code") in _REPAIR_CODES
    ]
    todos = fold_todos(events) or []
    open_todos = [item["content"] for item in todos if item.get("status") != "completed"]
    return get_prompt(
        "session_health_report",
        turn_state=get_prompt("session_health_open" if log.has_open_turn() else "session_health_closed"),
        count=str(len(events)),
        unpaired=unpaired_text,
        repairs=", ".join(repairs) or get_prompt("session_health_none"),
        todos=", ".join(open_todos) if open_todos else get_prompt("session_health_none"),
        plan=get_prompt("session_health_on" if fold_plan_mode(events) else "session_health_off"),
    )


register_tool(
    ToolSpec(
        name="session_health",
        description=get_prompt("tool_desc_session_health"),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        func=session_health,
    )
)
