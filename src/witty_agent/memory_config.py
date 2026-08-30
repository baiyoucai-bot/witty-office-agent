"""九宫格与分类配置。正文提示词仍只在 prompts.toml。"""

from __future__ import annotations

import os
from witty_agent.tomlcompat import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from witty_agent.paths import project_root

_ENV_MEMORY_FILE = "WITTY_MEMORY_FILE"
_DEFAULT = project_root() / "config" / "memory.toml"

_FALLBACK_CELLS = (
    ("who", "身份与角色", "用户是谁、岗位、职责"),
    ("goals", "当前目标", "正在推进的任务和成功标准"),
    ("constraints", "红线与约束", "不能做、必须先问、合规限制"),
    ("prefs", "个人偏好", "称呼、详略、工具习惯、交互偏好"),
    ("domain", "领域要点", "反复出现、下次还用得上的领域事实"),
    ("assets", "项目与资产", "项目、系统、台账、资料包"),
    ("people", "关系与组织", "协作对象、班组、科室"),
    ("decisions", "已做决定", "已经拍板、下次不要再问的选择"),
    ("followups", "待跟进", "用户交代下次要做的事"),
)


@dataclass(frozen=True)
class MemoryCell:
    id: str
    title: str
    description: str


@dataclass(frozen=True)
class MemoryTaxonomy:
    id: str
    title: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class MemorySettings:
    auto_harvest: bool = True
    harvest_process: bool = False
    judge_leftover: bool = True
    max_bullets: int = 40
    working_set: int = 12
    archive_cap: int = 80
    retrieve_limit: int = 4
    retrieve_min_score: int = 3
    retrieve_archive_min_score: int = 5
    retrieve_decay_days: int = 14
    retrieve_decay_penalty: int = 2
    retrieve_archive: bool = True
    retrieve_rare_corpus_min: int = 25
    retrieve_rare_df_ratio: float = 0.05
    inject_claim_cap: int = 3
    inject_char_cap: int = 1200
    topic_switch_overlap: float = 0.4
    focus_max_chars: int = 2200
    judge_settle_sec: float = 8.0
    consolidate_enabled: bool = True
    consolidate_total_cap: int = 400
    consolidate_high_water: int = 10
    consolidate_min_turns: int = 20
    consolidate_max_cells: int = 2
    gc_enabled: bool = True
    gc_workspace_ttl_days: int = 30
    stopwords: tuple[str, ...] = field(default_factory=tuple)
    cells: tuple[MemoryCell, ...] = field(default_factory=tuple)
    taxonomy: tuple[MemoryTaxonomy, ...] = field(default_factory=tuple)
    cues: dict[str, tuple[str, ...]] = field(default_factory=dict)
    assistant_cues: dict[str, tuple[str, ...]] = field(default_factory=dict)
    skip_needles: tuple[str, ...] = field(default_factory=tuple)
    process_needles: tuple[str, ...] = field(default_factory=tuple)
    pref_slots: dict[str, tuple[str, ...]] = field(default_factory=dict)
    pref_retract: tuple[str, ...] = field(default_factory=tuple)
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def cell(self, cell_id: str) -> MemoryCell | None:
        for item in self.cells:
            if item.id == cell_id:
                return item
        return None

    def tax(self, tax_id: str) -> MemoryTaxonomy | None:
        for item in self.taxonomy:
            if item.id == tax_id:
                return item
        return None


def memory_file() -> Path:
    override = os.environ.get(_ENV_MEMORY_FILE)
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT


@lru_cache(maxsize=4)
def _load_raw(path: str) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    with file_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}


