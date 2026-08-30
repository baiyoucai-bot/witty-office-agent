"""待办整表替换，会话日志 last-write-wins。"""

from __future__ import annotations

import json
from typing import Any

from witty_agent.prompts import get_prompt
from witty_agent.runtime import todo_settings
from witty_agent.session_log import TODO_STATUSES, SessionLog, fold_todos


def describe() -> str:
    allow = todo_settings()["allow_parallel_in_progress"]
    key = "tool_desc_todo_write_parallel" if allow else "tool_desc_todo_write_single"
    return get_prompt(key)


def to_todo_list(raw: Any, *, allow_parallel: bool | None = None) -> list[dict[str, str]]:
    if allow_parallel is None:
        allow_parallel = todo_settings()["allow_parallel_in_progress"]
    items = _coerce_items(raw)
    todos: list[dict[str, str]] = []
    seen: set[str] = set()
    active = 0
    for item in items:
        extra = set(item) - {"content", "status"}
        if extra:
            raise ValueError(get_prompt("todo_unknown_keys", keys=", ".join(sorted(extra))))
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "")
        if not content:
            raise ValueError(get_prompt("todo_empty_content"))
        if content in seen:
            raise ValueError(get_prompt("todo_duplicate", content=content))
        if status not in TODO_STATUSES:
            raise ValueError(get_prompt("todo_bad_status", status=status))
        seen.add(content)
        if status == "in_progress":
            active += 1
        todos.append({"content": content, "status": status})
    if not allow_parallel and active > 1:
        raise ValueError(get_prompt("todo_too_many_active", count=str(active)))
    return todos


def apply_todo_write(log: SessionLog | None, raw: Any) -> str:
    if log is None:
        raise RuntimeError(get_prompt("todo_needs_session"))
    todos = to_todo_list(raw)
    log.append("todo/write", {"todos": todos})
    counts = {
        "pending": sum(1 for item in todos if item["status"] == "pending"),
        "inProgress": sum(1 for item in todos if item["status"] == "in_progress"),
        "completed": sum(1 for item in todos if item["status"] == "completed"),
    }
    return get_prompt(
        "todo_updated",
        pending=str(counts["pending"]),
        in_progress=str(counts["inProgress"]),
        completed=str(counts["completed"]),
    )


_OPEN_STATUSES = frozenset({"pending", "in_progress"})


def current_todos(log: SessionLog | None) -> list[dict[str, str]] | None:
    if log is None:
        return None
    return fold_todos(log.events)


def has_open_todos(todos: list[dict[str, str]] | None) -> bool:
    if not todos:
        return False
    return any(str(item.get("status") or "") in _OPEN_STATUSES for item in todos)


def completed_titles(todos: list[dict[str, str]] | None) -> str:
    if not todos:
        return ""
    names = [str(item.get("content") or "").strip() for item in todos if item.get("content")]
    return "; ".join(name for name in names if name)


def format_todo_section(todos: list[dict[str, str]] | None) -> str:
    if not todos:
        return ""
    rows = "\n".join(
        get_prompt("todo_item", status=str(item.get("status") or ""), content=str(item.get("content") or ""))
        for item in todos
        if item.get("content")
    )
    if not rows:
        return ""
    if not has_open_todos(todos):
        titles = completed_titles(todos)
        return "\n" + get_prompt("todo_section_done", titles=titles) + "\n"
    return "\n" + get_prompt("todo_section", rows=rows) + "\n"


def _coerce_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        parsed = raw
    if not isinstance(parsed, list):
        raise ValueError(get_prompt("todo_not_list"))
    items: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError(get_prompt("todo_not_list"))
        items.append(item)
    return items
