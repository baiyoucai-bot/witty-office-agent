"""apply_patch 文本格式。本切片只解析，应用由工具面决定。"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File:"
_UPDATE = "*** Update File:"
_CHANGE = "*** Change File:"
_DELETE = "*** Delete File:"
_RENAME_FROM = "*** Rename from:"
_RENAME_FILE = "*** Rename File:"
_FILE_OPS = (_ADD, _UPDATE, _CHANGE, _DELETE, _RENAME_FROM, _RENAME_FILE)
_MOVE = "*** Move to:"
_RENAME_TO = "*** Rename to:"
_EOF = "*** End of File"
_MODE = "*** Mode:"
_FILEMODE = "*** FileMode:"
_MODE_BITS = re.compile(r"^[0-7]{3,4}$")


@dataclass
class PatchHunk:
    action: str
    path: str
    dest: str = ""
    eof: bool = False
    mode: int | None = None
    lines: list[str] = field(default_factory=list)

    def content(self) -> str:
        lines = list(self.lines)
        while lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return ""
        body = "\n".join(lines)
        if self.eof:
            return body
        return body + "\n"


def _patch_body(raw: str) -> str:
    """有 Begin/End 用框内；模型漏框时从第一条 *** Add/Update/Delete 收到 End 或文末。"""
    has_op = any(marker in raw for marker in _FILE_OPS)
    if _BEGIN in raw:
        start = raw.find(_BEGIN) + len(_BEGIN)
        stop = raw.find(_END, start)
        if stop < 0:
            if not has_op:
                raise ValueError("frame")
            return raw[start:]
        return raw[start:stop]
    if not has_op:
        raise ValueError("frame")
    start = min(raw.find(marker) for marker in _FILE_OPS if marker in raw)
    stop = raw.find(_END, start)
    return raw[start:] if stop < 0 else raw[start:stop]


def parse_file_mode(raw: str) -> int:
    """认 755 / 0755 / 100755（git）。"""
    text = (raw or "").strip().lower().removeprefix("0o")
    if text.startswith("100") and len(text) == 6:
        text = text[3:]
    if not _MODE_BITS.fullmatch(text):
        raise ValueError("mode")
    return stat.S_IMODE(int(text, 8))


def _mode_from_line(line: str) -> int | None:
    for prefix in (_MODE, _FILEMODE):
        if line.startswith(prefix):
            return parse_file_mode(line[len(prefix) :])
    return None


def parse_apply_patch(text: str) -> list[PatchHunk]:
    """解析 *** Begin Patch … *** End Patch。缺框但有文件操作时仍解析。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    body = _patch_body(raw)
    hunks: list[PatchHunk] = []
    current: PatchHunk | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            hunks.append(current)
            current = None

    for line in body.split("\n"):
        mode = _mode_from_line(line)
        if mode is not None:
            if current is None:
                raise ValueError("mode")
            current.mode = mode
            continue
        if line.startswith(_EOF):
            if current is not None:
                current.eof = True
            continue
        if line.startswith(_ADD):
            flush()
            path = line[len(_ADD) :].strip()
            if not path:
                raise ValueError("path")
            current = PatchHunk(action="add", path=path)
        elif line.startswith(_UPDATE) or line.startswith(_CHANGE):
            flush()
            prefix = _UPDATE if line.startswith(_UPDATE) else _CHANGE
            path = line[len(prefix) :].strip()
            if not path:
                raise ValueError("path")
            current = PatchHunk(action="update", path=path)
        elif line.startswith(_DELETE):
            flush()
            path = line[len(_DELETE) :].strip()
            if not path:
                raise ValueError("path")
            current = PatchHunk(action="delete", path=path)
        elif line.startswith(_RENAME_FROM) or line.startswith(_RENAME_FILE):
            flush()
            prefix = _RENAME_FROM if line.startswith(_RENAME_FROM) else _RENAME_FILE
            path = line[len(prefix) :].strip()
            if not path:
                raise ValueError("path")
            current = PatchHunk(action="update", path=path)
        elif line.startswith(_MOVE) or line.startswith(_RENAME_TO):
            prefix = _MOVE if line.startswith(_MOVE) else _RENAME_TO
            dest = line[len(prefix) :].strip()
            if current is None or not dest:
                raise ValueError("path")
            current.action = "move"
            current.dest = dest
        elif current is not None and current.action == "add":
            if line.startswith("+"):
                current.lines.append(line[1:])
            elif line.startswith("\\"):
                if "newline" in line.lower():
                    current.eof = True
                continue
            elif line.startswith(("***", "-")):
                raise ValueError("add_body")
            elif line == "":
                current.lines.append("")
            else:
                raise ValueError("add_body")
        elif current is not None and current.action in {"update", "move"}:
            if line.startswith("***"):
                raise ValueError("update_body")
            current.lines.append(line)
    flush()
    if not hunks:
        raise ValueError("empty")
    return hunks


def apply_patch_paths(text: str) -> list[str]:
    try:
        found: list[str] = []
        for item in parse_apply_patch(text):
            if item.path:
                found.append(item.path)
            if item.dest:
                found.append(item.dest)
        return found
    except ValueError:
        return []


def _split_update_sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("@@") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    return sections


_UNIFIED_AT = re.compile(r"^(?:@@)?\s*-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s*(?:@@)?\s*")


def _section_anchor(lines: list[str]) -> str:
    """@@ 后的定位词。纯 unified 行号不当锚点。"""
    for line in lines:
        if not line.startswith("@@"):
            continue
        rest = line[2:].strip()
        rest = _UNIFIED_AT.sub("", rest).strip().strip("@").strip()
        return rest
    return ""


def _section_old_new(lines: list[str]) -> tuple[str, str]:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for line in lines:
        if line.startswith("@@") or line.startswith("\\"):
            continue
        if line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
        elif line == "":
            old_lines.append("")
            new_lines.append("")
        else:
            old_lines.append(line)
            new_lines.append(line)
    while old_lines and old_lines[-1] == "" and new_lines and new_lines[-1] == "":
        old_lines.pop()
        new_lines.pop()
    return "\n".join(old_lines), "\n".join(new_lines)


def update_old_new(hunk: PatchHunk) -> tuple[str, str]:
    """单个 @@ 段的 old/new。多段请用 update_replacements。"""
    pairs = update_replacements(hunk)
    if len(pairs) != 1:
        raise ValueError("multi_hunk")
    old_text, new_text, _anchor = pairs[0]
    return old_text, new_text


def update_replacements(hunk: PatchHunk) -> list[tuple[str, str, str]]:
    """同一 Update File 里每个 @@ 各自还原成 old/new/锚点。"""
    if hunk.action not in {"update", "move"}:
        raise ValueError("not_update")
    pairs: list[tuple[str, str, str]] = []
    for section in _split_update_sections(hunk.lines):
        old_text, new_text = _section_old_new(section)
        if not old_text and not new_text:
            raise ValueError("empty_hunk")
        if old_text == new_text:
            raise ValueError("no_change")
        pairs.append((old_text, new_text, _section_anchor(section)))
    if not pairs:
        raise ValueError("empty_hunk")
    return pairs
