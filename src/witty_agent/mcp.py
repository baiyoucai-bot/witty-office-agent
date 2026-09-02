"""MCP 客户端：stdio 与 Streamable HTTP 两种传输，tools / resources / prompts 三类原语。

工具名 `mcp__<server>__<tool>`。resources 与 prompts 不进内核命令表，按服务端声明的
capabilities 合成为工具（`list_resources` / `read_resource` / `list_prompts` / `get_prompt`）。

连接进池，卸下才 terminate。stdio 读循环吃掉 notifications/tools/list_changed 再 tools/list；
HTTP 模式不开 GET 长连接，收不到推送通知——`drain()` 恒 False，刷新靠重新对账。
"""

from __future__ import annotations

import json
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.runtime import load_runtime
from witty_agent.tools.registry import ToolSpec

logger = get_logger("mcp")
_CACHE: list[ToolSpec] | None = None
_CLIENTS: dict[str, "McpClient"] = {}
_LOCK = threading.RLock()

PROTOCOL_VERSION = "2025-03-26"
_LIST_CHANGED = "notifications/tools/list_changed"


@dataclass
class McpServerSpec:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout_sec: int = 30
    # 外部工具写不写盘我们不知道，默认按危险处理；配置 trusted = true 才当只读放行
    trusted: bool = False

    @property
    def transport_kind(self) -> str:
        return "http" if self.url else "stdio"


# ---------------------------------------------------------------- 传输层


