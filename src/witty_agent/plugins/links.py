"""链接库业务工具：独立于九宫格，不进内核包。"""

from __future__ import annotations

from typing import Any

from witty_agent.links import (
    habit_summary,
    harvest_links,
    render_links,
    resolve_mention,
    search_links,
    upsert_link,
)
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool


def link_add(url: str, title: str = "", intent: str = "", note: str = "", alias: str = "") -> str:
    item = upsert_link(
        url,
        title=title,
        intent=intent,
        note=note,
        alias=alias,
        source="tool",
    )
    return get_prompt(
        "link_saved",
        url=str(item.get("url") or url),
        hits=str(item.get("hits") or 1),
        title=str(item.get("title") or ""),
    )


def link_search(query: str = "", limit: int = 12) -> str:
    rows = search_links(query, limit=max(1, min(int(limit or 12), 40)))
    if not rows:
        return get_prompt("link_empty")
    return render_links(rows)


def link_ingest(text: str, intent: str = "") -> str:
    rows = harvest_links(text, intent=intent)
    if not rows:
        return get_prompt("link_no_urls")
    return render_links(rows)


def link_resolve(mention: str) -> str:
    rows = resolve_mention(mention)
    if not rows:
        return get_prompt("link_no_mention", mention=mention)
    return render_links(rows)


def link_habits(limit: int = 8) -> str:
    text = habit_summary(limit=max(1, min(int(limit or 8), 20)))
    return text or get_prompt("link_empty")


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
    "link_add",
    link_add,
    {
        "url": {"type": "string", "description": get_prompt("link_param_url")},
        "title": {"type": "string", "description": get_prompt("link_param_title")},
        "intent": {"type": "string", "description": get_prompt("link_param_intent")},
        "note": {"type": "string", "description": get_prompt("link_param_note")},
        "alias": {"type": "string", "description": get_prompt("link_param_alias")},
    },
    ["url"],
)
_spec(
    "link_search",
    link_search,
    {
        "query": {"type": "string", "description": get_prompt("link_param_query")},
        "limit": {"type": "integer", "description": get_prompt("link_param_limit")},
    },
)
_spec(
    "link_ingest",
    link_ingest,
    {
        "text": {"type": "string", "description": get_prompt("link_param_text")},
        "intent": {"type": "string", "description": get_prompt("link_param_intent")},
    },
    ["text"],
)
_spec(
    "link_resolve",
    link_resolve,
    {"mention": {"type": "string", "description": get_prompt("link_param_mention")}},
    ["mention"],
)
_spec(
    "link_habits",
    link_habits,
    {"limit": {"type": "integer", "description": get_prompt("link_param_limit")}},
)
