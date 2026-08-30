"""工作区指令：用户全局 $WITTY_HOME/AGENTS.md + 项目根到 cwd 的候选项。"""

from __future__ import annotations

import re
from pathlib import Path

from witty_agent.logging import get_logger

logger = get_logger("context")
_CANDIDATES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")
_LOCAL_CANDIDATES = ("AGENTS.local.md", "CLAUDE.local.md")
_ROOT_MARKERS = (".git",)
USER_GLOBAL_FILE = "AGENTS.md"
_FRAME_CLOSE = re.compile(
    r"</(system-reminder|project_instructions|project_context)\s*>",
    re.IGNORECASE,
)


def escape_instruction_text(text: str) -> str:
    """仓库正文不能提前关掉 harness 框：转义 </system-reminder>。"""
    if not text:
        return text
    return _FRAME_CLOSE.sub(lambda match: f"</\u200b{match.group(1)}>", text)


def instruction_file_candidates(*, local: bool = True) -> tuple[str, ...]:
    """指令文件候选名单。配置非法或为空则用内置名单。"""
    from witty_agent.runtime import context_settings

    cfg = context_settings()
    base = [str(item) for item in cfg.get("instruction_files") or () if item]
    if not base:
        base = list(_CANDIDATES)
    names: list[str] = []
    seen: set[str] = set()
    for item in base:
        if item in seen:
            continue
        seen.add(item)
        names.append(item)
    if local:
        extra = [str(item) for item in cfg.get("local_instruction_files") or () if item]
        if not extra:
            extra = list(_LOCAL_CANDIDATES)
        for item in extra:
            if item in seen:
                continue
            seen.add(item)
            names.append(item)
    return tuple(names)


def is_instruction_name(path: str | Path) -> bool:
    name = Path(path).name
    return name in instruction_file_candidates(local=True)


def instruction_candidate_rank(name: str) -> int:
    """同目录候选项顺序：基线在前，local overlay 在后。"""
    names = list(instruction_file_candidates(local=True))
    try:
        return names.index(name)
    except ValueError:
        return len(names)


DEFAULT_SOURCE_BYTES = 1_048_576
_STREAM_CHUNK = 65_536


def _source_bytes(override: int | None) -> int:
    if override is not None:
        return override
    from witty_agent.runtime import context_settings

    return int(context_settings()["max_source_bytes"])


def load_instruction_in(
    directory: str | Path,
    *,
    max_source_bytes: int | None = None,
) -> dict[str, str] | None:
    items = load_instructions_in(directory, max_source_bytes=max_source_bytes)
    return items[0] if items else None


def load_instructions_in(
    directory: str | Path,
    *,
    local: bool = True,
    max_source_bytes: int | None = None,
) -> list[dict[str, str]]:
    """同目录：全部互异基线候选项，再未重复的 local overlay。全局目录应传 local=False。"""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    out: list[dict[str, str]] = []
    seen_text: set[str] = set()
    seen_paths: set[str] = set()
    names = list(instruction_file_candidates(local=local))
    for name in names:
        item = _read_instruction(
            folder / name, max_source_bytes=_source_bytes(max_source_bytes)
        )
        if item is None:
            continue
        key = str(Path(item["path"]).resolve())
        if key in seen_paths:
            continue
        trimmed = item["content"].strip()
        if not trimmed or trimmed in seen_text:
            continue
        seen_paths.add(key)
        seen_text.add(trimmed)
        out.append(item)
    return out


def global_agent_dir() -> Path:
    """SYSTEM.md / APPEND_SYSTEM.md 目录。指令正文不走这里。"""
    return Path.home() / ".witty" / "agent"


def default_data_root() -> Path:
    return Path.home() / ".witty" / "data"


def witty_home_display() -> str:
    """主目录显示：默认 ~/.witty/data，覆盖后 $WITTY_HOME。"""
    from witty_agent.layout import data_root

    if data_root().expanduser().resolve() == default_data_root().expanduser().resolve():
        return "~/.witty/data"
    return "$WITTY_HOME"


def global_instruction_path() -> Path:
    """用户全局永远是 $WITTY_HOME/AGENTS.md，不用候选项、无 overlay。"""
    from witty_agent.layout import data_root

    return data_root() / USER_GLOBAL_FILE


def project_root_markers() -> tuple[str, ...]:
    from witty_agent.runtime import context_settings

    markers = [str(item) for item in context_settings().get("project_root_markers") or () if item]
    return tuple(markers) if markers else _ROOT_MARKERS


def find_project_root(cwd: str | Path) -> Path:
    """找项目根：向上找到第一个标记；没有则 cwd 自己是根。"""
    current = Path(cwd).resolve()
    probe = current
    markers = project_root_markers()
    while True:
        for marker in markers:
            if (probe / marker).exists():
                return probe
        parent = probe.parent
        if parent == probe:
            return current
        probe = parent


def ancestor_chain(root: str | Path, cwd: str | Path) -> list[Path]:
    """从项目根到 cwd，宽到窄。cwd 不在根下时只留 cwd。"""
    resolved_root = Path(root).resolve()
    current = Path(cwd).resolve()
    try:
        current.relative_to(resolved_root)
    except ValueError:
        return [current]
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current == resolved_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    chain.reverse()
    return chain


