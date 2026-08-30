"""对照官方 session-projection：从日志折叠当前有效切面。"""

from __future__ import annotations

from typing import Any

from witty_agent.session_log import SessionLog, fold_plan_mode, project_todos


def project_session(log: SessionLog, *, plan_pending: bool = False) -> dict[str, Any]:
    return {
        "todos": project_todos(log.events),
        "plan": {"active": fold_plan_mode(log.events), "pending": plan_pending},
    }
