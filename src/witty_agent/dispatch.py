"""子代理分配：何时串行、何时 run_subagent、何时 run_fanout。

原则是「默认自己干」：子代理是隔离多步活，
不是便宜查询的默认路径。模型仍选工具；本模块在工具边界拒绝不合理拉起。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from witty_agent.prompts import get_prompt
from witty_agent.types import AgentMessage

Action = Literal["serial", "subagent", "fanout"]

_READ_VERB = re.compile(
    r"^(?:please\s+)?(?:just\s+)?(?:then\s+|again\s+)?(?:read|cat|head|tail|file|type)\s+(\S+)(?:\s+again)?[。.!！?？]*$",
    re.IGNORECASE,
)
_LIST_VERB = re.compile(
    r"^(?:please\s+)?(?:just\s+)?(?:then\s+|again\s+)?(?:ls|stat|grep|find)\s+(\S+)(?:\s+again)?[。.!！?？]*$",
    re.IGNORECASE,
)
_ZH_READ = re.compile(
    r"^(?:请)?(?:再|先|然后|接着|继续)?(?:请)?(?:读(?:取|一下)?|查看(?:文件)?|看一下)\s+(\S+)[。.!！?？]*$",
)
_ZH_LIST = re.compile(
    r"^(?:请)?(?:再|先|然后|接着|继续)?列出(?:目录|文件)?\s+(\S+)[。.!！?？]*$",
)
_BARE = re.compile(
    r"^(?:pwd|whoami|what time(?: is it)?|what(?:'s| is) the time|current time)[。.!！?？]*$",
    re.IGNORECASE,
)
_PATH_ONLY = re.compile(r"^(?:[./~]|[A-Za-z]:\\)[\w./\\-]+$")
_PATHISH = re.compile(r"[/\\]|^~|^\.|[.][A-Za-z0-9]{1,8}$")
_BARE_FILES = frozenset(
    {
        "agents",
        "authors",
        "changelog",
        "codeowners",
        "contributing",
        "copying",
        "credits",
        "dockerfile",
        "gemfile",
        "history",
        "install",
        "justfile",
        "license",
        "maintainers",
        "makefile",
        "news",
        "notice",
        "procfile",
        "rakefile",
        "readme",
        "security",
        "skill",
        "taskfile",
        "todo",
        "vagrantfile",
    }
)
_CAPS_FILE = re.compile(r"^[A-Z][A-Z0-9_-]{2,}$")
_ECHO_STOP = frozenset({"a", "an", "and", "for", "just", "of", "or", "please", "the", "to"})
_CHAT_ATOM = (
    r"thank you|say hi|got it|no problem|good morning|good night|good evening|"
    r"你好啊|你好呀|在不在|谢谢你|谢谢啦|谢谢了|明白了|知道了|没问题|辛苦了|"
    r"早上好|中午好|下午好|晚上好|再见|拜拜|多谢|感谢|好的|好吧|收到|明白|"
    r"你好|在吗|谢谢|可以|"
    r"hello|thanks|okay|sure|yeah|yup|cool|great|nice|goodbye|"
    r"hi|hey|yo|ok|np|bye|"
    r"嗯+|哦+|哈+"
)
_CHAT = re.compile(
    rf"^(?:{_CHAT_ATOM})(?:[\s,，、]*(?:{_CHAT_ATOM})){{0,2}}(?:\s*[!！.。?？]*)?$",
    re.IGNORECASE,
)
_LOOKUP_LEAD = re.compile(
    r"^(?:please\s+)?(?:just\s+)?(?:then\s+|again\s+)?"
    r"(?:read|cat|head|tail|file|type|ls|stat|grep|find)\s+",
    re.IGNORECASE,
)
_ZH_LOOKUP_LEAD = re.compile(
    r"^(?:请)?(?:再|先|然后|接着|继续)?(?:请)?"
    r"(?:读(?:取|一下)?|查看(?:文件)?|看一下|列出(?:目录|文件)?)\s+",
)
_SPLIT_PATHS = re.compile(
    r"\s*(?:,|;|、|和|以及|及|&|\band\b|\bor\b)\s+",
    re.IGNORECASE,
)
_TRAIL_PUNCT = re.compile(r"[。.!！?？]+$")
_TRAIL_AGAIN = re.compile(r"\s+again$", re.IGNORECASE)
_LOOKUP_PATH_LIGHT = re.compile(
    r"^(?:please\s+)?(?:just\s+)?(?:then\s+|again\s+)?"
    r"(?:read|cat|head|tail|file|type|ls|stat|grep|find)\s+(\S+)\s+"
    r"(?:and then|and|then|,)\s+(?:please\s+)?"
    r"(?:it\s+)?"
    r"(?:summarize|summary|explain|describe|outline|recap|print|"
    r"what(?:'s| is)?(?:\s+in)?(?:\s+it)?(?:\s+say)?|"
    r"show (?:me )?(?:the )?contents?)\b",
    re.IGNORECASE,
)
_ZH_LOOKUP_PATH_LIGHT = re.compile(
    r"^(?:请)?(?:再|先|然后|接着|继续)?(?:请)?"
    r"(?:读(?:取|一下)?|查看(?:文件)?|看一下|列出(?:目录|文件)?)\s+(\S+)\s*"
    r"(?:并|然后|再|，|,)\s*"
    r"(?:打印|摘要|总结|说明|概述|看看?内容)",
)


@dataclass(frozen=True)
class Allocation:
    action: Action
    ok: bool
    code: str
    tasks: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if self.ok:
            return ""
        reason = get_prompt(f"dispatch_reason_{self.code}")
        key = "dispatch_refuse_fanout" if self.code in {"too_few", "all_cheap"} else "dispatch_refuse_trivial"
        return get_prompt(key, reason=reason)


def parent_user_prompt() -> str | None:
    from witty_agent import hooks

    log = hooks.session_log
    if log is None:
        return None
    for event in reversed(getattr(log, "events", []) or []):
        if getattr(event, "type", "") != "user/message":
            continue
        data = getattr(event, "data", {}) or {}
        source = str(data.get("source") or "user")
        if source.startswith("plugin:"):
            continue
        text = str(data.get("text") or "").strip()
        if text:
            return text
    return None


def guard_spawn(prompt: str, *, parent_prompt: str | None = None) -> Allocation:
    """Refuse spawn when this turn was allocated cheap, or the child restates the parent."""
    parent = parent_prompt if parent_prompt is not None else parent_user_prompt()
    if parent and is_cheap_lookup(parent):
        return Allocation("serial", False, "stay_serial", ((prompt or "").strip(),) if (prompt or "").strip() else ())
    return assess_subagent(prompt, parent_prompt=parent)


def guard_fanout(prompts: Sequence[str], *, parent_prompt: str | None = None) -> Allocation:
    parent = parent_prompt if parent_prompt is not None else parent_user_prompt()
    if parent and is_cheap_lookup(parent):
        return Allocation("serial", False, "stay_serial")
    return assess_fanout(prompts, parent_prompt=parent)


def assess_subagent(prompt: str, *, parent_prompt: str | None = None) -> Allocation:
    text = (prompt or "").strip()
    if not text:
        return Allocation("serial", False, "empty")
    if is_cheap_lookup(text):
        return Allocation("serial", False, "cheap_lookup", (text,))
    if _echoes_parent(text, parent_prompt):
        return Allocation("serial", False, "echo_parent", (text,))
    return Allocation("subagent", True, "ok", (text,))


def assess_fanout(prompts: Sequence[str], *, parent_prompt: str | None = None) -> Allocation:
    kept: list[str] = []
    seen: set[str] = set()
    cheap = 0
    for raw in prompts:
        text = str(raw).strip()
        if not text:
            continue
        key = _norm(text)
        if key in seen:
            continue
        seen.add(key)
        one = assess_subagent(text, parent_prompt=parent_prompt)
        if not one.ok:
            cheap += 1
            continue
        kept.append(text)
    if len(kept) >= 2:
        return Allocation("fanout", True, "ok", tuple(kept))
    if cheap and not kept:
        return Allocation("serial", False, "all_cheap")
    return Allocation("serial", False, "too_few", tuple(kept))


def recommend(
    prompt: str,
    *,
    tasks: Sequence[str] | None = None,
    parent_prompt: str | None = None,
) -> Allocation:
    """默认串行。仅当已拆出 2+ 条独立非平凡任务时建议 fanout。"""
    items = [str(item).strip() for item in (tasks or ()) if str(item).strip()]
    if not items:
        items = _split_task_lines(prompt)
    if len(items) >= 2:
        fan = assess_fanout(items, parent_prompt=parent_prompt)
        if fan.ok:
            return fan
    return Allocation("serial", True, "ok", ((prompt or "").strip(),) if (prompt or "").strip() else ())


_SHARE = re.compile(
    r"^(?:我(?:爱|喜欢|想要|想|是|叫|在)|i (?:love|like|am|'m|want)\b).{0,48}$",
    re.IGNORECASE,
)
_SHARE_ASK = re.compile(r"谁|什么|哪些|是否|多少|为何|为什么|哪里|哪|吗|呢|[?？]")


def is_idle_prompt(prompt: str) -> bool:
    """True for empty or greeting-only turns (no task, no lookup)."""
    text = (prompt or "").strip()
    return not text or bool(_CHAT.match(text))


def is_share_prompt(prompt: str) -> bool:
    """True for a short first-person disclosure, not a question or task."""
    text = (prompt or "").strip()
    if not text or _SHARE_ASK.search(text):
        return False
    return bool(_SHARE.match(text))


def is_chat_turn(prompt: str) -> bool:
    """Greeting or a short self-statement; not a lookup or task."""
    return is_idle_prompt(prompt) or is_share_prompt(prompt)


def allocation_hint(prompt: str) -> AgentMessage | None:
    """One first-turn hint so the model sees this turn's allocation, not only the static guideline."""
    text = (prompt or "").strip()
    if is_chat_turn(text):
        return None
    decision = recommend(text)
    if decision.action == "fanout" and decision.ok:
        tasks = "\n".join(f"- {item}" for item in decision.tasks)
        body = get_prompt("dispatch_hint_fanout", count=str(len(decision.tasks)), tasks=tasks)
    elif paths := _batch_paths(text):
        body = get_prompt(
            "dispatch_hint_serial_batch",
            count=str(len(paths)),
            paths=", ".join(paths),
        )
    elif is_cheap_lookup(text):
        body = get_prompt("dispatch_hint_serial_cheap")
    else:
        body = get_prompt("dispatch_hint_serial")
    return AgentMessage(role="user", content=body, source="plugin:dispatch-hint")


