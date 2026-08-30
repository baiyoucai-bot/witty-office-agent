"""给调用方当库后台跑。工作区默认是调用方 cwd。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from witty_agent.llm import OpenAICompatLLM
from witty_agent.logging import setup_logging
from witty_agent.permission import (
    AskFn,
    PermissionLevel,
    PermissionPolicy,
    TimeoutAction,
    normalize_level,
    normalize_timeout_action,
)
from witty_agent.result import RunResult, build_run_result, public_event, result_text
from witty_agent.runtime import library_settings
from witty_agent.session import Session, WittyAgent, create_agent, create_session
from witty_agent.types import AgentEvent


class Witty:
    """pip 安装后的后台入口。桌面窗口不用。"""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        permission: str | None = None,
        timeout_sec: float | None = None,
        on_timeout: str | None = None,
        ask: AskFn | None = None,
        inbox: str | Path | None = None,
        api_key: str = "",
        base_url: str = "",
        model_id: str = "",
        llm: Callable[..., object] | None = None,
        on_event: Callable[[dict], object] | None = None,
        log_level: str | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
        agent_id: str = DEFAULT_AGENT_ID,
        root: str | Path | None = None,
    ) -> None:
        settings = library_settings()
        setup_logging(
            level=log_level or str(settings["log_level"]),
            force=True,
        )
        level = normalize_level(permission if permission is not None else str(settings["permission"]))
        wait = float(timeout_sec if timeout_sec is not None else settings["timeout_sec"])
        timeout_action = normalize_timeout_action(
            on_timeout if on_timeout is not None else str(settings["on_timeout"])
        )
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.policy = PermissionPolicy(
            level=level,
            timeout_sec=wait,
            on_timeout=timeout_action,
            ask=ask,
            inbox=Path(inbox).expanduser() if inbox is not None else None,
        )
        data = Path(root).expanduser().resolve() if root is not None else None
        self.agent: WittyAgent = create_agent(project_id, agent_id, root=data)
        self.session: Session = create_session(self.agent, workspace_dir=self.workspace)
        self.on_event = on_event
        self.llm = llm or OpenAICompatLLM(
            model_id=model_id,
            api_key=api_key,
            base_url=base_url,
        )
        if on_event is not None and hasattr(self.llm, "on_text_delta"):
            self.llm.on_text_delta = lambda chunk: on_event(
                {"type": "text_delta", "text": str(chunk)}
            )

    @property
    def permission(self) -> PermissionLevel:
        return self.policy.level

    async def arun(self, prompt: str) -> RunResult:
        async def emit(event: AgentEvent) -> None:
            if self.on_event is None:
                return
            payload = public_event(event)
            if payload is None:
                return
            maybe = self.on_event(payload)
            if asyncio.iscoroutine(maybe):
                await maybe

        raw = await self.session.run(
            prompt,
            stream_fn=self.llm,  # type: ignore[arg-type]
            approve=self.policy.make_approve(self.session.session_id),
            approval_mode=self.policy.approval_mode(),
            emit=emit if self.on_event is not None else None,
        )
        result = build_run_result(raw, session_id=self.session.session_id)
        if self.on_event is not None:
            maybe = self.on_event(
                {"type": "done", "text": result.text, "ok": result.ok, "stop_reason": result.stop_reason}
            )
            if asyncio.iscoroutine(maybe):
                await maybe
        return result

    def run(self, prompt: str) -> RunResult:
        return asyncio.run(self.arun(prompt))
