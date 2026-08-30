"""本机环境。内核每轮必带：系统、用户、路径、Git、网络策略。不是业务分支。"""

from __future__ import annotations

import getpass
import locale
import os
import platform
import subprocess
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.runtime import model_settings, web_settings
from witty_agent.session_log import SessionLog
from witty_agent.types import AgentMessage

logger = get_logger("host")

_LOGGED = False


def host_family(system: str | None = None) -> str:
    name = (system or platform.system()).strip().casefold()
    if name in {"darwin", "macos", "mac os", "osx"}:
        return "macos"
    if name.startswith("win"):
        return "windows"
    return "linux"


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "-"


def _locale() -> str:
    env = os.environ.get("LANG") or os.environ.get("LC_ALL") or ""
    if env:
        return env
    try:
        lang, _enc = locale.getlocale()
    except Exception:
        lang = None
    return lang or "-"


def _encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def _git_line(cwd: Path) -> str:
    try:
        root = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        if root.returncode != 0:
            return get_prompt("host_git_none")
        top = (root.stdout or "").strip() or str(cwd)
        branch = subprocess.run(
            ["git", "-C", top, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        name = (branch.stdout or "").strip() or "-"
        dirty = subprocess.run(
            ["git", "-C", top, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        state = get_prompt("host_git_dirty") if (dirty.stdout or "").strip() else get_prompt("host_git_clean")
        return get_prompt("host_git_yes", root=top, branch=name, state=state)
    except Exception:
        return get_prompt("host_git_none")


def _network_line() -> str:
    settings = web_settings()
    hosts = ", ".join(str(item) for item in (settings.get("allow_hosts") or []) if item) or "-"
    if settings.get("deny_public", False):
        return get_prompt("host_net_intranet", hosts=hosts)
    return get_prompt("host_net_open", hosts=hosts)


def host_environment(*, system: str | None = None, cwd: str | Path | None = None) -> dict[str, str]:
    family = host_family(system)
    raw = (system or platform.system()).strip() or "unknown"
    work = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    if family == "windows":
        sep = "\\"
        shell = os.environ.get("COMSPEC") or "powershell.exe"
        eol = "CRLF"
    else:
        sep = "/"
        shell = os.environ.get("SHELL") or ("/bin/zsh" if family == "macos" else "/bin/bash")
        eol = "LF"
    label = get_prompt(f"host_label_{family}")
    from witty_agent.sandbox import public_sandbox

    model_id = str(model_settings().get("model_id") or "-")
    box = public_sandbox(workspace=str(work))
    ready_key = "host_sandbox_ready_yes" if box.get("ready") == "true" else "host_sandbox_ready_no"
    env = {
        "family": family,
        "label": label,
        "system": raw,
        "release": platform.release(),
        "machine": platform.machine() or "unknown",
        "sep": sep,
        "eol": eol,
        "shell": shell,
        "home": str(Path.home()),
        "cwd": str(work),
        "username": _username(),
        "hostname": platform.node() or "-",
        "locale": _locale(),
        "encoding": _encoding(),
        "python": platform.python_version(),
        "cpus": str(os.cpu_count() or "-"),
        "tmp": str(Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")),
        "git": _git_line(work),
        "network": _network_line(),
        "model_id": model_id,
        "sandbox_work": box["work"] or "-",
        "sandbox_python": box["python"] or "-",
        "sandbox_packages": box["packages"] or "-",
        "sandbox_ready": get_prompt(ready_key),
        "sandbox_tmp": box.get("tmp") or "-",
        "sandbox_policy": get_prompt("host_sandbox_policy"),
    }
    global _LOGGED
    if not _LOGGED:
        logger.info(
            "本机环境 family=%s user=%s git=%s net=%s",
            family,
            env["username"],
            env["git"],
            env["network"],
        )
        _LOGGED = True
    return env


def host_section(*, system: str | None = None, cwd: str | Path | None = None) -> str:
    return get_prompt("host_now_section", **host_environment(system=system, cwd=cwd))


def maybe_inject(
    log: SessionLog,
    *,
    system: str | None = None,
    cwd: str | Path | None = None,
) -> AgentMessage | None:
    for event in log.events:
        if event.type == "user/message" and event.data.get("source") == "plugin:host-context":
            return None
    env = host_environment(system=system, cwd=cwd)
    text = get_prompt("host_context_once", **env)
    return AgentMessage(role="user", content=text, source="plugin:host-context")
