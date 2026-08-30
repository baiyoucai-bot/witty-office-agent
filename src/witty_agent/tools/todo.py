"""todo_write：每次整表替换，并追加 todo/write 会话事件。"""

from __future__ import annotations

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.todo import apply_todo_write, describe
from witty_agent.tools.registry import ToolSpec, register_tool


def todo_write(todos: list | None = None, todos_json: str | None = None) -> str:
    """把完整任务列表写入当前 agent 会话。"""
    raw = todos if todos is not None else todos_json
    return apply_todo_write(hooks.session_log, raw)


def _spec() -> ToolSpec:
    return ToolSpec(
        name="todo_write",
        description=describe(),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": get_prompt("todo_param_todos"),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": get_prompt("todo_param_content"),
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": get_prompt("todo_param_status"),
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
        func=todo_write,
    )


def refresh_todo_tool() -> ToolSpec:
    spec = _spec()
    register_tool(spec)
    return spec


refresh_todo_tool()
