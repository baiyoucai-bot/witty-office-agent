"""指令刷新：指令文件更新/移除，以及触及子目录时追加嵌套指令。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from witty_agent.context import (
    escape_instruction_text,
    instruction_candidate_rank,
    instruction_display,
    is_instruction_name,
    load_context_files,
    load_instructions_in,
)
from witty_agent.prompts import get_prompt
from witty_agent.system_prompt import format_project_context
from witty_agent.types import AgentMessage

_MAX_CHARS = 8000
BASELINE_SOURCE = "plugin:instruction-baseline"


def instruction_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def visible_instruction_baseline(messages: list[AgentMessage] | None) -> bool:
    return any(getattr(item, "source", "") == BASELINE_SOURCE for item in messages or ())


def visible_baseline_identity(messages: list[AgentMessage] | None) -> str:
    for item in reversed(messages or ()):
        if getattr(item, "source", "") == BASELINE_SOURCE:
            return str((getattr(item, "meta", None) or {}).get("digest") or "")
    return ""


def instruction_baseline_identity(workspace: str) -> str:
    """基线身份：发现/优先级/项目根/预算，不含正文。"""
    from witty_agent.context import (
        find_project_root,
        instruction_file_candidates,
        project_root_markers,
    )
    from witty_agent.runtime import context_settings

    cwd = Path(workspace).resolve()
    root = find_project_root(cwd)
    try:
        rel = Path(os.path.relpath(root, cwd)).as_posix()
    except ValueError:
        rel = str(root)
    if rel in {".", ""}:
        rel = ""
    cfg = context_settings()
    base = list(instruction_file_candidates(local=False))
    local = [name for name in instruction_file_candidates(local=True) if name not in base]
    payload = {
        "projectRoot": rel,
        "projectRootMarkers": list(project_root_markers()),
        "maxBytes": int(cfg["max_chars"]),
        "maxSourceBytes": int(cfg["max_source_bytes"]),
        "instructionFileCandidates": base,
        "localInstructionFileCandidates": local,
    }
    return instruction_digest(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def instruction_baseline_message(
    workspace: str,
    *,
    replace: bool = False,
) -> AgentMessage | None:
    """首轮折入基线；身份变了则整份替换。"""
    files = load_context_files(workspace)
    text = format_project_context(files)
    digest = instruction_baseline_identity(workspace)
    if not text.strip():
        if not replace:
            return None
        text = get_prompt("instruction_baseline_replace_empty")
        action = "replace"
    elif replace:
        text = get_prompt("instruction_baseline_replace") + "\n\n" + text
        action = "replace"
    else:
        action = "set"
    return AgentMessage(
        role="user",
        content=text,
        source=BASELINE_SOURCE,
        meta={"action": action, "baseline": "true", "digest": digest},
    )


def instruction_trimmed_digest(text: str) -> str:
    """去首尾空白后的 SHA-1，作同目录去重键。"""
    return instruction_digest(text.strip())


def _earlier_sibling_duplicate(path: Path, text: str) -> bool:
    want = text.strip()
    if not want:
        return False
    parent = path.parent
    rank = instruction_candidate_rank(path.name)
    for sibling in parent.iterdir() if parent.is_dir() else []:
        if sibling.resolve() == path.resolve():
            continue
        if not is_instruction_name(sibling.name):
            continue
        if instruction_candidate_rank(sibling.name) >= rank:
            continue
        try:
            if not sibling.is_file():
                continue
            other = _read_instruction_text(sibling)
        except (OSError, UnicodeDecodeError):
            continue
        if not other:
            continue
        if instruction_trimmed_digest(other) == instruction_trimmed_digest(text):
            return True
    return False


def _scope_digest(path: Path, text: str) -> str:
    if not path.is_file() or not text.strip() or _earlier_sibling_duplicate(path, text):
        return ""
    return instruction_digest(text)


def instruction_fs_version(path: Path) -> str | None:
    """不透明 stat 指纹。None=探测失败；空串=确认不存在。"""
    try:
        info = path.stat()
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return ""
    mtime = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
    return f"{info.st_dev}:{info.st_ino}:{mtime}:{info.st_size}"


def _read_instruction_text(path: Path) -> str | None:
    """超单源字节上限 / 解码失败当暂时不可用。空串=确认不是可读文件。"""
    if not path.is_file():
        return ""
    from witty_agent.context import _read_instruction, _source_bytes

    loaded = _read_instruction(path, max_source_bytes=_source_bytes(None))
    if loaded is None:
        return None
    return loaded.get("content") or ""


def _store_instruction_version(
    versions: dict[str, dict[str, str]] | None,
    key: str,
    path: Path,
    digest: str,
    text: str = "",
) -> None:
    if versions is None:
        return
    version = instruction_fs_version(path)
    if version is None:
        versions.pop(key, None)
        return
    versions[key] = {
        "version": version,
        "digest": digest,
        "trimmedDigest": instruction_trimmed_digest(text) if text else "",
    }


def _dirty_instruction_dirs(
    seen: dict[str, str],
    versions: dict[str, dict[str, str]] | None,
) -> set[str]:
    dirty: set[str] = set()
    for key in seen:
        current = instruction_fs_version(Path(key))
        if current is None:
            continue
        cached = (versions or {}).get(key)
        if not cached or cached.get("version") != current:
            dirty.add(str(Path(key).parent))
    return dirty


def seed_instruction_seen(workspace: str) -> dict[str, str]:
    return {
        str(Path(item["path"]).resolve()): instruction_digest(item.get("content") or "")
        for item in load_context_files(workspace)
    }


def fold_instruction_seen(events: list) -> dict[str, str]:
    """从会话日志还原 path→digest：SHA-1 落在持久源里。"""
    seen: dict[str, str] = {}
    for event in events:
        kind = getattr(event, "type", "") or ""
        data = getattr(event, "data", None) or {}
        if not isinstance(data, dict):
            continue
        if kind == "turn/instruction-update":
            path = str(data.get("path") or "")
            digest = data.get("digest")
            if path and isinstance(digest, str):
                seen[path] = digest
        elif kind in {"turn/instruction-additional", "turn/instruction-baseline"}:
            digests = data.get("digests") or {}
            if not isinstance(digests, dict):
                continue
            for path, digest in digests.items():
                if path and isinstance(digest, str):
                    seen[str(path)] = digest
    return seen


def instruction_offline_transitions(
    workspace: str,
    seen: dict[str, str],
    *,
    versions: dict[str, dict[str, str]] | None = None,
) -> list[AgentMessage]:
    """身份兼容时，离线增删改走 set/replace/remove，不整份替换。"""
    current = seed_instruction_seen(workspace)
    notices: list[AgentMessage] = []
    for key in list(seen):
        if key in current or not seen.get(key):
            continue
        seen[key] = ""
        path = Path(key)
        _store_instruction_version(versions, key, path, "")
        notices.append(
            _instruction_notice(
                "instruction_removed",
                shown=escape_instruction_text(instruction_display(path, workspace)),
                source="plugin:instruction-update",
                action="remove",
                key=key,
            )
        )
    for key, digest in current.items():
        prior = seen.get(key)
        if prior == digest:
            continue
        path = Path(key)
        try:
            text = _read_instruction_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        if text is None:
            continue
        shown = escape_instruction_text(instruction_display(path, workspace))
        seen[key] = digest
        _store_instruction_version(versions, key, path, digest, text)
        body = text
        if len(body) > _MAX_CHARS:
            body = f"{body[:_MAX_CHARS]}\n{get_prompt('instruction_updated_omitted')}"
        if not prior:
            notices.append(
                _instruction_notice(
                    "instruction_additional",
                    shown=shown,
                    source="plugin:instruction-additional",
                    action="set",
                    key=key,
                    digest=digest,
                    body=escape_instruction_text(body),
                )
            )
            continue
        notices.append(
            _instruction_notice(
                "instruction_updated",
                shown=shown,
                source="plugin:instruction-update",
                action="replace",
                key=key,
                digest=digest,
                body=escape_instruction_text(body),
            )
        )
    return notices


def resolved_instruction_key(workspace: str, raw: str) -> str:
    from witty_agent.sandbox import resolve_allowed

    try:
        return str(resolve_allowed(workspace, raw, follow=True))
    except ValueError:
        return str(raw or "")


def remember_instruction_path(
    workspace: str,
    raw: str,
    seen: dict[str, str],
    *,
    versions: dict[str, dict[str, str]] | None = None,
) -> None:
    if not is_instruction_name(raw):
        return
    from witty_agent.sandbox import resolve_allowed

    try:
        path = resolve_allowed(workspace, raw, follow=True)
    except ValueError:
        return
    if not is_instruction_name(path.name):
        return
    try:
        text = _read_instruction_text(path)
    except (OSError, UnicodeDecodeError):
        return
    if text is None:
        return
    digest = _scope_digest(path, text)
    seen[str(path)] = digest
    _store_instruction_version(versions, str(path), path, digest, text)


def instruction_update_hint(
    workspace: str,
    raw: str,
    *,
    seen: dict[str, str] | None = None,
    versions: dict[str, dict[str, str]] | None = None,
) -> AgentMessage | None:
    if not is_instruction_name(raw):
        return None
    from witty_agent.sandbox import resolve_allowed

    try:
        path = resolve_allowed(workspace, raw, follow=True)
    except ValueError:
        return None
    if not is_instruction_name(path.name):
        return None
    shown = escape_instruction_text(instruction_display(path, workspace))
    try:
        text = _read_instruction_text(path)
    except (OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    digest = _scope_digest(path, text)
    key = str(path)
    if seen is not None and seen.get(key) == digest:
        _store_instruction_version(versions, key, path, digest, text)
        return None
    if seen is not None:
        seen[key] = digest
    _store_instruction_version(versions, key, path, digest, text)
    if not digest:
        return _instruction_notice(
            "instruction_removed",
            shown=shown,
            source="plugin:instruction-update",
            action="remove",
            key=key,
        )
    if len(text) > _MAX_CHARS:
        text = f"{text[:_MAX_CHARS]}\n{get_prompt('instruction_updated_omitted')}"
    return _instruction_notice(
        "instruction_updated",
        shown=shown,
        source="plugin:instruction-update",
        action="replace",
        key=key,
        digest=digest,
        body=escape_instruction_text(text),
    )


def instruction_reconcile_seen(
    workspace: str,
    seen: dict[str, str],
    *,
    versions: dict[str, dict[str, str]] | None = None,
) -> list[AgentMessage]:
    """每次 first-party 触及对账已加载 scope；读失败当暂时不可用，不当删除。"""
    notices: list[AgentMessage] = []
    dirty = _dirty_instruction_dirs(seen, versions) if versions is not None else set()
    for key in list(seen):
        path = Path(key)
        version = instruction_fs_version(path)
        if version is None:
            continue
        cached = (versions or {}).get(key)
        if (
            versions is not None
            and cached
            and cached.get("version") == version
            and cached.get("digest") == seen.get(key)
            and str(path.parent) not in dirty
        ):
            continue
        try:
            text = _read_instruction_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        if text is None:
            continue
        digest = _scope_digest(path, text)
        _store_instruction_version(versions, key, path, digest, text)
        if seen.get(key) == digest:
            continue
        seen[key] = digest
        shown = escape_instruction_text(instruction_display(path, workspace))
        if not digest:
            notices.append(
                _instruction_notice(
                    "instruction_removed",
                    shown=shown,
                    source="plugin:instruction-update",
                    action="remove",
                    key=key,
                )
            )
            continue
        body = text
        if len(body) > _MAX_CHARS:
            body = f"{body[:_MAX_CHARS]}\n{get_prompt('instruction_updated_omitted')}"
        notices.append(
            _instruction_notice(
                "instruction_updated",
                shown=shown,
                source="plugin:instruction-update",
                action="replace",
                key=key,
                digest=digest,
                body=escape_instruction_text(body),
            )
        )
    return notices


def instruction_additional_hints(
    workspace: str,
    raw: str,
    *,
    seen: dict[str, str],
    versions: dict[str, dict[str, str]] | None = None,
) -> list[AgentMessage]:
    """成功 read/write/edit 后，注入被触及子目录里尚未加载的 AGENTS.md / CLAUDE.md。"""
    from witty_agent.sandbox import resolve_allowed

    if not str(raw or "").strip():
        return []
    try:
        path = resolve_allowed(workspace, raw, follow=True)
    except ValueError:
        return []
    root = Path(workspace).resolve()
    start = path if path.is_dir() else path.parent
    try:
        start.relative_to(root)
    except ValueError:
        return []
    chain: list[Path] = []
    current = start
    while True:
        chain.append(current)
        if current == root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    extras: list[AgentMessage] = []
    for directory in reversed(chain):
        try:
            found_items = load_instructions_in(directory)
        except (OSError, UnicodeDecodeError):
            continue
        for found in found_items:
            body = found.get("content") or ""
            if not body.strip():
                continue
            key = str(Path(found["path"]).resolve())
            if key in seen:
                continue
            seen[key] = instruction_digest(body)
            _store_instruction_version(versions, key, Path(found["path"]), seen[key], body)
            if len(body) > _MAX_CHARS:
                body = f"{body[:_MAX_CHARS]}\n{get_prompt('instruction_updated_omitted')}"
            extras.append(
                _instruction_notice(
                    "instruction_additional",
                    shown=escape_instruction_text(
                        instruction_display(Path(found["path"]), workspace)
                    ),
                    source="plugin:instruction-additional",
                    action="set",
                    key=key,
                    digest=seen[key],
                    body=escape_instruction_text(body),
                )
            )
    return extras


_INSTRUCTION_SOURCES = frozenset(
    {"plugin:instruction-additional", "plugin:instruction-update"}
)


def _instruction_notice(
    prompt_key: str,
    *,
    shown: str,
    source: str,
    action: str,
    key: str,
    digest: str = "",
    body: str | None = None,
) -> AgentMessage:
    """可见正文无隐藏标记；path/action/digest 只进 meta。"""
    params: dict[str, str] = {"path": shown}
    if body is not None:
        params["body"] = body
    meta: dict[str, str] = {"action": action, "path": key}
    if digest:
        meta["digest"] = digest
    return AgentMessage(
        role="user",
        content=get_prompt(prompt_key, **params),
        source=source,
        meta=meta,
    )


def _visible_instruction_keys(
    messages: list[AgentMessage],
    workspace: str,
    seen: dict[str, str],
) -> set[str]:
    covered: set[str] = set()
    leftover: list[str] = []
    for item in messages:
        if getattr(item, "source", "") not in _INSTRUCTION_SOURCES:
            continue
        meta = getattr(item, "meta", None) or {}
        key = meta.get("path") if isinstance(meta, dict) else None
        if isinstance(key, str) and key:
            covered.add(key)
            continue
        leftover.append(item.text())
    if leftover:
        for key in seen:
            shown = instruction_display(Path(key), workspace)
            escaped = escape_instruction_text(shown)
            if any(shown in text or escaped in text for text in leftover):
                covered.add(key)
    return covered


def instruction_rearm_after_compact(
    before: list[AgentMessage],
    after: list[AgentMessage],
    workspace: str,
    seen: dict[str, str],
) -> list[AgentMessage]:
    """压缩影子化的每个非基线 scope 各自重装；基线事件被影子化则整份重折。"""
    lost = _visible_instruction_keys(before, workspace, seen) - _visible_instruction_keys(
        after, workspace, seen
    )
    extras: list[AgentMessage] = []
    if visible_instruction_baseline(before) and not visible_instruction_baseline(after):
        folded = instruction_baseline_message(workspace)
        if folded is not None:
            extras.append(folded)
    if not lost:
        return extras
    baseline = {
        str(Path(item["path"]).resolve()) for item in load_context_files(workspace)
    }
    for key in lost:
        digest = seen.get(key) or ""
        if not digest or key in baseline:
            continue
        path = Path(key)
        try:
            text = _read_instruction_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        if text is None:
            continue
        if _scope_digest(path, text) != digest:
            continue
        body = text
        if len(body) > _MAX_CHARS:
            body = f"{body[:_MAX_CHARS]}\n{get_prompt('instruction_updated_omitted')}"
        extras.append(
            _instruction_notice(
                "instruction_rearm",
                shown=escape_instruction_text(instruction_display(path, workspace)),
                source="plugin:instruction-additional",
                action="set",
                key=key,
                digest=digest,
                body=escape_instruction_text(body),
            )
        )
    return extras