def _project_display(root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
    return path.name if rel in {".", ""} else rel


def instruction_display(path: str | Path, workspace: str | Path | None = None) -> str:
    """模型可见路径：全局用符号名，项目内相对工作区。"""
    resolved = Path(path)
    try:
        if resolved.expanduser().resolve() == global_instruction_path().expanduser().resolve():
            return f"{witty_home_display()}/{USER_GLOBAL_FILE}"
    except OSError:
        pass
    if workspace:
        from witty_agent.sandbox import display_path

        return display_path(resolved, str(workspace))
    return str(resolved)


def _load_user_global() -> dict[str, str] | None:
    item = _read_instruction(global_instruction_path())
    if item is None or not (item.get("content") or "").strip():
        return None
    item["display"] = f"{witty_home_display()}/{USER_GLOBAL_FILE}"
    return item


def load_context_files(cwd: str | Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    global_item = _load_user_global()
    if global_item is not None:
        key = str(Path(global_item["path"]).resolve())
        files.append(global_item)
        seen.add(key)
    current = Path(cwd).resolve()
    root = find_project_root(current)
    for directory in ancestor_chain(root, current):
        for found in load_instructions_in(directory):
            key = str(Path(found["path"]).resolve())
            if key in seen:
                continue
            found["display"] = _project_display(root, Path(found["path"]))
            files.append(found)
            seen.add(key)
    logger.info("上下文文件 count=%s", len(files))
    return files


def load_system_overrides() -> tuple[str | None, str]:
    """SYSTEM.md 整段替换，APPEND_SYSTEM.md 追加。项目优先于全局。"""
    custom: str | None = None
    appends: list[str] = []
    for directory in (global_agent_dir(), Path.cwd() / ".witty"):
        system = directory / "SYSTEM.md"
        if system.is_file():
            custom = system.read_text(encoding="utf-8")
        extra = directory / "APPEND_SYSTEM.md"
        if extra.is_file():
            appends.append(extra.read_text(encoding="utf-8"))
    return custom, "\n\n".join(appends)


DEFAULT_INSTRUCTION_BUDGET = 65536


def budget_instruction_files(
    files: list[dict[str, str]],
    *,
    max_chars: int = DEFAULT_INSTRUCTION_BUDGET,
) -> tuple[list[dict[str, str]], list[str], str | None]:
    """先留最具体的。整份丢掉更宽的文件，不够再截最具体的一份。0 = 不限制。"""
    if max_chars <= 0 or not files:
        return list(files), [], None
    kept: list[dict[str, str]] = []
    omitted: list[str] = []
    truncated: str | None = None
    used = 0
    for item in reversed(files):
        body = item.get("content") or ""
        size = len(body)
        if used + size <= max_chars:
            kept.append(dict(item))
            used += size
            continue
        if kept:
            omitted.append(item.get("display") or item.get("path") or "")
            continue
        room = max(0, max_chars)
        cut = dict(item)
        cut["content"] = body[:room]
        kept.append(cut)
        used = room
        truncated = item.get("display") or item.get("path") or ""
    kept.reverse()
    omitted.reverse()
    return kept, [path for path in omitted if path], truncated or None


def stream_text_prefix(
    path: Path,
    max_bytes: int,
    *,
    known_size: int | None = None,
) -> tuple[bytes, bool]:
    """读最多 max_bytes。返回 (前缀, 是否还有剩余)。0 字节帽当空前缀。"""
    if max_bytes <= 0:
        omitted = True if known_size is None else known_size > 0
        return b"", omitted
    chunks: list[bytes] = []
    total = 0
    peeked = b""
    with path.open("rb") as handle:
        while total < max_bytes:
            chunk = handle.read(min(_STREAM_CHUNK, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if known_size is None and total >= max_bytes:
            peeked = handle.read(1)
    data = b"".join(chunks)
    if known_size is not None:
        return data, known_size > len(data)
    return data, bool(peeked)


def _stream_text_bounded(
    path: Path,
    max_source_bytes: int,
    *,
    known_size: int | None = None,
) -> bytes | None:
    """有界读：stat 超帽先跳过，再流式累计字节。0=不限制。"""
    if max_source_bytes > 0 and known_size is not None and known_size > max_source_bytes:
        return None
    if max_source_bytes <= 0:
        with path.open("rb") as handle:
            return handle.read()
    data, omitted = stream_text_prefix(path, max_source_bytes)
    if omitted:
        return None
    return data


def _read_instruction(
    path: Path,
    *,
    max_source_bytes: int = DEFAULT_SOURCE_BYTES,
) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        raw = _stream_text_bounded(
            path, max_source_bytes, known_size=path.stat().st_size
        )
        if raw is None:
            logger.info("指令源文件超帽 path=%s cap=%s", path, max_source_bytes)
            return None
        return {"path": str(path), "content": raw.decode("utf-8")}
    except (OSError, UnicodeDecodeError):
        return None
