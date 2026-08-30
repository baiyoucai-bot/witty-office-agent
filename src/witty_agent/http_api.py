"""后端 HTTP 协议面。壳只调用这些路由，不定义协议。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from witty_agent.approval import APPROVAL_MODES
from witty_agent.catalog import load_catalog, set_skill_enabled, set_tool_enabled
from witty_agent.commands import CommandRegistry
from witty_agent.kernel_surface import is_kernel_tool
from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, data_root, scratchpad_dir, traces_dir
from witty_agent.llm import THINK_LEVELS
from witty_agent.logging import get_logger
from witty_agent.memory import (
    _topic_description,
    attach_workspace_public,
    public_memory,
    read_topic,
    rebuild_memory_index,
    resolve_session_memory,
    write_topic,
)
from witty_agent.model_catalog import (
    ModelProfile,
    activate_model,
    apply_active_model,
    delete_model,
    ensure_model_catalog,
    normalize_model_name,
    public_models,
    upsert_model,
)
from witty_agent.prompts import get_prompt_record, public_prompt_index, save_prompt
from witty_agent.runtime import model_settings, schedule_settings
from witty_agent.session import Session, create_agent, create_session, list_project_agents
from witty_agent.todo import current_todos
from witty_agent.skills import (
    install_user_skill,
    list_skill_groups,
    list_skills,
    load_skill,
    network_label,
    uninstall_user_skill,
    user_skills_dir,
)
from witty_agent.store import (
    delete_session_file,
    load_messages,
    list_trace_summaries,
    read_session_meta,
    session_path,
)
from witty_agent.tools import list_tools
from witty_agent.types import AgentEvent, AgentMessage
from witty_agent.user_questions import AskUserAnswer, AskUserAnswerItem
from witty_agent.vault import delete_vault_entry, load_vault, mask_vault, set_vault_entry

logger = get_logger("http")


class ApiState:
    def __init__(self, *, root: Path | None = None, stream_factory=None) -> None:
        self.root = root
        self.stream_factory = stream_factory
        self.sessions: dict[str, Session] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.run_lock = threading.Lock()


STATE = ApiState()
_TICKER_STOP = threading.Event()
_TICKER_THREAD: threading.Thread | None = None
_TICK_LOCK = threading.Lock()
_BUSY_RUN = frozenset({"running", "awaiting_approval", "awaiting_question"})


def _any_run_busy() -> bool:
    return any(item.get("status") in _BUSY_RUN for item in STATE.runs.values())


def configure_api(*, root: Path | None = None, stream_factory=None) -> ApiState:
    from witty_agent.plugins.live import load_live, set_busy_probe

    STATE.root = root
    STATE.stream_factory = stream_factory
    STATE.runs.clear()
    set_busy_probe(_any_run_busy)
    load_live(root)
    from witty_agent.plugins.watch import start_watcher

    start_watcher()
    _hydrate_model_secrets(DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID)
    ensure_model_catalog(DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID, root=root)
    apply_active_model(DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID, root=root)
    return STATE


def _hydrate_model_secrets(project_id: str, agent_id: str) -> None:
    vault = load_vault(project_id, agent_id, root=STATE.root)
    if vault.get("WITTY_API_KEY") and not os.environ.get("WITTY_API_KEY"):
        os.environ["WITTY_API_KEY"] = vault["WITTY_API_KEY"]
    if vault.get("WITTY_BASE_URL") and not os.environ.get("WITTY_BASE_URL"):
        os.environ["WITTY_BASE_URL"] = vault["WITTY_BASE_URL"]
    if vault.get("WITTY_MODEL_ID") and not os.environ.get("WITTY_MODEL_ID"):
        os.environ["WITTY_MODEL_ID"] = vault["WITTY_MODEL_ID"]
    if vault.get("WITTY_MAX_TOKENS") and not os.environ.get("WITTY_MAX_TOKENS"):
        os.environ["WITTY_MAX_TOKENS"] = vault["WITTY_MAX_TOKENS"]
    if vault.get("WITTY_TIMEOUT_SEC") and not os.environ.get("WITTY_TIMEOUT_SEC"):
        os.environ["WITTY_TIMEOUT_SEC"] = vault["WITTY_TIMEOUT_SEC"]


def package_version() -> str:
    """装出来的版本号；源码树里跑（没装 dist-info）就退回 pyproject。"""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("witty-office-agent")
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        return "0.0.0"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        head, _, tail = line.partition("=")
        if head.strip() == "version":
            return tail.strip().strip('"').strip("'") or "0.0.0"
    return "0.0.0"


def _model_public(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
) -> dict[str, Any]:
    settings = model_settings()
    key = str(settings.get("api_key") or "")
    source = "none"
    if os.environ.get("WITTY_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        source = "env"
    elif key:
        source = "vault"
    catalog = public_models(project_id, agent_id, root=STATE.root)
    active = next((item for item in catalog["models"] if item["active"]), None)
    return {
        "has_key": bool(key),
        "name": (active or {}).get("name") or "",
        "base_url": str(settings.get("base_url") or ""),
        "model_id": str(settings.get("model_id") or ""),
        "max_tokens": int(settings.get("max_tokens") or 2048),
        "timeout_sec": int(settings.get("timeout_sec") or 3600),
        "source": source,
        "approval_modes": sorted(APPROVAL_MODES),
        "models": catalog["models"],
        "active": catalog["active"],
    }


def _plan_public(session: Any) -> dict[str, bool]:
    if session is None:
        return {"active": False, "pending": False}
    projected = session.project()
    plan = projected.get("plan") if isinstance(projected, dict) else None
    if not isinstance(plan, dict):
        return {"active": False, "pending": False}
    return {"active": bool(plan.get("active")), "pending": bool(plan.get("pending"))}


def _as_wait(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", ""}
    return default


def _run_public(run: dict[str, Any]) -> dict[str, Any]:
    session = STATE.sessions.get(str(run.get("session_id") or ""))
    return {
        "session_id": run["session_id"],
        "status": run["status"],
        "text": run.get("text") or "",
        "reasoning": run.get("reasoning") or "",
        "evidence": list(run.get("evidence") or []),
        "trace_reason": run.get("trace_reason") or "",
        "sealed": run.get("sealed") or "",
        "events": list(run.get("events") or []),
        "timeline": list(run.get("timeline") or []),
        "error": run.get("error") or "",
        "pending": run.get("pending"),
        "question": run.get("question"),
        "todos": list(current_todos(session.log) or []) if session is not None else [],
        "plan": _plan_public(session),
    }


def iter_run_stream_events(
    session_id: str,
    *,
    idle_limit: int = 6000,
    wait: float = 0.05,
) -> Iterator[dict[str, Any]]:
    """Yield public timeline items for GET /v1/sessions/{id}/stream."""
    import time

    cursor = 0
    idle = 0
    while idle < idle_limit:
        run = STATE.runs.get(session_id)
        if run is None:
            yield {"type": "error", "error": "no run"}
            return
        with STATE.run_lock:
            timeline = list(run.get("timeline") or [])
            status = run.get("status")
            snapshot = _run_public(run)
        while cursor < len(timeline):
            yield dict(timeline[cursor])
            cursor += 1
            idle = 0
        if status in {"done", "error"} and cursor >= len(timeline):
            if not any(item.get("type") == "done" for item in timeline):
                yield {
                    "type": "done",
                    "text": snapshot.get("text") or "",
                    "status": status,
                }
            return
        time.sleep(wait)
        idle += 1


def _visible_assistant_close(messages: list[AgentMessage]) -> dict[str, Any]:
    """Last model answer, plus a harness seal if one was stamped after it."""
    sealed = ""
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        source = str(message.source or "")
        if source == "plugin:evidence-seal":
            if not sealed:
                sealed = message.text()
            continue
        if source.startswith("plugin:"):
            continue
        return {
            "text": message.text(),
            "evidence": list(message.evidence or []),
            "trace_reason": message.trace_reason or "",
            "sealed": sealed,
        }
    return {"text": "", "evidence": [], "trace_reason": "", "sealed": sealed}


def _scope(payload: dict[str, Any], query: dict[str, str]) -> tuple[str, str]:
    project_id = str(payload.get("project_id") or query.get("project_id") or DEFAULT_PROJECT_ID)
    agent_id = str(payload.get("agent_id") or query.get("agent_id") or DEFAULT_AGENT_ID)
    return project_id, agent_id


def _message_public(item: AgentMessage) -> dict[str, Any]:
    payload = {
        "role": item.role,
        "text": item.text(),
        "reasoning": item.reasoning or "",
        "evidence": list(item.evidence or []),
        "trace_reason": item.trace_reason or "",
        "tool_name": item.tool_name,
        "tool_call_id": item.tool_call_id,
        "is_error": bool(item.is_error),
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in item.tool_calls()
        ],
    }
    if item.source:
        payload["source"] = item.source
    return payload


def _event_public(event: AgentEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": event.type,
        "tool_call_id": event.tool_call_id,
        "tool_name": event.tool_name,
        "args": event.args,
        "reason": event.reason,
    }
    if event.message is not None:
        payload["role"] = event.message.role
        payload["text"] = event.message.text()
        payload["is_error"] = bool(event.message.is_error)
        if event.message.source:
            payload["source"] = event.message.source
        if event.message.reasoning:
            payload["reasoning"] = event.message.reasoning
        if event.message.evidence:
            payload["evidence"] = list(event.message.evidence)
        if event.message.trace_reason:
            payload["trace_reason"] = event.message.trace_reason
        payload["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in event.message.tool_calls()
        ]
    return payload


def _publish_run(run: dict[str, Any], item: dict[str, Any]) -> None:
    with STATE.run_lock:
        timeline = run.setdefault("timeline", [])
        entry = dict(item)
        timeline.append(entry)
        entry["seq"] = len(timeline) - 1
        kind = entry.get("type")
        if kind == "stream_reset":
            run["text"] = ""
            run["reasoning"] = ""
        elif kind == "text_delta":
            run["text"] = str(run.get("text") or "") + str(entry.get("text") or "")
        elif kind == "reasoning_delta":
            run["reasoning"] = str(run.get("reasoning") or "") + str(entry.get("text") or "")
        elif kind == "message_end" and entry.get("role") == "assistant":
            if str(entry.get("source") or "") == "plugin:evidence-seal":
                if entry.get("text"):
                    run["sealed"] = str(entry.get("text") or "")
            else:
                if entry.get("text"):
                    run["text"] = str(entry.get("text") or "")
                if entry.get("reasoning"):
                    run["reasoning"] = str(entry.get("reasoning") or "")
                if entry.get("evidence") is not None:
                    run["evidence"] = list(entry.get("evidence") or [])
                if entry.get("trace_reason"):
                    run["trace_reason"] = str(entry.get("trace_reason") or "")
        elif kind == "done":
            if entry.get("evidence") is not None:
                run["evidence"] = list(entry.get("evidence") or [])
            if entry.get("trace_reason"):
                run["trace_reason"] = str(entry.get("trace_reason") or "")
            if entry.get("sealed"):
                run["sealed"] = str(entry.get("sealed") or "")
        if kind:
            events = run.setdefault("events", [])
            events.append(kind)


def _resolve_session(
    session_id: str,
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    workspace_dir: str | None = None,
) -> Session | None:
    live = STATE.sessions.get(session_id)
    if live is not None:
        return live
    agent = create_agent(project_id, agent_id, root=STATE.root)
    path = session_path(traces_dir(project_id, agent_id, root=STATE.root), session_id)
    if not path.is_file() and not workspace_dir:
        return None
    meta = read_session_meta(path) if path.is_file() else {}
    session = create_session(
        agent,
        workspace_dir=workspace_dir or meta.get("cwd") or None,
        session_id=session_id,
        parent_id=meta.get("parent"),
    )
    if meta.get("title"):
        session.title = str(meta["title"])
    STATE.sessions[session.session_id] = session
    return session


def _bind_stream(stream_fn: object, run: dict[str, Any], think_level: str) -> object:
    if hasattr(stream_fn, "think_level"):
        stream_fn.think_level = think_level
    if hasattr(stream_fn, "on_text_delta"):
        stream_fn.on_text_delta = lambda text: _publish_run(
            run, {"type": "text_delta", "text": text}
        )
    if hasattr(stream_fn, "on_reasoning_delta"):
        stream_fn.on_reasoning_delta = lambda text: _publish_run(
            run, {"type": "reasoning_delta", "text": text}
        )
    if hasattr(stream_fn, "on_stream_reset"):
        stream_fn.on_stream_reset = lambda: _publish_run(run, {"type": "stream_reset"})
    if hasattr(stream_fn, "on_tool_delta"):
        stream_fn.on_tool_delta = lambda name: _publish_run(
            run, {"type": "tool_preparing", "tool_name": name}
        )
    return stream_fn


def _start_session_run(
    session: Session, prompt: str, approval_mode: str, think_level: str = "short"
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "session_id": session.session_id,
        "status": "running",
        "text": "",
        "reasoning": "",
        "evidence": [],
        "trace_reason": "",
        "sealed": "",
        "events": [],
        "timeline": [],
        "error": "",
        "pending": None,
        "question": None,
        "gate": None,
        "qgate": None,
        "decision": None,
        "answers": None,
        "think_level": think_level,
    }
    STATE.runs[session.session_id] = run

    async def approve(name: str, call_id: str, args: dict) -> str:
        timeout = float(os.environ.get("WITTY_APPROVAL_TIMEOUT_SEC") or "300")
        gate = threading.Event()
        with STATE.run_lock:
            run["gate"] = gate
            run["decision"] = None
            run["pending"] = {
                "tool_name": name,
                "tool_call_id": call_id,
                "args": args,
            }
            run["status"] = "awaiting_approval"
        _publish_run(
            run,
            {
                "type": "approval_required",
                "tool_name": name,
                "tool_call_id": call_id,
                "args": args,
            },
        )
        await asyncio.to_thread(gate.wait, timeout)
        with STATE.run_lock:
            decision = run.get("decision") or "deny"
            run["pending"] = None
            run["gate"] = None
            run["status"] = "running"
        return decision if decision in {"allow", "deny"} else "deny"

    async def ask_user(questions: list) -> AskUserAnswer:
        timeout = float(
            os.environ.get("WITTY_QUESTION_TIMEOUT_SEC")
            or os.environ.get("WITTY_APPROVAL_TIMEOUT_SEC")
            or "300"
        )
        gate = threading.Event()
        payload = {
            "questions": [
                {
                    "id": item.id,
                    "question": item.question,
                    "header": item.header or "",
                    "options": [
                        {"label": opt.label, "description": opt.description or ""}
                        for opt in (item.options or [])
                    ],
                    "multi_select": bool(item.multi_select),
                }
                for item in questions
            ]
        }
        with STATE.run_lock:
            run["qgate"] = gate
            run["answers"] = None
            run["question"] = payload
            run["status"] = "awaiting_question"
        _publish_run(run, {"type": "question_required", "questions": payload["questions"]})
        await asyncio.to_thread(gate.wait, timeout)
        with STATE.run_lock:
            raw = run.get("answers") or []
            run["question"] = None
            run["qgate"] = None
            run["answers"] = None
            run["status"] = "running"
        rows: list[AskUserAnswerItem] = []
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                selected = row.get("selected") or []
                if not isinstance(selected, list):
                    selected = [selected]
                rows.append(
                    AskUserAnswerItem(
                        id=str(row.get("id") or ""),
                        selected=[str(item) for item in selected if str(item)],
                        custom=str(row.get("custom") or ""),
                    )
                )
        return AskUserAnswer(answers=rows)

    def worker() -> None:
        try:
            factory = STATE.stream_factory
            if factory is None:
                from witty_agent.llm import OpenAICompatLLM

                factory = OpenAICompatLLM
            stream_fn = _bind_stream(factory(), run, think_level)

            async def emit(event: AgentEvent) -> None:
                _publish_run(run, _event_public(event))

            result = asyncio.run(
                session.run(
                    prompt,
                    stream_fn=stream_fn,
                    approve=approve,
                    approval_mode=approval_mode,  # type: ignore[arg-type]
                    ask_user=ask_user,
                    emit=emit,
                )
            )
            close = _visible_assistant_close(result.messages)
            last = str(close.get("text") or "")
            evidence = list(close.get("evidence") or [])
            trace_reason = str(close.get("trace_reason") or "")
            sealed = str(close.get("sealed") or "")
            with STATE.run_lock:
                if last:
                    run["text"] = last
                run["evidence"] = evidence
                run["trace_reason"] = trace_reason
                if sealed:
                    run["sealed"] = sealed
                run["events"] = [item.type for item in result.events]
                run["status"] = "done"
            _publish_run(
                run,
                {
                    "type": "done",
                    "text": run.get("text") or last,
                    "evidence": evidence,
                    "trace_reason": trace_reason,
                    "sealed": sealed,
                },
            )
            from witty_agent.plugins.live import flush_pending

            flush_pending()
        except BaseException as exc:
            # 必须接到 BaseException：`CancelledError` 在 3.8+ 不是 `Exception` 的子类，
            # 只接 Exception 的话它会漏穿这里、把线程静默做掉，`status` 永远停在 running，
            # 于是这个会话之后每次发送都被 409 "run in progress" 挡掉，用户只能新开会话。
            logger.warning("异步会话失败 session=%s err=%s", session.session_id, exc)
            with STATE.run_lock:
                run["error"] = str(exc) or type(exc).__name__
                run["status"] = "error"
            _publish_run(run, {"type": "error", "error": run["error"]})
            from witty_agent.plugins.live import flush_pending

            flush_pending()
        finally:
            # 兜底：不管上面走了哪条路，run 都不许留在非终态，否则这个会话就废了
            with STATE.run_lock:
                if run.get("status") in {"running", "awaiting_approval"}:
                    run["error"] = run.get("error") or "run ended without a verdict"
                    run["status"] = "error"
                    stranded = True
                else:
                    stranded = False
            if stranded:
                logger.warning("会话运行未落终态，已强制标记 error session=%s", session.session_id)
                _publish_run(run, {"type": "error", "error": run["error"]})

    threading.Thread(target=worker, daemon=True, name="witty-session-run").start()
    return run


async def handle_request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    payload = body or {}
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"
    query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}

    if method == "GET" and route == "/v1/health":
        from witty_agent.host_context import host_environment

        status = _model_public()
        host = host_environment()
        from witty_agent.sandbox import public_sandbox

        box = public_sandbox()
        return 200, {
            "ok": True,
            "version": package_version(),
            "has_key": status["has_key"],
            "model_id": status["model_id"],
            "active": status.get("active") or "",
            "host": {"family": host["family"], "label": host["label"], "system": host["system"]},
            "sandbox": box,
        }

    if route == "/v1/web":
        from witty_agent.runtime import save_web_overlay, web_settings

        if method == "GET":
            settings = web_settings(root=STATE.root)
            return 200, {
                "mode": settings["mode"],
                "deny_public": settings["deny_public"],
                "allow_hosts": settings["allow_hosts"],
                "allow_private": settings["allow_private"],
            }
        if method == "PUT":
            mode = str(payload.get("mode") or "").strip().casefold()
            if "deny_public" in payload:
                deny = bool(payload.get("deny_public"))
            elif mode in {"intranet", "private", "内网"}:
                deny = True
            elif mode in {"public", "open", "外网"}:
                deny = False
            else:
                return 400, {"error": "mode must be public or intranet"}
            save_web_overlay(deny, root=STATE.root)
            settings = web_settings(root=STATE.root)
            return 200, {
                "mode": settings["mode"],
                "deny_public": settings["deny_public"],
                "allow_hosts": settings["allow_hosts"],
                "allow_private": settings["allow_private"],
            }

    if method == "POST" and route == "/v1/inbox":
        workspace = Path(str(payload.get("workspace_dir") or query.get("workspace_dir") or Path.cwd()))
        raw = str(payload.get("content_base64") or "")
        if not raw:
            return 400, {"error": "content_base64 required"}
        try:
            blob = base64.b64decode(raw, validate=False)
        except Exception:
            return 400, {"error": "invalid base64"}
        if not blob:
            return 400, {"error": "empty file"}
        if len(blob) > 12 * 1024 * 1024:
            return 400, {"error": "file too large"}
        mime = str(payload.get("mime") or "image/png").split(";")[0].strip().lower()
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        name = Path(str(payload.get("filename") or "paste")).name
        suffix = Path(name).suffix.lower()
        # 收件箱收任意文件：原名有像样的后缀就保留（.docx 不能被改成 .png），
        # 没有才按 mime 补，都补不出就 .bin。
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ext_map.get(mime, ".bin")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = workspace / ".witty-inbox" / f"{stamp}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        logger.info("收进粘贴文件 path=%s size=%s", dest, dest.stat().st_size)
        return 200, {
            "ok": True,
            "path": str(dest),
            "token": f".witty-inbox/{dest.name}",
            "name": dest.name,
            "mime": mime,
            "size": dest.stat().st_size,
        }

    if method == "GET" and route == "/v1/file-preview":
        # 聊天气泡里内联渲染模型产出的本地图片。围栏与 file: 引用/视觉通道同一套
        # （resolve_allowed：工作区 + 沙箱，venv 与越界拒绝），只认图片后缀。
        from witty_agent.file_reference import image_mime, is_image_path
        from witty_agent.sandbox import resolve_allowed

        workspace = str(query.get("workspace_dir") or payload.get("workspace_dir") or "").strip()
        raw = str(query.get("path") or payload.get("path") or "").strip()
        if raw.startswith("file:"):
            raw = raw[len("file:") :]
        if not workspace or not raw:
            return 400, {"error": "workspace_dir and path required"}
        try:
            target = resolve_allowed(workspace, raw, follow=True)
        except ValueError as exc:
            return 403, {"error": str(exc)}
        if not target.is_file():
            return 404, {"error": "not a file"}
        if not is_image_path(target):
            return 415, {"error": "not an image"}
        size = target.stat().st_size
        if size <= 0 or size > 12 * 1024 * 1024:
            return 413, {"error": "empty or too large"}
        return 200, {
            "ok": True,
            "path": str(target),
            "mime": image_mime(target),
            "size": size,
            "content_base64": base64.b64encode(target.read_bytes()).decode("ascii"),
        }

    if method == "GET" and route == "/v1/workspace":
        from witty_agent.file_reference import list_mention_paths, resolve_mention_root

        raw = str(query.get("dir") or payload.get("dir") or "").strip()
        session_id = str(query.get("session_id") or payload.get("session_id") or "").strip()
        allowed: list[str] = []
        if session_id:
            live = STATE.sessions.get(session_id)
            if live is None:
                return 200, {"dir": raw, "paths": [], "denied": True}
            allowed.append(str(live.workspace_dir))
        else:
            allowed.extend(str(item.workspace_dir) for item in STATE.sessions.values())
        root = resolve_mention_root(raw or (allowed[0] if len(allowed) == 1 else ""), allowed=allowed)
        if root is None:
            return 200, {"dir": raw, "paths": [], "denied": True}
        return 200, {"dir": str(root), "paths": list_mention_paths(str(root))}

    if route == "/v1/models" or route.startswith("/v1/models/"):
        project_id, agent_id = _scope(payload, query)
        ensure_model_catalog(project_id, agent_id, root=STATE.root)
        if method == "GET" and route == "/v1/models":
            return 200, public_models(project_id, agent_id, root=STATE.root)
        if method == "GET" and route.startswith("/v1/models/"):
            name = route.rsplit("/", 1)[-1]
            payload_models = public_models(project_id, agent_id, root=STATE.root)
            found = next((item for item in payload_models["models"] if item["name"] == name), None)
            if found is None:
                return 404, {"error": f"unknown model {name}"}
            return 200, found
        name = payload.get("name") or route.rsplit("/", 1)[-1]
        if route.endswith("/activate") and method == "POST":
            name = payload.get("name") or route.rstrip("/").split("/")[-2]
            try:
                activate_model(str(name), project_id, agent_id, root=STATE.root)
            except KeyError as exc:
                return 404, {"error": str(exc)}
            return 200, public_models(project_id, agent_id, root=STATE.root)
        if method == "DELETE":
            try:
                delete_model(str(name), project_id, agent_id, root=STATE.root)
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return 200, public_models(project_id, agent_id, root=STATE.root)
        if method == "PUT":
            try:
                upsert_model(
                    ModelProfile(
                        name=normalize_model_name(str(payload.get("name") or name)),
                        model_id=str(payload.get("model_id") or ""),
                        base_url=str(payload.get("base_url") or ""),
                        display_name=str(payload.get("display_name") or ""),
                        max_tokens=int(payload.get("max_tokens") or 2048),
                        timeout_sec=int(payload.get("timeout_sec") or 3600),
                    ),
                    api_key=str(payload["api_key"]).strip() if payload.get("api_key") else None,
                    activate=bool(payload.get("activate", True)),
                    project_id=project_id,
                    agent_id=agent_id,
                    root=STATE.root,
                )
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return 200, public_models(project_id, agent_id, root=STATE.root)

    if route == "/v1/model":
        project_id, agent_id = _scope(payload, query)
        _hydrate_model_secrets(project_id, agent_id)
        ensure_model_catalog(project_id, agent_id, root=STATE.root)
        apply_active_model(project_id, agent_id, root=STATE.root)
        if method == "GET":
            return 200, _model_public(project_id, agent_id)
        if method == "PUT":
            if payload.get("clear_key"):
                os.environ.pop("WITTY_API_KEY", None)
                delete_vault_entry("WITTY_API_KEY", project_id, agent_id, root=STATE.root)
            key = payload.get("api_key")
            if isinstance(key, str) and key.strip():
                os.environ["WITTY_API_KEY"] = key.strip()
                set_vault_entry("WITTY_API_KEY", key.strip(), project_id, agent_id, root=STATE.root)
            base_url = payload.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                os.environ["WITTY_BASE_URL"] = base_url.strip()
                set_vault_entry("WITTY_BASE_URL", base_url.strip(), project_id, agent_id, root=STATE.root)
            model_id = payload.get("model_id")
            if isinstance(model_id, str) and model_id.strip():
                os.environ["WITTY_MODEL_ID"] = model_id.strip()
                set_vault_entry("WITTY_MODEL_ID", model_id.strip(), project_id, agent_id, root=STATE.root)
            if payload.get("max_tokens") is not None and str(payload.get("max_tokens")).strip():
                tokens = str(int(payload["max_tokens"]))
                os.environ["WITTY_MAX_TOKENS"] = tokens
                set_vault_entry("WITTY_MAX_TOKENS", tokens, project_id, agent_id, root=STATE.root)
            if payload.get("timeout_sec") is not None and str(payload.get("timeout_sec")).strip():
                timeout = str(int(payload["timeout_sec"]))
                os.environ["WITTY_TIMEOUT_SEC"] = timeout
                set_vault_entry("WITTY_TIMEOUT_SEC", timeout, project_id, agent_id, root=STATE.root)
            catalog = ensure_model_catalog(project_id, agent_id, root=STATE.root)
            name = str(payload.get("name") or catalog.active or "default")
            try:
                upsert_model(
                    ModelProfile(
                        name=normalize_model_name(name),
                        model_id=str(os.environ.get("WITTY_MODEL_ID") or payload.get("model_id") or name),
                        base_url=str(os.environ.get("WITTY_BASE_URL") or payload.get("base_url") or ""),
                        display_name=str(payload.get("display_name") or ""),
                        max_tokens=int(os.environ.get("WITTY_MAX_TOKENS") or payload.get("max_tokens") or 2048),
                        timeout_sec=int(os.environ.get("WITTY_TIMEOUT_SEC") or payload.get("timeout_sec") or 3600),
                    ),
                    api_key=str(payload["api_key"]).strip() if payload.get("api_key") else None,
                    activate=True,
                    project_id=project_id,
                    agent_id=agent_id,
                    root=STATE.root,
                )
            except ValueError:
                pass
            return 200, _model_public(project_id, agent_id)

    if route == "/v1/prompts" or route.startswith("/v1/prompts/"):
        if method == "GET" and route == "/v1/prompts":
            return 200, public_prompt_index()
        name = unquote(route.rsplit("/", 1)[-1])
        if method == "GET":
            try:
                return 200, get_prompt_record(name)
            except KeyError as exc:
                return 404, {"error": str(exc)}
        if method == "PUT":
            try:
                return 200, save_prompt(name, str(payload.get("text") or ""))
            except KeyError as exc:
                return 404, {"error": str(exc)}
            except ValueError as exc:
                return 400, {"error": str(exc)}
        return 405, {"error": "method not allowed"}

    if route == "/v1/skills" or route.startswith("/v1/skills/"):
        project_id, agent_id = _scope(payload, query)
        catalog = load_catalog(project_id, agent_id, root=STATE.root)
        if method == "GET" and route == "/v1/skills":
            groups = list_skill_groups(project_id, agent_id, root=STATE.root)

            def _row(item) -> dict[str, Any]:
                return {
                    "name": item.name,
                    "description": item.description,
                    "path": str(item.path),
                    "enabled": catalog.skill_enabled(item.name),
                    "allowed_tools": list(item.allowed_tools),
                    "license": item.license,
                    "origin": item.origin,
                    "network": item.network,
                    "network_label": network_label(item.network),
                }

            system = [_row(item) for item in groups["system"]]
            user = [_row(item) for item in groups["user"]]
            return 200, {
                "skills": system + user,
                "system": system,
                "user": user,
                "user_dir": groups["user_dir"],
                "project_id": project_id,
                "agent_id": agent_id,
            }
        if method == "POST" and route == "/v1/skills":
            source = str(payload.get("source") or payload.get("path") or "").strip()
            text = payload.get("text")
            brief = str(payload.get("brief") or "").strip()
            wanted = str(payload.get("name") or "").strip()
            overwrite = payload.get("overwrite")
            if overwrite is None:
                overwrite = False
            elif not isinstance(overwrite, bool):
                return 400, {"error": "overwrite must be bool"}
            try:
                if brief and not source and text is None:
                    from witty_agent.skill_scaffold import create_skill_from_brief

                    meta = create_skill_from_brief(
                        brief,
                        name=wanted,
                        overwrite=overwrite,
                        project_id=project_id,
                        agent_id=agent_id,
                        root=STATE.root,
                    )
                else:
                    meta = install_user_skill(
                        source or None,
                        text=str(text) if text is not None else None,
                        project_id=project_id,
                        agent_id=agent_id,
                        root=STATE.root,
                        overwrite=overwrite,
                    )
            except FileExistsError as exc:
                message = str(exc)
                existing = message.removeprefix("用户技能 ").split(" ", 1)[0]
                return 409, {"error": message, "name": existing}
            except FileNotFoundError as exc:
                return 404, {"error": str(exc)}
            except ValueError as exc:
                return 400, {"error": str(exc)}
            except OSError as exc:
                return 400, {"error": str(exc)}
            catalog = load_catalog(project_id, agent_id, root=STATE.root)
            return 200, {
                "name": meta.name,
                "description": meta.description,
                "path": str(meta.path),
                "skill_file": str(meta.skill_file),
                "enabled": catalog.skill_enabled(meta.name),
                "allowed_tools": list(meta.allowed_tools),
                "license": meta.license,
                "origin": meta.origin,
                "network": meta.network,
                "network_label": network_label(meta.network),
                "user_dir": str(user_skills_dir(project_id, agent_id, root=STATE.root)),
                "project_id": project_id,
                "agent_id": agent_id,
            }
        name = route.rsplit("/", 1)[-1]
        if method == "GET":
            try:
                skill = load_skill(name, project_id, agent_id, root=STATE.root)
            except KeyError as exc:
                return 404, {"error": str(exc)}
            return 200, {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
                "skill_file": str(skill.skill_file),
                "body": skill.body,
                "enabled": catalog.skill_enabled(skill.name),
                "allowed_tools": list(skill.allowed_tools),
                "scripts": bool(skill.scripts_dir),
                "references": bool(skill.references_dir),
                "assets": bool(skill.assets_dir),
                "license": skill.license,
                "compatibility": skill.compatibility,
                "metadata": dict(skill.metadata),
                "origin": skill.origin,
                "network": skill.network,
                "network_label": network_label(skill.network),
            }
        if method == "PUT":
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                return 400, {"error": "enabled must be bool"}
            try:
                load_skill(name, project_id, agent_id, root=STATE.root)
            except KeyError as exc:
                return 404, {"error": str(exc)}
            catalog = set_skill_enabled(
                name, enabled, project_id, agent_id, root=STATE.root
            )
            return 200, {"name": name, "enabled": catalog.skill_enabled(name)}
        if method == "DELETE":
            try:
                path = uninstall_user_skill(name, project_id, agent_id, root=STATE.root)
            except KeyError as exc:
                return 404, {"error": str(exc)}
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return 200, {"name": name, "removed": True, "path": str(path)}

    if method == "GET" and route == "/v1/commands":
        session_id = query.get("session_id") or ""
        session = STATE.sessions.get(session_id) if session_id else None
        commands = session.slash_commands() if session is not None else CommandRegistry.kernel_catalog()
        return 200, {"commands": commands}

    if route == "/v1/tools" or route.startswith("/v1/tools/"):
        project_id, agent_id = _scope(payload, query)
        catalog = load_catalog(project_id, agent_id, root=STATE.root)
        specs = list_tools()
        if method == "GET" and route == "/v1/tools":
            rows = []
            for spec in specs:
                rows.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                        "kernel": is_kernel_tool(spec.name),
                        "enabled": catalog.tool_enabled(spec.name),
                        "timeout_ms": spec.timeout_ms,
                    }
                )
            return 200, {"tools": rows, "project_id": project_id, "agent_id": agent_id}
        name = route.rsplit("/", 1)[-1]
        spec = next((item for item in specs if item.name == name), None)
        if spec is None:
            return 404, {"error": f"unknown tool {name}"}
        if method == "GET":
            return 200, {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
                "kernel": is_kernel_tool(spec.name),
                "enabled": catalog.tool_enabled(spec.name),
                "timeout_ms": spec.timeout_ms,
            }
        if method == "PUT":
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                return 400, {"error": "enabled must be bool"}
            try:
                catalog = set_tool_enabled(
                    name, enabled, project_id, agent_id, root=STATE.root
                )
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return 200, {
                "name": name,
                "enabled": catalog.tool_enabled(name),
                "kernel": is_kernel_tool(name),
            }

    if route == "/v1/mail":
        from witty_agent.plugins.mail import public_mail_snapshot, save_mail_settings

        project_id, agent_id = _scope(payload, query)
        if method == "GET":
            return 200, public_mail_snapshot(project_id, agent_id, root=STATE.root)
        if method == "PUT":
            try:
                return 200, save_mail_settings(payload, project_id, agent_id, root=STATE.root)
            except (TypeError, ValueError) as exc:
                return 400, {"error": str(exc)}
        return 405, {"error": "method not allowed"}

    if route == "/v1/plugins" or route.startswith("/v1/plugins/"):
        from witty_agent.plugins import (
            attach_mcp,
            attach_package,
            attach_skill_path,
            detach_mcp,
            detach_package,
            detach_skill_path,
            flush_pending,
            list_plugins,
            public_live,
            reload_surface,
        )

        project_id, agent_id = _scope(payload, query)
        if route == "/v1/plugins":
            if method != "GET":
                return 405, {"error": "method not allowed"}
            return 200, list_plugins(project_id, agent_id, root=STATE.root)
        tail = route[len("/v1/plugins/") :].strip("/")
        try:
            force = bool(payload.get("force"))
            if tail == "reload" and method == "POST":
                return 200, reload_surface(force=force)
            if tail == "flush" and method == "POST":
                return 200, flush_pending() or {"applied": False, "deferred": False, **public_live()}
            if tail == "paths" and method == "POST":
                return 200, attach_skill_path(str(payload.get("path") or ""), force=force)
            if tail == "paths" and method == "DELETE":
                return 200, detach_skill_path(str(payload.get("path") or query.get("path") or ""), force=force)
            if tail == "packages" and method == "POST":
                depends = payload.get("depends")
                if depends is not None and not isinstance(depends, list):
                    return 400, {"error": "depends must be list"}
                return 200, attach_package(
                    str(payload.get("package") or payload.get("name") or ""),
                    path=str(payload.get("path") or "") or None,
                    depends=[str(item) for item in depends] if depends else None,
                    force=force,
                )
            if tail == "packages" and method == "DELETE":
                return 200, detach_package(
                    str(payload.get("package") or payload.get("name") or query.get("package") or ""),
                    force=force,
                )
            if tail == "mcp" and method == "POST":
                args = payload.get("args") or []
                if not isinstance(args, list):
                    return 400, {"error": "args must be list"}
                return 200, attach_mcp(
                    str(payload.get("name") or ""),
                    str(payload.get("command") or ""),
                    [str(item) for item in args],
                    force=force,
                )
            if tail.startswith("mcp/") and method == "DELETE":
                return 200, detach_mcp(unquote(tail.split("/", 1)[-1]), force=force)
            if tail == "mcp" and method == "DELETE":
                return 200, detach_mcp(str(payload.get("name") or query.get("name") or ""), force=force)
        except FileNotFoundError as exc:
            return 404, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 405, {"error": "method not allowed"}

    if route == "/v1/links":
        from witty_agent.links import habit_summary, harvest_links, render_links, search_links, upsert_link

        if method == "GET":
            asked = str(query.get("q") or payload.get("q") or "")
            rows = search_links(asked)
            return 200, {"links": rows, "text": render_links(rows), "habits": habit_summary()}
        if method == "POST":
            url = str(payload.get("url") or "")
            text = str(payload.get("text") or "")
            if url:
                item = upsert_link(
                    url,
                    title=str(payload.get("title") or ""),
                    intent=str(payload.get("intent") or ""),
                    note=str(payload.get("note") or ""),
                    alias=str(payload.get("alias") or ""),
                    source=str(payload.get("source") or "http"),
                )
                return 200, {"link": item}
            if text:
                rows = harvest_links(text, intent=str(payload.get("intent") or ""))
                return 200, {"links": rows, "text": render_links(rows)}
            return 400, {"error": "url or text required"}
        return 405, {"error": "method not allowed"}

    if route == "/v1/wiki":
        from witty_agent.plugins.llmwiki import public_wiki, wiki_add, wiki_init, wiki_remove
        from witty_agent.prompts import get_prompt

        work = str(query.get("workspace_dir") or payload.get("workspace_dir") or "").strip()
        if not work:
            return 400, {"error": get_prompt("wiki_need_workspace")}
        if method == "GET":
            return 200, public_wiki(work)
        if method == "POST":
            if payload.get("init"):
                text = wiki_init(work)
                body = public_wiki(work)
                body["text"] = text
                return 200, body
            source = str(payload.get("source") or payload.get("url") or payload.get("path") or "")
            text = wiki_add(source, work)
            body = public_wiki(work)
            body["text"] = text
            return 200, body
        if method == "DELETE":
            source_id = str(query.get("id") or payload.get("id") or payload.get("source_id") or "")
            text = wiki_remove(source_id, work)
            body = public_wiki(work)
            body["text"] = text
            return 200, body
        return 405, {"error": "method not allowed"}

    if route == "/v1/diary":
        from witty_agent.diary import append_diary, list_diary_days, read_diary
        from witty_agent.layout import memory_user_dir

        # 日记跟着 agent 走，别让它落在服务进程碰巧所在的工作目录里。
        project_id, agent_id = _scope(payload, query)
        home = memory_user_dir(project_id, agent_id, root=STATE.root)
        if method == "GET":
            if query.get("list") or payload.get("list"):
                return 200, {"days": list_diary_days(memory_dir=home)}
            day = str(query.get("day") or payload.get("day") or "")
            return 200, {
                "day": day or "today",
                "body": read_diary(day or None, memory_dir=home),
            }
        if method == "POST":
            text = str(payload.get("text") or "")
            if not text.strip():
                return 400, {"error": "text required"}
            path = append_diary(
                text,
                day=str(payload.get("day") or "") or None,
                kind="note",
                memory_dir=home,
            )
            return 200, {"path": path}
        return 405, {"error": "method not allowed"}

    if method == "POST" and route == "/v1/agents":
        agent = create_agent(
            str(payload.get("project_id") or DEFAULT_PROJECT_ID),
            str(payload.get("agent_id") or DEFAULT_AGENT_ID),
            root=STATE.root,
            description=str(payload.get("description") or ""),
        )
        return 200, {
            "project_id": agent.project.project_id,
            "agent_id": agent.record.agent_id,
        }

    if method == "GET" and route == "/v1/agents":
        project_id = query.get("project_id") or DEFAULT_PROJECT_ID
        return 200, {"agents": list_project_agents(project_id, root=STATE.root)}

    if method == "GET" and route == "/v1/sessions":
        project_id, agent_id = _scope(payload, query)
        directory = traces_dir(project_id, agent_id, root=STATE.root)
        rows = []
        seen: set[str] = set()
        now = datetime.now(timezone.utc).timestamp()
        for meta in list_trace_summaries(directory):
            sid = str(meta.get("id") or "")
            if not sid:
                continue
            seen.add(sid)
            live = STATE.sessions.get(sid)
            rows.append(
                {
                    "session_id": sid,
                    "title": (live.title if live and live.title else meta.get("title")) or sid[:8],
                    "workspace_dir": str(
                        live.workspace_dir if live else meta.get("cwd") or ""
                    ),
                    "messages": meta.get("messages") or 0,
                    "updated_at": float(meta.get("updated_at") or 0),
                    "live": live is not None,
                }
            )
        for sid, live in STATE.sessions.items():
            if sid in seen:
                continue
            if live.agent.project.project_id != project_id:
                continue
            if live.agent.record.agent_id != agent_id:
                continue
            rows.append(
                {
                    "session_id": sid,
                    "title": live.title or sid[:8],
                    "workspace_dir": str(live.workspace_dir),
                    "messages": 0,
                    "updated_at": now,
                    "live": True,
                }
            )
        rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return 200, {"sessions": rows, "project_id": project_id, "agent_id": agent_id}

    if method == "POST" and route == "/v1/sessions":
        project_id = str(payload.get("project_id") or DEFAULT_PROJECT_ID)
        agent_id = str(payload.get("agent_id") or DEFAULT_AGENT_ID)
        requested = payload.get("session_id")
        if requested:
            existing = _resolve_session(
                str(requested),
                project_id=project_id,
                agent_id=agent_id,
                workspace_dir=payload.get("workspace_dir"),
            )
            if existing is not None:
                return 200, {
                    "session_id": existing.session_id,
                    "workspace_dir": str(existing.workspace_dir),
                    "scratchpad": str(existing.scratchpad) if existing.scratchpad else "",
                    "title": existing.title,
                }
        agent = create_agent(project_id, agent_id, root=STATE.root)
        session = create_session(
            agent,
            workspace_dir=payload.get("workspace_dir"),
            session_id=payload.get("session_id"),
        )
        STATE.sessions[session.session_id] = session
        return 200, {
            "session_id": session.session_id,
            "workspace_dir": str(session.workspace_dir),
            "scratchpad": str(session.scratchpad) if session.scratchpad else "",
            "title": session.title,
        }

    if method == "GET" and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        project_id, agent_id = _scope(payload, query)
        session = _resolve_session(session_id, project_id=project_id, agent_id=agent_id)
        if session is None:
            return 404, {"error": "session not found"}
        if route.endswith("/messages"):
            messages = [_message_public(item) for item in load_messages(session._store_path())]
            return 200, {
                "session_id": session_id,
                "title": session.title,
                "messages": messages,
                "todos": list(current_todos(session.log) or []),
                "plan": _plan_public(session),
            }
        if route.endswith("/projection"):
            return 200, session.project()
        if route.endswith("/run"):
            run = STATE.runs.get(session_id)
            if run is None:
                return 404, {"error": "no run"}
            return 200, _run_public(run)
        return 200, {
            "session_id": session.session_id,
            "project_id": session.agent.project.project_id,
            "agent_id": session.agent.record.agent_id,
            "parent_id": session.parent_id,
            "title": session.title,
            "workspace_dir": str(session.workspace_dir),
        }

    if method == "DELETE" and route.startswith("/v1/sessions/") and len(route.strip("/").split("/")) == 3:
        session_id = unquote(route.split("/")[3])
        project_id, agent_id = _scope(payload, query)
        directory = traces_dir(project_id, agent_id, root=STATE.root)
        existed = delete_session_file(directory, session_id)
        live = STATE.sessions.pop(session_id, None)
        STATE.runs.pop(session_id, None)
        from witty_agent.fs_observe import forget_session

        forget_session(session_id)
        pad = scratchpad_dir(session_id, project_id, agent_id, root=STATE.root)
        if pad.is_dir():
            import shutil

            shutil.rmtree(pad, ignore_errors=True)
        logger.info("删除会话 session=%s existed=%s live=%s", session_id, existed, live is not None)
        return 200, {"ok": True, "session_id": session_id, "existed": bool(existed or live)}

    if method == "POST" and route.endswith("/messages") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        project_id, agent_id = _scope(payload, query)
        session = _resolve_session(session_id, project_id=project_id, agent_id=agent_id)
        if session is None:
            return 404, {"error": "session not found"}
        prompt = str(payload.get("prompt") or "")
        approval_mode = str(payload.get("approval_mode") or "allow-all")
        think_level = str(payload.get("think_level") or "short")
        if approval_mode not in APPROVAL_MODES:
            return 400, {"error": f"approval_mode must be one of {sorted(APPROVAL_MODES)}"}
        if think_level not in THINK_LEVELS:
            return 400, {"error": f"think_level must be one of {sorted(THINK_LEVELS)}"}
        if not _as_wait(payload.get("wait"), True):
            current = STATE.runs.get(session_id)
            if current and current.get("status") in {"running", "awaiting_approval"}:
                return 409, {"error": "run in progress"}
            run = _start_session_run(session, prompt, approval_mode, think_level)
            return 202, _run_public(run)
        factory = STATE.stream_factory
        if factory is None:
            from witty_agent.llm import OpenAICompatLLM

            factory = OpenAICompatLLM
        stream_fn = factory()
        if hasattr(stream_fn, "think_level"):
            stream_fn.think_level = think_level
        result = await session.run(
            prompt,
            stream_fn=stream_fn,
            approval_mode=approval_mode,  # type: ignore[arg-type]
        )
        close = _visible_assistant_close(result.messages)
        return 200, {
            "session_id": session_id,
            "text": str(close.get("text") or ""),
            "sealed": str(close.get("sealed") or ""),
            "evidence": list(close.get("evidence") or []),
            "trace_reason": str(close.get("trace_reason") or ""),
            "events": [item.type for item in result.events],
        }

    if method == "POST" and route.endswith("/approval") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        session = STATE.sessions.get(session_id)
        if session is None:
            return 404, {"error": "session not found"}
        run = STATE.runs.get(session_id)
        if run is None or run.get("status") != "awaiting_approval" or not run.get("pending"):
            return 409, {"error": "no pending approval"}
        pending = run["pending"]
        call_id = payload.get("tool_call_id")
        if call_id and call_id != pending.get("tool_call_id"):
            return 409, {"error": "tool_call_id mismatch"}
        decision = str(payload.get("decision") or "")
        if decision not in {"allow", "deny"}:
            return 400, {"error": "decision must be allow or deny"}
        with STATE.run_lock:
            run["decision"] = decision
            gate = run.get("gate")
        if gate is not None:
            gate.set()
        return 200, {"ok": True, "decision": decision}

    if method == "POST" and route.endswith("/answer") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        session = STATE.sessions.get(session_id)
        if session is None:
            return 404, {"error": "session not found"}
        run = STATE.runs.get(session_id)
        if run is None or run.get("status") != "awaiting_question" or not run.get("question"):
            return 409, {"error": "no pending question"}
        answers = payload.get("answers")
        if not isinstance(answers, list):
            return 400, {"error": "answers must be a list"}
        with STATE.run_lock:
            run["answers"] = answers
            gate = run.get("qgate")
        if gate is not None:
            gate.set()
        return 200, {"ok": True}

    if method == "POST" and route.endswith("/fork") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        session = STATE.sessions.get(session_id)
        if session is None:
            return 404, {"error": "session not found"}
        child = session.fork()
        STATE.sessions[child.session_id] = child
        return 200, {"session_id": child.session_id, "parent_id": session.session_id}

    if method == "POST" and route.endswith("/steer") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        session = STATE.sessions.get(session_id)
        if session is None:
            return 404, {"error": "session not found"}
        text = str(payload.get("text") or "").strip()
        if not text:
            return 400, {"error": "text required"}
        session.steer(text)
        return 200, {"ok": True}

    if method == "POST" and route.endswith("/abort") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        session = STATE.sessions.get(session_id)
        if session is None:
            return 404, {"error": "session not found"}
        session.abort()
        # 中止是协作式的：worker 要跑到下一个回合边界才认账，中间可能正卡在一次长模型调用
        # 或一个长工具里。所以这里立刻把 run 落终态——否则界面已经显示「已停止生成」，服务端
        # 却还挂在 running，用户接着发就被 409 "run in progress" 挡死，只能新开会话。
        # 用 done 而不是新造一个状态：SSE 和前端都只认 done / error 两个终态。
        # 旧 worker 之后还会往这份 run 字典里写，但下一轮会在 STATE.runs 换上新字典，
        # 写不到新一轮头上；它自己也会在下个回合边界看到 `_run_gen` 变了而退出。
        with STATE.run_lock:
            run = STATE.runs.get(session_id)
            live = bool(run) and run.get("status") in {"running", "awaiting_approval", "awaiting_question"}
            gate = qgate = None
            if live and run is not None:
                gate, qgate = run.get("gate"), run.get("qgate")
                run["status"] = "done"
                run["aborted"] = True
                run["pending"] = None
                run["question"] = None
                run["gate"] = None
                run["qgate"] = None
        # 放掉可能正等在审批/提问上的 worker，别让它干等到超时（默认 300s）
        for waiter in (gate, qgate):
            if waiter is not None:
                waiter.set()
        if live and run is not None:
            _publish_run(run, {"type": "done", "text": str(run.get("text") or ""), "aborted": True})
        logger.info("中止会话 session=%s 释放运行=%s", session_id, live)
        return 200, {"ok": True, "aborted": live}

    if method == "POST" and route.endswith("/command") and route.startswith("/v1/sessions/"):
        session_id = route.split("/")[3]
        session = STATE.sessions.get(session_id)
        if session is None:
            return 404, {"error": "session not found"}
        result = session.dispatch_command(str(payload.get("text") or payload.get("command") or ""))
        if result is None:
            return 400, {"error": "unknown command"}
        return 200, {"kind": result.kind, "text": result.text, "remainder": result.remainder}

    if route == "/v1/vault":
        project_id = payload.get("project_id") or query.get("project_id") or DEFAULT_PROJECT_ID
        agent_id = payload.get("agent_id") or query.get("agent_id") or DEFAULT_AGENT_ID
        if method == "GET":
            return 200, {"keys": mask_vault(load_vault(project_id, agent_id, root=STATE.root))}
        if method == "PUT":
            set_vault_entry(
                str(payload["key"]),
                str(payload["value"]),
                project_id,
                agent_id,
                root=STATE.root,
            )
            return 200, {"ok": True, "key": payload["key"]}
        if method == "DELETE":
            delete_vault_entry(str(payload.get("key") or query.get("key") or ""), project_id, agent_id, root=STATE.root)
            return 200, {"ok": True}

    if route == "/v1/memory":
        project_id = payload.get("project_id") or query.get("project_id") or DEFAULT_PROJECT_ID
        agent_id = payload.get("agent_id") or query.get("agent_id") or DEFAULT_AGENT_ID
        workspace = payload.get("workspace_dir") or query.get("workspace_dir") or str(Path.cwd())
        memory = resolve_session_memory(
            project_id=project_id,
            agent_id=agent_id,
            workspace=workspace,
            root=STATE.root,
        )
        scope = payload.get("scope") or query.get("scope") or "user"
        directory = memory.workspace_dir if scope == "workspace" else memory.user_dir
        if directory is None:
            return 400, {"error": "workspace memory missing"}
        if method == "GET":
            slug = query.get("slug")
            if slug:
                return 200, {"slug": slug, "body": read_topic(directory, slug)}
            label = scope if scope in {"user", "workspace"} else "user"
            asked = str(query.get("q") or payload.get("q") or "")
            payload = public_memory(
                directory,
                query=asked,
                scope=label,
            )
            if label == "user" and memory.workspace_dir is not None:
                payload = attach_workspace_public(payload, memory.workspace_dir, query=asked)
            return 200, payload
        if method == "POST":
            slug = str(payload.get("slug") or "")
            if not slug:
                return 400, {"error": "slug required"}
            description = str(payload.get("description") or "").strip()
            if not description:
                description = _topic_description(directory, slug)
            path = write_topic(
                directory,
                slug,
                description=description,
                body=str(payload.get("body") or ""),
            )
            if scope != "workspace":
                rebuild_memory_index(directory)
            return 200, {"ok": True, "path": str(path), "slug": slug}

    if route == "/v1/schedules" or (
        route.startswith("/v1/schedules/") and route != "/v1/schedules/tick"
    ):
        from witty_agent.schedule import (
            ScheduleDefinition,
            Scheduler,
            delete_schedule,
            list_schedule_files,
            parse_instant,
            parse_period,
            set_schedule_enabled,
            write_schedule,
        )

        project_id = payload.get("project_id") or query.get("project_id") or DEFAULT_PROJECT_ID
        agent_id = payload.get("agent_id") or query.get("agent_id") or DEFAULT_AGENT_ID
        if method == "GET" and route == "/v1/schedules":
            tracker = Scheduler(STATE.root) if STATE.root is not None else None
            rows = []
            for item in list_schedule_files(project_id, agent_id, root=STATE.root):
                if item.ok and item.definition:
                    status = (
                        tracker.task_status(project_id, agent_id, item.definition.name)
                        if tracker is not None
                        else "active"
                    )
                    next_fire = (
                        tracker.next_fire_iso(project_id, agent_id, item.definition)
                        if tracker is not None
                        else None
                    )
                    rows.append(
                        {
                            "name": item.definition.name,
                            "enabled": item.definition.enabled,
                            "prompt": item.definition.prompt,
                            "start_at": item.definition.start_at,
                            "period": item.definition.period,
                            "end_at": item.definition.end_at,
                            "session_id": item.definition.session_id,
                            "workspace": item.definition.workspace,
                            "status": status,
                            "next_fire_at": next_fire,
                        }
                    )
                else:
                    rows.append({"error": item.error})
            return 200, {"schedules": rows}
        if method == "PUT" and route == "/v1/schedules":
            start = parse_instant(payload.get("start_at"))
            if start is None:
                return 400, {"error": "start_at invalid"}
            period = str(payload.get("period") or "")
            period_ms = parse_period(period) if period else None
            if period and period_ms is None:
                return 400, {"error": "period must look like 30m / 12h / 7d"}
            end = parse_instant(payload.get("end_at")) if payload.get("end_at") else None
            if payload.get("end_at") and end is None:
                return 400, {"error": "end_at invalid"}
            session_id = payload.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                return 400, {"error": "session_id must be a string"}
            definition = ScheduleDefinition(
                name=str(payload["name"]),
                prompt=str(payload["prompt"]),
                enabled=bool(payload.get("enabled", False)),
                start_at=start[1],
                start_at_ms=start[0],
                period=period or None,
                period_ms=period_ms,
                end_at=end[1] if end else None,
                end_at_ms=end[0] if end else None,
                session_id=session_id or None,
                workspace=payload.get("workspace"),
            )
            path = write_schedule(definition, project_id, agent_id, root=STATE.root)
            tracker = Scheduler(STATE.root) if STATE.root is not None else None
            next_fire = (
                tracker.next_fire_iso(project_id, agent_id, definition) if tracker is not None else None
            )
            return 200, {"path": str(path), "name": definition.name, "next_fire_at": next_fire}
        if method == "PATCH" and route.startswith("/v1/schedules/"):
            name = unquote(route.rsplit("/", 1)[-1])
            if "enabled" not in payload:
                return 400, {"error": "enabled required"}
            try:
                definition = set_schedule_enabled(
                    name,
                    bool(payload.get("enabled")),
                    project_id,
                    agent_id,
                    root=STATE.root,
                )
            except FileNotFoundError:
                return 404, {"error": f"unknown schedule {name}"}
            except ValueError as exc:
                return 400, {"error": str(exc)}
            tracker = Scheduler(STATE.root) if STATE.root is not None else None
            next_fire = (
                tracker.next_fire_iso(project_id, agent_id, definition) if tracker is not None else None
            )
            return 200, {"name": definition.name, "enabled": definition.enabled, "next_fire_at": next_fire}
        if method == "DELETE" and route.startswith("/v1/schedules/"):
            name = unquote(route.rsplit("/", 1)[-1])
            try:
                removed = delete_schedule(name, project_id, agent_id, root=STATE.root)
            except ValueError as exc:
                return 400, {"error": str(exc)}
            if not removed:
                return 404, {"error": f"unknown schedule {name}"}
            return 200, {"name": name, "deleted": True}
        return 405, {"error": "method not allowed"}

    if method == "GET" and route == "/v1/traces":
        project_id = query.get("project_id") or DEFAULT_PROJECT_ID
        agent_id = query.get("agent_id") or DEFAULT_AGENT_ID
        directory = traces_dir(project_id, agent_id, root=STATE.root)
        return 200, {"traces": list_trace_summaries(directory)}

    if method == "GET" and route == "/v1/benchmarks":
        from witty_agent.evolution.benchmark import ensure_benchmark
        from witty_agent.evolution.cases import list_cases
        from witty_agent.state.agent_state import load_agent_state

        project_id = query.get("project_id") or DEFAULT_PROJECT_ID
        agent_id = query.get("agent_id") or DEFAULT_AGENT_ID
        benchmark_id = query.get("benchmark_id") or "example-benchmark"
        record = load_agent_state(project_id, agent_id, root=STATE.root)
        cases = [{"case_id": item.case_id} for item in list_cases(record, benchmark_id, root=STATE.root)]
        return 200, {"benchmark_id": benchmark_id, "cases": cases, "path": str(ensure_benchmark(record, benchmark_id, root=STATE.root))}

    if method == "POST" and route == "/v1/schedules/tick":
        return 200, await run_due_schedules()

    if method == "POST" and route == "/v1/jobs":
        from witty_agent.orchestrator import JobSpec, Orchestrator

        if STATE.root is None:
            return 400, {"error": "api root not configured"}
        factory = STATE.stream_factory
        if factory is None:
            from witty_agent.llm import OpenAICompatLLM

            factory = OpenAICompatLLM
        orch = Orchestrator(STATE.root, factory())
        result = await orch.dispatch(
            JobSpec(
                prompt=str(payload.get("prompt") or ""),
                kind=payload.get("kind") or "chat",  # type: ignore[arg-type]
                project_id=str(payload.get("project_id") or DEFAULT_PROJECT_ID),
                agent_id=str(payload.get("agent_id") or DEFAULT_AGENT_ID),
                workspace=payload.get("workspace_dir"),
                budget_rounds=int(payload.get("budget_rounds") or -1),
                fanout_prompts=list(payload.get("fanout_prompts") or []),
            )
        )
        return 200, {
            "job_id": result.job_id,
            "status": result.status,
            "text": result.text,
            "session_id": result.session_id,
            "children": result.children,
        }

    if method == "GET" and route == "/v1/jobs":
        from witty_agent.orchestrator import list_jobs

        if STATE.root is None:
            return 400, {"error": "api root not configured"}
        project_id = query.get("project_id") or DEFAULT_PROJECT_ID
        return 200, {"jobs": list_jobs(project_id, root=STATE.root)}

    return 404, {"error": f"unknown route {method} {route}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logger.info(format, *args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route.startswith("/v1/sessions/") and route.endswith("/stream"):
            self._stream_run(route.split("/")[3])
            return
        self._dispatch("GET")

    def _stream_run(self, session_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        try:
            for item in iter_run_stream_events(session_id):
                payload = json.dumps(item, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except BrokenPipeError:
            return
        except ConnectionResetError:
            return

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        import asyncio

        try:
            status, payload = asyncio.run(handle_request(method, self.path, self._body()))
        except Exception as exc:
            logger.warning("HTTP 失败 path=%s err=%s", self.path, exc)
            status, payload = 400, {"error": str(exc)}
        self._send(status, payload)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    from witty_agent.plugins.live import load_live, set_busy_probe
    from witty_agent.plugins.watch import start_watcher, stop_watcher

    if STATE.root is None:
        STATE.root = data_root()
    set_busy_probe(_any_run_busy)
    load_live(STATE.root)
    start_watcher()
    _hydrate_model_secrets(DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID)
    ensure_model_catalog(DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID, root=STATE.root)
    apply_active_model(DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID, root=STATE.root)
    start_schedule_ticker()
    from witty_agent.sandbox import warm_sandbox

    threading.Thread(
        target=warm_sandbox,
        kwargs={"root": STATE.root},
        daemon=True,
        name="witty-sandbox-warm",
    ).start()
    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("HTTP API %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        stop_schedule_ticker()
        stop_watcher()


def api_root() -> Path:
    return STATE.root or data_root()


def _run_is_busy(session_id: str) -> bool:
    current = STATE.runs.get(session_id)
    return bool(current and current.get("status") in _BUSY_RUN)


async def run_due_schedules() -> dict[str, Any]:
    """扫到期任务。绑了会话的走窗口同一套 run；会话忙则跳过这一枪。"""
    from witty_agent.orchestrator import JobSpec, Orchestrator
    from witty_agent.schedule import Scheduler

    root = api_root()
    with _TICK_LOCK:
        fires = Scheduler(root).tick()
    started: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    factory = STATE.stream_factory
    for fire in fires:
        if fire.session_id:
            session = _resolve_session(
                fire.session_id,
                project_id=fire.project_id,
                agent_id=fire.agent_id,
                workspace_dir=fire.workspace,
            )
            if session is None:
                skipped.append({"name": fire.name, "reason": "session-missing"})
                continue
            if _run_is_busy(session.session_id):
                logger.info("循环触发时会话忙，跳过 name=%s session=%s", fire.name, session.session_id)
                skipped.append({"name": fire.name, "reason": "busy", "session_id": session.session_id})
                continue
            _start_session_run(session, fire.prompt, "always-ask")
            started.append(
                {
                    "name": fire.name,
                    "session_id": session.session_id,
                    "status": "started",
                }
            )
            continue
        if factory is None:
            skipped.append({"name": fire.name, "reason": "no-stream"})
            continue
        orch = Orchestrator(root, factory())
        result = await orch.dispatch(
            JobSpec(
                kind="schedule",
                prompt=fire.prompt,
                project_id=fire.project_id,
                agent_id=fire.agent_id,
                workspace=fire.workspace,
                session_id=fire.session_id,
            )
        )
        jobs.append({"job_id": result.job_id, "status": result.status, "name": fire.name})
    return {
        "ran": bool(started or jobs),
        "fires": [
            {
                "project_id": item.project_id,
                "agent_id": item.agent_id,
                "name": item.name,
                "session_id": item.session_id,
            }
            for item in fires
        ],
        "started": started,
        "skipped": skipped,
        "jobs": jobs,
    }


def start_schedule_ticker(*, interval_s: float | None = None) -> None:
    """挂在 serve 进程里扫定时任务。interval_s=0 关闭。不是第二套调度器。"""
    global _TICKER_THREAD
    stop_schedule_ticker()
    seconds = schedule_settings()["tick_interval_s"] if interval_s is None else float(interval_s)
    if seconds <= 0:
        logger.info("定时扫描已关闭 tick_interval_s=0")
        return
    _TICKER_STOP.clear()

    def loop() -> None:
        while not _TICKER_STOP.wait(seconds):
            try:
                asyncio.run(run_due_schedules())
            except Exception as exc:
                logger.warning("定时扫描失败 err=%s", exc)

    _TICKER_THREAD = threading.Thread(target=loop, name="schedule-ticker", daemon=True)
    _TICKER_THREAD.start()
    logger.info("定时扫描已启动 interval_s=%s", seconds)


def stop_schedule_ticker() -> None:
    global _TICKER_THREAD
    _TICKER_STOP.set()
    thread = _TICKER_THREAD
    _TICKER_THREAD = None
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1.5)

