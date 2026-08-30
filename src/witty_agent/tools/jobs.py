"""列出、读取、停止项目作业。"""

from __future__ import annotations

import json

from witty_agent import hooks
from witty_agent.layout import DEFAULT_PROJECT_ID
from witty_agent.orchestrator import list_jobs
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool


def _root_project() -> tuple[object, str]:
    root = hooks.current_root
    project = hooks.current_project_id or DEFAULT_PROJECT_ID
    if root is None:
        raise RuntimeError(get_prompt("job_needs_session"))
    return root, project


def job_list() -> str:
    """列出当前项目已落盘的后台作业。"""
    root, project = _root_project()
    rows = list_jobs(project, root=root)
    if not rows:
        return get_prompt("job_list_empty")
    public = [
        {
            "id": item.get("job_id"),
            "kind": item.get("kind"),
            "status": item.get("status"),
            "session_id": item.get("session_id"),
            "text": str(item.get("text") or "")[:200],
        }
        for item in rows
    ]
    return json.dumps(public, ensure_ascii=False)


def job_output(job_id: str) -> str:
    """读取一个作业的当前快照。"""
    root, project = _root_project()
    for item in list_jobs(project, root=root):
        if item.get("job_id") == job_id:
            return json.dumps(item, ensure_ascii=False)
    raise ValueError(get_prompt("job_not_found", job_id=job_id))


def job_kill(job_id: str) -> str:
    """把作业标记为 aborted（不杀外部进程）。"""
    from pathlib import Path

    from witty_agent.layout import jobs_dir

    root, project = _root_project()
    path = jobs_dir(project, root=root) / f"{job_id}.json"
    if not path.is_file():
        raise ValueError(get_prompt("job_not_found", job_id=job_id))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "aborted"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return get_prompt("job_killed", job_id=job_id)


register_tool(
    ToolSpec(
        name="job_list",
        description=get_prompt("tool_desc_job_list"),
        parameters={"type": "object", "properties": {}},
        func=job_list,
    )
)
register_tool(
    ToolSpec(
        name="job_output",
        description=get_prompt("tool_desc_job_output"),
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        func=job_output,
    )
)
register_tool(
    ToolSpec(
        name="job_kill",
        description=get_prompt("tool_desc_job_kill"),
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        func=job_kill,
    )
)
