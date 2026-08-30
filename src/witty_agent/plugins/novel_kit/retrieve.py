"""查询条件化检索：把状态库切成一小片「章节安全」的证据包。

这一层是整套东西里最值钱的部分。NWM 的对照实验里，同一份状态全量序列化塞给
模型只有 0.358 的多跳准确率，改成按查询条件检索是 0.898；八成的漏答是「证据在
包里但被截断了」。所以默认不是「把状态都给你」，而是「按这次要写的东西挑」。

排序用 BM25 而不是 IDF 简单叠加：叙事记录长短悬殊（一条 `chapter_digest` 摘要
可能是一条 `thread` 的十倍），不做长度归一化的话长记录永远排前面，挤掉真正相关
的短记录。TF 饱和同理——某个名字在一条记录里出现五次，不该是出现一次的五倍分。

因果截断在 `chapter` 参数上，且只看叙述章 `ch`：写第 N+1 章时看不到还没写出来的
东西。故事时间 `occurred_ch` 只影响状态折叠，不影响可见性。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from witty_agent.plugins.novel_kit.registry import Registry, fold
from witty_agent.prompts import get_prompt

_WORD = re.compile(r"[a-z0-9_]{2,}")
_HAN = re.compile(r"[\u4e00-\u9fff]+")

DEFAULT_BUDGET = 6000
DEFAULT_HOPS = 1
RECENT_CHAPTERS = 2
DUE_SOON = 10
NEEDLE_LIMIT = 5

# BM25 的两个常数用通行默认值。这是算法参数不是业务策略，所以不进 [novel] 配置：
# 调它们要有检索评测撑腰，不该让用户在没有度量的情况下拧。
BM25_K1 = 1.5
BM25_B = 0.75


def _tokens(text: str) -> set[str]:
    """词面切分。中文切双字，和 plugins/llmwiki、plugins/nl2sql 的口径一致，不引入 jieba。"""
    return set(_token_counts(text))


def _token_counts(text: str) -> Counter[str]:
    raw = str(text or "")
    counts: Counter[str] = Counter(_WORD.findall(raw.lower()))
    for run in _HAN.findall(raw):
        if len(run) == 1:
            counts[run] += 1
        else:
            counts.update(run[index : index + 2] for index in range(len(run) - 1))
    return counts


def record_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in item.items():
        if key in {"type", "ch", "occurred_ch"}:
            continue
        if isinstance(value, list):
            parts.extend(str(entry) for entry in value)
        else:
            parts.append(str(value))
    return " ".join(parts)


@dataclass
class Bm25Index:
    docs: list[dict[str, Any]] = field(default_factory=list)
    freqs: list[Counter[str]] = field(default_factory=list)
    df: dict[str, int] = field(default_factory=dict)
    avgdl: float = 1.0

    def idf(self, token: str) -> float:
        total = len(self.docs) or 1
        seen = self.df.get(token, 0)
        return math.log(1.0 + (total - seen + 0.5) / (seen + 0.5))

    def rank(self, query: str) -> list[tuple[float, dict[str, Any]]]:
        needles = _tokens(query)
        if not needles or not self.docs:
            return []
        out: list[tuple[float, dict[str, Any]]] = []
        for index, counts in enumerate(self.freqs):
            length = sum(counts.values()) or 1
            norm = BM25_K1 * (1 - BM25_B + BM25_B * length / self.avgdl)
            score = 0.0
            for token in needles:
                freq = counts.get(token, 0)
                if not freq:
                    continue
                score += self.idf(token) * freq * (BM25_K1 + 1) / (freq + norm)
            if score > 0:
                out.append((score, self.docs[index]))
        out.sort(key=lambda pair: (-pair[0], pair[1]["ch"]))
        return out


def build_index(records: Iterable[dict[str, Any]]) -> Bm25Index:
    docs = list(records)
    freqs = [_token_counts(record_text(item)) for item in docs]
    df: dict[str, int] = {}
    for counts in freqs:
        for token in counts:
            df[token] = df.get(token, 0) + 1
    lengths = [sum(counts.values()) for counts in freqs]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    return Bm25Index(docs=docs, freqs=freqs, df=df, avgdl=max(avgdl, 1.0))


def anchor_characters(reg: Registry, query: str, index: Bm25Index, limit: int = 6) -> list[str]:
    """查询里点到名的角色。图扩展从这里出发。"""
    needles = _tokens(query)
    if not needles:
        return []
    ranked: list[tuple[float, str]] = []
    for who in reg.characters:
        hits = needles & _tokens(who)
        if not hits:
            continue
        ranked.append((sum(index.idf(token) for token in hits), who))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]))
    return [who for _, who in ranked[:limit]]


def expand(
    reg: Registry, anchors: Iterable[str], *, hops: int = DEFAULT_HOPS
) -> tuple[list[dict[str, Any]], list[str]]:
    """沿关系图做 BFS 扩展，返回途经的边和新拉进来的人。

    默认一跳。跳数是成本换召回：每多一跳，包里的人可能翻几倍，而预算是固定的，
    挤掉的往往是更相关的近邻。要多跳的场景（群像戏、势力交锋）用 [novel].expand_hops 调。
    """
    seen = set(anchors)
    frontier = set(seen)
    edges: list[dict[str, Any]] = []
    added: list[str] = []
    for _ in range(max(0, hops)):
        nxt: set[str] = set()
        for key, entry in reg.relationships.items():
            touching = frontier & set(key)
            if not touching:
                continue
            if entry not in edges:
                edges.append(entry)
            for who in set(key) - seen:
                nxt.add(who)
        if not nxt:
            break
        added.extend(sorted(nxt))
        seen |= nxt
        frontier = nxt
    return edges, added


def _character_row(entry: dict[str, Any]) -> str:
    knows = "、".join(sorted(entry.get("knows", {}))) or "-"
    unknowns = "、".join(sorted(entry.get("unknowns", {}))) or "-"
    # 恒定特征必须进包。眼睛颜色、疤痕这类东西一旦写岔，读者比谁都先发现，
    # 而模型没有它们就只能现编——ConStory 把这类列为主导失败模式之一。
    traits = "、".join(f"{name}={value}" for name, value in sorted(entry.get("traits", {}).items()))
    return get_prompt(
        "novel_pack_row_character",
        who=entry.get("who", "-"),
        status=entry.get("status", "-"),
        location=entry.get("location", "-"),
        goal=entry.get("goal", "-"),
        knows=knows,
        unknowns=unknowns,
        traits=traits or "-",
        last_ch=entry.get("last_ch", "-"),
    )


def _thread_row(entry: dict[str, Any]) -> str:
    return get_prompt(
        "novel_pack_row_thread",
        thread_id=entry.get("id", "-"),
        status=entry.get("status", "-"),
        summary=entry.get("summary", "-"),
        opened_ch=entry.get("opened_ch", "-"),
        due_ch=entry.get("due_ch", "-"),
        last_ch=entry.get("last_ch", "-"),
    )


def _section(title_key: str, rows: list[str]) -> str:
    if not rows:
        return ""
    return get_prompt("novel_pack_section", title=get_prompt(title_key), rows="\n".join(rows))


def context_pack(
    records: Iterable[dict[str, Any]],
    *,
    chapter: int,
    query: str = "",
    budget: int = DEFAULT_BUDGET,
    hops: int = DEFAULT_HOPS,
) -> str:
    """组装第 `chapter` 章可见的证据包。按优先级填，填不下就截断并说明。

    优先级刻意不是「按时间倒序」：最近两章的梗概保证接得上，未回收伏笔保证不丢线，
    然后才是查询点到的人和物。全量倾倒会把预算浪费在与本章无关的状态上。
    """
    safe = [item for item in records if item["ch"] <= chapter]
    reg = fold(safe, through=chapter)
    index = build_index(safe)
    anchors = anchor_characters(reg, query, index)
    edges, neighbours = expand(reg, anchors, hops=hops)
    focus = list(dict.fromkeys([*anchors, *neighbours]))

    recent = [
        get_prompt(
            "novel_pack_row_chapter",
            ch=number,
            title=reg.chapters[number].get("title", "-"),
            summary=reg.chapters[number].get("summary", "-"),
        )
        for number in reg.chapter_numbers()[-RECENT_CHAPTERS:]
    ]

    threads = sorted(
        reg.open_threads(),
        key=lambda item: (item.get("due_ch") or 10**6, item.get("last_ch") or 0),
    )

    def _is_urgent(item: dict[str, Any]) -> bool:
        return item.get("due_ch") is None or int(item["due_ch"]) <= chapter + DUE_SOON

    urgent = [_thread_row(item) for item in threads if _is_urgent(item)]
    other_threads = [_thread_row(item) for item in threads if not _is_urgent(item)]

    people = [_character_row(reg.characters[who]) for who in focus if who in reg.characters]
    if not people:
        recent_people = sorted(
            reg.characters.values(), key=lambda item: -(item.get("last_ch") or 0)
        )
        people = [_character_row(entry) for entry in recent_people[:5]]

    bonds = [
        get_prompt(
            "novel_pack_row_relationship",
            pair="↔".join(entry.get("pair", [])),
            polarity=entry.get("polarity", "-"),
            kind=entry.get("kind", "-"),
            summary=entry.get("summary", "-"),
        )
        for entry in edges
    ]

    query_tokens = _tokens(query)
    objects = [
        get_prompt(
            "novel_pack_row_object",
            name=entry.get("object", "-"),
            owner=entry.get("owner", "-"),
            location=entry.get("location", "-"),
            condition=entry.get("condition", "-"),
        )
        for entry in reg.objects.values()
        if not focus or entry.get("owner") in focus or query_tokens & _tokens(entry.get("object", ""))
    ]

    world = [
        get_prompt("novel_pack_row_world", fact=entry.get("fact", "-"), scope=entry.get("scope", "-"))
        for entry in reg.world_facts
        if entry.get("valid_to") is None or entry["valid_to"] >= chapter
    ]

    needles = [
        get_prompt("novel_pack_row_needle", ch=item["ch"], kind=item["type"], text=record_text(item))
        for _, item in index.rank(query)[:NEEDLE_LIMIT]
    ]

    blocks = [
        _section("novel_pack_title_recent", recent),
        _section("novel_pack_title_threads", urgent),
        _section("novel_pack_title_characters", people),
        _section("novel_pack_title_relationships", bonds),
        _section("novel_pack_title_objects", objects),
        _section("novel_pack_title_world", world),
        _section("novel_pack_title_needles", needles),
        _section("novel_pack_title_threads_other", other_threads),
    ]

    head = get_prompt("novel_pack_header", chapter=chapter)
    out = [head]
    used = len(head)
    dropped = 0
    for block in blocks:
        if not block:
            continue
        if used + len(block) > budget:
            dropped += 1
            continue
        out.append(block)
        used += len(block)
    if dropped:
        out.append(get_prompt("novel_pack_truncated", count=dropped, budget=budget))
    if len(out) == 1:
        out.append(get_prompt("novel_pack_empty"))
    return "\n".join(out)
