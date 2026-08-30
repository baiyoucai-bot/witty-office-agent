"""改 Agent State 前先打版本快照，失败可回滚。"""

from __future__ import annotations

import tarfile
from pathlib import Path

from witty_agent.layout import agent_state_dir, snapshots_dir
from witty_agent.logging import get_logger
from witty_agent.state.agent_state import AgentRecord

logger = get_logger("evolution.snapshot")
_SKIP = {".vault.toml"}


def snapshot_path(record: AgentRecord, version: int | None = None, *, root: Path | None = None) -> Path:
    number = record.version if version is None else version
    return snapshots_dir(record.project_id, record.agent_id, root=root) / f"v{number}.tar.gz"


def save_snapshot(record: AgentRecord, *, root: Path | None = None) -> Path:
    dest = snapshot_path(record, root=root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        logger.info("快照已存在 version=%s path=%s", record.version, dest)
        return dest
    state = agent_state_dir(record.project_id, record.agent_id, root=root)
    with tarfile.open(dest, "w:gz") as archive:
        for item in state.rglob("*"):
            if item.name in _SKIP:
                continue
            archive.add(item, arcname=str(item.relative_to(state)))
    logger.info("写入快照 version=%s path=%s", record.version, dest)
    return dest


def restore_snapshot(record: AgentRecord, version: int, *, root: Path | None = None) -> Path:
    source = snapshot_path(record, version, root=root)
    if not source.is_file():
        raise FileNotFoundError(f"没有 version={version} 的快照")
    state = agent_state_dir(record.project_id, record.agent_id, root=root)
    for child in state.iterdir():
        if child.name in _SKIP:
            continue
        if child.is_file():
            child.unlink()
        else:
            _rmtree(child)
    with tarfile.open(source, "r:gz") as archive:
        archive.extractall(state, filter="data")
    logger.info("回滚快照 version=%s path=%s", version, source)
    return source


def _rmtree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            _rmtree(child)
        else:
            child.unlink()
    path.rmdir()
