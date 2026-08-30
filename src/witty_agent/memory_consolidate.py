"""记忆巩固：把一格里越攒越碎的条目合并成更少更准的几条。

收割只会 append + 按**字面**去重（`memory._fact_key` 比的是整串），所以同一件事换个
说法就占两个槽：`点表在共享盘` 和 `点表台账放 //nas/dispatch/points/ 下面` 是两条。
一格的工作集只有 `working_set` 个槽，碎片多了就把真正有用的条目挤进归档——**记忆越用
越糊**，这是纯字面手段修不了的最后一段，PROGRESS 里也是这么记的（「字面操作到顶了，
要语义相似度」）。

巩固是唯一能把这件事扳回来的一步：拿模型读一格，合并同类项、丢掉被推翻的旧结论。
它不在关键路径上——由 `Session` 挪到后台，跟判官同一套路子。

安全网：合并前把原样的条目抄进 `retired/`，模型合错了人还能翻回去。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from witty_agent.async_bridge import run_sync
from witty_agent.logging import get_logger
from witty_agent.memory import (
    RETIRED_DIR,
    _bullets,
    memory_budget,
    read_turns,
    rebuild_memory_index,
    topic_body,
    write_topic,
)
from witty_agent.memory_config import MemorySettings, load_memory_settings
from witty_agent.prompts import get_prompt

logger = get_logger("memory")
_STAMP = "consolidated_turn"


def pick_cells(
    directory: Path,
    settings: MemorySettings | None = None,
) -> list[str]:
    """这一轮该整理哪几格。没到水位就返回空，别每轮都喊模型。"""
    settings = settings or load_memory_settings()
    if not settings.consolidate_enabled:
        return []
    turns = read_turns(directory)
    last = _read_stamp(directory)
    if turns - last < settings.consolidate_min_turns:
        return []
    budget = memory_budget(directory, settings)
    rows = [item for item in budget["cells"] if int(item["count"]) >= settings.consolidate_high_water]
    if not rows and not budget["over_budget"]:
        return []
    if not rows:
        # 总量超预算但没有哪一格特别满：整理最满的那几格，总归能腾出位置。
        rows = list(budget["cells"])
    return [str(item["slug"]) for item in rows[: settings.consolidate_max_cells]]


async def aconsolidate(
    directory: Path,
    slugs: list[str] | None = None,
    *,
    settings: MemorySettings | None = None,
    merge_fn=None,
) -> dict[str, object]:
    """整理指定的格子。`merge_fn` 可注入，测试不必联网；同步异步都收。

    格子之间是串行的：它们改的是同一份九宫格，并发合并会互相覆盖。
    """
    settings = settings or load_memory_settings()
    targets = slugs if slugs is not None else pick_cells(directory, settings)
    if not targets:
        return {"cells": [], "removed": 0}
    merge = merge_fn or _model_merge
    done: list[str] = []
    removed = 0
    for slug in targets:
        before = _bullets(topic_body(directory, slug))
        if len(before) < 2:
            continue
        try:
            after = merge(before, slug, settings)
            if inspect.isawaitable(after):
                after = await after
        except Exception as exc:
            logger.warning("记忆巩固失败 slug=%s err=%s", slug, exc)
            continue
        after = _sane(after, before)
        if after is None or len(after) >= len(before):
            continue
        _retire(directory, slug, before)
        description = _describe(slug, settings)
        write_topic(directory, slug, description=description, body="\n".join(f"- {x}" for x in after))
        removed += len(before) - len(after)
        done.append(slug)
        logger.info("记忆巩固 slug=%s %s条 -> %s条", slug, len(before), len(after))
    if done:
        _write_stamp(directory, read_turns(directory))
        rebuild_memory_index(directory, settings=settings)
    return {"cells": done, "removed": removed}


def consolidate(
    directory: Path,
    slugs: list[str] | None = None,
    *,
    settings: MemorySettings | None = None,
    merge_fn=None,
) -> dict[str, object]:
    """`aconsolidate` 的同步包装，给脚本和测试用。"""
    return run_sync(
        aconsolidate(directory, slugs, settings=settings, merge_fn=merge_fn),
        entry="aconsolidate",
    )


def _sane(after: object, before: list[str]) -> list[str] | None:
    """模型给的结果得像样才收：非空、都是字符串、不比原来还长。

    合并是**有损**操作，所以宁可不合也不能合坏。这儿只做形状检查；内容对不对靠
    `retired/` 兜底，人翻得回去。
    """
    if not isinstance(after, list) or not after:
        return None
    rows = [str(item).strip() for item in after if str(item).strip()]
    if not rows or len(rows) > len(before):
        return None
    # 一格被合成一条通常是模型把不相干的事揉一起了，不是真的只剩一条。
    if len(before) >= 6 and len(rows) == 1:
        return None
    return rows


def _describe(slug: str, settings: MemorySettings) -> str:
    cell = settings.cell(slug)
    if cell is not None:
        return cell.description or cell.title
    tax = settings.tax(slug)
    if tax is not None:
        return tax.title
    return slug


def _retire(directory: Path, slug: str, lines: list[str]) -> None:
    """合并前留底。巩固是有损的，原文得能翻回来。"""
    retired = directory / RETIRED_DIR
    retired.mkdir(parents=True, exist_ok=True)
    existing = _bullets(topic_body(retired, slug))
    body = "\n".join(f"- {item}" for item in [*existing, *lines])
    write_topic(retired, slug, description=f"retired {slug}", body=body)


def _stamp_path(directory: Path) -> Path:
    return directory / ".consolidate"


def _read_stamp(directory: Path) -> int:
    path = _stamp_path(directory)
    if not path.is_file():
        return 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(_STAMP):
            _, _, raw = line.partition("=")
            try:
                return max(0, int(raw.strip()))
            except ValueError:
                return 0
    return 0


def _write_stamp(directory: Path, turns: int) -> None:
    _stamp_path(directory).write_text(f"{_STAMP} = {turns}\n", encoding="utf-8")


async def _model_merge(lines: list[str], slug: str, settings: MemorySettings) -> list[str]:
    title = _describe(slug, settings)
    cell = settings.cell(slug)
    numbered = "\n".join(f"{index + 1}. {line}" for index, line in enumerate(lines))
    system = get_prompt(
        "memory_consolidate_system",
        title=(cell.title if cell is not None else title),
        description=title,
    )
    user = get_prompt(
        "memory_consolidate_user",
        title=(cell.title if cell is not None else title),
        count=str(len(lines)),
        lines=numbered[:4000],
    )
    return _parse(await _ask(system, user))


async def _ask(system: str, user: str) -> str:
    from witty_agent.llm import OpenAICompatLLM
    from witty_agent.types import AgentContext, AgentMessage, ModelRef

    llm = OpenAICompatLLM(stream=False, timeout=40, max_tokens=1200, retry_attempts=1)
    llm.think_level = "off"
    context = AgentContext(
        system_prompt=system,
        messages=[AgentMessage(role="user", content=user)],
        tools=[],
        workspace_dir="",
        model=ModelRef(provider="openai", model_id=llm.model_id),
        project_id="",
        agent_id="memory-consolidate",
        session_id="memory-consolidate",
    )
    message = await llm(context)
    if message.stop_reason == "error":
        raise RuntimeError(message.text() or "consolidate error")
    return message.text()


def _parse(raw: str) -> list[str]:
    text = (raw or "").strip()
    if "```" in text:
        start = text.find("```")
        chunk = text[start + 3 :]
        if chunk.lstrip().lower().startswith("json"):
            chunk = chunk.lstrip()[4:]
        end = chunk.find("```")
        if end >= 0:
            chunk = chunk[:end]
        text = chunk.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if str(item).strip()]
