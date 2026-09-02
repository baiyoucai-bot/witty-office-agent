"""serve 的进程活性：pidfile、心跳文件、后台启动 / 停止 / 状态。

`witty-agent serve` 默认前台跑，终端一关就没了。这里补三样基础件：

* **pidfile** `WITTY_HOME/serve/serve.pid`：记 pid + host + port + 启动时间，避免同一 WITTY_HOME 起两份。
* **心跳** `WITTY_HOME/serve/heartbeat.json`：serve 进程每 `interval` 秒刷一次时间戳。
  `status` 靠它区分「进程活着但卡死」和「正常」——pid 存在只证明没退出，
  心跳过期才说明主循环不动了。
* **后台化** `serve --daemon`：POSIX 双 fork 脱离终端，stdout/stderr 重定向到
  `WITTY_HOME/serve/serve.log`；Windows 用 DETACHED_PROCESS 起子进程。
  `serve --stop` 按 pidfile 发 SIGTERM（Windows 走 taskkill），`serve --status` 打印活性。

不做 systemd/launchd 单元、不做崩溃自动拉起：那是部署侧的事，
这里只保证「脱离终端能活、活没活能查、想停能停」。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from witty_agent.layout import data_root
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt

logger = get_logger("daemon")

HEARTBEAT_INTERVAL_SEC = 5.0
# 心跳超过 3 个周期没刷就算失联：单次 GC 停顿或磁盘抖动不至于误判
HEARTBEAT_STALE_SEC = HEARTBEAT_INTERVAL_SEC * 3


def serve_dir(root: Path | None = None) -> Path:
    path = (root or data_root()) / "serve"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_path(root: Path | None = None) -> Path:
    return serve_dir(root) / "serve.pid"


def heartbeat_path(root: Path | None = None) -> Path:
    return serve_dir(root) / "heartbeat.json"


def log_path(root: Path | None = None) -> Path:
    return serve_dir(root) / "serve.log"


@dataclass
class ServeStatus:
    running: bool
    pid: int | None
    host: str
    port: int
    started_at: float | None
    heartbeat_at: float | None
    stale: bool

    @property
    def uptime_sec(self) -> float:
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pidfile(root: Path | None = None) -> dict[str, object]:
    path = pid_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_pidfile(host: str, port: int, *, root: Path | None = None) -> None:
    payload = {"pid": os.getpid(), "host": host, "port": port, "started_at": time.time()}
    pid_path(root).write_text(json.dumps(payload), encoding="utf-8")


def clear_pidfile(root: Path | None = None) -> None:
    current = read_pidfile(root)
    # 只清自己写的；别把新起的那份删掉
    if current and int(current.get("pid") or 0) not in (0, os.getpid()):
        return
    pid_path(root).unlink(missing_ok=True)
    heartbeat_path(root).unlink(missing_ok=True)


def write_heartbeat(root: Path | None = None) -> None:
    payload = {"pid": os.getpid(), "at": time.time()}
    path = heartbeat_path(root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def read_heartbeat(root: Path | None = None) -> float | None:
    path = heartbeat_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("at"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


class Heartbeat:
    """后台线程按周期刷心跳文件。serve 主循环里起一份，退出时 stop。"""

    def __init__(self, root: Path | None = None, interval: float = HEARTBEAT_INTERVAL_SEC) -> None:
        self.root = root
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        write_heartbeat(self.root)
        self._thread = threading.Thread(target=self._loop, name="witty-heartbeat", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                write_heartbeat(self.root)
            except OSError as exc:
                logger.warning("心跳写入失败 err=%s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def status(root: Path | None = None) -> ServeStatus:
    info = read_pidfile(root)
    pid = int(info.get("pid") or 0) or None
    alive = bool(pid and _pid_alive(pid))
    beat = read_heartbeat(root)
    stale = bool(alive and (beat is None or time.time() - beat > HEARTBEAT_STALE_SEC))
    return ServeStatus(
        running=alive,
        pid=pid if alive else None,
        host=str(info.get("host") or ""),
        port=int(info.get("port") or 0),
        started_at=float(info.get("started_at")) if alive and info.get("started_at") else None,
        heartbeat_at=beat if alive else None,
        stale=stale,
    )


def render_status(state: ServeStatus) -> str:
    if not state.running:
        return get_prompt("daemon_status_stopped")
    if state.stale:
        return get_prompt(
            "daemon_status_stale",
            pid=str(state.pid),
            host=state.host,
            port=str(state.port),
            age=f"{time.time() - (state.heartbeat_at or 0):.0f}",
        )
    return get_prompt(
        "daemon_status_running",
        pid=str(state.pid),
        host=state.host,
        port=str(state.port),
        uptime=f"{state.uptime_sec / 60:.1f}",
    )


def stop(root: Path | None = None, *, timeout: float = 10.0) -> int:
    """按 pidfile 停掉 serve。返回退出码：0 已停，1 没在跑，2 停不掉。"""
    state = status(root)
    if not state.running or not state.pid:
        print(get_prompt("daemon_stop_not_running"))
        return 1
    pid = state.pid
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            clear_pidfile(root)
            print(get_prompt("daemon_stop_not_running"))
            return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            pid_path(root).unlink(missing_ok=True)
            heartbeat_path(root).unlink(missing_ok=True)
            print(get_prompt("daemon_stop_ok", pid=str(pid)))
            return 0
        time.sleep(0.2)
    print(get_prompt("daemon_stop_timeout", pid=str(pid)))
    return 2


def _already_running(root: Path | None) -> ServeStatus | None:
    state = status(root)
    return state if state.running else None


def spawn_background(host: str, port: int, *, root: Path | None = None) -> int:
    """脱离终端起 serve。返回退出码：0 起了，1 已在跑。"""
    running = _already_running(root)
    if running:
        print(get_prompt("daemon_already_running", pid=str(running.pid), port=str(running.port)))
        return 1
    log_file = log_path(root)
    argv = [sys.executable, "-m", "witty_agent", "serve", "--host", host, "--port", str(port), "--foreground"]
    env = dict(os.environ)
    if root is not None:
        env["WITTY_HOME"] = str(root)
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        with log_file.open("ab") as out:
            proc = subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, creationflags=flags)
        pid = proc.pid
    else:
        pid = os.fork()
        if pid == 0:
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.execve(argv[0], argv, env)
        os.waitpid(pid, 0)
        pid = 0
    # 等 pidfile 落盘再报，最多 8 秒；报的是 serve 自己写的 pid，不是中间那层 fork 的
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        state = status(root)
        if state.running:
            print(get_prompt("daemon_started", pid=str(state.pid), host=host, port=str(port), log=str(log_file)))
            return 0
        time.sleep(0.2)
    print(get_prompt("daemon_start_timeout", log=str(log_file)))
    return 2
