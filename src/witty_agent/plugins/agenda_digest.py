"""日程只读摘要：下次触发、暂停、到期。不进内核循环，不另起调度器。"""

from __future__ import annotations

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.schedule import Scheduler, list_schedule_files, time_ms
from witty_agent.tools.registry import ToolSpec, register_tool


def _clip(text: str, limit: int = 40) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def agenda_digest() -> str:
    """列出当前 Agent 的定时任务摘要：启用与否和下次触发。"""
    project_id = hooks.current_project_id or "default_project"
    agent_id = hooks.current_agent_id or "default_agent"
    root = hooks.current_root
    rows = list_schedule_files(project_id, agent_id, root=root)
    if not rows:
        return get_prompt("agenda_digest_empty")
    tracker = Scheduler(root, now_ms=time_ms) if root is not None else None
    lines: list[str] = []
    live = 0
    paused = 0
    for item in rows:
        if not item.ok or item.definition is None:
            lines.append(get_prompt("agenda_digest_invalid", error=item.error or "?"))
            continue
        definition = item.definition
        if definition.enabled:
            live += 1
        else:
            paused += 1
        nxt = None
        if tracker is not None:
            nxt = tracker.next_fire_iso(project_id, agent_id, definition)
        if not definition.enabled:
            when = get_prompt("agenda_digest_paused")
        elif nxt:
            when = nxt
        else:
            when = get_prompt("agenda_digest_none")
        lines.append(
            get_prompt(
                "agenda_digest_item",
                job=definition.name,
                period=definition.period or get_prompt("agenda_digest_once"),
                when=when,
                body=_clip(definition.prompt),
            )
        )
    return get_prompt(
        "agenda_digest_report",
        count=str(len(rows)),
        live=str(live),
        paused=str(paused),
        rows="\n".join(lines),
    )


register_tool(
    ToolSpec(
        name="agenda_digest",
        description=get_prompt("tool_desc_agenda_digest"),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        func=agenda_digest,
    )
)
