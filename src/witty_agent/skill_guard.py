"""技能 compatibility 门槛，以及 allowed-tools 收权。"""

from __future__ import annotations

from collections.abc import Iterable

from witty_agent.logging import get_logger
from witty_agent.runtime import web_settings
from witty_agent.skills import SkillMeta, load_skill

logger = get_logger("skills")

_ALWAYS = frozenset({"skill", "list_available_skills", "ask_user_question"})
_ALIASES = {
    "glob": "find",
    "shell": "bash",
    "execute": "bash",
}


def normalize_tool_token(token: str) -> str:
    raw = str(token or "").strip()
    if "(" in raw:
        raw = raw.split("(", 1)[0]
    key = raw.replace("-", "_").casefold()
    return _ALIASES.get(key, key)


def skill_compatible(meta: SkillMeta) -> bool:
    settings = web_settings()
    if not settings.get("deny_public", False):
        return True
    text = (meta.compatibility or "").casefold()
    needles = ("public-internet", "requires-internet", "needs internet", "requires public", "外网", "公网")
    if any(item in text for item in needles):
        logger.info("技能与内网策略不合 name=%s compatibility", meta.name)
        return False
    return True


def allowlist_for_skills(names: Iterable[str]) -> frozenset[str] | None:
    """有技能声明了 allowed-tools 时返回并集（另加 skill / list_available_skills）。否则 None=不收权。"""
    declared: set[str] = set()
    any_limit = False
    for name in names:
        try:
            loaded = load_skill(name)
        except KeyError:
            continue
        if not loaded.allowed_tools:
            continue
        any_limit = True
        declared.update(normalize_tool_token(item) for item in loaded.allowed_tools)
    if not any_limit:
        return None
    declared.update(_ALWAYS)
    return frozenset(declared)


def tool_permitted(name: str, allow: frozenset[str] | None) -> bool:
    if allow is None:
        return True
    return normalize_tool_token(name) in allow
