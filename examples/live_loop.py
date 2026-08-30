"""用真实循环打内网 DeepSeek。密钥只读环境变量，不写进仓库。

    WITTY_API_KEY=... uv run python examples/live_loop.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from witty_agent.llm import OpenAICompatLLM
from witty_agent.logging import setup_logging
from witty_agent.orchestrator import JobSpec, Orchestrator
from witty_agent.runtime import model_settings
from witty_agent.session import create_agent, create_session


async def main() -> int:
    setup_logging()
    settings = model_settings()
    key = str(settings["api_key"] or "")
    print(f"base_url={settings['base_url']}")
    print(f"model_id={settings['model_id']}")
    print(f"api_key_present={bool(key)} api_key_len={len(key)}")
    if not key:
        print("缺少 WITTY_API_KEY / OPENAI_API_KEY，无法做 live loop。", file=sys.stderr)
        return 2

    stream = OpenAICompatLLM(retry_attempts=1, timeout=60, stream=False)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "ws"
        workspace.mkdir()
        agent = create_agent("live_project", "live_agent", root=root)
        session = create_session(agent, workspace_dir=workspace)

        async def allow(_name: str, _call_id: str, _args: dict) -> str:
            return "allow"

        result = await session.run(
            "只用一句话回答：底座循环是否接通。不要调用工具。",
            stream_fn=stream,
            approve=allow,
            approval_mode="allow-all",
        )
        last = result.messages[-1] if result.messages else None
        print(f"stop_reason={getattr(last, 'stop_reason', None)}")
        print(f"text={(last.text() if last else '')[:400]}")
        print(f"events={[item.type for item in result.events]}")
        if last is None or last.stop_reason == "error":
            return 1

        orch = Orchestrator(root, stream, approve=allow)
        job = await orch.dispatch(
            JobSpec(
                prompt="回复 ok 两个字母，不要调用工具。",
                kind="chat",
                project_id="live_project",
                agent_id="live_agent",
                workspace=workspace,
                approval_mode="allow-all",
                max_turns=1,
            )
        )
        print(f"job_status={job.status} job_text={job.text[:200]}")
        return 0 if job.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
