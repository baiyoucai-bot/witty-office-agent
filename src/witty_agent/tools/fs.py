"""内核四件套：read / write / edit / bash。写和命令视为危险工具。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.tools.registry import tool

logger = get_logger("tools.fs")
_MAX_LINES = 2000
_MAX_LINE = 2000
_MAX_BYTES = 256 * 1024
_TEXT_SAMPLE = 8192


def _safe_path(workspace: str, raw: str, *, follow: bool = True) -> Path:
    from witty_agent.sandbox import resolve_allowed

    return resolve_allowed(workspace, raw, follow=follow)


def bind_workspace(workspace: str, session_id: str = "") -> None:
    """把工作区写到环境变量，供本轮工具使用。"""
    os.environ["WITTY_WORKSPACE"] = workspace
    from witty_agent.fs_observe import bind_owner
    from witty_agent.runtime import sandbox_settings
    from witty_agent.sandbox import sandbox_tmp, sandbox_work, workspace_owns_sandbox_name

    bind_owner(workspace, session_id)
    if sandbox_settings()["enabled"] and not workspace_owns_sandbox_name(workspace):
        sandbox_work(workspace=workspace).mkdir(parents=True, exist_ok=True)
        sandbox_tmp(workspace=workspace).mkdir(parents=True, exist_ok=True)


def _workspace() -> str:
    value = os.environ.get("WITTY_WORKSPACE")
    if not value:
        raise RuntimeError("未设置工作区 WITTY_WORKSPACE")
    return value


@tool
def read(path: str, offset: int = 1, limit: int = 0) -> str:
    """读取工作区内的文本文件。大文件用 offset/limit 分页。

    Args:
        path: 相对或绝对路径（不得越出工作区）
        offset: 起始行，从 1 开始
        limit: 最多读多少行，0 表示用默认上限
    """
    workspace = _workspace()
    file_path = _safe_path(workspace, path)
    from witty_agent.fs_observe import observe_absent, observe_present
    from witty_agent.prompts import get_prompt
    from witty_agent.sandbox import display_path

    shown = display_path(file_path, workspace)
    if not file_path.exists():
        observe_absent(file_path)
        raise ValueError(get_prompt("read_not_found", path=shown))
    if file_path.is_dir():
        return _read_directory(file_path, shown, offset=offset, limit=limit)
    if not file_path.is_file():
        raise ValueError(get_prompt("read_not_file", path=shown))
    _reject_not_text(file_path, shown)
    try:
        with file_path.open("r", encoding="utf-8", errors="strict") as handle:
            text = _format_read(handle, offset=offset, limit=limit, shown=shown)
    except UnicodeDecodeError:
        raise ValueError(get_prompt("read_not_text", path=shown)) from None
    observe_present(file_path)
    return text


def _read_directory(directory: Path, shown: str, *, offset: int, limit: int) -> str:
    """read 目录只列一层名字，不当文件观察。"""
    from witty_agent.prompts import get_prompt

    names = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    lines = [f"{item.name}{'/' if item.is_dir() else ''}" for item in names]
    start = max(int(offset), 1)
    cap = max(int(limit), 0) or 500
    window = lines[start - 1 : start - 1 + cap]
    body = "\n".join(window) if window else get_prompt("read_dir_empty")
    footer = get_prompt("read_dir_footer", path=shown, count=str(len(lines)))
    if not window and lines:
        return footer
    return f"{body}\n{footer}"


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


def _reject_not_text(path: Path, shown: str) -> None:
    from witty_agent.prompts import get_prompt

    with path.open("rb") as handle:
        sample = handle.read(_TEXT_SAMPLE)
    if b"\x00" in sample or not _utf8_sample_ok(sample):
        raise ValueError(get_prompt("read_not_text", path=shown))


def _format_read(
    source: object,
    *,
    offset: int,
    limit: int,
    shown: str,
) -> str:
    from witty_agent.prompts import get_prompt

    if isinstance(source, str):
        lines = source.splitlines()
    else:
        lines = (_strip_nl(line) for line in source)
    start = max(offset, 1)
    cap = min(limit, _MAX_LINES) if limit > 0 else _MAX_LINES
    window: list[tuple[int, str]] = []
    total = 0
    output_bytes = 0
    hit_bytes = False
    for raw in lines:
        total += 1
        if hit_bytes or total < start or len(window) >= cap:
            continue
        text = raw
        if len(text) > _MAX_LINE:
            text = text[:_MAX_LINE] + get_prompt("read_line_truncated", max=str(_MAX_LINE))
        size = len(text.encode("utf-8")) + (1 if window else 0)
        if output_bytes + size > _MAX_BYTES:
            hit_bytes = True
            continue
        output_bytes += size
        window.append((total, text))
    if start > total and not (total == 0 and start == 1):
        raise ValueError(get_prompt("read_offset_oob", offset=str(start), path=shown, total=str(total)))
    numbered = [f"{number}|{text}" for number, text in window]
    end = window[-1][0] if window else max(start - 1, 0)
    nxt = end + 1
    if hit_bytes:
        footer = get_prompt("read_footer_capped", start=str(start), end=str(max(end, start)), next=str(nxt))
    elif window and window[-1][0] < total:
        footer = get_prompt(
            "read_footer_window",
            start=str(start),
            end=str(end),
            total=str(total),
            next=str(nxt),
        )
    else:
        footer = get_prompt("read_footer_eof", total=str(total))
    if not numbered:
        return footer
    return "\n".join(numbered) + "\n" + footer


def _strip_nl(line: str) -> str:
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _with_trailing_newline(content: str) -> str:
    if not content or content.endswith("\n"):
        return content
    if "\r\n" in content:
        return content + "\r\n"
    return content + "\n"


@tool
def write(path: str, content: str) -> str:
    """写入或覆盖工作区内的文件。危险操作，必须先批准。

    Args:
        path: 相对或绝对路径
        content: 完整文件内容
    """
    return _commit_write(path, content, pad_newline=True)


def _commit_write(path: str, content: str, *, pad_newline: bool, mode: int | None = None) -> str:
    workspace = _workspace()
    file_path = _safe_path(workspace, path, follow=False)
    from witty_agent.fs_observe import authorize_write, observe_present
    from witty_agent.sandbox import display_path

    from witty_agent.prompts import get_prompt

    shown = display_path(file_path, workspace)
    if file_path.is_symlink():
        existed = False
        before = ""
    else:
        authorize_write(file_path, shown)
        existed = file_path.is_file()
        before = file_path.read_text(encoding="utf-8") if existed else ""
    from witty_agent.atomic_write import write_file_atomic

    if pad_newline:
        content = _with_trailing_newline(content)
    write_file_atomic(file_path, content, mode=mode)
    observe_present(file_path)
    plus, minus = _line_delta(before, content)
    logger.info(
        "写入文件 path=%s bytes=%s op=%s plus=%s minus=%s",
        shown,
        len(content.encode()),
        "update" if existed else "create",
        plus,
        minus,
    )
    key = "write_ok_update" if existed else "write_ok_create"
    header = get_prompt(key, path=shown, plus=str(plus), minus=str(minus))
    return _fs_receipt(header, before, content)


def _line_delta(before: str, after: str) -> tuple[int, int]:
    import difflib

    old_lines = before.splitlines()
    new_lines = after.splitlines()
    plus = minus = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            plus += j2 - j1
        elif tag == "delete":
            minus += i2 - i1
        elif tag == "replace":
            minus += i2 - i1
            plus += j2 - j1
    return plus, minus


_CONTEXT = 3
_CARD_MAX = 8


def _context_card(before: str, after: str) -> str:
    import difflib

    from witty_agent.prompts import get_prompt

    old_lines = before.splitlines()
    new_lines = after.splitlines()
    if not new_lines:
        return ""
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    start = 0
    end = min(len(new_lines), _CONTEXT)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        start = max(0, j1 - _CONTEXT)
        end = min(len(new_lines), max(j2, j1) + _CONTEXT)
        break
    if end - start > _CARD_MAX:
        end = start + _CARD_MAX
    rows = [
        get_prompt("fs_context_line", num=str(idx), text=line)
        for idx, line in enumerate(new_lines[start:end], start=start + 1)
    ]
    return get_prompt("fs_context_card", body="\n".join(rows)) if rows else ""


def _fs_receipt(header: str, before: str, after: str) -> str:
    card = _context_card(before, after)
    return f"{header}\n{card}" if card else header


@tool
def apply_patch(patch: str) -> str:
    """按补丁文本新建、改、删或改名。一条补丁可含多个文件，按出现顺序应用。

    Args:
        patch: 一段或多段 Add / Update / Delete / Move to；Begin/End 可省略
    """
    from witty_agent.patch_text import parse_apply_patch
    from witty_agent.prompts import get_prompt

    try:
        hunks = parse_apply_patch(patch)
    except ValueError as exc:
        if str(exc) == "mode":
            raise ValueError(get_prompt("apply_patch_bad_mode")) from None
        raise ValueError(get_prompt("apply_patch_bad_frame")) from None
    unknown = sorted({item.action for item in hunks if item.action not in {"add", "update", "delete", "move"}})
    if unknown:
        raise ValueError(get_prompt("apply_patch_unsupported", actions=",".join(unknown)))
    undo: list[_PatchSnap] = []
    receipts: list[str] = []
    try:
        for hunk in hunks:
            snap = _snapshot_hunk(hunk)
            receipts.append(_apply_one_hunk(hunk))
            undo.append(snap)
    except ValueError as exc:
        if undo:
            _rollback_patch(undo)
            raise ValueError(
                get_prompt("apply_patch_rolled_back", reason=str(exc))
            ) from None
        raise
    return "\n".join(receipts)


@dataclass
class _PatchSnap:
    action: str
    path: Path
    dest: Path | None
    content: str | None
    dest_existed: bool
    created_parents: tuple[Path, ...]
    mode: int | None = None


def _missing_parents(path: Path, workspace: Path) -> tuple[Path, ...]:
    """path 的尚不存在父目录，从最深一层到工作区之下。"""
    try:
        root = workspace.resolve()
    except OSError:
        root = workspace
    missing: list[Path] = []
    current = path.parent
    while True:
        try:
            current.resolve().relative_to(root)
        except (ValueError, OSError):
            break
        if current == root or current.resolve() == root:
            break
        if current.exists():
            break
        missing.append(current)
        if not current.parent.parts or current.parent == current:
            break
        current = current.parent
    return tuple(missing)


def _rmdir_created(parents: tuple[Path, ...]) -> None:
    for directory in parents:
        try:
            if directory.is_dir() and next(directory.iterdir(), None) is None:
                directory.rmdir()
        except OSError:
            break


def _snapshot_hunk(hunk) -> _PatchSnap:
    workspace = Path(_workspace())
    path = _safe_path(_workspace(), hunk.path, follow=False)
    dest = (
        _safe_path(_workspace(), hunk.dest, follow=False)
        if str(getattr(hunk, "dest", "") or "").strip()
        else None
    )
    text = None
    mode = None
    if path.is_file() and not path.is_symlink():
        text = path.read_text(encoding="utf-8")
        mode = _stat_mode(path)
    created = _missing_parents(dest or path, workspace) if hunk.action in {"add", "move"} else ()
    return _PatchSnap(
        action=hunk.action,
        path=path,
        dest=dest,
        content=text,
        dest_existed=bool(dest and dest.exists()),
        created_parents=created,
        mode=mode,
    )


def _rollback_patch(undo: list[_PatchSnap]) -> None:
    from witty_agent.atomic_write import write_file_atomic
    from witty_agent.fs_observe import observe_absent, observe_present

    for snap in reversed(undo):
        if snap.action == "add" and snap.path.exists():
            snap.path.unlink()
            observe_absent(snap.path)
            _rmdir_created(snap.created_parents)
            continue
        if snap.action == "move" and snap.dest is not None and snap.dest.exists() and not snap.dest_existed:
            snap.dest.unlink()
            observe_absent(snap.dest)
            _rmdir_created(snap.created_parents)
        if snap.content is not None:
            write_file_atomic(snap.path, snap.content, mode=snap.mode)
            observe_present(snap.path)
        elif snap.mode is not None and snap.path.is_file():
            snap.path.chmod(snap.mode)


def _apply_one_hunk(hunk) -> str:
    from witty_agent.prompts import get_prompt
    from witty_agent.sandbox import display_path

    file_path = _safe_path(_workspace(), hunk.path, follow=False)
    shown = display_path(file_path, _workspace())
    if hunk.action == "add":
        if file_path.exists():
            raise ValueError(get_prompt("apply_patch_exists", path=shown))
        return _commit_write(
            hunk.path,
            hunk.content(),
            pad_newline=not hunk.eof,
            mode=hunk.mode,
        )
    if hunk.action == "delete":
        return _apply_patch_delete(file_path, shown)
    if hunk.action == "move":
        receipt = _apply_patch_move(hunk, file_path, shown)
        dest = _safe_path(_workspace(), hunk.dest, follow=False)
        return _with_mode(receipt, dest, hunk.mode)
    if hunk.mode is not None and not any(line for line in hunk.lines if line and not line.startswith("@@")):
        return _apply_patch_mode(file_path, shown, hunk.mode)
    receipt = _apply_patch_update(hunk, shown)
    return _with_mode(receipt, file_path, hunk.mode)


def _stat_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _with_mode(receipt: str, path: Path, mode: int | None) -> str:
    if mode is None:
        return receipt
    extra = _apply_patch_mode(path, path.name, mode)
    return f"{receipt}\n{extra}" if receipt else extra


def _apply_patch_mode(path: Path, shown: str, mode: int) -> str:
    from witty_agent.fs_observe import authorize_edit
    from witty_agent.prompts import get_prompt
    from witty_agent.sandbox import display_path

    label = display_path(path, _workspace())
    authorize_edit(path, label)
    if not path.is_file() or path.is_symlink():
        raise ValueError(get_prompt("apply_patch_mode_missing", path=label))
    path.chmod(mode)
    return get_prompt("apply_patch_mode_ok", path=label, mode=f"{mode:o}")


def _apply_patch_update(hunk, shown: str) -> str:
    from witty_agent.patch_text import update_replacements
    from witty_agent.prompts import get_prompt

    try:
        pairs = update_replacements(hunk)
    except ValueError:
        raise ValueError(get_prompt("apply_patch_empty_hunk", path=shown)) from None
    receipts: list[str] = []
    last = len(pairs) - 1
    for index, (old_text, new_text, anchor) in enumerate(pairs):
        if not old_text:
            if hunk.eof and index == last:
                receipts.append(_append_at_end(hunk.path, new_text, shown))
            elif anchor:
                receipts.append(_insert_after_anchor(hunk.path, anchor, new_text, shown))
            else:
                raise ValueError(get_prompt("apply_patch_need_context", path=shown))
            continue
        if hunk.eof and index == last:
            receipts.append(_edit_at_eof(hunk.path, old_text, new_text, shown))
        else:
            receipts.append(edit(hunk.path, old_text=old_text, new_text=new_text))
    return "\n".join(receipts)


def _strip_one_nl(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _append_at_end(rel: str, new_text: str, shown: str) -> str:
    """Update 只有 + 且 *** End of File 时追加到末尾。"""
    from witty_agent.fs_observe import authorize_edit
    from witty_agent.prompts import get_prompt

    file_path = _safe_path(_workspace(), rel, follow=False)
    authorize_edit(file_path, shown)
    if not file_path.is_file() or file_path.is_symlink():
        raise ValueError(get_prompt("apply_patch_not_eof", path=shown))
    raw = file_path.read_text(encoding="utf-8")
    extra = new_text
    if extra and not extra.endswith("\n"):
        extra += "\n"
    if raw and not raw.endswith("\n"):
        raw += "\n"
    return write(rel, raw + extra)


def _insert_after_anchor(rel: str, anchor: str, new_text: str, shown: str) -> str:
    """@@ 定位词唯一命中时，在该行后插入只有 + 的段。"""
    from witty_agent.fs_observe import authorize_edit
    from witty_agent.prompts import get_prompt

    file_path = _safe_path(_workspace(), rel, follow=False)
    authorize_edit(file_path, shown)
    if not file_path.is_file() or file_path.is_symlink():
        raise ValueError(get_prompt("apply_patch_delete_missing", path=shown))
    raw = file_path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if anchor in line]
    if not hits:
        raise ValueError(get_prompt("apply_patch_anchor_missing", path=shown, anchor=anchor))
    if len(hits) > 1:
        raise ValueError(
            get_prompt("apply_patch_anchor_ambiguous", path=shown, anchor=anchor, count=str(len(hits)))
        )
    at = hits[0]
    extra = new_text
    if extra and not extra.endswith("\n"):
        extra += "\n"
    if lines and not lines[at].endswith("\n"):
        lines[at] += "\n"
    lines.insert(at + 1, extra)
    return write(rel, "".join(lines))


def _edit_at_eof(rel: str, old_text: str, new_text: str, shown: str) -> str:
    """*** End of File 要求最后一段贴在文件末尾。"""
    from witty_agent.prompts import get_prompt

    file_path = _safe_path(_workspace(), rel, follow=False)
    from witty_agent.fs_observe import authorize_edit

    authorize_edit(file_path, shown)
    if not file_path.is_file():
        raise ValueError(get_prompt("apply_patch_not_eof", path=shown))
    raw = file_path.read_text(encoding="utf-8")
    body = _strip_one_nl(raw)
    old = _strip_one_nl(old_text)
    if not old or not body.endswith(old):
        raise ValueError(get_prompt("apply_patch_not_eof", path=shown))
    if body.count(old) == 1:
        return edit(rel, old_text=old_text, new_text=new_text)
    new = _strip_one_nl(new_text)
    updated = body[: -len(old)] + new
    if raw.endswith("\n") and not updated.endswith("\n"):
        updated += "\n" if not raw.endswith("\r\n") else "\r\n"
    return write(rel, updated)


def _apply_patch_delete(file_path, shown: str) -> str:
    from witty_agent.fs_observe import authorize_edit, observe_absent
    from witty_agent.prompts import get_prompt

    if file_path.is_symlink():
        raise ValueError(get_prompt("edit_is_symlink", path=shown))
    authorize_edit(file_path, shown)
    if not file_path.is_file():
        raise ValueError(get_prompt("apply_patch_delete_missing", path=shown))
    file_path.unlink()
    observe_absent(file_path)
    return get_prompt("apply_patch_deleted", path=shown)


def _apply_patch_move(hunk, file_path: Path, shown: str) -> str:
    from witty_agent.fs_observe import authorize_edit, observe_absent
    from witty_agent.prompts import get_prompt
    from witty_agent.sandbox import display_path

    dest_raw = str(hunk.dest or "").strip()
    if not dest_raw:
        raise ValueError(get_prompt("apply_patch_move_missing", path=shown))
    dest_path = _safe_path(_workspace(), dest_raw, follow=False)
    dest_shown = display_path(dest_path, _workspace())
    if dest_path.exists():
        raise ValueError(get_prompt("apply_patch_exists", path=dest_shown))
    if dest_path.resolve() == file_path.resolve():
        raise ValueError(get_prompt("apply_patch_move_same", path=shown))
    if file_path.is_symlink():
        raise ValueError(get_prompt("edit_is_symlink", path=shown))
    authorize_edit(file_path, shown)
    if not file_path.is_file():
        raise ValueError(get_prompt("apply_patch_delete_missing", path=shown))
    receipts: list[str] = []
    if any(line[:1] in "+-" for line in hunk.lines):
        receipts.append(_apply_patch_update(hunk, shown))
    write(dest_raw, file_path.read_text(encoding="utf-8"))
    file_path.unlink()
    observe_absent(file_path)
    receipts.append(get_prompt("apply_patch_moved", path=shown, dest=dest_shown))
    return "\n".join(receipts)


@tool
def edit(
    path: str,
    old_text: str = "",
    new_text: str = "",
    edits_json: str = "",
    replace_all: bool = False,
) -> str:
    """对原文件做定点替换。默认 old_text 必须恰好一次；重复处加上下文或 replace_all。

    Args:
        path: 文件路径
        old_text: 单次替换的原文
        new_text: 单次替换的新文
        edits_json: 多次替换 JSON，形如 [{"oldText":"...","newText":"...","replaceAll":false}]
        replace_all: 为真则替换全部出现处
    """
    from witty_agent.fs_observe import authorize_edit, observe_present
    from witty_agent.prompts import get_prompt
    from witty_agent.sandbox import display_path

    workspace = _workspace()
    file_path = _safe_path(workspace, path, follow=False)
    shown = display_path(file_path, workspace)
    if file_path.is_symlink():
        raise ValueError(get_prompt("edit_is_symlink", path=shown))
    authorize_edit(file_path, shown)
    original = file_path.read_text(encoding="utf-8")
    edits = _parse_edits(old_text, new_text, edits_json, replace_all)
    updated = original
    replaced = 0
    for item in edits:
        needle = item["oldText"]
        fresh = item["newText"]
        if not needle:
            raise ValueError(get_prompt("edit_empty_old", path=shown))
        if needle == fresh:
            raise ValueError(get_prompt("edit_no_change", path=shown))
        found = updated.count(needle)
        if found == 0:
            spans = _line_trimmed_spans(updated, needle)
            if not spans:
                spans = _block_anchor_spans(updated, needle)
            if not spans:
                spans = _whitespace_normalized_spans(updated, needle)
            if not spans:
                spans = _escape_normalized_spans(updated, needle)
            if not spans:
                spans = _trimmed_boundary_spans(updated, needle)
            if not spans:
                spans = _context_aware_spans(updated, needle)
            if not spans:
                raise ValueError(get_prompt("edit_not_found", path=shown))
            if any(_is_disproportionate_match(updated[start:end], needle) for start, end in spans):
                raise ValueError(get_prompt("edit_span_too_wide", path=shown))
            if len(spans) > 1 and not item["replaceAll"]:
                raise ValueError(
                    get_prompt("edit_not_unique", path=shown, count=str(len(spans)))
                )
            updated = _replace_spans(updated, spans, fresh)
            replaced += len(spans)
            continue
        if found > 1 and not item["replaceAll"]:
            raise ValueError(get_prompt("edit_not_unique", path=shown, count=str(found)))
        if item["replaceAll"]:
            updated = updated.replace(needle, fresh)
            replaced += found
        else:
            updated = updated.replace(needle, fresh, 1)
            replaced += 1
    updated = _with_trailing_newline(updated)
    from witty_agent.atomic_write import write_file_atomic

    write_file_atomic(file_path, updated)
    observe_present(file_path)
    plus, minus = _line_delta(original, updated)
    logger.info(
        "编辑文件 path=%s count=%s plus=%s minus=%s",
        shown,
        replaced,
        plus,
        minus,
    )
    header = get_prompt(
        "edit_ok",
        path=shown,
        count=str(replaced),
        plus=str(plus),
        minus=str(minus),
    )
    return _fs_receipt(header, original, updated)


def _line_trimmed_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """行修剪回退：每行去首尾空白后唯一对齐。"""
    want = [line.strip() for line in needle.splitlines()]
    if not want or all(not line for line in want):
        return []
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n").strip() for line in lines]
    width = len(want)
    hits: list[tuple[int, int]] = []
    for index in range(len(stripped) - width + 1):
        if stripped[index : index + width] != want:
            continue
        start = sum(len(lines[item]) for item in range(index))
        end = start + sum(len(lines[item]) for item in range(index, index + width))
        hits.append((start, end))
    return hits


_BLOCK_ANCHOR_MIN_LINES = 3
_BLOCK_ANCHOR_SIMILARITY = 0.65


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left or not right:
        return max(len(left), len(right))
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, start=1):
        current = [index]
        for column, other in enumerate(right, start=1):
            insert = current[column - 1] + 1
            delete = previous[column] + 1
            swap = previous[column - 1] + (char != other)
            current.append(min(insert, delete, swap))
        previous = current
    return previous[-1]


def _block_anchor_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """块锚定回退：首尾行锚定，中间按编辑距离打分。"""
    want = list(needle.splitlines())
    if len(want) < _BLOCK_ANCHOR_MIN_LINES:
        return []
    first = want[0].strip()
    last = want[-1].strip()
    if not first or not last:
        return []
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n").strip() for line in lines]
    width = len(want)
    slack = max(1, width // 4)
    candidates: list[tuple[int, int]] = []
    for start, line in enumerate(stripped):
        if line != first:
            continue
        for end in range(start + 2, len(stripped)):
            if stripped[end] != last:
                continue
            if abs((end - start + 1) - width) <= slack:
                candidates.append((start, end))
            break
    scored: list[tuple[float, int, int]] = []
    for start, end in candidates:
        score = _block_anchor_score(stripped, want, start, end)
        if score >= _BLOCK_ANCHOR_SIMILARITY:
            scored.append((score, start, end))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    start, end = scored[0][1], scored[0][2]
    begin = sum(len(lines[item]) for item in range(start))
    stop = begin + sum(len(lines[item]) for item in range(start, end + 1))
    return [(begin, stop)]


def _block_anchor_score(
    stripped: list[str],
    want: list[str],
    start: int,
    end: int,
) -> float:
    actual = end - start + 1
    to_check = min(len(want) - 2, actual - 2)
    if to_check <= 0:
        return 1.0
    total = 0.0
    limit = min(len(want) - 1, actual - 1)
    for index in range(1, limit):
        original = stripped[start + index]
        search = want[index].strip()
        longest = max(len(original), len(search))
        if longest == 0:
            continue
        total += 1 - _levenshtein(original, search) / longest
    return total / to_check


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _ws_subspan(line: str, needle: str) -> tuple[int, int] | None:
    import re

    words = needle.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    match = re.search(pattern, line)
    if match is None:
        return None
    return match.start(), match.end()


def _whitespace_normalized_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """空白归一回退：空白折叠后再对齐。"""
    want = _collapse_ws(needle)
    if not want:
        return []
    lines = text.splitlines(keepends=True)
    hits: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add(start: int, end: int) -> None:
        span = (start, end)
        if start < end and span not in seen:
            seen.add(span)
            hits.append(span)

    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        start = sum(len(lines[item]) for item in range(index))
        collapsed = _collapse_ws(body)
        if collapsed == want:
            add(start, start + len(line))
            continue
        if want not in collapsed:
            continue
        inner = _ws_subspan(body, needle)
        if inner is not None:
            add(start + inner[0], start + inner[1])
    width = len(needle.splitlines())
    if width > 1:
        for index in range(len(lines) - width + 1):
            block = "".join(lines[index : index + width])
            if _collapse_ws(block) != want:
                continue
            start = sum(len(lines[item]) for item in range(index))
            add(start, start + len(block))
    return hits


_ESCAPE_MAP = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "'": "'",
    '"': '"',
    "`": "`",
    "\\": "\\",
    "\n": "\n",
    "$": "$",
}


def _unescape_old(text: str) -> str:
    import re

    return re.sub(r"\\(n|t|r|'|\"|`|\\|\n|\$)", lambda match: _ESCAPE_MAP.get(match.group(1), match.group(0)), text)


def _escape_normalized_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """转义归一回退：把字面 \\n/\\t 还原后再找。"""
    decoded = _unescape_old(needle)
    hits: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add(start: int, end: int) -> None:
        span = (start, end)
        if start < end and span not in seen:
            seen.add(span)
            hits.append(span)

    if decoded != needle:
        start = 0
        while True:
            index = text.find(decoded, start)
            if index < 0:
                break
            add(index, index + len(decoded))
            start = index + len(decoded)
    lines = text.splitlines(keepends=True)
    width = len(decoded.splitlines()) or 1
    for index in range(len(lines) - width + 1):
        block = "".join(lines[index : index + width])
        if _unescape_old(block) != decoded and _unescape_old(block.rstrip("\r\n")) != decoded.rstrip("\r\n"):
            continue
        start = sum(len(lines[item]) for item in range(index))
        add(start, start + len(block))
    return hits


def _trimmed_boundary_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """边界修剪回退：整块去首尾空白后再找。"""
    trimmed = needle.strip()
    if not trimmed or trimmed == needle:
        return []
    hits: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add(start: int, end: int) -> None:
        span = (start, end)
        if start < end and span not in seen:
            seen.add(span)
            hits.append(span)

    start = 0
    while True:
        index = text.find(trimmed, start)
        if index < 0:
            break
        add(index, index + len(trimmed))
        start = index + len(trimmed)
    lines = text.splitlines(keepends=True)
    width = len(needle.splitlines()) or 1
    for index in range(len(lines) - width + 1):
        block = "".join(lines[index : index + width])
        if block.strip() != trimmed:
            continue
        begin = sum(len(lines[item]) for item in range(index))
        add(begin, begin + len(block))
    return hits


_CONTEXT_AWARE_MIN_LINES = 3
_CONTEXT_AWARE_RATIO = 0.5


def _context_aware_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """上下文感知回退：首尾对齐且中间非空行半数相同。"""
    want = list(needle.splitlines())
    if len(want) < _CONTEXT_AWARE_MIN_LINES:
        return []
    first = want[0].strip()
    last = want[-1].strip()
    if not first or not last:
        return []
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n").strip() for line in lines]
    width = len(want)
    for start, line in enumerate(stripped):
        if line != first:
            continue
        for end in range(start + 2, len(stripped)):
            if stripped[end] != last:
                continue
            if end - start + 1 != width:
                break
            matched = 0
            nonempty = 0
            for index in range(1, width - 1):
                block_line = stripped[start + index]
                find_line = want[index].strip()
                if not block_line and not find_line:
                    continue
                nonempty += 1
                if block_line == find_line:
                    matched += 1
            if nonempty == 0 or matched / nonempty >= _CONTEXT_AWARE_RATIO:
                begin = sum(len(lines[item]) for item in range(start))
                stop = begin + sum(len(lines[item]) for item in range(start, end + 1))
                return [(begin, stop)]
            break
    return []


def _is_disproportionate_match(matched: str, needle: str) -> bool:
    """比例检查：模糊命中远大于 old_text 则拒绝。"""
    old_lines = len(needle.split("\n"))
    search_lines = len(matched.split("\n"))
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    trimmed_old = len(needle.strip())
    return len(matched.strip()) > max(trimmed_old + 500, trimmed_old * 4)


def _replace_spans(text: str, spans: list[tuple[int, int]], fresh: str) -> str:
    updated = text
    for start, end in reversed(spans):
        updated = updated[:start] + fresh + updated[end:]
    return updated


def _parse_edits(
    old_text: str,
    new_text: str,
    edits_json: str,
    replace_all: bool,
) -> list[dict[str, object]]:
    import json

    edits: list[dict[str, object]] = []
    if edits_json.strip():
        parsed = json.loads(edits_json)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("edits_json 必须是非空数组")
        for item in parsed:
            if not isinstance(item, dict) or "oldText" not in item or "newText" not in item:
                raise ValueError("每个 edit 需要 oldText/newText")
            flag = item.get("replaceAll", item.get("replace_all", replace_all))
            edits.append(
                {
                    "oldText": str(item["oldText"]),
                    "newText": str(item["newText"]),
                    "replaceAll": bool(flag),
                }
            )
    if old_text:
        edits.append({"oldText": old_text, "newText": new_text, "replaceAll": bool(replace_all)})
    if not edits:
        raise ValueError("必须提供 old_text 或 edits_json")
    return edits


@tool
def bash(command: str, timeout: int = 30) -> str:
    """在工作区执行 shell 命令。危险操作，必须先批准。

    Args:
        command: shell 命令
        timeout: 超时秒数
    """
    if timeout <= 0 or timeout > 3600:
        raise ValueError("timeout 必须在 1..3600 秒")
    from witty_agent.vault import bound_vault

    from witty_agent.sandbox import apply_exec_env, bash_argv, check_command_paths, rewrite_sandbox_tokens

    workspace = _workspace()
    check_command_paths(workspace, command)
    env = apply_exec_env(os.environ.copy())
    env.update(bound_vault())
    launched = rewrite_sandbox_tokens(command)
    try:
        completed = subprocess.run(
            bash_argv(launched),
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        from witty_agent.guard import timeout_result_text

        raise TimeoutError(timeout_result_text(timeout * 1000)) from None
    from witty_agent.fs_observe import changed_notice

    return _format_bash_result(
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    ) + changed_notice(workspace)


def _format_bash_result(returncode: int, stdout: str, stderr: str) -> str:
    body = _clip_tool_output(_join_streams(stdout, stderr))
    marker = _signal_marker(returncode)
    if marker:
        body = f"{body}\n{marker}"
    return f"exit={returncode}\n{body}"


def _signal_marker(returncode: int) -> str:
    if returncode >= 0:
        return ""
    import signal

    from witty_agent.prompts import get_prompt

    number = -int(returncode)
    try:
        name = signal.Signals(number).name
    except ValueError:
        name = str(number)
    return get_prompt("bash_killed_by_signal", signal=name)


def _join_streams(stdout: str, stderr: str) -> str:
    from witty_agent.prompts import get_prompt

    if not stderr:
        return stdout
    header = get_prompt("bash_stderr_header")
    if not stdout:
        return f"{header}\n{stderr}"
    sep = "" if stdout.endswith("\n") else "\n"
    return f"{stdout}{sep}{header}\n{stderr}"


def _clip_tool_output(output: str) -> str:
    from witty_agent.prompts import get_prompt

    if not output:
        return get_prompt("bash_no_output")
    total = len(output)
    if total <= _MAX_BYTES:
        return output
    archived = _archive_truncated(output)
    clipped = output[-_MAX_BYTES:]
    path = archived or get_prompt("bash_footer_unavailable")
    return clipped + "\n" + get_prompt(
        "bash_footer_capped",
        shown=str(len(clipped)),
        total=str(total),
        path=path,
    )


def _archive_truncated(output: str) -> str:
    scratch = os.environ.get("WITTY_SCRATCHPAD")
    if not scratch:
        return ""
    directory = Path(scratch)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "truncated-tool-output.txt"
    path.write_text(output, encoding="utf-8")
    return str(path)
