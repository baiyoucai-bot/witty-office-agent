"""grep / find / ls 检索工具。"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from witty_agent.tools.fs import _safe_path, _workspace
from witty_agent.tools.registry import tool

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox"}


def _is_default_path(path: str) -> bool:
    return path in {"", ".", "./"}


@tool
def ls(path: str = ".", limit: int = 500) -> str:
    """列出工作区目录内容。

    Args:
        path: 目录，默认当前工作区
        limit: 最多返回多少条，默认 500
    """
    workspace = _workspace()
    directory = _safe_path(workspace, path)
    if not directory.is_dir():
        # 走 prompts 而不是 f-string：证伪账本按**报错文案的 key** 认「路径不在」这一类
        # 失败（见 `negative_ledger._missing_patterns`），文案留在代码里它就认不出来。
        from witty_agent.prompts import get_prompt
        from witty_agent.sandbox import display_path

        raise ValueError(get_prompt("ls_not_dir", path=display_path(directory, workspace)))
    names = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    lines = []
    from witty_agent.runtime import sandbox_settings
    from witty_agent.sandbox import workspace_owns_sandbox_name

    if (
        _is_default_path(path)
        and sandbox_settings()["enabled"]
        and not workspace_owns_sandbox_name(workspace)
    ):
        have = {item.name for item in names}
        if "sandbox" not in have:
            lines.append("sandbox/")
        if "sandbox-tmp" not in have:
            lines.append("sandbox-tmp/")
    for item in names:
        mark = "/" if item.is_dir() else ""
        lines.append(f"{item.name}{mark}")
    cap = max(int(limit), 1)
    shown = lines[:cap]
    body = "\n".join(shown)
    extra = len(lines) - len(shown)
    if extra <= 0:
        return body
    from witty_agent.prompts import get_prompt

    return body + "\n" + get_prompt(
        "search_footer_capped",
        shown=str(len(shown)),
        total=str(len(lines)),
    )


@tool
def find(pattern: str, path: str = ".", limit: int = 1000) -> str:
    """按 glob 查找文件，跳过 .git/.venv。

    Args:
        pattern: glob，例如 **/*.py
        path: 搜索根目录
        limit: 最多结果数
    """
    workspace = _workspace()
    matches: list[str] = []
    from witty_agent.sandbox import display_path

    extra = 0
    for root in _search_roots(workspace, path):
        for item in root.rglob("*"):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            rel = display_path(item, workspace)
            name = item.name
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                if len(matches) < limit:
                    matches.append(rel + ("/" if item.is_dir() else ""))
                else:
                    extra += 1
    return _with_semantic(_render_hits(matches, limit=limit, extra=extra), path, pattern)


@tool
def grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    ignore_case: bool = False,
    literal: bool = False,
    limit: int = 100,
) -> str:
    """搜索文件内容。有 rg 就用 rg，否则用 Python。

    Args:
        pattern: 正则或字面量
        path: 文件或目录
        glob: 文件过滤，例如 *.py
        ignore_case: 忽略大小写
        literal: 按字面量而不是正则
        limit: 最多命中数
    """
    workspace = _workspace()
    roots = _search_roots(workspace, path)
    rg = _try_rg(pattern, roots, glob, ignore_case, literal, limit, workspace)
    if rg is not None:
        return rg
    flags = re.IGNORECASE if ignore_case else 0
    expr = re.compile(re.escape(pattern) if literal else pattern, flags)
    hits: list[str] = []
    extra = 0
    from witty_agent.sandbox import display_path

    for root in roots:
        files = [root] if root.is_file() else root.rglob("*")
        for file_path in files:
            if not file_path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in file_path.parts):
                continue
            if glob and not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            rel = display_path(file_path, workspace)
            for index, line in enumerate(text.splitlines(), start=1):
                if not expr.search(line):
                    continue
                if len(hits) < limit:
                    hits.append(f"{rel}:{index}:{line}")
                else:
                    extra += 1
    return _with_semantic(_render_hits(hits, limit=limit, extra=extra), path, pattern)


def _search_roots(workspace: str, path: str) -> list[Path]:
    from witty_agent.runtime import sandbox_settings
    from witty_agent.sandbox import sandbox_tmp, sandbox_work, workspace_owns_sandbox_name

    if _is_default_path(path):
        roots = [Path(workspace).resolve()]
        if sandbox_settings()["enabled"] and not workspace_owns_sandbox_name(workspace):
            for folder in (
                sandbox_work(workspace=workspace),
                sandbox_tmp(workspace=workspace),
            ):
                if folder.is_dir():
                    roots.append(folder.resolve())
        return roots
    return [_safe_path(workspace, path)]


def _try_rg(
    pattern: str,
    roots: list[Path],
    glob: str,
    ignore_case: bool,
    literal: bool,
    limit: int,
    workspace: str,
) -> str | None:
    cmd = ["rg", "--no-heading", "--line-number", "--color", "never", "-m", str(limit)]
    if ignore_case:
        cmd.append("-i")
    if literal:
        cmd.append("-F")
    if glob:
        cmd.extend(["-g", glob])
    cmd.append(pattern)
    cmd.extend(str(item) for item in roots)
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if completed.returncode not in (0, 1):
        return None
    text = completed.stdout.strip()
    if not text:
        return "(no matches)"
    from witty_agent.sandbox import rewrite_visible_paths

    lines = rewrite_visible_paths(text, workspace).splitlines()
    extra = max(0, len(lines) - limit)
    if extra == 0 and len(lines) >= limit:
        extra = 1
        total_label = f"{limit}+"
    else:
        total_label = ""
    return _with_semantic(
        _render_hits(lines[:limit], limit=limit, extra=extra, total_label=total_label),
        str(roots[0]) if roots else ".",
        pattern,
    )


def _knowledge_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("WITTY_MEMORY_USER", "WITTY_MEMORY_WORKSPACE"):
        raw = os.environ.get(key)
        if raw:
            roots.append(Path(raw).resolve())
    workspace = os.environ.get("WITTY_WORKSPACE")
    if workspace:
        wiki = Path(workspace).resolve() / "wiki"
        if wiki.is_dir():
            roots.append(wiki)
    return roots


def _under_knowledge(path: str) -> Path | None:
    if _is_default_path(path):
        return None
    try:
        target = Path(path).resolve()
    except OSError:
        return None
    for root in _knowledge_roots():
        try:
            target.relative_to(root)
            return root
        except ValueError:
            if target == root:
                return root
    return None


def _semantic_query(pattern: str) -> str:
    tokens = [part for part in re.split(r"[^\w\u4e00-\u9fff]+", pattern or "") if len(part) >= 2]
    return " ".join(tokens[:8])


def _with_semantic(body: str, path: str, pattern: str) -> str:
    root = _under_knowledge(path)
    query = _semantic_query(pattern)
    if root is None or not query:
        return body
    from witty_agent.memory import retrieve_for_query
    from witty_agent.prompts import get_prompt

    extra = retrieve_for_query(root, query)
    if not extra:
        return body
    return body + "\n" + get_prompt("search_semantic_extra", body=extra)


def _render_hits(
    hits: list[str],
    *,
    limit: int,
    extra: int = 0,
    total_label: str = "",
) -> str:
    from witty_agent.prompts import get_prompt

    if not hits:
        return "(no matches)"
    shown = hits[: max(limit, 1)]
    body = "\n".join(shown)
    if extra <= 0:
        return body
    total = total_label or str(len(shown) + extra)
    return body + "\n" + get_prompt("search_footer_capped", shown=str(len(shown)), total=total)
