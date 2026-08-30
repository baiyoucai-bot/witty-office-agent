"""工作区 Markdown wiki（Karpathy LLM Wiki）。业务插件，不进内核循环。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from witty_agent import hooks
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.runtime import llmwiki_settings
from witty_agent.tools.registry import ToolSpec, register_tool

logger = get_logger("llmwiki")

_FOLDERS = ("sources", "entities", "concepts", "synthesis")
_BOOTSTRAP = ("SCHEMA.md", "index.md", "log.md")
_SKIP_ORPHAN = {"schema", "index", "log"}
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_WORD = re.compile(r"[a-z0-9_]{2,}")
_HAN = re.compile(r"[\u4e00-\u9fff]+")
_SOFT_CAP = 400
_MANIFEST = "manifest.jsonl"
_SAFE_SLUG = re.compile(r"[^a-z0-9]+")
_URL = re.compile(r"^https?://", re.IGNORECASE)


def _enabled() -> bool:
    return bool(llmwiki_settings().get("enabled", True))


def _workspace() -> Path:
    raw = str(hooks.current_workspace or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.cwd()


def _project(root: str = "") -> Path:
    text = str(root or "").strip()
    if text:
        return Path(text).expanduser()
    return _workspace()


def _wiki_dir(root: str = "") -> Path:
    return _project(root) / "wiki"


def _pages(wiki: Path) -> list[Path]:
    if not wiki.is_dir():
        return []
    return sorted(path for path in wiki.rglob("*.md") if path.is_file())


def _slug(path: Path, wiki: Path) -> str:
    try:
        rel = path.relative_to(wiki).as_posix()
    except ValueError:
        rel = path.name
    if rel.lower().endswith(".md"):
        rel = rel[:-3]
    return rel


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    words = set(_WORD.findall(lowered))
    for run in _HAN.findall(text):
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[i : i + 2] for i in range(len(run) - 1))
    return words


def _has_frontmatter(text: str) -> bool:
    body = text.lstrip("\ufeff")
    if not body.startswith("---"):
        return False
    rest = body[3:]
    return "\n---" in rest or rest.startswith("\n")


def wiki_init(root: str = "") -> str:
    """在工作区建 wiki/ 与 raw/，已有页不覆盖。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    project = _project(root)
    wiki = project / "wiki"
    raw = project / "raw"
    created: list[str] = []
    existed: list[str] = []
    wiki.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "assets").mkdir(exist_ok=True)
    for name in _FOLDERS:
        (wiki / name).mkdir(exist_ok=True)
    templates = {
        "SCHEMA.md": get_prompt("wiki_schema_body"),
        "index.md": get_prompt("wiki_index_body"),
        "log.md": get_prompt("wiki_log_body"),
    }
    for name, body in templates.items():
        target = wiki / name
        if target.exists():
            existed.append(name)
            continue
        target.write_text(body, encoding="utf-8")
        created.append(name)
    if created:
        return get_prompt(
            "wiki_init_ok",
            wiki=str(wiki),
            raw=str(raw),
            created=", ".join(created),
        )
    return get_prompt("wiki_init_exists", wiki=str(wiki))


