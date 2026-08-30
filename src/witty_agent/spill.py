"""spill 策略：过大的纯文本工具结果落盘，模型只看有界预览。"""

from __future__ import annotations

import re
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.runtime import spill_settings
from witty_agent.types import AgentMessage, ToolCallBlock

SKIP_TOOLS = frozenset({"read", "skill"})


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def retain_head_tail(text: str, budget: int) -> str:
    raw = text.encode("utf-8")
    if budget <= 0:
        return ""
    if len(raw) <= budget:
        return text
    marker = b"\n...\n"
    available = budget - len(marker)
    if available <= 1:
        return raw[:budget].decode("utf-8", errors="ignore")
    head = available // 2
    tail = available - head
    return (raw[:head] + marker + raw[-tail:]).decode("utf-8", errors="ignore")


def spill_locator(session_id: str, call_id: str) -> str:
    safe_session = re.sub(r"[^a-zA-Z0-9._-]+", "_", session_id) or "session"
    safe_call = re.sub(r"[^a-zA-Z0-9._-]+", "_", call_id) or "call"
    return f"spill:{safe_session}:{safe_call}"


def spill_filename(session_id: str, call_id: str, tool_name: str) -> str:
    safe_tool = re.sub(r"[^a-zA-Z0-9._-]+", "_", tool_name) or "tool"
    safe_call = re.sub(r"[^a-zA-Z0-9._-]+", "_", call_id) or "call"
    safe_session = re.sub(r"[^a-zA-Z0-9._-]+", "_", session_id) or "session"
    return f"{safe_session}-{safe_call}-{safe_tool}.txt"


def save_spill(
    content: str,
    *,
    directory: Path,
    session_id: str,
    tool_name: str,
    call_id: str,
) -> Path:
    target = directory / "spills"
    target.mkdir(parents=True, exist_ok=True)
    path = target / spill_filename(session_id, call_id, tool_name)
    path.write_text(content, encoding="utf-8")
    return path


def resolve_spill(directory: Path, locator: str) -> str | None:
    """Read spilled text by spill:session:call, basename, or absolute path under spills/."""
    raw = (locator or "").strip()
    if not raw:
        return None
    folder = directory / "spills"
    if raw.startswith("spill:"):
        parts = raw.split(":", 2)
        if len(parts) < 3:
            return None
        prefix = f"{parts[1]}-{parts[2]}-"
        if folder.is_dir():
            for path in sorted(folder.glob(f"{prefix}*.txt")):
                return path.read_text(encoding="utf-8")
        return None
    candidate = Path(raw)
    if candidate.is_file():
        try:
            candidate.resolve().relative_to(folder.resolve())
        except ValueError:
            return None
        return candidate.read_text(encoding="utf-8")
    named = folder / Path(raw).name
    if named.is_file():
        return named.read_text(encoding="utf-8")
    return None


def apply_spill(
    result: AgentMessage,
    call: ToolCallBlock,
    *,
    scratchpad: Path | None,
    session_id: str,
    max_inline_bytes: int | None = None,
) -> AgentMessage:
    settings = spill_settings()
    limit = int(settings["max_inline_bytes"] if max_inline_bytes is None else max_inline_bytes)
    if limit <= 0 or result.is_error or call.name in SKIP_TOOLS:
        return result
    if scratchpad is None:
        return result
    text = result.text()
    size = utf8_len(text)
    if size <= limit:
        return result
    path = save_spill(
        text,
        directory=scratchpad,
        session_id=session_id,
        tool_name=call.name,
        call_id=call.id,
    )
    locator = spill_locator(session_id, call.id)
    hint = get_prompt("spill_hint")
    notice = get_prompt(
        "spill_notice",
        omitted=str(size),
        locator=locator,
        path=str(path),
        hint=hint,
    )
    notice_bytes = utf8_len("\n\n" + notice)
    if notice_bytes >= limit:
        return result
    preview = retain_head_tail(text, limit - notice_bytes)
    replaced = f"{preview}\n\n{notice}" if preview else notice
    return AgentMessage(
        role="toolResult",
        content=replaced,
        tool_call_id=result.tool_call_id,
        tool_name=result.tool_name,
        is_error=result.is_error,
        source=result.source,
    )
