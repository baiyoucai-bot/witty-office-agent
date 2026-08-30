"""会话工作板：目标 / 约束 / 决定 / 字面锚点。压缩后重装，清空则归档。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.memory import topic_body
from witty_agent.prompts import get_prompt

logger = get_logger("focus")

FOCUS_NAME = "focus.md"
ARCHIVE_DIR = "focus-archive"
DEFAULT_MAX_CHARS = 2200
_SLUG = "focus"
_ANCHOR_PATH = re.compile(
    r"(?:[A-Za-z]:)?(?:/[^\s<>\"'`]{1,80}){1,8}\.[A-Za-z0-9]{1,8}"
    r"|(?:[A-Za-z0-9_.-]+/){1,6}[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}"
)
_ANCHOR_QUOTE = re.compile(r"[「『\"']([^」』\"']{2,80})[」』\"']")
_ANCHOR_CMD = re.compile(r"`([^`]{2,120})`")


@dataclass
class FocusBoard:
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (
            self.objective.strip()
            or self.constraints
            or self.decisions
            or self.anchors
        )


def focus_path(directory: Path) -> Path:
    return directory / FOCUS_NAME


def load_focus(directory: Path) -> FocusBoard:
    path = focus_path(directory)
    if path.is_file():
        return parse_focus(path.read_text(encoding="utf-8"))
    body = topic_body(directory, _SLUG)
    if body.strip():
        return parse_focus(body)
    return FocusBoard()


def parse_focus(text: str) -> FocusBoard:
    board = FocusBoard()
    section = "objective"
    buckets = {
        "objective": [],
        "constraints": [],
        "decisions": [],
        "anchors": [],
    }
    aliases = {
        "目标": "objective",
        "objective": "objective",
        "约束": "constraints",
        "constraints": "constraints",
        "决定": "decisions",
        "decisions": "decisions",
        "锚点": "anchors",
        "anchors": "anchors",
        "前提": "anchors",
    }
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("---") or line.startswith("name:"):
            continue
        heading = line.lstrip("#").strip()
        key = aliases.get(heading.casefold()) or aliases.get(heading)
        if key:
            section = key
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if line:
            buckets[section].append(line)
    board.objective = " ".join(buckets["objective"]).strip()
    board.constraints = _uniq(buckets["constraints"])
    board.decisions = _uniq(buckets["decisions"])
    board.anchors = _uniq(buckets["anchors"])
    return board


def render_focus(board: FocusBoard, *, limit: int | None = None) -> str:
    if board.empty():
        return ""
    cap = DEFAULT_MAX_CHARS if limit is None else max(200, int(limit))
    lines = [get_prompt("focus_board_title")]
    if board.objective:
        lines.extend([get_prompt("focus_board_objective"), board.objective])
    if board.constraints:
        lines.append(get_prompt("focus_board_constraints"))
        lines.extend(f"- {item}" for item in board.constraints)
    if board.decisions:
        lines.append(get_prompt("focus_board_decisions"))
        lines.extend(f"- {item}" for item in board.decisions)
    if board.anchors:
        lines.append(get_prompt("focus_board_anchors"))
        lines.extend(f"- {item}" for item in board.anchors)
    text = "\n".join(lines).strip()
    if len(text) > cap:
        text = text[: cap - 1] + "…"
    return text


def save_focus(directory: Path, board: FocusBoard) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = focus_path(directory)
    path.write_text(render_focus(board, limit=20_000) + "\n", encoding="utf-8")
    logger.info("写入工作板 path=%s", path)
    return path


def save_focus_text(directory: Path, text: str) -> Path:
    return save_focus(directory, parse_focus(text))


def archive_focus(directory: Path) -> Path | None:
    board = load_focus(directory)
    if board.empty():
        path = focus_path(directory)
        if path.is_file():
            path.unlink()
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = directory / ARCHIVE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{stamp}.md"
    dest.write_text(render_focus(board, limit=20_000) + "\n", encoding="utf-8")
    path = focus_path(directory)
    if path.is_file():
        path.unlink()
    topic = directory / f"{_SLUG}.md"
    if topic.is_file():
        topic.unlink()
    logger.info("归档工作板 path=%s", dest)
    return dest


def missing_anchors(summary: str, board: FocusBoard) -> list[str]:
    hay = summary or ""
    missed: list[str] = []
    for item in board.anchors:
        needle = item.strip()
        if len(needle) < 2:
            continue
        if needle not in hay:
            missed.append(needle)
    return missed


def extract_anchors(text: str) -> list[str]:
    found: list[str] = []
    for expr in (_ANCHOR_PATH, _ANCHOR_QUOTE, _ANCHOR_CMD):
        found.extend(match.group(0) if expr is _ANCHOR_PATH else match.group(1) for match in expr.finditer(text or ""))
    return _uniq(found)[:12]


def seed_from_lattice(directory: Path, board: FocusBoard) -> FocusBoard:
    if not board.empty():
        return board
    return FocusBoard(
        objective=_excerpt(topic_body(directory, "goals")),
        constraints=_bullets(topic_body(directory, "constraints")),
        decisions=_bullets(topic_body(directory, "decisions")),
        anchors=[],
    )


def focus_notice(board: FocusBoard, *, limit: int | None = None) -> str:
    body = render_focus(board, limit=limit)
    if not body:
        return ""
    return get_prompt("focus_board_inject", body=body)


def premise_notice(missing: list[str]) -> str:
    return get_prompt("premise_guard_notice", anchors="、".join(missing[:8]))


def _excerpt(body: str, *, limit: int = 200) -> str:
    text = " ".join((body or "").split())
    return text[:limit]


def _bullets(body: str) -> list[str]:
    rows: list[str] = []
    for line in (body or "").splitlines():
        text = line.strip().lstrip("-* ").strip()
        if text:
            rows.append(text)
    return _uniq(rows)[:8]


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        folded = key.casefold()
        if not key or folded in seen:
            continue
        seen.add(folded)
        out.append(key)
    return out