def wiki_search(query: str, root: str = "", limit: int = 8) -> str:
    """词面检索 wiki 页，不下载向量、不改文件。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    wiki = _wiki_dir(root)
    if not wiki.is_dir():
        return get_prompt("wiki_missing", wiki=str(wiki))
    needles = _tokens(query)
    if not needles:
        return get_prompt("wiki_search_empty")
    cap = max(1, min(int(limit or 8), 20))
    ranked: list[tuple[int, Path, str]] = []
    for path in _pages(wiki):
        text = path.read_text(encoding="utf-8", errors="replace")
        score = len(needles & _tokens(text))
        if score <= 0:
            continue
        snippet = " ".join(text.split())
        if len(snippet) > 80:
            snippet = snippet[:79] + "…"
        ranked.append((score, path, snippet))
    ranked.sort(key=lambda item: (-item[0], str(item[1])))
    if not ranked:
        return get_prompt("wiki_search_empty")
    rows = [
        get_prompt(
            "wiki_search_item",
            score=str(score),
            path=_slug(path, wiki),
            snippet=snippet,
        )
        for score, path, snippet in ranked[:cap]
    ]
    return get_prompt(
        "wiki_search_report",
        query=query,
        count=str(len(rows)),
        rows="\n".join(rows),
    )


def wiki_lint(root: str = "") -> str:
    """检查缺 frontmatter、断链、孤儿页、超长页。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    wiki = _wiki_dir(root)
    pages = _pages(wiki)
    if not pages:
        return get_prompt("wiki_missing", wiki=str(wiki))
    by_slug = {_slug(path, wiki): path for path in pages}
    by_stem = {path.stem: path for path in pages}
    inbound: dict[str, int] = {slug: 0 for slug in by_slug}
    findings: list[str] = []
    for path in pages:
        text = path.read_text(encoding="utf-8", errors="replace")
        slug = _slug(path, wiki)
        if not _has_frontmatter(text):
            findings.append(get_prompt("wiki_lint_no_frontmatter", path=slug))
        lines = text.count("\n") + 1
        if lines > _SOFT_CAP:
            findings.append(get_prompt("wiki_lint_oversize", path=slug, lines=str(lines)))
        for raw in _WIKILINK.findall(text):
            target = raw.strip().replace("\\", "/")
            if target.endswith(".md"):
                target = target[:-3]
            hit = by_slug.get(target) or by_stem.get(Path(target).name)
            if hit is None:
                findings.append(get_prompt("wiki_lint_broken", path=slug, link=raw.strip()))
                continue
            dest = _slug(hit, wiki)
            if dest != slug:
                inbound[dest] = inbound.get(dest, 0) + 1
    for slug, count in inbound.items():
        stem = Path(slug).name.lower()
        if count == 0 and stem not in _SKIP_ORPHAN:
            findings.append(get_prompt("wiki_lint_orphan", path=slug))
    if not findings:
        return get_prompt("wiki_lint_ok", wiki=str(wiki), count=str(len(pages)))
    return get_prompt(
        "wiki_lint_report",
        wiki=str(wiki),
        count=str(len(findings)),
        rows="\n".join(f"- {item}" for item in findings),
    )


def _raw_dir(root: str = "") -> Path:
    return _project(root) / "raw"


def _manifest_path(root: str = "") -> Path:
    return _raw_dir(root) / _MANIFEST


def _today() -> str:
    from witty_agent.time_context import clock_now

    return str(clock_now()["date"])


def _make_slug(text: str) -> str:
    folded = (text or "").strip().casefold()
    slug = _SAFE_SLUG.sub("-", folded).strip("-")
    return (slug or "source")[:60]


def _unique_slug(raw: Path, base: str) -> str:
    slug = base or "source"
    candidate = slug
    index = 2
    while (raw / f"{candidate}.md").exists() or (raw / candidate).exists():
        candidate = f"{slug}-{index}"
        index += 1
    return candidate


def _load_manifest(root: str = "") -> list[dict[str, str]]:
    path = _manifest_path(root)
    if not path.is_file():
        return []
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            rows.append({str(key): str(value) for key, value in item.items()})
    return rows


def _write_manifest(rows: list[dict[str, str]], root: str = "") -> None:
    path = _manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows)
    path.write_text(body, encoding="utf-8")


def _append_log(root: str, line: str) -> None:
    wiki = _wiki_dir(root)
    log = wiki / "log.md"
    if not log.is_file():
        return
    current = log.read_text(encoding="utf-8", errors="replace")
    if not current.endswith("\n"):
        current += "\n"
    log.write_text(current + line.rstrip() + "\n", encoding="utf-8")


