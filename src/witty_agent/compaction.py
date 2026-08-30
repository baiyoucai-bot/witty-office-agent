"""压缩：超窗就摘要，保留近期尾巴。"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.types import AgentMessage, ContentBlock, TextBlock, ToolCallBlock

logger = get_logger("compaction")

COMPACTION_CHECKPOINT_SOURCE = "plugin:compaction-checkpoint"
_RANGE = re.compile(r"^(\d+)\s*[-:]\s*(\d+)$")


class CompactionBusy(Exception):
    """压缩接纳已被占用。"""


class CompactionLock:
    """会话内一次只许一段压缩，空闲才接纳。"""

    def __init__(self) -> None:
        self._held = False
        self._guard = threading.Lock()

    @property
    def busy(self) -> bool:
        with self._guard:
            return self._held

    def acquire(self) -> None:
        with self._guard:
            if self._held:
                raise CompactionBusy("busy")
            self._held = True

    def release(self) -> None:
        with self._guard:
            self._held = False

    def __enter__(self) -> CompactionLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@dataclass
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000
    context_window: int = 128000
    use_model: bool = True
    tool_result_threshold: int = 8192
    tool_result_head: int = 4096
    tool_result_tail: int = 1024
    tool_call_arg_threshold: int = 8192
    tool_call_arg_head: int = 2048
    tool_call_arg_tail: int = 512
    clear_at_least_chars: int = 0
    prune_exclude_tools: tuple[str, ...] = ()


def settings_from_runtime(raw: dict | None = None) -> CompactionSettings:
    data = raw if isinstance(raw, dict) else {}
    excluded = data.get("prune_exclude_tools")
    return CompactionSettings(
        enabled=bool(data.get("enabled", True)),
        use_model=bool(data.get("use_model", True)),
        reserve_tokens=int(data.get("reserve_tokens") or 16384),
        keep_recent_tokens=int(data.get("keep_recent_tokens") or 20000),
        context_window=int(data.get("context_window") or 128000),
        tool_result_threshold=int(data.get("tool_result_threshold") or 8192),
        tool_result_head=int(data.get("tool_result_head") or 4096),
        tool_result_tail=int(data.get("tool_result_tail") or 1024),
        tool_call_arg_threshold=int(data.get("tool_call_arg_threshold") or 0),
        tool_call_arg_head=int(data.get("tool_call_arg_head") or 2048),
        tool_call_arg_tail=int(data.get("tool_call_arg_tail") or 512),
        clear_at_least_chars=int(data.get("clear_at_least_chars") or 0),
        prune_exclude_tools=tuple(str(item) for item in excluded) if isinstance(excluded, list) else (),
    )


def _prune_excluded(name: str | None, settings: CompactionSettings) -> bool:
    return (name or "") in settings.prune_exclude_tools


def prune_tool_result_text(text: str, settings: CompactionSettings | None = None) -> str | None:
    """Tool-result pruning: keep head + marker + tail. None when already in budget."""
    cfg = settings or CompactionSettings()
    if cfg.tool_result_threshold <= 0:
        return None
    body = text or ""
    if len(body) <= cfg.tool_result_threshold:
        return None
    marker = f"\n\n{get_prompt('tool_result_pruned')}\n\n"
    head = max(0, cfg.tool_result_head)
    tail = max(0, cfg.tool_result_tail)
    if head + tail >= len(body):
        return None
    pruned = f"{body[:head]}{marker}{body[-tail:] if tail else ''}"
    if len(pruned) >= len(body) or len(pruned) > cfg.tool_result_threshold:
        return None
    return pruned


def _live_tool_result_start(messages: list[AgentMessage]) -> int:
    """Index of the trailing toolResult run (current step). Do not prune those."""
    start = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role != "toolResult":
            break
        start = index
    return start


_FAILED_TOOL = re.compile(r"exit=(?!0\b)-?\d+|不存在|No such file|not found|file access denied", re.IGNORECASE)


def triage_messages(messages: list[AgentMessage]) -> list[AgentMessage]:
    """Drop stale chatter from the summary window: failed stubs, consecutive dupes."""
    out: list[AgentMessage] = []
    prev_text = ""
    for message in messages:
        if message.role != "toolResult":
            out.append(message)
            prev_text = ""
            continue
        text = message.text()
        if message.is_error or _FAILED_TOOL.search(text or ""):
            stub = get_prompt(
                "compaction_failed_stub",
                tool=message.tool_name or "tool",
                preview=(text or "").splitlines()[0][:180] if text else "",
            )
            out.append(replace(message, content=stub))
            prev_text = stub
            continue
        if text and text == prev_text:
            out.append(replace(message, content=get_prompt("compaction_dup_stub")))
            continue
        out.append(message)
        prev_text = text
    return out


def prune_tool_results(
    messages: list[AgentMessage],
    settings: CompactionSettings | None = None,
) -> list[AgentMessage]:
    """Rewrite older over-budget toolResult bodies. Latest step stays full."""
    cfg = settings or CompactionSettings()
    if cfg.tool_result_threshold <= 0:
        return messages
    live_from = _live_tool_result_start(messages)
    saved = 0
    out: list[AgentMessage] = []
    for index, message in enumerate(messages):
        if message.role != "toolResult" or index >= live_from or _prune_excluded(message.tool_name, cfg):
            out.append(message)
            continue
        body = message.text()
        pruned = prune_tool_result_text(body, cfg)
        if pruned is None:
            out.append(message)
            continue
        saved += len(body) - len(pruned)
        out.append(replace(message, content=pruned))
    return out if _worth_clearing(saved, cfg) else messages


def _worth_clearing(saved_chars: int, settings: CompactionSettings) -> bool:
    """Anthropic clear_at_least: skip a rewrite that frees too little to be worth it.

    Every rewrite invalidates the provider's prompt cache from that message onward, and
    cached input is an order of magnitude cheaper than uncached. Trimming a few hundred
    characters therefore costs more than it saves, so below the floor we hand back the
    original list untouched. 0 keeps the old always-prune behaviour.
    """
    if saved_chars <= 0:
        return False
    return saved_chars >= settings.clear_at_least_chars


def _live_tool_call_index(messages: list[AgentMessage]) -> int:
    """Index of the newest assistant message carrying tool calls (the live step)."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "assistant" and messages[index].tool_calls():
            return index
    return -1


