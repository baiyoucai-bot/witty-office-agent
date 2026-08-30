"""一句话生成用户技能草稿。走 install_user_skill，不进内核循环。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.skills import _NAME_RE, SkillMeta, install_user_skill

logger = get_logger("skills")

_LATIN_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "of",
        "or",
        "the",
        "to",
        "with",
        "skill",
        "skills",
    }
)
_ZH_MAP = (
    ("知识库", "wiki"),
    ("幻灯片", "slides"),
    ("第二大脑", "wiki"),
    ("发票", "invoice"),
    ("税号", "tax"),
    ("抬头", "title"),
    ("表格", "table"),
    ("质检", "qa"),
    ("检查", "check"),
    ("审核", "review"),
    ("邮件", "mail"),
    ("周报", "week"),
    ("日报", "diary"),
    ("文档", "doc"),
    ("合同", "contract"),
    ("会议", "meeting"),
    ("审批", "approve"),
    ("报告", "report"),
    ("链接", "link"),
    ("演示", "slides"),
    ("格式", "format"),
    ("技能", ""),
)


def parse_create_skill_args(raw: str) -> tuple[str, str, bool]:
    """返回 (brief, explicit_name, overwrite)。"""
    tokens = str(raw or "").strip().split()
    overwrite = False
    name = ""
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--overwrite", "-f"}:
            overwrite = True
            index += 1
            continue
        if token == "--name" and index + 1 < len(tokens):
            name = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--name="):
            name = token.split("=", 1)[1]
            index += 1
            continue
        kept.append(token)
        index += 1
    if not name and kept and "-" in kept[0] and _NAME_RE.fullmatch(kept[0]) and len(kept) > 1:
        name = kept.pop(0)
    brief = " ".join(kept).strip()
    if not brief and name:
        brief = name.replace("-", " ")
    return brief, name, overwrite


def slug_from_brief(brief: str, explicit: str = "") -> str:
    wanted = str(explicit or "").strip()
    if wanted and _NAME_RE.fullmatch(wanted):
        return wanted
    text = str(brief or "").strip()
    first, _, rest = text.partition(" ")
    if "-" in first and _NAME_RE.fullmatch(first) and rest:
        return first
    if _NAME_RE.fullmatch(text):
        return text
    latin = [
        item.lower()
        for item in re.findall(r"[A-Za-z0-9]+", text)
        if item.lower() not in _LATIN_STOP
    ]
    mapped: list[str] = []
    keys = tuple(sorted(_ZH_MAP, key=lambda item: len(item[0]), reverse=True))
    index = 0
    while index < len(text):
        hit = ""
        step = 1
        for zh, en in keys:
            if zh and text.startswith(zh, index):
                hit = en
                step = len(zh)
                break
        if hit and hit not in mapped:
            mapped.append(hit)
        index += step
    parts: list[str] = []
    for item in latin + mapped:
        if item and item not in parts:
            parts.append(item)
    slug = "-".join(parts[:5])
    if slug and _NAME_RE.fullmatch(slug):
        return slug[:64]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"user-skill-{digest}"


def _one_line(text: str, limit: int = 200) -> str:
    cleaned = " ".join(str(text or "").split()).replace(":", "：")
    return cleaned[:limit]


def render_skill_markdown(name: str, brief: str) -> str:
    title = name.replace("-", " ")
    flat = _one_line(brief)
    return get_prompt(
        "skill_scaffold_markdown",
        skill=name,
        brief=flat,
        title=title,
        description=get_prompt("skill_scaffold_description", skill=name, brief=flat),
    )


def create_skill_from_brief(
    brief: str,
    *,
    name: str = "",
    overwrite: bool = False,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    root: Path | None = None,
) -> SkillMeta:
    text = str(brief or "").strip()
    if not text:
        raise ValueError(get_prompt("create_skill_usage"))
    slug = slug_from_brief(text, name)
    markdown = render_skill_markdown(slug, text)
    meta = install_user_skill(
        text=markdown,
        project_id=project_id,
        agent_id=agent_id,
        root=root,
        overwrite=overwrite,
    )
    logger.info("一句话生成技能 name=%s brief_chars=%s", meta.name, len(text))
    return meta
