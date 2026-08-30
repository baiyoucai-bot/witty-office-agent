"""偏好槽位：同槽替换、明说作废。沉默不删。规则来自 memory.toml。"""

from __future__ import annotations

import re
from pathlib import Path

from witty_agent.memory import (
    _CITE_TAIL,
    _DATE_PREFIX,
    _archive_bullets,
    _bullets,
    _clean_bullet,
    _fact_key,
    _merge_fact,
    _should_skip,
    topic_body,
    write_topic,
)
from witty_agent.memory_config import MemorySettings, load_memory_settings

_TRAIL = re.compile(r"[了啦的吧啊呢]+$")


def strip_bullet_meta(text: str) -> str:
    """剥掉收割元数据（日期前缀 + `[cite:…]` 尾巴），只留用户那句话。

    此前只剥日期，于是从盘上读回来的偏好，`parse_pref_line` 抠出的 value 尾巴上还挂着
    `[cite:s1#1]`——两句本该判成同一件事的偏好因为 cite 不同而对不上。
    """
    return _CITE_TAIL.sub("", _DATE_PREFIX.sub("", (text or "").lstrip("- ").strip(), count=1)).strip()


def is_process_line(line: str, settings: MemorySettings | None = None) -> bool:
    settings = settings or load_memory_settings()
    text = strip_bullet_meta(line)
    if not text:
        return False
    for needle in settings.process_needles:
        if needle and needle in text:
            return True
    return False


def _longest_hit(text: str, cues: object) -> str:
    """这堆线索里，在 text 中命中且字面最长的那条。"""
    best = ""
    for cue in cues or ():  # type: ignore[union-attr]
        if cue and cue in text and len(cue) > len(best):
            best = cue
    return best


def parse_pref_line(line: str, settings: MemorySettings | None = None) -> tuple[str, bool, str]:
    """返回 (slot, retract, value)。无槽位时 slot 为空，整句当独立偏好。

    槽位先在**原句**上找，别在抠掉作废词之后找：`pref_retract` 里的 `别再` 是
    `pref_slots.avoid` 里 `别再用` 的前缀，先抠 `别再` 会把 `别再用` 拆散，于是
    `别再用自动生成的点表` 判成「无槽位的作废」——而无槽位作废在 `upsert_pref_bullets`
    里既不写入（`if retract: continue`）又删不掉（没有同槽可比），整条偏好凭空消失。
    同样的意思换个说法 `下次不要用自动生成的点表` 却能进 avoid 槽。

    命中的槽位线索包含了作废线索时，按更长的那条读——也就是当槽位赋值，不当作废。
    这和格子仲裁是同一条规矩：字面更长的线索是写给更窄的情形的。`改成` 不是任何槽位
    线索的子串，所以 `以后改成叫我老李` 仍是「作废旧值 + 写新值」，行为不变。

    槽位之间**不比长短**，按配置里的槽位顺序取第一个命中的：`以后都叫我老王` 里
    `以后都`（habit）比 `叫我`（address）长，可这句说的是怎么称呼，address 在前是
    配置作者定的优先级，不能拿字面长度盖过去。长短只在同一个槽位内部用。
    """
    settings = settings or load_memory_settings()
    text = strip_bullet_meta(line)
    retract_cue = _longest_hit(text, settings.pref_retract)
    slot = ""
    slot_cue = ""
    for name, cues in settings.pref_slots.items():
        hit = _longest_hit(text, cues)
        if hit:
            slot, slot_cue = name, hit
            break
    retract = bool(retract_cue) and not (slot_cue and retract_cue in slot_cue)
    value = text
    if slot_cue:
        value = value.replace(slot_cue, " ", 1)
    if retract and retract_cue in value:
        value = value.replace(retract_cue, " ", 1)
    value = _TRAIL.sub("", re.sub(r"\s+", " ", value).strip(" ，,。.;；"))
    return slot, retract, value


def upsert_pref_bullets(
    directory: Path,
    *,
    description: str,
    lines: list[str],
    settings: MemorySettings | None = None,
) -> int:
    settings = settings or load_memory_settings()
    incoming = [_clean_bullet(item) for item in lines]
    incoming = [item for item in incoming if item and not _should_skip(item, settings)]
    if not incoming:
        return 0
    existing = _bullets(topic_body(directory, "prefs"))
    parsed_new = [parse_pref_line(item, settings) for item in incoming]
    drop: list[str] = []
    live: list[str] = []
    for item in existing:
        slot_old, _, value_old = parse_pref_line(item, settings)
        remove = False
        for slot, retract, value in parsed_new:
            if retract:
                if value and value in strip_bullet_meta(item):
                    remove = True
                    break
                # 作废句和被作废句都被 parse 抠掉了线索词，只能值对值比。
                # 此前一路比的是 `value in item` 原文——而 `replace(cue, " ")` 会在
                # 中间留个空格，所以「我不吃辣了」抠出的 `我 辣` 永远不是
                # 「我喜欢吃辣。」的子串；而它 slot 为空，同槽那条分支也走不到。
                # 结果：**两边抠出来一模一样（都是 `我 辣`）却判成两回事**，偏好作废
                # 对所有不带槽位线索的作废句都是空转。
                if value and value_old and (value in value_old or value_old in value):
                    remove = True
                    break
            elif slot and slot_old == slot:
                remove = True
                break
        if remove:
            drop.append(item)
        else:
            live.append(item)
    added = 0
    for line, (slot, retract, _value) in zip(incoming, parsed_new, strict=True):
        if retract:
            continue
        # 没有槽位线索的偏好（`表格一律用 Markdown` 不匹配任何 pref_slots cue，
        # 也就是大多数）此前只比整串，而每条都盖了当天日期 → 隔天再说就是新行。
        # prefs 又是常驻格、不衰减，于是同一条偏好能把 12 个槽全占满。
        merged = _merge_fact(live, line) if _fact_key(line) else live
        if merged != live:
            added += 1
        live = merged
        del slot
    if added or drop:
        cap = settings.working_set
        spilled = live[:-cap] if len(live) > cap else []
        keep = live[-cap:]
        write_topic(
            directory,
            "prefs",
            description=description,
            body="\n".join(f"- {item}" for item in keep),
        )
        archived = [*drop, *spilled]
        if archived:
            _archive_bullets(directory, "prefs", archived, settings.archive_cap)
    return added + len(drop)
