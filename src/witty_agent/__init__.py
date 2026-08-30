from witty_agent.capability import CapabilityRegistry
from witty_agent.commands import CommandRegistry, CommandResult
from witty_agent.kernel_surface import KERNEL_COMMANDS, KERNEL_TOOLS, is_kernel_command, is_kernel_tool
from witty_agent.evolution import (
    append_score,
    ensure_benchmark,
    restore_snapshot,
    run_optimize_loop,
    save_snapshot,
)
from witty_agent.goal import run_goal_loop
from witty_agent.http_api import configure_api, handle_request, serve
from witty_agent.kernel import apply_kernel_update
from witty_agent.llm import OpenAICompatLLM, ScriptedLLM
from witty_agent.logging import get_logger, set_trace_id, setup_logging
from witty_agent.mcp import load_mcp_tools
from witty_agent.orchestrator import JobSpec, Orchestrator
from witty_agent.plan_mode import PlanModeController
from witty_agent.prompts import get_prompt, load_prompts
from witty_agent.session_log import SessionLog, derive_messages, fold_plan_mode, fold_todos
from witty_agent.embed import Witty
from witty_agent.result import RunResult, result_text
from witty_agent.permission import PermissionPolicy
from witty_agent.session import WittyAgent, create_agent, create_session, list_project_agents
from witty_agent.schedule import Scheduler, parse_schedule_file
from witty_agent.session_tree import fork_session
from witty_agent.skills import list_skills, load_skill
from witty_agent.tools import get_tool, list_tools, tool
from witty_agent.vault import load_vault, set_vault_entry

__all__ = [
    "CapabilityRegistry",
    "CommandRegistry",
    "CommandResult",
    "KERNEL_COMMANDS",
    "KERNEL_TOOLS",
    "JobSpec",
    "is_kernel_command",
    "is_kernel_tool",
    "OpenAICompatLLM",
    "Orchestrator",
    "PlanModeController",
    "Scheduler",
    "ScriptedLLM",
    "SessionLog",
    "Witty",
    "WittyAgent",
    "PermissionPolicy",
    "RunResult",
    "derive_messages",
    "fold_plan_mode",
    "fold_todos",
    "append_score",
    "apply_kernel_update",
    "configure_api",
    "create_agent",
    "create_session",
    "handle_request",
    "ensure_benchmark",
    "fork_session",
    "get_logger",
    "get_prompt",
    "get_tool",
    "list_project_agents",
    "list_skills",
    "list_tools",
    "load_mcp_tools",
    "load_prompts",
    "load_skill",
    "parse_schedule_file",
    "restore_snapshot",
    "result_text",
    "run_goal_loop",
    "run_optimize_loop",
    "save_snapshot",
    "serve",
    "set_trace_id",
    "set_vault_entry",
    "setup_logging",
    "load_vault",
    "tool",
    "main",
]


def main() -> None:
    import sys

    setup_logging()
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "mail-live":
        from witty_agent.plugins.mail import probe_live

        raise SystemExit(probe_live())
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from witty_agent.doctor import run_doctor

        raise SystemExit(run_doctor())
    logger = get_logger("cli")
    prompt = get_prompt("harness_system")
    skills = list_skills()
    tools = list_tools()
    logger.info(
        "启动完成 prompt_ready=%s skills=%s tools=%s",
        bool(prompt),
        len(skills),
        len(tools),
    )
    print(prompt.splitlines()[0])
    print(f"skills={len(skills)} tools={len(tools)}")

