"""非内核热插拔面。

愿望清单立刻落盘。回滚副作用、杀 MCP、reload 模块等到没有进行中的一轮再落地。
可逆 effect + 差集对账；对照 MCP：list_changed 刷新工具表。
内核包不可动。
"""

from __future__ import annotations

import importlib
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from witty_agent.kernel_surface import KERNEL_TOOL_PACKAGE, is_kernel_tool_module
from witty_agent.layout import data_root
from witty_agent.logging import get_logger
from witty_agent.plugins.effects import STACK

logger = get_logger("plugins")

_LOCK = threading.RLock()
_LIVE_NAME = "plugins.live.toml"
_BUSY: Callable[[], bool] | None = None
_MOUNTED: dict[str, str] = {}
_PERSIST_MTIME = 0.0
_LAST_RECONCILE: dict[str, object] = {"added": [], "removed": [], "unchanged": [], "errors": []}


@dataclass
class LiveSurface:
    extra_skill_paths: list[str] = field(default_factory=list)
    extra_packages: list[str] = field(default_factory=list)
    extra_sys_paths: list[str] = field(default_factory=list)
    extra_mcp: list[dict[str, Any]] = field(default_factory=list)
    disabled_packages: list[str] = field(default_factory=list)
    package_depends: dict[str, list[str]] = field(default_factory=dict)
    pending: bool = False
    root: Path | None = None


_STATE = LiveSurface()


def set_busy_probe(probe: Callable[[], bool] | None) -> None:
    global _BUSY
    _BUSY = probe


def surface_busy() -> bool:
    if _BUSY is None:
        return False
    try:
        return bool(_BUSY())
    except Exception:
        return False


def live_file(root: Path | None = None) -> Path:
    return (root or _STATE.root or data_root()) / _LIVE_NAME


def extra_skill_paths() -> list[Path]:
    with _LOCK:
        return [Path(item) for item in _STATE.extra_skill_paths]


def extra_packages() -> list[str]:
    with _LOCK:
        return list(_STATE.extra_packages)


def extra_sys_paths() -> list[str]:
    with _LOCK:
        return list(_STATE.extra_sys_paths)


def extra_mcp_servers() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(item) for item in _STATE.extra_mcp]


def persist_mtime() -> float:
    with _LOCK:
        return _PERSIST_MTIME


def mounted_units() -> dict[str, str]:
    with _LOCK:
        return dict(_MOUNTED)


def last_reconcile() -> dict[str, object]:
    with _LOCK:
        return {
            "added": list(_LAST_RECONCILE.get("added") or []),
            "removed": list(_LAST_RECONCILE.get("removed") or []),
            "unchanged": list(_LAST_RECONCILE.get("unchanged") or []),
            "errors": list(_LAST_RECONCILE.get("errors") or []),
        }


def disabled_packages() -> set[str]:
    with _LOCK:
        return set(_STATE.disabled_packages)


def _dump() -> str:
    def _arr(name: str, values: list[str]) -> str:
        inner = ", ".join(f'"{item}"' for item in values)
        return f"{name} = [{inner}]\n"

    lines = [
        "# witty live plugins（非内核）。愿望清单立刻写盘；忙时副作用延后回滚。\n",
        _arr("extra_skill_paths", _STATE.extra_skill_paths),
        _arr("extra_packages", _STATE.extra_packages),
        _arr("extra_sys_paths", _STATE.extra_sys_paths),
        _arr("disabled_packages", _STATE.disabled_packages),
        "\n[package_depends]\n",
    ]
    if not _STATE.package_depends:
        lines.append("# child = [\"parent\"]\n")
    for name, needs in sorted(_STATE.package_depends.items()):
        inner = ", ".join(f'"{item}"' for item in needs)
        lines.append(f'{name} = [{inner}]\n')
    lines.append("\n")
    for row in _STATE.extra_mcp:
        args = ", ".join(f'"{item}"' for item in (row.get("args") or []))
        lines.append("[[mcp]]\n")
        lines.append(f'name = "{row["name"]}"\n')
        lines.append(f'command = "{row["command"]}"\n')
        lines.append(f"args = [{args}]\n\n")
    return "".join(lines)


def _persist() -> Path:
    global _PERSIST_MTIME
    path = live_file(_STATE.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(), encoding="utf-8")
    try:
        _PERSIST_MTIME = path.stat().st_mtime
    except OSError:
        _PERSIST_MTIME = 0.0
    logger.info("写入热插拔面 path=%s", path)
    return path


