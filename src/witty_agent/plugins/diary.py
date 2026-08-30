"""日记业务工具：按日落盘并回写时间线，不进内核包。"""

from __future__ import annotations

from typing import Any

from witty_agent.diary import append_diary, list_diary_days, read_diary
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool


def diary_write(text: str, day: str = "") -> str:
    path = append_diary(text, day=day or None, kind="note")
    return path or get_prompt("diary_skipped")


def diary_read(day: str = "") -> str:
    body = read_diary(day or None)
    return body or get_prompt("diary_empty_day")


def diary_list(limit: int = 14) -> str:
    days = list_diary_days(limit=max(1, min(int(limit or 14), 60)))
    return "\n".join(days) if days else get_prompt("diary_empty")


def _spec(name: str, func: Any, properties: dict[str, Any], required: list[str] | None = None) -> None:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    register_tool(
        ToolSpec(
            name=name,
            description=get_prompt(f"tool_desc_{name}"),
            parameters=parameters,
            func=func,
        )
    )


_spec(
    "diary_write",
    diary_write,
    {
        "text": {"type": "string", "description": get_prompt("diary_param_text")},
        "day": {"type": "string", "description": get_prompt("diary_param_day")},
    },
    ["text"],
)
_spec(
    "diary_read",
    diary_read,
    {"day": {"type": "string", "description": get_prompt("diary_param_day")}},
)
_spec(
    "diary_list",
    diary_list,
    {"limit": {"type": "integer", "description": get_prompt("diary_param_limit")}},
)
