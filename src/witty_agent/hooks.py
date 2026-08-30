"""运行期钩子：子代理复用当前 stream_fn，避免循环依赖。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from witty_agent.types import AgentContext, AgentMessage

StreamFn = Callable[[AgentContext], Awaitable[AgentMessage]]

subagent_stream_fn: StreamFn | None = None
subagent_approve: Callable[..., Awaitable[str]] | None = None
current_project_id: str = ""
current_agent_id: str = ""
current_workspace: str = ""
current_root: Any = None
command_manager: Any = None
repl_host: Any = None
subagent_sessions: dict[str, Any] = {}
subagent_depth: int = 0
session_log: Any = None
plan_mode: Any = None
user_questions: Any = None
capabilities: Any = None


def bind(
    *,
    stream_fn: StreamFn,
    approve: Callable[..., Awaitable[str]] | None,
    project_id: str,
    workspace: str,
    root: Any,
    agent_id: str = "",
    commands: Any = None,
    session_log_obj: Any = None,
    plan_mode_obj: Any = None,
    user_questions_obj: Any = None,
    capabilities_obj: Any = None,
) -> None:
    global subagent_stream_fn, subagent_approve, current_project_id, current_agent_id
    global current_workspace, current_root, command_manager
    global session_log, plan_mode, user_questions, capabilities
    subagent_stream_fn = stream_fn
    subagent_approve = approve
    current_project_id = project_id
    current_agent_id = agent_id
    current_workspace = workspace
    current_root = root
    if commands is not None:
        command_manager = commands
    if session_log_obj is not None:
        session_log = session_log_obj
    if plan_mode_obj is not None:
        plan_mode = plan_mode_obj
    if user_questions_obj is not None:
        user_questions = user_questions_obj
    if capabilities_obj is not None:
        capabilities = capabilities_obj


def reset() -> None:
    global subagent_stream_fn, subagent_approve, current_project_id, current_agent_id
    global current_workspace, current_root, command_manager, subagent_sessions, subagent_depth
    global session_log, plan_mode, user_questions, capabilities, repl_host
    # 解释器是真进程，不收就漏。鸭子调用，免得 hooks 反过来依赖 repl。
    if repl_host is not None:
        try:
            repl_host.close()
        except Exception:  # noqa: BLE001 - 收尾不该把 reset 弄崩
            pass
    repl_host = None
    subagent_stream_fn = None
    subagent_approve = None
    current_project_id = ""
    current_agent_id = ""
    current_workspace = ""
    current_root = None
    command_manager = None
    subagent_sessions = {}
    subagent_depth = 0
    session_log = None
    plan_mode = None
    user_questions = None
    capabilities = None
