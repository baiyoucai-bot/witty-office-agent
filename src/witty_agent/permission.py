"""库用法的权限等级。桌面/HTTP 默认仍走 always-ask，这里给后台跑。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from witty_agent.approval import ApprovalDecision, ApprovalMode, ApproveFn
from witty_agent.layout import data_root
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt

AskFn = Callable[[str, str, dict], Awaitable[ApprovalDecision | None] | ApprovalDecision | None]
TimeoutAction = Literal["allow", "deny"]
PermissionLevel = Literal["allow", "ask", "read-only", "deny"]

LEVEL_TO_MODE: dict[str, ApprovalMode] = {
    "allow": "allow-all",
    "ask": "always-ask",
    "read-only": "read-only",
    "deny": "deny-all",
}
LEVELS = frozenset(LEVEL_TO_MODE)
TIMEOUT_ACTIONS = frozenset({"allow", "deny"})
_SAFE_ARG_KEYS = ("path", "command", "query", "dest")

logger = get_logger("permission")


def normalize_level(raw: str) -> PermissionLevel:
    text = (raw or "").strip().lower().replace("_", "-")
    aliases = {
        "allow-all": "allow",
        "always-ask": "ask",
        "deny-all": "deny",
        "readonly": "read-only",
        "read_only": "read-only",
    }
    text = aliases.get(text, text)
    if text not in LEVELS:
        raise ValueError(f"未知权限等级 {raw!r}，可选: allow / ask / read-only / deny")
    return text  # type: ignore[return-value]


def normalize_timeout_action(raw: str) -> TimeoutAction:
    text = (raw or "").strip().lower()
    if text in {"continue", "proceed", "yes"}:
        text = "allow"
    if text in {"abort", "stop", "no"}:
        text = "deny"
    if text not in TIMEOUT_ACTIONS:
        raise ValueError(f"超时动作必须是 allow 或 deny: {raw!r}")
    return text  # type: ignore[return-value]


def _safe_args(args: dict) -> dict[str, str]:
    public: dict[str, str] = {}
    for key in _SAFE_ARG_KEYS:
        if key not in args:
            continue
        public[key] = str(args[key])[:200]
    return public


@dataclass
class PermissionPolicy:
    level: PermissionLevel = "ask"
    timeout_sec: float = 30.0
    on_timeout: TimeoutAction = "allow"
    ask: AskFn | None = None
    inbox: Path | None = None

    def approval_mode(self) -> ApprovalMode:
        return LEVEL_TO_MODE[self.level]

    def make_approve(self, session_id: str) -> ApproveFn | None:
        if self.level != "ask":
            return None
        inbox = self.inbox
        ask = self.ask
        timeout_sec = max(0.0, float(self.timeout_sec))
        on_timeout = self.on_timeout

        async def approve(tool_name: str, call_id: str, args: dict) -> ApprovalDecision:
            pending = _write_pending(inbox, session_id, tool_name, call_id, args)
            reply = pending.with_suffix(".reply")
            action = get_prompt(
                "library_timeout_allow" if on_timeout == "allow" else "library_timeout_deny"
            )
            logger.info(
                get_prompt(
                    "library_approval_pending",
                    tool=tool_name,
                    call_id=call_id,
                    reply=str(reply),
                    timeout=str(timeout_sec),
                    action=action,
                )
            )
            deadline = time.monotonic() + timeout_sec
            ask_task: asyncio.Task[ApprovalDecision | None] | None = None
            if ask is not None:
                ask_task = asyncio.create_task(_invoke_ask(ask, tool_name, call_id, args))
            try:
                while True:
                    if ask_task is not None and ask_task.done():
                        error = ask_task.exception()
                        if error is not None:
                            logger.warning("审批回调失败 tool=%s: %s", tool_name, error)
                            ask_task = None
                        else:
                            decided = ask_task.result()
                            if decided in {"allow", "deny"}:
                                return decided
                            ask_task = None
                    file_decision = _read_reply(reply)
                    if file_decision is not None:
                        return file_decision
                    if time.monotonic() >= deadline:
                        logger.info(
                            get_prompt(
                                "library_approval_timeout",
                                tool=tool_name,
                                call_id=call_id,
                                action=action,
                            )
                        )
                        return on_timeout
                    await asyncio.sleep(min(0.2, max(0.02, deadline - time.monotonic())))
            finally:
                if ask_task is not None and not ask_task.done():
                    ask_task.cancel()

        return approve


async def _invoke_ask(
    ask: AskFn, tool_name: str, call_id: str, args: dict
) -> ApprovalDecision | None:
    result = ask(tool_name, call_id, args)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        result = await result  # type: ignore[assignment]
    if result in {"allow", "deny"}:
        return result  # type: ignore[return-value]
    return None


def _write_pending(
    inbox: Path | None,
    session_id: str,
    tool_name: str,
    call_id: str,
    args: dict,
) -> Path:
    root = inbox if inbox is not None else data_root() / "approvals"
    folder = root / session_id
    folder.mkdir(parents=True, exist_ok=True)
    pending = folder / f"{call_id}.json"
    payload = {
        "tool": tool_name,
        "call_id": call_id,
        "args": _safe_args(args),
        "reply": str(pending.with_suffix(".reply")),
    }
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return pending


def _read_reply(path: Path) -> ApprovalDecision | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if not text:
        return None
    if text[0] == "{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        text = str(data.get("decision") or "").strip().lower()
    if text in {"allow", "deny"}:
        return text  # type: ignore[return-value]
    return None
