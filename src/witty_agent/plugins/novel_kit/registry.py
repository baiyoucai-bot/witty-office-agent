"""把 records.jsonl 折叠成「截至第 N 章」的当前状态。

registry 是纯派生物：删了随时能重建，所以改稿只要截断事实源再 fold 一次。
折叠按 (章号, 文件顺序) 稳定重放，同章内后写的覆盖先写的。

`knows` 存成 事实 -> 首次知道的章号，不是列表。检查「某角色第 30 章还不该知道
这件事」需要的是章号，丢了章号这条规则就没法写。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from witty_agent.plugins.novel_kit.records import load_records, story_ch

REGISTRY_VERSION = 1


#: 会被当作「人名」解析的字段。别名归一必须覆盖全部，漏一个就等于状态分裂。
NAME_FIELDS = ("who", "owner", "pov")
NAME_LIST_FIELDS = ("participants", "pair")


def pair_key(pair: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(pair))


def build_alias_map(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """别名 -> 正名。返回映射和冲突清单。

    中文长篇尤其躲不开这个：本名、字、号、绰号、尊称、外号，同一个人五六种叫法。
    不归一的话「沈砚」和「沈公子」在库里就是两个人——知道的事各记一半，
    出场次数各算一半，`absent_character` 和 `knowledge_regression` 全部失准，
    而且**不报错**，只是悄悄给出错的答案。这比报错危险得多。

    映射先扫全量再折叠，所以第 50 章才交代「沈公子就是沈砚」，前 49 章也会并过去。
    """
    direct: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []
    for item in sorted(rows, key=lambda row: row["ch"]):
        if item["type"] != "character_state":
            continue
        canonical = item["who"]
        for alias in item.get("aka", []):
            previous = direct.get(alias)
            if previous is not None and previous != canonical:
                collisions.append((alias, previous, canonical))
                continue
            direct[alias] = canonical

    resolved: dict[str, str] = {}
    for alias in direct:
        seen = {alias}
        target = direct[alias]
        # 别名可以套别名（甲 aka 乙、乙 aka 丙），一路走到头；环就地停住，别死循环。
        while target in direct and target not in seen:
            seen.add(target)
            target = direct[target]
        if target != alias:
            resolved[alias] = target
    return resolved, collisions


def canonicalize(item: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    if not aliases:
        return item
    out = dict(item)
    for name in NAME_FIELDS:
        if name in out:
            out[name] = aliases.get(out[name], out[name])
    for name in NAME_LIST_FIELDS:
        if name in out:
            renamed = [aliases.get(who, who) for who in out[name]]
            out[name] = sorted(set(renamed)) if name == "pair" else list(dict.fromkeys(renamed))
    return out


@dataclass
class Registry:
    through: int = 0
    characters: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    world_facts: list[dict[str, Any]] = field(default_factory=list)
    chapters: dict[int, dict[str, Any]] = field(default_factory=dict)
    scenes: list[dict[str, Any]] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    alias_collisions: list[tuple[str, str, str]] = field(default_factory=list)

    def chapter_numbers(self) -> list[int]:
        return sorted(self.chapters)

    def open_threads(self) -> list[dict[str, Any]]:
        return [item for item in self.threads.values() if item.get("status") == "open"]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "through": self.through,
            "characters": self.characters,
            "relationships": [dict(item) for item in self.relationships.values()],
            "objects": self.objects,
            "threads": self.threads,
            "world_facts": self.world_facts,
            "chapters": {str(key): value for key, value in self.chapters.items()},
            "scenes": self.scenes,
            "aliases": self.aliases,
        }


def _touch(entry: dict[str, Any], chapter: int) -> None:
    entry["first_ch"] = min(entry.get("first_ch", chapter), chapter)
    entry["last_ch"] = max(entry.get("last_ch", chapter), chapter)


def _apply_character(reg: Registry, item: dict[str, Any]) -> None:
    who = item["who"]
    chapter = item["ch"]
    entry = reg.characters.setdefault(
        who, {"who": who, "status": "alive", "knows": {}, "unknowns": {}, "traits": {}}
    )
    _touch(entry, chapter)
    entry.setdefault("traits", {}).update(item.get("traits", {}))
    for name in ("status", "location", "goal"):
        if name in item:
            entry[name] = item[name]
            if name == "status":
                # 死讯按故事时间记。倒叙里「他还活着」说的是更早的时间点，不能翻案。
                entry["status_ch"] = story_ch(item)
    for fact in item.get("knows", []):
        entry["knows"].setdefault(fact, story_ch(item))
        entry["unknowns"].pop(fact, None)
    for fact in item.get("unknowns", []):
        if fact not in entry["knows"]:
            entry["unknowns"].setdefault(fact, story_ch(item))


def _apply_relationship(reg: Registry, item: dict[str, Any]) -> None:
    key = pair_key(item["pair"])
    entry = reg.relationships.setdefault(key, {"pair": list(key)})
    entry.update(
        {
            name: item[name]
            for name in ("polarity", "kind", "summary", "valid_from", "valid_to")
            if name in item
        }
    )
    entry["ch"] = item["ch"]


def _apply_thread(reg: Registry, item: dict[str, Any]) -> None:
    thread_id = item["id"]
    chapter = item["ch"]
    entry = reg.threads.setdefault(
        thread_id, {"id": thread_id, "status": "open", "opened_ch": chapter}
    )
    _touch(entry, chapter)
    if "summary" in item:
        entry["summary"] = item["summary"]
    if "due_ch" in item:
        entry["due_ch"] = item["due_ch"]
    role = item.get("role")
    status = item.get("status")
    if role == "payoff" or status == "closed":
        entry["status"] = "closed"
        entry["closed_ch"] = chapter
    elif status == "open":
        entry["status"] = "open"
        entry.pop("closed_ch", None)


def _apply_object(reg: Registry, item: dict[str, Any]) -> None:
    name = item["object"]
    entry = reg.objects.setdefault(name, {"object": name})
    _touch(entry, item["ch"])
    entry.setdefault("traits", {}).update(item.get("traits", {}))
    for key in ("owner", "location", "condition"):
        if key in item:
            entry[key] = item[key]


def _apply_scene(reg: Registry, item: dict[str, Any]) -> None:
    reg.scenes.append(dict(item))
    for who in item.get("participants", []):
        entry = reg.characters.setdefault(
            who, {"who": who, "status": "alive", "knows": {}, "unknowns": {}, "traits": {}}
        )
        _touch(entry, item["ch"])


def fold(records: Iterable[dict[str, Any]], *, through: int | None = None) -> Registry:
    """双时间轴重放。

    截断看 `ch`（叙述章）——写第 N+1 章时只能用已经写出来的东西。
    排序看 `occurred_ch`（故事时间）——决定故事里哪件事覆盖哪件事。
    两根轴混用会让倒叙算错：第 500 章回忆里的「他还活着」会顶掉第 100 章的死讯。
    """
    rows = [item for item in records if through is None or item["ch"] <= through]
    rows.sort(key=lambda item: (story_ch(item), item["ch"]))
    aliases, collisions = build_alias_map(rows)
    rows = [canonicalize(item, aliases) for item in rows]
    reg = Registry()
    reg.aliases = aliases
    reg.alias_collisions = collisions
    for item in rows:
        kind = item["type"]
        if kind == "chapter_digest":
            reg.chapters[item["ch"]] = dict(item)
        elif kind == "scene":
            _apply_scene(reg, item)
        elif kind == "character_state":
            _apply_character(reg, item)
        elif kind == "relationship":
            _apply_relationship(reg, item)
        elif kind == "object_state":
            _apply_object(reg, item)
        elif kind == "thread":
            _apply_thread(reg, item)
        elif kind == "world_fact":
            reg.world_facts.append(dict(item))
    reg.through = through if through is not None else max((item["ch"] for item in rows), default=0)
    return reg


def load_registry(records_path: Path, *, through: int | None = None) -> Registry:
    return fold(load_records(records_path), through=through)


def write_registry(path: Path, reg: Registry) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(reg.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return target
