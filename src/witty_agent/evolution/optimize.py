"""自进化闭环：快照 → 假设 → 评测矩阵 → 均分严格更高才接受。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from witty_agent.evolution.benchmark import append_score
from witty_agent.evolution.cases import CaseSpec, is_frozen, list_cases, score_artifacts
from witty_agent.evolution.protocol import EvalReport, is_valid_matrix
from witty_agent.evolution.snapshot import restore_snapshot, save_snapshot
from witty_agent.logging import get_logger
from witty_agent.state.agent_state import AgentRecord, bump_version, load_agent_state

logger = get_logger("evolution.optimize")
Mutator = Callable[[], Awaitable[None] | None]
CaseRunner = Callable[[CaseSpec, Path], Awaitable[None]]


@dataclass
class CaseResult:
    case_id: str
    score: float
    session_id: str = ""


@dataclass
class OptimizeResult:
    keep: bool
    before: float
    after: float
    version: int
    cases: list[CaseResult] = field(default_factory=list)
    reason: str = ""


def append_hypothesis(record: AgentRecord, text: str) -> Path:
    path = record.state_dir / "evolution_log.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = f"## {stamp} v{record.version}\n\n{text.strip()}\n\n"
    previous = path.read_text(encoding="utf-8") if path.is_file() else "# Evolution log\n\n"
    path.write_text(previous + block, encoding="utf-8")
    return path


def accept_if_higher(
    record: AgentRecord,
    *,
    before: float,
    after: float,
    baseline_version: int,
    benchmark_id: str,
    summary: str,
    root: Path | None = None,
) -> bool:
    if after > before:
        append_score(record, benchmark_id, score=after, summary=summary, root=root)
        logger.info("接受候选 before=%s after=%s version=%s", before, after, record.version)
        return True
    restore_snapshot(record, baseline_version, root=root)
    logger.info("回滚候选 before=%s after=%s to=%s", before, after, baseline_version)
    return False


async def evaluate_cases(
    record: AgentRecord,
    benchmark_id: str,
    *,
    workspace: Path,
    runner: CaseRunner,
    root: Path | None = None,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in list_cases(record, benchmark_id, root=root):
        case_dir = workspace / case.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "STATEMENT.md").write_text(case.statement + "\n", encoding="utf-8")
        await runner(case, case_dir)
        results.append(CaseResult(case_id=case.case_id, score=score_artifacts(case_dir, case.rubric)))
    return results


async def run_optimize_loop(
    record: AgentRecord,
    benchmark_id: str,
    *,
    workspace: Path,
    runner: CaseRunner,
    mutate: Mutator,
    hypothesis: str,
    root: Path | None = None,
    require_frozen: bool = True,
) -> OptimizeResult:
    if require_frozen and not is_frozen(record, benchmark_id, root=root):
        return OptimizeResult(keep=False, before=0, after=0, version=record.version, reason="benchmark_not_frozen")
    save_snapshot(record, root=root)
    baseline_version = record.version
    before_rows = await evaluate_cases(record, benchmark_id, workspace=workspace / "before", runner=runner, root=root)
    before = _mean(before_rows)
    append_hypothesis(record, hypothesis)
    maybe = mutate()
    if hasattr(maybe, "__await__"):
        await maybe  # type: ignore[misc]
    live = load_agent_state(record.project_id, record.agent_id, root=root)
    expected = live.version
    after_rows = await evaluate_cases(live, benchmark_id, workspace=workspace / "after", runner=runner, root=root)
    after = _mean(after_rows)
    checked = load_agent_state(record.project_id, record.agent_id, root=root)
    after_reports = [EvalReport(status="ok", case_id=item.case_id, score=item.score) for item in after_rows]
    if not is_valid_matrix(after_reports, expected, checked.version) or not before_rows:
        restore_snapshot(record, baseline_version, root=root)
        return OptimizeResult(
            keep=False, before=before, after=after, version=live.version, cases=after_rows, reason="invalid_matrix"
        )
    keep = accept_if_higher(
        live,
        before=before,
        after=after,
        baseline_version=baseline_version,
        benchmark_id=benchmark_id,
        summary=hypothesis[:120],
        root=root,
    )
    if keep and live.version == baseline_version:
        bump_version(live)
    return OptimizeResult(keep=keep, before=before, after=after, version=live.version, cases=after_rows)


def _mean(rows: list[CaseResult]) -> float:
    if not rows:
        return 0.0
    return round(sum(item.score for item in rows) / len(rows), 2)
