"""按 Agent 记录技能/工具启停。内核工具不可关闭。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from witty_agent.kernel_surface import is_kernel_tool
from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, agent_state_dir
from witty_agent.logging import get_logger

logger = get_logger("catalog")

_CURRENT: ContextVar["SurfaceCatalog | None"] = ContextVar("witty_catalog", default=None)


@dataclass
class SurfaceCatalog:
    disabled_skills: set[str] = field(default_factory=set)
    disabled_tools: set[str] = field(default_factory=set)

    def skill_enabled(self, name: str) -> bool:
        return name not in self.disabled_skills

    def tool_enabled(self, name: str) -> bool:
        if is_kernel_tool(name):
            return True
        return name not in self.disabled_tools


def catalog_path(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "catalog.toml"


def load_catalog(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> SurfaceCatalog:
    from witty_agent.tomlcompat import tomllib

    path = catalog_path(project_id, agent_id, root=root)
    if not path.is_file():
        return SurfaceCatalog()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    skills = {str(item) for item in (data.get("disabled_skills") or []) if str(item).strip()}
    tools = {str(item) for item in (data.get("disabled_tools") or []) if str(item).strip()}
    tools = {name for name in tools if not is_kernel_tool(name)}
    return SurfaceCatalog(disabled_skills=skills, disabled_tools=tools)


def save_catalog(
    catalog: SurfaceCatalog,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    path = catalog_path(project_id, agent_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    skills = ", ".join(f'"{name}"' for name in sorted(catalog.disabled_skills))
    tools = ", ".join(
        f'"{name}"' for name in sorted(catalog.disabled_tools) if not is_kernel_tool(name)
    )
    path.write_text(
        "# witty surface catalog\n"
        f"disabled_skills = [{skills}]\n"
        f"disabled_tools = [{tools}]\n",
        encoding="utf-8",
    )
    logger.info(
        "写入表面目录 project=%s agent=%s skills=%s tools=%s",
        project_id,
        agent_id,
        len(catalog.disabled_skills),
        len(catalog.disabled_tools),
    )
    return path


def set_skill_enabled(
    name: str,
    enabled: bool,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> SurfaceCatalog:
    catalog = load_catalog(project_id, agent_id, root=root)
    if enabled:
        catalog.disabled_skills.discard(name)
    else:
        catalog.disabled_skills.add(name)
    save_catalog(catalog, project_id, agent_id, root=root)
    return catalog


def set_tool_enabled(
    name: str,
    enabled: bool,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> SurfaceCatalog:
    if is_kernel_tool(name):
        raise ValueError(f"内核工具不可关闭: {name}")
    catalog = load_catalog(project_id, agent_id, root=root)
    if enabled:
        catalog.disabled_tools.discard(name)
    else:
        catalog.disabled_tools.add(name)
    save_catalog(catalog, project_id, agent_id, root=root)
    return catalog


def bind_catalog(catalog: SurfaceCatalog | None) -> object:
    return _CURRENT.set(catalog)


def reset_catalog(token: object) -> None:
    _CURRENT.reset(token)  # type: ignore[arg-type]


def current_catalog() -> SurfaceCatalog:
    return _CURRENT.get() or SurfaceCatalog()
