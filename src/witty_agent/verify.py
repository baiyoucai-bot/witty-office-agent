"""完成判据的客观那一半：shell gate 的退出码、工作树指纹、回归义务台账。

模型说「做完了」不算判据，命令的退出码才算。三件事：

1. **gate**：一条 shell 命令 + 退出码。0 就是过，非 0 就是没过，不经过任何模型。
2. **工作树指纹**：失败的 gate 在工作区没变之前不重跑。同一棵字节相同的树跑第二遍
   `pytest` 只会烧掉同样的时间打印同样的红字。
3. **回归义务**：过了的判据登记下来，之后每一轮全部重跑。LoopsBench 的实测是长任务栽在
   「守住已完成的部分」而不是「写出下一块」——它量到的四种 loop 全都还有回归事件。

gate 命令来自调用方与配置（`[goal]`、`run_goal(gates=...)`），**不来自模型**。这是
`shell=True` 能成立的前提：它等价于运维自己写的 `npm run check`，不是一条可以被模型
构造出来的命令。哪天要让模型提出 gate，必须先过 `bash` 那条 always-ask 审批，不能从这里绕。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt

logger = get_logger("verify")

GATE_OUTPUT_LIMIT = 4096
_GIT_TIMEOUT_SEC = 20
_FINGERPRINT_MAX_FILES = 4000
_FINGERPRINT_MAX_BYTES = 1 << 20
# 指纹不进这些目录：装出来的东西和缓存不是「gate 能读到的源」，
# 而且 .venv / node_modules 能把一次指纹拖成几万个 stat。
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".idea",
        ".DS_Store",
    }
)


def _git(workspace: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
        )
    except Exception:
        return None
    return done.stdout if done.returncode == 0 else None


def _absorb_file(path: Path, digest: "hashlib._Hash") -> None:
    try:
        if not path.is_file():
            return
        raw = path.read_bytes()[:_FINGERPRINT_MAX_BYTES]
    except OSError:
        return
    digest.update(path.name.encode("utf-8", "replace"))
    digest.update(hashlib.sha256(raw).digest())


def _git_fingerprint(workspace: Path, digest: "hashlib._Hash") -> bool:
    if (_git(workspace, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return False
    digest.update((_git(workspace, "rev-parse", "HEAD") or "no-head").strip().encode())
    # `git status --porcelain` 单看是不够的：改一个已经是 "M" 的文件，状态行一模一样。
    # 内容盲的指纹会把真改动看成没改动，于是跳过一个现在本来会过的 gate。
    digest.update((_git(workspace, "diff", "HEAD") or "").encode("utf-8", "replace"))
    status = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all") or ""
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        name = line[3:].strip().strip('"')
        if not name or _skipped_path(name):
            continue
        _absorb_file(workspace / name, digest)
    return True


def _skipped_path(relative: str) -> bool:
    """True for build output and caches, which are not sources a gate reads.

    A repo that forgets to gitignore `__pycache__` would otherwise have `pytest` move the
    fingerprint by running, and a fingerprint the gate itself perturbs can never cache.
    """
    return any(part in _SKIP_DIRS for part in Path(relative).parts)


def _walk_fingerprint(workspace: Path, digest: "hashlib._Hash") -> None:
    seen = 0
    for current, dirs, files in os.walk(workspace):
        dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS)
        base = Path(current)
        for name in sorted(files):
            if name in _SKIP_DIRS:
                continue
            if seen >= _FINGERPRINT_MAX_FILES:
                digest.update(b"truncated")
                return
            try:
                info = (base / name).stat()
            except OSError:
                continue
            rel = (base / name).relative_to(workspace)
            digest.update(f"{rel}:{info.st_size}:{info.st_mtime_ns}".encode("utf-8", "replace"))
            seen += 1


def worktree_fingerprint(workspace: Path) -> str:
    """A digest that moves exactly when something a gate could read has moved.

    Git when available (HEAD + the whole tracked diff + the bytes of every untracked file),
    otherwise a bounded walk over size and mtime. Two different trees are allowed to collide
    only in the direction that costs correctness nothing: a collision re-uses a cached
    failure, and the caller re-runs everything that passes anyway.
    """
    digest = hashlib.sha256()
    if not _git_fingerprint(workspace, digest):
        _walk_fingerprint(workspace, digest)
    return digest.hexdigest()


@dataclass(frozen=True)
class GateSpec:
    """One objective completion criterion: run this, exit 0 means satisfied."""

    name: str
    command: str
    timeout_sec: int = 300


@dataclass
class GateResult:
    name: str
    ok: bool
    exit_code: int = 0
    output: str = ""
    skipped: bool = False

    def line(self) -> str:
        return get_prompt(
            "gate_result_line",
            name=self.name,
            exit_code=str(self.exit_code),
            output=self.output or "-",
        )


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.results)

    def failures(self) -> list[GateResult]:
        return [item for item in self.results if not item.ok]

    def passed(self) -> list[GateResult]:
        return [item for item in self.results if item.ok]


@dataclass
class GateRunner:
    """Runs shell gates, re-using a known failure while the worktree stands still.

    A failed gate cannot start passing until something it reads changes, so the previous
    verdict is kept until the fingerprint moves. Passing gates are always re-run — that is
    the regression check, and it is the half that must not be cached.
    """

    workspace: Path
    _failed: dict[str, tuple[str, GateResult]] = field(default_factory=dict)

    def run(self, specs: Sequence[GateSpec]) -> GateReport:
        if not specs:
            return GateReport()
        before = worktree_fingerprint(self.workspace)
        results: list[GateResult] = []
        ran = False
        for spec in specs:
            cached = self._failed.get(spec.name)
            if cached is not None and cached[0] == before:
                logger.info("gate 跳过：工作区未变 name=%s", spec.name)
                results.append(replace(cached[1], skipped=True))
                continue
            ran = True
            results.append(self._execute(spec))
        # 缓存键必须是**跑完之后**的指纹。gate 自己会往工作区写东西——覆盖率文件、日志、
        # 没被 gitignore 的缓存目录。拿开跑前的指纹当键，下一轮开跑前一比就不一样，
        # 于是缓存永远命中不了，红着的 gate 每轮照跑，这个机制等于没有。
        after = worktree_fingerprint(self.workspace) if ran else before
        for result in results:
            # 这一轮跳过的失败也重新按 after 记：整张缓存表描述的是同一个树状态，
            # 否则它们下一轮会因为别的 gate 的副作用被无谓叫醒。
            if result.ok:
                self._failed.pop(result.name, None)
            else:
                self._failed[result.name] = (after, result)
        return GateReport(results=results, fingerprint=after)

    def _execute(self, spec: GateSpec) -> GateResult:
        try:
            done = subprocess.run(
                spec.command,
                shell=True,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=max(1, spec.timeout_sec),
            )
        except subprocess.TimeoutExpired:
            logger.info("gate 超时 name=%s timeout=%s", spec.name, spec.timeout_sec)
            return GateResult(
                name=spec.name,
                ok=False,
                exit_code=-1,
                output=get_prompt("gate_timeout", name=spec.name, timeout_sec=str(spec.timeout_sec)),
            )
        except Exception as exc:
            logger.warning("gate 无法执行 name=%s err=%s", spec.name, exc)
            return GateResult(name=spec.name, ok=False, exit_code=-1, output=str(exc))
        merged = f"{done.stdout or ''}{done.stderr or ''}".strip()
        logger.info("gate 完成 name=%s exit=%s", spec.name, done.returncode)
        return GateResult(
            name=spec.name,
            ok=done.returncode == 0,
            exit_code=done.returncode,
            output=merged[-GATE_OUTPUT_LIMIT:],
        )


@dataclass(frozen=True)
class Obligation:
    """A criterion that has already been proven once, and must stay proven."""

    name: str
    command: str
    criterion: str = ""
    recorded_at: str = ""
    timeout_sec: int = 300

    def spec(self) -> GateSpec:
        return GateSpec(name=self.name, command=self.command, timeout_sec=self.timeout_sec)


class ObligationLedger:
    """Append-only table of proven criteria, re-run on every later verification.

    Append-only on purpose: same shape as the session log, so it replays, and a later entry
    for a name simply supersedes the earlier one. Nothing is ever deleted, so which command
    proved a criterion, and when, stays answerable after the fact.
    """

    filename = "done_criteria.jsonl"

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / self.filename

    def record(self, obligation: Obligation) -> None:
        stamped = (
            obligation
            if obligation.recorded_at
            else replace(obligation, recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stamped.__dict__, ensure_ascii=False) + "\n")
        logger.info("回归义务登记 name=%s", stamped.name)

    def load(self) -> list[Obligation]:
        if not self.path.is_file():
            return []
        latest: dict[str, Obligation] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError:
                continue
            name = str(row.get("name") or "").strip()
            command = str(row.get("command") or "").strip()
            if not name or not command:
                continue
            latest[name] = Obligation(
                name=name,
                command=command,
                criterion=str(row.get("criterion") or ""),
                recorded_at=str(row.get("recorded_at") or ""),
                timeout_sec=int(row.get("timeout_sec") or 300),
            )
        return list(latest.values())

    def names(self) -> set[str]:
        return {item.name for item in self.load()}


def merge_specs(obligations: Sequence[Obligation], gates: Sequence[GateSpec]) -> list[GateSpec]:
    """Obligations plus configured gates, deduped by name with the gate winning.

    The gate carries the authoritative command and timeout for this run; the ledger row is
    only a record that the name once passed. Same name must not run twice in one round.
    """
    merged: dict[str, GateSpec] = {item.name: item.spec() for item in obligations}
    for spec in gates:
        merged[spec.name] = spec
    return list(merged.values())
