"""文件点名：用户点名 file:<path> 注入有界不可信快照。"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from witty_agent.context import stream_text_prefix
from witty_agent.prompts import get_prompt
from witty_agent.types import AgentMessage

_REF = re.compile(r"(?<![\w./])file:(?!//)([^\s]+)")
_HASH_RANGE = re.compile(r"#L(\d+)(?:-L?(\d+))?$", re.I)
_COLON_RANGE = re.compile(r":(\d+)(?:-(\d+))?$")
_MAX_REFS = 3
_MAX_CHARS = 4000
_MAX_DIR_ENTRIES = 40
_SAMPLE = 8192
_UTF8_MAX = 4
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".tox"})
_IMAGE_SUFFIX = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MAX_VISION = 4
_MAX_VISION_BYTES = 8 * 1024 * 1024


def split_file_ref(raw: str) -> tuple[str, int | None, int | None]:
    text = (raw or "").strip()
    match = _HASH_RANGE.search(text)
    if match:
        start = int(match.group(1))
        end = int(match.group(2) or start)
        return text[: match.start()], start, end
    match = _COLON_RANGE.search(text)
    if match:
        start = int(match.group(1))
        end = int(match.group(2) or start)
        return text[: match.start()], start, end
    return text, None, None


def parse_file_refs(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _REF.finditer(text or ""):
        raw = match.group(1).strip()
        if not _looks_like_path(raw):
            continue
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def _looks_like_path(token: str) -> bool:
    path, _start, _end = split_file_ref(token)
    if not path or path in {".", ".."}:
        return False
    return "/" in path or "\\" in path or "." in Path(path).name


def _utf8_sample_ok(sample: bytes) -> bool:
    if not sample:
        return True
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if "unexpected end of data" not in exc.reason:
            return False
        try:
            sample[: exc.start].decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True


def _decode_prefix(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for end in range(len(raw), max(-1, len(raw) - _UTF8_MAX), -1):
        try:
            return raw[:end].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return ""


def project_file_snapshot(
    path: Path,
    *,
    max_chars: int = _MAX_CHARS,
    start_line: int | None = None,
    end_line: int | None = None,
) -> tuple[str, bool]:
    """只流式读前缀或指定行段，不 read_text 整文件。"""
    try:
        known_size = path.stat().st_size
    except OSError:
        return "", False
    if known_size <= 0:
        return "", False
    sample, _ = stream_text_prefix(path, _SAMPLE, known_size=known_size)
    if b"\x00" in sample or not _utf8_sample_ok(sample):
        return "", False
    if start_line is not None:
        return _snapshot_lines(
            path,
            start_line=start_line,
            end_line=end_line or start_line,
            max_chars=max_chars,
        )
    byte_cap = max(max_chars, 1) * _UTF8_MAX
    raw, omitted = stream_text_prefix(path, byte_cap, known_size=known_size)
    text = _decode_prefix(raw)
    if not text:
        return "", False
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, omitted


def _snapshot_lines(
    path: Path,
    *,
    start_line: int,
    end_line: int,
    max_chars: int,
) -> tuple[str, bool]:
    start = max(1, start_line)
    stop = max(start, end_line)
    kept: list[str] = []
    used = 0
    omitted = False
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            for index, line in enumerate(handle, 1):
                if index < start:
                    continue
                if index > stop:
                    break
                text = line.rstrip("\n")
                extra = len(text) + (1 if kept else 0)
                if kept and used + extra > max_chars:
                    omitted = True
                    break
                if extra > max_chars:
                    kept.append(text[:max_chars])
                    omitted = True
                    break
                kept.append(text)
                used += extra
    except (OSError, UnicodeDecodeError):
        return "", False
    return "\n".join(kept), omitted


def project_dir_snapshot(
    path: Path,
    *,
    max_entries: int = _MAX_DIR_ENTRIES,
) -> tuple[str, bool, int]:
    """对照 mention：目录只列一层名字，不递归、不读正文。"""
    try:
        names = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
    except OSError:
        return "", False, 0
    rows: list[str] = []
    for item in names:
        if item.name in _SKIP_DIRS:
            continue
        mark = "/" if item.is_dir() else ""
        rows.append(f"{item.name}{mark}")
    if not rows:
        return "", False, 0
    cap = max(int(max_entries), 1)
    omitted = len(rows) > cap
    return "\n".join(rows[:cap]), omitted, len(rows)


_MENTION_SKIP = _SKIP_DIRS | frozenset({".codegraph"})
_MENTION_MAX = 200
_MENTION_DEPTH = 3


def resolve_mention_root(raw: str, *, allowed: list[str]) -> Path | None:
    """dir 必须落在某个允许的工作区根下。返回该根；越狱或空则 None。"""
    if not raw:
        return None
    try:
        target = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not target.is_dir():
        return None
    for item in allowed:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            base = Path(text).expanduser().resolve()
        except OSError:
            continue
        if not base.is_dir():
            continue
        try:
            target.relative_to(base)
        except ValueError:
            continue
        return base
    return None


def list_mention_paths(
    workspace: str,
    *,
    max_items: int = _MENTION_MAX,
    max_depth: int = _MENTION_DEPTH,
) -> list[str]:
    """工作区里可供 file: mention 的相对路径。目录带尾 /。"""
    root = Path(workspace or "").expanduser()
    if not workspace or not root.is_dir():
        return []
    try:
        base = root.resolve()
    except OSError:
        return []
    cap = max(int(max_items), 1)
    depth_cap = max(int(max_depth), 0)
    out: list[str] = []

    def walk(current: Path, depth: int) -> None:
        if len(out) >= cap or depth > depth_cap:
            return
        try:
            names = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        except OSError:
            return
        for item in names:
            if len(out) >= cap:
                return
            if item.name.startswith(".") or item.name in _MENTION_SKIP:
                continue
            try:
                resolved = item.resolve()
                resolved.relative_to(base)
            except (OSError, ValueError):
                continue
            rel = resolved.relative_to(base).as_posix()
            if item.is_dir():
                out.append(rel + "/")
                walk(item, depth + 1)
            elif item.is_file():
                out.append(rel)

    walk(base, 0)
    return out


def file_reference_hint(
    prompt: str,
    *,
    workspace: str,
    max_chars: int = _MAX_CHARS,
    max_refs: int = _MAX_REFS,
) -> AgentMessage | None:
    refs = parse_file_refs(prompt)
    if not refs:
        return None
    from witty_agent.sandbox import display_path, resolve_allowed

    blocks: list[str] = []
    for raw in refs:
        if len(blocks) >= max_refs:
            break
        loc, start_line, end_line = split_file_ref(raw)
        try:
            path = resolve_allowed(workspace, loc, follow=True)
        except ValueError:
            continue
        if path.is_dir():
            if start_line is not None:
                continue
            excerpt, omitted, total = project_dir_snapshot(path)
            if not excerpt:
                continue
            if omitted:
                excerpt = (
                    f"{get_prompt('file_reference_dir_omitted', shown=str(_MAX_DIR_ENTRIES), total=str(total))}\n"
                    f"{excerpt}"
                )
            shown = display_path(path, workspace).rstrip("/\\") + "/"
            blocks.append(get_prompt("file_reference_item", path=shown, excerpt=excerpt))
            continue
        if not path.is_file():
            continue
        if is_image_path(path):
            shown = display_path(path, workspace)
            blocks.append(
                get_prompt(
                    "file_reference_image",
                    path=shown,
                    mime=image_mime(path),
                    size=str(path.stat().st_size),
                )
            )
            continue
        excerpt, omitted = project_file_snapshot(
            path,
            max_chars=max_chars,
            start_line=start_line,
            end_line=end_line,
        )
        if not excerpt:
            continue
        if omitted:
            excerpt = f"{get_prompt('file_reference_omitted')}\n{excerpt}"
        shown = display_path(path, workspace)
        if start_line is not None:
            shown = f"{shown}#L{start_line}-L{end_line or start_line}"
        blocks.append(get_prompt("file_reference_item", path=shown, excerpt=excerpt))
    if not blocks:
        return None
    return AgentMessage(
        role="user",
        content=get_prompt("file_reference", body="\n\n".join(blocks)),
        source="plugin:file-reference",
    )


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIX


def image_mime(path: Path) -> str:
    return _IMAGE_MIME.get(path.suffix.lower(), "application/octet-stream")


def collect_image_parts(
    text: str,
    *,
    workspace: str,
    max_n: int = _MAX_VISION,
    max_bytes: int = _MAX_VISION_BYTES,
) -> list[dict]:
    """把 file: 图片做成 OpenAI image_url 段。越界或超大的跳过。"""
    from witty_agent.sandbox import resolve_allowed

    parts: list[dict] = []
    for raw in parse_file_refs(text):
        if len(parts) >= max_n:
            break
        loc, _start, _end = split_file_ref(raw)
        try:
            path = resolve_allowed(workspace, loc, follow=True)
        except ValueError:
            continue
        if not path.is_file() or not is_image_path(path):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0 or size > max_bytes:
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        mime = image_mime(path)
        encoded = base64.b64encode(blob).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )
    return parts
