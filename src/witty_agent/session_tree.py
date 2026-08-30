"""session 树：父会话、分叉、回滚到某条消息。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.store import append_message, load_messages, session_path, write_header
from witty_agent.types import AgentMessage

logger = get_logger("session_tree")


@dataclass
class SessionNode:
    session_id: str
    parent_id: str | None
    leaf_index: int
    path: Path


def write_header_with_parent(
    path: Path, session_id: str, cwd: str, parent_id: str | None
) -> None:
    write_header(path, session_id, cwd, parent_id)


def read_parent(path: Path) -> str | None:
    if not path.is_file():
        return None
    import json

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("type") == "session":
            parent = data.get("parentSession")
            return str(parent) if parent else None
        if data.get("type") == "parent":
            parent = data.get("parent_id")
            return str(parent) if parent else None
    return None


def fork_session(
    directory: Path,
    source_id: str,
    new_id: str,
    *,
    cwd: str,
    keep: int | None = None,
) -> list[AgentMessage]:
    source = session_path(directory, source_id)
    messages = load_messages(source)
    if keep is not None:
        messages = messages[:keep]
    dest = session_path(directory, new_id)
    write_header_with_parent(dest, new_id, cwd, source_id)
    for message in messages:
        append_message(dest, message)
    logger.info("分叉会话 from=%s to=%s keep=%s", source_id, new_id, len(messages))
    return messages


def rollback_session(directory: Path, session_id: str, keep: int) -> list[AgentMessage]:
    """截到 keep 条消息，相当于在当前会话上回滚。"""
    path = session_path(directory, session_id)
    messages = load_messages(path)[:keep]
    parent = read_parent(path)
    cwd = ""
    import json

    if path.is_file():
        first = path.read_text(encoding="utf-8").splitlines()[:1]
        if first:
            header = json.loads(first[0])
            cwd = str(header.get("cwd") or "")
    path.write_text("", encoding="utf-8")
    write_header(path, session_id, cwd, parent)
    for message in messages:
        append_message(path, message)
    logger.info("回滚会话 id=%s keep=%s", session_id, len(messages))
    return messages


def list_session_ids(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.jsonl"))
