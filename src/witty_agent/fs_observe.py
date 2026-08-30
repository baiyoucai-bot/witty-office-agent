"""会话内文件观察：read 后才允许覆盖写 / edit。

不做事件闸。状态只在本进程有效，按会话隔离，不落盘。
版本用 mtime_ns+size，窗口读也算观察整文件。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from witty_agent.prompts import get_prompt


@dataclass(frozen=True)
class _Record:
    kind: str
    stamp: tuple[int, int] | None


_OWNER = ""
_STATES: OrderedDict[str, dict[str, _Record]] = OrderedDict()
_MAX_OWNERS = 32


def _owner_key(workspace: str, session_id: str = "") -> str:
    root = str(Path(workspace).resolve())
    sid = (session_id or "").strip()
    return f"{root}::{sid}" if sid else root


def bind_owner(workspace: str, session_id: str = "") -> None:
    """切到该工作区+会话的观察表。其它会话的记录保留。"""
    global _OWNER
    _OWNER = _owner_key(workspace, session_id)
    if _OWNER in _STATES:
        _STATES.move_to_end(_OWNER)
    else:
        _STATES[_OWNER] = {}
        _evict_owners()


def _state() -> dict[str, _Record]:
    return _STATES.setdefault(_OWNER, {})


def clear_observations() -> None:
    _state().clear()


def drop_owner(workspace: str, session_id: str = "") -> bool:
    """丢掉一份观察表。会话删除时调用。"""
    return _drop_key(_owner_key(workspace, session_id))


def forget_session(session_id: str) -> int:
    """按 session_id 清掉所有工作区下的观察表。"""
    sid = (session_id or "").strip()
    if not sid:
        return 0
    suffix = f"::{sid}"
    victims = [key for key in _STATES if key.endswith(suffix)]
    for key in victims:
        _drop_key(key)
    return len(victims)


def observation_owner_count() -> int:
    return len(_STATES)


def _drop_key(key: str) -> bool:
    global _OWNER
    existed = key in _STATES
    _STATES.pop(key, None)
    if _OWNER == key:
        _OWNER = ""
    return existed


def _evict_owners() -> None:
    while len(_STATES) > _MAX_OWNERS:
        victim = next((key for key in _STATES if key != _OWNER), None)
        if victim is None:
            break
        _STATES.pop(victim, None)


def observe_present(path: Path) -> None:
    _state()[_key(path)] = _Record("present", _stamp(path))


def observe_absent(path: Path) -> None:
    _state()[_key(path)] = _Record("absent", None)


def forget_changed() -> list[Path]:
    """bash/外部改过的文件不再算已观察。"""
    dropped: list[Path] = []
    state = _state()
    for key, record in list(state.items()):
        path = Path(key)
        current = _stamp(path)
        changed = (record.kind == "present" and record.stamp != current) or (
            record.kind == "absent" and current is not None
        )
        if not changed:
            continue
        del state[key]
        dropped.append(path)
    return dropped


def changed_notice(workspace: str) -> str:
    """bash/exec 结束后点名已变路径，须再 read。"""
    from witty_agent.sandbox import display_path

    changed = forget_changed()
    if not changed:
        return ""
    names = ", ".join(display_path(item, workspace) for item in changed[:8])
    if len(changed) > 8:
        names += "…"
    return "\n" + get_prompt("fs_bash_changed", paths=names)


def authorize_write(path: Path, shown: str) -> None:
    """已存在且未见过 → 拒覆盖；见过后没了或变了 → 过期。未见或确认不存在可新建。"""
    if not _enabled():
        return
    current = _stamp(path)
    record = _state().get(_key(path))
    if current is None:
        if record is None or record.kind == "absent":
            return
        raise ValueError(get_prompt("fs_stale_version", path=shown))
    if record is None:
        raise ValueError(get_prompt("fs_not_observed_write", path=shown))
    if record.kind != "present" or record.stamp != current:
        raise ValueError(get_prompt("fs_stale_version", path=shown))


def authorize_edit(path: Path, shown: str) -> None:
    """未见过 → 先 read；确认不存在 → 不能改；版本变了或读后被删 → 再读。"""
    if not _enabled():
        return
    current = _stamp(path)
    record = _state().get(_key(path))
    if record is None:
        raise ValueError(get_prompt("fs_not_observed_edit", path=shown))
    if record.kind == "absent":
        raise ValueError(get_prompt("fs_not_found_edit", path=shown))
    if current is None or record.stamp != current:
        raise ValueError(get_prompt("fs_stale_version", path=shown))


def _key(path: Path) -> str:
    return str(path.resolve())


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _enabled() -> bool:
    from witty_agent.runtime import fs_observe_settings

    return bool(fs_observe_settings()["observe"])
