"""HTTP GET 取回页面文本，有字节上限。"""

from __future__ import annotations

from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from witty_agent.net_policy import assert_fetchable
from witty_agent.prompts import get_prompt
from witty_agent.runtime import web_settings
from witty_agent.tools.registry import ToolSpec, register_tool


def web_fetch(url: str) -> str:
    """抓取一个 http/https URL 的文本正文。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(get_prompt("web_fetch_bad_url"))
    assert_fetchable(url)
    limit = int(web_settings()["max_body_bytes"])
    timeout = int(web_settings()["timeout_sec"])
    request = Request(url, headers={"User-Agent": "witty-agent/web_fetch"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme already checked
            raw = response.read(limit + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(get_prompt("web_fetch_failed", reason=str(exc))) from exc
    text = raw[:limit].decode("utf-8", errors="replace")
    if len(raw) > limit:
        text += "\n" + get_prompt("web_fetch_truncated", limit=str(limit))
    from witty_agent.links import record_opened_url

    record_opened_url(url, intent="web_fetch")
    return text


register_tool(
    ToolSpec(
        name="web_fetch",
        description=get_prompt("tool_desc_web_fetch"),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": get_prompt("web_param_url")}},
            "required": ["url"],
        },
        func=web_fetch,
        timeout_ms=int(web_settings()["timeout_sec"]) * 1000,
    )
)
