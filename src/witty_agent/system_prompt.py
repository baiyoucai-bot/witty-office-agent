"""拼装系统提示，正文全部来自 prompts.toml。"""

from __future__ import annotations

from pathlib import Path

from witty_agent.context import (
    budget_instruction_files,
    escape_instruction_text,
    load_context_files,
    load_system_overrides,
)
from witty_agent.dispatch import is_cheap_lookup, is_chat_turn
from witty_agent.memory import SessionMemory
from witty_agent.prompts import get_prompt
from witty_agent.skills import SkillMeta, list_skills, match_relevant_skills, network_label
from witty_agent.host_context import host_section
from witty_agent.time_context import clock_now

_CATALOG_LIMIT = 3
_IDLE_GUIDELINES = (
    "guideline_concise",
    "guideline_decide",
    "guideline_stop",
)
_CHEAP_GUIDELINES = (
    "guideline_concise",
    "guideline_decide",
    "guideline_tools_cheap",
    "guideline_stop",
    "guideline_cite",
)
_TASK_GUIDELINES = (
    "guideline_concise",
    "guideline_decide",
    "guideline_tools",
    "guideline_dispatch",
    "guideline_ask",
    "guideline_stop",
    "guideline_cite",
    "guideline_show_paths",
    "guideline_timeline",
)
_PLAN_GUIDELINES = (
    "guideline_concise",
    "guideline_decide",
    "guideline_cite",
)
_MEMORY_TOOLS = frozenset({"memory_read", "memory_status", "memory_write"})
_MAIL_TOOLS = frozenset(
    {
        "mail_status",
        "mail_list",
        "mail_read",
        "mail_analyze",
        "mail_draft",
        "mail_attach",
        "mail_send",
        "mail_save",
        "mail_reply",
    }
)
_LINK_TOOLS = frozenset({"link_add", "link_search", "link_ingest", "link_resolve", "link_habits"})
_DIARY_TOOLS = frozenset({"diary_write", "diary_read", "diary_list"})
_WEB_TOOLS = frozenset({"web_fetch"})
_PPTX_TOOLS = frozenset(
    {
        "pptx_create",
        "pptx_add_slide",
        "pptx_edit_slide",
        "pptx_add_picture",
        "pptx_outline",
        "pptx_themes",
        "pptx_render",
        "pptx_from_html",
        # 与 tool_surface._GROUPS 里的 pptx 组对齐：两边必须同一批名字，
        # 否则续问轮只留 used_names 时会公示了工具却不给指引。
        "pptx_check",
        "pptx_snapshot",
        "pptx_replace_slide",
        "pptx_list_boxes",
        "pptx_edit_box",
        "pptx_add_page",
        "pptx_add_pages",
    }
)