class StdioTransport:
    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._io = threading.Lock()
        self.on_notification = None

    @property
    def alive(self) -> bool:
        return bool(self._proc is not None and self._proc.poll() is None)

    def open(self) -> None:
        self._proc = subprocess.Popen(
            [self.spec.command, *self.spec.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None

    def _stdout_ready(self) -> bool:
        if not self._proc or not self._proc.stdout:
            return False
        try:
            return bool(select.select([self._proc.stdout.fileno()], [], [], 0)[0])
        except (ValueError, OSError):
            return False

    def _write(self, payload: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError(get_prompt("mcp_not_connected"))
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _readline(self) -> str:
        if not self._proc or not self._proc.stdout:
            return ""
        raw = self._proc.stdout.readline()
        if not raw:
            return ""
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)

    def _ingest(self, line: str) -> dict[str, Any] | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        if "id" not in data and data.get("method") and self.on_notification:
            self.on_notification(data)
            return None
        return data

    def drain(self, timeout: float = 0.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._io:
            while True:
                if self._stdout_ready():
                    line = self._readline()
                    if not line:
                        break
                    self._ingest(line)
                    continue
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict) -> dict:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError(get_prompt("mcp_not_connected"))
        with self._io:
            expect = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": expect, "method": method, "params": params})
            while True:
                line = self._readline()
                if not line:
                    raise RuntimeError(get_prompt("mcp_no_response"))
                data = self._ingest(line)
                if data is None or data.get("id") != expect:
                    continue
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                while self._stdout_ready():
                    extra = self._readline()
                    if not extra:
                        break
                    self._ingest(extra)
                return data.get("result") or {}


class HttpTransport:
    """Streamable HTTP：每个请求一次 POST，响应是 JSON 或 SSE；会话靠 Mcp-Session-Id 头。"""

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._next_id = 1
        self._io = threading.Lock()
        self._session_id = ""
        self._opened = False
        self.on_notification = None

    @property
    def alive(self) -> bool:
        return self._opened

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._session_id = ""

    def drain(self, timeout: float = 0.0) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "witty-agent/mcp",
        }
        headers.update({str(k): str(v) for k, v in (self.spec.headers or {}).items()})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> tuple[str, bytes]:
        request = Request(self.spec.url, data=json.dumps(payload).encode("utf-8"), headers=self._headers(), method="POST")
        try:
            with urlopen(request, timeout=self.spec.timeout_sec) as response:  # noqa: S310 - 地址来自配置
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = str(session)
                content_type = str(response.headers.get("Content-Type") or "")
                return content_type, response.read()
        except HTTPError as exc:
            raise RuntimeError(get_prompt("mcp_http_failed", url=self.spec.url, reason=f"HTTP {exc.code}")) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(get_prompt("mcp_http_failed", url=self.spec.url, reason=str(exc))) from exc

    def _messages_from_response(self, content_type: str, raw: bytes) -> list[dict[str, Any]]:
        text = raw.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type:
            out: list[dict[str, Any]] = []
            for chunk in text.replace("\r\n", "\n").split("\n\n"):
                data_lines = [line[5:].strip() for line in chunk.split("\n") if line.startswith("data:")]
                if not data_lines:
                    continue
                try:
                    out.append(json.loads("\n".join(data_lines)))
                except json.JSONDecodeError:
                    continue
            return out
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else [data]

    def notify(self, method: str, params: dict) -> None:
        with self._io:
            self._post({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict) -> dict:
        with self._io:
            expect = self._next_id
            self._next_id += 1
            content_type, raw = self._post({"jsonrpc": "2.0", "id": expect, "method": method, "params": params})
            for data in self._messages_from_response(content_type, raw):
                if "id" not in data and data.get("method") and self.on_notification:
                    self.on_notification(data)
                    continue
                if data.get("id") != expect:
                    continue
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("result") or {}
            raise RuntimeError(get_prompt("mcp_no_response"))


def make_transport(spec: McpServerSpec):
    return HttpTransport(spec) if spec.transport_kind == "http" else StdioTransport(spec)


def spec_fingerprint(spec: McpServerSpec) -> tuple[str, str, tuple[str, ...], str, tuple[tuple[str, str], ...]]:
    """对账用：名字、命令、参数、url、头任一变了都算新连接。"""
    return (
        spec.name,
        spec.command,
        tuple(spec.args),
        spec.url,
        tuple(sorted((str(k), str(v)) for k, v in (spec.headers or {}).items())),
    )


# ---------------------------------------------------------------- 客户端


class McpClient:
    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.transport = make_transport(spec)
        self.transport.on_notification = self._on_notification
        self.list_changed = False
        self.tools: list[ToolSpec] = []
        self.capabilities: dict[str, Any] = {}

    @property
    def fingerprint(self) -> tuple[str, str, tuple[str, ...], str, tuple[tuple[str, str], ...]]:
        return spec_fingerprint(self.spec)

    @property
    def alive(self) -> bool:
        return self.transport.alive

    def _on_notification(self, data: dict[str, Any]) -> None:
        if str(data.get("method") or "") == _LIST_CHANGED:
            self.list_changed = True
            logger.info("MCP 工具变更通知 server=%s", self.spec.name)

    def connect(self) -> list[ToolSpec]:
        self.transport.open()
        result = self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "clientInfo": {"name": "witty-agent", "version": "0.1.0"},
            },
        )
        caps = result.get("capabilities") if isinstance(result, dict) else None
        self.capabilities = dict(caps) if isinstance(caps, dict) else {}
        self.transport.notify("notifications/initialized", {})
        self.refresh_tools()
        logger.info(
            "MCP 已连接 server=%s transport=%s tools=%s",
            self.spec.name,
            self.spec.transport_kind,
            len(self.tools),
        )
        return list(self.tools)

    def refresh_tools(self) -> list[ToolSpec]:
        listed = self.transport.request("tools/list", {})
        tools: list[ToolSpec] = []
        for item in listed.get("tools") or []:
            name = f"mcp__{self.spec.name}__{item.get('name')}"
            tools.append(
                ToolSpec(
                    name=name,
                    description=str(item.get("description") or name),
                    parameters=item.get("inputSchema") or {"type": "object", "properties": {}},
                    func=self._make_caller(str(item.get("name"))),
                )
            )
        tools.extend(self._synthetic_tools())
        self.tools = tools
        self.list_changed = False
        logger.info("MCP 工具表 server=%s tools=%s", self.spec.name, len(tools))
        return list(tools)

    def close(self) -> None:
        self.transport.close()
        logger.info("MCP 已断开 server=%s", self.spec.name)

    def drain(self, timeout: float = 0.0) -> bool:
        """抽走通知。timeout>0 时等到通知或超时。HTTP 传输收不到推送，恒 False。"""
        self.transport.drain(timeout)
        return self.list_changed

    # ---- tools/call

    def _make_caller(self, tool_name: str):
        def call(**kwargs: Any) -> str:
            result = self.transport.request("tools/call", {"name": tool_name, "arguments": kwargs})
            return json.dumps(result, ensure_ascii=False)

        call.__name__ = tool_name
        return call

    # ---- resources / prompts 合成工具

    def _synthetic_tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        prefix = f"mcp__{self.spec.name}__"
        if isinstance(self.capabilities.get("resources"), dict):
            out.append(
                ToolSpec(
                    name=f"{prefix}list_resources",
                    description=get_prompt("mcp_tool_desc_list_resources", server=self.spec.name),
                    parameters={"type": "object", "properties": {}},
                    func=self._list_resources,
                )
            )
            out.append(
                ToolSpec(
                    name=f"{prefix}read_resource",
                    description=get_prompt("mcp_tool_desc_read_resource", server=self.spec.name),
                    parameters={
                        "type": "object",
                        "properties": {"uri": {"type": "string", "description": get_prompt("mcp_param_uri")}},
                        "required": ["uri"],
                    },
                    func=self._read_resource,
                )
            )
        if isinstance(self.capabilities.get("prompts"), dict):
            out.append(
                ToolSpec(
                    name=f"{prefix}list_prompts",
                    description=get_prompt("mcp_tool_desc_list_prompts", server=self.spec.name),
                    parameters={"type": "object", "properties": {}},
                    func=self._list_prompts,
                )
            )
            out.append(
                ToolSpec(
                    name=f"{prefix}get_prompt",
                    description=get_prompt("mcp_tool_desc_get_prompt", server=self.spec.name),
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": get_prompt("mcp_param_prompt_name")},
                            "arguments": {"type": "object", "description": get_prompt("mcp_param_prompt_arguments")},
                        },
                        "required": ["name"],
                    },
                    func=self._get_prompt,
                )
            )
        return out

    def _list_resources(self) -> str:
        result = self.transport.request("resources/list", {})
        rows = [item for item in (result.get("resources") or []) if isinstance(item, dict)]
        if not rows:
            return get_prompt("mcp_resources_empty", server=self.spec.name)
        lines = []
        for item in rows:
            head = f"- {item.get('uri', '-')}"
            name = str(item.get("name") or "").strip()
            desc = str(item.get("description") or "").strip()
            mime = str(item.get("mimeType") or "").strip()
            extra = "  ".join(part for part in (name, desc, f"[{mime}]" if mime else "") if part)
            lines.append(f"{head}  {extra}".rstrip())
        return "\n".join(lines)

    def _read_resource(self, uri: str) -> str:
        result = self.transport.request("resources/read", {"uri": str(uri)})
        parts: list[str] = []
        for item in result.get("contents") or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item.get("blob"), str):
                size = len(item["blob"]) * 3 // 4
                parts.append(get_prompt("mcp_resource_blob", size=str(size), mime=str(item.get("mimeType") or "-")))
        return "\n".join(parts) if parts else get_prompt("mcp_resource_empty", uri=str(uri))

    def _list_prompts(self) -> str:
        result = self.transport.request("prompts/list", {})
        rows = [item for item in (result.get("prompts") or []) if isinstance(item, dict)]
        if not rows:
            return get_prompt("mcp_prompts_empty", server=self.spec.name)
        lines = []
        for item in rows:
            args = ", ".join(
                str(arg.get("name") or "") + ("*" if arg.get("required") else "")
                for arg in (item.get("arguments") or [])
                if isinstance(arg, dict)
            )
            desc = str(item.get("description") or "").strip()
            lines.append(f"- {item.get('name', '-')}({args})  {desc}".rstrip())
        return "\n".join(lines)

    def _get_prompt(self, name: str, arguments: object = None) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        result = self.transport.request("prompts/get", {"name": str(name), "arguments": args})
        lines: list[str] = []
        desc = str(result.get("description") or "").strip()
        if desc:
            lines.append(desc)
        for message in result.get("messages") or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            text = ""
            if isinstance(content, dict):
                text = str(content.get("text") or "")
            elif isinstance(content, list):
                text = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
            elif isinstance(content, str):
                text = content
            lines.append(f"{message.get('role', 'user')}: {text}")
        return "\n".join(lines) if lines else get_prompt("mcp_prompt_empty", name=str(name))


