"""记忆工具：写/读 user 或 workspace 作用域。"""

from __future__ import annotations

import os

from witty_agent.memory import read_topic, write_topic
from witty_agent.tools.registry import tool


def _scope_dir(scope: str):
    from pathlib import Path

    if scope == "workspace":
        raw = os.environ.get("WITTY_MEMORY_WORKSPACE")
    else:
        raw = os.environ.get("WITTY_MEMORY_USER")
    if not raw:
        raise RuntimeError(f"记忆目录未绑定 scope={scope}")
    return Path(raw)


@tool
def memory_write(slug: str, description: str, body: str, scope: str = "user") -> str:
    """写入或改写一条长期记忆。body 留空则清空该格，用于删错记、改记。

    Args:
        slug: 小写短名，须与文件名一致
        description: 一行摘要，供回忆时判断要不要打开正文
        body: 记忆正文；空字符串表示清空这一格
        scope: user 或 workspace
    """
    if scope not in {"user", "workspace"}:
        raise ValueError("scope 只能是 user 或 workspace")
    if slug == "focus":
        from witty_agent.focus_board import archive_focus, save_focus_text

        try:
            target = _scope_dir("workspace")
        except RuntimeError:
            target = _scope_dir(scope)
        if not (body or "").strip():
            archived = archive_focus(target)
            return f"archived focus -> {archived}" if archived else "focus already empty"
        path = save_focus_text(target, body)
        return f"saved memory workspace/focus -> {path}"
    path = write_topic(_scope_dir(scope), slug, description=description, body=body)
    if scope == "user":
        from witty_agent.memory import rebuild_memory_index

        rebuild_memory_index(_scope_dir(scope))
    return f"saved memory {scope}/{slug} -> {path}"


@tool
def memory_status(scope: str = "user") -> str:
    """查看九宫格记忆、用户画像和已分类条目。

    Args:
        scope: user 或 workspace
    """
    from witty_agent.memory import public_memory

    if scope not in {"user", "workspace"}:
        raise ValueError("scope 只能是 user 或 workspace")
    payload = public_memory(_scope_dir(scope))
    archive = payload.get("archive") or []
    archive_line = (
        ", ".join(
            f"{item.get('id')} ({item.get('count')})"
            for item in archive
            if isinstance(item, dict) and item.get("id")
        )
        or "（无）"
    )
    return (
        f"{payload.get('lattice') or ''}\n\n"
        f"{payload.get('profile') or ''}\n\n"
        f"## Timeline\n{payload.get('timeline') or '（空）'}\n\n"
        f"## Links\n{_format_links(payload.get('links') or [])}\n\n"
        f"## Archive\n{archive_line}\n\n"
        f"turns={payload.get('turns') or 0}"
    ).strip()


def _format_links(rows: list) -> str:
    if not rows:
        return "（无）"
    return "\n".join(
        f"- {item.get('from_title') or item.get('from')} ↔ {item.get('to_title') or item.get('to')}"
        for item in rows[:20]
    )


@tool
def memory_read(slug: str, scope: str = "user") -> str:
    """读取一条长期记忆正文。

    Args:
        slug: 记忆短名
        scope: user 或 workspace
    """
    if scope not in {"user", "workspace"}:
        raise ValueError("scope 只能是 user 或 workspace")
    if slug == "focus":
        from witty_agent.focus_board import load_focus, render_focus

        try:
            target = _scope_dir("workspace")
        except RuntimeError:
            target = _scope_dir(scope)
        text = render_focus(load_focus(target))
        if not text:
            raise FileNotFoundError("没有记忆 focus")
        return text
    return read_topic(_scope_dir(scope), slug)