def prune_tool_call_arg_text(text: str, settings: CompactionSettings) -> str | None:
    """Head + marker + tail for one over-budget argument. None when already in budget."""
    if settings.tool_call_arg_threshold <= 0 or len(text) <= settings.tool_call_arg_threshold:
        return None
    marker = f"\n\n{get_prompt('tool_call_arg_pruned')}\n\n"
    head = max(0, settings.tool_call_arg_head)
    tail = max(0, settings.tool_call_arg_tail)
    if head + tail >= len(text):
        return None
    pruned = f"{text[:head]}{marker}{text[-tail:] if tail else ''}"
    return pruned if len(pruned) < len(text) else None


def prune_tool_call_args(
    messages: list[AgentMessage],
    settings: CompactionSettings | None = None,
) -> list[AgentMessage]:
    """Trim huge string arguments on older tool calls. The live step stays full.

    `prune_tool_results` only ever looked at what came back. What went out can be just as
    heavy: a `write` body or an `apply_patch` diff sits in the assistant message and is
    counted against the window on every later request, even though the call already ran
    and its result is what matters now. Names, paths and every short argument survive, so
    `_mentions` and the summary window still see which files were touched.
    """
    cfg = settings or CompactionSettings()
    if cfg.tool_call_arg_threshold <= 0:
        return messages
    live = _live_tool_call_index(messages)
    saved = 0
    out: list[AgentMessage] = []
    for index, message in enumerate(messages):
        blocks = message.content
        if message.role != "assistant" or index >= live or not isinstance(blocks, list):
            out.append(message)
            continue
        rewritten: list[ContentBlock] = []
        touched = False
        for block in blocks:
            if not isinstance(block, ToolCallBlock) or _prune_excluded(block.name, cfg):
                rewritten.append(block)
                continue
            arguments = dict(block.arguments)
            trimmed = False
            for key, value in block.arguments.items():
                if not isinstance(value, str):
                    continue
                pruned = prune_tool_call_arg_text(value, cfg)
                if pruned is None:
                    continue
                arguments[key] = pruned
                saved += len(value) - len(pruned)
                trimmed = True
            touched = touched or trimmed
            rewritten.append(replace(block, arguments=arguments) if trimmed else block)
        out.append(replace(message, content=rewritten) if touched else message)
    return out if _worth_clearing(saved, cfg) else messages


