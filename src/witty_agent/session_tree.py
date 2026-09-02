"""session 树：父会话、分叉、回滚到某条消息。

历史只追加（tombstone）：分叉拷的是原始序列，回滚是追加一条标记，都不重写文件。
于是压缩检查点之前的记录在任何支线上都还在盘上，`raw=True` 就能寻址回去。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.store import (
    append_message,
    load_messages,
    load_raw_messages,
    project_messages,
    rollback_marker,
    session_path,
    write_header,
)
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
    raw: bool = False,
) -> list[AgentMessage]:
    """分叉：子会话文件拷父会话的**原始序列**（含检查点与回滚标记），投影因而与父一致。

    `keep`：`raw=False` 按折叠后下标截（用户看到的列表），实现是拷全部原始记录再追加一条
    折叠语义的回滚标记——原始前缀仍在子文件里；`raw=True` 按原始下标截，可落到压缩之前。
    返回子会话的投影。
    """
    source = session_path(directory, source_id)
    raw_messages = load_raw_messages(source)
    dest = session_path(directory, new_id)
    write_header_with_parent(dest, new_id, cwd, source_id)
    if keep is not None and raw:
        for message in raw_messages[: max(0, keep)]:
            append_message(dest, message)
    else:
        for message in raw_messages:
            append_message(dest, message)
        if keep is not None:
            append_message(dest, rollback_marker(keep, raw=False))
    projected = load_messages(dest)
    logger.info("分叉会话 from=%s to=%s keep=%s raw=%s visible=%s", source_id, new_id, keep, raw, len(projected))
    return projected


def rollback_session(directory: Path, session_id: str, keep: int, *, raw: bool = False) -> list[AgentMessage]:
    """回滚到 keep 条：追加一条回滚标记，不重写文件，盘上历史一条不少。

    `raw=False` 的 keep 是折叠后下标（与 load_messages 返回的列表对齐）；
    `raw=True` 是原始下标，可回到压缩检查点之前。返回回滚后的投影。
    """
    path = session_path(directory, session_id)
    if not path.is_file():
        return []
    append_message(path, rollback_marker(keep, raw=raw))
    projected = project_messages(load_raw_messages(path))
    logger.info("回滚会话 id=%s keep=%s raw=%s visible=%s", session_id, keep, raw, len(projected))
    return projected


def list_session_ids(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.jsonl"))
