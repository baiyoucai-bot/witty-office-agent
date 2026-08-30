"""库调用的对外结果。默认只要最终分析正文；steps/tools 用来区分类型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from witty_agent.loop import LoopResult
from witty_agent.types import AgentEvent


def result_text(result: LoopResult | RunResult) -> str:
    if isinstance(result, RunResult):
        return result.text
    for message in reversed(result.messages):
        if message.role == "assistant":
            return message.text()
    return ""


@dataclass
class RunResult:
    """默认当字符串用就是分析后的结果。"""

    text: str
    ok: bool = True
    stop_reason: str = "end_turn"
    session_id: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def public_event(event: AgentEvent) -> dict[str, Any] | None:
    """把内部事件收成前端可用的一小段。默认 run 不推这些。"""
    kind = event.type
    if kind == "tool_execution_start":
        return {
            "type": "tool_start",
            "id": event.tool_call_id or "",
            "name": event.tool_name or "",
            "args": dict(event.args or {}),
        }
    if kind == "tool_execution_end":
        message = event.message
        output = message.text() if message is not None else ""
        error = bool(message is not None and message.is_error)
        return {
            "type": "tool_end",
            "id": event.tool_call_id or "",
            "name": event.tool_name or "",
            "ok": not error,
            "output": output,
        }
    if kind == "approval_required":
        return {
            "type": "approval",
            "id": event.tool_call_id or "",
            "name": event.tool_name or "",
            "args": dict(event.args or {}),
        }
    return None


def build_run_result(loop: LoopResult, *, session_id: str) -> RunResult:
    text = ""
    stop = "end_turn"
    ok = True
    tools: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for message in loop.messages:
        if message.role == "assistant":
            if message.stop_reason:
                stop = message.stop_reason
            if message.stop_reason in {"error", "aborted"}:
                ok = False
            body = message.text()
            calls = message.tool_calls()
            if body.strip() and not calls:
                text = body
                steps.append({"type": "text", "text": body})
            for call in calls:
                item = {
                    "type": "tool",
                    "id": call.id,
                    "name": call.name,
                    "args": dict(call.arguments or {}),
                }
                pending[call.id] = item
                steps.append({"type": "tool_start", "id": call.id, "name": call.name, "args": item["args"]})
        elif message.role == "toolResult":
            call_id = message.tool_call_id or ""
            item = pending.pop(call_id, {"type": "tool", "id": call_id, "name": message.tool_name or ""})
            item["ok"] = not message.is_error
            item["output"] = message.text()
            if message.is_error:
                item["error"] = message.text()
                ok = ok and False
            tools.append(item)
            steps.append(
                {
                    "type": "tool_end",
                    "id": call_id,
                    "name": item.get("name") or message.tool_name or "",
                    "ok": item["ok"],
                    "output": item["output"],
                }
            )
    if stop in {"error", "aborted"}:
        ok = False
    if not text:
        text = result_text(loop)
    return RunResult(
        text=text,
        ok=ok,
        stop_reason=stop,
        session_id=session_id,
        tools=tools,
        steps=steps,
    )
