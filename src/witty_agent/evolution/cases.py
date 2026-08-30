"""冻结 Benchmark 的 Case：statement 公开，rubric 私有。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from witty_agent.evolution.benchmark import ensure_benchmark
from witty_agent.logging import get_logger
from witty_agent.state.agent_state import AgentRecord

logger = get_logger("evolution.cases")
_CASE_RE = re.compile(r"^CASE-\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    statement: str
    rubric: str
    path: Path


def write_case(
    record: AgentRecord,
    benchmark_id: str,
    case_id: str,
    *,
    statement: str,
    rubric: str,
    root: Path | None = None,
) -> CaseSpec:
    if not _CASE_RE.fullmatch(case_id):
        raise ValueError(f"case_id 不合法: {case_id}")
    directory = ensure_benchmark(record, benchmark_id, root=root) / case_id
    (directory / "statement").mkdir(parents=True, exist_ok=True)
    (directory / "rubric").mkdir(parents=True, exist_ok=True)
    (directory / "statement" / "README.md").write_text(statement.strip() + "\n", encoding="utf-8")
    (directory / "rubric" / "README.md").write_text(rubric.strip() + "\n", encoding="utf-8")
    logger.info("写入 case benchmark=%s case=%s", benchmark_id, case_id)
    return CaseSpec(case_id=case_id, statement=statement.strip(), rubric=rubric.strip(), path=directory)


def list_cases(record: AgentRecord, benchmark_id: str, *, root: Path | None = None) -> list[CaseSpec]:
    directory = ensure_benchmark(record, benchmark_id, root=root)
    cases: list[CaseSpec] = []
    for item in sorted(directory.iterdir()):
        if not item.is_dir() or not _CASE_RE.fullmatch(item.name):
            continue
        statement = _read(item / "statement" / "README.md")
        rubric = _read(item / "rubric" / "README.md")
        cases.append(CaseSpec(case_id=item.name, statement=statement, rubric=rubric, path=item))
    return cases


def freeze_benchmark(record: AgentRecord, benchmark_id: str, *, root: Path | None = None) -> Path:
    directory = ensure_benchmark(record, benchmark_id, root=root)
    path = directory / "benchmark_config.toml"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if "frozen" not in current:
        current += ("\n" if current and not current.endswith("\n") else "") + "frozen = true\n"
    elif "frozen = false" in current:
        current = current.replace("frozen = false", "frozen = true")
    if "title" not in current:
        current = f'title = "{benchmark_id}"\nruns = 1\n' + current
    path.write_text(current, encoding="utf-8")
    logger.info("冻结 benchmark=%s", benchmark_id)
    return path


def is_frozen(record: AgentRecord, benchmark_id: str, *, root: Path | None = None) -> bool:
    path = ensure_benchmark(record, benchmark_id, root=root) / "benchmark_config.toml"
    if not path.is_file():
        return False
    return "frozen = true" in path.read_text(encoding="utf-8")


def score_artifacts(workspace: Path, rubric: str) -> float:
    """按 rubric 里的 `- N pts: file` 和可选 contains 列表打分，满分 100。"""
    total = 0.0
    earned = 0.0
    for points, filename, needles in _parse_rubric(rubric):
        total += points
        path = workspace / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if needles and not all(item in text for item in needles):
            earned += points / 2
        else:
            earned += points
    if total <= 0:
        return 0.0
    return round(min(100.0, earned / total * 100.0), 2)


def _parse_rubric(rubric: str) -> list[tuple[float, str, list[str]]]:
    rows: list[tuple[float, str, list[str]]] = []
    for line in rubric.splitlines():
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*pts?.*?`([^`]+)`", line, re.I)
        if not match:
            continue
        needles: list[str] = []
        contains = re.search(r"contains:\s*(.+)$", line, re.I)
        if contains:
            needles = [part.strip().strip("`") for part in contains.group(1).split(",") if part.strip()]
        rows.append((float(match.group(1)), match.group(2), needles))
    return rows


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()
