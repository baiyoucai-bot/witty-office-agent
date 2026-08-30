"""时间上下文：合格步骤把带时区的时钟读数记进会话日志。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from witty_agent.prompts import get_prompt
from witty_agent.runtime import time_context_settings
from witty_agent.session_log import SessionLog
from witty_agent.types import AgentMessage


def format_duration(elapsed_ms: int) -> str:
    seconds = max(0, elapsed_ms) // 1000
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def format_timestamp(now: datetime, zone: str) -> str:
    stamp = now.isoformat(timespec="seconds")
    return f"{stamp} ({zone})"


def clock_now(*, now: datetime | None = None, time_zone: str | None = None) -> dict[str, object]:
    settings = time_context_settings()
    zone = time_zone or str(settings["time_zone"])
    clock = now or datetime.now(ZoneInfo(zone))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo(zone))
    weekday = clock.strftime("%A")
    return {
        "timestamp": format_timestamp(clock, zone),
        "zone": zone,
        "iso": clock.isoformat(timespec="seconds"),
        "date": clock.date(),
        "weekday": weekday,
        "weekday_zh": get_prompt(f"host_weekday_{weekday.lower()}"),
    }


def latest_injection_ms(log: SessionLog) -> int | None:
    for event in reversed(log.events):
        if event.type == "user/message" and event.data.get("source") == "plugin:time-context":
            return event.time_ms
    return None


def preceding_visible_ms(log: SessionLog) -> int | None:
    for event in reversed(log.events):
        if event.type in {"user/message", "assistant/message", "tool/result"}:
            if event.type == "user/message" and event.data.get("source") == "plugin:time-context":
                continue
            return event.time_ms
    return None


def maybe_inject(
    log: SessionLog,
    *,
    now: datetime | None = None,
    interval_ms: int | None = None,
    time_zone: str | None = None,
) -> AgentMessage | None:
    settings = time_context_settings()
    if not settings["enabled"]:
        return None
    zone = time_zone or str(settings["time_zone"])
    refresh = settings["refresh_interval_ms"] if interval_ms is None else interval_ms
    clock = now or datetime.now(ZoneInfo(zone))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo(zone))
    now_ms = int(clock.timestamp() * 1000)
    last = latest_injection_ms(log)
    if refresh > 0 and last is not None and now_ms - last < refresh:
        return None
    turn, step = log.turn_and_step()
    if turn <= 0:
        turn = 1
    step = max(step, 1)
    previous = preceding_visible_ms(log)
    elapsed = format_duration(now_ms - previous) if previous is not None else "unavailable"
    key = "time_context_step1" if step == 1 else "time_context_later"
    text = get_prompt(
        key,
        turn=str(turn),
        step=str(step),
        timestamp=format_timestamp(clock, zone),
        zone=zone,
        elapsed=elapsed,
    )
    return AgentMessage(role="user", content=text, source="plugin:time-context")
