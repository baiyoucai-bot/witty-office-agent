"""exit_plan_mode：呈交完整 markdown 计划，等人审再退出。"""

from __future__ import annotations

import json

from witty_agent import hooks
from witty_agent.plan_mode import first_heading
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool
from witty_agent.user_questions import (
    AskUserQuestionItem,
    AskUserQuestionOption,
    UserQuestionError,
)


async def exit_plan_mode(plan: str) -> str:
    """仅在计划模式中使用：把完整计划交给用户审，批准后退出计划模式。"""
    controller = hooks.plan_mode
    log = hooks.session_log
    questions = hooks.user_questions
    if controller is None or not controller.get(log).active:
        raise RuntimeError(get_prompt("plan_exit_inactive"))
    if not first_heading(plan):
        raise ValueError(get_prompt("plan_exit_need_heading"))
    if questions is None:
        raise RuntimeError(get_prompt("ask_user_no_provider"))
    try:
        answer = await questions.ask(
            [
                AskUserQuestionItem(
                    id="plan-review",
                    question=get_prompt("plan_review_question"),
                    detail=plan,
                    header=get_prompt("plan_review_header"),
                    options=[
                        AskUserQuestionOption(label=get_prompt("plan_review_approve")),
                        AskUserQuestionOption(label=get_prompt("plan_review_keep")),
                    ],
                    intent={"kind": "plan-review", "approve": get_prompt("plan_review_approve")},
                )
            ]
        )
    except UserQuestionError as exc:
        raise RuntimeError(str(exc)) from exc
    item = next((row for row in answer.answers if row.id == "plan-review"), None)
    approved_label = get_prompt("plan_review_approve")
    selected = item.selected if item is not None else []
    custom = item.custom if item is not None else ""
    if item is not None and approved_label in selected and not custom:
        controller.set(log, False, narrate=False)
        return json.dumps({"approved": True})
    feedback = custom or (", ".join(selected) if selected else get_prompt("plan_review_keep"))
    return json.dumps({"approved": False, "feedback": feedback})


register_tool(
    ToolSpec(
        name="exit_plan_mode",
        description=get_prompt("tool_desc_exit_plan_mode"),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": get_prompt("plan_param_plan"),
                }
            },
            "required": ["plan"],
        },
        func=exit_plan_mode,
    )
)
