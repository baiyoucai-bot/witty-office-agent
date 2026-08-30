"""从一轮对话里收偏好、分类和九宫格条目。规则来自 memory.toml，不是业务 if。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from witty_agent.async_bridge import in_event_loop, run_sync
from witty_agent.guard import is_empty_lookup_text
from witty_agent.logging import get_logger
from witty_agent.memory import (
    FACT_TOOLS,
    _bullets,
    append_unique_bullets,
    ensure_lattice,
    read_turns,
    rebuild_memory_index,
    resolve_session_memory,
    topic_body,
    write_profile,
    write_topic,
)
from witty_agent.memory_config import MemorySettings, load_memory_settings
from witty_agent.memory_prefs import is_process_line, parse_pref_line, strip_bullet_meta, upsert_pref_bullets
from witty_agent.prompts import get_prompt
from witty_agent.time_context import clock_now
from witty_agent.timeline import harvest_timeline
from witty_agent.types import AgentMessage

logger = get_logger("memory")
_SPLIT = re.compile(r"[。！？!?\n]+")
# 分句：线索只在自己所在的分句里较量（见 `_winning_cells`）。
_CLAUSE_SPLIT = re.compile(r"[，,；;]+")
_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")
_ASSISTANT_CELLS = frozenset({"decisions", "followups"})


def harvest_turn(
    *,
    project_id: str,
    agent_id: str,
    workspace: str | Path,
    root: Path | None,
    user_text: str,
    assistant_text: str = "",
) -> dict[str, object]:
    memory = resolve_session_memory(
        project_id=project_id,
        agent_id=agent_id,
        workspace=workspace,
        root=root,
    )
    report = harvest_user_text(memory.user_dir, user_text)
    if assistant_text and memory.workspace_dir is not None:
        extra = harvest_assistant_notes(memory.workspace_dir, assistant_text)
        report = {**report, "assistant_added": extra.get("added", 0), "assistant_cells": extra.get("cells", [])}
    return report


def harvest_tool_facts(
    directory: Path,
    items: Sequence[dict[str, Any]] | None,
    *,
    cite: str = "",
) -> dict[str, object]:
    """Optionally write observational tool excerpts into workspace memory.

    Default off：执行过程不进记忆。只在 [memory].harvest_process = true 时写入。
    User lattice stays speaker-true: only harvest_user_text writes prefs/domain.
    """
    settings = load_memory_settings()
    if not settings.auto_harvest or not settings.harvest_process or not items:
        return {"skipped": True, "added": 0, "slugs": []}
    added = 0
    slugs: list[str] = []
    today = str(clock_now()["date"])
    for item in items:
        if str(item.get("kind") or "tool") != "tool" or item.get("ok") is False:
            continue
        source = str(item.get("source") or "").strip()
        if source not in FACT_TOOLS:
            continue
        locator = str(item.get("locator") or "").strip()
        excerpt = str(item.get("excerpt") or "").strip()
        if not excerpt or is_empty_lookup_text(excerpt):
            continue
        slug = _fact_slug(source, locator)
        line = f"{source} {locator}: {excerpt}".strip() if locator else f"{source}: {excerpt}"
        count = append_unique_bullets(
            directory,
            slug,
            description=get_prompt("memory_tool_fact", tool=source, locator=locator or source),
            lines=_stamp([line], today, cite=cite),
        )
        if count:
            added += count
            slugs.append(slug)
    if added:
        rebuild_memory_index(directory, settings=settings)
        logger.info("工具事实收割 added=%s slugs=%s", added, ",".join(slugs))
    return {"skipped": False, "added": added, "slugs": slugs}


def last_assistant_text(messages: Sequence[AgentMessage] | None) -> str:
    for message in reversed(messages or ()):
        if message.role != "assistant":
            continue
        if str(message.source or "").startswith("plugin:"):
            continue
        if message.tool_calls() and not message.text():
            continue
        text = message.text().strip()
        if text:
            return text
    return ""


def harvest_assistant_notes(
    directory: Path,
    assistant_text: str,
    *,
    settings: MemorySettings | None = None,
    cite: str = "",
) -> dict[str, object]:
    """Record assistant-stated decisions/followups into workspace memory.

    Never writes the user lattice. User cues like 我喜欢 stay speaker-true.

    `settings` 可注入，跟 `harvest_user_text` 一致。此前这儿硬读配置，于是助手侧的
    线索行为没法用合成线索表测——线索语法是两张表共用的，只测得了一张就等于只保住一张。
    """
    settings = settings or load_memory_settings()
    if not settings.auto_harvest or len((assistant_text or "").strip()) < 4:
        return {"skipped": True, "added": 0, "cells": []}
    sentences = _sentences(assistant_text)
    added = 0
    cells_hit: list[str] = []
    today = str(clock_now()["date"])
    prefix = get_prompt("memory_assistant_prefix")
    for cell_id, cues in settings.assistant_cues.items():
        if cell_id not in _ASSISTANT_CELLS:
            continue
        # 走 `_cue_weight` 而不是 `cue in line`：算子语法两张线索表得一致，否则哪天在
        # `assistant_cues` 里写了 `核心+护词`，这儿会按字面找 `核心+护词` 这七个字，
        # 永远不命中，还不报错。不带算子的线索两种写法结果一样。
        hits = [line for line in sentences if any(_cue_weight(cue, line) for cue in cues)]
        if not hits:
            continue
        cell = settings.cell(cell_id)
        description = (
            cell.description
            if cell is not None
            else get_prompt("memory_assistant_note", cell=cell_id)
        )
        count = append_unique_bullets(
            directory,
            cell_id,
            description=description,
            lines=_stamp([f"{prefix}{line}" for line in hits], today, cite=cite),
        )
        if count:
            added += count
            cells_hit.append(cell_id)
    if added:
        rebuild_memory_index(directory, settings=settings)
        logger.info("助手记录收割 added=%s cells=%s", added, ",".join(cells_hit))
    return {"skipped": False, "added": added, "cells": cells_hit}


def harvest_user_text(
    user_dir: Path,
    user_text: str,
    *,
    judge_fn: Callable[[list[str], str, MemorySettings], list[tuple[str, str]]] | None = None,
    settings: MemorySettings | None = None,
    cite: str = "",
    defer_judge: bool = False,
) -> dict[str, object]:
    """收割一轮用户话。`defer_judge` 把**模型判官**那一段留给调用方去后台跑。

    判官是这条路上唯一的网络调用（`_ask_judge` 走同步 urlopen，25 秒超时），而它跑在
    每轮结束的关键路径上：答案都流完了，`run()` 还要再等一次模型往返才返回，事件循环
    连带被占住。`defer_judge=True` 时这儿只做确定性的那部分（线索词、分类、时间线、
    画像），把判官要看的句子放进 `pending_judge` 原样交回，谁调用谁决定什么时候判。

    判官本来就跑不起来时（没配 key / 测试里），仍旧当场走结构判据——那是纯正则，快，
    没有理由推迟，推迟了反而让「这一格什么时候落盘」多出一种情况。
    """
    settings = settings or load_memory_settings()
    if not settings.auto_harvest or len((user_text or "").strip()) < 2:
        return {"skipped": True, "added": 0}
    ensure_lattice(user_dir, settings)
    scrub_transient_domain(user_dir, settings)
    sentences = _sentences(user_text)
    tax_hit: list[str] = []
    today = str(clock_now()["date"])

    matched = _match_cues(sentences, settings)
    retracts = [
        line
        for line in sentences
        if parse_pref_line(line, settings)[1] and line not in (matched.get("prefs") or [])
    ]
    if retracts:
        matched.setdefault("prefs", []).extend(retracts)
    # 走 `_apply_decided`，不要在这儿再抄一遍写入：那份抄本漏了 `_keep_decided_line`，
    # 而 `_match_cues` 里的 `_worth_keeping` 又对 prefs 例外，于是带 prefs 线索的问句
    # （`以后叫我什么好呢`、`是不是都叫我老王`）直接落进 prefs——常驻格、不衰减，进去不走。
    # 实测 8 句这种里 7 句能驻进去。
    added, cells_hit = _apply_decided(
        user_dir,
        [(cell_id, line) for cell_id, lines in matched.items() for line in lines],
        settings,
        today,
        cite=cite,
    )

    for item in settings.taxonomy:
        hits = [
            line
            for line in sentences
            if not is_process_line(line, settings)
            and any(word and word in line for word in item.keywords)
            and _worth_keeping(line)
        ]
        if not hits:
            continue
        count = append_unique_bullets(
            user_dir,
            item.id,
            description=item.title,
            lines=_stamp(hits, today, cite=cite),
        )
        assets = settings.cell("assets")
        if assets is not None:
            count += append_unique_bullets(
                user_dir,
                assets.id,
                description=assets.description or assets.title,
                lines=_stamp([f"{item.title}：{line}" for line in hits], today, cite=cite),
            )
        if count:
            added += count
            tax_hit.append(item.id)
            if assets is not None and "assets" not in cells_hit:
                cells_hit.append("assets")

    leftover = [
        line
        for line in sentences
        if not _already_kept(line, cells_hit, tax_hit, settings)
    ]
    leftover_only = False
    pending_judge: list[str] = []
    if leftover and _defer_to_judge(defer_judge, judge_fn, settings):
        # 交给后台判官的句子这儿一条都不落盘：结构判据和判官对同一句可能判去不同格
        # （`改 setting_v3.docx 必须两人复核` 结构判据认 constraints，判官可能认 assets），
        # 两边都跑就成了同一句占两格。要么当场判，要么推迟判，不能都判。
        pending_judge = [line for line in leftover if _worth_keeping(line)]
    elif leftover:
        decided = _decide_leftover(leftover, user_text, settings, judge_fn)
        before = set(cells_hit)
        extra, extra_cells = _apply_decided(user_dir, decided, settings, today, cite=cite)
        if extra:
            added += extra
            for cell_id in extra_cells:
                if cell_id not in cells_hit:
                    cells_hit.append(cell_id)
            leftover_only = extra_cells == ["domain"] and "domain" not in before

    timed = harvest_timeline(user_dir, user_text)
    if timed:
        added += timed
    from witty_agent.links import harvest_links

    links = harvest_links(user_text)
    if links:
        added += len(links)
    from witty_agent.diary import harvest_diary

    noted = harvest_diary(user_text, memory_dir=user_dir)
    if noted:
        added += noted
    linked = list(cells_hit) + list(tax_hit)
    if leftover_only:
        linked = [item for item in linked if item != "domain"]
    if timed:
        linked.append("timeline")
    if len(linked) >= 2:
        from witty_agent.memory_graph import add_cooccurrence_links

        add_cooccurrence_links(user_dir, linked, reason="same-turn")
    turns = read_turns(user_dir) + 1
    write_profile(user_dir, turns=turns, settings=settings)
    rebuild_memory_index(user_dir, settings=settings)
    if added:
        logger.info("记忆收割 added=%s cells=%s tax=%s timed=%s", added, ",".join(cells_hit), ",".join(tax_hit), timed)
    else:
        logger.info("记忆收割 added=0 turns=%s", turns)
    return {
        "skipped": False,
        "added": added,
        "cells": cells_hit,
        "taxonomy": tax_hit,
        "turns": turns,
        "timeline": timed,
        "pending_judge": pending_judge,
    }


def _defer_to_judge(
    defer_judge: bool,
    judge_fn: Callable[[list[str], str, MemorySettings], list[tuple[str, str]]] | None,
    settings: MemorySettings,
) -> bool:
    """这一轮的 leftover 该不该留给后台判官。

    只有「调用方愿意推迟」且「判官真的会跑」时才推迟。传了 `judge_fn` 的调用方（测试、
    脚本）自带判官，那是同步的，不推迟。
    """
    return bool(
        defer_judge
        and judge_fn is None
        and settings.judge_leftover
        and _live_judge_allowed()
    )


async def ajudge_pending_leftover(
    user_dir: Path,
    pending: list[str],
    user_text: str,
    *,
    settings: MemorySettings | None = None,
    cite: str = "",
) -> dict[str, object]:
    """把 `harvest_user_text` 推迟下来的句子交给模型判官，判完落盘。

    调用方负责挪出关键路径（见 `Session._harvest_memory`）。判官挂了就按判官原本的语义
    丢掉这些句子——`_model_judge` 失败时返回空，配置注释写的就是「判不了就丢掉，不默认
    塞进领域要点」。
    """
    lines = [line for line in (pending or []) if str(line).strip()]
    if not lines:
        return {"added": 0, "cells": []}
    settings = settings or load_memory_settings()
    decided = _filter_decided(await _amodel_judge(lines, user_text, settings))
    if not decided:
        return {"added": 0, "cells": []}
    today = str(clock_now()["date"])
    added, cells_hit = _apply_decided(user_dir, decided, settings, today, cite=cite)
    if added:
        write_profile(user_dir, turns=read_turns(user_dir), settings=settings)
        rebuild_memory_index(user_dir, settings=settings)
        logger.info("判官补收 added=%s cells=%s", added, ",".join(cells_hit))
    return {"added": added, "cells": cells_hit}


def judge_pending_leftover(
    user_dir: Path,
    pending: list[str],
    user_text: str,
    *,
    settings: MemorySettings | None = None,
    cite: str = "",
) -> dict[str, object]:
    """`ajudge_pending_leftover` 的同步包装，给脚本和测试用。"""
    return run_sync(
        ajudge_pending_leftover(user_dir, pending, user_text, settings=settings, cite=cite),
        entry="ajudge_pending_leftover",
    )


def _fact_slug(source: str, locator: str) -> str:
    base = Path(locator).name if locator else source
    cleaned = _SLUG_CLEAN.sub("-", base.casefold()).strip("-")
    if not cleaned:
        cleaned = _SLUG_CLEAN.sub("-", source.casefold()).strip("-") or "tool"
    return cleaned[:48]


def _sentences(text: str) -> list[str]:
    rows: list[str] = []
    for part in _SPLIT.split(text or ""):
        line = re.sub(r"\s+", " ", part).strip()
        if len(line) >= 4:
            rows.append(line[:240])
    return rows


def _stamp(lines: list[str], today: str, *, cite: str = "") -> list[str]:
    tag = f" {cite}" if cite and cite.startswith("[cite:") else ""
    rows: list[str] = []
    for line in lines:
        body = line if line.startswith(today) else f"{today} {line}"
        if tag and tag.strip() not in body:
            body = f"{body}{tag}"
        rows.append(body)
    return rows


def _split_cue(cue: str) -> tuple[str, str, str]:
    """拆 `核心+护词|护词` / `核心-忌词|忌词`。

    只有算子两侧都有实字才当算子，所以 `sk-` 这种以横线收尾的线索仍按字面匹配。
    一条线索只支持一个算子，取最先出现的那个。
    """
    for index, char in enumerate(cue):
        if char in "+-" and cue[:index] and cue[index + 1 :]:
            return cue[:index], char, cue[index + 1 :]
    return cue, "", ""


def _cue_weight(cue: str, text: str) -> int:
    """这条线索在这段文字里匹配到多少字面；0 表示不算命中。

    字面长度就是「有多具体」的现成度量——配置里 `下次不要`（prefs）比 `下次`（followups）
    长，长的那条本来就是写给更窄的情形的。护词算进长度里，`我在+科室` 才压得住 `科室`。
    """
    core, op, extra = _split_cue(cue)
    if not core or core not in text:
        return 0
    if op == "-":
        return 0 if any(word and word in text for word in extra.split("|")) else len(core)
    if op == "+":
        hit = [word for word in extra.split("|") if word and word in text]
        return len(core) + max(len(word) for word in hit) if hit else 0
    return len(cue)


def _clauses(line: str) -> list[str]:
    """按逗号/分号切分句。切不动就返回整句。"""
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(line) if part.strip()]
    return parts or [line]


def _winning_cells(line: str, settings: MemorySettings) -> set[str]:
    """这句话该归哪几格：分句内比证据强弱，分句间取并集。

    改前每一格各自扫全句，命中就算，谁也不让谁。于是 `下次不要每条都加铺垫` 同时进 prefs
    和 followups（一条偏好吃两格预算），`五防校验不能通过是因为双位置不一致` 被 `不能`
    捞进 constraints——那是常驻格、豁免衰减，错进去就不出来。

    分句是关键：`我是自动化专责，下次的点表核对归我` 真是两件事，两格都该进；而
    `以后都叫我老王，联系我走内网邮箱` 里的 `联系` 只是动词，跟 people 无关。让线索只
    在自己那个分句里较量，两种情形就自然分开了——单分句的句子行为与改前完全一致。
    """
    won: set[str] = set()
    for clause in _clauses(line):
        best: dict[str, int] = {}
        for cell_id, cues in settings.cues.items():
            weight = max((_cue_weight(cue, clause) for cue in cues), default=0)
            if weight:
                best[cell_id] = weight
        if not best:
            continue
        top = max(best.values())
        # 平手就都进：证据一样强的时候硬挑一个是瞎猜，而这一带真有两件事同时说的句子。
        won.update(cell_id for cell_id, weight in best.items() if weight == top)
    return won


def _match_cues(sentences: list[str], settings: MemorySettings) -> dict[str, list[str]]:
    winners = [_winning_cells(line, settings) for line in sentences]
    found: dict[str, list[str]] = {}
    # 按配置里的格子顺序装，别按谁先赢——下游 `cells_hit` 的顺序会跟着变。
    for cell_id in settings.cues:
        hits = [
            line
            for line, cells in zip(sentences, winners)
            if cell_id in cells and (cell_id == "prefs" or _worth_keeping(line))
        ]
        if hits:
            found[cell_id] = hits
    return found


def _apply_decided(
    user_dir: Path,
    decided: list[tuple[str, str]],
    settings: MemorySettings,
    today: str,
    *,
    cite: str = "",
) -> tuple[int, list[str]]:
    added = 0
    cells_hit: list[str] = []
    grouped: dict[str, list[str]] = {}
    for cell_id, line in decided:
        if settings.cell(cell_id) is None or not line.strip():
            continue
        if not _keep_decided_line(cell_id, line):
            continue
        grouped.setdefault(cell_id, []).append(line)
    for cell_id, lines in grouped.items():
        cell = settings.cell(cell_id)
        if cell is None:
            continue
        stamped = _stamp(lines, today, cite=cite)
        if cell_id == "prefs":
            count = upsert_pref_bullets(
                user_dir,
                description=cell.description or cell.title,
                lines=stamped,
                settings=settings,
            )
        else:
            count = append_unique_bullets(
                user_dir,
                cell.id,
                description=cell.description or cell.title,
                lines=stamped,
            )
        if count:
            added += count
            cells_hit.append(cell_id)
    return added, cells_hit


def _decide_leftover(
    leftover: list[str],
    user_text: str,
    settings: MemorySettings,
    judge_fn: Callable[[list[str], str, MemorySettings], list[tuple[str, str]]] | None,
) -> list[tuple[str, str]]:
    candidates = [line for line in leftover if _worth_keeping(line)]
    if not candidates:
        return []
    if judge_fn is not None:
        return _filter_decided(judge_fn(candidates, user_text, settings))
    if settings.judge_leftover:
        # 在事件循环里就不能同步等模型，那会把整个 agent 堵住。这条路上判官本来就该走
        # `defer_judge=True` 挪到后台（见 `Session._harvest_memory`），这里退回结构判定。
        if not _live_judge_allowed() or in_event_loop():
            return _structural_leftover(candidates, settings)
        return _filter_decided(_model_judge(candidates, user_text, settings))
    return _structural_leftover(candidates, settings)


def _structural_leftover(
    candidates: list[str], settings: MemorySettings
) -> list[tuple[str, str]]:
    """判官跑不起来时，按句子的**结构**认领域事实和资产。

    `domain` / `assets` 是九宫格里唯一两个没有 cue 的格子——这是有意的，它们没有词法标记，
    硬编 cue 只会让半个语料掉进 domain（`是` / `放在` 一加就完）。原设计让判官兜这两格，
    可这个部署没有 API key，判官压根跑不起来，于是这两格在确定性路径上**一个入口都没有**，
    实测 19 条领域/资产事实进来 0 条。

    换成结构判据：到得了这儿的句子已经过了 `_worth_keeping`（问句/派活/寒暄早没了），
    也已经没被任何 cue 命中（不跟别的格子抢）。剩下要分的只是「陈述事实」和「说该怎么做」：
      资产 —— 句子里有路径或文件名，且不是在派活（`点表台账放在 //nas/dispatch/points/ 下面`）
      领域 —— 泛化断言或系动词断言，且不是在派活（`遥信抖动一般是接点接触不良`）
    「不是在派活」是这两条的共同前提，也是配置注释「判不了就丢掉，不默认塞进领域要点」
    真正在防的事——防的是把 leftover 当垃圾桶倒进 domain，不是防有正面证据的句子进来。

    留出集上 domain 认出 5/6、assets 4/4，两边误认都是 0。
    """
    decided: list[tuple[str, str]] = []
    for line in candidates:
        text = strip_bullet_meta(line).strip()
        if not text or _is_imperative(text):
            continue
        if _states_a_rule(text):
            # 红线优先于资产：`改 setting_v3.docx 必须两人复核` 有文件名，可它说的是
            # 「动这个文件要守什么规矩」，那是红线不是资产。不分先后这一句会同时占两格。
            decided.append(("constraints", line))
        elif _HAS_LOCATOR.search(text):
            # 资产优先于领域：`主站配置备份是 backup_20260801.tar.gz。` 也是系动词断言，但它说的是
            # 「东西叫啥、在哪」，那是资产。不分先后的话这一句会同时占两格。
            decided.append(("assets", line))
        elif _GENERALIZES.search(text) or _asserts_state(text):
            decided.append(("domain", line))
    # 不套 `_filter_decided`：这些句子已经过了上面的 `_worth_keeping`，而
    # `_keep_decided_line` 在这条路上查的每一项（问句、对助手打听、派活）都是
    # `_worth_keeping` 的子集，写入时 `_apply_decided` 还会再过一遍。套上去是空转，
    # 而空转的代码没法用变异测试压住——实测把它去掉，14 个变异一个都不变色。
    return decided


def _is_imperative(text: str) -> bool:
    """在派活——让人现在去做哪件事。

    `_looks_like_task` 只看句首，管不了 `得先把老站的点表补齐` 这种把动词埋在中间的句子；
    这儿看整句有没有派活标记。

    只认派活标记，不认规程语气：这两家原来挤在同一张表里，于是 `点号必须一致` 跟
    `把点表补齐` 同判——前者是长期规矩，被当成一次性活儿扔掉，实测规则句漏收 9/9。
    """
    return bool(_TASK_MARKER.search(_ASCII_RUN.sub("〇", text)))


def _states_a_rule(text: str) -> bool:
    """规程语气的长期规矩，不是这一回的义务。

    `constraints` 是常驻格、不衰减，错进去就永远在提示里，所以这儿只放长期规矩：
    带得出日期的句子（`今天必须…` / `8月25日前必须…`）说的是这一回，交给时间线，不驻常驻格。
    日期判据直接借 `timeline.extract_dated_events`——时间引用的识别那儿已经有一份，
    这儿再写一张表就是第二个真相来源。

    话头词也要排掉：`原则是必须两人复核` 既有话头词又有规程语气，可它是在给自己的判断
    起头。这跟 `_asserts_state` 排话头词是同一条理由，只是那条管系动词、这条管规程语气。
    """
    if not _RULE_MODAL.search(text):
        return False
    if _ONE_TIME.search(text) or _TOPIC_LEAD.search(text):
        return False
    from witty_agent.timeline import extract_dated_events

    return not extract_dated_events(text)


def _asserts_state(text: str) -> bool:
    """系动词断言 `X 是 Y`，但排掉「话头词 + 是」。

    `结论是先按老规约跑一段` / `难点是老站没有电子版点表` / `第一步是把那份导出来` 都是
    `X 是 Y`，可 `是` 前面那个词（结论/难点/第一步…）表明这是在给自己的判断或计划起头，
    不是在陈述领域规律。实测不排话头词的话留出集误认 7/14，排掉之后 0/14。
    """
    return bool(_COPULA.search(text)) and not _TOPIC_LEAD.search(text)


def _keep_decided_line(cell_id: str, line: str) -> bool:
    text = strip_bullet_meta(line).strip()
    if not text:
        return False
    if cell_id == "domain" and not _worth_keeping(line):
        return False
    if _is_question(text) or _TO_AGENT.match(text):
        return False
    # 偏好天生长成祈使句：`别再用自动生成的点表`、`下次不要每条都加铺垫`、`请用 Markdown
    # 排表格`。派活判据在这一格是按构造错的——它要挡的「一次性活儿」和偏好共用同一个句型，
    # 分开靠的是线索词（`别再用` / `下次不要` / `请用` 都在 prefs 线索里），不是句型。
    # 问句判据留着：`以后叫我什么好呢` 有线索也不是偏好。
    if cell_id != "prefs" and _looks_like_task(text):
        return False
    return True


def _filter_decided(decided: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(cell, line) for cell, line in decided if _keep_decided_line(cell, line)]


def _live_judge_allowed() -> bool:
    flag = (os.environ.get("WITTY_MEMORY_JUDGE") or "").strip()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag == "1":
        return True
    import sys

    if "unittest" in sys.modules:
        return False
    from witty_agent.runtime import model_settings

    return bool(str(model_settings().get("api_key") or "").strip())


async def _amodel_judge(
    leftover: list[str],
    user_text: str,
    settings: MemorySettings,
) -> list[tuple[str, str]]:
    allowed = {cell.id for cell in settings.cells}
    catalog = "\n".join(f"- {cell.id}：{cell.description or cell.title}" for cell in settings.cells)
    numbered = "\n".join(f"{index + 1}. {line}" for index, line in enumerate(leftover))
    system = get_prompt("memory_judge_system", cells=catalog)
    user = get_prompt("memory_judge_user", text=user_text[:800], lines=numbered[:2000])
    try:
        raw = await _ask_judge(system, user)
    except Exception as exc:
        logger.warning("记忆判定失败，未归类句子不落盘 err=%s", exc)
        return []
    decided = _parse_judge(raw, allowed)
    logger.info("记忆判定 kept=%s leftover=%s", len(decided), len(leftover))
    return decided


def _model_judge(
    leftover: list[str],
    user_text: str,
    settings: MemorySettings,
) -> list[tuple[str, str]]:
    """同步包装，只给 `_decide_leftover` 那条不推迟的老路用（脚本、测试）。"""
    return run_sync(_amodel_judge(leftover, user_text, settings), entry="_amodel_judge")


async def _ask_judge(system: str, user: str) -> str:
    from witty_agent.llm import OpenAICompatLLM
    from witty_agent.types import AgentContext, AgentMessage, ModelRef

    llm = OpenAICompatLLM(stream=False, timeout=25, max_tokens=400, retry_attempts=1)
    llm.think_level = "off"
    context = AgentContext(
        system_prompt=system,
        messages=[AgentMessage(role="user", content=user)],
        tools=[],
        workspace_dir="",
        model=ModelRef(provider="openai", model_id=llm.model_id),
        project_id="",
        agent_id="memory-judge",
        session_id="memory-judge",
    )
    message = await llm(context)
    if message.stop_reason == "error":
        raise RuntimeError(message.text() or "judge error")
    return message.text()


def _parse_judge(raw: str, allowed: set[str]) -> list[tuple[str, str]]:
    text = (raw or "").strip()
    if "```" in text:
        start_fence = text.find("```")
        chunk = text[start_fence + 3 :]
        if chunk.lstrip().lower().startswith("json"):
            chunk = chunk.lstrip()[4:]
        end_fence = chunk.find("```")
        if end_fence >= 0:
            chunk = chunk[:end_fence]
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
    decided: list[tuple[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        cell = str(item.get("cell") or "").strip()
        line = str(item.get("text") or "").strip()
        if cell in allowed and line:
            decided.append((cell, line))
    return decided


_NOISE = re.compile(r"^(你好|在吗|谢谢|好的|嗯+|哦+|哈+|ok|okay|测试|/)\s*$", re.I)
# 明说请托：`请` / `帮我` 打头，动词是什么都算派活（`帮我把这两份比一遍` 里 `比` 不在动词表里）。
_ASK_LEAD = re.compile(r"^(?:请|帮我|麻烦|劳驾)")
_STEP_LEAD = re.compile(r"^(?:请|帮我|麻烦|劳驾)?(?:先|再|然后|接着|继续)?")
_EN_TASK_LEAD = re.compile(
    r"^(?:please\s+|just\s+)?(?:then\s+|again\s+)?"
    r"(?:review|read|write|create|implement|refactor|rewrite|migrate|analyze|fix|update|add|check)\b",
    re.IGNORECASE,
)
# 双字动词自己就成句（`分析一下这批 SOE`）；单字动词得跟量词/趋向补语，
# 否则 `改定值`（名词）、`查线记录`（名词）会被当成派活。
_MULTI_VERB = (
    "生成", "创建", "实现", "重构", "重写", "迁移", "审查", "审阅", "分析",
    "总结", "报告", "整理", "核对", "做一份", "做个",
)
_ONE_VERB = "写读看改修查搜列"
# 只收量词，不收趋向补语。`查出来` / `改完` / `写下来` 照样能领一个名词短语
# （`查出来的差异归二次班保管`），跟单字动词裸奔是同一个坑，白搭三条误挡换不来一条命中。
# `下` 要排掉后面跟 `来` 的：否则 `写下来` 会被切成 `写` + 量词 `下`。
_VERB_QUANT = re.compile(r"^(?:一下|一遍|一份|一个|个|下(?!来)|两|三|几)")
# 无条件疑问：正反问（`能不能` / `是不是`）和问号，在句中任何位置都是提问。
_ALWAYS_QUESTION = re.compile(
    r"[?？]|能不能|可不可以|有没有|会不会|是不是|要不要|能否|是否|"
    r"\b(?:what|why|where|which|how|can you|do you)\b",
    re.IGNORECASE,
)
# 句末语气词。`这个能改吗，还是要报批` 里 `吗` 在逗号前，也算。
_FINAL_PARTICLE = re.compile(r"(?:吗|呢)(?=[，,]|$)")
# 疑问词本身不表态：得看它站在哪儿（见 `_is_question`）。
# `哪.` 一把收掉 `哪个/哪台/哪年…`——枚举永远补不全，而句尾窗口已经把 `哪个间隔都得核对`
# 这类挡在外面了。`还是` 是选择问，它是唯一不带疑问标记的问句形状（`叫我老王还是叫我老陈`），
# 同样靠窗口分开陈述用法（`规约是 104 还是 101 得看现场`）。
_QUESTION_WORD = re.compile(r"什么|哪.|多少|为何|为什么|怎[么样]|如何|谁|还是")
# 疑问词站句首也是提问，而且跟句子长短无关。句尾窗口量的是「疑问词离句尾多远」，句子一长，
# 句首的疑问词就掉出窗口：`什么是数据治理`（距句尾 5 词）判对，`什么是数字化审计`（6 词）
# 判错——同一个问题多一个字结论就翻。实测真库里存下来的问句全是这个形状。
# 陈述句不会拿光疑问词开头，唯一的例外是任指（`什么都行`），单独排掉。
# `还是` 不进这张表：句首的 `还是` 是拍板（`还是按老规约跑`），不是选择问。
_QUESTION_HEAD = re.compile(r"^(?:为什么|为何|什么|哪.|多少|怎[么样]|如何|谁)")
# 任指：疑问词后面在**同一分句里**跟着 `都/也`，说的是「全都」不是在问——`什么都行` /
# `谁都可以` / `多少年前的老图纸都还留着`。窗口不能设上限：`都` 离疑问词能隔很远（那句里隔了
# 6 个字），设了上限就把它当提问，而 `_worth_keeping` 兼作保留判据，误判会把已存的条目抹掉。
# 分句边界要守住，跨逗号的 `都` 是另一件事的。`为什么/为何` 不列进来：它们问原因，没有任指
# 用法，而 `为什么都要报备` 是真问句，列进来就把它放跑了。
_ANY_REFERENT = re.compile(r"^(?:什么|哪.|多少|怎么|谁)[^，,]*[都也]")
# 陈述框架：疑问词在这些词后面是被引住的，不提问。
_DECLARATIVE_FRAME = re.compile(r"不管|无论|不论|搞清楚|弄清楚|想清楚|查明|就是为什么|原因是|是因为")
_TO_AGENT = re.compile(
    r"^你(?:现在|到底|是不是|会不会|能不能|有没有)?"
    r"(?:能|会|有|是|要|不会|可以)"
)
_TAIL_TRIM = re.compile(r"[。.！!…，,\s]+$")
# 窗口按「词」量，不按字符量：`104` / `IEC 61850` / `//nas/dispatch/` 各算一个词，
# 否则一个型号就能把疑问词推出窗口（`用 104 还是 101 规约` 里 `还是` 距句尾 7 字）。
_ASCII_RUN = re.compile(r"[0-9A-Za-z][0-9A-Za-z./_%:-]*\s*")
# 疑问词离句尾多远还算提问。实测 4~8 词这段是平的（3 词会漏 `五防逻辑该怎么改`），取中间值。
_QUESTION_TAIL = 6
_IMPERATIVE = re.compile(r"(?:开始做|你自己定|剩下的你|直接做吧|赶紧做)|(?:吧)$")
_KEEP_FLOOR = 8

# —— 判官跑不起来时认 domain/assets 的结构判据（见 `_structural_leftover`）——
# 路径或文件名：说的是「东西在哪、叫什么」。只认带分隔符的路径和带扩展名的文件名，
# 不认 `共享盘` / `档案室` 这类泛指——那些词在派活句里一样多，认了就等于没判据。
_HAS_LOCATOR = re.compile(
    r"(?://|\\\\)[\w./-]+"                      # //nas/dispatch/points/
    r"|[A-Za-z]:[/\\][\w./\\-]+"                # D:/check/result.xlsx
    r"|[\w-]+\.(?:xlsx?|docx?|pptx?|pdf|csv|json|ya?ml|toml|md|txt|db|sql|zip|tar|gz|log|ini|cfg)\b",
    re.IGNORECASE,
)
# 泛化断言：说的是一般规律，不是这一回。`一般`/`默认` 要带后续词，否则 `默认用` 这种
# 偏好句（已归 prefs cue）也会撞上。
_GENERALIZES = re.compile(r"通常|一般是|一般都|默认是|默认走|大多|往往")
# 话头词 + 是：给自己的判断或计划起头，不是陈述领域规律。
_TOPIC_LEAD = re.compile(
    r"(?:重点|结论|难点|想法|打算|关键|问题|标准|要求|最急的|最麻烦的|第一步|下一步|目标|原则)"
    r"(?:就)?是"
)
# 系动词。要求 `是` 前面不是空白/逗号，避免把 `我看，是这样` 这类口语当断言。
_COPULA = re.compile(r"[^\s，,]是")
# 规程语气：义务落在**事物该是什么样**上，说的是长期规矩——`点号必须一致` / `软压板不许
# 远方投退` / `时钟应当同源`。规程的母语就是这几个词，而 `config/memory.toml` 的
# constraints 线索里本来就有 `禁止` / `必须先` / `未经批准`——配置早就认为义务词标记的是
# 红线。这张表把 `必须` / `不许` / `不准` / `应当` / `一律` 补齐到同一含义上。
_RULE_MODAL = re.compile(r"必须|不许|不准|禁止|应当|一律")
# 派活标记：义务落在**现在去做哪件事**上。`得先` / `要先` 是给手上这摊活儿排顺序，
# `把 X 动词` 是处置式——两者都是一次性的。`把.{0,12}` 的窗口按折过 ASCII 的串量，
# 否则一条路径就能把动词挤出去（`把 fw_logic.db 备份` 中间隔 13 个字符、2 个词）。
_TASK_MARKER = re.compile(r"得先|要先|要把|把.{0,12}(?:补|导|改|理|备份|核对|汇总|扫描)")
# 一次性场合词：`这次` / `本次` / `月底前` 这类落不到具体日期上，`extract_dated_events`
# 认不出来，可说的同样是这一回。只这几个词，不铺开——常驻格宁可漏收。
_ONE_TIME = re.compile(r"这次|本次|这回|这批|眼下|月底前|周前|当天")


def _is_question(text: str) -> bool:
    """这句是在提问，还是只是句子里带了个疑问词。

    此前一见疑问词就判提问，于是陈述句里的疑问词全被当成提问：`不管多少次遥控都要先
    报备`（红线）、`这就是为什么我们不用自动生成的点表`（领域事实）。而 `_worth_keeping`
    既是**入口**也是 `scrub_transient_domain` 的**保留判据**——所以误判不只是不收，
    是把已经记住的条目在下一轮抹掉。实测两条正则合起来误挡耐久事实 10/12。

    分四段：正反问和问号无条件算提问；句末 `吗/呢` 算提问（框架否决也压不过它）；框架否决；
    过了否决之后，疑问词站**句首**算提问（任指除外），其余疑问词只在**句尾附近**才算——做定语
    或被 `不管 / 搞清楚 / 原因是` 引住时落在句中。

    句首那段是后补的：只看句尾等于假定问句总把疑问词放在末尾，可中文两头都问——`数字化审计
    是什么` 和 `什么是数字化审计` 是同一个问题，改前只挡得住前者。实测漏挡 8/11。
    """
    if _ALWAYS_QUESTION.search(text):
        return True
    body = _TAIL_TRIM.sub("", text)
    if _FINAL_PARTICLE.search(body):
        return True
    if _DECLARATIVE_FRAME.search(text):
        return False
    # 句首判据必须排在陈述框架否决**后面**：框架词并不都站句首，`为什么会不一致，原因是双位置`
    # 的 `原因是` 在后半句，疑问词照样占着句首。排前面的话这句就被判成提问——它是领域事实，
    # 而 `_worth_keeping` 兼作 `scrub_transient_domain` 的保留判据，判错等于把它从库里抹掉。
    if _QUESTION_HEAD.match(body) and not _ANY_REFERENT.match(body):
        return True
    body = _ASCII_RUN.sub("〇", body)
    return any(len(body) - match.end() < _QUESTION_TAIL for match in _QUESTION_WORD.finditer(body))


def _looks_like_task(text: str) -> bool:
    """这句是在派活，还是只是拿动词字开头。

    此前是「动词字打头即派活」，于是 `改定值必须先走调度许可`、`查线记录归二次班保管`
    这类以动名词开头的陈述句被当成派活扔掉（实测 5/12）。派活是有形状的：明说请托、
    或双字动词打头、或单字动词后面跟量词（`写个脚本` / `看下这个点表`）。
    """
    if _EN_TASK_LEAD.match(text) or _ASK_LEAD.match(text):
        return True
    rest = text[_STEP_LEAD.match(text).end() :]
    if rest.startswith(_MULTI_VERB):
        return True
    return bool(rest[:1] and rest[0] in _ONE_VERB and _VERB_QUANT.match(rest[1:]))



def _keep_weight(text: str) -> int:
    """ASCII 一字一分，汉字两分。去日期后「旧施工图在柜里」不该当碎片。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _worth_keeping(line: str) -> bool:
    text = strip_bullet_meta(line).strip()
    if _keep_weight(text) < _KEEP_FLOOR or _NOISE.match(text) or text.startswith("/"):
        return False
    if _is_question(text):
        return False
    if _TO_AGENT.match(text):
        return False
    if _looks_like_task(text) or _IMPERATIVE.search(text):
        return False
    if is_process_line(text):
        return False
    return True


