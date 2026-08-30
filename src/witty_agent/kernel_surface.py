"""底座内核面：内置工具和命令不可覆盖、不可卸载。"""

from __future__ import annotations

KERNEL_TOOL_PACKAGE = "witty_agent.tools"

KERNEL_TOOLS = frozenset(
    {
        "apply_patch",
        "ask_user_question",
        "bash",
        "edit",
        "exec_command",
        "exit_plan_mode",
        "find",
        "grep",
        "input_command",
        "input_subagent",
        "job_kill",
        "job_list",
        "job_output",
        "list_available_skills",
        "list_commands",
        "ls",
        "memory_read",
        "memory_status",
        "memory_write",
        "plan_read",
        "plan_write",
        "python_repl",
        "python_repl_status",
        "read",
        "run_fanout",
        "run_subagent",
        "schedule_delete",
        "schedule_list",
        "schedule_write",
        "session_query",
        "skill",
        "spill_read",
        "todo_write",
        "web_fetch",
        "write",
    }
)

KERNEL_COMMANDS = frozenset({"abort", "compact", "loop", "plan"})


def is_kernel_tool(name: str) -> bool:
    return name in KERNEL_TOOLS


def is_kernel_command(name: str) -> bool:
    return name in KERNEL_COMMANDS


def is_kernel_tool_module(module_name: str) -> bool:
    return module_name == KERNEL_TOOL_PACKAGE or module_name.startswith(f"{KERNEL_TOOL_PACKAGE}.")
