"""会话循环：/loop 把「对本会话反复续做」写成一条定时意图，由 serve 进程内 tick 触发。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from witty_agent.commands import CommandResult
from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.schedule import (
    MIN_PERIOD_MS,
    ScheduleDefinition,
    Scheduler,
    delete_schedule,
    list_schedule_files,
    parse_instant,
    parse_period,
    write_schedule,
)

if TYPE_CHECKING:
    from witty_agent.session import Session

logger = get_logger("loop_control")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class LoopRequest:
    action: str
    period: str | None = None
    period_ms: int | None = None
    end_at: str | None = None
    end_at_ms: int | None = None
    prompt: str = ""
    error_key: str | None = None


def loop_schedule_name(session_id: str) -> str:
    slug = _SLUG_RE.sub("-", (session_id or "").lower()).strip("-")
    if not slug:
        slug = "session"
    return f"loop-{slug[:48]}"


def parse_loop_args(rest: str, *, now: datetime | None = None) -> LoopRequest:
    text = (rest or "").strip()
    if not text or text.lower() in {"status", "show"}:
        return LoopRequest(action="status")
    if text.lower() in {"off", "stop", "cancel"}:
        return LoopRequest(action="stop")
    tokens = text.split()
    period = tokens[0]
    period_ms = parse_period(period)
    if period_ms is None or period_ms < MIN_PERIOD_MS:
        return LoopRequest(action="error", error_key="loop_invalid_period")
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    idx = 1
    end_at: str | None = None
    end_at_ms: int | None = None
    if idx < len(tokens) and tokens[idx].lower() == "until":
        idx += 1
        if idx >= len(tokens):
            return LoopRequest(action="error", error_key="loop_usage")
        until = tokens[idx]
        idx += 1
        duration_ms = parse_period(until)
        if duration_ms is not None:
            end = stamp + timedelta(milliseconds=duration_ms)
            end_at_ms = int(end.timestamp() * 1000)
            end_at = end.isoformat()
        else:
            parsed = parse_instant(until)
            if parsed is None:
                return LoopRequest(action="error", error_key="loop_invalid_until")
            end_at_ms, end_at = parsed[0], parsed[1]
        if end_at_ms is not None and end_at_ms <= int(stamp.timestamp() * 1000):
            return LoopRequest(action="error", error_key="loop_invalid_until")
        if end_at_ms is not None and end_at_ms - int(stamp.timestamp() * 1000) < period_ms:
            return LoopRequest(action="error", error_key="loop_until_too_soon")
    body = " ".join(tokens[idx:]).strip()
    return LoopRequest(
        action="start",
        period=period,
        period_ms=period_ms,
        end_at=end_at,
        end_at_ms=end_at_ms,
        prompt=body,
    )


def apply_loop(session: Session, rest: str, *, now: datetime | None = None) -> CommandResult:
    request = parse_loop_args(rest, now=now)
    if request.action == "error":
        return CommandResult(kind="error", text=get_prompt(request.error_key or "loop_usage"))
    if request.action == "status":
        return CommandResult(kind="success", text=_status_text(session))
    if request.action == "stop":
        return CommandResult(kind="success", text=_stop_loop(session))
    return CommandResult(kind="success", text=_start_loop(session, request, now=now))


def _ids(session: Session) -> tuple[str, str, Path | None]:
    project_id = getattr(getattr(session.agent, "project", None), "project_id", None) or DEFAULT_PROJECT_ID
    agent_id = getattr(getattr(session.agent, "record", None), "agent_id", None) or DEFAULT_AGENT_ID
    root = getattr(session.agent, "root", None)
    return project_id, agent_id, root


def _find_loop(session: Session):
    project_id, agent_id, root = _ids(session)
    name = loop_schedule_name(session.session_id)
    for item in list_schedule_files(project_id, agent_id, root=root):
        if item.ok and item.definition is not None and item.definition.name == name:
            return item.definition
    return None


def _status_text(session: Session) -> str:
    definition = _find_loop(session)
    if definition is None or not definition.enabled:
        return get_prompt("loop_status_idle")
    project_id, agent_id, root = _ids(session)
    next_fire = "—"
    if root is not None:
        tracker = Scheduler(root)
        stamp = tracker.next_fire_iso(project_id, agent_id, definition)
        if stamp:
            next_fire = stamp
    end_at = definition.end_at or get_prompt("loop_no_end")
    return get_prompt(
        "loop_status_on",
        period=definition.period or "once",
        next_fire=next_fire,
        end_at=end_at,
    )


def _stop_loop(session: Session) -> str:
    project_id, agent_id, root = _ids(session)
    name = loop_schedule_name(session.session_id)
    if delete_schedule(name, project_id, agent_id, root=root):
        logger.info("停止会话循环 session=%s name=%s", session.session_id, name)
        return get_prompt("loop_stopped")
    return get_prompt("loop_status_idle")


def _start_loop(session: Session, request: LoopRequest, *, now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    start_ms = int(stamp.timestamp() * 1000)
    start_at = stamp.isoformat()
    body = (request.prompt or "").strip() or get_prompt("loop_default_prompt")
    project_id, agent_id, root = _ids(session)
    name = loop_schedule_name(session.session_id)
    definition = ScheduleDefinition(
        name=name,
        prompt=body,
        enabled=True,
        start_at=start_at,
        start_at_ms=start_ms,
        period=request.period,
        period_ms=request.period_ms,
        end_at=request.end_at,
        end_at_ms=request.end_at_ms,
        session_id=session.session_id,
        workspace=str(session.workspace_dir),
    )
    write_schedule(definition, project_id, agent_id, root=root)
    if root is not None:
        Scheduler(root).arm(project_id, agent_id, name, seen_ms=start_ms - 1)
    logger.info(
        "开启会话循环 session=%s period=%s end=%s",
        session.session_id,
        request.period,
        request.end_at or "",
    )
    end_at = request.end_at or get_prompt("loop_no_end")
    return get_prompt("loop_started", period=request.period or "", end_at=end_at)


def next_loop_fire_iso(session: Session) -> str | None:
    definition = _find_loop(session)
    if definition is None or not definition.enabled:
        return None
    project_id, agent_id, root = _ids(session)
    if root is None:
        return None
    return Scheduler(root).next_fire_iso(project_id, agent_id, definition)
