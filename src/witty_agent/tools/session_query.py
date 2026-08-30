"""对照官方 session-query：在当前会话日志里按类型或正文检索。"""

from __future__ import annotations

import json

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool


def session_query(query: str, event_type: str = "") -> str:
    """搜索当前会话日志。query 匹配事件类型或 data 正文。"""
    log = hooks.session_log
    if log is None:
        raise RuntimeError(get_prompt("session_query_needs_session"))
    needle = query.lower()
    hits: list[str] = []
    for event in log.events:
        if event_type and event.type != event_type:
            continue
        blob = json.dumps(event.data, ensure_ascii=False)
        if needle in event.type.lower() or needle in blob.lower():
            hits.append(f"{event.seq} {event.type} {blob[:240]}")
        if len(hits) >= 50:
            break
    return "\n".join(hits) if hits else get_prompt("session_query_empty")


register_tool(
    ToolSpec(
        name="session_query",
        description=get_prompt("tool_desc_session_query"),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": get_prompt("session_query_param_query")},
                "event_type": {"type": "string", "description": get_prompt("session_query_param_event_type")},
            },
            "required": ["query"],
        },
        func=session_query,
    )
)
