"""记忆体：user 跨工作区，workspace 跟目录走。索引进提示词，正文按需读。"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from witty_agent.layout import agent_state_dir, memory_user_dir, memory_workspace_dir
from witty_agent.logging import get_logger
from witty_agent.memory_config import MemorySettings, load_memory_settings
from witty_agent.prompts import get_prompt

logger = get_logger("memory")
INDEX_NAME = "MEMORY.md"
MARKER_NAME = ".workspace"
# 归档再溢出的去处。只追加、不检索、不自动删——记忆是用户数据。
RETIRED_DIR = "retired"
META_NAME = "meta.toml"
PROFILE_SLUG = "profile"
INDEX_MAX_LINES = 200
INDEX_MAX_CHARS = 25_000
EMPTY_NOTE = "(the index is empty — nothing has been saved yet)"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FACT_TOOLS = frozenset({"read", "grep", "find", "ls", "web_fetch"})
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+")
_TOOL_NAME_PREFIX = re.compile(
    rf"^(?:{'|'.join(sorted(FACT_TOOLS))})\s+",
    re.IGNORECASE,
)
# 打分在每轮的热路径上，别在循环里现编。
_LATIN_TOKEN = re.compile(r"[a-z0-9_]+")
# 倒排索引按目录缓存，指纹（文件数 / mtime / 字节数）变了才重建。见 `_load_index`。
_INDEX_CACHE: dict[tuple, object] = {}


@dataclass(frozen=True)
class SessionMemory:
    user_dir: Path
    user_index: str
    workspace_key: str | None = None
    workspace_dir: Path | None = None
    workspace_index: str | None = None
    lattice: str = ""
    profile: str = ""
    timeline: str = ""
    retrieved: str = ""
    hits: tuple[dict[str, object], ...] = ()
    empty: dict[str, object] = field(default_factory=dict)


def workspace_memory_key(workspace: str | Path) -> str:
    real = Path(workspace).resolve()
    digest = hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:8]
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", real.name).lower()
    # key 要过 layout.assert_id（分隔符不能连续、不能在两端）。目录名以 _ 结尾时
    # 拼上 -digest 会出现 `_-` 相邻，layout 直接拒绝——criteria/记忆整条路都走不通。
    # 只修不合法的形状，已合法的 key 保持原样，不能让存量工作区的记忆换目录。
    base = re.sub(r"[_-]{2,}", "-", base).strip("_-")[:40].rstrip("_-")
    if not base:
        base = "workspace"
    return f"{base}-{digest}"


def _ensure_index(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    index = directory / INDEX_NAME
    if not index.exists():
        index.write_text("", encoding="utf-8")


def read_index(directory: Path) -> str:
    path = directory / INDEX_NAME
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()[:INDEX_MAX_LINES]
    clipped = "\n".join(lines)
    if len(clipped) > INDEX_MAX_CHARS:
        clipped = clipped[:INDEX_MAX_CHARS] + "\n[index truncated]"
    return clipped


def cap_index(text: str) -> str:
    body = text.strip()
    return body if body else EMPTY_NOTE


def resolve_session_memory(
    *,
    project_id: str,
    agent_id: str,
    workspace: str | Path,
    root: Path | None,
) -> SessionMemory:
    user_dir = memory_user_dir(project_id, agent_id, root=root)
    _ensure_index(user_dir)
    key = workspace_memory_key(workspace)
    ws_dir = memory_workspace_dir(key, project_id, agent_id, root=root)
    _ensure_index(ws_dir)
    ensure_lattice(user_dir)
    marker = ws_dir / MARKER_NAME
    marker.write_text(f"{Path(workspace).resolve()}\n", encoding="utf-8")
    memory = SessionMemory(
        user_dir=user_dir,
        user_index=cap_index(read_index(user_dir)),
        workspace_key=key,
        workspace_dir=ws_dir,
        workspace_index=cap_index(read_index(ws_dir)),
        lattice=render_lattice(user_dir),
        profile=read_profile(user_dir),
        timeline=_timeline_text(user_dir),
    )
    logger.info("记忆体就绪 user=%s workspace=%s", user_dir, ws_dir)
    return memory


def write_topic(
    directory: Path,
    slug: str,
    *,
    description: str,
    body: str,
) -> Path:
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"记忆 slug 不合法: {slug!r}")
    _ensure_index(directory)
    path = directory / f"{slug}.md"
    text = (
        f"---\nname: {slug}\ndescription: {description}\n"
        f"updated_at: {date.today().isoformat()}\n---\n\n{body.strip()}\n"
    )
    path.write_text(text, encoding="utf-8")
    _upsert_index(directory, slug, description)
    logger.info("写入记忆 slug=%s path=%s", slug, path)
    return path


def _archive_name(slug: str) -> str | None:
    text = (slug or "").strip()
    for prefix in ("archive/", "archive-"):
        if text.startswith(prefix):
            name = text[len(prefix) :].strip().strip("/")
            if name and "/" not in name and name not in {".", ".."}:
                return name
    return None


def _topic_path(directory: Path, slug: str) -> Path:
    archived = _archive_name(slug)
    if archived:
        return directory / "archive" / f"{archived}.md"
    return directory / f"{slug}.md"


def read_topic(directory: Path, slug: str) -> str:
    path = _topic_path(directory, slug)
    if not path.is_file():
        raise FileNotFoundError(f"没有记忆 {slug}")
    return path.read_text(encoding="utf-8")


def _upsert_index(directory: Path, slug: str, description: str) -> None:
    index = directory / INDEX_NAME
    line = f"- [{slug}]({slug}.md) — {description}"
    existing = index.read_text(encoding="utf-8") if index.is_file() else ""
    rows = [row for row in existing.splitlines() if f"]({slug}.md)" not in row]
    rows.append(line)
    index.write_text("\n".join(rows).strip() + "\n", encoding="utf-8")


def agent_memory_root(project_id: str, agent_id: str, *, root: Path | None) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "memory"


def ensure_lattice(directory: Path, settings: MemorySettings | None = None) -> None:
    settings = settings or load_memory_settings()
    _ensure_index(directory)
    for cell in settings.cells:
        path = directory / f"{cell.id}.md"
        if not path.is_file():
            write_topic(directory, cell.id, description=cell.description or cell.title, body="")
    if not (directory / f"{PROFILE_SLUG}.md").is_file():
        write_topic(directory, PROFILE_SLUG, description="用户画像", body=_empty_profile(0))
    rebuild_memory_index(directory, settings=settings)


def topic_body(directory: Path, slug: str) -> str:
    path = _topic_path(directory, slug)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def replace_path_token(text: str, stale: str, found: str) -> str:
    """Replace a bare path token; do not touch a longer path that already contains it."""
    src = str(stale or "").strip()
    dest = str(found or "").strip()
    body = str(text or "")
    if not src or not dest or src == dest or not body:
        return body
    pattern = re.compile(rf"(?<![\w./\\-]){re.escape(src)}(?![\w./\\-])")
    return pattern.sub(dest, body)


def apply_relocated_hits(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
    pairs: list[tuple[str, str]] | tuple[tuple[str, str], ...] | None,
) -> int:
    """Rewrite Recalled hit excerpts in place and attach relocated from→to."""
    mapping = [(str(src).strip(), str(dest).strip()) for src, dest in (pairs or ()) if src and dest and src != dest]
    changed = 0
    for item in hits or ():
        blob = str(item.get("text") or item.get("excerpt") or "")
        relocated: list[dict[str, str]] = []
        next_blob = blob
        for src, dest in mapping:
            rewritten = replace_path_token(next_blob, src, dest)
            if rewritten != next_blob:
                next_blob = rewritten
                relocated.append({"from": src, "to": dest})
        if not relocated:
            continue
        item["text"] = next_blob
        if item.get("excerpt"):
            item["excerpt"] = next_blob
        item["relocated"] = relocated
        changed += 1
    return changed


def rewrite_relocated_paths(
    directory: Path | None,
    pairs: list[tuple[str, str]] | tuple[tuple[str, str], ...] | None,
) -> int:
    """Persist unique find-and-read paths into workspace memory bullets."""
    if directory is None or not pairs:
        return 0
    mapping = [(str(src).strip(), str(dest).strip()) for src, dest in pairs if src and dest and src != dest]
    if not mapping:
        return 0
    settings = load_memory_settings()
    changed = 0
    for slug, _title, body in _all_topics(directory, settings, include_archive=False):
        if slug == PROFILE_SLUG:
            continue
        next_body = body
        for src, dest in mapping:
            next_body = replace_path_token(next_body, src, dest)
        if next_body == body:
            continue
        write_topic(
            directory,
            slug.replace("archive-", "") if slug.startswith("archive-") else slug,
            description=_topic_description(directory, slug) or slug,
            body="\n".join(f"- {item}" for item in _bullets(next_body)) or next_body,
        )
        changed += 1
    if changed:
        rebuild_memory_index(directory, settings=settings)
        logger.info("记忆路径改写 count=%s", changed)
    return changed


def _topic_description(directory: Path, slug: str) -> str:
    path = _topic_path(directory, slug)
    if not path.is_file():
        return slug
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return slug
    parts = text.split("---", 2)
    if len(parts) < 3:
        return slug
    for line in parts[1].splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip() or slug
    return slug


def append_unique_bullets(
    directory: Path,
    slug: str,
    *,
    description: str,
    lines: list[str],
    max_bullets: int | None = None,
    dedupe_by_fact: bool = True,
) -> int:
    """按「事」去重后写入；返回本次**落盘的条数**（新事实 + 被刷新的重述）。

    `dedupe_by_fact=False` 留给时间线：那里行首日期是事件本身（一句话可以对应两个
    日期），不是收割元数据，所以只能比整串。
    """
    settings = load_memory_settings()
    cap = max_bullets if max_bullets is not None else settings.working_set
    before = _bullets(topic_body(directory, slug))
    rows = list(before)
    seen = {item.casefold() for item in rows}
    added = 0
    for raw in lines:
        line = _clean_bullet(raw)
        if not line or _should_skip(line, settings):
            continue
        if not dedupe_by_fact:
            if line.casefold() in seen:
                continue
            rows.append(line)
            seen.add(line.casefold())
            added += 1
            continue
        if not _fact_key(line):
            continue
        merged = _merge_fact(rows, line)
        # 刷新也算「这轮记下了这一格」——`cells_hit` 靠它连共现边，而重述同一件事
        # 在旧实现里本来就落成新行、本来就计数。只有整串一字不差且已在队尾才是零。
        if merged != rows:
            added += 1
        rows = merged
    if rows != before:
        live = rows[-cap:]
        spilled = rows[:-cap] if len(rows) > cap else []
        write_topic(directory, slug, description=description, body="\n".join(f"- {item}" for item in live))
        if spilled:
            _archive_bullets(directory, slug, spilled, settings.archive_cap)
    return added


def render_lattice(directory: Path, settings: MemorySettings | None = None) -> str:
    settings = settings or load_memory_settings()
    cells = list(settings.cells)
    titles = [cell.title for cell in cells]
    while len(titles) < 9:
        titles.append("—")
    table = [
        f"| {titles[0]} | {titles[1]} | {titles[2]} |",
        "| --- | --- | --- |",
        f"| {titles[3]} | {titles[4]} | {titles[5]} |",
        f"| {titles[6]} | {titles[7]} | {titles[8]} |",
    ]
    rows = ["\n".join(table), ""]
    for cell in settings.cells:
        excerpt = _excerpt(topic_body(directory, cell.id)) or "（空）"
        rows.append(f"- {cell.title} (`{cell.id}`): {excerpt}")
    return "\n".join(rows).strip()


def read_profile(directory: Path) -> str:
    return topic_body(directory, PROFILE_SLUG)


def write_profile(directory: Path, *, turns: int, settings: MemorySettings | None = None) -> None:
    settings = settings or load_memory_settings()
    who = _excerpt(topic_body(directory, "who"), limit=160) or "尚未记录"
    prefs = _excerpt(topic_body(directory, "prefs"), limit=200) or "尚未记录"
    assets = _excerpt(topic_body(directory, "assets"), limit=160) or "尚未记录"
    followups = _excerpt(topic_body(directory, "followups"), limit=160) or "无"
    body = get_prompt(
        "memory_profile_body",
        turns=str(turns),
        who=who,
        prefs=prefs,
        assets=assets,
        followups=followups,
    )
    write_topic(directory, PROFILE_SLUG, description="用户画像", body=body)
    _write_meta(directory, turns)


def read_turns(directory: Path) -> int:
    path = directory / META_NAME
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("turns"):
            _, _, raw = line.partition("=")
            try:
                return max(0, int(raw.strip()))
            except ValueError:
                return 0
    return 0


def public_memory(
    directory: Path,
    settings: MemorySettings | None = None,
    *,
    query: str = "",
    scope: str = "user",
) -> dict:
    settings = settings or load_memory_settings()
    from witty_agent.memory_harvest import scrub_transient_domain

    scrub_transient_domain(directory, settings)
    cells = []
    for cell in settings.cells:
        body = topic_body(directory, cell.id)
        cells.append(
            {
                "id": cell.id,
                "title": cell.title,
                "description": cell.description,
                "body": body,
                "count": len(_bullets(body)),
            }
        )
    taxonomy = []
    for item in settings.taxonomy:
        if not (directory / f"{item.id}.md").is_file():
            continue
        body = topic_body(directory, item.id)
        taxonomy.append(
            {
                "id": item.id,
                "title": item.title,
                "body": body,
                "count": len(_bullets(body)),
            }
        )
    from witty_agent.timeline import list_timeline_events

    query_text = query.strip()
    hits = retrieve_hits(directory, query_text, settings) if query_text else []
    label = scope if scope in {"user", "workspace"} else "user"
    for item in hits:
        item["scope"] = label
    extras = extra_topics(directory, settings)
    for item in extras:
        item["scope"] = label
    archive_rows = list_archive(directory)
    for item in archive_rows:
        item["scope"] = label
    return {
        "index": cap_index(read_index(directory)),
        "lattice": render_lattice(directory, settings),
        "profile": read_profile(directory),
        "turns": read_turns(directory),
        "cells": cells,
        "taxonomy": taxonomy,
        "extras": extras,
        "archive": archive_rows,
        "timeline": _timeline_text(directory),
        "timeline_events": list_timeline_events(directory),
        "links": _public_links(directory, settings),
        "retrieved": _format_hit_list(hits),
        "hits": hits,
        "query": query_text,
        "scope": label,
        "empty": _empty_state(
            directory,
            settings,
            query=query_text,
            hits=hits,
            cells=cells,
            taxonomy=taxonomy,
            extras=extras,
            scope=label,
        ),
    }


def rebuild_memory_index(directory: Path, settings: MemorySettings | None = None) -> None:
    settings = settings or load_memory_settings()
    lines = ["# 九宫格记忆", "", render_lattice(directory, settings), "", "## 格子"]
    for cell in settings.cells:
        lines.append(f"- [{cell.id}]({cell.id}.md) — {cell.title}")
    tax_lines = [
        f"- [{item.id}]({item.id}.md) — {item.title}"
        for item in settings.taxonomy
        if (directory / f"{item.id}.md").is_file()
    ]
    if tax_lines:
        lines.extend(["", "## 分类", *tax_lines])
    lines.extend(["", f"- [{PROFILE_SLUG}]({PROFILE_SLUG}.md) — 用户画像"])
    extras = []
    reserved = {cell.id for cell in settings.cells} | {item.id for item in settings.taxonomy} | {PROFILE_SLUG, "timeline"}
    if (directory / "timeline.md").is_file():
        lines.extend(["", "- [timeline](timeline.md) — 时间线"])
    for path in sorted(directory.glob("*.md")):
        if path.name == INDEX_NAME or path.stem in reserved:
            continue
        extras.append(f"- [{path.stem}]({path.name})")
    if extras:
        lines.extend(["", "## 其他条目", *extras])
    (directory / INDEX_NAME).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def extra_topics(directory: Path, settings: MemorySettings | None = None) -> list[dict[str, object]]:
    settings = settings or load_memory_settings()
    reserved = {cell.id for cell in settings.cells} | {item.id for item in settings.taxonomy} | {
        PROFILE_SLUG,
        "timeline",
        Path(INDEX_NAME).stem,
    }
    rows: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.md")):
        if path.name == INDEX_NAME or path.stem in reserved:
            continue
        body = topic_body(directory, path.stem)
        if not body:
            continue
        rows.append(
            {
                "id": path.stem,
                "slug": path.stem,
                "title": path.stem,
                "body": body,
                "count": len(_bullets(body)),
                "kind": "extra",
            }
        )
    return rows


def attach_workspace_public(
    payload: dict[str, object],
    workspace_dir: Path | None,
    *,
    query: str = "",
) -> dict[str, object]:
    """Fold workspace notes (decisions / tool facts) into a user-scope memory payload."""
    if workspace_dir is None:
        return payload
    ws = public_memory(workspace_dir, query=query, scope="workspace")
    topics: list[dict[str, object]] = []
    for cell in ws.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        if int(cell.get("count") or 0) <= 0:
            continue
        topics.append({**cell, "kind": "cell", "scope": "workspace"})
    for extra in ws.get("extras") or []:
        if isinstance(extra, dict):
            topics.append({**extra, "kind": "extra", "scope": "workspace"})
    payload["workspace_topics"] = topics
    if query:
        seen = {(str(item.get("slug")), str(item.get("text"))) for item in payload.get("hits") or []}
        merged: list[dict[str, object]] = list(payload.get("hits") or [])
        for hit in ws.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            key = (str(hit.get("slug")), str(hit.get("text")))
            if key in seen:
                continue
            merged.append(hit)
            seen.add(key)
        payload["hits"] = order_hits_working_first(merged)
        payload["retrieved"] = _format_hit_list(payload["hits"])
    payload["empty"] = _merge_empty(
        payload.get("empty") if isinstance(payload.get("empty"), dict) else {},
        ws.get("empty") if isinstance(ws.get("empty"), dict) else {},
        query=query,
        hits=payload.get("hits") or [],
    )
    return payload


def attach_retrieval(memory: SessionMemory, query: str) -> SessionMemory:
    user_hits = retrieve_hits(memory.user_dir, query)
    for item in user_hits:
        item["scope"] = "user"
    workspace_hits: list[dict[str, object]] = []
    if memory.workspace_dir is not None:
        seen = {str(item.get("text") or "").casefold() for item in user_hits}
        for item in retrieve_hits(memory.workspace_dir, query):
            text = str(item.get("text") or "").casefold()
            if not text or text in seen:
                continue
            item["scope"] = "workspace"
            workspace_hits.append(item)
            seen.add(text)
    ranked = budget_hits(order_hits_working_first((*user_hits, *workspace_hits)))
    combined = tuple(item for item in ranked if item.get("decision") != "ignore")
    empty: dict[str, object] = {}
    if not combined:
        user_empty = directory_empty_state(memory.user_dir, query, user_hits, scope="user")
        workspace_empty = (
            directory_empty_state(memory.workspace_dir, query, workspace_hits, scope="workspace")
            if memory.workspace_dir is not None
            else {}
        )
        empty = _merge_empty(user_empty, workspace_empty, query=query, hits=combined)
    else:
        seen_slugs = {str(item.get("slug") or "") for item in combined if item.get("slug")}
        archive: list[dict[str, object]] = []
        for directory, scope in (
            (memory.user_dir, "user"),
            (memory.workspace_dir, "workspace"),
        ):
            if directory is None:
                continue
            for item in overlapping_archive_hints(
                directory, query, scope=scope, exclude=seen_slugs
            ):
                slug = str(item.get("id") or "")
                if not slug or slug in seen_slugs:
                    continue
                archive.append(item)
                seen_slugs.add(slug)
        if archive:
            empty = {
                "reason": "",
                "archive": archive[:8],
                "archive_count": sum(int(item.get("count") or 0) for item in archive[:8]),
            }
    return SessionMemory(
        user_dir=memory.user_dir,
        user_index=memory.user_index,
        workspace_key=memory.workspace_key,
        workspace_dir=memory.workspace_dir,
        workspace_index=memory.workspace_index,
        lattice=memory.lattice,
        profile=memory.profile,
        timeline=memory.timeline,
        retrieved=_format_hit_list(list(combined)),
        hits=combined,
        empty=empty,
    )


def directory_empty_state(
    directory: Path,
    query: str,
    hits: list[dict[str, object]],
    *,
    scope: str,
    settings: MemorySettings | None = None,
) -> dict[str, object]:
    settings = settings or load_memory_settings()
    cells = [
        {
            "id": cell.id,
            "title": cell.title,
            "count": len(_bullets(topic_body(directory, cell.id))),
        }
        for cell in settings.cells
    ]
    taxonomy = []
    for item in settings.taxonomy:
        body = topic_body(directory, item.id)
        if not body:
            continue
        taxonomy.append({"id": item.id, "title": item.title, "count": len(_bullets(body))})
    return _empty_state(
        directory,
        settings,
        query=query,
        hits=hits,
        cells=cells,
        taxonomy=taxonomy,
        extras=extra_topics(directory, settings),
        scope=scope,
    )


def _populated_hints(
    cells: list[dict[str, object]],
    taxonomy: list[dict[str, object]],
    extras: list[dict[str, object]],
    *,
    scope: str,
    limit: int = 12,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    def add(item: dict[str, object], kind: str, default_scope: str) -> None:
        slug = str(item.get("id") or item.get("slug") or "")
        label = str(item.get("scope") or default_scope or "")
        key = (slug, label)
        if not slug or key in seen:
            return
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return
        seen.add(key)
        rows.append(
            {
                "id": slug,
                "title": str(item.get("title") or slug),
                "count": count,
                "kind": kind,
                "scope": label,
            }
        )

    for cell in cells:
        add(cell, "cell", scope)
    for item in taxonomy:
        add(item, "tax", scope)
    for extra in extras:
        add(extra, str(extra.get("kind") or "extra"), scope)
    return rows[:limit]


def list_archive(directory: Path) -> list[dict[str, object]]:
    archive = directory / "archive"
    if not archive.is_dir():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(archive.glob("*.md")):
        body = topic_body(archive, path.stem)
        count = len(_bullets(body))
        if not count:
            continue
        rows.append(
            {
                "id": f"archive/{path.stem}",
                "slug": path.stem,
                "title": f"归档·{path.stem}",
                "body": body,
                "count": count,
            }
        )
    return rows


def _archive_hints(
    archive_rows: list[dict[str, object]],
    tokens: list[str],
    settings: MemorySettings,
    scope: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in archive_rows:
        hint: dict[str, object] = {
            "id": item["id"],
            "title": item["title"],
            "count": item["count"],
            "kind": "archive",
            "scope": scope,
        }
        body = str(item.get("body") or "")
        score = _overlap_score(body, tokens) if tokens else 0
        if score >= settings.retrieve_min_score:
            hint["overlap"] = True
            excerpt = _archive_excerpt(body, tokens)
            if excerpt:
                hint["excerpt"] = excerpt
        rows.append(hint)
        if len(rows) >= 8:
            break
    return rows


def overlapping_archive_hints(
    directory: Path,
    query: str,
    *,
    scope: str,
    exclude: set[str] | None = None,
    settings: MemorySettings | None = None,
) -> list[dict[str, object]]:
    """工作集已命中时，重叠归档只作浏览，不升 Recalled。"""
    settings = settings or load_memory_settings()
    tokens = _query_tokens(query, settings.stopwords)
    if not tokens:
        return []
    skip = {item for item in (exclude or set()) if item}
    rows: list[dict[str, object]] = []
    for item in _archive_hints(list_archive(directory), tokens, settings, scope):
        if not item.get("overlap"):
            continue
        slug = str(item.get("id") or "")
        if not slug or slug in skip:
            continue
        rows.append(item)
        skip.add(slug)
    return rows


def _archive_excerpt(body: str, tokens: list[str], *, limit: int = 80) -> str:
    pieces = _bullets(body) or ([body.strip()] if body.strip() else [])
    for piece in pieces:
        if _overlap_score(piece, tokens) <= 0:
            continue
        text = " ".join(str(piece).lstrip("- ").split())
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"
    return ""


def _archive_bullet_count(directory: Path) -> int:
    return sum(int(item.get("count") or 0) for item in list_archive(directory))


def _empty_reason(query: str, tokens: list[str], hits: list[dict[str, object]]) -> str:
    if not str(query or "").strip() or hits:
        return ""
    return "too_generic" if not tokens else "no_overlap"


def _empty_state(
    directory: Path,
    settings: MemorySettings,
    *,
    query: str,
    hits: list[dict[str, object]],
    cells: list[dict[str, object]],
    taxonomy: list[dict[str, object]],
    extras: list[dict[str, object]],
    scope: str,
) -> dict[str, object]:
    tokens = _query_tokens(query, settings.stopwords)
    archive_rows = list_archive(directory)
    archive_hints = _archive_hints(archive_rows, tokens, settings, scope)
    return {
        "reason": _empty_reason(query, tokens, hits),
        "tokens": tokens[:8],
        "populated": _populated_hints(cells, taxonomy, extras, scope=scope),
        "archive": archive_hints[:8],
        "archive_count": sum(int(item.get("count") or 0) for item in archive_hints),
    }


def _merge_empty(
    user_empty: dict[str, object],
    workspace_empty: dict[str, object],
    *,
    query: str,
    hits: list[dict[str, object]] | tuple[dict[str, object], ...],
) -> dict[str, object]:
    tokens = [str(item) for item in (user_empty.get("tokens") or workspace_empty.get("tokens") or []) if item]
    populated: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*(user_empty.get("populated") or []), *(workspace_empty.get("populated") or [])]:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("id") or ""), str(item.get("scope") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        populated.append(item)
    archive: list[dict[str, object]] = []
    seen_arch: set[tuple[str, str]] = set()
    for item in [*(user_empty.get("archive") or []), *(workspace_empty.get("archive") or [])]:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("id") or ""), str(item.get("scope") or ""))
        if not key[0] or key in seen_arch:
            continue
        seen_arch.add(key)
        archive.append(item)
    return {
        "reason": _empty_reason(query, tokens, list(hits)),
        "tokens": tokens[:8],
        "populated": populated[:16],
        "archive": archive[:8],
        "archive_count": int(user_empty.get("archive_count") or 0) + int(workspace_empty.get("archive_count") or 0),
    }


def _load_index(directory: Path, settings: MemorySettings, *, archive: bool):
    """取（或重建）一份倒排索引。按目录内容指纹缓存，改了记忆下一轮自动重建。"""
    from witty_agent.memory_index import build_index, directory_signature

    target = (directory / "archive") if archive else directory
    key = (str(target.resolve()) if target.exists() else str(target), archive)
    signature = directory_signature(target)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached.signature == signature:
        return cached
    rows = (
        _archive_topic_rows(directory)
        if archive
        else _all_topics(directory, settings, include_archive=False)
    )
    index = build_index(
        rows,
        _bullets,
        _scoreable_text,
        signature=signature,
        rare_corpus_min=settings.retrieve_rare_corpus_min,
        rare_df_ratio=settings.retrieve_rare_df_ratio,
    )
    _INDEX_CACHE[key] = index
    return index


def clear_memory_index_cache() -> None:
    _INDEX_CACHE.clear()


def retrieve_hits(
    directory: Path,
    query: str,
    settings: MemorySettings | None = None,
    *,
    today: date | None = None,
) -> list[dict[str, object]]:
    settings = settings or load_memory_settings()
    from witty_agent.memory_index import expand_aliases

    # 同义说法在配置里打通（本月 / 这个月 / 当月）。原句不动，只把同义词接在后面。
    expanded = expand_aliases(query, settings.aliases)
    tokens = _query_tokens(expanded, settings.stopwords)
    if not tokens:
        return []
    now = today or date.today()
    ranked = _rank_indexed(
        _load_index(directory, settings, archive=False),
        query,
        tokens,
        settings,
        now,
        standing_ok=True,
    )
    picked = _dedupe_hits(ranked, settings.retrieve_limit)
    layer = "working"
    if not picked and settings.retrieve_archive:
        ranked = _rank_indexed(
            _load_index(directory, settings, archive=True),
            query,
            tokens,
            settings,
            now,
            standing_ok=False,
            min_score=settings.retrieve_archive_min_score,
        )
        picked = _dedupe_hits(ranked, settings.retrieve_limit)
        layer = "archive"
    if not picked:
        return []
    extra: list[tuple[int, str, str, str]] = []
    if layer == "working":
        from witty_agent.memory_graph import neighbors

        seen_slugs = {item[1] for item in picked}
        seen_text = {item[3].casefold() for item in picked}
        for _score, slug, _title, _piece in picked:
            for other in neighbors(directory, slug):
                if other in seen_slugs:
                    continue
                title, body = _topic_label_body(directory, other, settings)
                neighbor_hits = []
                for piece in _bullets(body) or ([body.strip()] if body.strip() else []):
                    score = _decay_score(_overlap_score(piece, tokens), piece, settings, now)
                    if score < settings.retrieve_min_score or piece.casefold() in seen_text:
                        continue
                    neighbor_hits.append((score, other, title, piece))
                if not neighbor_hits:
                    continue
                neighbor_hits.sort(key=lambda item: item[0], reverse=True)
                extra.append(neighbor_hits[0])
                seen_slugs.add(other)
                seen_text.add(neighbor_hits[0][3].casefold())
                if len(picked) + len(extra) >= settings.retrieve_limit + 2:
                    break
            if len(picked) + len(extra) >= settings.retrieve_limit + 2:
                break
    rows: list[dict[str, object]] = []
    for score, slug, title, piece in [*picked, *extra]:
        text = piece.strip()
        if not text:
            continue
        rows.append(
            {
                "slug": slug,
                "title": title,
                "text": text,
                "score": int(score),
                "layer": layer if not str(slug).startswith("archive/") else "archive",
            }
        )
    return rows


def _archive_topic_rows(directory: Path) -> list[tuple[str, str, str]]:
    archive = directory / "archive"
    if not archive.is_dir():
        return []
    rows: list[tuple[str, str, str]] = []
    for path in sorted(archive.glob("*.md")):
        body = topic_body(archive, path.stem)
        if body:
            rows.append((f"archive/{path.stem}", f"归档·{path.stem}", body))
    return rows


def _rank_indexed(
    index,
    query: str,
    tokens: list[str],
    settings: MemorySettings,
    now: date,
    *,
    standing_ok: bool,
    min_score: int | None = None,
) -> list[tuple[int, str, str, str]]:
    """只给倒排表捞出来的候选打分。

    此前每轮把所有格子的正文读出来、逐条子弹跟每个 query token 比一遍。记忆一多这就是
    每轮固定的线性开销，而绝大多数子弹跟这个问句一个词都不沾。现在先用倒排表取「至少
    共一个词」的那些，再打分。
    """
    from witty_agent.memory_index import score_bullet

    ranked: list[tuple[int, str, str, str]] = []
    floor = settings.retrieve_min_score if min_score is None else min_score
    for position in index.candidates(tokens):
        item = index.bullets[position]
        tax = settings.tax(item.slug)
        tax_bonus = 6 if tax and any(word and word in query for word in tax.keywords) else 0
        overlap = score_bullet(
            _scoreable_text(item.text),
            tokens,
            index=index,
            floor=floor,
        )
        score = overlap + tax_bonus if overlap else 0
        score = _decay_score(score, item.text, settings, now)
        # 偏好和红线是常驻的：命中够格就不因为写得早被衰减扣出召回。
        # 门槛跟着 floor 走——放宽到「沾一个短词」会让 `生产者消费者` 撞出
        # 「生产环境」红线，实测假命中 2/32 → 11/32。
        standing = (
            standing_ok
            and item.slug in {"prefs", "constraints"}
            and overlap >= settings.retrieve_min_score
        )
        if score < floor and not standing:
            continue
        ranked.append((score, item.slug, item.title, item.text))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def retrieve_for_query(directory: Path, query: str, settings: MemorySettings | None = None) -> str:
    return _format_hit_list(retrieve_hits(directory, query, settings))


def _timeline_text(directory: Path) -> str:
    from witty_agent.timeline import render_timeline

    return render_timeline(directory)


def _empty_profile(turns: int) -> str:
    return get_prompt(
        "memory_profile_body",
        turns=str(turns),
        who="尚未记录",
        prefs="尚未记录",
        assets="尚未记录",
        followups="无",
    )


def _write_meta(directory: Path, turns: int) -> None:
    (directory / META_NAME).write_text(f"turns = {turns}\n", encoding="utf-8")


def _bullets(body: str) -> list[str]:
    rows: list[str] = []
    for line in body.splitlines():
        text = line.strip()
        if text.startswith(("- ", "* ")):
            text = text[2:].strip()
        if text:
            rows.append(text)
    return rows


_CITE_TAIL = re.compile(r" \[cite:[^\]]+\]$")


def _clean_bullet(raw: str) -> str:
    text = raw.strip().lstrip("-* ").strip()
    cite = ""
    match = _CITE_TAIL.search(text)
    if match:
        cite = match.group(0)
        text = text[: match.start()]
    return (re.sub(r"\s+", " ", text)[:240] + cite).strip()


def _fact_key(line: str) -> str:
    """一条子弹讲的「事」——去掉日期前缀、cite 尾巴和收尾标点。

    去重此前比的是整串，但 `memory_harvest._stamp` 给每条都盖上当天日期和 cite，
    所以**同一句话隔一天再说就是另一个字符串**，永远撞不上。实测 20 天里用户只讲了
    8 件事，`who` 格 12 个槽被同一件事吃掉 8 个，3 件事被挤出工作集。
    """
    text = _DATE_PREFIX.sub("", _CITE_TAIL.sub("", (line or "").strip()), count=1)
    return re.sub(r"\s+", "", text).strip("。.，,；;！!？?").casefold()


def _merge_fact(rows: list[str], line: str) -> list[str]:
    """同一件事只留一条，且留**最新那条**。

    重述不是新事实，但也不是噪音：用户今天又说了一遍，说明这事还成立。所以拿新句子
    换掉旧句子（日期跟着换新 → 不被 `_decay_score` 扣出召回），并挪到队尾
    （→ 反复提到的事不会被按新鲜度挤走）。

    只认**整句相同**，不认包含关系。试过把「短句是长句子串」也当同一件事，实测同类
    12 对里 4 对该并的并了、8 对里 7 对判错——而且这些「错」有一半复核后其实是对的
    （`以前老陈管检修，现在换成老李了` 该顶掉 `老陈管检修`）。既然连人工标注都摇摆，
    字面包含就分不开「补充说明 / 反转作废 / 碰巧撞上」三种，不做。更硬的一条理由：
    「留最新那条」是方向盲的，用户先说全路径、后来松口提一句，并掉就等于用
    `放在共享盘` 换掉 `放在共享盘 //nas/dispatch/points/ 下面`。同一件事换个说法
    仍会占两个槽，那要语义相似度，不是字符串操作。
    """
    key = _fact_key(line)
    kept = [row for row in rows if _fact_key(row) != key]
    kept.append(line)
    return kept


def cite_tag(session_id: str, seq: int) -> str:
    sid = re.sub(r"[^A-Za-z0-9._-]+", "", session_id or "")[:32]
    if not sid or seq <= 0:
        return ""
    return f"[cite:{sid}#{seq}]"


def topic_switched(previous: str, current: str, settings: MemorySettings | None = None) -> bool:
    cfg = settings or load_memory_settings()
    prev = _query_tokens(previous, cfg.stopwords)
    now = _query_tokens(current, cfg.stopwords)
    if not prev or not now:
        return True
    overlap = len(set(prev) & set(now)) / max(len(set(now)), 1)
    return overlap < max(0.0, float(cfg.topic_switch_overlap))


def budget_hits(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...],
    settings: MemorySettings | None = None,
) -> list[dict[str, object]]:
    cfg = settings or load_memory_settings()
    claim_cap = max(1, int(cfg.inject_claim_cap))
    char_cap = max(200, int(cfg.inject_char_cap))
    used = 0
    chars = 0
    out: list[dict[str, object]] = []
    for item in hits:
        row = dict(item)
        text = str(row.get("text") or "")
        score = int(row.get("score") or 0)
        if used >= claim_cap or chars + len(text) > char_cap:
            row["decision"] = "ignore"
        elif score >= cfg.retrieve_min_score + 2 or used == 0:
            row["decision"] = "use"
            used += 1
            chars += len(text)
        else:
            row["decision"] = "verify"
            used += 1
            chars += len(text)
        out.append(row)
    return out


def _should_skip(text: str, settings: MemorySettings) -> bool:
    """Drop secret-looking lines. Prefix needles (sk-) stay substring; words must be standalone."""
    lowered = (text or "").casefold()
    if not lowered:
        return False
    for needle in settings.skip_needles:
        raw = str(needle or "").casefold().strip()
        if not raw:
            continue
        if raw[-1] in "-_=":
            if raw in lowered:
                return True
            continue
        pattern = re.compile(rf"(?<![a-z0-9_-]){re.escape(raw)}(?![a-z0-9_-])")
        if pattern.search(lowered):
            return True
    return False


def _excerpt(body: str, limit: int = 80) -> str:
    bullets = _bullets(body)
    if not bullets:
        compact = re.sub(r"\s+", " ", body).strip()
        return compact[:limit]
    joined = "；".join(bullets[:3])
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


def _archive_bullets(directory: Path, slug: str, lines: list[str], cap: int) -> None:
    archive = directory / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    existing = _bullets(topic_body(archive, slug))
    for line in lines:
        # 同一件事可以「溢出 → 再被说到 → 再溢出」，所以归档也按事去重，留最新那条。
        existing = _merge_fact(existing, line) if _fact_key(line) else existing
    if len(existing) > cap:
        # 归档满了以前是 `existing[-cap:]`，多出来的**直接消失**，不写日志不留痕。
        # 记忆是用户数据，越界的处置不能是静默删除：挪进 retired/，检索不看，人能翻到。
        _retire_bullets(directory, slug, existing[:-cap])
        existing = existing[-cap:]
    write_topic(
        archive,
        slug,
        description=f"archived {slug}",
        body="\n".join(f"- {item}" for item in existing),
    )


def _retire_bullets(directory: Path, slug: str, lines: list[str]) -> None:
    """归档溢出的条目落到 retired/。只追加，不参与检索。"""
    rows = [item for item in lines if str(item).strip()]
    if not rows:
        return
    retired = directory / RETIRED_DIR
    retired.mkdir(parents=True, exist_ok=True)
    existing = _bullets(topic_body(retired, slug))
    body = "\n".join(f"- {item}" for item in [*existing, *rows])
    write_topic(retired, slug, description=f"retired {slug}", body=body)
    logger.info("记忆退休 slug=%s count=%s path=%s", slug, len(rows), retired / f"{slug}.md")


def workspace_has_content(directory: Path) -> bool:
    """这个工作区目录里有没有真写过东西。

    只看子弹，不看脚手架：`_ensure_index` 会给每个 cwd 建一份空的 MEMORY.md，所以
    「目录存在」什么都不说明。
    """
    if not directory.is_dir():
        return False
    for path in directory.rglob("*.md"):
        if path.name == INDEX_NAME:
            continue
        if _bullets(topic_body(path.parent, path.stem)):
            return True
    return False


def gc_workspace_memory(
    memory_root: Path,
    settings: MemorySettings | None = None,
    *,
    keep: str | None = None,
    today: date | None = None,
) -> list[str]:
    """清掉一条记忆都没写过的工作区目录。返回清掉的 key。

    工作区记忆按 cwd 哈希建目录，`resolve_session_memory` 每次都会建。于是一次性的
    cwd——临时目录、`witty-approve-*` 这种测试工作区、跑完就删的检出——各留一个空壳，
    只增不减。

    **有内容的目录一律不动**，哪怕它的 cwd 早就删了：那是用户数据，clean-up 脚本没资格
    替用户决定。只清空壳，而且还要满足「原 cwd 已不在」或「超过 ttl 没动过」之一。
    """
    settings = settings or load_memory_settings()
    if not settings.gc_enabled or not memory_root.is_dir():
        return []
    now = today or date.today()
    dropped: list[str] = []
    for path in sorted(memory_root.iterdir()):
        if not path.is_dir() or path.name in {"user", keep}:
            continue
        if workspace_has_content(path):
            continue
        marker = path / MARKER_NAME
        cwd_gone = True
        if marker.is_file():
            recorded = marker.read_text(encoding="utf-8").strip()
            cwd_gone = bool(recorded) and not Path(recorded).exists()
        stale = _idle_days(path, now) >= settings.gc_workspace_ttl_days
        if not cwd_gone and not stale:
            continue
        shutil.rmtree(path, ignore_errors=True)
        dropped.append(path.name)
    if dropped:
        logger.info("清理空工作区记忆 count=%s keys=%s", len(dropped), ",".join(dropped[:8]))
    return dropped


def _idle_days(directory: Path, today: date) -> int:
    newest = 0.0
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    if newest <= 0:
        return 0
    from datetime import datetime

    return max(0, (today - datetime.fromtimestamp(newest).date()).days)


def memory_budget(directory: Path, settings: MemorySettings | None = None) -> dict[str, object]:
    """这份记忆现在有多少条、哪几格最满。给预算判断和 `memory_status` 用。"""
    settings = settings or load_memory_settings()
    cells: list[dict[str, object]] = []
    total = 0
    for slug, title, body in _all_topics(directory, settings, include_archive=False):
        count = len(_bullets(body))
        if not count:
            continue
        total += count
        cells.append({"slug": slug, "title": title, "count": count})
    cells.sort(key=lambda item: int(item["count"]), reverse=True)
    archived = 0
    archive = directory / "archive"
    if archive.is_dir():
        for path in archive.glob("*.md"):
            archived += len(_bullets(topic_body(archive, path.stem)))
    retired = 0
    retired_dir = directory / RETIRED_DIR
    if retired_dir.is_dir():
        for path in retired_dir.glob("*.md"):
            retired += len(_bullets(topic_body(retired_dir, path.stem)))
    return {
        "total": total,
        "archived": archived,
        "retired": retired,
        "cells": cells,
        "over_budget": total > settings.consolidate_total_cap,
    }


def _public_links(directory: Path, settings: MemorySettings) -> list[dict[str, str]]:
    from witty_agent.memory_graph import load_links

    titles = {cell.id: cell.title for cell in settings.cells}
    titles.update({item.id: item.title for item in settings.taxonomy})
    titles["timeline"] = "时间线"
    rows: list[dict[str, str]] = []
    for item in load_links(directory):
        rows.append(
            {
                **item,
                "from_title": titles.get(item["from"], item["from"]),
                "to_title": titles.get(item["to"], item["to"]),
            }
        )
    return rows


def _all_topics(
    directory: Path,
    settings: MemorySettings,
    *,
    include_archive: bool = False,
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for cell in settings.cells:
        rows.append((cell.id, cell.title, topic_body(directory, cell.id)))
    for item in settings.taxonomy:
        body = topic_body(directory, item.id)
        if body:
            rows.append((item.id, item.title, body))
    timeline = _timeline_text(directory)
    if timeline:
        rows.append(("timeline", "时间线", timeline))
    if include_archive:
        archive = directory / "archive"
        if archive.is_dir():
            for path in archive.glob("*.md"):
                body = topic_body(archive, path.stem)
                if body:
                    rows.append((f"archive-{path.stem}", f"归档·{path.stem}", body))
    reserved = {cell.id for cell in settings.cells} | {item.id for item in settings.taxonomy} | {
        PROFILE_SLUG,
        "timeline",
        Path(INDEX_NAME).stem,
    }
    for path in sorted(directory.glob("*.md")):
        if path.name == INDEX_NAME or path.stem in reserved:
            continue
        body = topic_body(directory, path.stem)
        if body:
            rows.append((path.stem, path.stem, body))
    return rows


def _topic_label_body(directory: Path, slug: str, settings: MemorySettings) -> tuple[str, str]:
    cell = settings.cell(slug)
    if cell is not None:
        return cell.title, topic_body(directory, slug)
    tax = settings.tax(slug)
    if tax is not None:
        return tax.title, topic_body(directory, slug)
    if slug == "timeline":
        return "时间线", _timeline_text(directory)
    return slug, topic_body(directory, slug)


def _query_tokens(query: str, stopwords: tuple[str, ...] = ()) -> list[str]:
    from witty_agent.memory_index import query_tokens

    return query_tokens(query, stopwords)


def _scoreable_text(body: str) -> str:
    """Drop harvest metadata so tool names / dates do not inflate overlap."""
    text = _DATE_PREFIX.sub("", (body or "").strip(), count=1)
    return _TOOL_NAME_PREFIX.sub("", text, count=1)


def _bullet_age_days(piece: str, today: date) -> int | None:
    match = _DATE_PREFIX.match((piece or "").lstrip("- ").strip())
    if not match:
        return None
    try:
        written = date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return max(0, (today - written).days)


def _decay_score(score: int, piece: str, settings: MemorySettings, today: date) -> int:
    window = settings.retrieve_decay_days
    if window <= 0 or score <= 0:
        return score
    age = _bullet_age_days(piece, today)
    if age is None or age <= window:
        return score
    return score - settings.retrieve_decay_penalty * (age // window)


def _overlap_score(body: str, tokens: list[str]) -> int:
    """不带语料的打分：只按词长给权重，等于 IDF 那一路的退化情形。

    留着是因为它是「一个长词，或两个短词」这条刻度的定义处，`retrieve_hits` 之外还有
    别处按纯字面比对。带语料的打分见 `memory_index.score_bullet`。
    """
    from witty_agent.memory_index import score_bullet

    return score_bullet(_scoreable_text(body), tokens)


def _dedupe_hits(
    ranked: list[tuple[int, str, str, str]], limit: int
) -> list[tuple[int, str, str, str]]:
    picked: list[tuple[int, str, str, str]] = []
    seen: set[str] = set()
    for item in ranked:
        key = item[3].casefold()
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def hit_is_archive(hit: dict[str, object]) -> bool:
    slug = str(hit.get("slug") or hit.get("id") or hit.get("locator") or "").strip()
    layer = str(hit.get("layer") or "")
    return layer == "archive" or slug.startswith("archive/")


def _hit_rank(hit: dict[str, object]) -> tuple[int, int]:
    archive = 1 if hit_is_archive(hit) else 0
    workspace = 1 if str(hit.get("scope") or "") == "workspace" else 0
    return (archive, workspace)


def order_hits_working_first(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
) -> list[dict[str, object]]:
    rows = [item for item in (hits or ()) if isinstance(item, dict)]
    return sorted(rows, key=_hit_rank)


def hits_have_scopes(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
) -> bool:
    saw_user = False
    saw_workspace = False
    for hit in hits or ():
        if not isinstance(hit, dict) or hit_is_archive(hit):
            continue
        if str(hit.get("scope") or "") == "workspace":
            saw_workspace = True
        else:
            saw_user = True
        if saw_user and saw_workspace:
            return True
    return False


def hits_layer(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
) -> str:
    saw_archive = False
    saw_working = False
    for hit in hits or ():
        slug = str(hit.get("slug") or hit.get("id") or "").strip()
        if not slug:
            continue
        if hit_is_archive(hit):
            saw_archive = True
        else:
            saw_working = True
        if saw_archive and saw_working:
            return "mixed"
    if saw_archive:
        return "archive"
    return "working"


def format_hit_list(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    excerpt_limit: int = 180,
) -> str:
    lines: list[str] = []
    ordered = order_hits_working_first(hits)
    if hits_layer(ordered) == "mixed":
        banner = get_prompt("recalled_layer_mixed").strip()
        if banner:
            lines.append(banner)
    elif hits_have_scopes(ordered):
        banner = get_prompt("recalled_scope_mixed").strip()
        if banner:
            lines.append(banner)
    for item in ordered:
        title = str(item.get("title") or item.get("slug") or "")
        if item.get("scope") == "workspace" and title and not title.startswith("工作区"):
            title = f"工作区·{title}"
        if hit_is_archive(item) and title and not title.startswith("归档"):
            title = f"归档·{title}"
        decision = str(item.get("decision") or "").strip()
        if decision:
            title = f"[{decision}] {title}"
        line = _format_recall(
            str(item.get("slug") or ""),
            title,
            str(item.get("text") or ""),
            excerpt_limit=excerpt_limit,
        )
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _format_hit_list(hits: list[dict[str, object]]) -> str:
    return format_hit_list(hits)


def _format_recall(slug: str, title: str, body: str, *, excerpt_limit: int = 180) -> str:
    excerpt = _excerpt(body, limit=excerpt_limit)
    if not excerpt:
        return ""
    return f"- {title} (`{slug}`): {excerpt}"
