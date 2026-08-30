"""/refine：复盘本会话轨迹，把验证过的经验沉淀成资产。

一次沉淀 = 一次无工具的模型调用（复盘员），产出三类资产：

- **role**：进这个 Agent 的 `agent_state/AGENTS.md`，每轮系统提示都带（有 2000 字帽）；
- **memory**：进工作区记忆格（跟证伪账本、义务台账同一个键法：经验锚在它产生的那棵树上）；
- **skill**：进用户技能目录，落成 SKILL.md 草稿。

自进化最大的坑不是「学不到」，是**把错的学进去**（misevolution）：一条幻觉出来的「经验」
写进系统提示，之后每一轮都在放大它。所以每道闸都是机械可查的，不靠模型自觉：

1. **证据必须是轨迹原文**。复盘员每条产出都要带 evidence，白名单化验证：规整空白后必须
   逐字出现在轨迹里，对不上就丢弃。模型编不出一段轨迹里没有的原文。
2. **义务台账红着就不沉淀**。这个工作区验过的完成判据（gate）现在跑不过，说明这条轨迹
   收在一个坏状态上——从失败里提炼「什么做对了」是最典型的错学。
3. **role 有总量帽**。`agent_state/AGENTS.md` 超过注入帽就拒绝追加，宁可不学也不能让
   系统提示无界膨胀（膨胀的另一头是把真正的指引挤出截断线）。
4. **先快照后落笔**，applied 为零就撤快照。`/refine undo` 一条命令回到沉淀之前——
   沉淀错误经验要能快速回滚。

复盘员的提示词在 `config/prompts.toml`（refine_system / refine_user），这里只有机械部分。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from witty_agent.commands import CommandResult
from witty_agent.layout import criteria_dir, snapshots_dir
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.runtime import refine_settings
from witty_agent.state.agent_state import ROLE_MAX_CHARS, AgentRecord, bump_version

logger = get_logger("refine")

_KINDS = frozenset({"role", "memory", "skill"})
# 证据下限（规整空白后的字符数）。太短的片段（"done"、"ok"）在任何轨迹里都能撞上，
# 等于没验；6 个字符起步才开始有区分度。
_EVIDENCE_MIN_CHARS = 6
_MARKER_NAME = "refine_last.json"
_ITEM_HEAD = "== item"


@dataclass
class RefineItem:
    kind: str = ""
    title: str = ""
    name: str = ""
    evidence: str = ""
    body: str = ""


@dataclass
class RefinePlan:
    items: list[RefineItem] = field(default_factory=list)
    dropped: list[tuple[RefineItem, str]] = field(default_factory=list)
    nothing: bool = False
    parsed: bool = True


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _strip_fences(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        lines = body.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines)
    return body.strip()


def parse_refine_reply(reply: str, transcript: str) -> RefinePlan:
    """把复盘员的回复切成条目，证据对不上轨迹的当场丢弃。"""
    body = _strip_fences(reply)
    if not body:
        return RefinePlan(parsed=False)
    if _norm(body).upper() == "NOTHING":
        return RefinePlan(nothing=True)
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if line.strip() == _ITEM_HEAD:
            current = []
            blocks.append(current)
            continue
        if current is not None:
            current.append(line)
    if not blocks:
        return RefinePlan(parsed=False)
    plan = RefinePlan()
    haystack = _norm(transcript)
    for block in blocks:
        item = _parse_block(block)
        reason = _screen(item, haystack)
        if reason:
            plan.dropped.append((item, reason))
        else:
            plan.items.append(item)
    return plan


def _parse_block(lines: list[str]) -> RefineItem:
    item = RefineItem()
    body_lines: list[str] | None = None
    for line in lines:
        if body_lines is not None:
            body_lines.append(line)
            continue
        if line.strip() == "body:":
            body_lines = []
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        name = key.strip().lower()
        if name in {"kind", "title", "name", "evidence"}:
            setattr(item, name, value.strip())
    item.body = "\n".join(body_lines or []).strip()
    return item


def _screen(item: RefineItem, haystack: str) -> str:
    """返回丢弃原因，空串表示留下。每条规则都是机械判定，不请模型复核。"""
    if item.kind not in _KINDS:
        return get_prompt("refine_skip_kind")
    if not item.body:
        return get_prompt("refine_skip_body")
    evidence = _norm(item.evidence)
    if len(evidence) < _EVIDENCE_MIN_CHARS or evidence not in haystack:
        return get_prompt("refine_skip_evidence")
    if item.kind in {"memory", "skill"} and not item.name:
        return get_prompt("refine_skip_slug")
    return ""


# ---------------------------------------------------------------- 落盘


def _marker_path(record: AgentRecord, *, root: Path | None) -> Path:
    return snapshots_dir(record.project_id, record.agent_id, root=root) / _MARKER_NAME


def _apply_role(item: RefineItem, record: AgentRecord) -> str:
    path = record.state_dir / "AGENTS.md"
    current = ""
    if path.is_file():
        current = path.read_text(encoding="utf-8").strip()
    # 还是种子脚手架的话直接换掉：种子文案本身不注入（agent_role_text 会跳过），
    # 留着它一起注入反而把脚手架说明灌进每轮系统提示。
    if current == get_prompt("agent_role_seed").strip():
        current = ""
    section = f"## {item.title}\n{item.body}".strip()
    merged = f"{current}\n\n{section}\n" if current else f"{section}\n"
    if len(merged.strip()) > ROLE_MAX_CHARS:
        raise ValueError(get_prompt("refine_skip_role_cap", max_chars=str(ROLE_MAX_CHARS)))
    path.write_text(merged, encoding="utf-8")
    return str(path)


def _apply_memory(item: RefineItem, record: AgentRecord, workspace_dir: Path, *, root: Path | None) -> str:
    from witty_agent.layout import memory_workspace_dir
    from witty_agent.memory import workspace_memory_key, write_topic

    directory = memory_workspace_dir(
        workspace_memory_key(workspace_dir),
        record.project_id,
        record.agent_id,
        root=root,
    )
    path = write_topic(directory, item.name, description=item.title, body=item.body)
    return str(path)


def _apply_skill(item: RefineItem, record: AgentRecord, *, root: Path | None) -> str:
    from witty_agent.skills import install_user_skill

    markdown = get_prompt(
        "refine_skill_markdown",
        skill=item.name,
        description=" ".join(item.title.split()).replace(":", "："),
        body=item.body,
    )
    meta = install_user_skill(
        text=markdown,
        project_id=record.project_id,
        agent_id=record.agent_id,
        root=root,
        overwrite=False,
    )
    return str(meta.path)


def apply_refinements(
    plan: RefinePlan,
    *,
    record: AgentRecord,
    workspace_dir: Path,
    root: Path | None = None,
    session_id: str = "",
) -> CommandResult:
    """先快照后落笔。全部条目都没落下去就撤掉快照，不留一个「回滚到原地」的假档。"""
    from witty_agent.evolution.snapshot import save_snapshot, snapshot_path

    limit = int(refine_settings()["max_items"])
    before_version = record.version
    tarball = snapshot_path(record, root=root)
    # save_snapshot 对已存在的快照直接返回。refine 的快照必须反映「这次沉淀之前」的状态，
    # 旧档挡路就重打——否则 undo 会把状态拉回一个更早的、已经不对的时间点。
    tarball.unlink(missing_ok=True)
    save_snapshot(record, root=root)
    applied: list[str] = []
    skipped: list[str] = [
        get_prompt("refine_skipped_line", kind=item.kind or "?", title=item.title or "-", reason=reason)
        for item, reason in plan.dropped
    ]
    count = 0
    for item in plan.items:
        if count >= limit:
            skipped.append(
                get_prompt("refine_skipped_line", kind=item.kind, title=item.title, reason=get_prompt("refine_skip_limit", limit=str(limit)))
            )
            continue
        try:
            if item.kind == "role":
                path = _apply_role(item, record)
            elif item.kind == "memory":
                path = _apply_memory(item, record, workspace_dir, root=root)
            else:
                path = _apply_skill(item, record, root=root)
        except (ValueError, FileExistsError) as exc:
            skipped.append(get_prompt("refine_skipped_line", kind=item.kind, title=item.title, reason=str(exc)))
            continue
        applied.append(get_prompt("refine_applied_line", kind=item.kind, title=item.title, path=path))
        count += 1
    if not applied:
        tarball.unlink(missing_ok=True)
        lines = "\n".join(skipped) or "-"
        return CommandResult(kind="error", text=get_prompt("refine_all_skipped", skipped=lines))
    bump_version(record)
    marker = _marker_path(record, root=root)
    marker.write_text(
        json.dumps(
            {"version": before_version, "session": session_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info("沉淀完成 applied=%s skipped=%s version=%s", len(applied), len(skipped), record.version)
    return CommandResult(
        kind="success",
        text=get_prompt(
            "refine_done",
            applied="\n".join(applied),
            skipped=("\n".join(skipped) or "-"),
            version=str(before_version),
        ),
    )


def undo_refine(record: AgentRecord, *, root: Path | None = None) -> CommandResult:
    from witty_agent.evolution.snapshot import restore_snapshot

    marker = _marker_path(record, root=root)
    if not marker.is_file():
        return CommandResult(kind="error", text=get_prompt("refine_undo_none"))
    try:
        version = int(json.loads(marker.read_text(encoding="utf-8")).get("version") or 0)
    except ValueError:
        version = 0
    if version <= 0:
        marker.unlink(missing_ok=True)
        return CommandResult(kind="error", text=get_prompt("refine_undo_none"))
    try:
        restore_snapshot(record, version, root=root)
    except FileNotFoundError:
        marker.unlink(missing_ok=True)
        return CommandResult(kind="error", text=get_prompt("refine_undo_none"))
    marker.unlink(missing_ok=True)
    logger.info("撤销沉淀 version=%s", version)
    return CommandResult(kind="success", text=get_prompt("refine_undo_ok", version=str(version)))


# ---------------------------------------------------------------- 编排


def _red_obligations(record: AgentRecord, workspace_dir: Path, *, root: Path | None) -> list[str]:
    """义务台账里验过的判据，现在还跑得过吗？跑不过就不配沉淀。"""
    from witty_agent.memory import workspace_memory_key
    from witty_agent.verify import GateRunner, ObligationLedger

    ledger = ObligationLedger(
        criteria_dir(
            workspace_memory_key(workspace_dir),
            record.project_id,
            record.agent_id,
            root=root,
        )
    )
    obligations = ledger.load()
    if not obligations:
        return []
    report = GateRunner(workspace=Path(workspace_dir)).run([item.spec() for item in obligations])
    return [item.line() for item in report.failures()]


async def run_refine(
    stream_fn,
    *,
    model,
    record: AgentRecord,
    workspace_dir: Path,
    history,
    note: str = "",
    root: Path | None = None,
    session_id: str = "",
) -> CommandResult:
    from witty_agent.goal import render_transcript
    from witty_agent.types import AgentContext, AgentMessage

    settings = refine_settings()
    if not settings["enabled"]:
        return CommandResult(kind="error", text=get_prompt("refine_disabled"))
    if not any(m.role == "assistant" and (m.text() or m.tool_calls()) for m in history):
        return CommandResult(kind="error", text=get_prompt("refine_empty"))
    if settings["run_gates"]:
        red = _red_obligations(record, workspace_dir, root=root)
        if red:
            return CommandResult(kind="error", text=get_prompt("refine_gate_red", failures="\n".join(red)))
    transcript = render_transcript(history, limit=int(settings["transcript_chars"]))
    context = AgentContext(
        system_prompt=get_prompt("refine_system"),
        messages=[
            AgentMessage(
                role="user",
                content=get_prompt("refine_user", transcript=transcript, note=note or "-"),
            )
        ],
        tools=[],
        workspace_dir=str(workspace_dir),
        model=model,
        project_id=record.project_id,
        agent_id=record.agent_id,
        session_id=session_id,
    )
    try:
        reply = await stream_fn(context)
    except Exception as exc:  # noqa: BLE001 - 复盘失败不该把会话弄崩
        logger.warning("复盘员调用失败 err=%s", exc)
        return CommandResult(kind="error", text=get_prompt("refine_unparsed"))
    if reply.stop_reason == "error":
        return CommandResult(kind="error", text=get_prompt("refine_unparsed"))
    plan = parse_refine_reply(reply.text(), transcript)
    if plan.nothing:
        return CommandResult(kind="success", text=get_prompt("refine_nothing"))
    if not plan.parsed:
        return CommandResult(kind="error", text=get_prompt("refine_unparsed"))
    if not plan.items and plan.dropped:
        lines = "\n".join(
            get_prompt("refine_skipped_line", kind=item.kind or "?", title=item.title or "-", reason=reason)
            for item, reason in plan.dropped
        )
        return CommandResult(kind="error", text=get_prompt("refine_all_skipped", skipped=lines))
    if not plan.items:
        return CommandResult(kind="error", text=get_prompt("refine_unparsed"))
    return apply_refinements(
        plan,
        record=record,
        workspace_dir=Path(workspace_dir),
        root=root,
        session_id=session_id,
    )
