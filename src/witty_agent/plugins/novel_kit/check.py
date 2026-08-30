"""确定性一致性校验。不调模型，因此免费、瞬时、可重复。

这一层存在的理由：把「第 30 章死了的人第 143 章又出现」这类问题，从「指望模型
自觉」变成「像编译器报类型错误」。写到第 300 章赶进度时，模型不会想起来自查的。

每条 finding 带一个稳定 key（规则 + 主体身份，不含措辞），豁免按 key 落盘。
一个改几个字就会复活的告警，用户很快就会连真错一起无视——那还不如没有检查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from witty_agent.plugins.novel_kit.layout import BookPaths
from witty_agent.plugins.novel_kit.records import load_records, story_ch
from witty_agent.plugins.novel_kit.registry import Registry, fold
from witty_agent.prompts import get_prompt

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

MOVEMENT_TYPES = frozenset({"thread", "character_state", "object_state", "world_fact", "relationship"})


@dataclass(frozen=True)
class Thresholds:
    dormant_thread_chapters: int = 3
    absent_character_chapters: int = 5
    main_character_min_appearances: int = 3
    stalled_run_chapters: int = 3

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None = None) -> Thresholds:
        data = settings or {}
        return cls(
            dormant_thread_chapters=int(data.get("dormant_thread_chapters", 3)),
            absent_character_chapters=int(data.get("absent_character_chapters", 5)),
            main_character_min_appearances=int(data.get("main_character_min_appearances", 3)),
            stalled_run_chapters=int(data.get("stalled_run_chapters", 3)),
        )


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    key: str
    chapter: int
    message: str

    def line(self) -> str:
        return get_prompt(
            "novel_finding_line",
            severity=self.severity,
            rule=self.rule,
            ch=self.chapter,
            message=self.message,
            key=self.key,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "key": self.key,
            "chapter": self.chapter,
            "message": self.message,
        }


def load_dismissals(path: Path) -> dict[str, dict[str, str]]:
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): dict(value) for key, value in data.items() if isinstance(value, dict)}


def save_dismissal(path: Path, key: str, reason: str, *, at: str = "") -> dict[str, dict[str, str]]:
    table = load_dismissals(path)
    table[key] = {"reason": reason, "at": at}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return table


def _dead_character_active(reg: Registry, records: list[dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    for who, entry in reg.characters.items():
        if entry.get("status") != "dead":
            continue
        died = int(entry.get("status_ch") or 0)
        if died <= 0:
            continue
        for item in records:
            # 比故事时间，不比叙述章。否则第 500 章写一段他生前的回忆就会误报。
            if story_ch(item) <= died:
                continue
            active = item["type"] == "scene" and who in item.get("participants", [])
            active = active or (item["type"] == "character_state" and item.get("who") == who)
            if not active:
                continue
            out.append(
                Finding(
                    rule="dead_character_active",
                    severity="critical",
                    key=f"dead_character_active:{who}:{story_ch(item)}",
                    chapter=item["ch"],
                    message=get_prompt(
                        "novel_finding_dead_active", who=who, died_ch=died, ch=story_ch(item)
                    ),
                )
            )
    return out


def _thread_findings(reg: Registry, limits: Thresholds, through: int) -> list[Finding]:
    out: list[Finding] = []
    for entry in reg.open_threads():
        thread_id = entry["id"]
        due = entry.get("due_ch")
        last = int(entry.get("last_ch") or entry.get("opened_ch") or 0)
        if due is not None and through > int(due):
            out.append(
                Finding(
                    rule="overdue_thread",
                    severity="critical",
                    key=f"overdue_thread:{thread_id}",
                    chapter=through,
                    message=get_prompt(
                        "novel_finding_overdue_thread", thread_id=thread_id, due_ch=due, ch=through
                    ),
                )
            )
            continue
        idle = through - last
        if idle > limits.dormant_thread_chapters:
            out.append(
                Finding(
                    rule="dormant_thread",
                    severity="warning",
                    key=f"dormant_thread:{thread_id}",
                    chapter=through,
                    message=get_prompt(
                        "novel_finding_dormant_thread", thread_id=thread_id, last_ch=last, idle=idle
                    ),
                )
            )
    return out


def _absent_characters(reg: Registry, limits: Thresholds, through: int) -> list[Finding]:
    out: list[Finding] = []
    appearances: dict[str, int] = {}
    for scene in reg.scenes:
        for who in scene.get("participants", []):
            appearances[who] = appearances.get(who, 0) + 1
    for who, entry in reg.characters.items():
        if entry.get("status") == "dead":
            continue
        if appearances.get(who, 0) < limits.main_character_min_appearances:
            continue
        last = int(entry.get("last_ch") or 0)
        gap = through - last
        if gap > limits.absent_character_chapters:
            out.append(
                Finding(
                    rule="absent_character",
                    severity="warning",
                    key=f"absent_character:{who}",
                    chapter=through,
                    message=get_prompt("novel_finding_absent", who=who, last_ch=last, gap=gap),
                )
            )
    return out


def _relationship_findings(reg: Registry) -> list[Finding]:
    out: list[Finding] = []
    for key, entry in reg.relationships.items():
        pair = list(key)
        label = "↔".join(pair)
        missing = [who for who in pair if who not in reg.characters]
        if missing:
            out.append(
                Finding(
                    rule="dangling_relationship",
                    severity="warning",
                    key=f"dangling_relationship:{label}",
                    chapter=int(entry.get("ch") or 0),
                    message=get_prompt(
                        "novel_finding_dangling", pair=label, missing="、".join(missing)
                    ),
                )
            )
            continue
        met = max(int(reg.characters[who].get("first_ch") or 0) for who in pair)
        start = entry.get("valid_from")
        if start is not None and int(start) < met:
            out.append(
                Finding(
                    rule="relationship_anachronism",
                    severity="warning",
                    key=f"relationship_anachronism:{label}",
                    chapter=int(start),
                    message=get_prompt(
                        "novel_finding_anachronism", pair=label, valid_from=start, met_ch=met
                    ),
                )
            )
    return out


def _hostile_co_present(reg: Registry) -> list[Finding]:
    """severity 是 info：这是「你确定要这样写吗」，不是「这里错了」。

    敌对双方同场每次都报，一段持续的宿敌关系会一章刷一条。1000 章合成书上实测 78 条，
    真按 warning 算会把 `--strict` 门禁变成永远红的，用户很快连真错一起无视。
    """
    hostile = {key for key, entry in reg.relationships.items() if entry.get("polarity") == "hostile"}
    out: list[Finding] = []
    for scene in reg.scenes:
        cast = set(scene.get("participants", []))
        for key in hostile:
            if not set(key) <= cast:
                continue
            label = "↔".join(key)
            out.append(
                Finding(
                    rule="hostile_co_present",
                    severity="info",
                    key=f"hostile_co_present:{label}:{scene['ch']}",
                    chapter=scene["ch"],
                    message=get_prompt(
                        "novel_finding_hostile", pair=label, ch=scene["ch"],
                        location=scene.get("location", "-"),
                    ),
                )
            )
    return out


def _knowledge_regression(records: list[dict[str, Any]]) -> list[Finding]:
    """先说知道、后又说不知道。反过来（先不知道后知道）是正常的剧情推进，不报。"""
    known_at: dict[tuple[str, str], int] = {}
    out: list[Finding] = []
    for item in sorted(records, key=lambda row: row["ch"]):
        if item["type"] != "character_state":
            continue
        who = item["who"]
        for fact in item.get("knows", []):
            known_at.setdefault((who, fact), item["ch"])
        for fact in item.get("unknowns", []):
            first = known_at.get((who, fact))
            if first is not None and first < item["ch"]:
                out.append(
                    Finding(
                        rule="knowledge_regression",
                        severity="critical",
                        key=f"knowledge_regression:{who}:{fact}",
                        chapter=item["ch"],
                        message=get_prompt(
                            "novel_finding_knowledge", who=who, fact=fact, known_ch=first, ch=item["ch"]
                        ),
                    )
                )
    return out


def _stalled_run(reg: Registry, records: list[dict[str, Any]], limits: Thresholds) -> list[Finding]:
    """中段塌陷可量化：一章什么都没推进，连着几章就是塌陷。不需要问模型书拖不拖。"""
    moved: dict[int, bool] = {}
    for item in records:
        if item["type"] in MOVEMENT_TYPES:
            moved[item["ch"]] = True
    chapters = reg.chapter_numbers()
    out: list[Finding] = []
    run: list[int] = []
    for number in chapters:
        if moved.get(number):
            run = []
            continue
        run.append(number)
        if len(run) == limits.stalled_run_chapters:
            out.append(
                Finding(
                    rule="stalled_run",
                    severity="warning",
                    key=f"stalled_run:{run[0]}",
                    chapter=run[0],
                    message=get_prompt(
                        "novel_finding_stalled", start_ch=run[0], end_ch=run[-1], count=len(run)
                    ),
                )
            )
    return out


def _missing_chapter_files(book: BookPaths, reg: Registry) -> list[Finding]:
    out: list[Finding] = []
    for number in reg.chapter_numbers():
        target = book.chapter_file(number)
        if target.is_file():
            continue
        out.append(
            Finding(
                rule="missing_chapter_file",
                severity="critical",
                key=f"missing_chapter_file:{number}",
                chapter=number,
                message=get_prompt("novel_finding_missing_file", ch=number, path=target.name),
            )
        )
    return out


def _unindexed_chapters(book: BookPaths, reg: Registry, ceiling: int) -> list[Finding]:
    """有正文、但状态库里一条记录都没有的章。

    这条补的是整套门禁最大的洞：正文写了三章、`records.jsonl` 空着，
    十条规则全都无事可查，`check --strict` 照样绿灯退出 0。goal 模式下更糟——
    客观门会一直是绿的，而质量系统其实一次都没跑过。沉默的通过比报错危险得多。
    """
    indexed = set(reg.chapter_numbers()) | {int(scene["ch"]) for scene in reg.scenes}
    out: list[Finding] = []
    for number in book.existing_chapters():
        if number in indexed or (ceiling and number > ceiling):
            continue
        out.append(
            Finding(
                rule="unindexed_chapter",
                severity="warning",
                key=f"unindexed_chapter:{number}",
                chapter=number,
                message=get_prompt("novel_finding_unindexed", ch=number),
            )
        )
    return out


def _alias_collisions(reg: Registry) -> list[Finding]:
    out: list[Finding] = []
    for alias, first, second in reg.alias_collisions:
        out.append(
            Finding(
                rule="alias_collision",
                severity="critical",
                key=f"alias_collision:{alias}",
                chapter=0,
                message=get_prompt(
                    "novel_finding_alias_collision", alias=alias, first=first, second=second
                ),
            )
        )
    return out


def _undeclared_characters(reg: Registry, records: list[dict[str, Any]]) -> list[Finding]:
    """只在别处被提到、从没有过自己 `character_state` 的名字。

    名字打错一个字，现在会静默多出一个角色——库不会报错，只会给出错的答案。
    这同时也是 ConStory 那套分类里 Nomenclature Confusions 的另一半：
    「用了正文里从没交代过的称呼」。
    """
    declared = {
        reg.aliases.get(item["who"], item["who"])
        for item in records
        if item["type"] == "character_state"
    }
    if not declared:
        return []
    mentioned: dict[str, int] = {}
    for scene in reg.scenes:
        for who in scene.get("participants", []):
            mentioned.setdefault(who, int(scene["ch"]))
    out: list[Finding] = []
    for who, chapter in sorted(mentioned.items(), key=lambda pair: (pair[1], pair[0])):
        if who in declared:
            continue
        out.append(
            Finding(
                rule="undeclared_character",
                severity="warning",
                key=f"undeclared_character:{who}",
                chapter=chapter,
                message=get_prompt("novel_finding_undeclared", who=who, ch=chapter),
            )
        )
    return out


def _trait_contradictions(records: list[dict[str, Any]]) -> list[Finding]:
    """同一恒定特征前后给了两个值：眼睛颜色变了、疤没了、身高缩了。

    ConStory-Bench 把 Factual & Detail 列为主导失败模式之一，而这类穿帮的特点是
    「首次确立在前 20%、矛盾出现在 40% 左右」——正是外部状态库该接住的长程问题，
    靠模型自己回看上下文接不住。
    """
    seen: dict[tuple[str, str, str], tuple[str, int]] = {}
    out: list[Finding] = []
    for item in sorted(records, key=lambda row: (story_ch(row), row["ch"])):
        subject = item.get("who") or item.get("object")
        if not subject or not item.get("traits"):
            continue
        for name, value in item["traits"].items():
            slot = (item["type"], subject, name)
            previous = seen.get(slot)
            if previous is None:
                seen[slot] = (value, item["ch"])
                continue
            if previous[0] == value:
                continue
            out.append(
                Finding(
                    rule="trait_contradiction",
                    severity="warning",
                    key=f"trait_contradiction:{subject}:{name}",
                    chapter=item["ch"],
                    message=get_prompt(
                        "novel_finding_trait",
                        subject=subject,
                        trait=name,
                        old=previous[0],
                        old_ch=previous[1],
                        new=value,
                        ch=item["ch"],
                    ),
                )
            )
    return out


def _pov_not_present(reg: Registry) -> list[Finding]:
    """场景声明了视角人物，人却不在场。

    只查这个结构性的点。真正的视角穿帮（限知第三人称里跳进别人脑子）要读正文，
    留给 M1 的模型评审；ConStory 实测视角类错误的「确立-矛盾」间距只有 4.7%，
    是局部失误而非长程失忆，本来也不该由状态库来兜。
    """
    out: list[Finding] = []
    for scene in reg.scenes:
        pov = scene.get("pov")
        if not pov or pov in scene.get("participants", []):
            continue
        out.append(
            Finding(
                rule="pov_not_present",
                severity="warning",
                key=f"pov_not_present:{scene['ch']}:{pov}",
                chapter=int(scene["ch"]),
                message=get_prompt("novel_finding_pov", who=pov, ch=scene["ch"]),
            )
        )
    return out


@dataclass(frozen=True)
class Coverage:
    """校验覆盖面。用来区分「查过了，干净」和「压根没东西可查」。"""

    prose_chapters: int = 0
    indexed_chapters: int = 0
    records: int = 0

    @property
    def evaluated(self) -> bool:
        return self.records > 0

    @property
    def gap(self) -> int:
        return max(0, self.prose_chapters - self.indexed_chapters)


def coverage(book: BookPaths, records: Iterable[dict[str, Any]]) -> Coverage:
    rows = list(records)
    indexed = {int(item["ch"]) for item in rows}
    return Coverage(
        prose_chapters=len(book.existing_chapters()),
        indexed_chapters=len(indexed),
        records=len(rows),
    )


def run_checks(
    book: BookPaths,
    *,
    through: int | None = None,
    limits: Thresholds | None = None,
    records: Iterable[dict[str, Any]] | None = None,
) -> list[Finding]:
    """跑全部规则，滤掉已豁免的，按 (严重度, 章号) 排序。"""
    rows = list(records) if records is not None else load_records(book.records)
    if through is not None:
        rows = [item for item in rows if item["ch"] <= through]
    ceiling = through if through is not None else max((item["ch"] for item in rows), default=0)
    reg = fold(rows, through=ceiling)
    bounds = limits or Thresholds()

    findings: list[Finding] = []
    findings += _dead_character_active(reg, rows)
    findings += _knowledge_regression(rows)
    findings += _thread_findings(reg, bounds, ceiling)
    findings += _absent_characters(reg, bounds, ceiling)
    findings += _relationship_findings(reg)
    findings += _hostile_co_present(reg)
    findings += _stalled_run(reg, rows, bounds)
    findings += _missing_chapter_files(book, reg)
    findings += _unindexed_chapters(book, reg, ceiling)
    findings += _alias_collisions(reg)
    findings += _undeclared_characters(reg, rows)
    findings += _trait_contradictions(rows)
    findings += _pov_not_present(reg)

    dismissed = load_dismissals(book.dismissals)
    findings = [item for item in findings if item.key not in dismissed]
    findings.sort(key=lambda item: (-SEVERITY_ORDER.get(item.severity, 0), item.chapter, item.key))
    return findings


def worst_severity(findings: Iterable[Finding]) -> str:
    rank = max((SEVERITY_ORDER.get(item.severity, 0) for item in findings), default=-1)
    for name, value in SEVERITY_ORDER.items():
        if value == rank:
            return name
    return "clean"
