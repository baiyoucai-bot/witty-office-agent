"""本轮回答的可追溯证据：工具结果的源头与摘录，不是编出来的引用。"""

from __future__ import annotations

import re
from typing import Any

from witty_agent.dispatch import is_chat_turn
from witty_agent.guard import is_empty_lookup, is_substantive_tool_result
from witty_agent.logging import get_logger
from witty_agent.memory import order_hits_working_first
from witty_agent.prompts import get_prompt
from witty_agent.types import AgentMessage

logger = get_logger("trace")

_LOCATOR_KEYS = ("path", "file", "url", "slug", "query", "pattern", "command", "prompt")
_EXCERPT = 160
_SKILL_NAME = re.compile(r'<skill\s+name="([^"]+)"', re.IGNORECASE)
_SKILL_BODY = re.compile(r"<skill\b[^>]*>(.*?)</skill>", re.IGNORECASE | re.DOTALL)


def collect_turn_evidence(
    messages: list[AgentMessage],
    *,
    memory_hits: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    memory_empty: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    start = 0
    for index, message in enumerate(messages):
        if message.role == "user" and not str(message.source or "").startswith("plugin:"):
            start = index
    args_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool]] = set()
    for message in messages[start:]:
        for call in message.tool_calls():
            args_by_id[call.id] = (call.name, dict(call.arguments or {}))
        if message.role == "user" and str(message.source or "") == "plugin:skill-invocation":
            skill = _skill_item(message)
            if skill is None:
                continue
            key = ("skill", str(skill["locator"]), False)
            if key in seen:
                continue
            seen.add(key)
            items.append(skill)
            continue
        if message.role != "toolResult" or not message.tool_name:
            continue
        name, args = args_by_id.get(message.tool_call_id or "", (message.tool_name, {}))
        empty = is_empty_lookup(name, message.text())
        if not is_substantive_tool_result(message) and not empty and not message.is_error:
            continue
        locator = _locator(args)
        scope = str(args.get("scope") or "").strip()
        key = (name, f"{scope}:{locator}", bool(message.is_error or empty))
        if key in seen:
            continue
        seen.add(key)
        loaded = name == "memory_read" and not message.is_error and not empty
        row = {
            "kind": "memory" if loaded else "tool",
            "source": name,
            "locator": locator,
            "excerpt": _excerpt(message.text()),
            "ok": not message.is_error and not empty,
        }
        if scope:
            row["scope"] = scope
        if loaded:
            row["loaded"] = True
        if locator.startswith("archive/") or str(args.get("layer") or "") == "archive":
            row["layer"] = "archive"
        items.append(row)
    for hit in order_hits_working_first(memory_hits):
        slug = str(hit.get("slug") or "").strip()
        excerpt = str(hit.get("text") or "").strip()
        if not slug and not excerpt:
            continue
        scope = str(hit.get("scope") or "").strip()
        key = ("memory_read", f"{scope}:{slug}", False)
        if key in seen:
            continue
        seen.add(key)
        row = {
            "kind": "memory",
            "source": "memory_read",
            "locator": slug,
            "excerpt": _excerpt(excerpt),
            "ok": True,
        }
        if scope:
            row["scope"] = scope
        try:
            score = int(hit.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score > 0:
            row["score"] = score
        layer = str(hit.get("layer") or "").strip()
        if layer == "archive" or slug.startswith("archive/"):
            row["layer"] = "archive"
        relocated = hit.get("relocated")
        if isinstance(relocated, list) and relocated:
            row["relocated"] = relocated
        items.append(row)
    items = _order_memory_evidence(items)
    memories = [item for item in items if item.get("kind") == "memory"]
    if not memories and not is_chat_turn(_last_user_text(messages)):
        items.extend(_browse_items(memory_empty))
    tools = [item for item in items if item.get("kind") == "tool" and item.get("ok")]
    memories = [item for item in items if item.get("kind") == "memory"]
    skills = [item for item in items if item.get("kind") == "skill"]
    browses = [item for item in items if item.get("kind") == "browse"]
    parts: list[str] = []
    if skills:
        names = ", ".join(dict.fromkeys(str(item["locator"]) for item in skills if item.get("locator")))
        parts.append(get_prompt("trace_reason_skill", skills=names or "-", count=str(len(skills))))
    if tools and memories:
        parts.append(
            get_prompt(
                "trace_reason_both",
                tools=", ".join(dict.fromkeys(item["source"] for item in tools)),
                tool_count=str(len(tools)),
                slugs=", ".join(dict.fromkeys(str(item["locator"]) for item in memories if item.get("locator"))),
                memory_count=str(len(memories)),
            )
        )
    elif tools:
        names = ", ".join(dict.fromkeys(item["source"] for item in tools))
        parts.append(get_prompt("trace_reason_tools", tools=names, count=str(len(tools))))
    elif memories:
        slugs = ", ".join(dict.fromkeys(str(item["locator"]) for item in memories if item.get("locator")))
        parts.append(get_prompt("trace_reason_memory", slugs=slugs or "-", count=str(len(memories))))
    elif browses:
        slugs = ", ".join(dict.fromkeys(str(item["locator"]) for item in browses if item.get("locator")))
        parts.append(get_prompt("trace_reason_browse", slugs=slugs or "-", count=str(len(browses))))
    reason = " ".join(parts) if parts else get_prompt("trace_reason_none")
    return items, reason


def _order_memory_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    memories = [item for item in items if item.get("kind") == "memory"]
    if len(memories) < 2:
        return items
    ordered = order_hits_working_first(memories)
    if ordered == memories:
        return items
    walk = iter(ordered)
    return [next(walk) if item.get("kind") == "memory" else item for item in items]


def attach_turn_evidence(
    messages: list[AgentMessage],
    *,
    memory_hits: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    memory_empty: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    items, reason = collect_turn_evidence(
        messages,
        memory_hits=memory_hits,
        memory_empty=memory_empty,
    )
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        if message.tool_calls() and not message.text():
            continue
        message.evidence = items
        message.trace_reason = reason
        logger.info("回答挂证据 items=%s tools=%s", len(items), ",".join(item["source"] for item in items) or "-")
        break
    return items, reason


def _browse_items(memory_empty: dict[str, Any] | None) -> list[dict[str, Any]]:
    empty = memory_empty or {}
    if empty.get("reason") != "no_overlap":
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*(empty.get("populated") or []), *(empty.get("archive") or [])]:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("id") or item.get("slug") or "").strip()
        scope = str(item.get("scope") or "").strip()
        key = (slug, scope)
        if not slug or key in seen:
            continue
        seen.add(key)
        title = str(item.get("title") or slug)
        count = item.get("count") or 0
        row = {
            "kind": "browse",
            "source": "memory_status",
            "locator": slug,
            "excerpt": f"{title} · {count}",
            "ok": True,
        }
        if scope:
            row["scope"] = scope
        rows.append(row)
        if len(rows) >= 8:
            break
    return rows


def _skill_item(message: AgentMessage) -> dict[str, Any] | None:
    text = message.text() or ""
    match = _SKILL_NAME.search(text)
    name = (match.group(1) if match else "").strip()
    if not name:
        return None
    inner = _SKILL_BODY.search(text)
    body = (inner.group(1) if inner else text).strip()
    return {
        "kind": "skill",
        "source": "skill",
        "locator": name,
        "excerpt": _excerpt(body),
        "ok": True,
    }


def _last_user_text(messages: list[AgentMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and not str(message.source or "").startswith("plugin:"):
            return message.text()
    return ""


def _locator(args: dict[str, Any]) -> str:
    for key in _LOCATOR_KEYS:
        value = args.get(key)
        if value:
            return str(value).strip()[:200]
    return ""


def _excerpt(text: str) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= _EXCERPT:
        return compact
    return compact[: _EXCERPT - 1] + "…"
