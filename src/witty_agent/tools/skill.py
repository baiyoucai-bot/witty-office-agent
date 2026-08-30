"""面向模型的 skill 加载器。"""

from __future__ import annotations

from witty_agent.catalog import current_catalog
from witty_agent.prompts import get_prompt
from witty_agent.skills import list_skills, load_skill, match_relevant_skills
from witty_agent.tools.registry import ToolSpec, register_tool
from witty_agent.types import AgentMessage


def skill(name: str) -> str:
    """加载可用技能的完整说明。任务匹配技能描述时先调用本工具再动手。"""
    if not current_catalog().skill_enabled(name):
        raise ValueError(get_prompt("skill_unknown", skill_name=name))
    try:
        loaded = load_skill(name)
    except KeyError as exc:
        raise ValueError(get_prompt("skill_unknown", skill_name=name)) from exc
    return get_prompt(
        "skill_invoke",
        skill_name=loaded.name,
        location=str(loaded.skill_file),
        skill_dir=str(loaded.path),
        body=loaded.body,
    )


def invoked_skill_names(messages: list[AgentMessage], *, reserved: set[str] | None = None) -> list[str]:
    """用户消息首行 `/name` 是确定性加载手势；命令命名空间除外。"""
    skip = reserved or set()
    catalog = current_catalog()
    known = {item.name for item in list_skills() if catalog.skill_enabled(item.name)}
    names: list[str] = []
    for message in messages:
        if message.role != "user":
            continue
        if message.source and str(message.source).startswith("plugin:"):
            continue
        first = message.text().splitlines()[0].strip() if message.text() else ""
        if not first.startswith("/"):
            continue
        token = first[1:].split(None, 1)[0] if first[1:] else ""
        if not token or token in skip or token not in known:
            continue
        if token not in names:
            names.append(token)
    return names


def inject_skill_bodies(names: list[str]) -> list[AgentMessage]:
    extras: list[AgentMessage] = []
    for name in names:
        extras.append(
            AgentMessage(
                role="user",
                content=skill(name),
                source="plugin:skill-invocation",
            )
        )
    return extras


def matched_skill_names(
    prompt: str,
    *,
    min_score: int = 4,
    limit: int = 1,
) -> list[str]:
    catalog = current_catalog()
    enabled = [item for item in list_skills() if catalog.skill_enabled(item.name)]
    return [
        item.name
        for item in match_relevant_skills(
            prompt, enabled, min_score=min_score, limit=limit
        )
    ]


def skill_names_for_turn(
    messages: list[AgentMessage],
    prompt: str,
    *,
    reserved: set[str] | None = None,
    auto: bool = True,
    min_score: int = 4,
    limit: int = 1,
    plan_active: bool = False,
) -> list[str]:
    """Slash `/name` wins; plan mode skips auto-load; otherwise load the best match."""
    names = invoked_skill_names(messages, reserved=reserved)
    if names or not auto or plan_active:
        return names
    return matched_skill_names(prompt, min_score=min_score, limit=limit)


register_tool(
    ToolSpec(
        name="skill",
        description=get_prompt("tool_desc_skill"),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": get_prompt("skill_param_name"),
                }
            },
            "required": ["name"],
        },
        func=skill,
    )
)
