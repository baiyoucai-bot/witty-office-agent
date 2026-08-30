"""HTTP GET 取回页面文本 + 网络搜索，有字节上限。"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from witty_agent.net_policy import assert_fetchable
from witty_agent.prompts import get_prompt
from witty_agent.runtime import web_settings
from witty_agent.tools.registry import ToolSpec, register_tool

_TAVILY_ENDPOINT = "https://api.tavily.com/search"


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


def _search_api_key() -> str:
    return (os.environ.get("WITTY_SEARCH_API_KEY") or os.environ.get("TAVILY_API_KEY") or "").strip()


def _http_json(request: Request, timeout: int) -> dict:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme由调用方限定
            raw = response.read(1 << 20)
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(get_prompt("web_search_failed", reason=str(exc))) from exc
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(get_prompt("web_search_failed", reason=str(exc))) from exc
    return data if isinstance(data, dict) else {}


def _tavily_rows(query: str, limit: int, timeout: int) -> list[dict]:
    key = _search_api_key()
    if not key:
        raise ValueError(get_prompt("web_search_no_key"))
    assert_fetchable(_TAVILY_ENDPOINT)
    payload = json.dumps({"query": query, "max_results": limit}).encode("utf-8")
    request = Request(
        _TAVILY_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "witty-agent/web_search",
        },
    )
    data = _http_json(request, timeout)
    return [item for item in (data.get("results") or []) if isinstance(item, dict)]


def _searxng_rows(query: str, limit: int, timeout: int, base_url: str) -> list[dict]:
    if not base_url:
        raise ValueError(get_prompt("web_search_no_base_url"))
    endpoint = f"{base_url.rstrip('/')}/search?q={quote(query)}&format=json"
    assert_fetchable(endpoint)
    request = Request(endpoint, headers={"User-Agent": "witty-agent/web_search"})
    data = _http_json(request, timeout)
    rows = [item for item in (data.get("results") or []) if isinstance(item, dict)]
    return rows[:limit]


def web_search(query: str, max_results: int = 0) -> str:
    """网络搜索，返回标题 / 网址 / 摘要列表。provider 与 key 见 [web] 配置。"""
    text = (query or "").strip()
    if not text:
        raise ValueError(get_prompt("web_search_bad_query"))
    settings = web_settings()
    limit = int(max_results) if int(max_results or 0) > 0 else int(settings["search_max_results"])
    limit = max(1, min(limit, 10))
    timeout = int(settings["timeout_sec"])
    provider = settings["search_provider"]
    if provider == "searxng":
        rows = _searxng_rows(text, limit, timeout, str(settings["search_base_url"]))
    else:
        rows = _tavily_rows(text, limit, timeout)
    lines: list[str] = []
    for index, item in enumerate(rows[:limit], start=1):
        title = " ".join(str(item.get("title") or "-").split())
        url = str(item.get("url") or "").strip()
        snippet = " ".join(str(item.get("content") or item.get("snippet") or "").split())[:300]
        lines.append(f"{index}. {title}\n   {url}\n   {snippet}".rstrip())
    if not lines:
        return get_prompt("web_search_no_results", query=text)
    return "\n".join(lines)


register_tool(
    ToolSpec(
        name="web_search",
        description=get_prompt("tool_desc_web_search"),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": get_prompt("web_param_query")},
                "max_results": {
                    "type": "integer",
                    "description": get_prompt("web_param_max_results"),
                },
            },
            "required": ["query"],
        },
        func=web_search,
        timeout_ms=int(web_settings()["timeout_sec"]) * 1000,
    )
)

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
