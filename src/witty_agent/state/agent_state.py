"""Agent State：版本、AGENTS.md、system_config、技能与记忆目录。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from witty_agent.layout import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    agent_state_dir,
    assert_id,
    benchmarks_dir,
    memory_user_dir,
    schedule_dir,
    skills_dir,
    snapshots_dir,
    traces_dir,
)
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt

logger = get_logger("state.agent")


@dataclass
class AgentRecord:
    project_id: str
    agent_id: str
    name: str
    description: str
    version: int
    approval_mode: str
    state_dir: Path


def _config_path(state: Path) -> Path:
    return state / "system_config.toml"


def _agents_md_path(state: Path) -> Path:
    return state / "AGENTS.md"


# 角色正文进每一轮系统提示，不设帽就能被一份长文档挤掉指引。
# /refine 往这个文件追加沉淀，追加前按同一个帽拒绝超量——两处必须是同一个数。
ROLE_MAX_CHARS = 2000
_ROLE_MAX_CHARS = ROLE_MAX_CHARS


def _first_line(text: str) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line:
            return line
    return ""


def agent_role_text(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> str:
    """这个 agent 自己的角色段（state/AGENTS.md），没写就是空串。

    只认用户真写过的内容：种子脚手架和历史上被灌进来的 `harness_system` 副本都返回空。
    旧版本把运行时角色提示词整份抄进这个文件，注入它等于把同一段角色说两遍。
    """
    path = _agents_md_path(agent_state_dir(project_id, agent_id, root=root))
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not text:
        return ""
    if text == get_prompt("agent_role_seed").strip():
        return ""
    if _first_line(text) == _first_line(get_prompt("harness_system")):
        logger.info("跳过 AGENTS.md 角色注入：仍是运行时角色提示词的副本 path=%s", path)
        return ""
    if len(text) > _ROLE_MAX_CHARS:
        return text[:_ROLE_MAX_CHARS].rstrip() + "\n" + get_prompt(
            "agent_role_truncated", max_chars=str(_ROLE_MAX_CHARS)
        )
    return text


def _write_config(record: AgentRecord) -> None:
    text = (
        f'name = "{record.name}"\n'
        f'description = "{record.description}"\n'
        f"version = {record.version}\n"
        f'approval_mode = "{record.approval_mode}"\n'
    )
    _config_path(record.state_dir).write_text(text, encoding="utf-8")


def init_agent_state(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
    description: str = "",
) -> AgentRecord:
    assert_id("project_id", project_id)
    assert_id("agent_id", agent_id)
    state = agent_state_dir(project_id, agent_id, root=root)
    state.mkdir(parents=True, exist_ok=True)
    for extra in (
        skills_dir(project_id, agent_id, root=root),
        memory_user_dir(project_id, agent_id, root=root),
        traces_dir(project_id, agent_id, root=root),
        snapshots_dir(project_id, agent_id, root=root),
        benchmarks_dir(project_id, agent_id, root=root),
        schedule_dir(project_id, agent_id, root=root),
    ):
        extra.mkdir(parents=True, exist_ok=True)
    index = memory_user_dir(project_id, agent_id, root=root) / "MEMORY.md"
    if not index.is_file():
        index.write_text("# Memory\n\n", encoding="utf-8")
    if not _agents_md_path(state).is_file():
        _agents_md_path(state).write_text(get_prompt("agent_role_seed") + "\n", encoding="utf-8")
    if _config_path(state).is_file():
        return load_agent_state(project_id, agent_id, root=root)
    record = AgentRecord(
        project_id=project_id,
        agent_id=agent_id,
        name=agent_id,
        description=description,
        version=1,
        approval_mode="always-ask",
        state_dir=state,
    )
    _write_config(record)
    if agent_id == DEFAULT_AGENT_ID:
        from witty_agent.evolution.example import provision_example_benchmark

        provision_example_benchmark(record, root=root)
    logger.info("初始化 Agent project=%s agent=%s", project_id, agent_id)
    return record


def load_agent_state(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> AgentRecord:
    from witty_agent.tomlcompat import tomllib

    state = agent_state_dir(project_id, agent_id, root=root)
    path = _config_path(state)
    if not path.is_file():
        return init_agent_state(project_id, agent_id, root=root)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return AgentRecord(
        project_id=project_id,
        agent_id=agent_id,
        name=str(data.get("name") or agent_id),
        description=str(data.get("description") or ""),
        version=int(data.get("version") or 1),
        approval_mode=str(data.get("approval_mode") or "always-ask"),
        state_dir=state,
    )


def save_agent_state(record: AgentRecord) -> None:
    record.state_dir.mkdir(parents=True, exist_ok=True)
    _write_config(record)


def bump_version(record: AgentRecord) -> AgentRecord:
    record.version += 1
    save_agent_state(record)
    return record
