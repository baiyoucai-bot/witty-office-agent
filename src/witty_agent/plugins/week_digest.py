"""近一周日记只读摘要。不进内核循环，不改日记。"""

from __future__ import annotations

from witty_agent.diary import list_diary_days, read_diary
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool


def _clip(text: str, limit: int = 48) -> str:
    raw = " ".join(str(text or "").split())
    if raw.startswith("- "):
        raw = raw[2:]
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def week_digest(days: int = 7) -> str:
    """汇总最近若干天的日记条目：天数、条数和摘录。"""
    span = max(1, min(int(days or 7), 31))
    listed = list_diary_days(limit=span)
    if not listed:
        return get_prompt("week_digest_empty")
    lines: list[str] = []
    entries = 0
    for day in listed:
        bullets = [line for line in read_diary(day).splitlines() if line.startswith("- ")]
        if not bullets:
            continue
        entries += len(bullets)
        preview = "；".join(_clip(item) for item in bullets[:4])
        extra = f" · +{len(bullets) - 4}" if len(bullets) > 4 else ""
        lines.append(
            get_prompt(
                "week_digest_day",
                day=day,
                count=str(len(bullets)),
                body=preview + extra,
            )
        )
    if not lines:
        return get_prompt("week_digest_empty")
    return get_prompt(
        "week_digest_report",
        days=str(len(listed)),
        entries=str(entries),
        rows="\n".join(lines),
    )


register_tool(
    ToolSpec(
        name="week_digest",
        description=get_prompt("tool_desc_week_digest"),
        parameters={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": get_prompt("week_digest_param_days"),
                }
            },
            "additionalProperties": False,
        },
        func=week_digest,
    )
)