# ---------------------------------------------------------------- 对账


def _spec_from_row(row: dict[str, object]) -> McpServerSpec:
    headers_raw = row.get("headers")
    headers = {str(k): str(v) for k, v in headers_raw.items()} if isinstance(headers_raw, dict) else {}
    table = load_runtime().get("mcp") or {}
    default_timeout = int(table.get("timeout_sec") or 30) if isinstance(table, dict) else 30
    return McpServerSpec(
        name=str(row["name"]),
        command=str(row.get("command") or ""),
        args=[str(item) for item in (row.get("args") or [])],
        url=str(row.get("url") or ""),
        headers=headers,
        timeout_sec=int(row.get("timeout_sec") or default_timeout),
        trusted=bool(row.get("trusted", False)),
    )


def mcp_server_trusted(server: str) -> bool:
    """审批判据：只有配置里 trusted = true 的服务器，其工具才当读类放行。"""
    name = str(server or "").strip()
    if not name:
        return False
    with _LOCK:
        client = _CLIENTS.get(name)
    if client is not None:
        return bool(client.spec.trusted)
    return any(spec.name == name and spec.trusted for spec in _desired_specs())


def _desired_specs() -> list[McpServerSpec]:
    table = load_runtime().get("mcp") or {}
    servers = table.get("servers") if isinstance(table, dict) else None
    if not isinstance(servers, list):
        servers = []
    from witty_agent.plugins.live import extra_mcp_servers

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in extra_mcp_servers() + list(servers):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        if not (row.get("command") or row.get("url")):
            continue
        key = str(row["name"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return [_spec_from_row(row) for row in rows]


def reconcile_mcp(*, apply_close: bool = True) -> list[ToolSpec]:
    """愿望清单对账。apply_close=False 时不杀仍在跑的连接（对话中）。"""
    global _CACHE
    desired = _desired_specs()
    tools: list[ToolSpec] = []
    keep: dict[str, McpClient] = {}
    with _LOCK:
        for spec in desired:
            existing = _CLIENTS.get(spec.name)
            same = existing is not None and existing.fingerprint == spec_fingerprint(spec) and existing.alive
            if same and existing is not None:
                existing.drain()
                if existing.list_changed:
                    try:
                        existing.refresh_tools()
                    except Exception as exc:
                        logger.warning("MCP 刷新失败 server=%s err=%s", spec.name, exc)
                tools.extend(existing.tools)
                keep[spec.name] = existing
                continue
            if existing is not None and not apply_close:
                existing.drain()
                tools.extend(existing.tools)
                keep[spec.name] = existing
                continue
            if existing is not None:
                existing.close()
            try:
                client = McpClient(spec)
                client.connect()
                keep[spec.name] = client
                tools.extend(client.tools)
            except Exception as exc:
                logger.warning("跳过 MCP server=%s err=%s", spec.name, exc)
        if apply_close:
            for name, client in list(_CLIENTS.items()):
                if name not in keep:
                    client.close()
        else:
            for name, client in list(_CLIENTS.items()):
                if name not in keep:
                    keep[name] = client
                    client.drain()
                    tools.extend(client.tools)
        _CLIENTS.clear()
        _CLIENTS.update(keep)
        _CACHE = tools
    return list(tools)


def load_mcp_tools(*, force: bool = False) -> list[ToolSpec]:
    global _CACHE
    if force:
        return reconcile_mcp(apply_close=True)
    if _CACHE is not None:
        changed = False
        with _LOCK:
            for client in _CLIENTS.values():
                if client.drain():
                    changed = True
                    try:
                        client.refresh_tools()
                    except Exception as exc:
                        logger.warning("MCP 刷新失败 server=%s err=%s", client.spec.name, exc)
            if changed:
                _CACHE = [item for client in _CLIENTS.values() for item in client.tools]
        return list(_CACHE)
    from witty_agent.plugins import live

    return reconcile_mcp(apply_close=not live.surface_busy())


def reset_mcp_cache() -> None:
    global _CACHE
    with _LOCK:
        for client in list(_CLIENTS.values()):
            client.close()
        _CLIENTS.clear()
        _CACHE = None


def mcp_clients() -> dict[str, McpClient]:
    with _LOCK:
        return dict(_CLIENTS)
