"""LLM 边界。生产用 OpenAI 兼容；测试用 ScriptedLLM。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable

from witty_agent.logging import get_logger, redact
from witty_agent.retry import RetryableLLMError, retry_call
from witty_agent.types import AgentContext, AgentMessage, TextBlock, ToolCallBlock, Usage

logger = get_logger("llm")
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 524}
THINK_LEVELS = frozenset({"off", "short", "long"})
_THINK_TAG = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


class ScriptedLLM:
    """按预设回复走完循环，不访问网络。"""

    def __init__(self, replies: Iterable[AgentMessage]):
        self._replies = list(replies)
        self.on_text_delta: object | None = None
        self.on_reasoning_delta: object | None = None
        self.on_tool_delta: object | None = None
        self.think_level: str = "short"

    async def __call__(self, context: AgentContext) -> AgentMessage:
        if not self._replies:
            return AgentMessage(role="assistant", content="", stop_reason="error")
        message = self._replies.pop(0)
        if message.reasoning and callable(self.on_reasoning_delta):
            self.on_reasoning_delta(message.reasoning)
        text = message.text()
        if text and callable(self.on_text_delta):
            self.on_text_delta(text)
        calls = message.tool_calls()
        if calls and callable(self.on_tool_delta) and calls[0].name:
            self.on_tool_delta(calls[0].name)
        return message


class OpenAICompatLLM:
    """OpenAI 协议子集：chat/completions + tools，默认 SSE 流式。"""

    def __init__(
        self,
        model_id: str = "",
        *,
        api_key: str = "",
        base_url: str = "",
        timeout: int | None = None,
        max_tokens: int | None = None,
        stream: bool = True,
        retry_attempts: int = 3,
    ) -> None:
        from witty_agent.runtime import model_settings

        settings = model_settings()
        self.model_id = model_id or str(settings["model_id"])
        self.api_key = api_key or str(settings["api_key"])
        self.base_url = (base_url or str(settings["base_url"])).rstrip("/")
        self.timeout = timeout if timeout is not None else int(settings["timeout_sec"])
        self.max_tokens = max_tokens if max_tokens is not None else int(settings["max_tokens"])
        self.stream = stream
        self.retry_attempts = retry_attempts
        self.on_text_delta: object | None = None
        self.on_reasoning_delta: object | None = None
        self.on_stream_reset: object | None = None
        self.on_tool_delta: object | None = None
        self.think_level: str = "short"

    async def __call__(self, context: AgentContext) -> AgentMessage:
        attempt = {"n": 0}

        async def once() -> AgentMessage:
            attempt["n"] += 1
            if attempt["n"] > 1 and callable(self.on_stream_reset):
                self.on_stream_reset()
            return await asyncio.to_thread(self._request, context)

        try:
            return await retry_call(once, attempts=self.retry_attempts)
        except Exception as exc:
            logger.warning("模型调用失败 err=%s", exc)
            return AgentMessage(role="assistant", content=str(exc), stop_reason="error")

    def _request(self, context: AgentContext) -> AgentMessage:
        url = f"{self.base_url}/chat/completions"
        body = _openai_chat_body(
            context,
            model_id=self.model_id,
            max_tokens=self.max_tokens,
            stream=self.stream,
            think_level=self.think_level,
        )
        session_header = os.environ.get("WITTY_SESSION_HEADER") or (
            f"witty-agent:{context.agent_id}:{context.session_id}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "X-Session-ID": session_header,
                "X-Gateway-Queue-Timeout-Seconds": "600",
                "Accept": "text/event-stream" if self.stream else "application/json",
            },
            method="POST",
        )
        logger.info("请求模型 model=%s stream=%s url=%s", self.model_id, self.stream, redact(url))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = _read_response(
                    response,
                    stream=self.stream,
                    on_text_delta=self.on_text_delta if callable(self.on_text_delta) else None,
                    on_reasoning_delta=(
                        self.on_reasoning_delta if callable(self.on_reasoning_delta) else None
                    ),
                    on_tool_delta=self.on_tool_delta if callable(self.on_tool_delta) else None,
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.warning("模型 HTTP 失败 status=%s", exc.code)
            # Retry-After 带进错误文本：调用方退避时才能听资源池的安排（错误跨
            # AgentMessage 边界后只剩字符串，头信息不带出来就丢了）
            text = f"{exc.code} {detail}{_retry_after_note(exc)}"
            if exc.code in _RETRYABLE_STATUS:
                raise RetryableLLMError(text) from exc
            return AgentMessage(role="assistant", content=text, stop_reason="error")
        except OSError as exc:
            logger.warning("模型网络失败 err=%s", exc)
            raise RetryableLLMError(str(exc)) from exc
        return _from_openai_choice(payload)


def _retry_after_note(exc: urllib.error.HTTPError) -> str:
    """把 Retry-After 头渲染成可解析的后缀；秒数形式才取（HTTP 日期形式忽略）。"""
    headers = getattr(exc, "headers", None)
    if headers is None:
        return ""
    value = str(headers.get("Retry-After") or "").strip()
    return f" retry-after: {value}s" if value.replace(".", "", 1).isdigit() else ""


def text_reply(text: str, *, reasoning: str = "") -> AgentMessage:
    return AgentMessage(
        role="assistant",
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        reasoning=reasoning,
    )


def tool_reply(name: str, arguments: dict, call_id: str = "call1") -> AgentMessage:
    return AgentMessage(
        role="assistant",
        content=[
            TextBlock(text=""),
            ToolCallBlock(id=call_id, name=name, arguments=arguments),
        ],
        stop_reason="toolUse",
    )


def _openai_chat_body(
    context: AgentContext,
    *,
    model_id: str,
    max_tokens: int,
    stream: bool,
    think_level: str,
) -> dict:
    """Build a chat/completions body. Never send tools=[]."""
    body = {
        "model": model_id,
        "messages": _to_openai_messages(context),
        "max_tokens": max_tokens,
        "user": context.session_id,
        "stream": stream,
    }
    tools = _to_openai_tools(context.tools)
    if tools:
        body["tools"] = tools
    body.update(_thinking_body(think_level))
    return body


def _thinking_body(level: str) -> dict:
    """off 关掉；短档不另塞思考字段，避免把工具轮次吃成只思考；长档才强开。"""
    if level == "off":
        return {"thinking": {"type": "disabled"}}
    if level == "long":
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    return {}


def split_think_tags(text: str) -> tuple[str, str]:
    """把 `<think>` 正文拆出来，剩余作为可见回复。"""
    if not text:
        return "", ""
    parts = [item.strip() for item in _THINK_TAG.findall(text) if item.strip()]
    cleaned = _THINK_TAG.sub("", text).strip()
    return "\n".join(parts), cleaned


class _ThinkRouter:
    """把流式 content 里的 <think> 段导到推理通道。"""

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False

    def feed(self, piece: str) -> tuple[str, str]:
        if not piece:
            return "", ""
        self.buf += piece
        reasoning: list[str] = []
        content: list[str] = []
        open_tag, close_tag = "<think>", "</think>"
        while self.buf:
            lower = self.buf.lower()
            if self.in_think:
                end = lower.find(close_tag)
                if end < 0:
                    keep = len(close_tag) - 1
                    if len(self.buf) > keep:
                        reasoning.append(self.buf[:-keep])
                        self.buf = self.buf[-keep:]
                    break
                reasoning.append(self.buf[:end])
                self.buf = self.buf[end + len(close_tag) :]
                self.in_think = False
                continue
            start = lower.find(open_tag)
            if start < 0:
                keep = len(open_tag) - 1
                if len(self.buf) > keep:
                    content.append(self.buf[:-keep])
                    self.buf = self.buf[-keep:]
                break
            content.append(self.buf[:start])
            self.buf = self.buf[start + len(open_tag) :]
            self.in_think = True
        return "".join(reasoning), "".join(content)

    def flush(self) -> tuple[str, str]:
        leftover = self.buf
        self.buf = ""
        if self.in_think:
            return leftover, ""
        return "", leftover


def _read_response(
    response: object,
    *,
    stream: bool,
    on_text_delta: object | None = None,
    on_reasoning_delta: object | None = None,
    on_tool_delta: object | None = None,
) -> dict:
    if not stream:
        return json.loads(response.read().decode("utf-8"))
    first = response.readline()
    if not first:
        raise RetryableLLMError("empty stream")
    stripped = first.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        raw = first + response.read()
        return json.loads(raw.decode("utf-8"))
    chunks: list[dict] = []
    line = first
    router = _ThinkRouter()
    announced_tool = ""
    while line:
        parsed = parse_sse_line(line.decode("utf-8", errors="replace"))
        if parsed:
            chunks.append(parsed)
            thought = _delta_reasoning(parsed)
            if thought and callable(on_reasoning_delta):
                on_reasoning_delta(thought)
            raw = _delta_text(parsed)
            if raw:
                tagged, visible = router.feed(raw)
                if tagged and callable(on_reasoning_delta):
                    on_reasoning_delta(tagged)
                if visible and callable(on_text_delta):
                    on_text_delta(visible)
            if callable(on_tool_delta):
                name = _delta_tool_name(parsed)
                if name and name != announced_tool:
                    announced_tool = name
                    on_tool_delta(name)
                elif not announced_tool and _delta_has_tool_call(parsed):
                    announced_tool = "*"
                    on_tool_delta("")
        line = response.readline()
    tagged, visible = router.flush()
    if tagged and callable(on_reasoning_delta):
        on_reasoning_delta(tagged)
    if visible and callable(on_text_delta):
        on_text_delta(visible)
    if not chunks:
        raise RetryableLLMError("stream ended without chunks")
    return accumulate_stream(chunks)


def _delta_text(chunk: dict) -> str:
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    return str(content) if content else ""


def _delta_reasoning(chunk: dict) -> str:
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    for key in _REASONING_KEYS:
        value = delta.get(key)
        if value:
            return str(value)
    return ""


def _delta_tool_name(chunk: dict) -> str:
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    for call in delta.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        if name:
            return name
    return ""


def _delta_has_tool_call(chunk: dict) -> bool:
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    calls = delta.get("tool_calls")
    return isinstance(calls, list) and any(isinstance(item, dict) for item in calls)


def parse_sse_line(line: str) -> dict | None:
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def accumulate_stream(chunks: list[dict]) -> dict:
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: dict[int, dict] = {}
    usage: dict = {}
    finish: str | None = None
    router = _ThinkRouter()
    for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        choice = (chunk.get("choices") or [{}])[0]
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta") or choice.get("message") or {}
        thought = _delta_reasoning(chunk)
        if not thought:
            for key in _REASONING_KEYS:
                if delta.get(key):
                    thought = str(delta[key])
                    break
        if thought:
            reasoning.append(thought)
        if delta.get("content"):
            tagged, visible = router.feed(str(delta["content"]))
            if tagged:
                reasoning.append(tagged)
            if visible:
                content.append(visible)
        for call in delta.get("tool_calls") or []:
            index = int(call.get("index") or 0)
            slot = tool_calls.setdefault(
                index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if call.get("id"):
                slot["id"] = str(call["id"])
            function = call.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                slot["function"]["arguments"] += str(function["arguments"])
    tagged, visible = router.flush()
    if tagged:
        reasoning.append(tagged)
    if visible:
        content.append(visible)
    calls = [tool_calls[index] for index in sorted(tool_calls)]
    return {
        "choices": [
            {
                "message": {
                    "content": "".join(content) or None,
                    "reasoning_content": "".join(reasoning) or None,
                    "tool_calls": calls or None,
                },
                "finish_reason": finish,
            }
        ],
        "usage": usage,
    }


def _to_openai_messages(context: AgentContext) -> list[dict]:
    rows: list[dict] = [{"role": "system", "content": context.system_prompt}]
    for message in context.messages:
        if message.role == "user":
            text = message.text()
            images = []
            if message.source != "plugin:file-reference":
                from witty_agent.file_reference import collect_image_parts

                images = collect_image_parts(text, workspace=context.workspace_dir)
            if images:
                rows.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": text}, *images],
                    }
                )
            else:
                rows.append({"role": "user", "content": text})
        elif message.role == "assistant":
            text = message.text()
            calls = message.tool_calls()
            if not text and not calls:
                continue
            row: dict = {"role": "assistant", "content": text or None}
            if message.reasoning:
                row["reasoning_content"] = message.reasoning
            if calls:
                row["tool_calls"] = [
                    {
                        "id": item.id,
                        "type": "function",
                        "function": {
                            "name": item.name,
                            "arguments": json.dumps(item.arguments, ensure_ascii=False),
                        },
                    }
                    for item in calls
                ]
            rows.append(row)
        elif message.role == "toolResult":
            rows.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.text(),
                }
            )
    return rows


def _to_openai_tools(tools: list) -> list[dict]:
    payload = []
    for item in tools:
        spec = getattr(item, "_witty_tool", None) or item
        name = getattr(spec, "name", None)
        if not name:
            continue
        payload.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": getattr(spec, "description", ""),
                    "parameters": getattr(spec, "parameters", {"type": "object", "properties": {}}),
                },
            }
        )
    return payload


def _from_openai_choice(payload: dict) -> AgentMessage:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage_raw = payload.get("usage") or {}
    usage = Usage(
        input=int(usage_raw.get("prompt_tokens") or 0),
        output=int(usage_raw.get("completion_tokens") or 0),
    )
    tool_calls = message.get("tool_calls") or []
    blocks: list = []
    raw_text = str(message.get("content") or "")
    tagged, visible = split_think_tags(raw_text)
    reasoning = ""
    for key in _REASONING_KEYS:
        if message.get(key):
            reasoning = str(message[key])
            break
    if tagged:
        reasoning = f"{reasoning}\n{tagged}".strip()
    if visible:
        blocks.append(TextBlock(text=visible))
    elif raw_text and not tagged:
        blocks.append(TextBlock(text=raw_text))
    for call in tool_calls:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        blocks.append(
            ToolCallBlock(
                id=str(call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    stop: str = "toolUse" if tool_calls else "end_turn"
    finish = choice.get("finish_reason")
    if finish == "length":
        stop = "length"
    if reasoning:
        logger.info("模型推理 tokens_or_chars=%s", len(reasoning))
    return AgentMessage(
        role="assistant",
        content=blocks or "",
        stop_reason=stop,
        usage=usage,
        reasoning=reasoning,
    )
