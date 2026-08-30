"""同步世界调异步入口的唯一桥。

`AGENTS.md`「并发：能异步就异步」要求对外同时给 `afoo` / `foo` 两个入口，`foo` 只做
`asyncio.run(afoo(...))`。这里把那层包装收成一处，顺便堵住「已经在事件循环里还调同步版」
——`asyncio.run` 在运行中的循环里会直接抛，而更早的写法是退回去调 `llm._request()` 那种
同步阻塞旁路，现场只看得到整个 agent 卡住，查不出原因。宁可报错也不要静默卡死。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

from witty_agent.prompts import get_prompt

T = TypeVar("T")


def in_event_loop() -> bool:
    """当前线程是否有运行中的事件循环。

    用来让同步代码里的「顺手喊一次模型」那种分支自己退让，而不是把循环堵住。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_sync(coro: Coroutine[Any, Any, T], *, entry: str) -> T:
    """在没有事件循环的线程里跑协程；已经在循环里就报错，指名该换哪个异步入口。"""
    if in_event_loop():
        coro.close()  # 不关会留一条 "coroutine was never awaited" 警告，掩盖真正的报错
        raise RuntimeError(get_prompt("async_sync_in_loop", entry=entry))
    return asyncio.run(coro)