def is_cheap_lookup(text: str) -> bool:
    compact = " ".join(text.split())
    if _PATH_ONLY.match(compact) or _BARE.match(compact):
        return True
    listed = _LIST_VERB.match(compact) or _ZH_LIST.match(compact)
    if listed:
        return True
    read = _READ_VERB.match(compact) or _ZH_READ.match(compact)
    if read:
        return _looks_like_path(read.group(1))
    light = _LOOKUP_PATH_LIGHT.match(compact) or _ZH_LOOKUP_PATH_LIGHT.match(compact)
    if light:
        return _looks_like_path(light.group(1))
    return bool(_batch_paths(compact))


def _batch_paths(text: str) -> list[str]:
    """Two or more path tokens after a lookup verb, with no leftover task words."""
    compact = _TRAIL_AGAIN.sub("", _TRAIL_PUNCT.sub("", " ".join((text or "").split())))
    lead = _LOOKUP_LEAD.match(compact) or _ZH_LOOKUP_LEAD.match(compact)
    if not lead:
        return []
    rest = compact[lead.end() :].strip()
    tokens = [part.strip().strip("'\"") for part in _SPLIT_PATHS.split(rest) if part.strip()]
    if len(tokens) == 1:
        tokens = [part for part in rest.split() if part.strip().strip("'\"")]
        tokens = [part.strip().strip("'\"") for part in tokens]
    if len(tokens) < 2:
        return []
    if all(_looks_like_path(part) for part in tokens):
        return tokens
    return []


