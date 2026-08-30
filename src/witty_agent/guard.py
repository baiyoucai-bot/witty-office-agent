"""工具守卫：工具超时替换结果；重复调用只提醒不否决。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from witty_agent.dispatch import is_chat_turn
from witty_agent.memory import hits_have_scopes, hits_layer
from witty_agent.plan_mode import first_heading
from witty_agent.prompts import get_prompt
from witty_agent.runtime import loop_settings
from witty_agent.types import AgentMessage, ToolCallBlock


def timeout_result_text(timeout_ms: int) -> str:
    return get_prompt("tool_timeout", timeout_ms=str(timeout_ms))


def canonicalize(arguments: object) -> str:
    return json.dumps(_sort(arguments), ensure_ascii=False, separators=(",", ":"))


def _sort(value: object) -> object:
    if isinstance(value, list):
        return [_sort(item) for item in value]
    if isinstance(value, dict):
        return {key: _sort(value[key]) for key in sorted(value)}
    return value


def _wildcard(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.compile(f"^{escaped}$")


def changes_state(name: str) -> bool:
    """这一步会不会改变别的调用「上次结果还作数」这件事。

    判据取 `loop.READONLY_TOOLS` 的补集，不另立一张表：只读工具按定义不会让别人的结果过期，
    其余（写、edit、bash、memory_write、插件工具、MCP 工具……）一律当成改了世界。名字不认识
    就算改了世界是**保守**方向——保守 = 少停轮，多让重复过去；反过来会误停正在推进的活。

    在函数体里 import：`loop` 模块级 import 了 `guard`（`timeout_result_text`），倒过来
    在模块级 import 就成环。调用时 `loop` 早已装好，只是一次字典查找。
    """
    from witty_agent.loop import READONLY_TOOLS

    return (name or "") not in READONLY_TOOLS


# 一轮里最多记这么多把 key 的计数，按最近用到的留（LRU）。长活一轮能几百次调用，
# 不设上限就是无界增长。挤掉旧的方向是**漏挡**（安全方向），不会凭空多挡。
_KEY_WINDOW = 64


@dataclass
class RepeatToolReminder:
    """同一 agent 重复同一次调用的计数；命中阈值提醒，到停点结束循环。

    「重复」= 同一把 key 又来一次，而**中间没有任何能改变它结果的调用**。只数「连续完全
    相同」是不够的：A-B-A-B 交替打转、被一次只读调用隔开的重试，计数永远停在 1，一次都不
    触发（真判据实测：调参集 8 条转圈里漏 7 条，留出集 7 条全漏）。只读调用插在中间不能
    为重复开脱——它没改变任何东西；`edit` / `bash` 这种插在中间就能，所以「改一处跑一次
    测试」来回几轮不会被当成转圈。
    """

    thresholds: list[int] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    preview_chars: int = 500
    stop_at: int | None = None
    workspace: str = ""
    _counts: dict[str, int] = field(default_factory=dict)
    _count: int = 0
    _name: str = ""

    def __post_init__(self) -> None:
        if not self.thresholds:
            raw = loop_settings().get("repeat_thresholds") or [3, 5, 8]
            self.thresholds = [int(item) for item in raw]
        self.thresholds = sorted(self.thresholds)
        self._include = [_wildcard(item) for item in self.include]
        self._exclude = [_wildcard(item) for item in self.exclude]
        self._threshold_set = set(self.thresholds)
        if self.stop_at is None:
            configured = loop_settings().get("repeat_stop")
            if configured is None:
                self.stop_at = self.thresholds[-1] if self.thresholds else 0
            else:
                self.stop_at = int(configured)

    def reset(self) -> None:
        self._counts = {}
        self._count = 0
        self._name = ""

    def observe(self, name: str, arguments: object) -> AgentMessage | None:
        if not self._tracked(name):
            return None
        key = self.fingerprint(name, arguments)
        if changes_state(name):
            # 世界变了：别人的「上次结果还作数」不再成立，计数作废。自己那把留着——
            # 同一条命令连着跑两遍、中间什么都没改，那本身就是在转圈。
            self._counts = {key: self._counts.get(key, 0)}
        count = self._counts.pop(key, 0) + 1
        self._counts[key] = count  # pop 再塞：把这把 key 挪到末尾，淘汰时先丢最久没用的
        while len(self._counts) > _KEY_WINDOW:
            self._counts.pop(next(iter(self._counts)))
        self._count = count
        self._name = name
        if self._count not in self._threshold_set:
            return None
        if self._count == self.thresholds[0]:
            text = get_prompt("repeat_gentle")
        else:
            preview = canonicalize(arguments)
            if len(preview) > self.preview_chars:
                preview = f"{preview[: self.preview_chars]}… (+{len(preview) - self.preview_chars} more chars)"
            text = get_prompt(
                "repeat_detailed",
                tool_name=name,
                count=str(self._count),
                arguments=preview,
            )
        return AgentMessage(role="user", content=text, source="plugin:repeat-tool-reminder")

    def fingerprint(self, name: str, arguments: object) -> str:
        """同一次调用的指纹。路径参数按工具真去看的那个文件算，不按模型的拼法算。

        `a.py` / `./a.py` / 绝对写法 / `dir/` 是同一次调用，模型换个拼法重试不该重新计数。
        走 `sandbox.fingerprint_target`（证伪账本盯证据用的同一个函数），两处对「哪个文件」
        的看法必须一致。除 `path` 之外的参数原样参与——`offset` 递进是翻页，不是重复。
        """
        if isinstance(arguments, dict):
            raw = arguments.get("path")
            if isinstance(raw, str) and raw.strip():
                arguments = {**arguments, "path": self._canonical_path(raw)}
        return json.dumps([name, canonicalize(arguments)], ensure_ascii=False)

    def _canonical_path(self, raw: str) -> str:
        text = raw.strip()
        if not self.workspace:
            # 没有工作区可谈（单元测试、非会话调用点）：只做纯文本归一。
            # 先并掉重复斜杠再剥 `./`，不能反过来：`.//a` 先剥前缀会剩下 `/a`，
            # 一个凭空冒出来的绝对路径，跟 `a` 再也对不上（方向是漏挡，但仍是错的）。
            cleaned = re.sub(r"/{2,}", "/", text.replace("\\", "/"))
            while cleaned.startswith("./"):
                cleaned = cleaned[2:]
            return cleaned.rstrip("/") or "/"
        from witty_agent.sandbox import fingerprint_target

        return str(fingerprint_target(self.workspace, text))

    def stop_notice(self) -> AgentMessage | None:
        if not self.stop_at or self._count < self.stop_at or not self._name:
            return None
        return AgentMessage(
            role="assistant",
            content=get_prompt(
                "repeat_stop",
                tool_name=self._name,
                count=str(self._count),
            ),
            stop_reason="end_turn",
            source="plugin:repeat-tool-stop",
        )

    def _tracked(self, name: str) -> bool:
        if self._include and not any(item.match(name) for item in self._include):
            return False
        return not any(item.match(name) for item in self._exclude)


@dataclass
class ProgressGuard:
    """Consecutive all-error tool turns stop the loop; 0 disables."""

    stall_limit: int | None = None
    _errors: int = 0

    def __post_init__(self) -> None:
        if self.stall_limit is None:
            self.stall_limit = int(loop_settings().get("stall_limit") or 3)

    def reset(self) -> None:
        self._errors = 0

    def observe_turn(
        self, assistant: AgentMessage, new_messages: list[AgentMessage]
    ) -> AgentMessage | None:
        if not self.stall_limit or self.stall_limit <= 0:
            return None
        calls = assistant.tool_calls()
        if not calls:
            self._errors = 0
            return None
        results = [item for item in new_messages if item.role == "toolResult"]
        tail = results[-len(calls) :] if results else []
        if tail and len(tail) == len(calls) and all(item.is_error for item in tail):
            self._errors += 1
        else:
            self._errors = 0
            return None
        if self._errors < self.stall_limit:
            return None
        return AgentMessage(
            role="assistant",
            content=get_prompt("stall_stop", count=str(self._errors)),
            stop_reason="end_turn",
            source="plugin:progress-guard",
        )


@dataclass
class FailStrategyReminder:
    """One hint after the first failed tool so the model changes approach before stall-stop."""

    enabled: bool | None = None
    _fired: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(loop_settings().get("fail_strategy", True))

    def reset(self) -> None:
        self._fired = False

    def observe(self, name: str, result: AgentMessage) -> AgentMessage | None:
        if not self.enabled or self._fired:
            return None
        tool = name or result.tool_name or "tool"
        if getattr(result, "is_error", False):
            text = result.text() or ""
            if "exit_plan_mode" in text:
                return None
            self._fired = True
            key = (
                "fail_strategy_sandbox"
                if "[sandbox: file access denied" in text
                else "fail_strategy"
            )
            return AgentMessage(
                role="user",
                content=get_prompt(key, tool_name=tool),
                source="plugin:fail-strategy",
            )
        if not is_empty_lookup(tool, result.text()):
            return None
        self._fired = True
        return AgentMessage(
            role="user",
            content=get_prompt("empty_lookup", tool_name=tool),
            source="plugin:fail-strategy",
        )


_ANSWERABLE = frozenset(
    {
        "find",
        "grep",
        "ls",
        "memory_read",
        "read",
        "session_query",
        "web_fetch",
    }
)
_PATHISH_TOKEN = re.compile(
    r"(?:[./~][\w./\\-]+)|(?:[\w-]+\.[A-Za-z0-9]{1,8})|"
    r"\b(?:README|LICENSE|TODO|CONTRIBUTING|NOTICE|CHANGELOG|AGENTS)\b",
    re.IGNORECASE,
)


def _multi_path_prompt(text: str) -> bool:
    names = {match.group(0).casefold() for match in _PATHISH_TOKEN.finditer(text or "")}
    return len(names) >= 2


@dataclass
class AnswerNowReminder:
    """One hint after the first usable lookup on a simple fact question."""

    enabled: bool | None = None
    _fired: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(loop_settings().get("answer_now", True))

    def reset(self) -> None:
        self._fired = False

    def observe(self, name: str, result: AgentMessage, *, prompt: str) -> AgentMessage | None:
        if not self.enabled or self._fired:
            return None
        text = (prompt or "").strip()
        if not needs_evidence(text) or needs_todo(text) or needs_choice(text):
            return None
        if _multi_path_prompt(text):
            return None
        tool = name or result.tool_name or "tool"
        if tool not in _ANSWERABLE or not is_substantive_tool_result(result):
            return None
        self._fired = True
        return AgentMessage(
            role="user",
            content=get_prompt("answer_now", tool_name=tool),
            source="plugin:answer-now",
        )


def excerpt_paths(*texts: str, limit: int = 4) -> list[str]:
    """File-like tokens from Recalled / evidence excerpts (not memory slugs)."""
    seen: set[str] = set()
    paths: list[str] = []
    for text in texts:
        for match in _PATHISH_TOKEN.finditer(text or ""):
            token = match.group(0)
            key = token.casefold()
            if key in seen:
                continue
            seen.add(key)
            paths.append(token)
            if len(paths) >= limit:
                return paths
    return paths


def recalled_answer_hint(
    prompt: str,
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
    *,
    enabled: bool | None = None,
) -> AgentMessage | None:
    """First-turn hint: Recalled already covers this fact question; do not probe files."""
    if enabled is None:
        enabled = bool(loop_settings().get("recalled_answer", True))
    if not enabled:
        return None
    text = (prompt or "").strip()
    if not needs_evidence(text) or needs_todo(text) or needs_choice(text):
        return None
    if _FILE_ASK.search(text) or _multi_path_prompt(text):
        return None
    cover_min = int(loop_settings().get("recalled_cover_min") or 5)
    slugs: list[str] = []
    seen: set[str] = set()
    best = 0
    for hit in hits or ():
        try:
            best = max(best, int(hit.get("score") or 0))
        except (TypeError, ValueError):
            pass
        slug = str(hit.get("slug") or hit.get("id") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
        if len(slugs) >= 6:
            break
    if not slugs or best < cover_min:
        return None
    layer = hits_layer(hits)
    if layer == "archive":
        key = "recalled_answer_archive"
    elif layer == "mixed":
        key = "recalled_answer_mixed"
    elif hits_have_scopes(hits):
        key = "recalled_answer_scopes"
    else:
        key = "recalled_answer"
    return AgentMessage(
        role="user",
        content=get_prompt(key, count=str(len(slugs)), slugs=", ".join(slugs)),
        source="plugin:recalled-answer",
    )


def _hits_are_archive(
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
) -> bool:
    return hits_layer(hits) == "archive"


def recalled_verify_paths(
    prompt: str,
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
    *,
    enabled: bool | None = None,
) -> list[str]:
    """Weak Recalled excerpt paths that should be verified by read."""
    if enabled is None:
        enabled = bool(loop_settings().get("recalled_answer", True))
    if not enabled:
        return []
    text = (prompt or "").strip()
    if is_chat_turn(text) or needs_todo(text) or needs_choice(text):
        return []
    if _FILE_ASK.search(text) or _multi_path_prompt(text):
        return []
    cover_min = int(loop_settings().get("recalled_cover_min") or 5)
    best = 0
    blobs: list[str] = []
    for hit in hits or ():
        try:
            best = max(best, int(hit.get("score") or 0))
        except (TypeError, ValueError):
            pass
        blobs.append(str(hit.get("text") or hit.get("excerpt") or ""))
    if best <= 0 or best >= cover_min:
        return []
    return excerpt_paths(*blobs)


def recalled_verify_hint(
    prompt: str,
    hits: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
    *,
    enabled: bool | None = None,
) -> AgentMessage | None:
    """Weak Recalled that names a file: read that path; do not treat the note as source."""
    paths = recalled_verify_paths(prompt, hits, enabled=enabled)
    if not paths:
        return None
    key = "recalled_verify_batch" if len(paths) >= 2 else "recalled_verify"
    return AgentMessage(
        role="user",
        content=get_prompt(key, count=str(len(paths)), paths=", ".join(paths)),
        source="plugin:recalled-verify",
    )


def autoload_recalled_verify(
    paths: list[str],
    *,
    enabled: bool | None = None,
) -> list[AgentMessage]:
    """Read weak-Recalled paths (parallel when 2+) and inject tool results."""
    if enabled is None:
        enabled = bool(loop_settings().get("recalled_verify_auto", True))
    rows = [str(item).strip() for item in paths if str(item).strip()]
    if not enabled or not rows:
        return []
    from concurrent.futures import ThreadPoolExecutor

    from witty_agent.tools.fs import read

    def _one(path: str) -> tuple[str, str, bool]:
        try:
            return path, read(path), False
        except Exception as exc:
            return path, str(exc), True

    if len(rows) == 1:
        loaded = [_one(rows[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(rows))) as pool:
            loaded = list(pool.map(_one, rows))
    located = _locate_failed([path for path, _text, failed in loaded if failed])
    tried = {path for path, _text, _failed in loaded}
    follow: list[tuple[str, str, bool]] = []
    for _pattern, text, failed in located:
        if failed or is_empty_lookup("find", text):
            continue
        found = _unique_find_file(text)
        if not found or found in tried:
            continue
        tried.add(found)
        follow.append(_one(found))
    calls: list[ToolCallBlock] = []
    results: list[AgentMessage] = []
    for index, (path, text, failed) in enumerate(loaded, start=1):
        call_id = f"verify-{index}"
        calls.append(ToolCallBlock(id=call_id, name="read", arguments={"path": path}))
        results.append(
            AgentMessage(
                role="toolResult",
                content=text,
                tool_call_id=call_id,
                tool_name="read",
                is_error=failed,
            )
        )
    for index, (pattern, text, failed) in enumerate(located, start=1):
        call_id = f"verify-find-{index}"
        calls.append(ToolCallBlock(id=call_id, name="find", arguments={"pattern": pattern, "limit": 20}))
        results.append(
            AgentMessage(
                role="toolResult",
                content=text,
                tool_call_id=call_id,
                tool_name="find",
                is_error=failed,
            )
        )
    for index, (path, text, failed) in enumerate(follow, start=1):
        call_id = f"verify-found-{index}"
        calls.append(ToolCallBlock(id=call_id, name="read", arguments={"path": path}))
        results.append(
            AgentMessage(
                role="toolResult",
                content=text,
                tool_call_id=call_id,
                tool_name="read",
                is_error=failed,
            )
        )
    assistant = AgentMessage(
        role="assistant",
        content=calls,
        stop_reason="toolUse",
        source="plugin:recalled-verify-read",
    )
    note = AgentMessage(
        role="user",
        content=_recalled_verify_note(loaded, located, follow),
        source="plugin:recalled-verify",
    )
    return [assistant, *results, note]


def _locate_failed(paths: list[str]) -> list[tuple[str, str, bool]]:
    """One find per failed basename so the model has a next path."""
    from pathlib import PurePosixPath

    from witty_agent.tools.search import find as find_files

    seen: set[str] = set()
    rows: list[tuple[str, str, bool]] = []
    for raw in paths:
        name = PurePosixPath(str(raw).replace("\\", "/")).name.strip()
        if not name or name in {".", ".."} or name in seen:
            continue
        seen.add(name)
        try:
            text = find_files(name, limit=20)
            failed = False
        except Exception as exc:
            text = str(exc)
            failed = True
        rows.append((name, text, failed))
        if len(rows) >= 3:
            break
    return rows


def _unique_find_file(text: str) -> str:
    """Return a single file path from a find listing; empty if 0 or 2+ files."""
    files: list[str] = []
    for line in str(text or "").splitlines():
        item = line.strip()
        if not item or item.startswith("(") or item.startswith("[") or item.endswith("/"):
            continue
        files.append(item)
    if len(files) != 1:
        return ""
    return files[0]


def recalled_relocations(
    messages: list[AgentMessage] | tuple[AgentMessage, ...] | None,
) -> list[tuple[str, str]]:
    """Pair a failed verify read with the unique follow-up path that succeeded."""
    from pathlib import PurePosixPath

    calls: dict[str, ToolCallBlock] = {}
    for item in messages or ():
        if item.role != "assistant":
            continue
        for call in item.tool_calls():
            if call.id:
                calls[str(call.id)] = call
    failed: dict[str, str] = {}
    found: dict[str, str] = {}
    for item in messages or ():
        if item.role != "toolResult" or (item.tool_name or "") != "read":
            continue
        call = calls.get(str(item.tool_call_id or ""))
        if call is None:
            continue
        path = str((call.arguments or {}).get("path") or "").strip()
        if not path:
            continue
        name = PurePosixPath(path.replace("\\", "/")).name
        if not name:
            continue
        if item.is_error:
            failed[name] = path
        elif str(item.tool_call_id or "").startswith("verify-found-"):
            found[name] = path
    pairs: list[tuple[str, str]] = []
    for name, dest in found.items():
        src = failed.get(name)
        if src and src != dest:
            pairs.append((src, dest))
    return pairs


def _recalled_verify_note(
    loaded: list[tuple[str, str, bool]],
    located: list[tuple[str, str, bool]] | None = None,
    follow: list[tuple[str, str, bool]] | None = None,
) -> str:
    ok = [path for path, _text, failed in loaded if not failed]
    bad = [path for path, _text, failed in loaded if failed]
    relocated = [path for path, _text, failed in (follow or []) if not failed]
    if relocated:
        return get_prompt(
            "recalled_verify_relocated",
            bad_paths=", ".join(bad) or "(none)",
            found=", ".join(relocated),
        )
    hits = [
        pattern
        for pattern, text, failed in (located or [])
        if not failed and not is_empty_lookup("find", text)
    ]
    if hits:
        return get_prompt(
            "recalled_verify_located",
            bad_paths=", ".join(bad) or "(none)",
            patterns=", ".join(hits),
        )
    if not bad:
        return get_prompt("recalled_verify_loaded", count=str(len(ok)), paths=", ".join(ok))
    if not ok:
        return get_prompt("recalled_verify_missed", count=str(len(bad)), paths=", ".join(bad))
    return get_prompt(
        "recalled_verify_partial",
        ok_count=str(len(ok)),
        ok_paths=", ".join(ok),
        bad_count=str(len(bad)),
        bad_paths=", ".join(bad),
    )


_SEEK = re.compile(
    r"文件|路径|prefs|偏好|记忆|九宫格|画像|条款|"
    r"哪些|是否|多少|为何|为什么|哪里|哪份|哪一|"
    r"\b(why|where)\b|"
    r"memory_|\bremember\b",
    re.IGNORECASE,
)
_FILE_ASK = re.compile(
    r"(?:文件|路径).{0,16}(?:什么|哪些|多少|内容|写了)|"
    r"(?:what|which).{0,32}(?:file|path)|"
    r"(?:read|open|查看|读取|列出).{0,40}\.(?:md|py|toml|json|txt|csv)\b|"
    r"\.(?:md|py|toml|json|txt|csv)\b.{0,20}(?:什么|内容|contain|say)",
    re.IGNORECASE,
)
_WRITE_TASK = re.compile(
    r"^(?:请)?(?:帮我)?(?:写|生成|创建|实现|改写)|"
    r"\b(?:write|create|implement|generate)\b",
    re.IGNORECASE,
)
_UNVERIFIED = re.compile(
    r"未核实|无证据|没有依据|unverified|no (?:tool )?evidence|i don'?t know",
    re.IGNORECASE,
)
_CHAT = re.compile(
    r"^(你好|在吗|谢谢|好的|嗯+|哦+|哈+|ok|okay|thanks|thank you)\s*[!！.。]*$",
    re.IGNORECASE,
)
_CHOICE = re.compile(
    r"\S{1,40}\s*(?:还是|或者)\s*\S{1,40}|"
    r"(?:选一个|选哪个|选哪一个|选哪种|帮我选|你来选|请选择|请选一个)|"
    r"(?:用哪[个种套份]|选哪[个种套份]|哪一种)|"
    r"\b(?:which one|pick one|choose one|please choose|"
    r"which (?:option|template|style|format|theme))\b|"
    r"\b[\w.-]{2,24}\s+or\s+[\w.-]{2,24}\s*[?？]",
    re.IGNORECASE,
)
_ASKING = re.compile(
    r"[?？]|请(?:你)?(?:选择|选|确认)|你(?:想|要|希望)|吗|呢|"
    r"选一个|选哪个|请选择|"
    r"\b(?:which |would you|please (?:choose|pick))\b",
    re.IGNORECASE,
)
_OPTION_LIST = re.compile(
    r"(?:^|\n)\s*(?:[A-Da-d]|[1-4１-４])[.、)）]\s+\S.+\n"
    r"\s*(?:[A-Da-d]|[1-4１-４])[.、)）]\s+\S",
    re.MULTILINE,
)
_DECIDED = re.compile(
    r"建议用|我选|采用|就用|决定用|"
    r"\bI (?:recommend|suggest|will use|chose|picked)\b",
    re.IGNORECASE,
)
_MULTI_STEP = re.compile(
    r"然后|接着|先.{0,20}再|分步|逐步|"
    r"\band then\b|"
    r"并(?:出|写|改|分析|总结|报告|列出)|"
    r"(?:review|analyze|implement|refactor).{6,80}\b(?:and|then)\b.{4,}|"
    r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s+\S.+\n\s*(?:[-*]|\d+[.)])\s+\S",
    re.IGNORECASE | re.DOTALL,
)


def needs_evidence(prompt: str) -> bool:
    text = (prompt or "").strip()
    if len(text) < 4 or _CHAT.match(text) or text.startswith("/") or _WRITE_TASK.search(text):
        return False
    if needs_choice(text):
        return False
    return bool(_SEEK.search(text) or _FILE_ASK.search(text))


_ARCHIVE_CUE = re.compile(
    r"旧|以前|之前|上次|归档|archived?|\bpreviously\b|\blast time\b",
    re.IGNORECASE,
)


def needs_memory_browse(prompt: str, memory_empty: dict[str, object] | None = None) -> bool:
    """Recalled missed, and a named slug or archive cue actually matches the question."""
    if not needs_evidence(prompt) or _FILE_ASK.search(prompt or ""):
        return False
    return bool(relevant_browse_slugs(prompt, memory_empty))


def relevant_browse_rows(
    prompt: str,
    memory_empty: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    """Populate/archive rows the question can name; skip unrelated standing cells."""
    empty = memory_empty or {}
    if empty.get("reason") != "no_overlap":
        return []
    want_archive = bool(_ARCHIVE_CUE.search(prompt or ""))
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in [*(empty.get("populated") or []), *(empty.get("archive") or [])]:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        archived = slug.startswith("archive/") or item.get("kind") == "archive"
        if not _slug_overlaps(prompt, item) and not (archived and want_archive):
            continue
        seen.add(slug)
        scope = str(item.get("scope") or "").strip() or "user"
        if scope not in {"user", "workspace"}:
            scope = "user"
        rows.append({"slug": slug, "scope": scope})
        if len(rows) >= 8:
            break
    return rows


def relevant_browse_slugs(
    prompt: str,
    memory_empty: dict[str, object] | None = None,
) -> list[str]:
    """Populate/archive ids the question can name; skip unrelated standing cells."""
    return [row["slug"] for row in relevant_browse_rows(prompt, memory_empty)]


def autoload_browse_read(
    rows: list[dict[str, str]] | tuple[dict[str, str], ...] | None,
    *,
    enabled: bool | None = None,
    prompt: str = "",
) -> list[AgentMessage]:
    """Read overlapping memory slugs (parallel when 2+) when Recalled missed."""
    if enabled is None:
        enabled = bool(loop_settings().get("browse_read_auto", True))
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows or ():
        slug = str((row or {}).get("slug") or "").strip()
        scope = str((row or {}).get("scope") or "user").strip() or "user"
        if scope not in {"user", "workspace"}:
            scope = "user"
        key = f"{scope}:{slug}"
        if not slug or key in seen:
            continue
        seen.add(key)
        items.append((slug, scope))
        if len(items) >= 3:
            break
    if not enabled or not items:
        return []
    from witty_agent.tools.memory_tools import memory_read

    def _one(pair: tuple[str, str]) -> tuple[str, str, str, bool]:
        slug, scope = pair
        try:
            return slug, scope, memory_read(slug, scope=scope), False
        except Exception as exc:
            return slug, scope, str(exc), True

    if len(items) == 1:
        loaded = [_one(items[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(3, len(items))) as pool:
            loaded = list(pool.map(_one, items))
    statuses = _browse_status_after(loaded)
    tried = {f"{scope}:{slug}" for slug, scope, _text, _failed in loaded}
    follow_pairs: list[tuple[str, str]] = []
    for scope, text, failed in statuses:
        if failed:
            continue
        for slug in _status_follow_slugs(text, tried, prompt=prompt):
            key = f"{scope}:{slug}"
            if key in tried:
                continue
            tried.add(key)
            follow_pairs.append((slug, scope))
            if len(follow_pairs) >= 3:
                break
        if len(follow_pairs) >= 3:
            break
    if len(follow_pairs) == 1:
        follow = [_one(follow_pairs[0])]
    elif follow_pairs:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(3, len(follow_pairs))) as pool:
            follow = list(pool.map(_one, follow_pairs))
    else:
        follow = []
    calls: list[ToolCallBlock] = []
    results: list[AgentMessage] = []
    for index, (slug, scope, text, failed) in enumerate(loaded, start=1):
        call_id = f"browse-{index}"
        calls.append(ToolCallBlock(id=call_id, name="memory_read", arguments={"slug": slug, "scope": scope}))
        results.append(
            AgentMessage(
                role="toolResult",
                content=text,
                tool_call_id=call_id,
                tool_name="memory_read",
                is_error=failed,
            )
        )
    for index, (scope, text, failed) in enumerate(statuses, start=1):
        call_id = f"browse-status-{index}"
        calls.append(ToolCallBlock(id=call_id, name="memory_status", arguments={"scope": scope}))
        results.append(
            AgentMessage(
                role="toolResult",
                content=text,
                tool_call_id=call_id,
                tool_name="memory_status",
                is_error=failed,
            )
        )
    for index, (slug, scope, text, failed) in enumerate(follow, start=1):
        call_id = f"browse-found-{index}"
        calls.append(ToolCallBlock(id=call_id, name="memory_read", arguments={"slug": slug, "scope": scope}))
        results.append(
            AgentMessage(
                role="toolResult",
                content=text,
                tool_call_id=call_id,
                tool_name="memory_read",
                is_error=failed,
            )
        )
    assistant = AgentMessage(
        role="assistant",
        content=calls,
        stop_reason="toolUse",
        source="plugin:browse-read",
    )
    ok_follow = any(not failed for _s, _c, _t, failed in follow)
    note = AgentMessage(
        role="user",
        content=_browse_read_note(loaded, statuses, follow),
        source="plugin:browse-read"
        if ok_follow or any(not failed for _s, _c, _t, failed in loaded)
        else "plugin:evidence-gate",
    )
    return [assistant, *results, note]


def browse_read_hits(
    messages: list[AgentMessage] | tuple[AgentMessage, ...] | None,
) -> list[dict[str, object]]:
    """Successful browse-read slugs, so Recalled can show them as already loaded."""
    args_by_id: dict[str, dict[str, object]] = {}
    hits: list[dict[str, object]] = []
    seen: set[str] = set()
    for message in messages or ():
        for call in message.tool_calls():
            if call.name != "memory_read":
                continue
            args_by_id[call.id] = dict(call.arguments or {})
        if message.role != "toolResult" or message.tool_name != "memory_read" or message.is_error:
            continue
        args = args_by_id.get(message.tool_call_id or "", {})
        slug = str(args.get("slug") or "").strip()
        scope = str(args.get("scope") or "user").strip() or "user"
        if scope not in {"user", "workspace"}:
            scope = "user"
        key = f"{scope}:{slug}"
        if not slug or key in seen:
            continue
        seen.add(key)
        blob = " ".join((message.text() or "").split())
        if len(blob) > 160:
            blob = blob[:159] + "…"
        hits.append(
            {
                "slug": slug,
                "title": slug,
                "text": blob,
                "scope": scope,
                "loaded": True,
            }
        )
    return hits


def _browse_status_after(loaded: list[tuple[str, str, str, bool]]) -> list[tuple[str, str, bool]]:
    """One memory_status per failed scope so the model sees live slugs."""
    if not any(failed for _slug, _scope, _text, failed in loaded):
        return []
    from witty_agent.tools.memory_tools import memory_status

    seen: set[str] = set()
    rows: list[tuple[str, str, bool]] = []
    for _slug, scope, _text, failed in loaded:
        if not failed or scope in seen:
            continue
        seen.add(scope)
        try:
            rows.append((scope, memory_status(scope=scope), False))
        except Exception as exc:
            rows.append((scope, str(exc), True))
        if len(rows) >= 2:
            break
    return rows


_STATUS_CELL = re.compile(
    r"(?:-\s+(?P<title>.+?)\s+)?\(`(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)`\):\s*(?P<excerpt>.+)$"
)


def _status_follow_slugs(text: str, tried: set[str], *, prompt: str = "") -> list[str]:
    """Untried populated lattice slugs; with a prompt, only those that overlap it."""
    found: list[tuple[str, str]] = []
    for line in str(text or "").splitlines():
        match = _STATUS_CELL.search(line.strip())
        if not match:
            continue
        slug = match.group("slug")
        excerpt = (match.group("excerpt") or "").strip()
        title = (match.group("title") or "").strip()
        if not excerpt or excerpt in {"（空）", "(empty)", "—"}:
            continue
        if slug in {"profile", "timeline"}:
            continue
        if any(key.endswith(f":{slug}") or key == slug for key in tried):
            continue
        found.append((slug, title))
    if prompt.strip():
        return [
            slug
            for slug, title in found
            if _slug_overlaps(prompt, {"id": slug, "slug": slug, "title": title})
        ][:3]
    if len(found) != 1:
        return []
    return [found[0][0]]


def _unique_status_slug(text: str, tried: set[str]) -> str:
    """Return the only populated lattice slug that was not already read."""
    found = _status_follow_slugs(text, tried)
    return found[0] if found else ""


def _browse_read_note(
    loaded: list[tuple[str, str, str, bool]],
    statuses: list[tuple[str, str, bool]] | None = None,
    follow: list[tuple[str, str, str, bool]] | None = None,
) -> str:
    ok = [f"{slug} ({scope})" for slug, scope, _text, failed in loaded if not failed]
    bad = [f"{slug} ({scope})" for slug, scope, _text, failed in loaded if failed]
    found = [f"{slug} ({scope})" for slug, scope, _text, failed in (follow or []) if not failed]
    listed = [scope for scope, _text, failed in (statuses or []) if not failed]
    if found:
        if len(found) == 1:
            return get_prompt(
                "evidence_gate_found",
                bad_slugs=", ".join(bad) or "(none)",
                found=found[0],
            )
        return get_prompt(
            "evidence_gate_found_batch",
            bad_slugs=", ".join(bad) or "(none)",
            count=str(len(found)),
            found=", ".join(found),
        )
    if ok and not bad:
        if len(ok) == 1:
            slug, scope, _text, _failed = next(item for item in loaded if not item[3])
            return get_prompt("evidence_gate_loaded", slug=slug, scope=scope)
        return get_prompt("evidence_gate_loaded_batch", count=str(len(ok)), slugs=", ".join(ok))
    if ok and bad:
        return get_prompt(
            "evidence_gate_loaded_partial",
            ok_slugs=", ".join(ok),
            bad_slugs=", ".join(bad),
        )
    if listed:
        return get_prompt(
            "evidence_gate_status",
            bad_slugs=", ".join(bad) or "(none)",
            scopes=", ".join(listed),
        )
    return get_prompt("evidence_gate_browse", slugs=", ".join(bad) or "(none)")


def _slug_overlaps(prompt: str, item: dict[str, object]) -> bool:
    if item.get("overlap"):
        return True
    text = (prompt or "").casefold()
    if not text:
        return False
    return any(needle in text for needle in _slug_needles(item))


def _slug_needles(item: dict[str, object]) -> list[str]:
    slug = str(item.get("id") or item.get("slug") or "").strip()
    title = str(item.get("title") or "").strip()
    name = slug.rsplit("/", 1)[-1]
    needles: list[str] = []
    for raw in (slug, name, title):
        key = raw.casefold()
        if key and key not in needles:
            needles.append(key)
    for part in re.split(r"[\s·/,_\-]+", title):
        key = part.strip().casefold()
        if len(key) >= 2 and key not in needles:
            needles.append(key)
    compact = re.sub(r"[\s·/,_\-]+", "", title)
    if re.fullmatch(r"[\u4e00-\u9fff]{2,}", compact):
        tail = compact[-2:]
        if tail not in needles:
            needles.append(tail)
    return needles


def needs_choice(prompt: str) -> bool:
    text = (prompt or "").strip()
    if len(text) < 4 or _CHAT.match(text) or text.startswith("/"):
        return False
    return bool(_CHOICE.search(text))


def is_choice_only(prompt: str) -> bool:
    """纯选择问：本轮不要挂写工具。写任务里夹带选择仍要能写。"""
    text = (prompt or "").strip()
    return needs_choice(text) and not _WRITE_TASK.search(text)


def poses_choice(text: str) -> bool:
    """助手正文在让用户点选，而不是已经替用户定了。"""
    body = (text or "").strip()
    if len(body) < 4:
        return False
    if _DECIDED.search(body) and not _ASKING.search(body):
        return False
    if _CHOICE.search(body):
        return True
    if _OPTION_LIST.search(body) and _ASKING.search(body):
        return True
    return False


_OPT_LINE = re.compile(r"^\s*(?:[-*]|[0-9]+[.)、]|[A-Da-d][.、)])\s+(\S.+)$")
_OR_SPLIT = re.compile(r"\s*(?:还是|或者|\bor\b)\s*", re.IGNORECASE)


def _clean_option(text: str) -> str:
    label = re.sub(r"[?？。.\s]+$", "", (text or "").strip())
    label = re.sub(r"^(?:用|请|选择)\s*", "", label)
    return label.strip()


def questions_from_assistant_text(text: str) -> list:
    """从助手正文抽出可点选的题。抽不出选项时仍返回一问，让弹窗先出来。"""
    from witty_agent.user_questions import AskUserQuestionItem, AskUserQuestionOption

    body = (text or "").strip()
    if not body:
        return []
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    blocks: list[tuple[str, list[str]]] = []
    question = ""
    options: list[str] = []

    def flush() -> None:
        nonlocal question, options
        labels = [_clean_option(item) for item in options]
        labels = [item for item in labels if item]
        heading = question.strip()
        if not heading and not labels:
            return
        if not heading:
            heading = "请选择"
        if len(labels) < 2:
            labels = _or_options(heading) or labels
        blocks.append((heading, labels))
        question = ""
        options = []

    for line in lines:
        matched = _OPT_LINE.match(line)
        if matched:
            options.append(matched.group(1))
            continue
        if question or options:
            flush()
        question = line
    flush()
    if not blocks:
        heading = lines[0] if lines else body[:200]
        blocks.append((heading, _or_options(heading)))
    items: list[AskUserQuestionItem] = []
    for index, (heading, labels) in enumerate(blocks, start=1):
        items.append(
            AskUserQuestionItem(
                id=f"q{index}",
                question=heading,
                options=[AskUserQuestionOption(label=item) for item in labels],
            )
        )
    return items


def _or_options(question: str) -> list[str]:
    core = re.sub(r"[?？。.\s]+$", "", (question or "").strip())
    parts = [_clean_option(item) for item in _OR_SPLIT.split(core) if item.strip()]
    labels = [item for item in parts if item and item != core]
    return labels if len(labels) >= 2 else []


def needs_todo(prompt: str) -> bool:
    text = (prompt or "").strip()
    if len(text) < 8 or _CHAT.match(text) or text.startswith("/"):
        return False
    if needs_choice(text):
        return False
    return bool(_MULTI_STEP.search(text))


@dataclass
class EvidenceGate:
    """One nudge when the model ends a fact-seeking turn with no tool evidence."""

    enabled: bool | None = None
    _fired: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(loop_settings().get("evidence_gate", True))

    def reset(self) -> None:
        self._fired = False

    def maybe_nudge(
        self,
        messages: list[AgentMessage],
        *,
        has_memory: bool = False,
        memory_empty: dict[str, object] | None = None,
    ) -> AgentMessage | None:
        if not self.enabled or self._fired:
            return None
        if any(str(item.source or "") == "plugin:evidence-gate" for item in messages):
            return None
        if not _still_bare(messages, has_memory=has_memory):
            return None
        self._fired = True
        prompt = _last_real_user(messages)
        slugs = relevant_browse_slugs(prompt, memory_empty)
        if slugs:
            text = get_prompt("evidence_gate_browse", slugs=", ".join(slugs))
        else:
            text = get_prompt("evidence_gate")
        return AgentMessage(
            role="user",
            content=text,
            source="plugin:evidence-gate",
        )

    def maybe_seal(
        self,
        messages: list[AgentMessage],
        *,
        has_memory: bool = False,
    ) -> AgentMessage | None:
        """After a nudge, if the model still invented, close with an unverified notice."""
        if not self.enabled or not self._fired:
            return None
        if any(str(item.source or "") == "plugin:evidence-seal" for item in messages):
            return None
        if not _still_bare(messages, has_memory=has_memory):
            return None
        return AgentMessage(
            role="assistant",
            content=get_prompt("evidence_seal"),
            stop_reason="end_turn",
            source="plugin:evidence-seal",
        )


@dataclass
class AskGate:
    """One nudge when the model guesses a blocking A/B choice."""

    enabled: bool | None = None
    _fired: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(loop_settings().get("ask_gate", True))

    def reset(self) -> None:
        self._fired = False

    def maybe_nudge(self, messages: list[AgentMessage], *, has_memory: bool = False) -> AgentMessage | None:
        if not self.enabled or self._fired:
            return None
        if any(str(item.source or "").startswith("plugin:ask-gate") for item in messages):
            return None
        if _asked_user(messages):
            return None
        last = next((item for item in reversed(messages) if item.role == "assistant"), None)
        if last is None or last.tool_calls() or not last.text():
            return None
        if last.stop_reason in {"error", "aborted"}:
            return None
        if str(last.source or "").startswith("plugin:"):
            return None
        posed = poses_choice(last.text())
        prompt = _last_real_user(messages)
        if posed:
            # 选择题写进了正文，桌面不会弹窗；提醒改走工具。已用过工具也要拦。
            self._fired = True
            return AgentMessage(
                role="user",
                content=get_prompt("ask_gate_posed"),
                source="plugin:ask-gate",
            )
        if not needs_choice(prompt) or has_memory:
            return None
        self._fired = True
        return AgentMessage(
            role="user",
            content=get_prompt("ask_gate"),
            source="plugin:ask-gate",
        )


@dataclass
class TodoGate:
    """One nudge when a multi-step request ends with no todo list."""

    enabled: bool | None = None
    _fired: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(loop_settings().get("todo_gate", True))

    def reset(self) -> None:
        self._fired = False

    def maybe_nudge(
        self,
        messages: list[AgentMessage],
        *,
        has_todos: bool = False,
        plan_active: bool = False,
    ) -> AgentMessage | None:
        if not self.enabled or self._fired or plan_active:
            return None
        if any(str(item.source or "") == "plugin:todo-gate" for item in messages):
            return None
        if has_todos or _used_named_tool(messages, "todo_write"):
            return None
        # 待办提醒是为了「别空手开长活」，不是交付后补单据。这一轮真动过工具就放过：
        # 否则每个多步任务都要多烧一轮跟模型讨要清单，越用越黏。
        if _used_ok_tool(messages):
            return None
        prompt = _last_real_user(messages)
        if not needs_todo(prompt):
            return None
        last = next((item for item in reversed(messages) if item.role == "assistant"), None)
        if last is None or last.tool_calls() or not last.text():
            return None
        if last.stop_reason in {"error", "aborted"}:
            return None
        if str(last.source or "").startswith("plugin:"):
            return None
        self._fired = True
        return AgentMessage(
            role="user",
            content=get_prompt("todo_gate"),
            source="plugin:todo-gate",
        )


@dataclass
class PlanPresentGate:
    """One nudge when plan mode is on and the model dumps a plan without exit_plan_mode."""

    enabled: bool | None = None
    _fired: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(loop_settings().get("plan_gate", True))

    def reset(self) -> None:
        self._fired = False

    def maybe_nudge(
        self,
        messages: list[AgentMessage],
        *,
        plan_active: bool = False,
    ) -> AgentMessage | None:
        if not self.enabled or self._fired or not plan_active:
            return None
        if any(str(item.source or "") == "plugin:plan-present-gate" for item in messages):
            return None
        if _used_named_tool(messages, "exit_plan_mode"):
            return None
        last = next((item for item in reversed(messages) if item.role == "assistant"), None)
        if last is None or last.tool_calls() or not last.text():
            return None
        if last.stop_reason in {"error", "aborted"}:
            return None
        if str(last.source or "").startswith("plugin:"):
            return None
        if first_heading(last.text()) is None:
            return None
        self._fired = True
        return AgentMessage(
            role="user",
            content=get_prompt("plan_present_gate"),
            source="plugin:plan-present-gate",
        )


def _still_bare(messages: list[AgentMessage], *, has_memory: bool) -> bool:
    if has_memory or _used_ok_tool(messages) or _asked_user(messages) or _loaded_skill(messages):
        return False
    prompt = _last_real_user(messages)
    if not needs_evidence(prompt):
        return False
    last = next((item for item in reversed(messages) if item.role == "assistant"), None)
    if last is None or last.tool_calls() or not last.text():
        return False
    if last.stop_reason in {"error", "aborted"}:
        return False
    if _UNVERIFIED.search(last.text()):
        return False
    if str(last.source or "") == "plugin:evidence-seal":
        return False
    return True


def _last_real_user(messages: list[AgentMessage]) -> str:
    for item in reversed(messages):
        if item.role != "user":
            continue
        if str(item.source or "").startswith("plugin:"):
            continue
        return item.text()
    return ""


_META_TOOLS = frozenset(
    {
        "ask_user_question",
        "exit_plan_mode",
        "job_kill",
        "job_list",
        "job_output",
        "list_available_skills",
        "list_commands",
        "memory_status",
        "memory_write",
        "plan_read",
        "plan_write",
        "schedule_list",
        "schedule_write",
        "schedule_delete",
        "skill",
        "todo_write",
    }
)
_LOOKUP_TOOLS = frozenset({"find", "grep", "session_query"})
_EMPTY_LOOKUP = re.compile(
    r"^\((?:no matches|no hits|empty(?: fanout)?|the index is empty[^)]*)\)$",
    re.IGNORECASE,
)


def is_empty_lookup(name: str, text: str) -> bool:
    """True when a search-like tool returned a miss, not a readable excerpt."""
    compact = (text or "").strip()
    if _EMPTY_LOOKUP.match(compact):
        return True
    return not compact and (name or "") in _LOOKUP_TOOLS


def is_empty_lookup_text(text: str) -> bool:
    return bool(_EMPTY_LOOKUP.match((text or "").strip()))


def is_substantive_tool_result(item: AgentMessage) -> bool:
    """True when a tool result can support a user-facing claim."""
    if item.role != "toolResult" or item.is_error or not item.tool_name:
        return False
    if item.tool_name in _META_TOOLS:
        return False
    return not is_empty_lookup(item.tool_name, item.text())


def _used_ok_tool(messages: list[AgentMessage]) -> bool:
    start = 0
    for index, item in enumerate(messages):
        if item.role == "user" and not str(item.source or "").startswith("plugin:"):
            start = index
    return any(is_substantive_tool_result(item) for item in messages[start:])


def _used_named_tool(messages: list[AgentMessage], name: str) -> bool:
    start = 0
    for index, item in enumerate(messages):
        if item.role == "user" and not str(item.source or "").startswith("plugin:"):
            start = index
    for item in messages[start:]:
        if item.role == "toolResult" and item.tool_name == name:
            return True
        if any(call.name == name for call in item.tool_calls()):
            return True
    return False


def _loaded_skill(messages: list[AgentMessage]) -> bool:
    start = 0
    for index, item in enumerate(messages):
        if item.role == "user" and not str(item.source or "").startswith("plugin:"):
            start = index
    return any(
        str(item.source or "") == "plugin:skill-invocation" for item in messages[start:]
    )


def _asked_user(messages: list[AgentMessage]) -> bool:
    return any(
        call.name == "ask_user_question"
        for item in messages
        for call in item.tool_calls()
    )
