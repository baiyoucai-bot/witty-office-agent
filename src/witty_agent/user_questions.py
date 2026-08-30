"""工具要等人回答才能继续时用的提供方无关接口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from witty_agent.prompts import get_prompt


class UserQuestionError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.name = "UserQuestionError"


@dataclass
class AskUserQuestionOption:
    label: str
    description: str = ""


@dataclass
class AskUserQuestionItem:
    id: str
    question: str
    detail: str = ""
    header: str = ""
    options: list[AskUserQuestionOption] = field(default_factory=list)
    multi_select: bool = False
    intent: dict[str, Any] | None = None


@dataclass
class AskUserAnswerItem:
    id: str
    selected: list[str] = field(default_factory=list)
    custom: str = ""


@dataclass
class AskUserAnswer:
    answers: list[AskUserAnswerItem] = field(default_factory=list)


AskProvider = Callable[[list[AskUserQuestionItem]], Awaitable[AskUserAnswer] | AskUserAnswer]


class UserQuestionService:
    """同一上下文只能有一个活跃提供方。"""

    def __init__(self) -> None:
        self._provider: AskProvider | None = None

    def register_provider(self, provider: AskProvider | None) -> None:
        self._provider = provider

    def has_provider(self) -> bool:
        return self._provider is not None

    async def ask(self, questions: list[AskUserQuestionItem]) -> AskUserAnswer:
        if not questions:
            raise UserQuestionError(get_prompt("ask_user_empty"), "EMPTY_QUESTIONS")
        if self._provider is None:
            raise UserQuestionError(get_prompt("ask_user_no_provider"), "NO_PROVIDER")
        result = self._provider(questions)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        if not isinstance(result, AskUserAnswer):
            raise UserQuestionError(get_prompt("ask_user_no_provider"), "NO_PROVIDER")
        return result