def _apply_sys_paths(paths: list[str]) -> None:
    for item in reversed(paths):
        if item and item not in sys.path:
            sys.path.insert(0, item)
            STACK.push(
                f"sys:{item}",
                lambda path=item: sys.path.remove(path) if path in sys.path else None,
                label=f"sys.path {item}",
            )


def _blank_state() -> None:
    _STATE.extra_skill_paths.clear()
    _STATE.extra_packages.clear()
    _STATE.extra_sys_paths.clear()
    _STATE.extra_mcp.clear()
    _STATE.disabled_packages.clear()
    _STATE.package_depends.clear()
    _STATE.pending = False


def load_live(root: Path | None = None) -> LiveSurface:
    from witty_agent.tomlcompat import tomllib

    with _LOCK:
        _STATE.root = root
        path = live_file(root)
        if not path.is_file():
            _blank_state()
            return snapshot_state()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        _STATE.extra_skill_paths = [
            str(item) for item in (data.get("extra_skill_paths") or []) if str(item).strip()
        ]
        _STATE.extra_packages = [
            str(item) for item in (data.get("extra_packages") or []) if str(item).strip()
        ]
        _STATE.extra_sys_paths = [
            str(item) for item in (data.get("extra_sys_paths") or []) if str(item).strip()
        ]
        _STATE.disabled_packages = [
            str(item)
            for item in (data.get("disabled_packages") or [])
            if str(item).strip() and not is_kernel_tool_module(str(item))
        ]
        depends = data.get("package_depends") or {}
        _STATE.package_depends = {}
        if isinstance(depends, dict):
            for key, values in depends.items():
                if isinstance(values, list):
                    _STATE.package_depends[str(key)] = [str(item) for item in values if str(item).strip()]
        rows = []
        for item in data.get("mcp") or []:
            if isinstance(item, dict) and item.get("name") and item.get("command"):
                rows.append(
                    {
                        "name": str(item["name"]),
                        "command": str(item["command"]),
                        "args": [str(part) for part in (item.get("args") or [])],
                    }
                )
        _STATE.extra_mcp = rows
        _STATE.pending = False
        _apply_sys_paths(_STATE.extra_sys_paths)
        logger.info(
            "加载热插拔面 skills=%s packages=%s mcp=%s disabled=%s",
            len(_STATE.extra_skill_paths),
            len(_STATE.extra_packages),
            len(_STATE.extra_mcp),
            len(_STATE.disabled_packages),
        )
        return snapshot_state()


def reset_live(*, persist: bool = False) -> None:
    from witty_agent.mcp import reset_mcp_cache
    from witty_agent.plugins.watch import reset_watch, stop_watcher

    global _PERSIST_MTIME
    with _LOCK:
        STACK.unwind_all()
        reset_mcp_cache()
        _blank_state()
        _MOUNTED.clear()
        _LAST_RECONCILE.update({"added": [], "removed": [], "unchanged": [], "errors": []})
        _PERSIST_MTIME = 0.0
        reset_watch()
        stop_watcher()
        if persist:
            _persist()


def snapshot_state() -> LiveSurface:
    with _LOCK:
        return LiveSurface(
            extra_skill_paths=list(_STATE.extra_skill_paths),
            extra_packages=list(_STATE.extra_packages),
            extra_sys_paths=list(_STATE.extra_sys_paths),
            extra_mcp=[dict(item) for item in _STATE.extra_mcp],
            disabled_packages=list(_STATE.disabled_packages),
            package_depends={key: list(val) for key, val in _STATE.package_depends.items()},
            pending=_STATE.pending,
            root=_STATE.root,
        )


def _scan_counts() -> tuple[int, int]:
    from witty_agent.skills import list_skills
    from witty_agent.tools.registry import list_tools

    return len(list_skills()), len(list_tools())


def desired_units() -> dict[str, str]:
    """当前愿望清单。key -> fingerprint。"""
    units: dict[str, str] = {}
    with _LOCK:
        for path in _STATE.extra_sys_paths:
            units[f"sys:{path}"] = path
        for path in _STATE.extra_skill_paths:
            units[f"skill:{path}"] = path
        for name in _STATE.extra_packages:
            if name not in _STATE.disabled_packages:
                units[f"pkg:{name}"] = name
        for row in _STATE.extra_mcp:
            key = str(row.get("name") or "")
            if key:
                units[f"mcp:{key}"] = f"{row.get('command')}\0{','.join(row.get('args') or [])}"
    return units


