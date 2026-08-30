"""自进化评分闭环示例：跑基准、打分、只升不降地采纳改动。"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from witty_agent.evolution.improve import run_scoring_loop, run_self_evolve
from witty_agent.llm import OpenAICompatLLM, ScriptedLLM, tool_reply, text_reply
from witty_agent.logging import setup_logging


def scripted_writer() -> ScriptedLLM:
    return ScriptedLLM(
        [
            tool_reply("write", {"path": "summary.md", "content": "Aurora is fast.\n\n- 120ms\n- 55%\n- Q3 2026\n"}),
            text_reply("wrote"),
        ]
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="调用内网 DeepSeek")
    parser.add_argument("--evolve", action="store_true", help="跑 self-evolve 而不是脚本改 AGENTS.md")
    args = parser.parse_args()
    setup_logging()
    stream = OpenAICompatLLM() if args.live else scripted_writer()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "data"
        workspace = Path(tmp) / "ws"
        if args.evolve:
            result = await run_self_evolve(root=root, workspace=workspace, stream_fn=stream)
        else:
            result = await run_scoring_loop(root=root, workspace=workspace, stream_fn=stream)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
