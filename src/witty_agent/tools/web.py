"""HTTP GET 取回页面文本 + 网络搜索，有字节上限。"""

from __future__ import annotations

import codecs
import json
import os
import re
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from witty_agent.net_policy import assert_fetchable
from witty_agent.prompts import get_prompt
from witty_agent.runtime import web_settings
from witty_agent.tools.registry import ToolSpec, register_tool

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_ANYSEARCH_ENDPOINT = "https://api.anysearch.com/v1/search"

_CHARSET_HEADER_RE = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)""", re.IGNORECASE)
# 同时覆盖 <meta charset="..."> 和 <meta http-equiv="Content-Type" content="...; charset=...">
_CHARSET_META_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)""", re.IGNORECASE)
# 页面常声明 gb2312/gbk 却混用超集字符，统一按 gb18030 解，无损兼容
_CHARSET_ALIASES = {"gb2312", "gbk", "gb-2312", "csgb2312", "gb_2312-80"}

_HTML_SKIP_TAGS = {"script", "style", "noscript", "template"}
_HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "option", "p", "pre", "section", "select",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def _declared_charset(content_type: str, body: bytes) -> str:
    match = _CHARSET_HEADER_RE.search(content_type or "")
    if not match:
        match = _CHARSET_META_RE.search(body[:4096])
    if not match:
        return ""
    charset = match.group(1)
    charset = charset.decode("ascii", errors="replace") if isinstance(charset, bytes) else charset
    charset = charset.strip().lower()
    if charset in _CHARSET_ALIASES:
        return "gb18030"
    try:
        codecs.lookup(charset)
    except LookupError:
        return ""
    return charset


def _try_decode(body: bytes, encoding: str) -> str | None:
    """严格解码；仅当错误出现在末尾 4 字节内（按字节截断切在多字节字符中间）时截尾重试。"""
    try:
        return body.decode(encoding)
    except UnicodeDecodeError as exc:
        if exc.start >= len(body) - 4:
            try:
                return body[: exc.start].decode(encoding)
            except UnicodeDecodeError:
                return None
        return None


def _decode_body(body: bytes, content_type: str) -> str:
    charset = _declared_charset(content_type, body)
    if charset:
        return body.decode(charset, errors="replace")
    for encoding in ("utf-8", "gb18030"):
        text = _try_decode(body, encoding)
        if text is not None:
            return text
    return body.decode("utf-8", errors="replace")


def _looks_like_html(content_type: str, body: bytes) -> bool:
    if "text/html" in (content_type or "").lower():
        return True
    head = body[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


class _TextExtractor(HTMLParser):
    """丢 script/style/noscript/template 内容，块级标签转换行；charref 由解析器直接还原。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(self._chunks).splitlines()]
        merged: list[str] = []
        for line in lines:
            if line:
                merged.append(line)
            elif merged and merged[-1]:
                merged.append("")
        return "\n".join(merged).strip()


def _html_to_text(markup: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    return extractor.text()


def web_fetch(url: str, raw: bool = False) -> str:
    """抓取一个 http/https URL 的文本正文；HTML 默认抽正文，raw=True 返回原文。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(get_prompt("web_fetch_bad_url"))
    assert_fetchable(url)
    limit = int(web_settings()["max_body_bytes"])
    timeout = int(web_settings()["timeout_sec"])
    request = Request(url, headers={"User-Agent": "witty-agent/web_fetch"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme already checked
            payload = response.read(limit + 1)
            content_type = str(response.headers.get("Content-Type") or "")
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(get_prompt("web_fetch_failed", reason=str(exc))) from exc
    body = payload[:limit]
    text = _decode_body(body, content_type)
    if not raw and _looks_like_html(content_type, body):
        text = _html_to_text(text)
    if len(payload) > limit:
        text += "\n" + get_prompt("web_fetch_truncated", limit=str(limit))
    from witty_agent.links import record_opened_url

    record_opened_url(url, intent="web_fetch")
    return text


def _search_api_key() -> str:
    return (os.environ.get("WITTY_SEARCH_API_KEY") or os.environ.get("TAVILY_API_KEY") or "").strip()


def _anysearch_api_key() -> str:
    # 兼容项目通用搜索钥匙，同时优先使用 AnySearch 官方变量名。
    return (os.environ.get("ANYSEARCH_API_KEY") or os.environ.get("WITTY_SEARCH_API_KEY") or "").strip()


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


def _anysearch_rows(query: str, limit: int, timeout: int) -> list[dict]:
    assert_fetchable(_ANYSEARCH_ENDPOINT)
    payload = json.dumps({"query": query, "max_results": limit}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "witty-agent/web_search",
    }
    key = _anysearch_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(_ANYSEARCH_ENDPOINT, data=payload, headers=headers)
    data = _http_json(request, timeout)
    code = data.get("code")
    if code not in (None, 0):
        raise RuntimeError(get_prompt("web_search_failed", reason=str(data.get("message") or code)))
    result_data = data.get("data")
    if not isinstance(result_data, dict):
        result_data = data
    return [item for item in (result_data.get("results") or []) if isinstance(item, dict)][:limit]


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
    elif provider == "anysearch":
        rows = _anysearch_rows(text, limit, timeout)
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
            "properties": {
                "url": {"type": "string", "description": get_prompt("web_param_url")},
                "raw": {"type": "boolean", "description": get_prompt("web_param_raw")},
            },
            "required": ["url"],
        },
        func=web_fetch,
        timeout_ms=int(web_settings()["timeout_sec"]) * 1000,
    )
)
