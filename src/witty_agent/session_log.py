"""会话日志是模型可见历史的来源。

规则：抵达模型请求的一切都必须能从日志重建（模型可见即已记录）。
不引入插件事件总线；这里只保留可回放的事件折叠。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from witty_agent.prompts import get_prompt
from witty_agent.types import AgentMessage, TextBlock, ToolCallBlock

TODO_STATUSES = ("pending", "in_progress", "completed")
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"


@dataclass
class SessionLogEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    time_ms: int = 0
    seq: int = 0


@dataclass
class SessionLog:
    events: list[SessionLogEvent] = field(default_factory=list)
    _next_seq: int = 1

    def append(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        time_ms: int | None = None,
    ) -> SessionLogEvent:
        event = SessionLogEvent(
            type=event_type,
            data=dict(data or {}),
            time_ms=int(time_ms if time_ms is not None else time.time() * 1000),
            seq=self._next_seq,
        )
        self._next_seq += 1
        self.events.append(event)
        return event

    def hydrate(self, events: list[SessionLogEvent]) -> None:
        self.events = list(events)
        self._next_seq = max((item.seq for item in self.events), default=0) + 1

    def turn_and_step(self) -> tuple[int, int]:
        turn = 0
        step = 0
        for event in self.events:
            if event.type == "turn/start":
                turn = int(event.data.get("turn") or turn + 1)
                step = 0
            elif event.type == "step/start":
                step = int(event.data.get("step") or step + 1)
        return turn, step

    def has_open_turn(self) -> bool:
        open_turn = False
        for event in self.events:
            if event.type == "turn/start":
                open_turn = True
            elif event.type == "turn/end":
                open_turn = False
        return open_turn


def fold_todos(events: list[SessionLogEvent], *, end: int | None = None) -> list[dict[str, str]] | None:
    """最近一次 todo/write 整表；没有任何写入则为 None。"""
    todos: list[dict[str, str]] | None = None
    limit = len(events) if end is None else end
    for event in events[:limit]:
        if event.type == "todo/write":
            raw = event.data.get("todos")
            if isinstance(raw, list):
                todos = [
                    {"content": str(item.get("content", "")), "status": str(item.get("status", ""))}
                    for item in raw
                    if isinstance(item, dict)
                ]
    return todos


def project_todos(events: list[SessionLogEvent]) -> list[dict[str, str]] | None:
    """当前有效计划：最近一次 todo/write，下一个 turn/start 清为 None。"""
    todos: list[dict[str, str]] | None = None
    for event in events:
        if event.type == "todo/write":
            todos = fold_todos([event])
        elif event.type == "turn/start":
            todos = None
    return todos


def fold_plan_mode(events: list[SessionLogEvent], *, end: int | None = None) -> bool:
    """最后一条 plan/mode 生效；没有则未激活。"""
    active = False
    limit = len(events) if end is None else end
    for event in events[:limit]:
        if event.type == "plan/mode":
            active = bool(event.data.get("active"))
    return active


def _pending_calls(events: list[SessionLogEvent]) -> dict[str, dict[str, object]]:
    """assistant 已点名、尚未有 tool/result 的调用。turn/end 不清掉，方便修已收口的残尾。"""
    pending: dict[str, dict[str, object]] = {}
    for event in events:
        if event.type == "assistant/message":
            for call in event.data.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id:
                    continue
                pending[call_id] = {
                    "name": str(call.get("name") or ""),
                    "started": False,
                    "step": event.data.get("step"),
                }
        elif event.type == "tool/call":
            call_id = str(event.data.get("id") or event.data.get("callId") or "")
            if call_id in pending:
                pending[call_id]["started"] = True
                pending[call_id]["call_seq"] = event.seq
        elif event.type == "tool/result":
            pending.pop(str(event.data.get("tool_call_id") or ""), None)
    return pending


def _result_closer(
    call_id: str,
    info: dict[str, object],
    *,
    time_ms: int,
    turn: int | None,
) -> SessionLogEvent:
    started = bool(info.get("started"))
    return SessionLogEvent(
        type="tool/result",
        data={
            "tool_call_id": call_id,
            "tool_name": str(info.get("name") or ""),
            "text": get_prompt("tool_outcome_unknown" if started else "tool_not_started"),
            "is_error": True,
            "code": TOOL_OUTCOME_UNKNOWN if started else TOOL_NOT_STARTED,
            **({"turn": turn} if turn is not None else {}),
            **({"step": info["step"]} if info.get("step") is not None else {}),
        },
        time_ms=time_ms,
    )


def unpaired_call_results(events: list[SessionLogEvent]) -> list[SessionLogEvent]:
    """给没有结果的工具调用补一条错误 tool/result，不改 turn 边界。"""
    if not events:
        return []
    last = events[-1]
    return [
        _result_closer(call_id, info, time_ms=last.time_ms, turn=None)
        for call_id, info in _pending_calls(events).items()
    ]


def interrupted_turn_closers(events: list[SessionLogEvent]) -> list[SessionLogEvent]:
    """对照官方 interruptedTurnClosers：收口未结束的一轮。"""
    open_turn: int | None = None
    open_step: int | None = None
    for event in events:
        if event.type == "turn/start":
            open_turn = int(event.data.get("turn") or 0)
            open_step = None
        elif event.type == "turn/end":
            open_turn = None
            open_step = None
        elif event.type == "step/start":
            open_step = int(event.data.get("step") or 0)
        elif event.type == "step/end":
            open_step = None
    if open_turn is None or not events:
        return []
    last = events[-1]
    closers = unpaired_call_results(events)
    if open_step is not None:
        closers.append(
            SessionLogEvent(
                type="step/end",
                data={"turn": open_turn, "step": open_step},
                time_ms=last.time_ms,
            )
        )
    closers.append(
        SessionLogEvent(
            type="turn/end",
            data={"turn": open_turn, "reason": "interrupted"},
            time_ms=last.time_ms,
        )
    )
    return closers


def result_message_from_repair(event: SessionLogEvent) -> AgentMessage:
    return AgentMessage(
        role="toolResult",
        content=str(event.data.get("text") or ""),
        tool_call_id=str(event.data.get("tool_call_id") or ""),
        tool_name=str(event.data.get("tool_name") or ""),
        is_error=True,
        source="plugin:session-repair",
    )


def repair_session_log(log: SessionLog) -> list[SessionLogEvent]:
    """把残尾写入日志。开着的一轮走完整收口；已 turn/end 的只补缺失结果。"""
    if log.has_open_turn():
        closers = interrupted_turn_closers(log.events)
    else:
        closers = unpaired_call_results(log.events)
    added: list[SessionLogEvent] = []
    for item in closers:
        added.append(log.append(item.type, item.data, time_ms=item.time_ms))
    return added


def derive_messages(events: list[SessionLogEvent]) -> list[AgentMessage]:
    """从日志投影模型历史。空 assistant 文本且无工具调用不进入历史。"""
    messages: list[AgentMessage] = []
    for event in events:
        if event.type == "user/message":
            raw_meta = event.data.get("meta")
            messages.append(
                AgentMessage(
                    role="user",
                    content=str(event.data.get("text") or ""),
                    source=event.data.get("source"),
                    meta=dict(raw_meta) if isinstance(raw_meta, dict) else {},
                )
            )
        elif event.type == "assistant/message":
            text = str(event.data.get("text") or "")
            calls = event.data.get("tool_calls") or []
            if not text and not calls:
                continue
            if calls:
                blocks: list[TextBlock | ToolCallBlock] = []
                if text:
                    blocks.append(TextBlock(text=text))
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    blocks.append(
                        ToolCallBlock(
                            id=str(call.get("id") or ""),
                            name=str(call.get("name") or ""),
                            arguments=dict(call.get("arguments") or {}),
                        )
                    )
                messages.append(
                    AgentMessage(
                        role="assistant",
                        content=blocks,
                        stop_reason=event.data.get("stop_reason"),
                        reasoning=str(event.data.get("reasoning") or ""),
                        source=event.data.get("source"),
                    )
                )
            else:
                messages.append(
                    AgentMessage(
                        role="assistant",
                        content=text,
                        stop_reason=event.data.get("stop_reason"),
                        reasoning=str(event.data.get("reasoning") or ""),
                        source=event.data.get("source"),
                    )
                )
        elif event.type == "tool/result":
            messages.append(
                AgentMessage(
                    role="toolResult",
                    content=str(event.data.get("text") or ""),
                    tool_call_id=event.data.get("tool_call_id"),
                    tool_name=event.data.get("tool_name"),
                    is_error=bool(event.data.get("is_error")),
                )
            )
    for event in unpaired_call_results(events):
        messages.append(result_message_from_repair(event))
    return messages


def event_to_record(event: SessionLogEvent) -> dict[str, Any]:
    return {
        "type": "session_event",
        "event": event.type,
        "seq": event.seq,
        "time_ms": event.time_ms,
        "data": event.data,
    }


def event_from_record(record: dict[str, Any]) -> SessionLogEvent | None:
    if record.get("type") != "session_event":
        return None
    return SessionLogEvent(
        type=str(record.get("event") or ""),
        data=dict(record.get("data") or {}),
        time_ms=int(record.get("time_ms") or 0),
        seq=int(record.get("seq") or 0),
    )
