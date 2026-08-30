"""模型调用重试：超时/网络/5xx 重试，鉴权与配额错误不重试。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TypeVar

from witty_agent.logging import get_logger
from witty_agent.types import AgentMessage

logger = get_logger("retry")
T = TypeVar("T")

_NON_RETRYABLE = re.compile(
    r"401|403|invalid api key|unauthorized|insufficient_quota|quota exceeded|out of budget|billing",
    re.I,
)
_RETRYABLE = re.compile(
    r"overloaded|rate.?limit|too many requests|429|408|409|500|502|503|504|524|"
    r"service.?unavailable|server.?error|internal.?error|network|connection|"
    r"timed? ?out|timeout|fetch failed|socket hang up|reset before headers|"
    r"please retry|try your request again",
    re.I,
)


class RetryableLLMError(RuntimeError):
    """瞬时失败，允许按策略再试。"""


async def retry_call(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> T:
    last: Exception | None = None
    for index in range(max(1, attempts)):
        try:
            return await factory()
        except Exception as exc:
            last = exc
            if not is_retryable_error(exc) or index == attempts - 1:
                raise
            delay = base_delay * (2**index)
            logger.warning("重试 %s/%s delay=%.1f err=%s", index + 1, attempts, delay, exc)
            if delay > 0:
                await asyncio.sleep(delay)
    raise last or RuntimeError("retry exhausted")


async def retry_assistant_call(
    produce: Callable[[], Awaitable[AgentMessage]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> AgentMessage:
    last: AgentMessage | None = None
    for index in range(max(1, attempts)):
        try:
            message = await produce()
        except Exception as exc:
            if not is_retryable_error(exc) or index == attempts - 1:
                return AgentMessage(role="assistant", content=str(exc), stop_reason="error")
            message = AgentMessage(role="assistant", content=str(exc), stop_reason="error")
        if message.stop_reason == "aborted":
            return message
        if not should_retry_message(message) or index == attempts - 1:
            return message
        last = message
        delay = base_delay * (2**index)
        logger.warning("助手重试 %s/%s delay=%.1f", index + 1, attempts, delay)
        if delay > 0:
            await asyncio.sleep(delay)
    return last or AgentMessage(role="assistant", content="retry exhausted", stop_reason="error")


def should_retry_message(message: AgentMessage) -> bool:
    if message.stop_reason != "error":
        return False
    return is_retryable_text(message.text())


def is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, RetryableLLMError):
        return True
    return is_retryable_text(str(exc))


def is_retryable_text(text: str) -> bool:
    if not text:
        return False
    if _NON_RETRYABLE.search(text):
        return False
    return bool(_RETRYABLE.search(text))
