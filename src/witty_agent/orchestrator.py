"""调度者：对照业界 agent harness 通行做法。

不搬插件树 / Web UI，只保留
Python 底座需要的调度面：

    会话日志是模型历史来源；能力 seam 可替换；
    Planner → Executor → tools → 终止检查（完成 / 预算 / 轮次 / 中止）；
    plan 作业先进入记日志的计划模式；fanout 并行子任务。

本模块在 Session 循环之上：分活、盯活、收活。不替代 loop.py。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from witty_agent.dispatch import assess_fanout
from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, jobs_dir
from witty_agent.logging import get_logger
from witty_agent.memory_harvest import last_assistant_text
from witty_agent.prompts import get_prompt
from witty_agent.schedule import Fire, Scheduler
from witty_agent.session import Session, create_agent, create_session
from witty_agent.types import AgentMessage

logger = get_logger("orchestrator")
StreamFn = Callable[..., Awaitable[AgentMessage]]
JobKind = Literal["chat", "plan", "goal", "fanout", "schedule"]
JobStatus = Literal["queued", "running", "completed", "budget", "max_rounds", "aborted", "failed"]
MAX_FANOUT = 8


@dataclass
class JobSpec:
    prompt: str
    kind: JobKind = "chat"
    project_id: str = DEFAULT_PROJECT_ID
    agent_id: str = DEFAULT_AGENT_ID
    workspace: str | Path | None = None
    session_id: str | None = None
    budget_rounds: int = -1
    max_turns: int = -1
    fanout_prompts: list[str] = field(default_factory=list)
    approval_mode: str = "allow-all"


@dataclass
class JobResult:
    job_id: str
    kind: str
    status: JobStatus
    text: str
    session_id: str = ""
    children: list[str] = field(default_factory=list)
    rounds: int = 0
    error: str = ""


class Orchestrator:
    """项目级调度者：一种入口派发多种跑法，并做终止判定。"""

    def __init__(
        self,
        root: Path,
        stream_fn: StreamFn,
        *,
        approve: Callable[..., Awaitable[str]] | None = None,
        max_fanout: int = MAX_FANOUT,
    ) -> None:
        self.root = root
        self.stream_fn = stream_fn
        self.approve = approve
        self.max_fanout = max(1, min(max_fanout, MAX_FANOUT))
        self.jobs: dict[str, JobResult] = {}

    async def dispatch(self, spec: JobSpec) -> JobResult:
        job_id = uuid.uuid4().hex
        result = JobResult(job_id=job_id, kind=spec.kind, status="running", text="")
        self.jobs[job_id] = result
        self._save(result, spec)
        logger.info("调度派发 job=%s kind=%s agent=%s", job_id, spec.kind, spec.agent_id)
        try:
            if spec.kind == "fanout":
                result = await self._run_fanout(job_id, spec)
            elif spec.kind == "goal":
                result = await self._run_goal(job_id, spec)
            elif spec.kind == "plan":
                result = await self._run_plan(job_id, spec)
            elif spec.kind == "schedule":
                result = await self._run_chat(job_id, spec)
            else:
                result = await self._run_chat(job_id, spec)
        except Exception as exc:
            result = JobResult(
                job_id=job_id, kind=spec.kind, status="failed", text="", error=str(exc)
            )
            logger.warning("调度失败 job=%s err=%s", job_id, exc)
        self.jobs[job_id] = result
        self._save(result, spec)
        return result

    async def fanout(self, prompts: list[str], spec: JobSpec) -> JobResult:
        copy = JobSpec(**{**spec.__dict__, "kind": "fanout", "fanout_prompts": list(prompts)})
        return await self.dispatch(copy)

    async def tick_and_run(self) -> list[JobResult]:
        """定时任务真正入队：扫 fire，再派进会话。"""
        fires = Scheduler(self.root).tick()
        results: list[JobResult] = []
        for fire in fires:
            spec = JobSpec(
                kind="schedule",
                prompt=fire.prompt,
                project_id=fire.project_id,
                agent_id=fire.agent_id,
                workspace=fire.workspace,
                session_id=fire.session_id,
            )
            results.append(await self.dispatch(spec))
        return results

    async def _session(self, spec: JobSpec) -> Session:
        agent = create_agent(spec.project_id, spec.agent_id, root=self.root)
        return create_session(agent, workspace_dir=spec.workspace, session_id=spec.session_id)

    async def _run_chat(self, job_id: str, spec: JobSpec) -> JobResult:
        session = await self._session(spec)
        last = ""
        rounds = 0
        status: JobStatus = "completed"
        budget = spec.budget_rounds
        while True:
            if spec.max_turns == 0:
                status = "max_rounds"
                break
            result = await session.run(
                spec.prompt if rounds == 0 else get_prompt("orchestrator_continue"),
                stream_fn=self.stream_fn,
                approve=self.approve,
                approval_mode=spec.approval_mode,  # type: ignore[arg-type]
            )
            rounds += 1
            # 交给上层的是「答案」，不是最后一条消息。闸门 nudge、收口 seal、
            # 模型空回一轮都会占住末位，取末位就把作业结果抹成空串。
            last = last_assistant_text(result.messages) or last
            stop = _terminate(result.messages, rounds, budget)
            if stop:
                status = stop
                break
            if result.messages and result.messages[-1].stop_reason in {"end_turn", "error", "aborted"}:
                if result.messages[-1].stop_reason == "aborted":
                    status = "aborted"
                elif result.messages[-1].stop_reason == "error":
                    status = "failed"
                break
            if budget <= 0:
                break
        return JobResult(
            job_id=job_id,
            kind=spec.kind,
            status=status,
            text=last,
            session_id=session.session_id,
            rounds=rounds,
        )

    async def _run_plan(self, job_id: str, spec: JobSpec) -> JobResult:
        session = await self._session(spec)
        session._hydrate_log()
        session.plan.set(session.log, True)
        plan_prompt = get_prompt("orchestrator_plan", task=spec.prompt)
        await session.run(
            plan_prompt,
            stream_fn=self.stream_fn,
            approve=self.approve,
            approval_mode=spec.approval_mode,  # type: ignore[arg-type]
        )
        exec_prompt = get_prompt("orchestrator_execute", task=spec.prompt)
        result = await session.run(
            exec_prompt,
            stream_fn=self.stream_fn,
            approve=self.approve,
            approval_mode=spec.approval_mode,  # type: ignore[arg-type]
        )
        last = last_assistant_text(result.messages)
        return JobResult(
            job_id=job_id,
            kind="plan",
            status="completed",
            text=last,
            session_id=session.session_id,
            rounds=2,
        )

    async def _run_goal(self, job_id: str, spec: JobSpec) -> JobResult:
        session = await self._session(spec)
        state = await session.run_goal(
            spec.prompt,
            stream_fn=self.stream_fn,
            approve=self.approve,
            approval_mode=spec.approval_mode,  # type: ignore[arg-type]
            budget=spec.budget_rounds,
        )
        mapped: JobStatus = "completed"
        if state.status == "budget":
            mapped = "budget"
        elif state.status == "max_rounds":
            mapped = "max_rounds"
        elif state.status == "blocked":
            mapped = "failed"
        return JobResult(
            job_id=job_id,
            kind="goal",
            status=mapped,
            text=state.status,
            session_id=session.session_id,
            rounds=state.round,
        )

    async def _run_fanout(self, job_id: str, spec: JobSpec) -> JobResult:
        prompts = [item.strip() for item in spec.fanout_prompts if item.strip()]
        if not prompts:
            prompts = _split_prompts(spec.prompt)
        decision = assess_fanout(prompts)
        if not decision.ok:
            return JobResult(
                job_id=job_id,
                kind="fanout",
                status="failed",
                text=decision.message,
                error=decision.code,
            )
        prompts = list(decision.tasks)[: self.max_fanout]

        async def one(index: int, prompt: str) -> tuple[str, str]:
            child_spec = JobSpec(
                kind="chat",
                prompt=prompt,
                project_id=spec.project_id,
                agent_id=spec.agent_id,
                workspace=spec.workspace,
                approval_mode=spec.approval_mode,
                max_turns=spec.max_turns,
            )
            child = await self._run_chat(f"{job_id}-{index}", child_spec)
            return child.session_id, f"[{index}] {child.text}"

        rows = await asyncio.gather(*[one(index, prompt) for index, prompt in enumerate(prompts, start=1)])
        children = [item[0] for item in rows]
        text = "\n".join(item[1] for item in rows)
        return JobResult(
            job_id=job_id,
            kind="fanout",
            status="completed",
            text=text,
            children=children,
            rounds=1,
        )

    def _save(self, result: JobResult, spec: JobSpec) -> None:
        directory = jobs_dir(spec.project_id, root=self.root)
        directory.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = asdict(result)
        payload["updated"] = datetime.now(timezone.utc).isoformat()
        (directory / f"{result.job_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def list_jobs(project_id: str, *, root: Path) -> list[dict[str, Any]]:
    directory = jobs_dir(project_id, root=root)
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def _terminate(messages: list[AgentMessage], rounds: int, budget: int) -> JobStatus | None:
    if budget > 0 and rounds >= budget:
        return "budget"
    last = messages[-1] if messages else None
    if last and last.stop_reason == "aborted":
        return "aborted"
    return None


def _split_prompts(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [line.strip(" -\t") for line in raw.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [raw]
