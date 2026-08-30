"""断言可观察事实，不检查服务是否存在。"""

from __future__ import annotations

from collections.abc import Callable

from witty_agent.logging import get_logger
from witty_agent.session_log import SessionLog, derive_messages
from witty_agent.types import AgentMessage

logger = get_logger("invariants")

Checker = Callable[[SessionLog, list[AgentMessage]], list[str]]

_CHECKS: dict[str, Checker] = {}


class InvariantError(RuntimeError):
    def __init__(self, package: str, message: str) -> None:
        super().__init__(f'invariant violated by "{package}": {message}')
        self.package = package
        self.code = "INVARIANT"


def register(name: str, checker: Checker) -> None:
    _CHECKS[name] = checker


def check_visible_logged(log: SessionLog, messages: list[AgentMessage]) -> list[str]:
    """模型可见即已记录：本轮 user/assistant/toolResult 必须能从日志投影出来。"""
    derived = {(item.role, item.text()) for item in derive_messages(log.events)}
    failures: list[str] = []
    for message in messages:
        if message.role not in {"user", "assistant", "toolResult"}:
            continue
        if message.role == "assistant" and not message.text() and not message.tool_calls():
            continue
        if (message.role, message.text()) not in derived:
            failures.append(f"{message.role} not in derived history: {message.text()[:80]}")
    return failures


def run_invariants(log: SessionLog, messages: list[AgentMessage], *, strict: bool = False) -> list[str]:
    failures: list[str] = []
    for name, checker in _CHECKS.items():
        found = checker(log, messages)
        for item in found:
            failures.append(f"{name}: {item}")
            logger.warning("不变式失败 name=%s detail=%s", name, item)
    if strict and failures:
        raise InvariantError("session_log", "; ".join(failures))
    return failures


register("session_log", check_visible_logged)
