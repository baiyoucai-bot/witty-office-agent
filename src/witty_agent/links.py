"""用户链接库：独立于九宫格，从聊天和打开的 URL 收割。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.time_context import clock_now

logger = get_logger("links")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_NAMED = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}(?:系统|门户|平台|网站|站点|网|OA|oa)?)"
    r"\s*(?:https?://)|"
    r"(?:打开|访问|登录|进了|看了|去了)\s*"
    r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
    re.IGNORECASE,
)
_ALIAS = re.compile(
    r"(?:又称|也叫|就是|简称)\s*[「『\"']?([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
    re.IGNORECASE,
)


def links_path() -> Path:
    raw = os.environ.get("WITTY_LINKS_FILE")
    if raw:
        return Path(raw).expanduser()
    user = os.environ.get("WITTY_MEMORY_USER")
    if user:
        agent_state = Path(user).resolve().parent.parent
        target = agent_state / "links" / "links.jsonl"
        nested = agent_state / "agent_state" / "links" / "links.jsonl"
        sibling = agent_state.parent / "links" / "links.jsonl"
        chosen = _prefer_migrated(target, nested)
        return _prefer_migrated(chosen, sibling)
    from witty_agent.layout import agent_state_dir

    target = agent_state_dir() / "links" / "links.jsonl"
    return _prefer_migrated(target, Path.cwd() / ".witty" / "links.jsonl")


def _prefer_migrated(target: Path, legacy: Path) -> Path:
    if target.is_file() or not legacy.is_file():
        return target
    if target.resolve() == legacy.resolve():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info("链接库已从旧路径迁入")
    return target


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL.finditer(text or ""):
        url = match.group(0).rstrip(".,;。，")
        key = url.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(url)
    return found


def guess_title_and_intent(text: str, url: str = "") -> tuple[str, str]:
    """从用户原话抽出系统称呼和意图，不写死业务分支。"""
    blob = text or ""
    title = ""
    for match in _NAMED.finditer(blob):
        title = (match.group(1) or match.group(2) or "").strip()
        if title and title.casefold() not in {"http", "https"}:
            break
        title = ""
    stripped = _URL.sub(" ", blob)
    stripped = re.sub(r"\s+", " ", stripped).strip(" 。；;，,")
    intent = stripped[:160]
    return title[:80], intent


def guess_aliases(text: str, title: str = "") -> list[str]:
    found: list[str] = []
    if title.strip():
        found.append(title.strip()[:40])
    for match in _ALIAS.finditer(text or ""):
        alias = (match.group(1) or "").strip()
        if alias and alias not in found and alias.casefold() not in {"http", "https"}:
            found.append(alias[:40])
    return found[:8]


def upsert_link(
    url: str,
    *,
    title: str = "",
    intent: str = "",
    note: str = "",
    alias: str = "",
    aliases: list[str] | None = None,
    source: str = "chat",
) -> dict:
    url = (url or "").strip()
    if not url:
        raise ValueError(get_prompt("link_url_required"))
    rows = load_links()
    now = str(clock_now()["iso"])
    host = (urlparse(url).hostname or "").casefold()
    for row in rows:
        if str(row.get("url") or "").casefold() == url.casefold():
            row["hits"] = int(row.get("hits") or 1) + 1
            row["updated_at"] = now
            row["last_used_at"] = now
            if title:
                row["title"] = title[:80]
            if intent:
                row["intent"] = intent[:160]
                history = [str(item) for item in (row.get("intents") or []) if item]
                if intent not in history:
                    history.append(intent[:160])
                row["intents"] = history[-12:]
            if note:
                row["note"] = note[:240]
            incoming = [item for item in ([alias] if alias else []) + list(aliases or []) if item]
            if incoming:
                current = _aliases(row)
                for name in incoming:
                    if name not in current:
                        current.append(name[:40])
                row["aliases"] = current[:8]
            if source:
                row["source"] = source
            _write_all(rows)
            return row
    item = {
        "url": url,
        "host": host,
        "title": (title or host or url)[:80],
        "intent": intent[:160],
        "intents": [intent[:160]] if intent else [],
        "note": note[:240],
        "aliases": [
            name[:40]
            for name in ([alias] if alias else []) + list(aliases or [])
            if name
        ][:8],
        "source": source or "chat",
        "hits": 1,
        "created_at": now,
        "updated_at": now,
        "last_used_at": now,
    }
    rows.append(item)
    _write_all(rows)
    logger.info("链接入库 host=%s", host)
    return item


def harvest_links(text: str, *, intent: str = "") -> list[dict]:
    added: list[dict] = []
    for url in extract_urls(text):
        title, guessed = guess_title_and_intent(text, url)
        hint = (intent or guessed or "").strip()[:160]
        added.append(
            upsert_link(
                url,
                title=title,
                intent=hint,
                aliases=guess_aliases(text, title),
                source="chat",
            )
        )
    return added


def record_opened_url(url: str, *, title: str = "", intent: str = "") -> dict | None:
    """web_fetch / 打开网页时记一条，失败不抛。"""
    target = (url or "").strip()
    if not target:
        return None
    try:
        return upsert_link(
            target,
            title=title,
            intent=(intent or get_prompt("link_open_intent"))[:160],
            source="fetch",
        )
    except ValueError:
        return None


def load_links() -> list[dict]:
    path = links_path()
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("url"):
            rows.append(item)
    return rows


def search_links(query: str, *, limit: int = 12) -> list[dict]:
    needle = (query or "").strip().casefold()
    rows = load_links()
    if not needle:
        rows.sort(key=lambda item: int(item.get("hits") or 0), reverse=True)
        return rows[:limit]
    scored: list[tuple[int, dict]] = []
    for row in rows:
        blob = " ".join(
            [
                str(row.get(key) or "")
                for key in ("url", "host", "title", "intent", "note")
            ]
            + [str(item) for item in (row.get("intents") or []) if item]
        ).casefold()
        aliases = " ".join(_aliases(row)).casefold()
        score = blob.count(needle) * 3 + (4 if needle in blob else 0) + int(row.get("hits") or 0)
        if needle and needle in aliases:
            score += 8
        score += _recency_bonus(row)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _score, item in scored[:limit]]


def resolve_mention(mention: str, *, limit: int = 5, touch: bool = True) -> list[dict]:
    """把口头指代（OA、周报系统）对到链接库；命中则记一次使用。"""
    rows = search_links(mention, limit=limit)
    if touch and rows:
        top = rows[0]
        updated = upsert_link(str(top.get("url") or ""))
        return [updated, *[item for item in rows[1:] if item.get("url") != updated.get("url")]]
    return rows


def habit_summary(*, limit: int = 8) -> str:
    rows = search_links("", limit=max(limit, 1))
    if not rows:
        return ""
    ranked = sorted(
        rows,
        key=lambda item: (int(item.get("hits") or 0), str(item.get("last_used_at") or "")),
        reverse=True,
    )
    return render_links(ranked[:limit])


def _recency_bonus(row: dict) -> int:
    stamp = str(row.get("last_used_at") or row.get("updated_at") or "")
    today = str(clock_now().get("date") or "")
    if today and today in stamp:
        return 5
    return 0


def render_links(rows: list[dict] | None = None) -> str:
    items = rows if rows is not None else load_links()
    if not items:
        return get_prompt("link_empty")
    lines = []
    for item in items[:40]:
        title = item.get("title") or item.get("host") or item.get("url")
        intent = item.get("intent") or ""
        extra = f" · {intent}" if intent else ""
        aliases = _aliases(item)
        aka = f" 又称 {', '.join(aliases)}" if aliases else ""
        lines.append(f"- {title} ({item.get('hits') or 1}) {item.get('url')}{aka}{extra}")
    return "\n".join(lines)


def _aliases(row: dict) -> list[str]:
    raw = row.get("aliases") or []
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()[:40]]
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in found:
            found.append(text[:40])
    return found


def _write_all(rows: list[dict]) -> None:
    path = links_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows),
        encoding="utf-8",
    )