def guideline_keys(
    *,
    prompt: str | None,
    tool_names: list[str],
    plan_active: bool = False,
) -> list[str]:
    """Idle greetings keep decide/stop; plan mode drops spawn/write/stop."""
    if prompt is not None and is_chat_turn(prompt):
        return list(_IDLE_GUIDELINES)
    if plan_active:
        keys = list(_PLAN_GUIDELINES)
        if _MEMORY_TOOLS & set(tool_names):
            keys.append("guideline_use_memory")
        return keys
    if prompt is not None and is_cheap_lookup(prompt):
        return list(_CHEAP_GUIDELINES)
    keys = list(_TASK_GUIDELINES)
    if _MEMORY_TOOLS & set(tool_names):
        keys.append("guideline_use_memory")
    if "bash" in tool_names and not {"grep", "find", "ls"} & set(tool_names):
        keys.insert(0, "guideline_use_bash_explore")
    return keys


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def clip_skill_description(text: str, *, limit: int = 88) -> str:
    """First-layer catalog: keep a short when-clause, not the full SKILL.md line."""
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    first = compact.split(". ", 1)[0].rstrip(".")
    if len(first) <= limit:
        return first
    cut = compact[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(".,;:") + "…"


def format_skills_section(
    skills: list[SkillMeta],
    *,
    prompt: str | None = None,
    plan_active: bool = False,
    skill_query: str | None = None,
) -> str:
    if prompt is not None and is_chat_turn(prompt):
        return "\n" + get_prompt("skills_idle") + "\n"
    if plan_active:
        return "\n" + get_prompt("skills_plan") + "\n"
    shown = skills
    intro = "skills_intro"
    radar = skill_query if skill_query is not None else prompt
    if radar is not None:
        shown = match_relevant_skills(radar, skills, min_score=4, limit=_CATALOG_LIMIT)
        if not shown:
            return "\n" + get_prompt("skills_miss") + "\n"
        intro = "skills_intro_matched"
    if not shown:
        return ""
    items = [
        get_prompt(
            "skill_item",
            skill_name=_escape(item.name),
            network_label=network_label(item.network),
            description=_escape(clip_skill_description(item.description)),
        )
        for item in shown
    ]
    return (
        "\n"
        + get_prompt(intro)
        + "\n"
        + get_prompt("skills_open")
        + "\n"
        + "\n".join(items)
        + "\n"
        + get_prompt("skills_close")
        + "\n"
    )


def format_project_context(
    files: list[dict[str, str]],
    *,
    max_chars: int | None = None,
) -> str:
    if not files:
        return ""
    if max_chars is None:
        from witty_agent.runtime import context_settings

        cap = int(context_settings()["max_chars"])
    else:
        cap = max_chars
    kept, omitted, truncated = budget_instruction_files(files, max_chars=cap)
    if not kept and not omitted and not truncated:
        return ""
    blocks = [get_prompt("project_context_open"), "", get_prompt("project_context_header"), ""]
    if omitted or truncated:
        parts: list[str] = []
        if omitted:
            parts.append(
                get_prompt(
                    "instruction_budget_omit",
                    paths="、".join(escape_instruction_text(path) for path in omitted),
                )
            )
        if truncated:
            parts.append(
                get_prompt("instruction_budget_trunc", path=escape_instruction_text(truncated))
            )
        blocks.append(get_prompt("instruction_budget", max_chars=str(cap), detail="".join(parts)))
        blocks.append("")
    for item in kept:
        shown = item.get("display") or item.get("path") or ""
        blocks.append(
            get_prompt("project_instructions_open", path=escape_instruction_text(shown))
        )
        blocks.append(escape_instruction_text(item["content"]))
        blocks.append(get_prompt("project_instructions_close"))
        blocks.append("")
    blocks.append(get_prompt("project_context_close"))
    blocks.append("")
    return "\n".join(blocks) + "\n"


def _capability_sections(tool_names: list[str], memory: SessionMemory | None = None) -> str:
    names = set(tool_names)
    parts: list[str] = []
    if names & _MAIL_TOOLS:
        parts.append(get_prompt("email_capability"))
        if names & _DIARY_TOOLS:
            parts.append(get_prompt("email_diary_bridge"))
    if names & _LINK_TOOLS:
        parts.append(get_prompt("link_capability"))
        from witty_agent.links import habit_summary

        habit = habit_summary()
        if habit:
            parts.append(get_prompt("link_habit_section", body=habit))
    if names & _DIARY_TOOLS:
        parts.append(get_prompt("diary_capability"))
        from witty_agent.diary import today_excerpt

        # 日记归属这个 agent 的记忆目录。不传就只能靠进程级环境变量猜，多 agent 会串。
        excerpt = today_excerpt(memory_dir=getattr(memory, "user_dir", None))
        if excerpt:
            parts.append(get_prompt("diary_today_section", body=excerpt))
    if names & _PPTX_TOOLS:
        parts.append(get_prompt("pptx_capability"))
    if names & _WEB_TOOLS:
        parts.append(get_prompt("web_capability"))
    if not parts:
        return ""
    return "\n" + "\n".join(parts) + "\n"


def format_vault_section(vault_keys: list[str] | None) -> str:
    if not vault_keys:
        return ""
    return "\n" + get_prompt("vault_section", keys=", ".join(sorted(vault_keys))) + "\n"


def is_placeholder_profile(text: str) -> bool:
    """True when the standing profile has no real who/prefs/assets/followups."""
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if not line or line.startswith("#") or line.startswith("对话轮次"):
            continue
        if "尚未记录" in line or line in {"无", "待跟进：无"}:
            continue
        if "：" in line:
            value = line.split("：", 1)[1].strip()
            if value in {"尚未记录", "无"}:
                continue
        return False
    return True


def format_recalled_text(memory: SessionMemory) -> str:
    if memory.hits:
        from witty_agent.memory import format_hit_list, hits_layer

        text = format_hit_list(memory.hits, excerpt_limit=80)
        if hits_layer(memory.hits) == "archive":
            return text
        rows = [
            item
            for item in (memory.empty or {}).get("archive") or []
            if isinstance(item, dict) and item.get("id") and item.get("overlap")
        ]
        slugs = [str(item.get("id")) for item in rows[:6]]
        if slugs:
            extra = get_prompt("recalled_archive_browse", slugs=", ".join(slugs))
            return f"{text}\n{extra}".strip()
        return text
    if memory.retrieved:
        return memory.retrieved
    empty = memory.empty or {}
    if empty.get("reason") != "no_overlap":
        return ""
    populated = empty.get("populated") or []
    slugs = ", ".join(
        f"{item.get('id')} ({item.get('count')})"
        for item in populated[:8]
        if isinstance(item, dict) and item.get("id")
    )
    archive_rows = empty.get("archive") or []
    archive_slugs = ", ".join(
        f"{item.get('id')} ({item.get('count')})"
        for item in archive_rows[:6]
        if isinstance(item, dict) and item.get("id")
    )
    if not slugs and not archive_slugs:
        return ""
    return get_prompt(
        "memory_empty_miss",
        slugs=slugs or "-",
        archive=str(int(empty.get("archive_count") or 0)),
        archive_slugs=archive_slugs or "-",
    )


def format_memory_section(memory: SessionMemory | None) -> str:
    if memory is None:
        return ""
    recalled = format_recalled_text(memory)
    if is_placeholder_profile(memory.profile):
        text = get_prompt(
            "memory_user_thin",
            user_dir=str(memory.user_dir),
            retrieved=recalled,
        )
    else:
        text = get_prompt(
            "memory_user",
            user_dir=str(memory.user_dir),
            profile=memory.profile or "",
            retrieved=recalled,
        )
    if memory.workspace_dir is not None:
        text += "\n" + get_prompt(
            "memory_workspace",
            workspace_dir=str(memory.workspace_dir),
        )
        from witty_agent.focus_board import load_focus, render_focus, seed_from_lattice
        from witty_agent.handoff_note import handoff_notice, workspace_cwd
        from witty_agent.memory_config import load_memory_settings

        cap = load_memory_settings().focus_max_chars
        board = seed_from_lattice(memory.workspace_dir, load_focus(memory.workspace_dir))
        focus = render_focus(board, limit=cap)
        if focus:
            text += "\n" + get_prompt("focus_board_section", body=focus)
        cwd = workspace_cwd(memory.workspace_dir)
        if cwd is not None:
            note = handoff_notice(memory.workspace_dir, cwd)
            if note:
                text += "\n" + note
    return "\n" + text + "\n"


def format_agent_role_section(role: str | None) -> str:
    """这个 agent 自己的角色段。空串（含只剩种子脚手架）不占位。"""
    body = (role or "").strip()
    if not body:
        return ""
    return "\n" + get_prompt("agent_role_section", body=escape_instruction_text(body)) + "\n"


def build_system_prompt(
    cwd: str | Path,
    *,
    tool_names: list[str],
    skills: list[SkillMeta] | None = None,
    context_files: list[dict[str, str]] | None = None,
    memory: SessionMemory | None = None,
    vault_keys: list[str] | None = None,
    plan_section: str = "",
    commands_section: str = "",
    todo_section: str = "",
    list_snippets: bool = True,
    prompt: str | None = None,
    plan_active: bool = False,
    skill_query: str | None = None,
    agent_role: str | None = None,
) -> str:
    custom, append = load_system_overrides()
    files = context_files if context_files is not None else load_context_files(cwd)
    loaded_skills = skills if skills is not None else list_skills()
    if list_snippets:
        snippets: list[str] = []
        for name in tool_names:
            key = f"tool_snippet_{name.replace('-', '_')}"
            try:
                snippets.append(f"- {name}: {get_prompt(key)}")
            except KeyError:
                continue
        tools_list = "\n".join(snippets) if snippets else "(none)"
    else:
        tools_list = get_prompt("tools_attached")
    clock = clock_now()
    time_section = get_prompt(
        "time_now_section",
        timestamp=str(clock["timestamp"]),
        zone=str(clock["zone"]),
        weekday=str(clock["weekday"]),
        weekday_zh=str(clock.get("weekday_zh") or clock["weekday"]),
        date=str(clock["date"]),
    )
    machine = host_section(cwd=cwd)
    guidelines = [
        get_prompt(key)
        for key in guideline_keys(prompt=prompt, tool_names=tool_names, plan_active=plan_active)
    ]
    project_context = format_project_context(files)
    skills_section = (
        format_skills_section(
            loaded_skills,
            prompt=prompt,
            plan_active=plan_active,
            skill_query=skill_query,
        )
        if {"read", "skill"} & set(tool_names)
        else ""
    )
    skills_section += _capability_sections(tool_names, memory)
    memory_section = format_memory_section(memory)
    vault_section = format_vault_section(vault_keys)
    role_section = format_agent_role_section(agent_role)
    if custom:
        body = custom
        if append:
            body += "\n\n" + append
        # SYSTEM.md 整段替换通用角色，但本 agent 的角色是它自己的配置，仍要跟上。
        body += role_section
        body += "\n\n" + time_section + "\n\n" + machine + "\n\n" + project_context + skills_section + memory_section + vault_section
        extra = (todo_section or "") + (plan_section or "") + (commands_section or "")
        if extra:
            body += "\n" + extra + "\n"
        body += f"\nCurrent working directory: {Path(cwd).as_posix()}\n"
        return body
    text = get_prompt(
        "system_default",
        harness_system=get_prompt("harness_system"),
        agent_role_section=role_section,
        time_section=time_section,
        host_section=machine,
        tools_list=tools_list,
        guidelines="\n".join(f"- {item}" for item in guidelines),
        project_context=project_context,
        skills_section=skills_section,
        memory_section=memory_section + vault_section,
        todo_section=todo_section or "",
        plan_section=(plan_section or "") + (commands_section or ""),
        cwd=Path(cwd).as_posix(),
    )
    if append:
        text += "\n\n" + append
    return text
