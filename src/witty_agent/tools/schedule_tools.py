"""定时任务工具：读写当前 Agent 的 schedule/*.toml。"""

from __future__ import annotations

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.schedule import (
    ScheduleDefinition,
    delete_schedule,
    list_schedule_files,
    parse_instant,
    parse_period,
    write_schedule,
)
from witty_agent.tools.registry import tool


def _ids() -> tuple[str, str]:
    project_id = hooks.current_project_id or "default_project"
    agent_id = hooks.current_agent_id or "default_agent"
    return project_id, agent_id


@tool
def schedule_list() -> str:
    """列出当前 Agent 的定时任务文件及解析结果。"""
    project_id, agent_id = _ids()
    rows = list_schedule_files(project_id, agent_id, root=hooks.current_root)
    if not rows:
        return "(no schedules)"
    lines = []
    for item in rows:
        if not item.ok or item.definition is None:
            lines.append(f"invalid\t{item.error}")
            continue
        definition = item.definition
        period = definition.period or "once"
        end = f"\tend={definition.end_at}" if definition.end_at else ""
        lines.append(
            f"{definition.name}\tenabled={definition.enabled}\tperiod={period}\tstart={definition.start_at}{end}"
        )
    return "\n".join(lines)


@tool
def schedule_write(
    name: str,
    prompt: str,
    start_at: str,
    period: str = "",
    enabled: bool = False,
    workspace: str = "",
    end_at: str = "",
) -> str:
    """写入一条定时任务意图文件。默认关闭。危险操作，必须先批准。

    Args:
        name: 任务短名，与文件名一致
        prompt: 触发时发给 Agent 的正文
        start_at: 首次触发的 ISO 8601 时间
        period: 周期，如 30m / 12h / 7d；空表示一次性
        enabled: 是否启用，默认 false。false 即暂停
        workspace: 新开会话时的工作区；空则用当前工作区
        end_at: 可选结束时间，过点不再触发
    """
    parsed_start = parse_instant(start_at)
    if parsed_start is None:
        raise ValueError("start_at 必须是 ISO 8601")
    period_ms = parse_period(period) if period else None
    if period and period_ms is None:
        raise ValueError("period 必须是 30m / 12h / 7d")
    parsed_end = parse_instant(end_at) if end_at else None
    if end_at and parsed_end is None:
        raise ValueError("end_at 必须是 ISO 8601")
    project_id, agent_id = _ids()
    definition = ScheduleDefinition(
        name=name,
        prompt=prompt,
        enabled=enabled,
        start_at=parsed_start[1],
        start_at_ms=parsed_start[0],
        period=period or None,
        period_ms=period_ms,
        end_at=parsed_end[1] if parsed_end else None,
        end_at_ms=parsed_end[0] if parsed_end else None,
        workspace=workspace or None,
    )
    path = write_schedule(definition, project_id, agent_id, root=hooks.current_root)
    return f"wrote schedule {path}"


@tool
def schedule_delete(name: str) -> str:
    """删除一条定时任务，不再触发。危险操作，必须先批准。

    Args:
        name: 任务短名，与文件名一致
    """
    project_id, agent_id = _ids()
    if delete_schedule(name, project_id, agent_id, root=hooks.current_root):
        return get_prompt("schedule_delete_ok", name=name)
    return get_prompt("schedule_delete_missing", name=name)
