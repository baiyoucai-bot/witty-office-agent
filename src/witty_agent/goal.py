"""goal loop：目标模式反复跑，直到判据成立、判定不可能，或触达上限。

模型在 GOAL.yaml 里写 `complete` 只是**申报**，不是判据。这一轮之后判据由三样东西给：

1. **客观 gate**（`verify.GateRunner`）：命令退出码，最便宜也最硬，所以先跑。
2. **回归义务**（`verify.ObligationLedger`）：过了的 gate 登记下来，之后每轮全部重跑。
3. **判官**（另一个便宜模型）：看轨迹里有没有权威证据。只声称而没有验证过的，一律算没满足。

三样都不满意时，把「还差什么」回灌成下一轮的指引，而不是空转重来。判官解析失败按**没完成**
处理：多跑一轮只是费钱，把没人验过的目标当完成才是真错。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.runtime import goal_settings
from witty_agent.types import AgentMessage
from witty_agent.verify import (
    GateReport,
    GateRunner,
    GateSpec,
    Obligation,
    ObligationLedger,
    merge_specs,
)

logger = get_logger("goal")
GOAL_MAX_ROUNDS = 100
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class GoalVerdict:
    """One adjudication of the objective. `parsed=False` means we could not read the judge."""

    score: float = 0.0
    complete: bool = False
    impossible: bool = False
    missing: str = ""
    parsed: bool = True


@dataclass
class GoalState:
    status: str
    objective: str
    round: int
    path: Path
    verdict: GoalVerdict | None = None
    reason: str = ""
    gates: GateReport = field(default_factory=GateReport)


JudgeFn = Callable[[str, str], Awaitable[GoalVerdict]]


def goal_path(scratch: Path) -> Path:
    return scratch / "GOAL.yaml"


def write_goal(path: Path, objective: str, status: str = "active", round_no: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = objective.replace("\n", "\n  ")
    path.write_text(
        f"status: {status}\nround: {round_no}\nobjective: |\n  {escaped}\n",
        encoding="utf-8",
    )


def read_goal_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "blocked"


def _round_prompt(objective: str, path: Path, round_no: int, budget: int, used: int) -> str:
    remaining = "unbounded" if budget <= 0 else str(max(0, budget - used))
    key = "goal_wrap_up" if budget > 0 and used + 1 >= budget else "goal_round"
    return get_prompt(
        key,
        objective=objective,
        goal_path=str(path),
        round_no=str(round_no),
        tokens_used=str(used),
        budget=str(budget if budget > 0 else "unbounded"),
        remaining=remaining,
    )


def parse_verdict(raw: str) -> GoalVerdict:
    """Strict JSON first, then the first balanced object embedded in prose.

    Anything we cannot read becomes score 0 / not complete. That direction costs one more
    round; the other direction stops on a goal nobody actually checked.
    """
    text = (raw or "").strip()
    row = _load_object(text)
    if row is None:
        match = _JSON_OBJECT.search(text)
        row = _load_object(match.group(0)) if match else None
    if row is None:
        return GoalVerdict(missing=get_prompt("goal_judge_unparsed"), parsed=False)
    try:
        score = float(row.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return GoalVerdict(
        score=max(0.0, min(1.0, score)),
        complete=bool(row.get("complete")),
        impossible=bool(row.get("impossible")),
        missing=str(row.get("missing") or "").strip(),
    )


def _load_object(text: str) -> dict | None:
    if not text:
        return None
    try:
        row = json.loads(text)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def render_transcript(messages: Sequence[AgentMessage], *, limit: int | None = None) -> str:
    """Tail-bounded transcript for the judge, with the system prompt left out.

    The system prompt runs to thousands of tokens and carries no evidence about this
    particular objective, so paying for it every round buys nothing. Tool names stay in:
    which tool produced a line is the judge's only way to tell running the tests apart from
    claiming to have run them.
    """
    budget = int(goal_settings()["transcript_chars"] if limit is None else limit)
    lines: list[str] = []
    total = 0
    for message in reversed(list(messages)):
        text = (message.text() or "").strip()
        calls = ", ".join(block.name for block in message.tool_calls())
        if not text and not calls:
            continue
        label = message.tool_name or message.role
        body = text if not calls else f"{text} [calls: {calls}]".strip()
        line = f"{label}: {body}"
        if budget > 0 and total + len(line) > budget:
            break
        lines.append(line)
        total += len(line)
    lines.reverse()
    return "\n".join(lines)


def model_judge(
    stream_fn,
    *,
    model=None,
    workspace_dir: str = "",
    project_id: str = "",
    agent_id: str = "",
    session_id: str = "",
) -> JudgeFn:
    """A judge backed by one model call per round. Use a small fast model here.

    Deliberately toolless: it rules on evidence the agent already surfaced into the
    transcript rather than going and looking for itself. That keeps adjudication at one cheap
    call, and it forces objectives to be written so the agent's own output can prove them —
    which is the property that makes the criterion checkable at all.
    """
    from witty_agent.types import AgentContext, ModelRef

    async def judge(objective: str, transcript: str) -> GoalVerdict:
        context = AgentContext(
            system_prompt=get_prompt("goal_judge_system"),
            messages=[
                AgentMessage(
                    role="user",
                    content=get_prompt("goal_judge_user", objective=objective, transcript=transcript),
                )
            ],
            tools=[],
            workspace_dir=workspace_dir,
            model=model or ModelRef(provider="openai", model_id="goal-judge"),
            project_id=project_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        try:
            reply = await stream_fn(context)
        except Exception as exc:
            logger.warning("判官调用失败 err=%s", exc)
            return GoalVerdict(missing=get_prompt("goal_judge_unparsed"), parsed=False)
        if reply.stop_reason == "error":
            return GoalVerdict(missing=get_prompt("goal_judge_unparsed"), parsed=False)
        verdict = parse_verdict(reply.text())
        logger.info(
            "判官裁定 complete=%s impossible=%s score=%.2f parsed=%s",
            verdict.complete,
            verdict.impossible,
            verdict.score,
            verdict.parsed,
        )
        return verdict

    return judge


def classify_failure(exc: BaseException) -> str:
    """"fatal" clears the goal; "transient" keeps it so the next round can retry.

    Rate limits and overload are the common case and they are transient — clearing the goal
    on those throws away work that was going to finish. Only the four classes that cannot
    resolve themselves (credentials, quota, a context overflow compaction could not fix, a
    model that is gone) are worth stopping for. Patterns live in `[goal]`, not here.
    """
    text = f"{type(exc).__name__}: {exc}"
    for pattern in goal_settings()["fatal_error_patterns"]:
        try:
            if re.search(pattern, text):
                return "fatal"
        except re.error:
            logger.warning("致命错误判据不是合法正则 pattern=%s", pattern)
    return "transient"


def _used_tools(messages: Sequence[AgentMessage]) -> bool:
    return any(message.tool_calls() for message in messages)


def _failure_guidance(report: GateReport, obligations: set[str]) -> str:
    regressions = [item for item in report.failures() if item.name in obligations]
    fresh = [item for item in report.failures() if item.name not in obligations]
    parts: list[str] = []
    if regressions:
        parts.append(get_prompt("goal_regression_failed", failures="\n".join(item.line() for item in regressions)))
    if fresh:
        parts.append(get_prompt("goal_gate_failed", failures="\n".join(item.line() for item in fresh)))
    return "\n\n".join(parts)


async def run_goal_loop(
    *,
    objective: str,
    scratch: Path,
    runner,
    max_rounds: int = GOAL_MAX_ROUNDS,
    budget: int = -1,
    judge: JudgeFn | None = None,
    gates: Sequence[GateSpec] = (),
    workspace: Path | None = None,
    stall_rounds: int | None = None,
    ledger_dir: Path | None = None,
) -> GoalState:
    """Run rounds until the criteria hold, the judge calls it impossible, or a cap trips.

    `runner(prompt)` may return that round's messages; returning None keeps the pre-judge
    behaviour (GOAL.yaml decides) so existing callers do not change meaning. With a judge or
    gates wired in, a self-declared `complete` is no longer sufficient on its own.

    `ledger_dir` 是回归义务台账的家。缺省落在 `scratch`（一次运行内有效）；`session.run_goal`
    传的是 Agent 级、按工作区分的目录，让「验过的判据」跨会话一直成立。
    """
    path = goal_path(scratch)
    write_goal(path, objective)
    settings = goal_settings()
    stall_limit = int(settings["stall_rounds"] if stall_rounds is None else stall_rounds)
    ledger = ObligationLedger(ledger_dir or scratch)
    runner_gates = GateRunner(workspace) if workspace is not None else None
    used = 0
    idle = 0
    guidance = ""

    def finish(status: str, index: int, **extra) -> GoalState:
        # 目标文件跟着裁定走，不跟着模型的申报走：事后翻 GOAL.yaml 看到的应该是判据的结论。
        # `stalled` 例外——那是交还控制权，目标本身还在，下一条用户消息可以接着跑。
        if status != "stalled":
            write_goal(path, objective, status=status, round_no=index)
        logger.info("goal 结束 status=%s round=%s", status, index)
        return GoalState(status=status, objective=objective, round=index, path=path, **extra)

    for index in range(1, max_rounds + 1):
        prompt = _round_prompt(objective, path, index, budget, used)
        if guidance:
            prompt = f"{prompt}\n\n{guidance}"
            guidance = ""
        try:
            produced = await runner(prompt)
        except Exception as exc:
            kind = classify_failure(exc)
            logger.warning("goal 轮次失败 round=%s kind=%s err=%s", index, kind, exc)
            if kind == "fatal":
                return finish("error", index, reason=f"{type(exc).__name__}: {exc}")
            used += 1
            guidance = get_prompt("goal_transient_retry", error=str(exc))
            continue
        used += 1
        messages = list(produced) if isinstance(produced, list) else []
        status = read_goal_status(path)
        logger.info("goal 轮次 round=%s status=%s messages=%s", index, status, len(messages))
        if status == "blocked":
            return finish("blocked", index)

        if messages:
            idle = idle + 1 if not _used_tools(messages) else 0
            if stall_limit > 0 and idle >= stall_limit:
                # 业界通行做法：连着几轮只说话不动工具就停下交还控制权，目标留着。
                return finish("stalled", index, reason=get_prompt("goal_stall_stop", count=str(idle)))

        obligations = ledger.load()
        specs = merge_specs(obligations, gates)
        report = runner_gates.run(specs) if (runner_gates is not None and specs) else GateReport()
        if report.failures():
            guidance = _failure_guidance(report, {item.name for item in obligations})
            continue
        _record_new_obligations(ledger, report, {item.name for item in obligations}, specs)

        verdict: GoalVerdict | None = None
        if judge is not None and messages:
            verdict = await judge(objective, render_transcript(messages))
            if verdict.impossible:
                return finish("impossible", index, verdict=verdict, reason=verdict.missing, gates=report)
            if verdict.complete:
                return finish("complete", index, verdict=verdict, gates=report)
            guidance = get_prompt("goal_followup", missing=verdict.missing or get_prompt("goal_judge_unparsed"))
        elif status == "complete":
            # 没接判官时保持旧口径：GOAL.yaml 说完成就算完成。接了判官，申报不再算数。
            return finish("complete", index, gates=report)
        if budget > 0 and used >= budget:
            return finish("budget", index, verdict=verdict, gates=report)

    return finish("max_rounds", max_rounds)


def _record_new_obligations(
    ledger: ObligationLedger,
    report: GateReport,
    known: set[str],
    specs: Sequence[GateSpec],
) -> None:
    """A gate that just went green becomes an obligation, so it has to stay green."""
    by_name = {spec.name: spec for spec in specs}
    for result in report.passed():
        if result.name in known or result.skipped:
            continue
        spec = by_name.get(result.name)
        if spec is None:
            continue
        ledger.record(Obligation(name=spec.name, command=spec.command, timeout_sec=spec.timeout_sec))
