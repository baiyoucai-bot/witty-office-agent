"""定时任务：agent_state/schedule/<name>.toml 是意图，运行状态另存。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, assert_id, schedule_dir
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt

logger = get_logger("schedule")
MIN_PERIOD_MS = 5 * 60_000
_PERIOD_RE = re.compile(r"^(\d+)([mhd])$")
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


@dataclass
class ScheduleDefinition:
    name: str
    prompt: str
    enabled: bool
    start_at: str
    start_at_ms: int
    period: str | None = None
    period_ms: int | None = None
    end_at: str | None = None
    end_at_ms: int | None = None
    session_id: str | None = None
    workspace: str | None = None


@dataclass
class ScheduleState:
    last_slot_ms: int | None = None
    status: str = "active"
    seen_ms: int | None = None


@dataclass
class Fire:
    project_id: str
    agent_id: str
    name: str
    prompt: str
    fire_ms: int
    session_id: str | None = None
    workspace: str | None = None


@dataclass
class ParseResult:
    ok: bool
    definition: ScheduleDefinition | None = None
    error: str = ""


def parse_period(raw: str) -> int | None:
    match = _PERIOD_RE.fullmatch(raw.strip())
    if not match:
        return None
    count = int(match.group(1))
    if count <= 0:
        return None
    unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[match.group(2)]
    return count * unit


def parse_instant(value: object) -> tuple[int, str] | None:
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000), stamp.isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000), value


def parse_schedule_file(name: str, raw: str) -> ParseResult:
    from witty_agent.tomlcompat import tomllib

    if not _NAME_RE.fullmatch(name):
        return ParseResult(False, error="schedule name 不合法")
    try:
        data = tomllib.loads(raw)
    except Exception as exc:
        return ParseResult(False, error=f"TOML 无效: {exc}")
    if not isinstance(data, dict):
        return ParseResult(False, error="Content is not a TOML table")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return ParseResult(False, error="Missing required field prompt")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        return ParseResult(False, error="enabled must be a boolean")
    start = parse_instant(data.get("start_at"))
    if start is None:
        return ParseResult(False, error="start_at is missing or not a valid ISO 8601 instant")
    period = None
    period_ms = None
    if "period" in data:
        if not isinstance(data["period"], str):
            return ParseResult(False, error="period must be a string")
        period_ms = parse_period(data["period"])
        if period_ms is None:
            return ParseResult(False, error="period must look like 30m / 12h / 7d")
        if period_ms < MIN_PERIOD_MS:
            return ParseResult(False, error="period is below the 5m minimum")
        period = data["period"].strip()
    end_at = None
    end_at_ms = None
    if "end_at" in data:
        parsed_end = parse_instant(data["end_at"])
        if parsed_end is None:
            return ParseResult(False, error="end_at is not a valid ISO 8601 instant")
        if parsed_end[0] <= start[0]:
            return ParseResult(False, error="end_at must be later than start_at")
        end_at, end_at_ms = parsed_end[1], parsed_end[0]
    session_id = data.get("session_id")
    workspace = data.get("workspace")
    if session_id is not None and not isinstance(session_id, str):
        return ParseResult(False, error="session_id must be a non-empty string")
    if workspace is not None and not isinstance(workspace, str):
        return ParseResult(False, error="workspace must be a non-empty string")
    if session_id is not None and not session_id.strip():
        return ParseResult(False, error="session_id must be a non-empty string")
    if workspace is not None and not workspace.strip():
        return ParseResult(False, error="workspace must be a non-empty string")
    return ParseResult(
        True,
        ScheduleDefinition(
            name=name,
            prompt=prompt,
            enabled=enabled,
            start_at=start[1],
            start_at_ms=start[0],
            period=period,
            period_ms=period_ms,
            end_at=end_at,
            end_at_ms=end_at_ms,
            session_id=session_id or None,
            workspace=workspace or None,
        ),
    )


def latest_slot_at(definition: ScheduleDefinition, now_ms: int) -> int | None:
    if now_ms < definition.start_at_ms:
        return None
    if definition.period_ms is None:
        return definition.start_at_ms
    steps = (now_ms - definition.start_at_ms) // definition.period_ms
    slot = definition.start_at_ms + steps * definition.period_ms
    if definition.end_at_ms is not None and slot > definition.end_at_ms:
        return None
    return slot


def format_fire_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def next_slot_at(
    definition: ScheduleDefinition,
    now_ms: int,
    *,
    last_slot_ms: int | None = None,
    status: str = "active",
) -> int | None:
    """下一次会打到的锚点。到期、一次性已打过、或已过 end_at 返回 None。"""
    if status in {"done", "missed", "invalid"}:
        return None
    if definition.end_at_ms is not None and now_ms > definition.end_at_ms:
        return None
    if now_ms < definition.start_at_ms:
        return definition.start_at_ms
    if definition.period_ms is None:
        if last_slot_ms is not None:
            return None
        return None
    latest = latest_slot_at(definition, now_ms)
    if latest is None:
        return None
    if last_slot_ms is not None and latest > last_slot_ms:
        return latest
    nxt = (last_slot_ms + definition.period_ms) if last_slot_ms is not None else latest + definition.period_ms
    if definition.end_at_ms is not None and nxt > definition.end_at_ms:
        return None
    return nxt


def scheduled_prompt(name: str, fire_ms: int, body: str) -> str:
    fire_time = datetime.fromtimestamp(fire_ms / 1000, tz=timezone.utc).isoformat()
    return get_prompt("scheduled_task", task_name=name, fire_time=fire_time, body=body)


def write_schedule(
    definition: ScheduleDefinition,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> Path:
    assert_id("project_id", project_id)
    assert_id("agent_id", agent_id)
    if not _NAME_RE.fullmatch(definition.name):
        raise ValueError(f"schedule name 不合法: {definition.name}")
    directory = schedule_dir(project_id, agent_id, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    lines = [
        f"prompt = {quote(definition.prompt)}",
        f"enabled = {'true' if definition.enabled else 'false'}",
        f"start_at = {quote(definition.start_at)}",
    ]
    if definition.period:
        lines.append(f'period = "{definition.period}"')
    if definition.end_at:
        lines.append(f'end_at = "{definition.end_at}"')
    if definition.session_id:
        lines.append(f'session_id = "{definition.session_id}"')
    if definition.workspace:
        lines.append(f'workspace = "{definition.workspace}"')
    path = directory / f"{definition.name}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def set_schedule_enabled(
    name: str,
    enabled: bool,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> ScheduleDefinition:
    """只改 enabled。暂停是停条件，不另起调度器。"""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"schedule name 不合法: {name}")
    path = schedule_dir(project_id, agent_id, root=root) / f"{name}.toml"
    if not path.is_file():
        raise FileNotFoundError(name)
    parsed = parse_schedule_file(name, path.read_text(encoding="utf-8"))
    if not parsed.ok or parsed.definition is None:
        raise ValueError(parsed.error or f"invalid schedule {name}")
    definition = parsed.definition
    definition.enabled = bool(enabled)
    write_schedule(definition, project_id, agent_id, root=root)
    logger.info(
        "定时任务%s project=%s agent=%s name=%s",
        "启用" if definition.enabled else "暂停",
        project_id,
        agent_id,
        name,
    )
    return definition


def delete_schedule(
    name: str,
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> bool:
    """删掉意图文件并忘掉运行状态。这是停条件，不是第二套调度器。"""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"schedule name 不合法: {name}")
    path = schedule_dir(project_id, agent_id, root=root) / f"{name}.toml"
    if not path.is_file():
        return False
    path.unlink()
    if root is not None:
        Scheduler(root).forget(project_id, agent_id, name)
    logger.info("删除定时任务 project=%s agent=%s name=%s", project_id, agent_id, name)
    return True


def list_schedule_files(project_id: str, agent_id: str, *, root: Path | None = None) -> list[ParseResult]:
    directory = schedule_dir(project_id, agent_id, root=root)
    if not directory.is_dir():
        return []
    rows: list[ParseResult] = []
    for path in sorted(directory.glob("*.toml")):
        rows.append(parse_schedule_file(path.stem, path.read_text(encoding="utf-8")))
    return rows


class Scheduler:
    def __init__(
        self,
        root: Path,
        *,
        now_ms: Callable[[], int] | None = None,
        runner: Callable[[Fire], Any] | None = None,
    ) -> None:
        self.root = root
        self.now_ms = now_ms or (lambda: int(time_ms()))
        self.runner = runner
        self._states: dict[str, ScheduleState] = {}
        self._load_states()

    def forget(self, project_id: str, agent_id: str, name: str) -> None:
        self._states.pop(f"{project_id}/{agent_id}/{name}", None)
        self._save_states()

    def arm(
        self,
        project_id: str,
        agent_id: str,
        name: str,
        *,
        seen_ms: int | None = None,
    ) -> None:
        """记成「已经看见」，这样下一次 tick 可以打当前锚点，而不是再空等一轮。"""
        key = f"{project_id}/{agent_id}/{name}"
        state = self._states.setdefault(key, ScheduleState())
        state.seen_ms = self.now_ms() if seen_ms is None else seen_ms
        state.status = "active"
        self._save_states()

    def task_status(self, project_id: str, agent_id: str, name: str) -> str:
        state = self._states.get(f"{project_id}/{agent_id}/{name}")
        return state.status if state is not None else "active"

    def task_last_slot_ms(self, project_id: str, agent_id: str, name: str) -> int | None:
        state = self._states.get(f"{project_id}/{agent_id}/{name}")
        return state.last_slot_ms if state is not None else None

    def next_fire_iso(
        self,
        project_id: str,
        agent_id: str,
        definition: ScheduleDefinition,
        *,
        now_ms: int | None = None,
    ) -> str | None:
        if not definition.enabled:
            return None
        now = self.now_ms() if now_ms is None else now_ms
        slot = next_slot_at(
            definition,
            now,
            last_slot_ms=self.task_last_slot_ms(project_id, agent_id, definition.name),
            status=self.task_status(project_id, agent_id, definition.name),
        )
        return format_fire_iso(slot) if slot is not None else None

    def tick(self) -> list[Fire]:
        now = self.now_ms()
        fires: list[Fire] = []
        for project_id, agent_id, parsed in self._iter_definitions():
            if not parsed.ok or parsed.definition is None:
                continue
            definition = parsed.definition
            if not definition.enabled:
                continue
            key = f"{project_id}/{agent_id}/{definition.name}"
            state = self._states.setdefault(key, ScheduleState())
            fire = self._consider(project_id, agent_id, definition, state, now)
            if fire is not None:
                fires.append(fire)
                if self.runner is not None:
                    self.runner(fire)
        self._save_states()
        return fires

    def _consider(
        self,
        project_id: str,
        agent_id: str,
        definition: ScheduleDefinition,
        state: ScheduleState,
        now: int,
    ) -> Fire | None:
        if state.status in {"done", "missed", "invalid"}:
            return None
        if state.seen_ms is None:
            state.seen_ms = now
            if definition.period_ms is None and definition.start_at_ms < now:
                state.status = "missed"
                return None
            if definition.period_ms is not None:
                state.last_slot_ms = latest_slot_at(definition, now)
            return None
        slot = latest_slot_at(definition, now)
        if slot is None:
            if definition.end_at_ms is not None and now > definition.end_at_ms:
                state.status = "done"
            return None
        if state.last_slot_ms is not None and slot <= state.last_slot_ms:
            return None
        if slot < state.seen_ms and definition.period_ms is None:
            state.status = "missed"
            return None
        state.last_slot_ms = slot
        if definition.period_ms is None:
            state.status = "done"
        prompt = scheduled_prompt(definition.name, slot, definition.prompt)
        logger.info("定时触发 project=%s agent=%s name=%s", project_id, agent_id, definition.name)
        return Fire(
            project_id=project_id,
            agent_id=agent_id,
            name=definition.name,
            prompt=prompt,
            fire_ms=slot,
            session_id=definition.session_id,
            workspace=definition.workspace,
        )

    def _iter_definitions(self):
        if not self.root.is_dir():
            return
        for project in sorted(self.root.iterdir()):
            agents = project / "agents"
            if not agents.is_dir():
                continue
            for agent in sorted(agents.iterdir()):
                if not agent.is_dir():
                    continue
                for parsed in list_schedule_files(project.name, agent.name, root=self.root):
                    yield project.name, agent.name, parsed

    def _state_path(self) -> Path:
        return self.root / ".schedule_state.json"

    def _load_states(self) -> None:
        path = self._state_path()
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, row in (data.get("tasks") or {}).items():
            self._states[key] = ScheduleState(
                last_slot_ms=row.get("last_slot_ms"),
                status=str(row.get("status") or "active"),
                seen_ms=row.get("seen_ms"),
            )

    def _save_states(self) -> None:
        path = self._state_path()
        payload = {"tasks": {key: asdict(value) for key, value in self._states.items()}}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def time_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
