"""ask_user_question：消费 userQuestions 能力 seam。"""

from __future__ import annotations

import json
from typing import Any

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool
from witty_agent.user_questions import (
    AskUserQuestionItem,
    AskUserQuestionOption,
    UserQuestionError,
)


async def ask_user_question(questions: list | None = None, questions_json: str | None = None) -> str:
    """向用户提一个或多个问题，等回答后再继续。"""
    raw = questions if questions is not None else questions_json
    if isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        parsed = raw
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(get_prompt("ask_user_empty"))
    items: list[AskUserQuestionItem] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError(get_prompt("ask_user_empty"))
        options = [
            AskUserQuestionOption(
                label=str(opt.get("label") or ""),
                description=str(opt.get("description") or ""),
            )
            for opt in (entry.get("options") or [])
            if isinstance(opt, dict)
        ]
        items.append(
            AskUserQuestionItem(
                id=str(entry.get("id") or ""),
                question=str(entry.get("question") or ""),
                header=str(entry.get("header") or ""),
                options=options,
                multi_select=bool(entry.get("multi_select") or entry.get("multiSelect")),
            )
        )
        if not items[-1].id or not items[-1].question:
            raise ValueError(get_prompt("ask_user_empty"))
    service = hooks.user_questions
    if service is None:
        raise RuntimeError(get_prompt("ask_user_no_provider"))
    try:
        answer = await service.ask(items)
    except UserQuestionError as exc:
        raise RuntimeError(str(exc)) from exc
    payload: dict[str, Any] = {
        "answers": [
            {
                "id": row.id,
                "selected": list(row.selected),
                **({"custom": row.custom} if row.custom else {}),
            }
            for row in answer.answers
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="ask_user_question",
        description=get_prompt("tool_desc_ask_user_question"),
        parameters={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": get_prompt("ask_user_param_questions"),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "question": {"type": "string"},
                            "header": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi_select": {"type": "boolean"},
                        },
                        "required": ["id", "question"],
                    },
                }
            },
            "required": ["questions"],
        },
        func=ask_user_question,
    )
)
