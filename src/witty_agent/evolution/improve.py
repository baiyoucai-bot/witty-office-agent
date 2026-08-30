"""self-improving 示例：评估 → 改 AGENTS.md → 再评估 → 保留或回滚。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from witty_agent.evolution.benchmark import append_score, ensure_benchmark
from witty_agent.evolution.snapshot import restore_snapshot, save_snapshot
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.session import create_agent, create_session
from witty_agent.state.agent_state import bump_version, load_agent_state

logger = get_logger("evolution.improve")

NOTES = """Project Aurora — Internal Notes

Aurora is a real-time analytics platform launched in Q1 2026. At peak it ingests roughly
2 million events per second through a Kafka-based pipeline. The core query engine was rewritten
in Rust after the original Go version could not keep p99 latency under control; the rewrite cut
p99 from 800ms to 120ms.

The production deployment is single-region with no failover; a multi-region rollout is scheduled
for Q3 2026. Storage is the largest cost line at about $48,000/month, driven by a 90-day
hot-retention policy; cutting retention to 30 days would reduce storage cost by roughly 55%.
"""

REFERENCE_REPORT = """<!-- ACME-DATA-PLATFORM -->
# Report: Project Borealis
Classification: INTERNAL

Borealis is a batch ETL platform migrated to Spark in 2025; it processes about 40TB nightly.

- Cut the nightly window from 6h to 90 minutes
- Runs on a 200-node autoscaling cluster
- Compute costs roughly $12,000/month

Reviewed-by: Aurora Team
"""


@dataclass
class ScoreResult:
    score: int
    detail: list[str]


async def _allow(_name: str, _call_id: str, _args: dict) -> str:
    return "allow"


def score_summary(text: str | None) -> ScoreResult:
    detail: list[str] = []
    points = 0
    if text is None:
        return ScoreResult(0, ["0/1  file summary.md was actually written"])
    points += 1
    detail.append("1/1  file summary.md was actually written")
    body = re.sub(r"^#.*$", "", text, flags=re.M).strip()
    overview = re.split(r"\n\s*[-*]", body)[0]
    sentences = len(re.findall(r"[.!?]\s|$", overview.strip() + " "))
    # 上面太松，改用句号计数
    sentences = len([part for part in re.split(r"[.!?]+", overview) if part.strip()])
    ok = 1 <= sentences <= 2
    points += int(ok)
    detail.append(f"{int(ok)}/1  overview is <= 2 sentences (found {sentences})")
    bullets = len(re.findall(r"^\s*[-*]\s+", text, flags=re.M))
    ok = bullets == 3
    points += int(ok)
    detail.append(f"{int(ok)}/1  exactly 3 bullet facts (found {bullets})")
    words = len([item for item in body.split() if item])
    ok = words < 60
    points += int(ok)
    detail.append(f"{int(ok)}/1  under 60 words (found {words})")
    anchors = ["120ms", "55%", "2 million", "$48,000", "Q3 2026", "800ms"]
    hits = sum(1 for item in anchors if item in text)
    ok = hits >= 2
    points += int(ok)
    detail.append(f"{int(ok)}/1  key facts accurate ({hits} source figures present)")
    return ScoreResult(points, detail)


async def run_task(agent, workspace: Path, prompt: str, stream_fn) -> ScoreResult:
    session = create_session(agent, workspace_dir=workspace)
    summary = workspace / "summary.md"
    # 每轮从干净产物起跑：留着上一轮的文件，一是分数会被继承（没写也照样得分，
    # 基准就废了），二是覆盖前必须先 read 的观察闸门会正当拦下这一轮的 write。
    summary.unlink(missing_ok=True)
    await session.run(prompt, stream_fn=stream_fn, approve=_allow, approval_mode="allow-all")
    text = summary.read_text(encoding="utf-8") if summary.is_file() else None
    return score_summary(text)


async def run_scoring_loop(*, root: Path, workspace: Path, stream_fn) -> dict:
    """评分闭环：脚本写入纪律，分数升高才保留。"""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text(NOTES, encoding="utf-8")
    agent = create_agent("default_project", "self-improve-demo", root=root)
    agents_md = agent.record.state_dir / "AGENTS.md"
    agents_md.write_text("", encoding="utf-8")
    record = load_agent_state("default_project", "self-improve-demo", root=root)
    save_snapshot(record, root=root)
    task = get_prompt("evolve_task")
    before = await run_task(agent, workspace, task, stream_fn)
    append_score(record, "self-improve", score=before.score * 20, summary="baseline", root=root)
    agents_md.write_text(get_prompt("evolve_discipline"), encoding="utf-8")
    bump_version(record)
    after = await run_task(agent, workspace, task, stream_fn)
    keep = after.score > before.score
    if keep:
        append_score(record, "self-improve", score=after.score * 20, summary="kept", root=root)
    else:
        restore_snapshot(record, 1, root=root)
    logger.info("评分闭环 before=%s after=%s keep=%s", before.score, after.score, keep)
    return {
        "before": before.score,
        "after": after.score,
        "keep": keep,
        "before_detail": before.detail,
        "after_detail": after.detail,
    }


async def run_self_evolve(*, root: Path, workspace: Path, stream_fn) -> dict:
    """自进化：模型自己改 AGENTS.md。"""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text(NOTES, encoding="utf-8")
    agent = create_agent("default_project", "self-evolve-demo", root=root)
    agents_md = agent.record.state_dir / "AGENTS.md"
    agents_md.write_text("", encoding="utf-8")
    record = load_agent_state("default_project", "self-evolve-demo", root=root)
    save_snapshot(record, root=root)
    task = get_prompt("evolve_task")
    before = await run_task(agent, workspace, task, stream_fn)
    rejected = (workspace / "summary.md").read_text(encoding="utf-8") if (workspace / "summary.md").is_file() else ""
    reflect = get_prompt("evolve_reflect", rejected=rejected, reference=REFERENCE_REPORT)
    await run_task(agent, workspace, reflect, stream_fn)
    bump_version(load_agent_state("default_project", "self-evolve-demo", root=root))
    after = await run_task(agent, workspace, task, stream_fn)
    keep = after.score > before.score
    if not keep:
        restore_snapshot(record, 1, root=root)
    logger.info("自我进化 before=%s after=%s keep=%s", before.score, after.score, keep)
    return {"before": before.score, "after": after.score, "keep": keep, "agents_md": agents_md.read_text(encoding="utf-8")}
