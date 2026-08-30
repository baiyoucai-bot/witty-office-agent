"""冻结 Benchmark + scoreboard 评测账本。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from witty_agent.layout import benchmarks_dir
from witty_agent.logging import get_logger
from witty_agent.state.agent_state import AgentRecord

logger = get_logger("evolution.benchmark")


def ensure_benchmark(
    record: AgentRecord, benchmark_id: str, *, root: Path | None = None
) -> Path:
    directory = benchmarks_dir(record.project_id, record.agent_id, root=root) / benchmark_id
    directory.mkdir(parents=True, exist_ok=True)
    cases = directory / "cases.json"
    if not cases.is_file():
        cases.write_text("[]\n", encoding="utf-8")
    board = directory / "scoreboard.json"
    if not board.is_file():
        board.write_text("[]\n", encoding="utf-8")
    return directory


def append_score(
    record: AgentRecord,
    benchmark_id: str,
    *,
    score: float,
    summary: str,
    root: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory = ensure_benchmark(record, benchmark_id, root=root)
    board = directory / "scoreboard.json"
    rows = json.loads(board.read_text(encoding="utf-8") or "[]")
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": record.version,
        "score": round(float(score), 2),
        "summary": summary,
    }
    if extra:
        entry.update(extra)
    rows.append(entry)
    board.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "记分 benchmark=%s version=%s score=%s", benchmark_id, record.version, entry["score"]
    )
    return entry
