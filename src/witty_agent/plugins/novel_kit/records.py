"""类型化叙事记录：append-only 的事实源。

七类记录的字段抄 Narrative World Model 那套叙事学分型，不用通用实体-关系三元组。
`knows` / `unknowns` 两个字段不能省——「某角色在第几章之前不该知道某事」是长篇
穿帮的头号来源，而通用知识图谱里根本没有这个槽位。

校验是严格的：字段名打错直接报错，不静默丢弃。长篇里一条静默丢掉的记录，
要到几百章后才会以「人物凭空知道某事」的形式暴露出来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

CHAPTER_FIELDS = ("ch", "occurred_ch", "due_ch", "valid_from", "valid_to", "words")

CHARACTER_STATUS = ("alive", "dead", "missing", "unknown")
POLARITY = ("ally", "hostile", "neutral", "romantic", "kin")
THREAD_ROLE = ("setup", "advance", "payoff")
THREAD_STATUS = ("open", "closed")

#: 每类记录的 required / optional 字段。`type`、`ch`、`evidence` 是所有类型的公共字段。
RECORD_TYPES: dict[str, dict[str, tuple[str, ...]]] = {
    "chapter_digest": {
        "required": ("summary",),
        "optional": ("title", "pov", "words"),
    },
    "scene": {
        "required": ("location", "participants"),
        "optional": ("summary", "event_order", "reveal_order", "pov"),
    },
    "character_state": {
        "required": ("who",),
        "optional": ("status", "location", "goal", "knows", "unknowns", "aka", "traits"),
    },
    "relationship": {
        "required": ("pair",),
        "optional": ("polarity", "kind", "valid_from", "valid_to", "summary"),
    },
    "object_state": {
        "required": ("object",),
        "optional": ("owner", "location", "condition", "traits"),
    },
    "thread": {
        "required": ("id",),
        "optional": ("role", "status", "due_ch", "summary"),
    },
    "world_fact": {
        "required": ("fact",),
        "optional": ("scope", "valid_from", "valid_to"),
    },
}

#: 双时间轴。`ch` 是**叙述章**（第几章写到的），`occurred_ch` 是**故事时间**（第几章的
#: 时间点上发生的），缺省相等。倒叙必须靠这两根轴分开才不会算错：第 500 章的一段回忆
#: 写到某人活着，说的是故事时间第 3 章的事，不该把他在第 100 章的死讯覆盖掉。
#: 因果截断永远看 `ch`（写第 N+1 章时只能用已经写出来的东西），
#: 状态折叠永远看 `occurred_ch`（故事里哪件事在后面发生）。
COMMON_OPTIONAL = ("evidence", "occurred_ch")

LIST_FIELDS = frozenset({"participants", "knows", "unknowns", "pair", "aka"})

#: `traits` 是**恒定特征**：眼睛颜色、疤痕、身高、口音这些「变了就是穿帮」的东西。
#: 会随剧情合理变化的（境界、伤势、发型）不要往这里放，放了就等着被规则天天报。
#: 真要改，用 `dismiss` 记一笔——那正是豁免机制存在的意义。
DICT_FIELDS = frozenset({"traits"})
INT_FIELDS = frozenset(CHAPTER_FIELDS)
CHOICES: dict[tuple[str, str], tuple[str, ...]] = {
    ("character_state", "status"): CHARACTER_STATUS,
    ("relationship", "polarity"): POLARITY,
    ("thread", "role"): THREAD_ROLE,
    ("thread", "status"): THREAD_STATUS,
}


class RecordError(ValueError):
    """记录不合规。带上行号，好让 ingest 直接指到出错那行。"""


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    return [text for text in (_clean_str(item) for item in items) if text]


def _clean_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        name = _clean_str(key)
        text = _clean_str(item)
        if name and text:
            out[name] = text
    return out


def allowed_fields(record_type: str) -> set[str]:
    spec = RECORD_TYPES[record_type]
    return {"type", "ch", *COMMON_OPTIONAL, *spec["required"], *spec["optional"]}


def validate(raw: Any) -> list[str]:
    """返回错误清单；空列表表示合规。不抛异常，方便批量校验后一次性报。"""
    if not isinstance(raw, dict):
        return ["记录必须是 JSON 对象"]
    errors: list[str] = []
    record_type = _clean_str(raw.get("type"))
    if not record_type:
        return ["缺少 type"]
    if record_type not in RECORD_TYPES:
        return [f"未知 type {record_type!r}，可用: {', '.join(sorted(RECORD_TYPES))}"]

    chapter = raw.get("ch")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        errors.append("ch 必须是 >= 1 的整数")

    known = allowed_fields(record_type)
    for key in raw:
        if key not in known:
            errors.append(f"{record_type} 不认识字段 {key!r}，可用: {', '.join(sorted(known))}")

    for field in RECORD_TYPES[record_type]["required"]:
        value = raw.get(field)
        if field in LIST_FIELDS:
            if not _clean_list(value):
                errors.append(f"{record_type}.{field} 不能为空")
        elif not _clean_str(value):
            errors.append(f"{record_type}.{field} 不能为空")

    for field, value in raw.items():
        if field in INT_FIELDS and field != "ch" and value is not None:
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{record_type}.{field} 必须是整数")
        if field in DICT_FIELDS and value is not None and not isinstance(value, dict):
            errors.append(f"{record_type}.{field} 必须是「特征名: 取值」的对象")
        choices = CHOICES.get((record_type, field))
        if choices and _clean_str(value) and _clean_str(value) not in choices:
            errors.append(f"{record_type}.{field} 只能是 {', '.join(choices)}")

    if record_type == "relationship" and len(_clean_list(raw.get("pair"))) != 2:
        errors.append("relationship.pair 必须正好两个角色")
    if record_type == "character_state":
        who = _clean_str(raw.get("who"))
        if who and who in _clean_list(raw.get("aka")):
            errors.append("character_state.aka 不能包含 who 本身")
    return errors


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """校验后规整成统一形状：列表字段一律 list[str]，`valid_from` 缺省等于 `ch`。"""
    errors = validate(raw)
    if errors:
        raise RecordError("；".join(errors))
    record_type = _clean_str(raw["type"])
    chapter = int(raw["ch"])
    item: dict[str, Any] = {"type": record_type, "ch": chapter}
    for field in sorted(allowed_fields(record_type) - {"type", "ch"}):
        if field not in raw or raw[field] is None:
            continue
        if field in LIST_FIELDS:
            value: Any = _clean_list(raw[field])
            if not value:
                continue
        elif field in DICT_FIELDS:
            value = _clean_dict(raw[field])
            if not value:
                continue
        elif field in INT_FIELDS:
            value = int(raw[field])
        else:
            value = _clean_str(raw[field])
            if not value:
                continue
        item[field] = value
    # `occurred_ch` 等于 `ch` 时不落盘：它是派生值，`story_ch()` 会兜底。
    # 1000 章合成书上实测，无脑物化会让 records.jsonl 涨 18%，还把每行 git diff 都撑长。
    if item.get("occurred_ch") == chapter:
        item.pop("occurred_ch")
    if record_type in {"relationship", "world_fact"} and "valid_from" not in item:
        item["valid_from"] = story_ch(item)
    if record_type == "relationship":
        item["pair"] = sorted(item["pair"])
    if "aka" in item:
        item["aka"] = sorted(set(item["aka"]))
    return item


def story_ch(item: dict[str, Any]) -> int:
    """故事时间。老记录没有 `occurred_ch` 时退回叙述章。"""
    return int(item.get("occurred_ch") or item["ch"])


def load_records(path: Path, *, through: int | None = None) -> list[dict[str, Any]]:
    """读 records.jsonl。`through` 是因果截断：写第 N+1 章时只能看到 <= N 的记录。"""
    if not Path(path).is_file():
        return []
    out: list[dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        body = line.strip()
        if not body or body.startswith("//"):
            continue
        try:
            raw = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RecordError(f"{path}:{number} 不是合法 JSON：{exc}") from exc
        try:
            item = normalize(raw)
        except RecordError as exc:
            raise RecordError(f"{path}:{number} {exc}") from exc
        if through is not None and item["ch"] > through:
            continue
        out.append(item)
    return out


def append_records(path: Path, items: Iterable[dict[str, Any]]) -> int:
    """追加写。事实源只增不改，改稿走截断重放。"""
    rows = [normalize(dict(item)) for item in items]
    if not rows:
        return 0
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def truncate_records(path: Path, through: int) -> int:
    """把事实源截断到第 `through` 章，返回删掉的条数。改稿重写下游用这个。"""
    kept = load_records(path, through=through)
    total = len(load_records(path))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kept)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return total - len(kept)


def max_chapter(records: Iterable[dict[str, Any]]) -> int:
    return max((int(item["ch"]) for item in records), default=0)
