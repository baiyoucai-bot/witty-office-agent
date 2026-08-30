"""业务插件目录。只加不卸核，给 HTTP 和桌面管理页用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from witty_agent.catalog import load_catalog
from witty_agent.kernel_surface import KERNEL_COMMANDS, KERNEL_TOOLS, is_kernel_tool
from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from witty_agent.logging import get_logger
from witty_agent.plugins.live import public_live
from witty_agent.plugins.watch import skill_generation
from witty_agent.runtime import tool_packages
from witty_agent.skills import list_skills, network_label
from witty_agent.tools.registry import list_tools

logger = get_logger("plugins")


def list_plugins(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """扫描已加载的业务工具包和技能，不暴露内核实现细节。"""
    catalog = load_catalog(project_id, agent_id, root=root)
    groups: dict[str, list[str]] = {}
    for spec in list_tools():
        module = str(getattr(spec.func, "__module__", "") or "")
        if not module.startswith("witty_agent.plugins"):
            continue
        groups.setdefault(module, []).append(spec.name)
    plugins = [
        {
            "name": module.rsplit(".", 1)[-1],
            "package": module,
            "kind": "tools",
            "kernel": False,
            "hotplug": True,
            "tools": names,
        }
        for module, names in sorted(groups.items())
    ]
    skills = []
    for item in list_skills(project_id, agent_id, root=root):
        skills.append(
            {
                "name": item.name,
                "origin": item.origin,
                "enabled": catalog.skill_enabled(item.name),
                "hotplug": item.origin == "user",
                "path": str(item.path),
                "network": item.network,
                "network_label": network_label(item.network),
            }
        )
    logger.info("插件目录 plugins=%s skills=%s", len(plugins), len(skills))
    return {
        "plugins": plugins,
        "packages": list(tool_packages()),
        "skills": skills,
        "kernel_tools": sorted(KERNEL_TOOLS),
        "kernel_commands": sorted(KERNEL_COMMANDS),
        "protected": True,
        "skill_generation": skill_generation(),
        **public_live(),
    }


def plugin_owns(name: str) -> bool:
    if is_kernel_tool(name):
        return False
    module = ""
    for spec in list_tools():
        if spec.name == name:
            module = str(getattr(spec.func, "__module__", "") or "")
            break
    return module.startswith("witty_agent.plugins")
