"""本轮公示给模型的工具面。内核工具仍可执行，只是不全部塞进请求。

对照微信文 / Databricks：工具面越大，模型越容易摸无关工具。
不上卸载；list_tools / KERNEL_TOOLS 不变。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from witty_agent.dispatch import is_cheap_lookup, is_chat_turn, is_idle_prompt, recommend
from witty_agent.guard import (
    is_choice_only,
    needs_choice,
    needs_memory_browse,
    needs_todo,
    recalled_answer_hint,
)
from witty_agent.logging import get_logger
from witty_agent.plan_mode import MUTATING_TOOLS
from witty_agent.skills import match_relevant_skills

logger = get_logger("tools")

CORE_TOOLS = frozenset(
    {
        "apply_patch",
        "bash",
        "edit",
        "find",
        "grep",
        "ls",
        "read",
        "write",
    }
)

_GROUPS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (
        re.compile(
            r"子代理|委派|并行|同时|fanout|subagent|delegate|split work|independen",
            re.IGNORECASE,
        ),
        frozenset({"run_subagent", "run_fanout", "input_subagent"}),
    ),
    (
        re.compile(r"定时|日程|cron|schedule|每天|每周|\bjob\b|后台进程|长命令", re.IGNORECASE),
        frozenset({"schedule_list", "schedule_write", "schedule_delete", "exec_command", "input_command", "list_commands", "job_list", "job_kill", "job_output", "agenda_digest"}),
    ),
    (
        re.compile(r"日程摘要|今日日程|下次触发|agenda.digest|\bagenda_digest\b", re.IGNORECASE),
        frozenset({"agenda_digest", "schedule_list"}),
    ),
    (
        re.compile(
            r"上次会话|历史会话|session_query|旧对话|会话健康|会话回顾|session health|transcript",
            re.IGNORECASE,
        ),
        frozenset({"session_query", "session_health"}),
    ),
    (
        re.compile(r"spill:|spill_read|省略了\s*\d+\s*字节", re.IGNORECASE),
        frozenset({"spill_read"}),
    ),
    (
        # 长驻解释器不进 CORE_TOOLS：它是「读进来很大、要看的很小」这类活的工具，
        # 「你好」和改个文件都用不上，常驻公示等于把已经缩小的工具面又撑回去。
        # 这张词表是手写枚举，跟仓库里别的词表一样——加词要能换来真命中，见 UNRESOLVED。
        re.compile(
            r"解释器|变量|数据|表格|统计|算一下|计算|筛选|分析|画图|绘图|"
            r"python|pandas|dataframe|numpy|csv|excel|json|matplotlib|plot|repl|notebook|ipython",
            re.IGNORECASE,
        ),
        frozenset({"python_repl", "python_repl_status"}),
    ),
    (
        re.compile(r"/plan|\bplan mode\b|计划模式|exit_plan", re.IGNORECASE),
        frozenset({"exit_plan_mode", "plan_read", "plan_write"}),
    ),
    (
        re.compile(r"邮件|inbox|imap|smtp|发信|收件|附件|回信|草稿|\bmail_", re.IGNORECASE),
        frozenset(
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
        ),
    ),
    (
        re.compile(r"链接|书签|网址|打开了|收藏夹|\blink_|那个系统|OA|常用系统", re.IGNORECASE),
        frozenset({"link_add", "link_search", "link_ingest", "link_resolve", "link_habits"}),
    ),
    (
        re.compile(r"日记|今天做|今天干|行为日记|时间线|\bdiary_", re.IGNORECASE),
        frozenset({"diary_write", "diary_read", "diary_list"}),
    ),
    (
        re.compile(
            r"周报摘要|本周摘要|这周干了|weekly digest|\bweek_digest\b|\bweek-digest\b",
            re.IGNORECASE,
        ),
        frozenset({"week_digest"}),
    ),
    (
        re.compile(r"ppt|pptx|幻灯片|演示文稿|witty-ppt-skills|\bpptx_|powerpoint", re.IGNORECASE),
        frozenset(
            {
                "pptx_create",
                "pptx_add_slide",
                "pptx_edit_slide",
                "pptx_add_picture",
                "pptx_outline",
                "pptx_themes",
                "pptx_render",
                "pptx_from_html",
                "pptx_check",
                "pptx_snapshot",
                "pptx_replace_slide",
                "pptx_list_boxes",
                "pptx_edit_box",
                "pptx_add_page",
                "pptx_add_pages",
            }
        ),
    ),
    (
        re.compile(
            r"质检|版式检查|检查PPT|检查幻灯片|空页|字号|\bdoc_qa\b|\bdoc-qa\b|document QA|layout review|deck QA",
            re.IGNORECASE,
        ),
        frozenset({"doc_qa"}),
    ),
    (
        re.compile(
            r"表格质检|CSV质检|检查CSV|检查表格|空列|重复表头|\btable_qa\b|\btable-qa\b|table QA",
            re.IGNORECASE,
        ),
        frozenset({"table_qa"}),
    ),
    (
        re.compile(
            r"\bwiki\b|知识库|第二大脑|second brain|llm-wiki|llmwiki|"
            r"wiki 说|初始化 wiki|lint the wiki|个人知识库|"
            r"收进 wiki|删除原文|wiki_add|wiki_remove",
            re.IGNORECASE,
        ),
        frozenset(
            {
                "wiki_init",
                "wiki_search",
                "wiki_lint",
                "wiki_stats",
                "wiki_add",
                "wiki_remove",
                "wiki_sources",
            }
        ),
    ),
    (
        re.compile(
            r"\bsql\b|\bnl2sql\b|text2sql|问数|查数|取数|数据库查|表结构|字段含义|"
            r"数据源|写(?:个|条|一)?查询|\bselect\b|"
            r"\bsql_(?:run|schema|tables|values|check|pick|export|sources)\b",
            re.IGNORECASE,
        ),
        frozenset(
            {
                "sql_sources",
                "sql_schema",
                "sql_tables",
                "sql_values",
                "sql_run",
                "sql_export",
                "sql_check",
                "sql_pick",
            }
        ),
    ),
)

_SPAWN = frozenset({"run_subagent", "run_fanout", "input_subagent"})
_PLAN = frozenset({"exit_plan_mode", "plan_read", "plan_write"})
_LOOKUP_TOOLS = frozenset({"find", "grep", "ls", "read"})
_ASK = frozenset({"ask_user_question"})
_WEB = frozenset({"web_fetch", "web_search"})
_TODO = frozenset({"todo_write"})
_SKILL = frozenset({"list_available_skills", "skill"})
_MEMORY = frozenset({"memory_read", "memory_status", "memory_write"})
_BROWSE_MEMORY = frozenset({"memory_read"})
_MEMORY_INTENT = re.compile(
    r"memory_(?:read|write|status)|"
    r"九宫格|用户画像|记忆格|长期记忆|记忆状态|"
    r"打开记忆|写入记忆|读(?:一下)?记忆|"
    r"记住我|记住：|记一下我|"
    r"我的偏好|偏好是什么|简短回复偏好|"
    r"\bremember (?:that|this|my|i)\b|"
    r"\bsave (?:this |it )?(?:to |in )?(?:memory|prefs)\b|"
    r"\bread (?:the )?(?:memory|prefs|lattice)\b|"
    r"\bwhat do you remember\b|"
    r"你还记得|"
    r"\barchive/\w+|"
    r"管理记忆|修正记忆|改(?:正|掉)记忆|"
    r"记忆不对|记错了|记错|"
    r"删(?:除|掉).{0,16}记忆|记忆.{0,16}删|"
    r"清空.{0,12}(?:记忆|格子|这一格)|"
    r"忘掉这",
    re.IGNORECASE,
)
# 点了「全部删掉」这类确认后，上一句用户话才带「记忆」；prior_text 不含助手正文
_MEMORY_CONFIRM = re.compile(r"全部删掉|整格删掉|确认删除|就删掉|删了吧")
_SKILL_INTENT = re.compile(
    r"list_available_skills|有哪些技能|技能列表|可用技能|列出技能|"
    r"\blist(?: the)? skills\b|\bavailable skills\b",
    re.IGNORECASE,
)
_WEB_INTENT = re.compile(
    r"https?://|www\.|"
    r"\bweb_fetch\b|"
    r"抓取|打开网页|上网搜|联网查|网页|网址|"
    r"\bfetch (?:the |this )?(?:url|page|site)\b|"
    r"\b(?:search|look up) (?:the )?(?:web|online)\b|"
    r"\bopen (?:the )?(?:url|page|website)\b",
    re.IGNORECASE,
)


def needs_web_fetch(prompt: str) -> bool:
    return bool(_WEB_INTENT.search(prompt or ""))


def needs_memory_tools(prompt: str, prior_text: str = "") -> bool:
    if _MEMORY_INTENT.search(prompt or "") or _MEMORY_INTENT.search(prior_text or ""):
        return True
    if _MEMORY_CONFIRM.search(prompt or "") and "记忆" in (prior_text or ""):
        return True
    return False


def needs_skill_tools(prompt: str) -> bool:
    text = (prompt or "").strip()
    if not text:
        return False
    if _SKILL_INTENT.search(text):
        return True
    return bool(match_relevant_skills(text, min_score=4, limit=1))


def select_advertised_names(
    prompt: str,
    available: Sequence[str],
    *,
    plan_active: bool = False,
    used_names: Iterable[str] = (),
    prior_text: str = "",
    enabled: bool = True,
    memory_empty: dict[str, object] | None = None,
    memory_hits: Sequence[dict[str, object]] | None = None,
) -> list[str]:
    names = [str(item) for item in available if item]
    if not enabled:
        return names
    if is_chat_turn(prompt or ""):
        wanted = set()
    elif is_cheap_lookup(prompt or ""):
        wanted = set(_LOOKUP_TOOLS)
    else:
        wanted = set(CORE_TOOLS)
        # 任务轮要能弹窗问选择；闲聊/便宜读仍不挂，避免乱问。
        wanted.update(_ASK)
    haystack = "\n".join(part for part in (prior_text, prompt) if part)
    for pattern, group in _GROUPS:
        if pattern.search(haystack):
            wanted.update(group)
    if plan_active:
        wanted.update(_PLAN)
        if not is_idle_prompt(prompt or ""):
            wanted.update(_LOOKUP_TOOLS)
    decision = recommend(prompt or "")
    if decision.action == "fanout" and decision.ok:
        wanted.update(_SPAWN)
    if needs_choice(prompt or "") or needs_choice(prior_text):
        wanted.update(_ASK)
    if is_choice_only(prompt or ""):
        wanted.difference_update(MUTATING_TOOLS)
    if needs_web_fetch(prompt or "") or needs_web_fetch(prior_text):
        wanted.update(_WEB)
    if needs_todo(prompt or "") or needs_todo(prior_text):
        wanted.update(_TODO)
    if plan_active:
        if _SKILL_INTENT.search(prompt or "") or _SKILL_INTENT.search(prior_text):
            wanted.update(_SKILL)
    elif needs_skill_tools(prompt or "") or needs_skill_tools(prior_text):
        wanted.update(_SKILL)
    if needs_memory_tools(prompt or "", prior_text):
        wanted.update(_MEMORY)
    elif _MEMORY_CONFIRM.search(prompt or "") and any(str(name) in _MEMORY for name in used_names):
        wanted.update(_MEMORY)
    if needs_memory_browse(prompt or "", memory_empty):
        wanted.update(_BROWSE_MEMORY)
    if recalled_answer_hint(prompt or "", memory_hits) is not None:
        wanted.difference_update(_LOOKUP_TOOLS)
        wanted.difference_update(MUTATING_TOOLS)
    wanted.update(str(item) for item in used_names if item)
    if plan_active:
        wanted.difference_update(MUTATING_TOOLS)
    known = CORE_TOOLS.union(_ASK, _WEB, _TODO, _SKILL, _MEMORY, *(group for _pat, group in _GROUPS))
    advertised = [name for name in names if name in wanted or name not in known]
    logger.info("本轮公示工具 count=%s of=%s", len(advertised), len(names))
    return advertised
