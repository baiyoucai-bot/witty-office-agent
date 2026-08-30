"""持久 Python 解释器：工具调用之间变量不清空，结果可当变量续用。

`bash` 和 `exec_command` 每次都是新进程，中间结果只能落盘再读回来。于是「读一万行 → 筛出
三十行」这件事，要么把一万行塞进上下文，要么写临时文件再 `read` 一遍。`spill` 解决了前者
（原文不进上下文），但没解决后者：拿到的是一个**路径**，要接着算还得重新读进来。

这里给的是第三条路：结果留在一个**活着的解释器**里，下一个单元格直接对变量做下一步操作。
上下文里只出现你主动打印的那几行。

三条语义上的硬要求，都是「宁可承认状态没了，也不能假装还在」：

1. **帧要靠哨兵，不靠等待时间。** 每个单元格执行完，驱动打一行带随机 nonce 的结束标记；
   父进程读到标记才算这一格结束。靠「等 N 毫秒」猜的话，慢一点的单元格会被读成空输出，
   而它的输出会串到下一格头上。nonce 让单元格没法自己打印一个假标记糊弄框架。
2. **超时先中断，不先杀。** 命名空间就是这个工具存在的理由，为一次超时把它清掉代价太大。
   所以先 SIGINT（驱动把 `KeyboardInterrupt` 当普通单元格错误接住），进程活下来、变量还在；
   只有连中断都不理才升级到 kill。
3. **状态丢了必须明说。** 进程死了之后静默重启一个空解释器是最坏的情况：模型以为 `df`
   还在，后面每一步都建在幻觉上。所以重启一定带一句「变量没了」。
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.runtime import repl_settings

logger = get_logger("repl")

CELL_END = "__WITTY_CELL_END__"
DRIVER_NAME = "_witty_repl_driver.py"
_GRACE_SEC = 5.0
_POLL_SEC = 0.02
_READ_CHUNK = 65536

# 这是**程序**，不是提示词：它从头到尾只在沙箱解释器里跑，一个字都不会发给模型。
# 发给模型的每句话都在 config/prompts.toml。
_DRIVER = '''\
import ast
import os
import signal
import sys
import traceback

NONCE = os.environ["WITTY_REPL_NONCE"]
END = os.environ["WITTY_REPL_END"]
NS = {"__name__": "__witty_repl__"}

if hasattr(signal, "SIGBREAK"):
    # Windows 没有跨进程 SIGINT；父进程超时打的是 CTRL_BREAK。默认动作是整个进程退出、
    # 变量全没——转成 KeyboardInterrupt 让下面的 BaseException 接住，语义就和 POSIX 一致。
    def _witty_break(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGBREAK, _witty_break)


def run(code):
    """按 REPL 语义跑：最后一句是表达式就回显它的 repr，并绑到 `_`。"""
    block = ast.parse(code, "<cell>", "exec")
    tail = None
    if block.body and isinstance(block.body[-1], ast.Expr):
        tail = ast.Expression(block.body.pop().value)
    if block.body:
        exec(compile(block, "<cell>", "exec"), NS)
    if tail is not None:
        value = eval(compile(tail, "<cell>", "eval"), NS)
        if value is not None:
            NS["_"] = value
            sys.stdout.write(repr(value) + "\\n")


seq = 0
while True:
    header = sys.stdin.readline()
    if not header:
        break
    try:
        size = int(header.strip())
    except ValueError:
        continue
    code = sys.stdin.read(size)
    seq += 1
    status = "ok"
    try:
        run(code)
    except BaseException:
        # 连 KeyboardInterrupt 一起接住：超时是父进程打进来的 SIGINT，那一格该算失败，
        # 但解释器要活下来，否则命名空间跟着没了。
        kind, error, tb = sys.exc_info()
        # 掐掉驱动自己那几帧。带着框架的文件名和行号回去，模型会以为是框架崩了而不是
        # 它这一格写错了，于是去查一个不存在的问题。语法错误没有 <cell> 帧，tb 归 None，
        # 异常自己带位置信息，照样够看。
        while tb is not None and tb.tb_frame.f_code.co_filename != "<cell>":
            tb = tb.tb_next
        traceback.print_exception(kind, error, tb, file=sys.stdout)
        status = "error"
    sys.stdout.write("\\n%s %s %d %s\\n" % (END, NONCE, seq, status))
    sys.stdout.flush()
'''


@dataclass
class CellResult:
    status: str
    output: str
    restarted: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class ReplProcess:
    """一个活着的解释器。死了就不复用，由 `ReplHost` 重新拉起。"""

    proc: subprocess.Popen[bytes]
    nonce: str
    _chunks: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    cells: int = 0

    def __post_init__(self) -> None:
        threading.Thread(target=self._pump, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def _pump(self) -> None:
        """`os.read` 有多少读多少，不等凑满一块，也不等换行。

        逐字符读会把一兆输出变成一百万次系统调用；按行读则会让「打了半行然后卡住」的单元格
        在超时诊断里完全看不到已经打出来的那半行。
        """
        stream = self.proc.stdout
        if stream is None:
            return
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            with self._lock:
                self._chunks.append(text)

    def _drain(self) -> str:
        """取出并清空缓冲。一格一清，所以不需要游标，也没有环形缓冲对不上偏移的问题。"""
        with self._lock:
            text = "".join(self._chunks)
            self._chunks.clear()
            return text

    def send(self, code: str) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("stdin 已关闭")
        # 先长度再正文。单元格里可以有任意换行和任意内容，按分隔符切迟早撞上；长度是字符数，
        # 驱动那侧 `sys.stdin.read(size)` 读的也是字符，所以非 ASCII 不会错位。
        self.proc.stdin.write(f"{len(code)}\n{code}".encode())
        self.proc.stdin.flush()
        self.cells += 1

    def read_cell(self, timeout_sec: float) -> tuple[str, str | None]:
        """读到本格结束标记为止。返回 (输出, 状态)；状态 None 表示没读到。"""
        marker = f"{CELL_END} {self.nonce} "
        buffer = ""
        deadline = time.monotonic() + max(0.05, timeout_sec)
        while True:
            buffer += self._drain()
            index = buffer.find(marker)
            if index >= 0:
                line_end = buffer.find("\n", index)
                if line_end >= 0:
                    fields = buffer[index + len(marker) : line_end].split()
                    return buffer[:index], (fields[-1] if fields else "ok")
            if not self.alive:
                # 进程没了，把管道里剩的收干净再回。
                time.sleep(_POLL_SEC)
                return buffer + self._drain(), None
            if time.monotonic() >= deadline:
                return buffer + self._drain(), None
            time.sleep(_POLL_SEC)

    def interrupt(self) -> None:
        self._signal_group(signal.SIGINT)

    def close(self) -> None:
        if self.alive:
            self._signal_group(signal.SIGTERM)
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # Windows 没有 SIGKILL；_signal_group 的 nt 分支对终止类信号一律硬杀
                self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
                self.proc.wait(timeout=1)
        # 管道要显式关。进程死了但 fd 还挂着的话，长会话反复重启解释器就会漏 fd。
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                continue

    def _signal_group(self, sig: signal.Signals) -> None:
        if os.name == "nt":
            # Windows 没有 killpg：中断发 CTRL_BREAK（spawn 时开了新进程组，驱动把它转成
            # KeyboardInterrupt），终止类直接硬杀。
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


def driver_path(work: Path) -> Path:
    """驱动落在沙箱工作区。内容变了才写，免得每次拉起都动一次盘。"""
    path = work / DRIVER_NAME
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if current != _DRIVER:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DRIVER, encoding="utf-8")
    return path


def spawn(*, workspace: str, root: Path | None = None) -> ReplProcess:
    """在沙箱解释器里拉起驱动。cwd 取工作区，相对路径跟 read / bash 一个含义。"""
    from witty_agent.sandbox import apply_exec_env, ensure_sandbox
    from witty_agent.vault import bound_vault

    snap = ensure_sandbox(workspace=workspace, root=root)
    nonce = secrets.token_hex(8)
    env = apply_exec_env(os.environ.copy(), workspace=workspace, root=root)
    env.update(bound_vault())
    env["WITTY_REPL_NONCE"] = nonce
    env["WITTY_REPL_END"] = CELL_END
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [str(snap.python), "-u", str(driver_path(snap.work))],
        cwd=workspace,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        # Windows 下 CTRL_BREAK 只能打给独立进程组；POSIX 上取 0 等于没传
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    logger.info("解释器启动 pid=%s work=%s", proc.pid, snap.work)
    return ReplProcess(proc=proc, nonce=nonce)


class ReplHost:
    """管一个解释器的生死。会话级单例，由 `hooks.repl_host` 持有。"""

    def __init__(self, *, workspace: str, root: Path | None = None) -> None:
        self.workspace = workspace
        self.root = root
        self.process: ReplProcess | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self.process is not None:
                self.process.close()
                self.process = None

    def status(self) -> str:
        process = self.process
        if process is None or not process.alive:
            return get_prompt("repl_status_down")
        return get_prompt("repl_status_up", cells=str(process.cells))

    def run(self, code: str, *, timeout_sec: float = 0.0, restart: bool = False) -> CellResult:
        settings = repl_settings()
        limit = float(timeout_sec or settings["timeout_sec"])
        max_chars = int(settings["max_output_chars"])
        with self._lock:
            # 上一格之后进程自己死了（被杀、段错误、os._exit）。这一格换个新解释器照样能跑成
            # `status=ok`，而变量已经全没了——这是最坏的一种成功，必须在输出最前面说清楚，
            # 否则模型拿着一句 ok 继续用不存在的 df。
            crashed = self.process is not None and not self.process.alive
            if crashed or (restart and self.process is not None):
                self.process.close()
                self.process = None
            fresh = self.process is None
            if fresh:
                self.process = spawn(workspace=self.workspace, root=self.root)
            result = self._cell(self.process, code, limit, fresh, max_chars)
        if crashed and result.status != "dead":
            result.output = f"{get_prompt('repl_state_lost')}\n{result.output}"
            result.restarted = True
        return result

    def _cell(
        self,
        process: ReplProcess,
        code: str,
        limit: float,
        fresh: bool,
        max_chars: int,
    ) -> CellResult:
        try:
            process.send(code)
        except (RuntimeError, OSError):
            process.close()
            self.process = None
            return CellResult("dead", get_prompt("repl_state_lost"), restarted=True)
        output, status = process.read_cell(limit)
        if status is None:
            return self._recover(process, output, limit, max_chars)
        return CellResult(status, _clip(output, max_chars), restarted=fresh)

    def _recover(self, process: ReplProcess, partial: str, limit: float, max_chars: int) -> CellResult:
        """读不到结束标记只有两种可能：进程死了，或者这一格还在跑。"""
        if not process.alive:
            logger.info("解释器意外退出 code=%s", process.proc.poll())
            process.close()
            self.process = None
            body = _clip(partial, max_chars)
            return CellResult("dead", f"{body}\n{get_prompt('repl_state_lost')}".strip(), restarted=True)
        logger.info("单元格超时，先中断 timeout=%.1fs", limit)
        process.interrupt()
        rest, status = process.read_cell(_GRACE_SEC)
        body = _clip(f"{partial}{rest}", max_chars)
        if status is not None:
            # 中断被接住了：这一格算失败，但解释器活着、变量还在。
            return CellResult("timeout", f"{body}\n{get_prompt('repl_timeout_kept', timeout_sec=f'{limit:g}')}".strip())
        process.close()
        self.process = None
        note = get_prompt("repl_timeout_killed", timeout_sec=f"{limit:g}")
        return CellResult("dead", f"{body}\n{note}".strip(), restarted=True)


def _clip(text: str, max_chars: int) -> str:
    body = (text or "").strip("\n")
    if not body:
        return get_prompt("repl_no_output")
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    # 留尾巴：报错和最后打印的结果都在末尾，掐头比掐尾有用。
    kept = body[-max_chars:]
    return get_prompt("repl_output_capped", shown=str(len(kept)), total=str(len(body))) + "\n" + kept