def estimate_tokens(message: AgentMessage) -> int:
    chars = 0
    if isinstance(message.content, str):
        chars = len(message.content)
    else:
        for block in message.content:
            if isinstance(block, TextBlock):
                chars += len(block.text)
            elif isinstance(block, ToolCallBlock):
                chars += len(block.name) + len(str(block.arguments))
    return max(1, (chars + 3) // 4)


def _last_checkpoint_index(messages: list[AgentMessage]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if str(messages[index].source or "") == COMPACTION_CHECKPOINT_SOURCE:
            return index
    return -1


def measured_prefix(messages: list[AgentMessage]) -> tuple[int, int]:
    """Newest trustworthy provider reading as (tokens, index of first unmeasured message).

    `usage.input` is what the provider actually charged for that request: the system prompt
    plus every message before it. The character heuristic can see neither the system prompt
    nor the provider's real tokenizer, so it under-counts — a reading is strictly better
    than an estimate, and once we have one we never fall back to guessing the same prefix.

    The one reading we must not trust is one taken before a compaction checkpoint. Compaction
    rewrites the head, so an older reading still describes the pre-compaction transcript; it
    would report the old size forever and re-trigger compaction every single turn. So the
    search floor is the newest checkpoint. Returns (0, ...) when nothing is measured, which
    is the normal case for history reloaded from disk — `store` keeps usage as its own rows,
    not per message.
    """
    floor = _last_checkpoint_index(messages)
    for index in range(len(messages) - 1, floor, -1):
        message = messages[index]
        if message.role != "assistant" or message.usage.input <= 0:
            continue
        return message.usage.input + message.usage.output, index + 1
    return 0, floor + 1


def total_tokens(messages: list[AgentMessage]) -> int:
    measured, start = measured_prefix(messages)
    if measured <= 0:
        return sum(estimate_tokens(item) for item in messages)
    return measured + sum(estimate_tokens(item) for item in messages[start:])


def should_compact(messages: list[AgentMessage], settings: CompactionSettings) -> bool:
    if not settings.enabled:
        return False
    return total_tokens(messages) > settings.context_window - settings.reserve_tokens


def _tool_delta(message: AgentMessage) -> int:
    if message.role == "assistant":
        blocks = message.content if isinstance(message.content, list) else []
        return sum(1 for block in blocks if isinstance(block, ToolCallBlock))
    if message.role == "toolResult":
        return -1
    return 0


def _unbalanced_prefix(messages: list[AgentMessage]) -> bool:
    """前缀里不能有未闭合的 tool-call。"""
    depth = 0
    for message in messages:
        depth += _tool_delta(message)
        if depth < 0:
            return True
    return depth != 0


def _cut_index(messages: list[AgentMessage], settings: CompactionSettings) -> int:
    kept = 0
    index = len(messages)
    while index > 0:
        candidate = messages[index - 1]
        next_kept = kept + estimate_tokens(candidate)
        if kept > 0 and next_kept > settings.keep_recent_tokens:
            break
        kept = next_kept
        index -= 1
        if kept >= settings.keep_recent_tokens:
            break
    while index > 0 and index < len(messages) and messages[index].role == "toolResult":
        index -= 1
    while index > 0 and _unbalanced_prefix(messages[:index]):
        index -= 1
    return index


def compact_now(
    messages: list[AgentMessage],
    settings: CompactionSettings | None = None,
    *,
    force: bool = True,
) -> list[AgentMessage] | None:
    """即使未达压力也压一段有效范围。没有安全切点则 None，不改写。"""
    cfg = settings or CompactionSettings()
    if not cfg.enabled:
        return None
    if not force and not should_compact(messages, cfg):
        return None
    cut = _cut_index(messages, cfg)
    if cut <= 0:
        return None
    head = triage_messages(messages[:cut])
    tail = messages[cut:]
    summary = _summary_prompt(head)
    logger.info("压缩上下文 dropped=%s kept=%s force=%s", cut, len(tail), force)
    marker = AgentMessage(
        role="user",
        content=f"[compaction]\n{summary}",
        source=COMPACTION_CHECKPOINT_SOURCE,
    )
    return [marker, *tail]


def parse_compact_range(rest: str) -> tuple[int, int] | None:
    """`/compact 2-10` / `2:10`。空 rest 表示整段 compactNow。非法则 ValueError。"""
    text = (rest or "").strip()
    if not text:
        return None
    match = _RANGE.fullmatch(text)
    if not match:
        raise ValueError("range")
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise ValueError("range")
    return start, end


def compact_region(
    messages: list[AgentMessage],
    start: int,
    end: int,
    settings: CompactionSettings | None = None,
) -> list[AgentMessage] | None:
    """区间压缩：压闭区间 [start, end]。切点两侧 tool-call 必须配对。"""
    cfg = settings or CompactionSettings()
    if not cfg.enabled:
        return None
    if start < 0 or end >= len(messages) or start > end or end - start < 1:
        return None
    if _unbalanced_prefix(messages[:start]) or _unbalanced_prefix(messages[start : end + 1]):
        return None
    region = triage_messages(messages[start : end + 1])
    summary = _summary_prompt(region)
    logger.info("压缩区间 start=%s end=%s dropped=%s kept=%s", start, end, len(region), len(messages) - len(region))
    marker = AgentMessage(
        role="user",
        content=f"[compaction]\n{summary}",
        source=COMPACTION_CHECKPOINT_SOURCE,
        meta={"keep_before": start} if start else {},
    )
    return [*messages[:start], marker, *messages[end + 1 :]]


def compact_messages(messages: list[AgentMessage], settings: CompactionSettings | None = None) -> list[AgentMessage]:
    cfg = settings or CompactionSettings()
    compacted = compact_now(messages, cfg, force=False)
    return messages if compacted is None else compacted


async def compact_messages_async(
    messages: list[AgentMessage],
    settings: CompactionSettings | None = None,
    *,
    stream_fn=None,
    workspace_dir: str = "",
    project_id: str = "",
    agent_id: str = "",
    session_id: str = "",
    model=None,
    force: bool = False,
) -> list[AgentMessage]:
    """超窗或 force 时先走模板摘录；有 stream_fn 再让模型写摘要，失败回退摘录。"""
    cfg = settings or CompactionSettings()
    extractive = compact_now(messages, cfg, force=force)
    if extractive is None:
        return messages
    if stream_fn is None or not cfg.use_model:
        return extractive
    cut = _cut_index(messages, cfg)
    if cut <= 0:
        return messages
    head = triage_messages(messages[:cut])
    tail = messages[cut:]
    user_text = _summary_prompt(head)
    from witty_agent.types import AgentContext, ModelRef

    context = AgentContext(
        system_prompt=get_prompt("compaction_system"),
        messages=[AgentMessage(role="user", content=user_text)],
        tools=[],
        workspace_dir=workspace_dir,
        model=model or ModelRef(provider="openai", model_id="compaction"),
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    try:
        summary = await stream_fn(context)
    except Exception as exc:
        logger.warning("模型摘要失败，回退摘录 err=%s", exc)
        return extractive
    text = summary.text().strip()
    if summary.stop_reason == "error" or not text:
        return extractive
    logger.info("模型摘要完成 chars=%s kept=%s", len(text), len(tail))
    return [
        AgentMessage(
            role="user",
            content=f"[compaction]\n{text}",
            source=COMPACTION_CHECKPOINT_SOURCE,
        ),
        *tail,
    ]


async def compact_region_async(
    messages: list[AgentMessage],
    start: int,
    end: int,
    settings: CompactionSettings | None = None,
    *,
    stream_fn=None,
    workspace_dir: str = "",
    project_id: str = "",
    agent_id: str = "",
    session_id: str = "",
    model=None,
) -> list[AgentMessage] | None:
    """区间压缩；有模型则摘要该段，失败回退摘录。"""
    cfg = settings or CompactionSettings()
    extractive = compact_region(messages, start, end, cfg)
    if extractive is None or stream_fn is None or not cfg.use_model:
        return extractive
    region = triage_messages(messages[start : end + 1])
    user_text = _summary_prompt(region)
    from witty_agent.types import AgentContext, ModelRef

    context = AgentContext(
        system_prompt=get_prompt("compaction_system"),
        messages=[AgentMessage(role="user", content=user_text)],
        tools=[],
        workspace_dir=workspace_dir,
        model=model or ModelRef(provider="openai", model_id="compaction"),
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    try:
        summary = await stream_fn(context)
    except Exception as exc:
        logger.warning("区间摘要失败，回退摘录 err=%s", exc)
        return extractive
    text = summary.text().strip()
    if summary.stop_reason == "error" or not text:
        return extractive
    logger.info("区间摘要完成 chars=%s start=%s end=%s", len(text), start, end)
    marker = AgentMessage(
        role="user",
        content=f"[compaction]\n{text}",
        source=COMPACTION_CHECKPOINT_SOURCE,
        meta={"keep_before": start} if start else {},
    )
    return [*messages[:start], marker, *messages[end + 1 :]]


def _summary_prompt(messages: list[AgentMessage]) -> str:
    reads, writes, commands, failed = _mentions(messages)
    transcript = "\n".join(f"{item.role}: {item.text()[:400]}" for item in messages)
    return get_prompt(
        "compaction_user",
        read_files=", ".join(sorted(reads)) or "(none)",
        modified_files=", ".join(sorted(writes)) or "(none)",
        commands=", ".join(sorted(commands)) or "(none)",
        failed_tools=", ".join(sorted(failed)) or "(none)",
        transcript=transcript[:12000],
    )


def _file_mentions(messages: list[AgentMessage]) -> tuple[set[str], set[str]]:
    reads, writes, _commands, _failed = _mentions(messages)
    return reads, writes


def _mentions(messages: list[AgentMessage]) -> tuple[set[str], set[str], set[str], set[str]]:
    reads: set[str] = set()
    writes: set[str] = set()
    commands: set[str] = set()
    failed: set[str] = set()
    for message in messages:
        if message.role == "toolResult" and (message.is_error or _FAILED_TOOL.search(message.text() or "")):
            if message.tool_name:
                failed.add(message.tool_name)
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if not isinstance(block, ToolCallBlock):
                continue
            path = str(block.arguments.get("path") or "")
            if path and block.name == "read":
                reads.add(path)
            elif path and block.name in {"write", "edit", "apply_patch"}:
                writes.add(path)
            if block.name == "bash":
                command = str(block.arguments.get("command") or block.arguments.get("cmd") or "").strip()
                if command:
                    commands.add(command[:160])
    return reads, writes, commands, failed
