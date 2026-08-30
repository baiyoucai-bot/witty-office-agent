"""Agent Vault：密钥只进子进程环境，不进模型上下文。"""

from __future__ import annotations

import re
from pathlib import Path

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, vault_path
from witty_agent.logging import get_logger

logger = get_logger("vault")
VAULT_VALUE_MAX_LENGTH = 8192
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_bound: dict[str, str] = {}


def assert_vault_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"vault key 不合法: {key!r}")
    return key


def assert_vault_value(key: str, value: str) -> str:
    if len(value) > VAULT_VALUE_MAX_LENGTH:
        raise ValueError(f"vault {key} 超长: {len(value)} > {VAULT_VALUE_MAX_LENGTH}")
    return value


def load_vault(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, str]:
    from witty_agent.tomlcompat import tomllib

    path = vault_path(project_id, agent_id, root=root)
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    vault: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, str) and _KEY_RE.fullmatch(key):
            vault[key] = value
    return vault


def save_vault(
    vault: dict[str, str],
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    path = vault_path(project_id, agent_id, root=root)
    cleaned = {
        assert_vault_key(key): assert_vault_value(key, value)
        for key, value in vault.items()
        if value != ""
    }
    if not cleaned:
        if path.exists():
            path.unlink()
        bind_vault({})
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{key} = "{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for key, value in cleaned.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    logger.info("写入 vault keys=%s", ",".join(sorted(cleaned)))
    return path


def set_vault_entry(
    key: str,
    value: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> None:
    table = load_vault(project_id, agent_id, root=root)
    table[assert_vault_key(key)] = assert_vault_value(key, value)
    save_vault(table, project_id, agent_id, root=root)


def delete_vault_entry(
    key: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> None:
    table = load_vault(project_id, agent_id, root=root)
    table.pop(key, None)
    save_vault(table, project_id, agent_id, root=root)


def mask_vault(vault: dict[str, str]) -> dict[str, str]:
    return {key: "***" for key in vault}


def bind_vault(values: dict[str, str]) -> None:
    global _bound
    _bound = dict(values)


def bound_vault() -> dict[str, str]:
    return dict(_bound)