def _unmount(key: str) -> None:
    if key.startswith("sys:"):
        STACK.unwind(key)
    # skill / pkg / mcp：清单不在就不再扫；MCP 由 reconcile_mcp 关进程


def _mount(key: str, fingerprint: str) -> None:
    if key.startswith("sys:"):
        _apply_sys_paths([key[4:]])
        return
    if key.startswith("pkg:"):
        importlib.invalidate_caches()
        importlib.import_module(key[4:])


def _apply_now(*, reload_modules: bool = False) -> dict[str, Any]:
    from witty_agent.mcp import reconcile_mcp
    from witty_agent.runtime import clear_runtime_cache

    desired = desired_units()
    with _LOCK:
        current = dict(_MOUNTED)
    remove = [key for key in current if key not in desired]
    add = [key for key in desired if key not in current]
    change = [key for key in desired if key in current and current[key] != desired[key]]
    remove.extend(change)
    add.extend(change)
    errors: list[str] = []
    applied: list[str] = []
    removed: list[str] = []
    try:
        for key in remove:
            _unmount(key)
            with _LOCK:
                _MOUNTED.pop(key, None)
            removed.append(key)
        for key in add:
            try:
                _mount(key, desired[key])
            except Exception as exc:
                errors.append(f"{key}: {exc}")
                logger.warning("对账挂载失败 unit=%s err=%s", key, exc)
                for undone in reversed(applied):
                    _unmount(undone)
                    with _LOCK:
                        _MOUNTED.pop(undone, None)
                raise
            with _LOCK:
                _MOUNTED[key] = desired[key]
            applied.append(key)
        if reload_modules:
            for name in list(desired):
                if not name.startswith("pkg:"):
                    continue
                pkg = name[4:]
                try:
                    if pkg in sys.modules:
                        importlib.reload(sys.modules[pkg])
                except Exception as exc:
                    errors.append(f"reload {pkg}: {exc}")
                    logger.warning("重载业务包失败 package=%s err=%s", pkg, exc)
        with _LOCK:
            _STATE.pending = False
            _LAST_RECONCILE.update(
                {
                    "added": list(applied),
                    "removed": list(removed),
                    "unchanged": [key for key in desired if key not in applied],
                    "errors": list(errors),
                }
            )
    except Exception as exc:
        with _LOCK:
            _LAST_RECONCILE["errors"] = [str(exc), *errors]
        raise
    clear_runtime_cache()
    reconcile_mcp(apply_close=True)
    skill_n, tool_n = _scan_counts()
    logger.info(
        "对账完成 added=%s removed=%s unchanged=%s skills=%s tools=%s",
        len(applied),
        len(removed),
        len(desired) - len(applied),
        skill_n,
        tool_n,
    )
    return {
        "reloaded": True,
        "applied": True,
        "deferred": False,
        "skills": skill_n,
        "tools": tool_n,
        "reconcile": last_reconcile(),
        **public_live(),
    }


def reconcile_from_disk() -> dict[str, Any]:
    """外部改了 plugins.live.toml 后重新加载愿望清单并对账。"""
    load_live(_STATE.root)
    return _commit(force=False)


def _commit(*, reload_modules: bool = False, force: bool = False) -> dict[str, Any]:
    if surface_busy() and not force:
        with _LOCK:
            _STATE.pending = True
        logger.info("热插拔已记下，本轮结束后再回滚/接 MCP")
        skill_n, tool_n = _scan_counts()
        return {
            "reloaded": False,
            "applied": False,
            "deferred": True,
            "message": "有对话在跑。愿望清单已记下，本轮结束后再卸进程、回滚副作用。",
            "skills": skill_n,
            "tools": tool_n,
            **public_live(),
        }
    return _apply_now(reload_modules=reload_modules)


def flush_pending() -> dict[str, Any] | None:
    with _LOCK:
        pending = _STATE.pending
    if not pending:
        return None
    if surface_busy():
        return None
    logger.info("对话结束，落地挂起的热插拔")
    return _apply_now(reload_modules=False)


def reload_surface(*, force: bool = False) -> dict[str, Any]:
    return _commit(reload_modules=True, force=force)


def public_live() -> dict[str, Any]:
    state = snapshot_state()
    return {
        "hotplug": True,
        "kernel_locked": True,
        "pending": state.pending,
        "busy": surface_busy(),
        "effects": STACK.scopes(),
        "extra_skill_paths": list(state.extra_skill_paths),
        "extra_packages": list(state.extra_packages),
        "extra_sys_paths": list(state.extra_sys_paths),
        "disabled_packages": list(state.disabled_packages),
        "package_depends": {key: list(val) for key, val in state.package_depends.items()},
        "mcp": [dict(item) for item in state.extra_mcp],
        "mounted": mounted_units(),
        "reconcile": last_reconcile(),
    }


