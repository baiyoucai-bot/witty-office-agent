"""session jsonl：头 + 消息，可恢复。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.session_log import SessionLogEvent, event_from_record, event_to_record
from witty_agent.types import AgentMessage, TextBlock, ToolCallBlock

logger = get_logger("store")
SESSION_VERSION = 3


def session_path(directory: Path, session_id: str) -> Path:
    return directory / f"{session_id}.jsonl"


def delete_session_file(directory: Path, session_id: str) -> bool:
    path = session_path(directory, session_id)
    if not path.is_file():
        return False
    path.unlink()
    logger.info("删除会话 path=%s", path)
    return True


def write_header(path: Path, session_id: str, cwd: str, parent_id: str | None = None) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "session",
        "version": SESSION_VERSION,
        "id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cwd": cwd,
        "parentSession": parent_id,
    }
    path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")


def append_title(path: Path, title: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "title", "title": title}, ensure_ascii=False) + "\n")


def session_topic(text: str, fallback: str = "") -> str:
    line = (text or "").strip().splitlines()[0].strip()
    return line[:48] if line else fallback


def _content_preview(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        chunks: list[str] = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") != "toolCall":
                chunks.append(str(item.get("text") or ""))
        return "".join(chunks)
    return ""


def read_session_meta(path: Path) -> dict:
    meta = {
        "id": path.stem,
        "title": "",
        "parent": None,
        "messages": 0,
        "cwd": "",
        "updated_at": 0.0,
        "created_at": "",
    }
    if not path.is_file():
        return meta
    meta["updated_at"] = path.stat().st_mtime
    first_user = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        kind = data.get("type")
        if kind == "session":
            meta["id"] = str(data.get("id") or path.stem)
            meta["parent"] = data.get("parentSession")
            meta["cwd"] = str(data.get("cwd") or "")
            meta["created_at"] = str(data.get("timestamp") or "")
        elif kind == "title":
            meta["title"] = str(data.get("title") or "")
        elif kind == "message":
            meta["messages"] += 1
            if not first_user and data.get("role") == "user":
                first_user = session_topic(_content_preview(data.get("content", "")))
    if not meta["title"]:
        meta["title"] = first_user
    return meta


def list_trace_summaries(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    rows = [read_session_meta(path) for path in directory.glob("*.jsonl")]
    rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return rows


def append_session_event(path: Path, event: SessionLogEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event_to_record(event), ensure_ascii=False) + "\n")


def load_session_events(path: Path) -> list[SessionLogEvent]:
    if not path.is_file():
        return []
    events: list[SessionLogEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        event = event_from_record(record)
        if event is not None:
            events.append(event)
    return events


def append_event(path: Path, event_type: str, **fields: object) -> None:
    record = {"type": "event", "event": event_type}
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_usage(path: Path, input_tokens: int, output_tokens: int) -> None:
    record = {
        "type": "usage",
        "input": input_tokens,
        "output": output_tokens,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_message(path: Path, message: AgentMessage) -> None:
    record = {
        "type": "message",
        "role": message.role,
        "content": _dump_content(message),
        "tool_call_id": message.tool_call_id,
        "tool_name": message.tool_name,
        "is_error": message.is_error,
        "stop_reason": message.stop_reason,
        "source": message.source,
        "reasoning": message.reasoning or "",
        "evidence": list(message.evidence or []),
        "trace_reason": message.trace_reason or "",
    }
    if message.meta:
        record["meta"] = dict(message.meta)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


ROLLBACK_MARKER_SOURCE = "plugin:rollback-marker"


def rollback_marker(keep: int, *, raw: bool) -> AgentMessage:
    """回滚标记：只追加，永不重写历史（tombstone）。

    `raw=False`：keep 数的是**折叠后**的列表——和用户在界面上看到的一致；
    `raw=True`：keep 数的是原始序列——能回到压缩检查点之前。
    """
    return AgentMessage(
        role="user",
        content="",
        source=ROLLBACK_MARKER_SOURCE,
        meta={"keep": int(max(0, keep)), "raw": bool(raw)},
    )


def project_messages(raw_messages: list[AgentMessage]) -> list[AgentMessage]:
    """原始序列 → 可见投影。历史只追加，读的时候按标记算。

    按文件顺序逐条应用回滚标记：`raw=True` 的标记把「到此为止累计的原始序列」截到 keep 条；
    `raw=False` 的标记把「到此为止的折叠视图」截到 keep 条（折叠视图之外的前缀就此不可见，
    与用户当时看到的列表一致，不会把压缩前的旧消息顺手捞回来）。标记本身不进投影。
    最后再做一次压缩检查点折叠。返回列表里的对象与输入同一身份（不复制），
    调用方可以用 id() 把投影下标映射回原始下标。
    """
    current: list[AgentMessage] = []
    for message in raw_messages:
        if message.source == ROLLBACK_MARKER_SOURCE:
            meta = message.meta or {}
            try:
                keep = max(0, int(meta.get("keep") or 0))
            except (TypeError, ValueError):
                keep = 0
            base = current if meta.get("raw") else fold_compaction_checkpoint(current)
            current = list(base[:keep])
            continue
        current.append(message)
    return fold_compaction_checkpoint(current)


def fold_compaction_checkpoint(messages: list[AgentMessage]) -> list[AgentMessage]:
    """压缩检查点：从最近一次检查点起读。区间压缩可保留检查点前 keep_before 条。"""
    last = -1
    keep_before = 0
    for index, message in enumerate(messages):
        if message.source == "plugin:compaction-checkpoint":
            last = index
            raw = message.meta.get("keep_before") if message.meta else 0
            try:
                keep_before = max(0, int(raw or 0))
            except (TypeError, ValueError):
                keep_before = 0
    if last <= 0:
        return messages
    begin = max(0, last - keep_before)
    return messages[begin:]


def load_raw_messages(path: Path) -> list[AgentMessage]:
    """文件里全部 message 记录，含压缩检查点与回滚标记，不做任何投影。"""
    return load_messages(path, fold=False)


def load_messages(path: Path, *, fold: bool = True) -> list[AgentMessage]:
    """读会话消息。默认返回投影（应用回滚标记 + 压缩折叠）；fold=False 返回原始序列。"""
    if not path.is_file():
        return []
    messages: list[AgentMessage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("type") != "message":
            continue
        messages.append(
            AgentMessage(
                role=data["role"],
                content=_load_content(data.get("content", "")),
                tool_call_id=data.get("tool_call_id"),
                tool_name=data.get("tool_name"),
                is_error=bool(data.get("is_error")),
                stop_reason=data.get("stop_reason"),
                source=data.get("source"),
                reasoning=str(data.get("reasoning") or ""),
                evidence=list(data.get("evidence") or []) if isinstance(data.get("evidence"), list) else [],
                trace_reason=str(data.get("trace_reason") or ""),
                meta=dict(data.get("meta") or {}) if isinstance(data.get("meta"), dict) else {},
            )
        )
    if not fold:
        return messages
    folded = project_messages(messages)
    logger.info("恢复会话 messages=%s folded=%s path=%s", len(messages), len(folded), path)
    return folded


def _dump_content(message: AgentMessage) -> object:
    if isinstance(message.content, str):
        return message.content
    dumped: list[dict] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            dumped.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolCallBlock):
            dumped.append(
                {
                    "type": "toolCall",
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.arguments,
                }
            )
    return dumped


def _load_content(raw: object) -> str | list:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return str(raw)
    blocks: list = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "toolCall":
            blocks.append(
                ToolCallBlock(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=dict(item.get("arguments") or {}),
                )
            )
        else:
            blocks.append(TextBlock(text=str(item.get("text") or "")))
    return blocks
