"""全局 / 项目 / Agent / 工作区 的数据目录约定。

全局家目录：WITTY_HOME，默认 ~/.witty/data
项目：租户，下面可以有多个 Agent（第一期只用 default_agent）
工作区：某次会话对应的代码目录，不负责存模型钥匙
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_PROJECT_ID = "default_project"
DEFAULT_AGENT_ID = "default_agent"

_ID_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


def assert_id(kind: str, value: str) -> str:
    if not value or len(value) > 64 or not _ID_RE.fullmatch(value):
        raise ValueError(f"{kind} 不合法: {value!r}（小写字母/数字/下划线或连字符，最长 64）")
    return value


def data_root() -> Path:
    override = os.environ.get("WITTY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".witty" / "data"


def project_dir(project_id: str = DEFAULT_PROJECT_ID, *, root: Path | None = None) -> Path:
    return (root or data_root()) / assert_id("project_id", project_id)


def agent_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return project_dir(project_id, root=root) / "agents" / assert_id("agent_id", agent_id)


def agent_state_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_dir(project_id, agent_id, root=root) / "agent_state"


def project_config_path(
    project_id: str = DEFAULT_PROJECT_ID, *, root: Path | None = None
) -> Path:
    return project_dir(project_id, root=root) / ".project_config.toml"


def vault_path(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / ".vault.toml"


def scratchpad_dir(
    session_id: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_dir(project_id, agent_id, root=root) / "scratchpad" / session_id


def traces_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_dir(project_id, agent_id, root=root) / "traces"


def snapshots_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_dir(project_id, agent_id, root=root) / "snapshots"


def benchmarks_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_dir(project_id, agent_id, root=root) / "benchmarks"


def jobs_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return project_dir(project_id, root=root) / "jobs"


def schedule_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "schedule"


def skills_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "skills"


def memory_user_dir(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return agent_state_dir(project_id, agent_id, root=root) / "memory" / "user"


def memory_workspace_dir(
    workspace_key: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    return (
        agent_state_dir(project_id, agent_id, root=root)
        / "memory"
        / assert_id("workspace_key", workspace_key)
    )


def criteria_dir(
    workspace_key: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    """已经证明过的完成判据（回归义务台账）。挂 Agent，再按工作区分。

    挂 Agent 是为了跨会话累积：判据一旦验过就该一直成立，换个会话不该从零开始。
    但**必须**再按工作区分——`pytest -q` 在 A 仓库验过，对 B 仓库既无意义又会红（那条
    命令在 B 里可能根本不存在），拿 A 的义务去卡 B 是纯误挡。跟证伪账本同一个键法。
    """
    return (
        agent_state_dir(project_id, agent_id, root=root)
        / "criteria"
        / assert_id("workspace_key", workspace_key)
    )
