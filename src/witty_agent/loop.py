"""agent 核心循环：事件流、多轮工具、截断则整批失败。"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

import asyncio

from witty_agent.approval import ApproveFn, ApprovalMode, decide_approval
from witty_agent.guard import timeout_result_text
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.retry import retry_assistant_call
from witty_agent.session_log import SessionLog, result_message_from_repair, unpaired_call_results
from witty_agent.todo import current_todos
from witty_agent.tool_validation import ToolArgumentError, validate_tool_arguments
from witty_agent.types import AgentContext, AgentEvent, AgentMessage, ToolCallBlock

EmitFn = Callable[[AgentEvent], Awaitable[None]]
StreamFn = Callable[[AgentContext], Awaitable[AgentMessage]]

logger = get_logger("loop")

READONLY_TOOLS = frozenset(
    {
        "find",
        "grep",
        "job_list",
        "job_output",
        "list_available_skills",
        "list_commands",
        "ls",
        "memory_read",
        "memory_status",
        "plan_read",
        "python_repl_status",
        "read",
        "schedule_list",
        "session_query",
        "session_health",
        "spill_read",
        "web_fetch",
        "mail_status",
        "mail_list",
        "mail_read",
        "mail_analyze",
        "link_search",
        "link_resolve",
        "link_habits",
        "diary_read",
        "diary_list",
        "pptx_outline",
        "pptx_themes",
        "pptx_check",
        "pptx_list_boxes",
        "doc_qa",
        "agenda_digest",
        "week_digest",
        "table_qa",
        "wiki_search",
        "wiki_lint",
        "wiki_stats",
        "wiki_sources",
        "sql_sources",
        "sql_schema",
        "sql_tables",
        "sql_values",
        "sql_run",
        "sql_check",
    }
)


def should_parallelize(
    tool_calls: list[ToolCallBlock],
    *,
    mode: str = "sequential",
    enabled: bool = True,
) -> bool:
    """Parallel when asked, or when every call is an independent read-only lookup."""
    if mode == "parallel":
        return True
    if not enabled or len(tool_calls) < 2:
        return False
    return all((call.name or "") in READONLY_TOOLS for call in tool_calls)


@dataclass
class LoopConfig:
    convert_to_llm: Callable[[list[AgentMessage]], list[AgentMessage]] | None = None
    transform_context: Callable[[list[AgentMessage]], Awaitable[list[AgentMessage]]] | None = None
    get_steering_messages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    get_follow_up_messages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    should_stop_after_turn: Callable[[AgentMessage, list[AgentMessage]], Awaitable[bool]] | None = None
    approval_mode: ApprovalMode | str = "always-ask"
    approve: ApproveFn | None = None
    tool_execution: str = "sequential"
    auto_parallel: bool = True
    retry_attempts: int = 3
    retry_base_delay: float = 0.5
    is_aborted: Callable[[], Awaitable[bool]] | None = None
    max_turns: int = -1
    session_log: SessionLog | None = None
    pre_step: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    on_tool_result: Callable[[ToolCallBlock, AgentMessage], list[AgentMessage]] | None = None
    rewrite_tool_result: Callable[[ToolCallBlock, AgentMessage], AgentMessage] | None = None
    gate_tool: Callable[[ToolCallBlock], AgentMessage | None] | None = None
    tool_timeout_ms: int = 0
    blocks_tool: Callable[[str], bool] | None = None
    block_reason: Callable[[str], str] | None = None


@dataclass
class LoopResult:
    messages: list[AgentMessage] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)


async def _default_emit(event: AgentEvent) -> None:
    return None


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    stream_fn: StreamFn,
    config: LoopConfig | None = None,
    emit: EmitFn | None = None,
) -> LoopResult:
    cfg = config or LoopConfig()
    sink = emit or _default_emit
    events: list[AgentEvent] = []

    async def push(event: AgentEvent) -> None:
        events.append(event)
        await sink(event)

    new_messages = list(prompts)
    context.messages.extend(prompts)
    await push(AgentEvent(type="agent_start"))
    await push(AgentEvent(type="turn_start"))
    log = cfg.session_log
    turn_no = 1
    step_no = 0
    if log is not None:
        last_turn, _ = log.turn_and_step()
        turn_no = last_turn + 1 if last_turn else 1
        log.append("turn/start", {"turn": turn_no})
    for prompt in prompts:
        await push(AgentEvent(type="message_start", message=prompt))
        await push(AgentEvent(type="message_end", message=prompt))

    first_turn = True
    pending = await _safe_list(cfg.get_steering_messages)
    while True:
        has_more_tools = True
        while has_more_tools or pending:
            if not first_turn:
                await push(AgentEvent(type="turn_start"))
            first_turn = False
            entered: list[AgentMessage] = []
            if pending:
                for message in pending:
                    await push(AgentEvent(type="message_start", message=message))
                    await push(AgentEvent(type="message_end", message=message))
                    context.messages.append(message)
                    new_messages.append(message)
                    entered.append(message)
                pending = []

            extras = await _safe_list(cfg.pre_step)
            injected: list[AgentMessage] = []
            injected_ids: set[int] = set()
            for message in extras:
                context.messages.append(message)
                new_messages.append(message)
                entered.append(message)
                injected.append(message)
                injected_ids.add(id(message))

            if cfg.is_aborted and await cfg.is_aborted():
                aborted = AgentMessage(role="assistant", content="", stop_reason="aborted")
                context.messages.append(aborted)
                new_messages.append(aborted)
                await push(AgentEvent(type="turn_end", message=aborted, tool_results=[]))
                if log is not None:
                    log.append("turn/end", {"turn": turn_no})
                await push(AgentEvent(type="agent_end", messages=list(new_messages)))
                return LoopResult(messages=new_messages, events=events)

            step_no += 1
            if log is not None:
                log.append("step/start", {"turn": turn_no, "step": step_no})
                if first_turn is False and step_no == 1:
                    for prompt in prompts:
                        _log_user(log, prompt)
                for message in entered:
                    if id(message) in injected_ids:
                        continue
                    _log_user(log, message)
            for message in injected:
                await _emit_and_log_pre_step(message, log, push)

            assistant = await _stream_assistant(context, cfg, stream_fn)
            if cfg.is_aborted and await cfg.is_aborted():
                if assistant.tool_calls():
                    assistant = replace(assistant, stop_reason="aborted")
                else:
                    assistant = AgentMessage(role="assistant", content="", stop_reason="aborted")
            context.messages.append(assistant)
            new_messages.append(assistant)
            await push(AgentEvent(type="message_start", message=assistant))
            await push(AgentEvent(type="message_end", message=assistant))
            if log is not None:
                _log_assistant(log, assistant)

            if assistant.stop_reason in {"error", "aborted"}:
                tool_results = []
                if log is not None:
                    for item in unpaired_call_results(log.events):
                        logged = log.append(item.type, item.data, time_ms=item.time_ms)
                        result = result_message_from_repair(logged)
                        context.messages.append(result)
                        new_messages.append(result)
                        tool_results.append(result)
                        await push(
                            AgentEvent(
                                type="tool_execution_end",
                                tool_call_id=result.tool_call_id,
                                tool_name=result.tool_name,
                                message=result,
                            )
                        )
                    log.append("step/end", {"turn": turn_no, "step": step_no})
                    log.append(
                        "turn/end",
                        {
                            "turn": turn_no,
                            "reason": "interrupted" if assistant.stop_reason == "aborted" else assistant.stop_reason,
                        },
                    )
                await push(AgentEvent(type="turn_end", message=assistant, tool_results=tool_results))
                await push(AgentEvent(type="agent_end", messages=list(new_messages)))
                return LoopResult(messages=new_messages, events=events)

            tool_calls = assistant.tool_calls()
            tool_results: list[AgentMessage] = []
            has_more_tools = False
            if tool_calls:
                if assistant.stop_reason == "length":
                    tool_results = await _fail_truncated(tool_calls, push)
                elif should_parallelize(
                    tool_calls,
                    mode=str(cfg.tool_execution),
                    enabled=bool(cfg.auto_parallel),
                ):
                    if cfg.tool_execution != "parallel":
                        logger.info("本轮只读工具并行 count=%s", len(tool_calls))
                    tool_results = await _execute_tools_parallel(context, tool_calls, cfg, push)
                else:
                    tool_results = await _execute_tools(context, tool_calls, cfg, push)
                if cfg.rewrite_tool_result:
                    tool_results = [
                        cfg.rewrite_tool_result(call, item)
                        for call, item in zip(tool_calls, tool_results, strict=True)
                    ]
                has_more_tools = True
                for call, result in zip(tool_calls, tool_results, strict=True):
                    context.messages.append(result)
                    new_messages.append(result)
                    if log is not None:
                        log.append(
                            "tool/call",
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments": call.arguments,
                            },
                        )
                        log.append(
                            "tool/result",
                            {
                                "tool_call_id": call.id,
                                "tool_name": call.name,
                                "text": result.text(),
                                "is_error": result.is_error,
                            },
                        )
                    if cfg.on_tool_result:
                        for extra in cfg.on_tool_result(call, result):
                            context.messages.append(extra)
                            new_messages.append(extra)
                            if log is not None:
                                _log_user(log, extra)
                    if call.name in {"todo_write", "exit_plan_mode"} and log is not None:
                        todos = current_todos(log)
                        if todos:
                            await push(AgentEvent(type="todos", args={"todos": list(todos)}))

            await push(AgentEvent(type="turn_end", message=assistant, tool_results=tool_results))
            if log is not None:
                log.append("step/end", {"turn": turn_no, "step": step_no})
            if cfg.max_turns > 0:
                turns = sum(1 for item in new_messages if item.role == "assistant")
                if turns >= cfg.max_turns:
                    if log is not None:
                        log.append("turn/end", {"turn": turn_no})
                    await push(AgentEvent(type="agent_end", messages=list(new_messages)))
                    return LoopResult(messages=new_messages, events=events)
            if cfg.should_stop_after_turn and await cfg.should_stop_after_turn(
                assistant, new_messages
            ):
                if log is not None:
                    log.append("turn/end", {"turn": turn_no})
                await push(AgentEvent(type="agent_end", messages=list(new_messages)))
                return LoopResult(messages=new_messages, events=events)
            pending = await _safe_list(cfg.get_steering_messages)
            if not tool_calls:
                has_more_tools = False

        follow = await _safe_list(cfg.get_follow_up_messages)
        if follow:
            pending = follow
            continue
        break

    if log is not None:
        log.append("turn/end", {"turn": turn_no})
    await push(AgentEvent(type="agent_end", messages=list(new_messages)))
    return LoopResult(messages=new_messages, events=events)


async def _stream_assistant(
    context: AgentContext, config: LoopConfig, stream_fn: StreamFn
) -> AgentMessage:
    messages = context.messages
    if config.transform_context:
        transformed = await config.transform_context(messages)
        if transformed is not messages:
            context.messages[:] = transformed
        messages = context.messages
    target = context
    if config.convert_to_llm:
        converted = config.convert_to_llm(messages)
        target = AgentContext(
            system_prompt=context.system_prompt,
            messages=converted,
            tools=context.tools,
            workspace_dir=context.workspace_dir,
            model=context.model,
            project_id=context.project_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
        )

    async def produce() -> AgentMessage:
        return await stream_fn(target)

    return await retry_assistant_call(
        produce, attempts=config.retry_attempts, base_delay=config.retry_base_delay
    )


async def _execute_tools(
    context: AgentContext,
    tool_calls: list[ToolCallBlock],
    config: LoopConfig,
    push: EmitFn,
) -> list[AgentMessage]:
    results: list[AgentMessage] = []
    tools = {getattr(item, "name", ""): item for item in context.tools}
    for call in tool_calls:
        await push(
            AgentEvent(
                type="tool_execution_start",
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
            )
        )
        blocked = _blocked_plan_result(call, config)
        if blocked is None and config.gate_tool is not None:
            blocked = config.gate_tool(call)
        if blocked is not None:
            await push(
                AgentEvent(
                    type="tool_execution_end",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    message=blocked,
                )
            )
            results.append(blocked)
            continue
        decision = await decide_approval(
            config.approval_mode, call.name, call.id, call.arguments, config.approve
        )
        if decision != "allow":
            await push(
                AgentEvent(
                    type="approval_required",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    reason="denied",
                )
            )
            result = _error_result(call, get_prompt("denied_tool", tool_name=call.name))
            await push(
                AgentEvent(
                    type="tool_execution_end",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    message=result,
                )
            )
            results.append(result)
            continue
        spec = tools.get(call.name)
        try:
            if spec is None:
                raise KeyError(get_prompt("unknown_tool", tool_name=call.name))
            result = await _invoke_tool(spec, call, config)
        except Exception as exc:
            logger.warning("工具失败 name=%s err=%s", call.name, exc)
            result = _error_result(call, str(exc))
        await push(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=call.id,
                tool_name=call.name,
                message=result,
            )
        )
        results.append(result)
    return results


async def _execute_tools_parallel(
    context: AgentContext,
    tool_calls: list[ToolCallBlock],
    config: LoopConfig,
    push: EmitFn,
) -> list[AgentMessage]:
    tools = {getattr(item, "name", ""): item for item in context.tools}
    decisions: list[tuple[ToolCallBlock, str, AgentMessage | None]] = []
    for call in tool_calls:
        await push(
            AgentEvent(
                type="tool_execution_start",
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
            )
        )
        blocked = _blocked_plan_result(call, config)
        if blocked is None and config.gate_tool is not None:
            blocked = config.gate_tool(call)
        if blocked is not None:
            decisions.append((call, "plan-block", blocked))
            continue
        decision = await decide_approval(
            config.approval_mode, call.name, call.id, call.arguments, config.approve
        )
        decisions.append((call, decision, None))

    async def run_one(call: ToolCallBlock, decision: str, blocked: AgentMessage | None) -> AgentMessage:
        if blocked is not None:
            return blocked
        if decision != "allow":
            await push(
                AgentEvent(
                    type="approval_required",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    reason="denied",
                )
            )
            return _error_result(call, get_prompt("denied_tool", tool_name=call.name))
        spec = tools.get(call.name)
        try:
            if spec is None:
                raise KeyError(get_prompt("unknown_tool", tool_name=call.name))
            return await _invoke_tool(spec, call, config)
        except Exception as exc:
            return _error_result(call, str(exc))

    results = list(
        await asyncio.gather(
            *[run_one(call, decision, blocked) for call, decision, blocked in decisions]
        )
    )
    for call, result in zip((item[0] for item in decisions), results, strict=True):
        await push(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=call.id,
                tool_name=call.name,
                message=result,
            )
        )
    return results


async def _fail_truncated(tool_calls: list[ToolCallBlock], push: EmitFn) -> list[AgentMessage]:
    results: list[AgentMessage] = []
    for call in tool_calls:
        await push(
            AgentEvent(
                type="tool_execution_start",
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
            )
        )
        result = _error_result(call, get_prompt("truncated_tool", tool_name=call.name))
        await push(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=call.id,
                tool_name=call.name,
                message=result,
            )
        )
        results.append(result)
    return results


async def _invoke_tool(spec: object, call: ToolCallBlock, config: LoopConfig) -> AgentMessage:
    timeout_ms = getattr(spec, "timeout_ms", None)
    if timeout_ms is None:
        timeout_ms = config.tool_timeout_ms
    timeout_ms = int(timeout_ms or 0)

    async def run() -> AgentMessage:
        try:
            args = validate_tool_arguments(spec, call.arguments)
        except ToolArgumentError as exc:
            return _error_result(call, str(exc))
        func = spec.func  # type: ignore[attr-defined]
        try:
            if timeout_ms > 0 and not inspect.iscoroutinefunction(func):
                output = await asyncio.to_thread(func, **args)
            else:
                output = func(**args)
                if hasattr(output, "__await__"):
                    output = await output
        except TimeoutError as exc:
            text = str(exc).strip() or timeout_result_text(timeout_ms)
            return _error_result(call, text)
        text = output if isinstance(output, str) else str(output)
        return AgentMessage(
            role="toolResult",
            content=text,
            tool_call_id=call.id,
            tool_name=call.name,
        )

    if timeout_ms <= 0:
        return await run()
    try:
        return await asyncio.wait_for(run(), timeout=timeout_ms / 1000)
    except TimeoutError:
        return _error_result(call, timeout_result_text(timeout_ms))


async def _emit_and_log_pre_step(
    message: AgentMessage,
    log: SessionLog | None,
    push: EmitFn,
) -> None:
    """Record harness-injected assistant/toolResult/user so they stay replayable."""
    if message.role == "assistant":
        await push(AgentEvent(type="message_start", message=message))
        await push(AgentEvent(type="message_end", message=message))
        if log is not None:
            _log_assistant(log, message)
        for call in message.tool_calls():
            if log is not None:
                log.append(
                    "tool/call",
                    {"id": call.id, "name": call.name, "arguments": call.arguments},
                )
            await push(
                AgentEvent(
                    type="tool_execution_start",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                )
            )
        return
    if message.role == "toolResult":
        if log is not None:
            log.append(
                "tool/result",
                {
                    "tool_call_id": message.tool_call_id,
                    "tool_name": message.tool_name,
                    "text": message.text(),
                    "is_error": message.is_error,
                },
            )
        await push(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=message.tool_call_id,
                tool_name=message.tool_name,
                message=message,
            )
        )
        return
    await push(AgentEvent(type="message_start", message=message))
    await push(AgentEvent(type="message_end", message=message))
    if log is not None:
        _log_user(log, message)


def _log_user(log: SessionLog, message: AgentMessage) -> None:
    if message.role != "user":
        return
    payload: dict = {"text": message.text(), "source": message.source or "user"}
    if message.meta:
        payload["meta"] = dict(message.meta)
    log.append("user/message", payload)


def _log_assistant(log: SessionLog, message: AgentMessage) -> None:
    calls = [
        {"id": block.id, "name": block.name, "arguments": block.arguments}
        for block in message.tool_calls()
    ]
    log.append(
        "assistant/message",
        {
            "text": message.text(),
            "tool_calls": calls,
            "stop_reason": message.stop_reason,
            "reasoning": message.reasoning or "",
            "source": message.source,
        },
    )


def _blocked_plan_result(call: ToolCallBlock, config: LoopConfig) -> AgentMessage | None:
    if not config.blocks_tool or not config.blocks_tool(call.name):
        return None
    if config.block_reason is not None:
        text = config.block_reason(call.name)
    else:
        text = get_prompt("plan_block_mutating", tool_name=call.name)
    logger.info("拒绝工具 name=%s", call.name)
    return _error_result(call, text)


def _error_result(call: ToolCallBlock, text: str) -> AgentMessage:
    return AgentMessage(
        role="toolResult",
        content=text,
        tool_call_id=call.id,
        tool_name=call.name,
        is_error=True,
    )


async def _safe_list(factory: Callable[[], Awaitable[list[AgentMessage]]] | None) -> list[AgentMessage]:
    if factory is None:
        return []
    return await factory()


def new_tool_call(name: str, arguments: dict, call_id: str | None = None) -> ToolCallBlock:
    return ToolCallBlock(id=call_id or uuid.uuid4().hex[:12], name=name, arguments=arguments)
