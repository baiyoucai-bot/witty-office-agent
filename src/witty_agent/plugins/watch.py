"""监视技能目录和 plugins.live.toml。无第三方依赖，轮询 mtime。"""

from __future__ import annotations

import threading
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.runtime import skill_paths

logger = get_logger("plugins")

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_GENERATION = 0
_SKILL_STAMP = ""
_LIVE_STAMP = 0.0


def skill_generation() -> int:
    with _LOCK:
        return _GENERATION


def _skill_stamp() -> str:
    from witty_agent.plugins.live import extra_skill_paths

    parts: list[str] = []
    roots = list(skill_paths()) + extra_skill_paths()
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if not root.exists():
            parts.append(f"{key}:missing")
            continue
        if root.is_file():
            try:
                stat = root.stat()
            except OSError:
                continue
            parts.append(f"{key}:{stat.st_mtime_ns}:{stat.st_size}")
            continue
        for child in sorted(root.iterdir()) if root.is_dir() else []:
            skill = child / "SKILL.md" if child.is_dir() else None
            if skill is None or not skill.is_file():
                continue
            try:
                stat = skill.stat()
            except OSError:
                continue
            parts.append(f"{skill}:{stat.st_mtime_ns}:{stat.st_size}")
    return "\n".join(parts)


def _live_mtime() -> float:
    from witty_agent.plugins.live import live_file, persist_mtime

    path = live_file()
    try:
        current = path.stat().st_mtime
    except OSError:
        return 0.0
    written = persist_mtime()
    if written and abs(current - written) < 0.05:
        return written
    return current


def poll_once() -> dict[str, object]:
    """扫一轮。技能文件变了加代；live.toml 被外部改了就对账。"""
    global _GENERATION, _SKILL_STAMP, _LIVE_STAMP
    from witty_agent.plugins.live import reconcile_from_disk

    changed_skills = False
    reconciled = False
    with _LOCK:
        stamp = _skill_stamp()
        if stamp != _SKILL_STAMP:
            if _SKILL_STAMP:
                _GENERATION += 1
                changed_skills = True
                logger.info("技能目录有变 generation=%s", _GENERATION)
            _SKILL_STAMP = stamp
        live_m = _live_mtime()
        if live_m and _LIVE_STAMP and live_m > _LIVE_STAMP + 0.05:
            reconciled = True
        if live_m:
            _LIVE_STAMP = live_m
        generation = _GENERATION
    if reconciled:
        logger.info("plugins.live.toml 外部变更，开始对账")
        reconcile_from_disk()
    return {
        "skill_generation": generation,
        "skills_changed": changed_skills,
        "reconciled": reconciled,
    }


def start_watcher(*, interval_s: float = 2.0) -> None:
    global _THREAD
    stop_watcher()
    _STOP.clear()
    poll_once()

    def loop() -> None:
        while not _STOP.wait(max(0.5, interval_s)):
            try:
                poll_once()
            except Exception as exc:
                logger.warning("监视技能目录失败 err=%s", exc)

    _THREAD = threading.Thread(target=loop, name="skill-watch", daemon=True)
    _THREAD.start()
    logger.info("技能目录监视已启动 interval_s=%s", interval_s)


def stop_watcher() -> None:
    global _THREAD
    _STOP.set()
    thread = _THREAD
    _THREAD = None
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1.0)


def reset_watch() -> None:
    global _GENERATION, _SKILL_STAMP, _LIVE_STAMP
    with _LOCK:
        _GENERATION = 0
        _SKILL_STAMP = ""
        _LIVE_STAMP = 0.0
