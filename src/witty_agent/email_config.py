"""本机邮箱通道：主机写 agent_state/email.toml，密码只进 vault。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from witty_agent.layout import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    agent_state_dir,
)
from witty_agent.logging import get_logger
from witty_agent.vault import load_vault, set_vault_entry

logger = get_logger("email_config")

IMAP_VAULT_KEY = "WITTY_IMAP_PASSWORD"
SMTP_VAULT_KEY = "WITTY_SMTP_PASSWORD"

_HOST_KEYS = ("imap_host", "smtp_host", "username", "mailbox")
_INT_KEYS = ("imap_port", "smtp_port")
_BOOL_KEYS = ("imap_ssl", "smtp_ssl", "smtp_starttls")


def email_overlay_path(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "email.toml"


def load_email_overlay(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    from witty_agent.tomlcompat import tomllib

    path = email_overlay_path(project_id, agent_id, root=root)
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value or "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_email_overlay(
    fields: dict[str, Any],
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    path = email_overlay_path(project_id, agent_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 本机邮箱通道。密码不写在这里，只进 vault。", ""]
    for key in (*_HOST_KEYS, *_INT_KEYS, *_BOOL_KEYS):
        if key not in fields:
            continue
        lines.append(f"{key} = {_toml_scalar(fields[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("写入邮箱通道 path=%s", path)
    return path


def save_email_secrets(
    *,
    imap_password: str = "",
    smtp_password: str = "",
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    root: Path | None = None,
) -> None:
    if imap_password:
        set_vault_entry(IMAP_VAULT_KEY, imap_password, project_id, agent_id, root=root)
    if smtp_password:
        set_vault_entry(SMTP_VAULT_KEY, smtp_password, project_id, agent_id, root=root)


def overlay_passwords(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, str]:
    vault = load_vault(project_id, agent_id, root=root)
    return {
        "imap_password": vault.get(IMAP_VAULT_KEY) or "",
        "smtp_password": vault.get(SMTP_VAULT_KEY) or "",
    }


def public_email_fields(cfg: dict[str, Any]) -> dict[str, Any]:
    imap_host = str(cfg.get("imap_host") or "").strip()
    smtp_host = str(cfg.get("smtp_host") or "").strip()
    username = str(cfg.get("username") or "").strip()
    has_imap = bool(cfg.get("imap_password"))
    has_smtp = bool(cfg.get("smtp_password"))
    return {
        "imap_host": imap_host,
        "imap_port": int(cfg.get("imap_port") or 993),
        "imap_ssl": bool(cfg.get("imap_ssl", True)),
        "smtp_host": smtp_host,
        "smtp_port": int(cfg.get("smtp_port") or 465),
        "smtp_ssl": bool(cfg.get("smtp_ssl", True)),
        "smtp_starttls": bool(cfg.get("smtp_starttls", False)),
        "username": username,
        "mailbox": str(cfg.get("mailbox") or "INBOX"),
        "imap_password": has_imap,
        "smtp_password": has_smtp,
        "configured": bool(imap_host and smtp_host and username and has_imap),
    }