def _norm_dir(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    resolved = resolved.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"找不到路径 {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"{resolved} 不是目录")
    return resolved


def attach_skill_path(path: str | Path, *, force: bool = False) -> dict[str, Any]:
    resolved = _norm_dir(path)
    text = str(resolved)
    with _LOCK:
        if text not in _STATE.extra_skill_paths:
            _STATE.extra_skill_paths.append(text)
        _persist()
    logger.info("挂载技能目录 path=%s", text)
    return {"attached": "skill_path", "path": text, **_commit(force=force)}


def detach_skill_path(path: str | Path, *, force: bool = False) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    text = str(resolved.resolve())
    with _LOCK:
        _STATE.extra_skill_paths = [item for item in _STATE.extra_skill_paths if item != text]
        _persist()
    logger.info("卸下技能目录 path=%s", text)
    return {"detached": "skill_path", "path": text, **_commit(force=force)}


def _assert_not_kernel_package(name: str) -> str:
    package = name.strip()
    if not package:
        raise ValueError("需要业务包名")
    if is_kernel_tool_module(package) or package == KERNEL_TOOL_PACKAGE:
        raise ValueError(f"内核工具包不可装卸: {package}")
    return package


def _dependents(package: str) -> list[str]:
    found: list[str] = []
    for name, needs in _STATE.package_depends.items():
        if package in needs:
            found.append(name)
            found.extend(_dependents(name))
    return found


def attach_package(
    name: str,
    path: str | None = None,
    depends: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    package = _assert_not_kernel_package(name)
    sys_path = ""
    if path:
        sys_path = str(_norm_dir(path))
    with _LOCK:
        if sys_path:
            if sys_path not in _STATE.extra_sys_paths:
                _STATE.extra_sys_paths.append(sys_path)
            _apply_sys_paths([sys_path])
        if depends:
            _STATE.package_depends[package] = [str(item) for item in depends if str(item).strip()]
        if package in _STATE.disabled_packages:
            _STATE.disabled_packages.remove(package)
        if package not in _STATE.extra_packages:
            _STATE.extra_packages.append(package)
        try:
            importlib.invalidate_caches()
            importlib.import_module(package)
        except ModuleNotFoundError as exc:
            if package in _STATE.extra_packages:
                _STATE.extra_packages.remove(package)
            raise FileNotFoundError(f"业务包不存在: {package}") from exc
        _persist()
    logger.info("挂载业务包 package=%s path=%s", package, sys_path or "-")
    return {"attached": "package", "package": package, "path": sys_path, **_commit(force=force)}


def detach_package(name: str, *, force: bool = False) -> dict[str, Any]:
    package = _assert_not_kernel_package(name)
    with _LOCK:
        cascade = [package, *_dependents(package)]
        for item in cascade:
            if is_kernel_tool_module(item):
                continue
            if item in _STATE.extra_packages:
                _STATE.extra_packages.remove(item)
            if item not in _STATE.disabled_packages:
                _STATE.disabled_packages.append(item)
        _persist()
    logger.info("卸下业务包 package=%s cascade=%s", package, cascade)
    return {
        "detached": "package",
        "package": package,
        "cascade": cascade,
        **_commit(force=force),
    }


def attach_mcp(
    name: str,
    command: str,
    args: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    server = str(name or "").strip()
    cmd = str(command or "").strip()
    if not server or not cmd:
        raise ValueError("MCP 需要 name 和 command")
    row = {"name": server, "command": cmd, "args": [str(item) for item in (args or [])]}
    with _LOCK:
        _STATE.extra_mcp = [item for item in _STATE.extra_mcp if item.get("name") != server]
        _STATE.extra_mcp.append(row)
        _persist()
    logger.info("挂载 MCP name=%s", server)
    return {"attached": "mcp", "name": server, **_commit(force=force)}


def detach_mcp(name: str, *, force: bool = False) -> dict[str, Any]:
    server = str(name or "").strip()
    if not server:
        raise ValueError("需要 MCP 名")
    with _LOCK:
        _STATE.extra_mcp = [item for item in _STATE.extra_mcp if item.get("name") != server]
        _persist()
    logger.info("卸下 MCP name=%s", server)
    return {"detached": "mcp", "name": server, **_commit(force=force)}
