"""从正文抽出有日期的事件，按时间线落盘。时区来自 runtime。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.memory import append_unique_bullets, topic_body
from witty_agent.time_context import clock_now

logger = get_logger("memory")
TIMELINE_SLUG = "timeline"
TIMELINE_CAP = 80

_ISO = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_CN_YMD = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_CN_YM = re.compile(r"(20\d{2})年(\d{1,2})月")
_CN_MD = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]")
_SPLIT = re.compile(r"[。！？!?\n；;]+")
_RELATIVE = {
    "今天": 0,
    "今日": 0,
    "昨天": -1,
    "昨日": -1,
    "明天": 1,
    "明日": 1,
    "前天": -2,
    "后天": 2,
}


def extract_dated_events(text: str, *, today: date | None = None) -> list[tuple[str, str]]:
    """返回 (YYYY-MM-DD, 事件摘要) 列表，同一句可对应多个日期。"""
    now = today or clock_now()["date"]
    found: list[tuple[str, str]] = []
    for raw in _SPLIT.split(text or ""):
        sentence = re.sub(r"\s+", " ", raw).strip()
        if len(sentence) < 4:
            continue
        for day in _dates_in(sentence, now):
            found.append((day.isoformat(), sentence[:200]))
    return found


def append_timeline(directory: Path, events: list[tuple[str, str]]) -> int:
    if not events:
        return 0
    lines = [f"{day} | {text}" for day, text in events]
    added = append_unique_bullets(
        directory,
        TIMELINE_SLUG,
        description="按日期梳理的事件时间线",
        lines=lines,
        max_bullets=TIMELINE_CAP,
        # 行首日期在这里是事件日期，同一句话可以挂两个日期，不能按「事」并成一条。
        dedupe_by_fact=False,
    )
    if added:
        _rewrite_sorted(directory)
        logger.info("时间线新增 events=%s", added)
    return added


def list_timeline_events(directory: Path, *, limit: int = 40) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for item in _sorted_rows(directory)[:limit]:
        day, sep, rest = item.partition(" | ")
        events.append({"date": day if sep else "", "text": (rest or item).strip()})
    return events


def render_timeline(directory: Path, *, limit: int = 40) -> str:
    rows = _sorted_rows(directory)
    if not rows:
        return ""
    return "\n".join(f"- {item}" for item in rows[:limit])


def harvest_timeline(directory: Path, text: str, *, today: date | None = None) -> int:
    return append_timeline(directory, extract_dated_events(text, today=today))


def _dates_in(sentence: str, today: date) -> list[date]:
    hits: list[date] = []
    for match in _CN_YMD.finditer(sentence):
        hits.append(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in _ISO.finditer(sentence):
        hits.append(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    if not hits:
        for match in _CN_YM.finditer(sentence):
            hits.append(_safe_date(int(match.group(1)), int(match.group(2)), 1))
        for match in _CN_MD.finditer(sentence):
            hits.append(_safe_date(today.year, int(match.group(1)), int(match.group(2))))
    for word, delta in _RELATIVE.items():
        if word in sentence:
            hits.append(today + timedelta(days=delta))
    if "上周" in sentence:
        hits.append(today - timedelta(days=7 + today.weekday()))
    if "下周" in sentence:
        hits.append(today + timedelta(days=7 - today.weekday()))
    if "本月" in sentence:
        hits.append(today.replace(day=1))
    if "上月" in sentence:
        first = today.replace(day=1)
        hits.append((first - timedelta(days=1)).replace(day=1))
    seen: set[date] = set()
    ordered: list[date] = []
    for item in hits:
        if item is None or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _sorted_rows(directory: Path) -> list[str]:
    rows: list[tuple[str, str]] = []
    for line in topic_body(directory, TIMELINE_SLUG).splitlines():
        text = line.strip().lstrip("- ").strip()
        if not text:
            continue
        day, sep, rest = text.partition(" | ")
        if not sep:
            day, rest = "9999-99-99", text
        rows.append((day, rest or text))
    rows.sort(key=lambda item: item[0])
    return [f"{day} | {rest}" for day, rest in rows]


def _rewrite_sorted(directory: Path) -> None:
    from witty_agent.memory import write_topic

    rows = _sorted_rows(directory)
    body = "\n".join(f"- {item}" for item in rows[-TIMELINE_CAP:])
    write_topic(directory, TIMELINE_SLUG, description="按日期梳理的事件时间线", body=body)
