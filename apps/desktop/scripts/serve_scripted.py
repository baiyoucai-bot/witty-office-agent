"""Temporary HTTP API with a scripted model, for desktop create-session + send-prompt checks."""

from __future__ import annotations

import os

from witty_agent.http_api import configure_api, serve
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply


def _factory():
    reply = os.environ.get("WITTY_SCRIPTED_REPLY", "from-api")
    tool = os.environ.get("WITTY_SCRIPTED_TOOL", "")
    if tool == "write":
        path = os.environ.get("WITTY_SCRIPTED_WRITE_PATH", "approved.txt")
        content = os.environ.get("WITTY_SCRIPTED_WRITE_CONTENT", "ok")
        return ScriptedLLM(
            [
                tool_reply("write", {"path": path, "content": content}),
                text_reply(reply),
            ]
        )
    return ScriptedLLM([text_reply(reply)])


def main() -> None:
    host = os.environ.get("WITTY_API_HOST", "127.0.0.1")
    port = int(os.environ.get("WITTY_API_PORT", "8765"))
    configure_api(stream_factory=_factory)
    serve(host, port)


if __name__ == "__main__":
    main()
