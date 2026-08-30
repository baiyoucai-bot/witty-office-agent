"""持久 Python 解释器工具。会话级一个进程，变量跨调用留着。"""

from __future__ import annotations

from witty_agent import hooks
from witty_agent.logging import get_logger
from witty_agent.tools.fs import _workspace
from witty_agent.tools.registry import tool

logger = get_logger("tools.repl")


def _host():
    from witty_agent.repl import ReplHost

    workspace = _workspace()
    host = hooks.repl_host
    # 工作区换了就换解释器：cwd 和相对路径的含义跟着工作区走，沿用旧进程会让路径悄悄指错地方。
    if host is None or host.workspace != workspace:
        if host is not None:
            host.close()
        host = ReplHost(workspace=workspace, root=hooks.current_root)
        hooks.repl_host = host
    return host


@tool
def python_repl(code: str, timeout: int = 0, restart: bool = False) -> str:
    """在长驻的沙箱 Python 解释器里执行代码，变量在多次调用之间保留。危险操作，必须先批准。

    Args:
        code: 要执行的 Python 代码，可多行。最后一句是表达式就回显它的值并绑到 `_`
        timeout: 本格超时秒数，0 用配置默认值。超时先中断，变量通常还在
        restart: true 表示先丢掉当前解释器，从空白状态重新开始
    """
    if timeout < 0 or timeout > 3600:
        raise ValueError("timeout 必须在 0..3600 秒")
    if not (code or "").strip() and not restart:
        raise ValueError("code 不能为空")
    result = _host().run(code, timeout_sec=float(timeout), restart=restart)
    logger.info("单元格完成 status=%s restarted=%s", result.status, result.restarted)
    return f"status={result.status}\n{result.output}"


@tool
def python_repl_status() -> str:
    """查看长驻 Python 解释器在不在、已经跑过几格。"""
    return _host().status()
