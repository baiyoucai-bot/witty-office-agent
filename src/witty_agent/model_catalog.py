"""本机多模型目录。钥匙进 vault，配置进 agent_state/models.toml。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, agent_state_dir
from witty_agent.logging import get_logger
from witty_agent.runtime import model_settings
from witty_agent.vault import delete_vault_entry, load_vault, set_vault_entry

logger = get_logger("models")
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY_PREFIX = "MODEL_KEY_"


@dataclass
class ModelProfile:
    name: str
    model_id: str
    base_url: str
    display_name: str = ""
    max_tokens: int = 2048
    timeout_sec: int = 3600


@dataclass
class ModelCatalog:
    active: str = ""
    profiles: list[ModelProfile] = field(default_factory=list)

    def get(self, name: str) -> ModelProfile | None:
        for item in self.profiles:
            if item.name == name:
                return item
        return None


def _slug(raw: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return text[:48] or "default"


def assert_model_name(name: str) -> str:
    if not name or len(name) > 64 or not _NAME_RE.fullmatch(name):
        raise ValueError(f"模型名不合法: {name!r}（小写字母/数字/单连字符，如 gpt-5-6）")
    return name


def normalize_model_name(raw: str) -> str:
    """把用户手输的目录名尽量救活，救不活再报错。

    目录名会变成 vault 键（MODEL_KEY_<NAME>，环境变量形状），字符集必须收紧；
    但「gpt-5.6」「GPT 5.6」这种输入不该报错赶人，折成 gpt-5-6 即可。
    全中文等折完为空的才拒绝。
    """
    text = str(raw or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")
    if not slug:
        raise ValueError(f"模型名不合法: {raw!r}（用小写字母/数字/连字符起名，如 gpt-5-6；显示名不受限）")
    return assert_model_name(slug)


def catalog_path(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "models.toml"


def _key_name(name: str) -> str:
    return f"{_KEY_PREFIX}{name.replace('-', '_').upper()}"


def load_model_catalog(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> ModelCatalog:
    from witty_agent.tomlcompat import tomllib

    path = catalog_path(project_id, agent_id, root=root)
    if not path.is_file():
        return ModelCatalog()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    profiles: list[ModelProfile] = []
    for row in data.get("models") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        model_id = str(row.get("model_id") or "").strip()
        if not name or not model_id:
            continue
        profiles.append(
            ModelProfile(
                name=name,
                model_id=model_id,
                base_url=str(row.get("base_url") or ""),
                display_name=str(row.get("display_name") or ""),
                max_tokens=int(row.get("max_tokens") or 2048),
                timeout_sec=int(row.get("timeout_sec") or 3600),
            )
        )
    active = str(data.get("active") or "")
    if active and not any(item.name == active for item in profiles):
        active = profiles[0].name if profiles else ""
    return ModelCatalog(active=active, profiles=profiles)


def save_model_catalog(
    catalog: ModelCatalog,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    path = catalog_path(project_id, agent_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'active = "{catalog.active}"', ""]
    for item in catalog.profiles:
        lines.extend(
            [
                "[[models]]",
                f'name = "{item.name}"',
                f'model_id = "{item.model_id}"',
                f'base_url = "{item.base_url}"',
                f'display_name = "{item.display_name}"',
                f"max_tokens = {int(item.max_tokens)}",
                f"timeout_sec = {int(item.timeout_sec)}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    logger.info("写入模型目录 count=%s active=%s", len(catalog.profiles), catalog.active)
    return path


def ensure_model_catalog(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> ModelCatalog:
    catalog = load_model_catalog(project_id, agent_id, root=root)
    if catalog.profiles:
        if not catalog.active:
            catalog.active = catalog.profiles[0].name
            save_model_catalog(catalog, project_id, agent_id, root=root)
        return catalog
    settings = model_settings()
    name = _slug(str(settings.get("model_id") or "default"))
    profile = ModelProfile(
        name=name,
        model_id=str(settings.get("model_id") or name),
        base_url=str(settings.get("base_url") or ""),
        display_name=str(settings.get("model_id") or name),
        max_tokens=int(settings.get("max_tokens") or 2048),
        timeout_sec=int(settings.get("timeout_sec") or 3600),
    )
    catalog.profiles.append(profile)
    catalog.active = name
    save_model_catalog(catalog, project_id, agent_id, root=root)
    vault = load_vault(project_id, agent_id, root=root)
    legacy = vault.get("WITTY_API_KEY") or os.environ.get("WITTY_API_KEY") or ""
    if legacy and not vault.get(_key_name(name)):
        set_vault_entry(_key_name(name), legacy, project_id, agent_id, root=root)
    return catalog


def upsert_model(
    profile: ModelProfile,
    *,
    api_key: str | None = None,
    activate: bool = False,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    root: Path | None = None,
) -> ModelCatalog:
    assert_model_name(profile.name)
    if not profile.model_id.strip():
        raise ValueError("model_id 不能为空")
    catalog = ensure_model_catalog(project_id, agent_id, root=root)
    existing = catalog.get(profile.name)
    if existing is None:
        catalog.profiles.append(profile)
    else:
        existing.model_id = profile.model_id
        existing.base_url = profile.base_url
        existing.display_name = profile.display_name or existing.display_name
        existing.max_tokens = profile.max_tokens
        existing.timeout_sec = profile.timeout_sec
    if activate or not catalog.active:
        catalog.active = profile.name
    save_model_catalog(catalog, project_id, agent_id, root=root)
    if api_key:
        set_vault_entry(_key_name(profile.name), api_key, project_id, agent_id, root=root)
    if catalog.active == profile.name:
        apply_active_model(project_id, agent_id, root=root)
    return catalog


def delete_model(
    name: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> ModelCatalog:
    catalog = ensure_model_catalog(project_id, agent_id, root=root)
    if len(catalog.profiles) <= 1:
        raise ValueError("至少保留一个模型")
    catalog.profiles = [item for item in catalog.profiles if item.name != name]
    if catalog.active == name:
        catalog.active = catalog.profiles[0].name
    save_model_catalog(catalog, project_id, agent_id, root=root)
    delete_vault_entry(_key_name(name), project_id, agent_id, root=root)
    apply_active_model(project_id, agent_id, root=root)
    return catalog


def activate_model(
    name: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> ModelCatalog:
    catalog = ensure_model_catalog(project_id, agent_id, root=root)
    if catalog.get(name) is None:
        raise KeyError(f"未找到模型 {name}")
    catalog.active = name
    save_model_catalog(catalog, project_id, agent_id, root=root)
    apply_active_model(project_id, agent_id, root=root)
    return catalog


def apply_active_model(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> ModelProfile | None:
    catalog = load_model_catalog(project_id, agent_id, root=root)
    profile = catalog.get(catalog.active) if catalog.active else None
    if profile is None:
        return None
    os.environ["WITTY_MODEL_ID"] = profile.model_id
    if profile.base_url:
        os.environ["WITTY_BASE_URL"] = profile.base_url
    os.environ["WITTY_MAX_TOKENS"] = str(profile.max_tokens)
    os.environ["WITTY_TIMEOUT_SEC"] = str(profile.timeout_sec)
    vault = load_vault(project_id, agent_id, root=root)
    key = vault.get(_key_name(profile.name)) or vault.get("WITTY_API_KEY") or ""
    if key:
        os.environ["WITTY_API_KEY"] = key
    return profile


def public_models(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict:
    catalog = ensure_model_catalog(project_id, agent_id, root=root)
    vault = load_vault(project_id, agent_id, root=root)
    rows = []
    for item in catalog.profiles:
        rows.append(
            {
                "name": item.name,
                "model_id": item.model_id,
                "base_url": item.base_url,
                "display_name": item.display_name or item.model_id,
                "max_tokens": item.max_tokens,
                "timeout_sec": item.timeout_sec,
                "has_key": bool(vault.get(_key_name(item.name)) or vault.get("WITTY_API_KEY")),
                "active": item.name == catalog.active,
            }
        )
    return {"models": rows, "active": catalog.active}