def _looks_like_path(token: str) -> bool:
    name = token.strip().strip("'\"")
    if not name or any(ch.isspace() for ch in name):
        return False
    if _PATHISH.search(name):
        return True
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.casefold() in _BARE_FILES:
        return True
    return bool(_CAPS_FILE.match(stem))


def _echoes_parent(child: str, parent: str | None) -> bool:
    if not parent:
        return False
    if _norm(child) == _norm(parent):
        return True
    if len(_split_task_lines(parent)) >= 2:
        return False
    child_tokens = _content_tokens(child)
    parent_tokens = _content_tokens(parent)
    if not child_tokens or not parent_tokens:
        return False
    if child_tokens == parent_tokens:
        return True
    smaller, larger = (
        (child_tokens, parent_tokens)
        if len(child_tokens) <= len(parent_tokens)
        else (parent_tokens, child_tokens)
    )
    if len(smaller) >= 3 and smaller <= larger:
        return True
    if len(smaller) >= 3 and len(child_tokens & parent_tokens) / len(smaller) >= 0.7:
        return True
    return False


def _content_tokens(text: str) -> set[str]:
    return {word for word in _norm(text).split() if word not in _ECHO_STOP and len(word) > 1}


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _split_task_lines(raw: str) -> list[str]:
    return [line.strip(" -\t*•") for line in (raw or "").splitlines() if line.strip()]
