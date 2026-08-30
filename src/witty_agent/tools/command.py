"""后台长命令：exec_command 超时未结束则挂起，input_command 续写/轮询。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from witty_agent import hooks
from witty_agent.logging import get_logger
from witty_agent.tools.fs import _safe_path, _workspace
from witty_agent.tools.registry import tool
from witty_agent.vault import bound_vault

logger = get_logger("tools.command")
DEFAULT_EXEC_YIELD_MS = 60_000
DEFAULT_WRITE_YIELD_MS = 250
DEFAULT_EMPTY_POLL_YIELD_MS = 5_000
OUTPUT_CAP = 1024 * 1024
MAX_SESSIONS = 64
INTERRUPT = "\x03"
HARDENED = {
    "GIT_EDITOR": "true",
    "GIT_TERMINAL_PROMPT": "0",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}
STRIPPED = {"PORT", "HOST", "FORCE_COLOR", "CLICOLOR_FORCE", "BASH_ENV", "ENV"}


@dataclass
class ManagedSession:
    process_id: str
    cmd: str
    cwd: str
    proc: subprocess.Popen[str]
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    _chunks: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        thread = threading.Thread(target=self._pump, daemon=True)
        thread.start()

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()

    def collect(self, yield_ms: int) -> str:
        self.last_used = time.time()
        deadline = time.monotonic() + max(yield_ms, 1) / 1000
        before = self._snapshot()
        while time.monotonic() < deadline:
            if not self.running:
                time.sleep(0.05)
                break
            time.sleep(0.02)
        after = self._snapshot()
        return after[len(before) :]

    def write(self, chars: str) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("stdin 已关闭")
        self.proc.stdin.write(chars)
        self.proc.stdin.flush()
        self.last_used = time.time()

    def interrupt(self) -> None:
        self._signal(signal.SIGINT)

    def kill(self) -> None:
        if not self.running:
            return
        self._signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # Windows 没有 SIGKILL；_signal 的 nt 分支对终止类信号一律硬杀
            self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def _signal(self, sig: signal.Signals) -> None:
        if os.name == "nt":
            # Windows 没有 killpg：中断发 CTRL_BREAK（spawn 时开了新进程组），终止类直接硬杀
            try:
                if sig == signal.SIGINT:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.kill()
            except (ProcessLookupError, OSError, ValueError):
                return
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                return

    def _pump(self) -> None:
        if self.proc.stdout is None:
            return
        try:
            while True:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                with self._lock:
                    self._chunks.append(chunk)
                    text = "".join(self._chunks)
                    if len(text) > OUTPUT_CAP:
                        self._chunks = [text[-OUTPUT_CAP:]]
        except OSError:
            return

    def _snapshot(self) -> str:
        with self._lock:
            return "".join(self._chunks)


class CommandSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ManagedSession] = {}
        self._next = 1

    def spawn(self, cmd: str, cwd: str) -> ManagedSession:
        from witty_agent.sandbox import apply_exec_env, bash_argv, check_command_paths, rewrite_sandbox_tokens

        workspace = _workspace()
        jail_cwd = None if Path(cwd).resolve() == Path(workspace).resolve() else cwd
        check_command_paths(workspace, cmd, cwd=jail_cwd)
        env = {key: value for key, value in os.environ.items() if key.upper() not in STRIPPED}
        env.update(HARDENED)
        env = apply_exec_env(env)
        env.update(bound_vault())
        launched = rewrite_sandbox_tokens(cmd)
        proc = subprocess.Popen(
            bash_argv(launched),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            # Windows 下 CTRL_BREAK 只能打给独立进程组；POSIX 上取 0 等于没传
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return ManagedSession(process_id="", cmd=cmd, cwd=cwd, proc=proc)

    def register(self, session: ManagedSession) -> str:
        self._evict()
        process_id = f"proc-{self._next:03d}"
        self._next += 1
        session.process_id = process_id
        self._sessions[process_id] = session
        logger.info("后台命令挂起 id=%s cmd=%s", process_id, session.cmd[:80])
        return process_id

    def get(self, process_id: str) -> ManagedSession | None:
        session = self._sessions.get(process_id)
        if session and not session.running:
            return session
        return session

    def pop_if_exited(self, process_id: str) -> ManagedSession | None:
        session = self._sessions.get(process_id)
        if session and not session.running:
            self._sessions.pop(process_id, None)
        return session

    def list(self) -> list[ManagedSession]:
        return list(self._sessions.values())

    def _evict(self) -> None:
        dead = [key for key, item in self._sessions.items() if not item.running]
        for key in dead:
            self._sessions.pop(key, None)
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda item: item.last_used)
            oldest.kill()
            self._sessions.pop(oldest.process_id, None)


def _manager() -> CommandSessionManager:
    manager = hooks.command_manager
    if manager is None:
        manager = CommandSessionManager()
        hooks.command_manager = manager
    return manager


def _yield_ms(raw: int, default: int) -> int:
    if raw <= 0:
        return default
    return min(raw, 3600_000)


@tool
def exec_command(cmd: str, workdir: str = "", yield_time_ms: int = DEFAULT_EXEC_YIELD_MS) -> str:
    """启动一条命令。yield 时间内结束就返回退出码；否则挂到后台并给出 process_id。危险操作，必须先批准。

    Args:
        cmd: 交给 bash -lc 的命令
        workdir: 相对工作区的目录，空则用工作区根
        yield_time_ms: 最多等待毫秒数，超时未结束则转后台
    """
    root = _workspace()
    cwd = str(_safe_path(root, workdir or "."))
    session = _manager().spawn(cmd, cwd)
    output = session.collect(_yield_ms(yield_time_ms, DEFAULT_EXEC_YIELD_MS))
    if session.running:
        process_id = _manager().register(session)
        note = f"[process running with process_id {process_id}; use input_command to send input or poll]"
        return f"{output}{note}" if output else note
    from witty_agent.fs_observe import changed_notice

    code = session.exit_code
    note = changed_notice(root)
    body = f"{output}exit={code}" if output else f"exit={code}"
    return f"{body}{note}"


@tool
def input_command(process_id: str, chars: str = "", yield_time_ms: int = 0) -> str:
    """向后台命令写 stdin 或轮询输出。单独传入 \\x03 发送 SIGINT。危险操作，必须先批准。

    Args:
        process_id: exec_command 返回的进程 id
        chars: 写入 stdin 的字符；空则只轮询；单独的 \\x03 表示中断
        yield_time_ms: 等待新输出的毫秒数
    """
    session = _manager().get(process_id)
    if session is None:
        return f"unknown process_id {process_id}"
    if INTERRUPT in chars:
        if chars != INTERRUPT:
            return "\\x03 must be sent alone"
        session.interrupt()
        default = DEFAULT_WRITE_YIELD_MS
    elif chars:
        session.write(chars)
        default = DEFAULT_WRITE_YIELD_MS
    else:
        default = DEFAULT_EMPTY_POLL_YIELD_MS
    output = session.collect(_yield_ms(yield_time_ms, default))
    if session.running:
        return f"{output}[still running process_id {process_id}]"
    _manager().pop_if_exited(process_id)
    from witty_agent.fs_observe import changed_notice

    return f"{output}exit={session.exit_code}{changed_notice(_workspace())}"


@tool
def list_commands() -> str:
    """列出当前会话里仍在管理的后台命令。"""
    rows = _manager().list()
    if not rows:
        return "(no command sessions)"
    lines = []
    for item in rows:
        state = "running" if item.running else f"exit={item.exit_code}"
        lines.append(f"{item.process_id}\t{state}\t{item.cmd}")
    return "\n".join(lines)
