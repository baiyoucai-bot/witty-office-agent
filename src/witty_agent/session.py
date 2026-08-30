"""Session 与 Agent 创建。一条会话一个 workspace、一条轨迹。"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from witty_agent.tools.command import CommandSessionManager

from witty_agent import hooks
from witty_agent.approval import ApprovalMode, ApproveFn
from witty_agent.capability import CapabilityRegistry
from witty_agent.catalog import bind_catalog, load_catalog, reset_catalog
from witty_agent.skills import bind_skill_scope, reset_skill_scope
from witty_agent.commands import CommandRegistry, CommandResult
from witty_agent.compaction import (
    CompactionBusy,
    CompactionLock,
    compact_messages_async,
    compact_now as force_compact,
    compact_region,
    compact_region_async,
    parse_compact_range,
    prune_tool_call_args,
    prune_tool_results,
    settings_from_runtime,
)
from witty_agent.dispatch import allocation_hint
from witty_agent.guard import (
    AskGate,
    EvidenceGate,
    AnswerNowReminder,
    FailStrategyReminder,
    PlanPresentGate,
    ProgressGuard,
    RepeatToolReminder,
    TodoGate,
    questions_from_assistant_text,
    recalled_answer_hint,
    recalled_relocations,
    recalled_verify_hint,
    recalled_verify_paths,
    relevant_browse_rows,
    autoload_recalled_verify,
    autoload_browse_read,
    browse_read_hits,
    needs_memory_browse,
)
from witty_agent.invariants import run_invariants
from witty_agent.kernel import apply_kernel_update
from witty_agent.layout import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    criteria_dir,
    data_root,
    scratchpad_dir,
    traces_dir,
)
from witty_agent.logging import get_logger, set_trace_id
from witty_agent.loop import LoopConfig, LoopResult, run_agent_loop
from witty_agent.mcp import load_mcp_tools
from witty_agent.memory import (
    SessionMemory,
    apply_relocated_hits,
    attach_retrieval,
    resolve_session_memory,
    rewrite_relocated_paths,
)
from witty_agent.plan_mode import (
    MUTATING_TOOLS,
    PlanModeController,
    blocks_tool,
    maybe_auto_enter,
    plan_steps,
)
from witty_agent.projection import project_session
from witty_agent.prompts import get_prompt
from witty_agent.runtime import compaction_settings, goal_settings, loop_settings
from witty_agent.session_log import SessionLog, repair_session_log, result_message_from_repair
from witty_agent.file_reference import file_reference_hint
from witty_agent.instruction_refresh import (
    fold_instruction_seen,
    instruction_additional_hints,
    instruction_baseline_identity,
    instruction_baseline_message,
    instruction_offline_transitions,
    instruction_rearm_after_compact,
    instruction_reconcile_seen,
    instruction_update_hint,
    remember_instruction_path,
    resolved_instruction_key,
    seed_instruction_seen,
    visible_baseline_identity,
    visible_instruction_baseline,
)
from witty_agent.session_reference import session_reference_hint
from witty_agent.session_tree import fork_session
from witty_agent.state.agent_state import (
    AgentRecord,
    agent_role_text,
    init_agent_state,
    load_agent_state,
)
from witty_agent.state.project import ProjectConfig, init_project, list_agents
from witty_agent.store import (
    append_event,
    append_message,
    append_session_event,
    append_usage,
    load_messages,
    load_session_events,
    session_path,
    write_header,
)
from witty_agent.system_prompt import build_system_prompt
from witty_agent.spill import apply_spill
from witty_agent.host_context import maybe_inject as maybe_inject_host
from witty_agent.time_context import maybe_inject
from witty_agent.todo import apply_todo_write, current_todos, format_todo_section, has_open_todos
from witty_agent.tool_surface import needs_skill_tools, select_advertised_names
from witty_agent.trace import attach_turn_evidence
from witty_agent.tools import list_tools
from witty_agent.skill_guard import allowlist_for_skills, tool_permitted
from witty_agent.tools.skill import inject_skill_bodies, skill_names_for_turn
from witty_agent.tools.fs import bind_workspace
from witty_agent.types import AgentContext, AgentEvent, AgentMessage, ModelRef, ToolCallBlock
from witty_agent.verify import GateSpec
from witty_agent.user_questions import UserQuestionService
from witty_agent.vault import bind_vault, load_vault

if TYPE_CHECKING:
    from witty_agent.goal import JudgeFn

logger = get_logger("session")
StreamFn = Callable[[AgentContext], Awaitable[AgentMessage]]


def _tool_touch_paths(call: ToolCallBlock) -> list[str]:
    if call.name == "apply_patch":
        from witty_agent.patch_text import apply_patch_paths

        return apply_patch_paths(str((call.arguments or {}).get("patch") or ""))
    raw = str((call.arguments or {}).get("path") or "")
    return [raw] if raw.strip() else []


@dataclass
class WittyAgent:
    project: ProjectConfig
    record: AgentRecord
    root: Path


@dataclass
class Session:
    agent: WittyAgent
    session_id: str
    workspace_dir: Path
    model: ModelRef
    parent_id: str | None = None
    scratchpad: Path | None = None
    title: str = ""
    commands: CommandSessionManager = field(default_factory=CommandSessionManager)
    log: SessionLog = field(default_factory=SessionLog)
    plan: PlanModeController = field(default_factory=PlanModeController)
    questions: UserQuestionService = field(default_factory=UserQuestionService)
    capabilities: CapabilityRegistry = field(default_factory=CapabilityRegistry)
    command_registry: CommandRegistry = field(default_factory=CommandRegistry)
    _steering: list[AgentMessage] = field(default_factory=list)
    _aborted: bool = False
    _run_gen: int = 0
    _persisted_seq: int = 0
    _hydrated: bool = False
    _run_active: bool = False
    _compact_lock: CompactionLock = field(default_factory=CompactionLock)
    _pending_checkpoint: list[AgentMessage] | None = None
    _instruction_seen: dict[str, str] = field(default_factory=dict)
    _instruction_versions: dict[str, dict[str, str]] = field(default_factory=dict)
    _profile_frozen: str | None = None
    _last_memory_query: str = ""
    _last_memory_hits: tuple = ()
    _last_memory_retrieved: str = ""
    # concurrent.futures.Future，不是 asyncio.Task：它要跨轮，而每轮是独立的 asyncio.run
    _harvest_pending: object | None = None
    _gc_swept: bool = False

    def steer(self, text: str) -> None:
        note = (text or "").strip()
        if not note:
            return
        self._steering.append(AgentMessage(role="user", content=note))

    def abort(self) -> None:
        self._aborted = True
        self._run_gen += 1

    def slash_commands(self) -> list[dict[str, object]]:
        self._ensure_commands()
        return self.command_registry.public_items()

    def dispatch_command(self, raw: str) -> CommandResult | None:
        self._ensure_commands()
        result = self.command_registry.dispatch(raw)
        if result is not None:
            self._hydrate_log()
            self.log.append(
                "command/run",
                {"name": (self.command_registry.parse(raw) or ("", ""))[0], "input": raw},
            )
        return result

    def project(self) -> dict:
        self._hydrate_log()
        state = self.plan.get(self.log)
        return project_session(self.log, plan_pending=state.pending)

    async def run(
        self,
        prompt: str,
        *,
        stream_fn: StreamFn,
        approve: ApproveFn | None = None,
        approval_mode: ApprovalMode | None = None,
        tool_execution: str | None = None,
        ask_user: object | None = None,
        emit: Callable[[AgentEvent], Awaitable[None] | None] | None = None,
    ) -> LoopResult:
        set_trace_id(self.session_id)
        bind_workspace(str(self.workspace_dir), self.session_id)
        run_gen = self._run_gen
        self._aborted = False
        self._ensure_commands()
        parsed = self.command_registry.parse(prompt)
        # /compact 与 /refine 要调模型，同步的命令表装不下，跟 /compact 一样在这里拿着
        # stream_fn 走异步特例；命令表里的同步版只是给直接 dispatch_command 的调用方兜底。
        if parsed is not None and parsed[0] in {"compact", "refine"}:
            self._hydrate_log()
            self.log.append("command/run", {"name": parsed[0], "input": prompt})
            if parsed[0] == "compact":
                command = await self.compact_now_async(stream_fn=stream_fn, rest=parsed[1])
            else:
                command = await self.refine_now_async(stream_fn=stream_fn, rest=parsed[1])
            notice = AgentMessage(role="assistant", content=command.text, stop_reason="end_turn")
            self.log.append("user/message", {"text": prompt, "source": "user"})
            self.log.append("assistant/message", {"text": command.text, "tool_calls": []})
            result = LoopResult(messages=[notice])
            self._persist(AgentMessage(role="user", content=prompt), result)
            return result
        command = self.dispatch_command(prompt)
        if command is not None and not command.remainder:
            notice = AgentMessage(role="assistant", content=command.text, stop_reason="end_turn")
            self.log.append("user/message", {"text": prompt, "source": "user"})
            self.log.append("assistant/message", {"text": command.text, "tool_calls": []})
            result = LoopResult(messages=[notice])
            self._persist(AgentMessage(role="user", content=prompt), result)
            return result
        if command is not None and command.remainder:
            prompt = command.remainder
        pad = self._ensure_scratchpad()
        os.environ["WITTY_SCRATCHPAD"] = str(pad)
        os.environ["WITTY_SESSION_ID"] = self.session_id
        vault = load_vault(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            root=self.agent.root,
        )
        bind_vault(vault)
        self._hydrate_log()
        self._bind_capabilities()
        if not self._instruction_seen:
            self._instruction_seen.update(seed_instruction_seen(str(self.workspace_dir)))
        if ask_user is not None:
            self.questions.register_provider(ask_user)  # type: ignore[arg-type]
        hooks.bind(
            stream_fn=stream_fn,
            approve=approve,
            project_id=self.agent.project.project_id,
            workspace=str(self.workspace_dir),
            root=self.agent.root,
            agent_id=self.agent.record.agent_id,
            commands=self.commands,
            session_log_obj=self.log,
            plan_mode_obj=self.plan,
            user_questions_obj=self.questions,
            capabilities_obj=self.capabilities,
        )
        memory = resolve_session_memory(
            project_id=self.agent.project.project_id,
            agent_id=self.agent.record.agent_id,
            workspace=self.workspace_dir,
            root=self.agent.root,
        )
        os.environ["WITTY_MEMORY_USER"] = str(memory.user_dir)
        if memory.workspace_dir is not None:
            os.environ["WITTY_MEMORY_WORKSPACE"] = str(memory.workspace_dir)
        catalog = load_catalog(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            root=self.agent.root,
        )
        catalog_token = bind_catalog(catalog)
        skill_token = bind_skill_scope(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            self.agent.root,
        )
        self._run_active = True
        try:
            return await self._run_bound(
                prompt,
                stream_fn=stream_fn,
                approve=approve,
                approval_mode=approval_mode,
                tool_execution=tool_execution,
                ask_user=ask_user,
                emit=emit,
                catalog=catalog,
                run_gen=run_gen,
            )
        finally:
            self._run_active = False
            reset_skill_scope(skill_token)
            reset_catalog(catalog_token)

    async def _run_bound(
        self,
        prompt: str,
        *,
        stream_fn: StreamFn,
        approve: ApproveFn | None,
        approval_mode: ApprovalMode | None,
        tool_execution: str | None,
        ask_user: object | None,
        emit: Callable[[AgentEvent], Awaitable[None] | None] | None,
        catalog,
        run_gen: int,
    ) -> LoopResult:
        del ask_user
        vault = load_vault(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            root=self.agent.root,
        )
        # 本 agent 自己的角色（state/AGENTS.md）。整轮不变，读一次。
        agent_role = agent_role_text(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            root=self.agent.root,
        )
        reminder = RepeatToolReminder(workspace=str(self.workspace_dir))
        fail_strategy = FailStrategyReminder()
        answer_now = AnswerNowReminder()
        progress = ProgressGuard()
        evidence_gate = EvidenceGate()
        ask_gate = AskGate()
        todo_gate = TodoGate()
        plan_gate = PlanPresentGate()
        mode = approval_mode or self.agent.project.approval_mode or self.agent.record.approval_mode
        loop_cfg = loop_settings()
        compact_opts = settings_from_runtime(compaction_settings())
        auto_notice = maybe_auto_enter(
            self.plan,
            self.log,
            prompt,
            enabled=bool(loop_cfg.get("auto_plan", True)),
        )
        if auto_notice is not None:
            logger.info("自动进入计划模式")
            self.log.append("turn/auto-plan", {})
        tools = [item for item in list_tools() if catalog.tool_enabled(item.name)]
        tools.extend(load_mcp_tools())
        history = load_messages(self._store_path())
        base_memory = resolve_session_memory(
            project_id=self.agent.project.project_id,
            agent_id=self.agent.record.agent_id,
            workspace=self.workspace_dir,
            root=self.agent.root,
        )
        from witty_agent.memory import topic_switched

        # 上一轮推迟给判官的句子必须先落盘再检索，否则这一轮召回的是判官落地之前的九宫格。
        await self._settle_harvest()
        if (
            self._last_memory_query
            and self._last_memory_hits
            and not topic_switched(self._last_memory_query, prompt)
        ):
            memory = replace(
                base_memory,
                hits=self._last_memory_hits,
                retrieved=self._last_memory_retrieved,
            )
        else:
            memory = attach_retrieval(base_memory, prompt)
            self._last_memory_query = prompt
            self._last_memory_hits = memory.hits
            self._last_memory_retrieved = memory.retrieved
        from witty_agent.system_prompt import is_placeholder_profile

        if self._profile_frozen is None:
            if not is_placeholder_profile(memory.profile):
                self._profile_frozen = memory.profile
        else:
            memory = replace(memory, profile=self._profile_frozen)
        advertised = select_advertised_names(
            prompt,
            [getattr(item, "name", "") for item in tools],
            plan_active=self.plan.get(self.log).active,
            used_names=_used_tool_names(history) + _spill_tool_names(history),
            prior_text=_prior_user_text(history),
            enabled=bool(loop_cfg.get("thin_tools", True)),
            memory_empty=memory.empty,
            memory_hits=memory.hits,
        )
        active_skills = skill_names_for_turn(
            [AgentMessage(role="user", content=prompt)],
            prompt,
            reserved=set(self.command_registry.names()),
            auto=bool(loop_cfg.get("auto_skill", True)),
            min_score=int(loop_cfg.get("auto_skill_min_score") or 4),
            limit=int(loop_cfg.get("auto_skill_limit") or 1),
            plan_active=self.plan.get(self.log).active,
        )
        skill_allow = allowlist_for_skills(active_skills)
        if skill_allow is not None:
            advertised = [name for name in advertised if tool_permitted(name, skill_allow)]
            logger.info("技能收权 skills=%s allow=%s", active_skills, sorted(skill_allow))
        shown = {name for name in advertised}
        pending_baseline = None
        pending_offline: list = []
        workspace = str(self.workspace_dir)
        if not visible_instruction_baseline(history):
            pending_baseline = instruction_baseline_message(workspace)
        elif visible_baseline_identity(history) != instruction_baseline_identity(workspace):
            pending_baseline = instruction_baseline_message(workspace, replace=True)
        if pending_baseline is not None:
            self._instruction_seen.clear()
            self._instruction_seen.update(seed_instruction_seen(workspace))
        else:
            pending_offline = instruction_offline_transitions(
                workspace,
                self._instruction_seen,
                versions=self._instruction_versions,
            )
        skip_project = pending_baseline is not None or visible_instruction_baseline(history)
        skill_query = "\n".join(part for part in (_prior_user_text(history), prompt) if part)
        system = build_system_prompt(
            self.workspace_dir,
            tool_names=advertised,
            memory=memory,
            vault_keys=list(vault),
            plan_section=self.plan.policy_text(self.log),
            commands_section=self.command_registry.catalog_text(),
            todo_section=format_todo_section(current_todos(self.log)),
            list_snippets=not bool(loop_cfg.get("thin_tools", True)),
            prompt=prompt,
            plan_active=self.plan.get(self.log).active,
            context_files=[] if skip_project else None,
            skill_query=skill_query,
            agent_role=agent_role,
        )
        prior_history = history
        history = await self._compact_locked(
            history,
            compact_opts,
            stream_fn=stream_fn,
        )
        if history is not prior_history:
            self.log.append(
                "compaction/result",
                {"before": len(prior_history), "after": len(history)},
            )
            rearmed = instruction_rearm_after_compact(
                prior_history,
                history,
                str(self.workspace_dir),
                self._instruction_seen,
            )
            if rearmed:
                history.extend(rearmed)
                self.log.append(
                    "turn/instruction-additional",
                    {"path": "", "count": len(rearmed), "digests": {}, "rearm": True},
                )
            self._write_compaction_checkpoint(history)
            history.extend(self._after_compact_notices(history, memory))
        context = AgentContext(
            system_prompt=system,
            messages=list(history),
            tools=tools,
            workspace_dir=str(self.workspace_dir),
            model=self.model,
            project_id=self.agent.project.project_id,
            agent_id=self.agent.record.agent_id,
            session_id=self.session_id,
        )
        user = AgentMessage(role="user", content=prompt)

        async def drain_steering() -> list[AgentMessage]:
            items = list(self._steering)
            self._steering.clear()
            if items:
                reminder.reset()
                fail_strategy.reset()
                answer_now.reset()
                progress.reset()
                evidence_gate.reset()
                ask_gate.reset()
                todo_gate.reset()
                plan_gate.reset()
            return items

        async def aborted() -> bool:
            return self._run_gen != run_gen

        async def should_stop_after_turn(
            assistant: AgentMessage, new_messages: list[AgentMessage]
        ) -> bool:
            notice = progress.observe_turn(assistant, new_messages)
            if notice is None:
                notice = reminder.stop_notice()
            if notice is None:
                return False
            new_messages.append(notice)
            if str(notice.source or "") == "plugin:repeat-tool-stop":
                logger.info("重复相同工具停轮 count=%s name=%s", reminder._count, reminder._name)
                self.log.append("turn/repeat-stop", {"count": reminder._count, "tool": reminder._name})
            else:
                logger.info("连续工具失败停轮 count=%s", progress._errors)
                self.log.append("turn/stall", {"count": progress._errors})
            return True

        skill_injected = {"done": False}
        dispatch_injected = {"done": False}
        auto_plan_injected = {"done": False}
        reference_injected = {"done": False}

        async def pre_step() -> list[AgentMessage]:
            nonlocal memory, pending_baseline, pending_offline
            extras: list[AgentMessage] = []
            extras.extend(self.plan.apply_pre_step(self.log))
            if pending_baseline is not None:
                extras.append(pending_baseline)
                self.log.append(
                    "turn/instruction-baseline",
                    {
                        "digest": (pending_baseline.meta or {}).get("digest") or "",
                        "digests": dict(self._instruction_seen),
                    },
                )
                pending_baseline = None
            if pending_offline:
                extras.extend(pending_offline)
                added = {
                    str((item.meta or {}).get("path") or ""): (item.meta or {}).get("digest") or ""
                    for item in pending_offline
                    if (item.meta or {}).get("action") == "set"
                }
                if added:
                    self.log.append(
                        "turn/instruction-additional",
                        {"path": "", "count": len(added), "digests": added},
                    )
                for item in pending_offline:
                    if (item.meta or {}).get("action") != "set":
                        self.log.append(
                            "turn/instruction-update",
                            {
                                "path": (item.meta or {}).get("path") or "",
                                "digest": (item.meta or {}).get("digest") or "",
                            },
                        )
                pending_offline = []
            if not reference_injected["done"]:
                referenced = session_reference_hint(
                    prompt,
                    self_id=self.session_id,
                    directory=self._store_path().parent,
                )
                if referenced is not None:
                    extras.append(referenced)
                    self.log.append("turn/session-reference", {"source": referenced.source})
                attached = file_reference_hint(prompt, workspace=str(self.workspace_dir))
                if attached is not None:
                    extras.append(attached)
                    self.log.append("turn/file-reference", {"source": attached.source})
                reference_injected["done"] = True
            if auto_notice is not None and not auto_plan_injected["done"]:
                extras.append(auto_notice)
                auto_plan_injected["done"] = True
            if not skill_injected["done"]:
                names = skill_names_for_turn(
                    [user],
                    prompt,
                    reserved=set(self.command_registry.names()),
                    auto=bool(loop_cfg.get("auto_skill", True)),
                    min_score=int(loop_cfg.get("auto_skill_min_score") or 4),
                    limit=int(loop_cfg.get("auto_skill_limit") or 1),
                    plan_active=self.plan.get(self.log).active,
                )
                extras.extend(inject_skill_bodies(names))
                if names:
                    self.log.append("turn/skill-match", {"names": names})
                    skill_injected["done"] = True
                elif not self.plan.get(self.log).active:
                    skill_injected["done"] = True
            if not dispatch_injected["done"]:
                recalled = recalled_answer_hint(prompt, memory.hits)
                if recalled is not None:
                    extras.append(recalled)
                    self.log.append("turn/recalled-answer", {"source": recalled.source})
                else:
                    verify = recalled_verify_hint(prompt, memory.hits)
                    if verify is not None:
                        paths = recalled_verify_paths(prompt, memory.hits)
                        loaded = autoload_recalled_verify(paths)
                        if loaded:
                            extras.extend(loaded)
                            moved = recalled_relocations(loaded)
                            if moved:
                                apply_relocated_hits(memory.hits, moved)
                                if memory.workspace_dir is not None:
                                    rewrite_relocated_paths(memory.workspace_dir, moved)
                            self.log.append(
                                "turn/recalled-verify",
                                {
                                    "source": "plugin:recalled-verify-read",
                                    "paths": list(paths),
                                    "relocated": [{"from": src, "to": dest} for src, dest in moved],
                                },
                            )
                        else:
                            extras.append(verify)
                            self.log.append("turn/recalled-verify", {"source": verify.source})
                    else:
                        browse_rows = (
                            relevant_browse_rows(prompt, memory.empty)
                            if needs_memory_browse(prompt, memory.empty)
                            else []
                        )
                        browsed = autoload_browse_read(browse_rows, prompt=prompt)
                        if browsed:
                            extras.extend(browsed)
                            loaded = browse_read_hits(browsed)
                            if loaded:
                                memory = replace(
                                    memory,
                                    hits=tuple((*memory.hits, *loaded)),
                                )
                            self.log.append(
                                "turn/browse-read",
                                {
                                    "source": "plugin:browse-read",
                                    "slugs": [row["slug"] for row in browse_rows],
                                    "loaded": [str(hit.get("slug") or "") for hit in loaded],
                                },
                            )
                        else:
                            hint = allocation_hint(prompt)
                            if hint is not None:
                                extras.append(hint)
                                self.log.append("turn/dispatch-hint", {"source": hint.source})
                dispatch_injected["done"] = True
            clock = maybe_inject(self.log)
            if clock is not None:
                extras.append(clock)
            machine = maybe_inject_host(self.log, cwd=self.workspace_dir)
            if machine is not None:
                extras.append(machine)
            return extras

        def refresh_system(*, plan_active: bool | None = None, plan_section: str | None = None) -> None:
            active = self.plan.get(self.log).active if plan_active is None else plan_active
            section = self.plan.policy_text(self.log) if plan_section is None else plan_section
            folded = visible_instruction_baseline(context.messages) or pending_baseline is not None
            context.system_prompt = build_system_prompt(
                self.workspace_dir,
                tool_names=sorted(shown),
                memory=memory,
                vault_keys=list(vault),
                plan_section=section,
                commands_section=self.command_registry.catalog_text(),
                todo_section=format_todo_section(current_todos(self.log)),
                list_snippets=not bool(loop_cfg.get("thin_tools", True)),
                prompt=prompt,
                plan_active=active,
                context_files=[] if folded else None,
                skill_query="\n".join(part for part in (_prior_user_text(context.messages), prompt) if part),
                agent_role=agent_role,
            )

        def on_tool_result(call: ToolCallBlock, result: AgentMessage) -> list[AgentMessage]:
            extras: list[AgentMessage] = []
            extra = reminder.observe(call.name, call.arguments)
            if extra is not None:
                extras.append(extra)
            switch = fail_strategy.observe(call.name, result)
            if switch is not None:
                extras.append(switch)
                self.log.append("turn/fail-strategy", {"tool": call.name})
            done = answer_now.observe(call.name, result, prompt=prompt)
            if done is not None:
                extras.append(done)
                self.log.append("turn/answer-now", {"tool": call.name})
            if call.name == "skill" and not getattr(result, "is_error", False):
                loaded = str((call.arguments or {}).get("name") or "").strip()
                if loaded and loaded not in active_skills:
                    active_skills.append(loaded)
                tightened = allowlist_for_skills(active_skills)
                if tightened is not None:
                    shown.intersection_update(
                        name for name in shown if tool_permitted(name, tightened)
                    )
                    refresh_system()
            if call.name in {"write", "edit", "apply_patch"} and not getattr(result, "is_error", False):
                for raw_path in _tool_touch_paths(call):
                    notice = instruction_update_hint(
                        str(self.workspace_dir),
                        raw_path,
                        seen=self._instruction_seen,
                        versions=self._instruction_versions,
                    )
                    remember_instruction_path(
                        str(self.workspace_dir),
                        raw_path,
                        self._instruction_seen,
                        versions=self._instruction_versions,
                    )
                    if notice is not None:
                        refresh_system()
                        extras.append(notice)
                        resolved = resolved_instruction_key(
                            str(self.workspace_dir), raw_path
                        )
                        self.log.append(
                            "turn/instruction-update",
                            {
                                "path": resolved,
                                "digest": self._instruction_seen.get(resolved, ""),
                            },
                        )
            if call.name in {"read", "write", "edit", "apply_patch"} and not getattr(result, "is_error", False):
                paths = _tool_touch_paths(call)
                raw_path = paths[0] if paths else ""
                prior_state = dict(self._instruction_seen)
                refreshed = instruction_reconcile_seen(
                    str(self.workspace_dir),
                    self._instruction_seen,
                    versions=self._instruction_versions,
                )
                extras.extend(refreshed)
                if refreshed:
                    refresh_system()
                    for key, digest in self._instruction_seen.items():
                        if prior_state.get(key) != digest:
                            self.log.append(
                                "turn/instruction-update",
                                {"path": key, "digest": digest},
                            )
                prior = set(self._instruction_seen)
                added = instruction_additional_hints(
                    str(self.workspace_dir),
                    raw_path,
                    seen=self._instruction_seen,
                    versions=self._instruction_versions,
                )
                extras.extend(added)
                if added:
                    self.log.append(
                        "turn/instruction-additional",
                        {
                            "path": raw_path,
                            "count": len(added),
                            "digests": {
                                key: self._instruction_seen[key]
                                for key in self._instruction_seen
                                if key not in prior
                            },
                        },
                    )
            if call.name == "todo_write" and not getattr(result, "is_error", False):
                refresh_system()
            if call.name == "exit_plan_mode" and not self.plan.get(self.log).active:
                plan_text = str((call.arguments or {}).get("plan") or "").strip()
                steps = plan_steps(plan_text)
                if steps and not has_open_todos(current_todos(self.log)):
                    seeded = [
                        {
                            "content": item,
                            "status": "in_progress" if index == 0 else "pending",
                        }
                        for index, item in enumerate(steps)
                    ]
                    apply_todo_write(self.log, seeded)
                    shown.add("todo_write")
                    self.log.append("turn/plan-todos", {"count": len(seeded)})
                extras.append(
                    AgentMessage(
                        role="user",
                        content=get_prompt("plan_approved", plan=plan_text[:2000] or "-"),
                        source="plugin:plan-approved",
                    )
                )
                shown.update(MUTATING_TOOLS)
                if needs_skill_tools(prompt):
                    shown.update({"skill", "list_available_skills"})
                refresh_system(plan_active=False, plan_section="")
                self.log.append("turn/plan-approved", {})
                if not skill_injected["done"]:
                    names = skill_names_for_turn(
                        [user],
                        prompt,
                        reserved=set(self.command_registry.names()),
                        auto=bool(loop_cfg.get("auto_skill", True)),
                        min_score=int(loop_cfg.get("auto_skill_min_score") or 4),
                        limit=int(loop_cfg.get("auto_skill_limit") or 1),
                        plan_active=False,
                    )
                    extras.extend(inject_skill_bodies(names))
                    if names:
                        self.log.append("turn/skill-match", {"names": names, "after": "plan-exit"})
                    skill_injected["done"] = True
            return extras

        def rewrite_tool_result(call: ToolCallBlock, result: AgentMessage) -> AgentMessage:
            if memory.workspace_dir is not None:
                from witty_agent.negative_ledger import record_failure

                record_failure(
                    memory.workspace_dir,
                    call,
                    result,
                    workspace=self.workspace_dir,
                )
            return apply_spill(
                result,
                call,
                scratchpad=self.scratchpad,
                session_id=self.session_id,
            )

        def gate_tool(call: ToolCallBlock) -> AgentMessage | None:
            if memory.workspace_dir is None:
                return None
            from witty_agent.negative_ledger import gate_attempt

            return gate_attempt(call=call, directory=memory.workspace_dir, workspace=self.workspace_dir)

        async def follow_up() -> list[AgentMessage]:
            asked = ask_gate.maybe_nudge(
                context.messages,
                has_memory=bool(memory.retrieved),
            )
            if asked is not None:
                shown.add("ask_user_question")
                posed = asked.text() == get_prompt("ask_gate_posed")
                if posed and self.questions.has_provider():
                    last = next(
                        (item for item in reversed(context.messages) if item.role == "assistant"),
                        None,
                    )
                    items = questions_from_assistant_text(last.text() if last else "")
                    if items:
                        logger.info("选择问 正文直接弹窗 count=%s", len(items))
                        self.log.append("turn/ask-gate", {"immediate": True, "count": len(items)})
                        try:
                            answer = await self.questions.ask(items)
                        except Exception as exc:
                            logger.warning("选择问直接弹窗失败 err=%s", exc)
                        else:
                            return [_answers_as_choice(answer)]
                logger.info("选择问 nudge posed=%s", posed)
                self.log.append("turn/ask-gate", {})
                return [asked]
            plan_on = self.plan.get(self.log).active
            presented = plan_gate.maybe_nudge(context.messages, plan_active=plan_on)
            if presented is not None:
                logger.info("计划模式 nudge 呈交")
                self.log.append("turn/plan-present-gate", {})
                return [presented]
            extra = evidence_gate.maybe_nudge(
                context.messages,
                has_memory=bool(memory.retrieved),
                memory_empty=memory.empty,
            )
            if extra is not None:
                logger.info("无证据收口 nudge")
                self.log.append("turn/evidence-gate", {})
                return [extra]
            planned = todo_gate.maybe_nudge(
                context.messages,
                has_todos=has_open_todos(current_todos(self.log)),
                plan_active=plan_on,
            )
            if planned is None:
                return []
            logger.info("多步任务 nudge todo")
            self.log.append("turn/todo-gate", {})
            return [planned]

        async def transform_context(messages: list[AgentMessage]) -> list[AgentMessage]:
            pruned = prune_tool_call_args(prune_tool_results(messages, compact_opts), compact_opts)
            compacted = await self._compact_locked(
                pruned,
                compact_opts,
                stream_fn=stream_fn,
            )
            # 比 `pruned` 而不是 `messages`：裁剪本身也会换出一份新列表，拿原始列表比会把
            # 「只裁了个工具结果」也记成一次压缩，还白跑一遍指令重装。
            if compacted is not pruned:
                self.log.append(
                    "compaction/result",
                    {"before": len(messages), "after": len(compacted)},
                )
                rearmed = instruction_rearm_after_compact(
                    messages,
                    compacted,
                    str(self.workspace_dir),
                    self._instruction_seen,
                )
                if rearmed:
                    compacted = [*compacted, *rearmed]
                    self.log.append(
                        "turn/instruction-additional",
                        {"path": "", "count": len(rearmed), "digests": {}, "rearm": True},
                    )
                self._pending_checkpoint = compacted
                notices = self._after_compact_notices(compacted, memory)
                if notices:
                    compacted = [*compacted, *notices]
            return compacted

        async def emit_loop(event: AgentEvent) -> None:
            if emit is None:
                return
            maybe = emit(event)
            if maybe is not None:
                await maybe

        def plan_blocks(name: str) -> bool:
            if blocks_tool(name, active=self.plan.get(self.log).active):
                return True
            allow = allowlist_for_skills(active_skills)
            return not tool_permitted(name, allow)

        def block_reason(name: str) -> str:
            if blocks_tool(name, active=self.plan.get(self.log).active):
                return get_prompt("plan_block_mutating", tool_name=name)
            allow = allowlist_for_skills(active_skills)
            allowed = "、".join(sorted(allow)) if allow else "-"
            return get_prompt(
                "skill_block_tool",
                tool_name=name,
                skills="、".join(active_skills) or "-",
                allowed=allowed,
            )

        async def stream_advertised(ctx: AgentContext) -> AgentMessage:
            live = set(shown)
            if self.plan.get(self.log).active:
                live.difference_update(MUTATING_TOOLS)
            if not loop_cfg.get("thin_tools", True) or len(live) >= len(ctx.tools):
                return await stream_fn(ctx)
            visible = [item for item in ctx.tools if getattr(item, "name", "") in live]
            return await stream_fn(replace(ctx, tools=visible))

        result = await run_agent_loop(
            [user],
            context,
            stream_advertised,
            LoopConfig(
                approval_mode=mode,
                approve=approve,
                convert_to_llm=_convert_to_llm,
                transform_context=transform_context,
                tool_execution=tool_execution or str(loop_cfg["tool_execution"]),
                auto_parallel=bool(loop_cfg.get("auto_parallel", True)),
                retry_attempts=int(loop_cfg["retry_attempts"]),
                max_turns=int(loop_cfg["max_turns"]),
                get_steering_messages=drain_steering,
                get_follow_up_messages=follow_up,
                is_aborted=aborted,
                should_stop_after_turn=should_stop_after_turn,
                session_log=self.log,
                pre_step=pre_step,
                on_tool_result=on_tool_result,
                rewrite_tool_result=rewrite_tool_result,
                gate_tool=gate_tool,
                tool_timeout_ms=int(loop_cfg.get("tool_timeout_ms") or 0),
                blocks_tool=plan_blocks,
                block_reason=block_reason,
            ),
            emit=emit_loop,
        )
        attach_turn_evidence(result.messages, memory_hits=memory.hits, memory_empty=memory.empty)
        seal = evidence_gate.maybe_seal(result.messages, has_memory=bool(memory.retrieved))
        if seal is not None:
            result.messages.append(seal)
            logger.info("无证据收口 seal")
            self.log.append("turn/evidence-seal", {})
            self.log.append(
                "assistant/message",
                {"text": seal.text(), "tool_calls": [], "source": seal.source},
            )
            await emit_loop(AgentEvent(type="message_end", message=seal))
        self._persist(user, result)
        run_invariants(self.log, result.messages)
        self._remember_title(prompt)
        self._harvest_memory(prompt, result, memory)
        return result

    def _after_compact_notices(
        self,
        compacted: list[AgentMessage],
        memory: SessionMemory,
    ) -> list[AgentMessage]:
        if memory.workspace_dir is None:
            return []
        from witty_agent.compaction import COMPACTION_CHECKPOINT_SOURCE
        from witty_agent.focus_board import (
            focus_notice,
            load_focus,
            missing_anchors,
            premise_notice,
            seed_from_lattice,
        )
        from witty_agent.memory_config import load_memory_settings

        board = seed_from_lattice(memory.workspace_dir, load_focus(memory.workspace_dir))
        extras: list[AgentMessage] = []
        already = {str(item.source or "") for item in compacted[-4:]}
        body = focus_notice(board, limit=load_memory_settings().focus_max_chars)
        if body and "plugin:focus-board" not in already:
            extras.append(
                AgentMessage(role="user", content=body, source="plugin:focus-board")
            )
        summary = ""
        for item in compacted:
            if item.source == COMPACTION_CHECKPOINT_SOURCE:
                summary = item.text()
                break
        missed = missing_anchors(summary, board)
        if missed and "plugin:premise-guard" not in already:
            extras.append(
                AgentMessage(
                    role="user",
                    content=premise_notice(missed),
                    source="plugin:premise-guard",
                )
            )
        return extras

    def _harvest_memory(self, prompt: str, result: LoopResult, memory: SessionMemory) -> None:
        """确定性收割：线索词、分类、工具事实、助手决定。全是本地文件读写，毫秒级。

        模型判官不在这儿——它是这条路上唯一的网络调用，交给 `_spawn_judge` 挪到后台。
        """
        try:
            from witty_agent.memory_harvest import (
                harvest_assistant_notes,
                harvest_tool_facts,
                harvest_user_text,
                last_assistant_text,
            )
            from witty_agent.trace import collect_turn_evidence

            from witty_agent.memory import cite_tag

            seq = self.log.events[-1].seq if self.log.events else 0
            cite = cite_tag(self.session_id, seq)
            report = harvest_user_text(memory.user_dir, prompt, cite=cite, defer_judge=True)
            wrote = int(report.get("added") or 0)
            items, _reason = collect_turn_evidence(result.messages, memory_hits=memory.hits)
            if memory.workspace_dir is not None:
                facts = harvest_tool_facts(memory.workspace_dir, items, cite=cite)
                notes = harvest_assistant_notes(
                    memory.workspace_dir,
                    last_assistant_text(result.messages),
                    cite=cite,
                )
                wrote += int(facts.get("added") or 0) + int(notes.get("added") or 0)
                from witty_agent.handoff_note import fold_handoff

                fold_handoff(
                    memory.workspace_dir,
                    self.workspace_dir,
                    user_text=prompt,
                    assistant_text=last_assistant_text(result.messages),
                )
            if wrote:
                self._invalidate_recall()
            self._note_diary(result, memory)
            self._spawn_upkeep(memory, list(report.get("pending_judge") or []), prompt, cite)
        except Exception as exc:
            logger.warning("记忆收割失败 err=%s", exc)

    def _note_diary(self, result: LoopResult, memory: SessionMemory) -> None:
        """把这一轮 agent 干的活记进日记。纯本地，不花 token。

        日记原来只收「用户说的话」里撞到线索词的碎片，助手做过什么一个字都不记，于是
        「今天做了什么」永远查不到。没动过工具的纯问答不记——那不算做了什么。
        """
        try:
            from witty_agent.diary import note_work, turn_actions

            line = turn_actions(result.messages)
            if line:
                note_work(line, memory_dir=memory.user_dir)
        except Exception as exc:
            logger.warning("日记记录失败 err=%s", exc)

    def _gc_due(self) -> bool:
        """一个会话只扫一次空工作区目录。每轮扫是白费 IO，一次都不扫就永远不清。"""
        if self._gc_swept:
            return False
        self._gc_swept = True
        return True

    def _invalidate_recall(self) -> None:
        """刚写进去的记忆必须参与下一轮检索。

        召回结果本来按话题缓存（`topic_switched` 为假就复用上一轮的 hits），可缓存只在
        检索时写、从来没人作废。于是「说一件事 → 接着追问同一件事」这条最常见的路径上，
        新记的东西整段对话都召不回——话题没切，永远走复用分支。
        """
        self._last_memory_query = ""
        self._last_memory_hits = ()
        self._last_memory_retrieved = ""

    def _spawn_upkeep(
        self,
        memory: SessionMemory,
        pending: list[str],
        prompt: str,
        cite: str,
    ) -> None:
        """判官 + 巩固 + 日记小结，几件慢活一起丢到后台任务。

        本轮不等它们，下一轮开头 `_settle_harvest` 等。都要喊模型，串在一个任务里
        跑就够了——判官和巩固改的是同一份九宫格，并行只会互相覆盖。
        """
        import asyncio

        from witty_agent.async_bridge import in_event_loop
        from witty_agent.diary import days_needing_summary
        from witty_agent.memory_consolidate import pick_cells
        from witty_agent.memory_harvest import _live_judge_allowed

        # 判官和巩固都要喊模型，没端点就免谈；GC 和日记是本地活，没模型也得照做——
        # 日记小结没模型时退回本地统计，总比「今天什么都没记」强。
        live = _live_judge_allowed()
        cells = pick_cells(memory.user_dir) if live else []
        todo = list(pending) if live else []
        sweep = self._gc_due()
        days = days_needing_summary(memory.user_dir)
        if not todo and not cells and not sweep and not days:
            return

        async def work() -> dict[str, object]:
            from witty_agent.diary import asummarize_day
            from witty_agent.memory_harvest import ajudge_pending_leftover

            added = 0
            if todo:
                report = await ajudge_pending_leftover(memory.user_dir, todo, prompt, cite=cite)
                added += int(report.get("added") or 0)
            if cells:
                from witty_agent.memory_consolidate import aconsolidate

                merged = await aconsolidate(memory.user_dir, cells)
                added += int(merged.get("removed") or 0)
            if sweep:
                from witty_agent.memory import gc_workspace_memory

                # 本地活但要遍历整棵记忆目录，别占着事件循环
                await asyncio.to_thread(
                    gc_workspace_memory,
                    memory.user_dir.parent,
                    keep=memory.workspace_key,
                )
            for day in days:
                await asummarize_day(day, memory_dir=memory.user_dir)
            return {"added": added}

        if not in_event_loop():
            asyncio.run(work())
            self._invalidate_recall()
            return

        # 养护要跨轮存活，所以给它自己的线程和自己的事件循环。
        #
        # 别改回 `loop.create_task(...)`：调用方每一轮都是一个独立的 `asyncio.run`
        # （见 `http_api._start_session_run` 的 worker），本轮循环一关，绑在上面的 Task
        # 就被取消；下一轮 `_settle_harvest` 去 await 它，拿到的是 `CancelledError`。
        # 那是 `BaseException`，`except Exception` 接不住，会一路漏穿 worker 把线程
        # 静默做掉，`run["status"]` 永远停在 running——表现就是之后每次发送都 409
        # "run in progress"。见 DEFECTS「后台养护任务跨事件循环」。
        import threading
        from concurrent.futures import Future

        settled: Future = Future()

        def runner() -> None:
            try:
                settled.set_result(asyncio.run(work()))
            except BaseException as exc:  # noqa: BLE001 - 后台线程，异常只能带回给等待方
                settled.set_exception(exc)

        threading.Thread(target=runner, daemon=True, name="witty-upkeep").start()
        self._harvest_pending = settled

    async def _settle_harvest(self) -> None:
        """等上一轮的后台判官落盘。它慢的时候宁可放它继续跑，也不拖住这一轮。"""
        settled = self._harvest_pending
        if settled is None:
            return
        self._harvest_pending = None
        import asyncio

        from witty_agent.memory_config import load_memory_settings

        wait = load_memory_settings().judge_settle_sec
        try:
            report = await asyncio.to_thread(settled.result, wait)
        except TimeoutError:
            logger.info("判官未在 %ss 内落盘，本轮先走，结果留给下一轮", wait)
            self._harvest_pending = settled
            return
        except asyncio.CancelledError:
            # concurrent.futures.CancelledError 就是 asyncio.CancelledError，两边同名。
            # 靠 future 自己的状态区分：它被取消是「养护没了」，本轮照走；否则是本轮自己
            # 被取消，得把取消信号原样抛上去，不能吞。
            if settled.cancelled():
                logger.warning("后台判官被取消，本轮跳过")
                return
            self._harvest_pending = settled
            raise
        except Exception as exc:
            logger.warning("后台判官失败 err=%s", exc)
            return
        if int((report or {}).get("added") or 0):
            self._invalidate_recall()

    async def run_goal(
        self,
        objective: str,
        *,
        stream_fn: StreamFn,
        approve: ApproveFn | None = None,
        approval_mode: ApprovalMode | None = None,
        budget: int = -1,
        max_rounds: int = 100,
        gates: Sequence[GateSpec] = (),
        judge: JudgeFn | None = None,
    ):
        """目标模式。判据由客观 gate + 回归义务 + 判官三样给，不由模型自述给。

        判官默认走本会话的模型，但它是**另一次**无工具调用，不在干活那条推理链上。生产上
        应该用 `[goal].judge_model_id` 指到一个小快模型：判官每轮都要跑一次，用主模型评一
        整条轨迹很贵，而这件事不需要主模型的本事。

        回归义务台账落 Agent 级、按工作区分（不在会话草稿区）：验过的判据换个会话仍然成立，
        这是它跟「一次运行内的临时状态」的区别；而按工作区分是因为一条 gate 命令只对它所在
        的那棵树有意义。
        """
        from witty_agent.goal import model_judge, run_goal_loop
        from witty_agent.memory import workspace_memory_key

        async def runner(prompt: str) -> list[AgentMessage]:
            result = await self.run(
                prompt,
                stream_fn=stream_fn,
                approve=approve,
                approval_mode=approval_mode,
            )
            return list(getattr(result, "messages", None) or [])

        goal_cfg = goal_settings()
        if judge is None and goal_cfg["judge"]:
            judge_model = self.model
            override = str(goal_cfg["judge_model_id"] or "")
            if override:
                judge_model = replace(self.model, model_id=override)
            judge = model_judge(
                stream_fn,
                model=judge_model,
                workspace_dir=str(self.workspace_dir),
                project_id=self.agent.project.project_id,
                agent_id=self.agent.record.agent_id,
                session_id=self.session_id,
            )
        return await run_goal_loop(
            objective=objective,
            scratch=self._ensure_scratchpad(),
            runner=runner,
            max_rounds=max_rounds,
            budget=budget,
            judge=judge,
            gates=gates,
            workspace=self.workspace_dir,
            ledger_dir=criteria_dir(
                workspace_memory_key(self.workspace_dir),
                self.agent.project.project_id,
                self.agent.record.agent_id,
                root=self.agent.root,
            ),
        )

    def fork(self, *, keep: int | None = None, session_id: str | None = None) -> Session:
        new_id = session_id or uuid.uuid4().hex
        directory = traces_dir(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            root=self.agent.root,
        )
        fork_session(
            directory,
            self.session_id,
            new_id,
            cwd=str(self.workspace_dir),
            keep=keep,
        )
        child = create_session(
            self.agent,
            workspace_dir=self.workspace_dir,
            session_id=new_id,
            parent_id=self.session_id,
        )
        logger.info("fork session from=%s to=%s", self.session_id, new_id)
        return child

    def _ensure_scratchpad(self) -> Path:
        if self.scratchpad is None:
            self.scratchpad = scratchpad_dir(
                self.session_id,
                self.agent.project.project_id,
                self.agent.record.agent_id,
                root=self.agent.root,
            )
        self.scratchpad.mkdir(parents=True, exist_ok=True)
        return self.scratchpad

    def _store_path(self) -> Path:
        directory = traces_dir(
            self.agent.project.project_id,
            self.agent.record.agent_id,
            root=self.agent.root,
        )
        return session_path(directory, self.session_id)

    async def _compact_locked(self, messages: list[AgentMessage], compact_opts, stream_fn):
        try:
            self._compact_lock.acquire()
        except CompactionBusy:
            logger.info("压缩忙，本轮跳过")
            return messages
        try:
            return await compact_messages_async(
                messages,
                compact_opts,
                stream_fn=stream_fn,
                workspace_dir=str(self.workspace_dir),
                project_id=self.agent.project.project_id,
                agent_id=self.agent.record.agent_id,
                session_id=self.session_id,
                model=self.model,
            )
        finally:
            self._compact_lock.release()

    def compact_now(self, rest: str = "") -> CommandResult:
        """手动压缩：空闲时强制压一段，忙则拒绝。"""
        if self._run_active:
            return CommandResult(kind="error", text=get_prompt("compaction_busy"))
        try:
            span = parse_compact_range(rest)
        except ValueError:
            return CommandResult(kind="error", text=get_prompt("compaction_bad_range"))
        try:
            self._compact_lock.acquire()
        except CompactionBusy:
            return CommandResult(kind="error", text=get_prompt("compaction_busy"))
        try:
            self._hydrate_log()
            history = load_messages(self._store_path())
            before = len(history)
            opts = settings_from_runtime(compaction_settings())
            if span is None:
                compacted = force_compact(history, opts, force=True)
            else:
                compacted = compact_region(history, span[0], span[1], opts)
            return self._finish_compact(history, compacted, before, span=span)
        finally:
            self._compact_lock.release()

    async def compact_now_async(self, *, stream_fn=None, rest: str = "") -> CommandResult:
        """斜杠 /compact：有模型则摘要，失败回退摘录。`/compact 2-10` 压闭区间。"""
        if self._run_active:
            return CommandResult(kind="error", text=get_prompt("compaction_busy"))
        try:
            span = parse_compact_range(rest)
        except ValueError:
            return CommandResult(kind="error", text=get_prompt("compaction_bad_range"))
        try:
            self._compact_lock.acquire()
        except CompactionBusy:
            return CommandResult(kind="error", text=get_prompt("compaction_busy"))
        try:
            self._hydrate_log()
            history = load_messages(self._store_path())
            before = len(history)
            opts = settings_from_runtime(compaction_settings())
            if span is None:
                compacted = await compact_messages_async(
                    history,
                    opts,
                    stream_fn=stream_fn,
                    force=True,
                    workspace_dir=str(self.workspace_dir),
                    project_id=self.agent.project.project_id,
                    agent_id=self.agent.record.agent_id,
                    session_id=self.session_id,
                    model=self.model,
                )
                return self._finish_compact(
                    history,
                    None if compacted is history else compacted,
                    before,
                )
            compacted = await compact_region_async(
                history,
                span[0],
                span[1],
                opts,
                stream_fn=stream_fn,
                workspace_dir=str(self.workspace_dir),
                project_id=self.agent.project.project_id,
                agent_id=self.agent.record.agent_id,
                session_id=self.session_id,
                model=self.model,
            )
            return self._finish_compact(history, compacted, before, span=span)
        finally:
            self._compact_lock.release()

    async def refine_now_async(self, *, stream_fn, rest: str = "") -> CommandResult:
        """斜杠 /refine：复盘本会话轨迹沉淀经验；`/refine undo` 回滚上一次沉淀。

        复盘员跟目标模式的判官同一形状：一次无工具调用，默认走本会话模型，
        `[refine].model_id` 可指到小快模型。落盘、回滚、证据筛查都在 refine.py。
        """
        from witty_agent.refine import run_refine, undo_refine
        from witty_agent.runtime import refine_settings

        token = (rest or "").strip()
        if token == "undo":
            return undo_refine(self.agent.record, root=self.agent.root)
        self._hydrate_log()
        model = self.model
        override = str(refine_settings()["model_id"] or "")
        if override:
            model = replace(self.model, model_id=override)
        return await run_refine(
            stream_fn,
            model=model,
            record=self.agent.record,
            workspace_dir=self.workspace_dir,
            history=load_messages(self._store_path()),
            note=token,
            root=self.agent.root,
            session_id=self.session_id,
        )

    def _finish_compact(
        self,
        history: list[AgentMessage],
        compacted: list[AgentMessage] | None,
        before: int,
        *,
        span: tuple[int, int] | None = None,
    ) -> CommandResult:
        if compacted is None:
            return CommandResult(kind="success", text=get_prompt("compaction_noop"))
        rearmed = instruction_rearm_after_compact(
            history,
            compacted,
            str(self.workspace_dir),
            self._instruction_seen,
        )
        if rearmed:
            compacted = [*compacted, *rearmed]
        self._persist_compaction(compacted, before, manual=True)
        if span is None:
            text = get_prompt("compaction_ok", before=str(before), after=str(len(compacted)))
        else:
            text = get_prompt(
                "compaction_ok_region",
                start=str(span[0]),
                end=str(span[1]),
                before=str(before),
                after=str(len(compacted)),
            )
        return CommandResult(kind="success", text=text)

    def _write_compaction_checkpoint(self, compacted: list[AgentMessage]) -> None:
        """压缩检查点：把替换表层追加进 jsonl，load 从检查点起读。"""
        path = self._store_path()
        write_header(path, self.session_id, str(self.workspace_dir), self.parent_id)
        for message in compacted:
            append_message(path, message)

    def _persist_compaction(
        self,
        compacted: list[AgentMessage],
        before: int,
        *,
        manual: bool = True,
    ) -> None:
        self._write_compaction_checkpoint(compacted)
        self.log.append(
            "compaction/result",
            {"before": before, "after": len(compacted), "manual": manual},
        )
        for event in self.log.events:
            if event.seq > self._persisted_seq:
                append_session_event(self._store_path(), event)
        self._persisted_seq = max((item.seq for item in self.log.events), default=self._persisted_seq)

    def _persist(self, user: AgentMessage, result: LoopResult) -> None:
        path = self._store_path()
        write_header(path, self.session_id, str(self.workspace_dir), self.parent_id)
        pending = self._pending_checkpoint
        self._pending_checkpoint = None
        if pending:
            self._write_compaction_checkpoint(pending)
            seen = {id(item) for item in pending}
            extras = [user, *[item for item in result.messages if item is not user]]
            for message in extras:
                if id(message) not in seen:
                    append_message(path, message)
        else:
            append_message(path, user)
            for message in result.messages:
                if message is user:
                    continue
                append_message(path, message)
        for event in result.events:
            if event.type in {"tool_execution_end", "approval_required", "agent_end"}:
                append_event(path, event.type, tool_name=event.tool_name, reason=event.reason)
        for event in self.log.events:
            if event.seq > self._persisted_seq:
                append_session_event(path, event)
        self._persisted_seq = max((item.seq for item in self.log.events), default=self._persisted_seq)
        usage_in = sum(item.usage.input for item in result.messages)
        usage_out = sum(item.usage.output for item in result.messages)
        if usage_in or usage_out:
            append_usage(path, usage_in, usage_out)

    def _remember_title(self, prompt: str) -> None:
        if self.title:
            return
        from witty_agent.store import append_title, session_topic

        line = session_topic(prompt, fallback=self.session_id)
        self.title = line
        append_title(self._store_path(), line)

    def _hydrate_log(self) -> None:
        if self._hydrated:
            return
        path = self._store_path()
        events = load_session_events(path)
        if events:
            self.log.hydrate(events)
            self._instruction_seen.update(fold_instruction_seen(self.log.events))
            self._persisted_seq = max(item.seq for item in events)
        closers = repair_session_log(self.log)
        if closers:
            write_header(path, self.session_id, str(self.workspace_dir), self.parent_id)
            for event in closers:
                append_session_event(path, event)
                if event.type == "tool/result":
                    append_message(path, result_message_from_repair(event))
            self._persisted_seq = max(item.seq for item in self.log.events)
        self._hydrated = True

    def _bind_capabilities(self) -> None:
        self.capabilities.provide("sessionLog", self.log)
        self.capabilities.provide("planMode", self.plan)
        self.capabilities.provide("userQuestions", self.questions)
        self.capabilities.provide("commands", self.command_registry)

    def _ensure_commands(self) -> None:
        if self.command_registry.names():
            return
        from witty_agent.prompts import get_prompt

        def plan_cmd(rest: str) -> CommandResult:
            token = rest.strip()
            if token == "off":
                self.plan.set(self.log, False)
                return CommandResult(kind="success", text=get_prompt("plan_mode_off"))
            self._hydrate_log()
            self.plan.set(self.log, True)
            if token:
                return CommandResult(kind="success", text=get_prompt("plan_mode_on"), remainder=token)
            return CommandResult(kind="success", text=get_prompt("plan_mode_on"))

        def abort_cmd(_rest: str) -> CommandResult:
            self.abort()
            return CommandResult(kind="success", text=get_prompt("command_aborted"))

        def loop_cmd(rest: str) -> CommandResult:
            from witty_agent.loop_control import apply_loop

            return apply_loop(self, rest)

        def compact_cmd(rest: str) -> CommandResult:
            return self.compact_now(rest)

        def create_skill_cmd(rest: str) -> CommandResult:
            from witty_agent.skill_scaffold import create_skill_from_brief, parse_create_skill_args

            brief, name, overwrite = parse_create_skill_args(rest)
            if not brief:
                return CommandResult(kind="error", text=get_prompt("create_skill_usage"))
            try:
                meta = create_skill_from_brief(
                    brief,
                    name=name,
                    overwrite=overwrite,
                    project_id=self.agent.project.project_id,
                    agent_id=self.agent.record.agent_id,
                    root=self.agent.root,
                )
            except FileExistsError as exc:
                existing = str(exc).removeprefix("用户技能 ").split(" ", 1)[0]
                return CommandResult(
                    kind="error",
                    text=get_prompt("create_skill_exists", skill=existing),
                )
            except ValueError as exc:
                return CommandResult(kind="error", text=str(exc))
            return CommandResult(
                kind="success",
                text=get_prompt("create_skill_ok", skill=meta.name, path=str(meta.path)),
            )

        def refine_cmd(rest: str) -> CommandResult:
            # 同步兜底只认 undo；复盘要模型，走 run() 里的异步特例（同 /compact 的形状）。
            from witty_agent.refine import undo_refine

            if (rest or "").strip() == "undo":
                return undo_refine(self.agent.record, root=self.agent.root)
            return CommandResult(kind="error", text=get_prompt("refine_needs_model"))

        self.command_registry.register("plan", get_prompt("command_desc_plan"), plan_cmd)
        self.command_registry.register("abort", get_prompt("command_desc_abort"), abort_cmd)
        self.command_registry.register("compact", get_prompt("command_desc_compact"), compact_cmd)
        self.command_registry.register("loop", get_prompt("command_desc_loop"), loop_cmd)
        self.command_registry.register(
            "create-skill",
            get_prompt("command_desc_create_skill"),
            create_skill_cmd,
        )
        self.command_registry.register("refine", get_prompt("command_desc_refine"), refine_cmd)


def create_agent(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
    description: str = "",
) -> WittyAgent:
    project = init_project(project_id, root=root)
    record = init_agent_state(project_id, agent_id, root=root, description=description)
    apply_kernel_update(record.state_dir / "system_config.toml")
    record = load_agent_state(project_id, agent_id, root=root)
    resolved = root or data_root()
    logger.info("create_agent project=%s agent=%s", project_id, agent_id)
    return WittyAgent(project=project, record=record, root=resolved)


def create_session(
    agent: WittyAgent,
    *,
    workspace_dir: str | Path | None = None,
    session_id: str | None = None,
    parent_id: str | None = None,
) -> Session:
    workspace = Path(workspace_dir or Path.cwd()).resolve()
    sid = session_id or uuid.uuid4().hex
    model = ModelRef(
        provider=agent.project.default_provider,
        model_id=agent.project.default_model_id,
    )
    if agent.project.models:
        first = agent.project.models[0]
        model = ModelRef(
            provider=first.provider,
            model_id=first.model_id,
            api_key=first.api_key,
            base_url=first.base_url,
        )
    pad = scratchpad_dir(sid, agent.project.project_id, agent.record.agent_id, root=agent.root)
    pad.mkdir(parents=True, exist_ok=True)
    logger.info("create_session id=%s workspace=%s parent=%s", sid, workspace, parent_id)
    session = Session(
        agent=agent,
        session_id=sid,
        workspace_dir=workspace,
        model=model,
        parent_id=parent_id,
        scratchpad=pad,
    )
    write_header(session._store_path(), sid, str(workspace), parent_id)
    return session


def list_project_agents(project_id: str = DEFAULT_PROJECT_ID, *, root: Path | None = None) -> list[str]:
    return list_agents(project_id, root=root)


def _answers_as_choice(answer: object) -> AgentMessage:
    rows = []
    for row in getattr(answer, "answers", []) or []:
        item = {
            "id": str(getattr(row, "id", "") or ""),
            "selected": [str(part) for part in (getattr(row, "selected", None) or [])],
        }
        custom = str(getattr(row, "custom", "") or "")
        if custom:
            item["custom"] = custom
        rows.append(item)
    return AgentMessage(
        role="user",
        content=get_prompt("ask_gate_answered", payload=json.dumps({"answers": rows}, ensure_ascii=False)),
        source="plugin:ask-gate",
    )


def _used_tool_names(messages: list[AgentMessage]) -> list[str]:
    names: list[str] = []
    for item in messages:
        for call in item.tool_calls():
            if call.name:
                names.append(call.name)
        if item.tool_name:
            names.append(item.tool_name)
    return names


def _spill_tool_names(messages: list[AgentMessage]) -> list[str]:
    for item in messages:
        if item.role != "toolResult":
            continue
        if "spill:" in item.text():
            return ["spill_read"]
    return []


def _prior_user_text(messages: list[AgentMessage], *, limit: int = 8) -> str:
    parts: list[str] = []
    for item in messages:
        if item.role != "user":
            continue
        if str(item.source or "").startswith("plugin:"):
            continue
        text = item.text().strip()
        if text:
            parts.append(text)
    return "\n".join(parts[-limit:])


def _convert_to_llm(messages: list[AgentMessage]) -> list[AgentMessage]:
    return [item for item in messages if item.role in {"user", "assistant", "toolResult"}]