def _cited_pages(wiki: Path, source_id: str) -> list[str]:
    found: list[str] = []
    needle = source_id.casefold()
    for path in _pages(wiki):
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text.casefold() or needle in _slug(path, wiki).casefold():
            found.append(_slug(path, wiki))
    return found


def wiki_add(source: str, root: str = "") -> str:
    """把本地文件或公网网址收入 raw/，登记后等模型编译 wiki 页。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    origin = str(source or "").strip()
    if not origin:
        return get_prompt("wiki_add_empty")
    wiki_init(root)
    raw = _raw_dir(root)
    raw.mkdir(parents=True, exist_ok=True)
    rows = _load_manifest(root)
    for item in rows:
        if item.get("origin") == origin and item.get("status") != "removed":
            return get_prompt("wiki_add_exists", source_id=item.get("id") or "", path=item.get("path") or "")
    today = _today()
    if _URL.match(origin):
        from witty_agent.tools.web import web_fetch

        try:
            body = web_fetch(origin)
        except (ValueError, RuntimeError) as exc:
            return str(exc)
        parsed = urlparse(origin)
        leaf = Path(parsed.path.rstrip("/") or parsed.netloc).name or parsed.netloc
        source_id = _unique_slug(raw, f"{today}-{_make_slug(leaf)}")
        dest = raw / f"{source_id}.md"
        dest.write_text(body, encoding="utf-8")
        kind = "url"
    else:
        src = Path(origin).expanduser()
        if not src.is_file():
            return get_prompt("wiki_add_missing", source=origin)
        source_id = _unique_slug(raw, f"{today}-{_make_slug(src.stem)}")
        dest = raw / f"{source_id}{src.suffix or '.md'}"
        dest.write_bytes(src.read_bytes())
        kind = "file"
    rel = dest.as_posix()
    try:
        rel = dest.relative_to(_project(root)).as_posix()
    except ValueError:
        pass
    row = {
        "id": source_id,
        "path": rel,
        "origin": origin,
        "kind": kind,
        "status": "pending",
        "added": today,
    }
    rows.append(row)
    _write_manifest(rows, root)
    _append_log(root, f"- {today} · add · {source_id} · {origin}")
    logger.info("wiki 入库 id=%s kind=%s", source_id, kind)
    return get_prompt("wiki_add_ok", source_id=source_id, path=rel, origin=origin)


def wiki_remove(source_id: str, root: str = "") -> str:
    """从来源清单拿掉一条，删除 raw 原文和对应 sources 摘要。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    asked = str(source_id or "").strip()
    if not asked:
        return get_prompt("wiki_remove_empty")
    rows = _load_manifest(root)
    match: dict[str, str] | None = None
    for item in rows:
        if item.get("id") == asked or item.get("path") == asked or Path(item.get("path") or "").name == asked:
            match = item
            break
    if match is None:
        return get_prompt("wiki_remove_missing", source_id=asked)
    if match.get("status") == "removed":
        return get_prompt("wiki_remove_gone", source_id=match["id"])
    project = _project(root)
    raw_file = Path(match.get("path") or "")
    if not raw_file.is_absolute():
        raw_file = project / raw_file
    deleted: list[str] = []
    if raw_file.is_file():
        raw_file.unlink()
        deleted.append(match.get("path") or str(raw_file))
    summary = _wiki_dir(root) / "sources" / f"{match['id']}.md"
    if summary.is_file():
        summary.unlink()
        deleted.append(f"wiki/sources/{match['id']}")
    match["status"] = "removed"
    _write_manifest(rows, root)
    cited = _cited_pages(_wiki_dir(root), match["id"])
    _append_log(root, f"- {_today()} · remove · {match['id']}")
    logger.info("wiki 删除 id=%s", match["id"])
    return get_prompt(
        "wiki_remove_ok",
        source_id=match["id"],
        deleted=", ".join(deleted) or "-",
        cited=", ".join(cited) or "-",
    )


def list_source_records(root: str = "") -> list[dict[str, str]]:
    return [item for item in _load_manifest(root) if item.get("status") != "removed"]


