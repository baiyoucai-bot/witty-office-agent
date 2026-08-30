"""计划模式是记入日志的协作状态，不是 PLAN.md。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from witty_agent.prompts import get_prompt
from witty_agent.session_log import SessionLog, fold_plan_mode
from witty_agent.types import AgentMessage

SetResult = Literal["committed", "queued", "cancelled", "noop"]

# Workspace side-effects. Research tools stay available; these are refused
# at the execution boundary while plan mode is on (prompt-only is not enough).
MUTATING_TOOLS = frozenset(
    {
        "apply_patch",
        "bash",
        "edit",
        "exec_command",
        "input_command",
        "input_subagent",
        "job_kill",
        "python_repl",
        "run_fanout",
        "run_subagent",
        "schedule_write",
        "schedule_delete",
        "write",
        "mail_send",
        "mail_draft",
        "mail_attach",
        "mail_save",
        "mail_reply",
        "pptx_create",
        "pptx_add_slide",
        "pptx_edit_slide",
        "pptx_add_picture",
        "pptx_render",
        "pptx_from_html",
        "pptx_replace_slide",
        "pptx_edit_box",
        "pptx_add_page",
        "wiki_init",
        "wiki_add",
        "wiki_remove",
    }
)


def blocks_tool(name: str, *, active: bool) -> bool:
    return bool(active) and (name or "") in MUTATING_TOOLS


_CHAT = re.compile(
    r"^(?:你好|在吗|谢谢|好的|嗯+|哦+|哈+|早上好|晚上好|"
    r"hi|hello|hey|yo|ok|okay|thanks|thank you|say hi)"
    r"(?:\s*[!！.。]*)?$",
    re.IGNORECASE,
)
_CHOICE = re.compile(
    r"\S{1,32}\s*(?:还是|或者)\s*\S{1,32}|"
    r"(?:选一个|选哪个|选哪一个|帮我选|你来选)|"
    r"\b(?:which one|pick one|choose one)\b|"
    r"\b[\w.-]{2,24}\s+or\s+[\w.-]{2,24}\s*[?？]",
    re.IGNORECASE,
)
_SCOPE_MUTATE = re.compile(
    r"\b(?:refactor|rewrite|migrate|overhaul)\b|"
    r"重构|重写|迁移|"
    r"整个.{0,16}(?:模块|目录|系统|项目|工作区)|"
    r"(?:模块|系统|工作区|项目).{0,16}(?:重构|重写|实现|改造)|"
    r"\bimplement\b.{0,48}\b(?:module|system|feature|across|auth|oauth)\b|"
    r"(?:实现|开发).{0,24}(?:模块|系统|功能)",
    re.IGNORECASE,
)
_MUTATE_VERB = re.compile(
    r"\b(?:write|edit|rewrite|refactor|implement|migrate|update)\b|"
    r"写|改|重构|重写|实现|迁移|更新",
    re.IGNORECASE,
)
_PATH_TOKEN = re.compile(
    r"(?:[./~\w-]+/)+[\w.-]+|"
    r"[\w.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|toml|md|json|yml|yaml)",
    re.IGNORECASE,
)


def needs_plan(prompt: str) -> bool:
    """Large workspace mutations should plan first; single-file writes should not."""
    text = (prompt or "").strip()
    if len(text) < 8 or _CHAT.match(text) or text.startswith("/"):
        return False
    if _CHOICE.search(text):
        return False
    if _SCOPE_MUTATE.search(text):
        return True
    paths = {item.group(0) for item in _PATH_TOKEN.finditer(text)}
    return len(paths) >= 2 and bool(_MUTATE_VERB.search(text))


def maybe_auto_enter(
    controller: PlanModeController,
    log: SessionLog | None,
    prompt: str,
    *,
    enabled: bool = True,
) -> AgentMessage | None:
    if not enabled or controller.get(log).active or not needs_plan(prompt):
        return None
    controller.set(log, True, narrate=False)
    return AgentMessage(
        role="user",
        content=get_prompt("plan_auto_enter"),
        source="plugin:auto-plan",
    )


@dataclass
class PlanState:
    active: bool
    pending: bool = False


@dataclass
class _Pending:
    active: bool
    narrate: bool


class PlanModeController:
    """生效状态始终是会话日志的纯折叠；选择可先挂起，等下一步再追加。"""

    def __init__(self) -> None:
        self._pending: _Pending | None = None

    def get(self, log: SessionLog | None) -> PlanState:
        logged = fold_plan_mode(log.events) if log is not None else False
        if self._pending is not None:
            return PlanState(active=self._pending.active, pending=True)
        return PlanState(active=logged, pending=False)

    def set(
        self,
        log: SessionLog | None,
        active: bool,
        *,
        narrate: bool = True,
    ) -> SetResult:
        current = self.get(log)
        if current.pending and self._pending is not None:
            if self._pending.active == active:
                return "noop"
            logged = fold_plan_mode(log.events) if log is not None else False
            if logged == active:
                self._pending = None
                return "cancelled"
        elif not current.pending and current.active == active:
            return "noop"
        if log is None or not log.has_open_turn():
            if log is not None:
                log.append("plan/mode", {"active": active})
            self._pending = None
            return "committed"
        self._pending = _Pending(active=active, narrate=narrate)
        return "queued"

    def apply_pre_step(self, log: SessionLog | None) -> list[AgentMessage]:
        if self._pending is None or log is None:
            return []
        pending = self._pending
        log.append("plan/mode", {"active": pending.active})
        self._pending = None
        if not pending.narrate:
            return []
        key = "plan_mode_on" if pending.active else "plan_mode_off"
        return [
            AgentMessage(
                role="user",
                content=get_prompt(key),
                source="plugin:plan-mode",
            )
        ]

    def policy_text(self, log: SessionLog | None) -> str:
        return get_prompt("plan_policy") if self.get(log).active else ""


def first_heading(plan: str) -> str | None:
    for line in plan.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return None


_STEP = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")


def plan_steps(plan: str, *, limit: int = 8) -> list[str]:
    """Bullet / numbered lines from an approved markdown plan (not the heading)."""
    steps: list[str] = []
    seen: set[str] = set()
    for line in (plan or "").splitlines():
        match = _STEP.match(line)
        if not match:
            continue
        text = match.group(1).strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        steps.append(text)
        if len(steps) >= limit:
            break
    return steps