def scrub_transient_domain(directory: Path, settings: MemorySettings | None = None) -> int:
    """把已经误收进领域要点的问句、派活、对助手打听清出去。归档同样洗，不把问句再塞进 archive。"""
    settings = settings or load_memory_settings()
    cell = settings.cell("domain")
    if cell is None:
        return 0
    dropped = 0
    existing = _bullets(topic_body(directory, "domain"))
    keep = [item for item in existing if _worth_keeping(item)]
    drop = [item for item in existing if item not in keep]
    if drop:
        write_topic(
            directory,
            "domain",
            description=cell.description or cell.title,
            body="\n".join(f"- {item}" for item in keep),
        )
        dropped += len(drop)
    dropped += _scrub_archive_domain(directory, settings)
    if dropped:
        rebuild_memory_index(directory, settings=settings)
        logger.info("清掉瞬时领域条目 dropped=%s kept=%s", dropped, len(keep))
    return dropped


def _scrub_archive_domain(directory: Path, settings: MemorySettings) -> int:
    archive = directory / "archive"
    path = archive / "domain.md"
    if not path.is_file():
        return 0
    existing = _bullets(topic_body(archive, "domain"))
    keep = [item for item in existing if _worth_keeping(item)]
    drop = [item for item in existing if item not in keep]
    if not drop:
        return 0
    if keep:
        write_topic(
            archive,
            "domain",
            description="archived domain",
            body="\n".join(f"- {item}" for item in keep),
        )
    else:
        path.unlink()
    del settings
    return len(drop)


def _already_kept(line: str, cells_hit: list[str], tax_hit: list[str], settings: MemorySettings) -> bool:
    if any(cue and cue in line for cues in settings.cues.values() for cue in cues):
        return True
    if parse_pref_line(line, settings)[1]:
        return True
    if any(word and word in line for item in settings.taxonomy for word in item.keywords):
        return True
    del cells_hit, tax_hit
    return False
