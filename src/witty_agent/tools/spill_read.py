"""按 spill id 取回被有损压缩的工具原文。"""

from __future__ import annotations

import os
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.spill import resolve_spill
from witty_agent.tools.registry import tool


@tool
def spill_read(locator: str, offset: int = 0, limit: int = 8000) -> str:
    """读取 spill 落盘的完整工具输出。locator 为 spill:会话:调用 或文件名。

    Args:
        locator: spill:session:call 或 spills 目录下的文件名
        offset: 起始字节
        limit: 最多返回多少字节，默认 8000
    """
    raw = os.environ.get("WITTY_SCRATCHPAD")
    if not raw:
        raise RuntimeError(get_prompt("spill_read_no_scratch"))
    text = resolve_spill(Path(raw), locator)
    if text is None:
        raise FileNotFoundError(get_prompt("spill_read_missing", locator=locator))
    start = max(0, int(offset))
    cap = max(1, int(limit))
    blob = text.encode("utf-8")
    chunk = blob[start : start + cap].decode("utf-8", errors="ignore")
    if start + cap < len(blob):
        return chunk + "\n" + get_prompt(
            "spill_read_more",
            next_offset=str(start + cap),
            total=str(len(blob)),
        )
    return chunk