def load_memory_settings() -> MemorySettings:
    raw = _load_raw(str(memory_file()))
    table = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    cells = _parse_cells(table.get("cells") if isinstance(table, dict) else None)
    taxonomy = _parse_taxonomy(table.get("taxonomy") if isinstance(table, dict) else None)
    cues = _parse_cue_table(table.get("cues") if isinstance(table, dict) else None)
    assistant_cues = _parse_cue_table(table.get("assistant_cues") if isinstance(table, dict) else None)
    skip_table = table.get("skip") if isinstance(table, dict) else {}
    needles: tuple[str, ...] = ()
    if isinstance(skip_table, dict) and isinstance(skip_table.get("needles"), list):
        needles = tuple(str(item) for item in skip_table["needles"] if str(item).strip())
    process_table = table.get("process") if isinstance(table, dict) else {}
    process_needles: tuple[str, ...] = ()
    if isinstance(process_table, dict) and isinstance(process_table.get("needles"), list):
        process_needles = tuple(str(item) for item in process_table["needles"] if str(item).strip())
    pref_table = table.get("pref_slots") if isinstance(table, dict) else {}
    pref_slots = _parse_cue_table(pref_table)
    retract_table = table.get("pref_retract") if isinstance(table, dict) else {}
    retract_cues: tuple[str, ...] = ()
    if isinstance(retract_table, dict) and isinstance(retract_table.get("cues"), list):
        retract_cues = tuple(str(item) for item in retract_table["cues"] if str(item).strip())
    con_table = table.get("consolidate") if isinstance(table, dict) else {}
    con_table = con_table if isinstance(con_table, dict) else {}
    gc_table = table.get("gc") if isinstance(table, dict) else {}
    gc_table = gc_table if isinstance(gc_table, dict) else {}
    retrieve_table = table.get("retrieve") if isinstance(table, dict) else {}
    stopwords: tuple[str, ...] = ()
    min_score = 3
    decay_days = 14
    decay_penalty = 2
    retrieve_archive = True
    archive_min_score = 5
    rare_corpus_min = 25
    rare_df_ratio = 0.05
    if isinstance(retrieve_table, dict):
        if retrieve_table.get("rare_corpus_min") is not None:
            rare_corpus_min = max(1, int(retrieve_table["rare_corpus_min"]))
        if retrieve_table.get("rare_df_ratio") is not None:
            rare_df_ratio = max(0.0, min(1.0, float(retrieve_table["rare_df_ratio"])))
        if isinstance(retrieve_table.get("stopwords"), list):
            stopwords = tuple(str(item) for item in retrieve_table["stopwords"] if str(item).strip())
        if retrieve_table.get("min_score") is not None:
            min_score = max(1, int(retrieve_table["min_score"]))
        if retrieve_table.get("decay_days") is not None:
            decay_days = max(0, int(retrieve_table["decay_days"]))
        if retrieve_table.get("decay_penalty") is not None:
            decay_penalty = max(0, int(retrieve_table["decay_penalty"]))
        if retrieve_table.get("archive") is not None:
            retrieve_archive = bool(retrieve_table.get("archive"))
        if retrieve_table.get("archive_min_score") is not None:
            archive_min_score = max(1, int(retrieve_table["archive_min_score"]))
    return MemorySettings(
        auto_harvest=bool(table.get("auto_harvest", True)) if isinstance(table, dict) else True,
        harvest_process=bool(table.get("harvest_process", False)) if isinstance(table, dict) else False,
        judge_leftover=bool(table.get("judge_leftover", True)) if isinstance(table, dict) else True,
        max_bullets=int(table.get("max_bullets") or 40) if isinstance(table, dict) else 40,
        working_set=int(table.get("working_set") or 12) if isinstance(table, dict) else 12,
        archive_cap=int(table.get("archive_cap") or 80) if isinstance(table, dict) else 80,
        retrieve_limit=int(table.get("retrieve_limit") or 4) if isinstance(table, dict) else 4,
        retrieve_min_score=min_score,
        retrieve_archive_min_score=archive_min_score,
        retrieve_decay_days=decay_days,
        retrieve_decay_penalty=decay_penalty,
        retrieve_archive=retrieve_archive,
        retrieve_rare_corpus_min=rare_corpus_min,
        retrieve_rare_df_ratio=rare_df_ratio,
        inject_claim_cap=max(1, int(table.get("inject_claim_cap") or 3)) if isinstance(table, dict) else 3,
        inject_char_cap=max(200, int(table.get("inject_char_cap") or 1200)) if isinstance(table, dict) else 1200,
        topic_switch_overlap=float(table.get("topic_switch_overlap") or 0.4) if isinstance(table, dict) else 0.4,
        focus_max_chars=max(400, int(table.get("focus_max_chars") or 2200)) if isinstance(table, dict) else 2200,
        judge_settle_sec=max(0.0, float(table.get("judge_settle_sec") or 8.0)) if isinstance(table, dict) else 8.0,
        consolidate_enabled=bool(con_table.get("enabled", True)),
        consolidate_total_cap=max(1, int(con_table.get("total_cap") or 400)),
        consolidate_high_water=max(2, int(con_table.get("cell_high_water") or 10)),
        consolidate_min_turns=max(0, int(con_table.get("min_turns_between") or 20)),
        consolidate_max_cells=max(1, int(con_table.get("max_cells_per_run") or 2)),
        gc_enabled=bool(gc_table.get("enabled", True)),
        gc_workspace_ttl_days=max(1, int(gc_table.get("workspace_ttl_days") or 30)),
        stopwords=stopwords,
        cells=cells,
        taxonomy=taxonomy,
        cues=cues,
        assistant_cues=assistant_cues,
        skip_needles=needles or ("password", "token", "api_key", "secret", "sk-"),
        process_needles=process_needles,
        pref_slots=pref_slots,
        pref_retract=retract_cues,
        aliases=_parse_cue_table(table.get("aliases") if isinstance(table, dict) else None),
    )


def clear_memory_config_cache() -> None:
    _load_raw.cache_clear()


def _parse_cue_table(raw: object) -> dict[str, tuple[str, ...]]:
    cues: dict[str, tuple[str, ...]] = {}
    if not isinstance(raw, dict):
        return cues
    for key, value in raw.items():
        if isinstance(value, list):
            cues[str(key)] = tuple(str(item) for item in value if str(item).strip())
    return cues


def _parse_cells(raw: object) -> tuple[MemoryCell, ...]:
    if isinstance(raw, list) and raw:
        items: list[MemoryCell] = []
        for row in raw:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            items.append(
                MemoryCell(
                    id=str(row["id"]),
                    title=str(row.get("title") or row["id"]),
                    description=str(row.get("description") or ""),
                )
            )
        if items:
            return tuple(items)
    return tuple(MemoryCell(cell_id, title, desc) for cell_id, title, desc in _FALLBACK_CELLS)


def _parse_taxonomy(raw: object) -> tuple[MemoryTaxonomy, ...]:
    if not isinstance(raw, list):
        return ()
    items: list[MemoryTaxonomy] = []
    for row in raw:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        keywords = row.get("keywords") or []
        words = tuple(str(item) for item in keywords if str(item).strip()) if isinstance(keywords, list) else ()
        items.append(
            MemoryTaxonomy(
                id=str(row["id"]),
                title=str(row.get("title") or row["id"]),
                keywords=words,
            )
        )
    return tuple(items)
