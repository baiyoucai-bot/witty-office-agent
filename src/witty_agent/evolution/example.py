"""default_agent 预置 example-benchmark。"""

from __future__ import annotations

from pathlib import Path

from witty_agent.evolution.cases import freeze_benchmark, write_case
from witty_agent.state.agent_state import AgentRecord

EXAMPLE_BENCHMARK_ID = "example-benchmark"


def provision_example_benchmark(record: AgentRecord, *, root: Path | None = None) -> None:
    if record.agent_id != "default_agent":
        return
    write_case(
        record,
        EXAMPLE_BENCHMARK_ID,
        "CASE-001-file-summary",
        statement=(
            "Read notes.txt and write summary.md with a short overview "
            "and exactly 3 bullet facts."
        ),
        rubric=(
            "- 40 pts: `summary.md` exists\n"
            "- 40 pts: `summary.md` contains: 3 facts\n"
            "- 20 pts: `summary.md` exists\n"
        ),
        root=root,
    )
    write_case(
        record,
        EXAMPLE_BENCHMARK_ID,
        "CASE-002-data-cleanup",
        statement="Clean users.csv into users_clean.csv: lowercase emails, drop empty and duplicates.",
        rubric=(
            "- 50 pts: `users_clean.csv` exists\n"
            "- 50 pts: `users_clean.csv` contains: @\n"
        ),
        root=root,
    )
    freeze_benchmark(record, EXAMPLE_BENCHMARK_ID, root=root)