def public_wiki(root: str = "") -> dict[str, Any]:
    if not _enabled():
        return {
            "enabled": False,
            "ready": False,
            "error": get_prompt("llmwiki_disabled"),
            "sources": [],
            "pending": 0,
            "text": get_prompt("llmwiki_disabled"),
        }
    wiki = _wiki_dir(root)
    raw = _raw_dir(root)
    rows = list_source_records(root)
    pending = sum(1 for item in rows if item.get("status") == "pending")
    return {
        "enabled": True,
        "ready": wiki.is_dir(),
        "wiki": str(wiki),
        "raw": str(raw),
        "sources": rows,
        "pending": pending,
        "text": wiki_sources(root),
        "compile_prompt": get_prompt("wiki_compile_prompt"),
    }


def wiki_sources(root: str = "") -> str:
    """列出已登记的原文来源。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    rows = [item for item in _load_manifest(root) if item.get("status") != "removed"]
    if not rows:
        return get_prompt("wiki_sources_empty")
    lines = [
        get_prompt(
            "wiki_sources_item",
            source_id=item.get("id") or "-",
            kind=item.get("kind") or "-",
            path=item.get("path") or "-",
            origin=item.get("origin") or "-",
            status=item.get("status") or "-",
        )
        for item in rows
    ]
    return get_prompt("wiki_sources_report", count=str(len(lines)), rows="\n".join(lines))


def wiki_stats(root: str = "") -> str:
    """统计 wiki 页数、分类和链密度。"""
    if not _enabled():
        return get_prompt("llmwiki_disabled")
    wiki = _wiki_dir(root)
    pages = _pages(wiki)
    if not pages:
        return get_prompt("wiki_missing", wiki=str(wiki))
    buckets = {name: 0 for name in _FOLDERS}
    buckets["root"] = 0
    links = 0
    for path in pages:
        try:
            rel = path.relative_to(wiki).as_posix()
        except ValueError:
            rel = path.name
        head = rel.split("/", 1)[0]
        key = head if head in buckets else "root"
        if "/" not in rel:
            key = "root"
        buckets[key] = buckets.get(key, 0) + 1
        text = path.read_text(encoding="utf-8", errors="replace")
        links += len(_WIKILINK.findall(text))
    density = f"{links / len(pages):.1f}" if pages else "0"
    return get_prompt(
        "wiki_stats_report",
        wiki=str(wiki),
        pages=str(len(pages)),
        sources=str(buckets.get("sources", 0)),
        entities=str(buckets.get("entities", 0)),
        concepts=str(buckets.get("concepts", 0)),
        synthesis=str(buckets.get("synthesis", 0)),
        links=str(links),
        density=density,
    )


def _spec(name: str, func: Any, properties: dict[str, Any], required: list[str] | None = None) -> None:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    register_tool(
        ToolSpec(
            name=name,
            description=get_prompt(f"tool_desc_{name}"),
            parameters=parameters,
            func=func,
        )
    )


_ROOT = {"root": {"type": "string", "description": get_prompt("wiki_param_root")}}

_spec("wiki_init", wiki_init, _ROOT)
_spec(
    "wiki_search",
    wiki_search,
    {
        **_ROOT,
        "query": {"type": "string", "description": get_prompt("wiki_param_query")},
        "limit": {"type": "integer", "description": get_prompt("wiki_param_limit")},
    },
    ["query"],
)
_spec("wiki_lint", wiki_lint, _ROOT)
_spec("wiki_stats", wiki_stats, _ROOT)
_spec(
    "wiki_add",
    wiki_add,
    {
        **_ROOT,
        "source": {"type": "string", "description": get_prompt("wiki_param_source")},
    },
    ["source"],
)
_spec(
    "wiki_remove",
    wiki_remove,
    {
        **_ROOT,
        "source_id": {"type": "string", "description": get_prompt("wiki_param_source_id")},
    },
    ["source_id"],
)
_spec("wiki_sources", wiki_sources, _ROOT)
