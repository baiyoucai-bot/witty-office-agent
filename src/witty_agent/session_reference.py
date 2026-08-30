"""会话引用：其它会话的有界只读快照，不是 fork。"""

from __future__ import annotations

import re
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.session_tree import list_session_ids
from witty_agent.store import load_messages, read_session_meta, session_path
from witty_agent.types import AgentMessage

_REF = re.compile(r"session:([0-9a-zA-Z][0-9a-zA-Z_-]{1,63})\b")
_MAX_REFS = 3
_MAX_CHARS = 4000


def parse_session_refs(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _REF.finditer(text or ""):
        raw = match.group(1)
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def resolve_session_ref(raw: str, directory: Path, *, self_id: str) -> str | None:
    needle = (raw or "").strip()
    if not needle:
        return None
    self_key = self_id.casefold()
    if needle.casefold() == self_key:
        return None
    ids = list_session_ids(directory)
    for item in ids:
        if item.casefold() == needle.casefold():
            return item
    matches = [item for item in ids if item.casefold().startswith(needle.casefold()) and item.casefold() != self_key]
    if len(matches) == 1:
        return matches[0]
    return None


def project_session_snapshot(
    messages: list[AgentMessage],
    *,
    max_chars: int = _MAX_CHARS,
) -> tuple[str, int]:
    lines: list[str] = []
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        if str(message.source or "").startswith("plugin:"):
            continue
        text = message.text().strip()
        if not text:
            continue
        lines.append(f"{message.role}: {text}")
    if not lines:
        return "", 0
    kept: list[str] = []
    used = 0
    omitted = 0
    for line in reversed(lines):
        extra = len(line) + (1 if kept else 0)
        if kept and used + extra > max_chars:
            omitted += 1
            continue
        kept.append(line)
        used += extra
    kept.reverse()
    body = "\n".join(kept)
    if omitted:
        body = f"{get_prompt('session_reference_omitted', count=str(omitted))}\n{body}"
    return body, omitted


def session_reference_hint(
    prompt: str,
    *,
    self_id: str,
    directory: Path,
    max_chars: int = _MAX_CHARS,
    max_refs: int = _MAX_REFS,
) -> AgentMessage | None:
    refs = parse_session_refs(prompt)
    if not refs:
        return None
    blocks: list[str] = []
    for raw in refs:
        if len(blocks) >= max_refs:
            break
        sid = resolve_session_ref(raw, directory, self_id=self_id)
        if sid is None:
            continue
        path = session_path(directory, sid)
        excerpt, _omitted = project_session_snapshot(load_messages(path), max_chars=max_chars)
        if not excerpt:
            continue
        title = str(read_session_meta(path).get("title") or sid)
        blocks.append(get_prompt("session_reference_item", title=title, session_id=sid, excerpt=excerpt))
    if not blocks:
        return None
    return AgentMessage(
        role="user",
        content=get_prompt("session_reference", body="\n\n".join(blocks)),
        source="plugin:session-reference",
    )
