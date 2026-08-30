"""底座自带工具。业务工具不要写在这里。"""

from witty_agent.catalog import current_catalog
from witty_agent.skills import list_skills
from witty_agent.tools.registry import tool


@tool
def list_available_skills() -> list[str]:
    """列出当前已发现的技能名称和一句话用途，便于决定是否加载技能正文。"""
    catalog = current_catalog()
    return [
        f"{item.name}: {item.description}"
        for item in list_skills()
        if catalog.skill_enabled(item.name)
    ]
