"""kernel update：哈希区分旧默认与用户改过的叶子。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from witty_agent.logging import get_logger

logger = get_logger("kernel")
KERNEL_VERSION = "2026-08-13"
EXCLUDED = frozenset({"name", "description", "version", "kernel_version"})

DEFAULTS: dict[str, Any] = {
    "approval_mode": "always-ask",
}


@dataclass
class KernelUpdateResult:
    advanced: list[str]
    kept: list[str]
    kernel_version: str


def hash_kernel_value(value: Any) -> str:
    packed = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()[:16]


KERNEL_HASH_HISTORY: dict[str, dict[str, str]] = {
    "1": {"approval_mode": hash_kernel_value("always-ask")},
    KERNEL_VERSION: {"approval_mode": hash_kernel_value("always-ask")},
}


def _dump_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        lines.append(f"{key} = {_dump_value(value)}")
    return "\n".join(lines) + "\n"


def _historical_hashes(path: str) -> set[str]:
    hashes: set[str] = set()
    for generation in KERNEL_HASH_HISTORY.values():
        digest = generation.get(path)
        if digest:
            hashes.add(digest)
    return hashes


def apply_kernel_update(config_path: Path) -> KernelUpdateResult:
    from witty_agent.tomlcompat import tomllib

    stored: dict[str, Any] = {}
    if config_path.is_file():
        loaded = tomllib.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            stored = loaded
    advanced: list[str] = []
    kept: list[str] = []
    merged = dict(stored)
    for key, default in DEFAULTS.items():
        if key in EXCLUDED:
            continue
        current = stored.get(key)
        if current is None:
            merged[key] = default
            advanced.append(key)
        elif current == default:
            kept.append(key)
        elif hash_kernel_value(current) in _historical_hashes(key):
            merged[key] = default
            advanced.append(key)
        else:
            kept.append(key)
    merged["kernel_version"] = KERNEL_VERSION
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_dump_toml(merged), encoding="utf-8")
    logger.info("kernel 更新 advanced=%s kept=%s", advanced, kept)
    return KernelUpdateResult(advanced=advanced, kept=kept, kernel_version=KERNEL_VERSION)
