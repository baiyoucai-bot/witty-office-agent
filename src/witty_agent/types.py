"""消息、事件、模型类型。审批/轨迹叠在这套上面。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "toolResult"]
StopReason = Literal["end_turn", "toolUse", "length", "error", "aborted"]
EventType = Literal[
    "agent_start",
    "turn_start",
    "message_start",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
    "todos",
    "turn_end",
    "agent_end",
    "approval_required",
]


@dataclass
class TextBlock:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass
class ToolCallBlock:
    type: Literal["toolCall"] = "toolCall"
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


ContentBlock = TextBlock | ToolCallBlock


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class AgentMessage:
    role: Role
    content: str | list[ContentBlock]
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    stop_reason: StopReason | None = None
    usage: Usage = field(default_factory=Usage)
    source: str | None = None
    reasoning: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    trace_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        parts = [block.text for block in self.content if isinstance(block, TextBlock)]
        return "".join(parts)

    def tool_calls(self) -> list[ToolCallBlock]:
        if isinstance(self.content, str):
            return []
        return [block for block in self.content if isinstance(block, ToolCallBlock)]


@dataclass
class AgentEvent:
    type: EventType
    message: AgentMessage | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    tool_results: list[AgentMessage] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    reason: str | None = None


@dataclass
class ModelRef:
    provider: str
    model_id: str
    api_key: str = ""
    base_url: str = ""


@dataclass
class AgentContext:
    system_prompt: str
    messages: list[AgentMessage]
    tools: list[Any]
    workspace_dir: str
    model: ModelRef
    project_id: str
    agent_id: str
    session_id: str
