"""按工作目录 + git 分支折叠交接，下次同分支注入。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.time_context import clock_now

logger = get_logger("handoff")

HANDOFF_DIR = "handoff"
HANDOFF_CAP = 1800
_SAFE_BRANCH = re.compile(r"[^A-Za-z0-9._-]+")
_STALE_SANDBOX = re.compile(
    r"workspace-write|锁死在工作区|只能写工作区或 sandbox",
    re.IGNORECASE,
)


def git_branch(cwd: str | Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "nogit"
    name = (completed.stdout or "").strip()
    if completed.returncode != 0 or not name or name == "HEAD":
        return "nogit"
    return name


def handoff_path(directory: Path, branch: str) -> Path:
    safe = _SAFE_BRANCH.sub("-", branch).strip("-") or "nogit"
    return directory / HANDOFF_DIR / f"{safe}.md"


def load_handoff(directory: Path, cwd: str | Path) -> str:
    path = handoff_path(directory, git_branch(cwd))
    if not path.is_file():
        return ""
    text = _without_stale_sandbox(path.read_text(encoding="utf-8")).strip()
    return text[:HANDOFF_CAP]


def fold_handoff(
    directory: Path,
    cwd: str | Path,
    *,
    user_text: str,
    assistant_text: str,
) -> Path | None:
    user = _clip(user_text)
    assistant = _clip(assistant_text)
    if not user and not assistant:
        return None
    branch = git_branch(cwd)
    path = handoff_path(directory, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.is_file() else ""
    stamp = str(clock_now()["date"])
    block = get_prompt(
        "handoff_turn",
        date=stamp,
        user=user or "-",
        assistant=assistant or "-",
    )
    body = (previous.strip() + "\n\n" + block).strip() if previous.strip() else block
    if len(body) > HANDOFF_CAP:
        body = body[-HANDOFF_CAP:]
        cut = body.find("\n")
        if 0 < cut < 80:
            body = body[cut + 1 :]
    header = get_prompt("handoff_header", branch=branch, date=stamp)
    if not body.startswith("#"):
        body = header + "\n\n" + body
    path.write_text(body.strip() + "\n", encoding="utf-8")
    logger.info("交接已折 branch=%s path=%s", branch, path)
    return path


def handoff_notice(directory: Path, cwd: str | Path) -> str:
    body = load_handoff(directory, cwd)
    if not body:
        return ""
    return get_prompt("handoff_inject", body=body, branch=git_branch(cwd))


def workspace_cwd(directory: Path) -> Path | None:
    marker = directory / ".workspace"
    if not marker.is_file():
        return None
    raw = marker.read_text(encoding="utf-8").strip()
    return Path(raw) if raw else None


def _without_stale_sandbox(text: str) -> str:
    """旧策略结论不能当成本轮权限。"""
    kept: list[str] = []
    for block in re.split(r"\n\s*\n", text or ""):
        chunk = block.strip()
        if not chunk or _STALE_SANDBOX.search(chunk):
            continue
        kept.append(chunk)
    return "\n\n".join(kept)


def _clip(text: str, *, limit: int = 220) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
