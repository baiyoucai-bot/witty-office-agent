"""租户项目：多 Agent + 模型表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from witty_agent.layout import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    assert_id,
    data_root,
    project_config_path,
    project_dir,
)
from witty_agent.logging import get_logger

logger = get_logger("state.project")


@dataclass
class ModelEntry:
    provider: str
    model_id: str
    api_key: str = ""
    base_url: str = ""
    display_name: str = ""


@dataclass
class ProjectConfig:
    project_id: str
    default_provider: str = "openai"
    default_model_id: str = ""
    approval_mode: str = "always-ask"
    models: list[ModelEntry] = field(default_factory=list)


def _write_toml(path: Path, config: ProjectConfig) -> None:
    lines = [
        f'project_id = "{config.project_id}"',
        f'default_provider = "{config.default_provider}"',
        f'default_model_id = "{config.default_model_id}"',
        f'approval_mode = "{config.approval_mode}"',
        "",
    ]
    for item in config.models:
        lines.append("[[models]]")
        lines.append(f'provider = "{item.provider}"')
        lines.append(f'model_id = "{item.model_id}"')
        if item.api_key:
            lines.append(f'api_key = "{item.api_key}"')
        if item.base_url:
            lines.append(f'base_url = "{item.base_url}"')
        if item.display_name:
            lines.append(f'display_name = "{item.display_name}"')
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)


def _read_toml(path: Path) -> ProjectConfig:
    from witty_agent.tomlcompat import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    models = [
        ModelEntry(
            provider=str(row.get("provider", "")),
            model_id=str(row.get("model_id", "")),
            api_key=str(row.get("api_key", "")),
            base_url=str(row.get("base_url", "")),
            display_name=str(row.get("display_name", "")),
        )
        for row in data.get("models") or []
        if isinstance(row, dict)
    ]
    return ProjectConfig(
        project_id=str(data.get("project_id") or path.parent.name),
        default_provider=str(data.get("default_provider") or "openai"),
        default_model_id=str(data.get("default_model_id") or ""),
        approval_mode=str(data.get("approval_mode") or "always-ask"),
        models=models,
    )


def init_project(
    project_id: str = DEFAULT_PROJECT_ID,
    *,
    root: Path | None = None,
    approval_mode: str = "always-ask",
) -> ProjectConfig:
    assert_id("project_id", project_id)
    path = project_config_path(project_id, root=root)
    if path.is_file():
        return _read_toml(path)
    config = ProjectConfig(project_id=project_id, approval_mode=approval_mode)
    _write_toml(path, config)
    (project_dir(project_id, root=root) / "agents").mkdir(parents=True, exist_ok=True)
    logger.info("初始化项目 project=%s path=%s", project_id, path)
    return config


def load_project_config(
    project_id: str = DEFAULT_PROJECT_ID, *, root: Path | None = None
) -> ProjectConfig:
    path = project_config_path(project_id, root=root)
    if not path.is_file():
        return init_project(project_id, root=root)
    return _read_toml(path)


def save_project_config(config: ProjectConfig, *, root: Path | None = None) -> None:
    _write_toml(project_config_path(config.project_id, root=root), config)


def list_agents(project_id: str = DEFAULT_PROJECT_ID, *, root: Path | None = None) -> list[str]:
    agents = project_dir(project_id, root=root) / "agents"
    if not agents.is_dir():
        return []
    return sorted(item.name for item in agents.iterdir() if item.is_dir())


def resolve_root() -> Path:
    return data_root()
