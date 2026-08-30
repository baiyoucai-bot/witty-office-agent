"""MCP：stdio JSON-RPC，工具名 mcp__<server>__<tool>。连不上就跳过。

连接进池，卸下才 terminate。读循环吃掉 notifications/tools/list_changed，再 tools/list。
"""

from __future__ import annotations

import json
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from witty_agent.logging import get_logger
from witty_agent.runtime import load_runtime
from witty_agent.tools.registry import ToolSpec

logger = get_logger("mcp")
_CACHE: list[ToolSpec] | None = None
_CLIENTS: dict[str, "McpClient"] = {}
_LOCK = threading.RLock()


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: list[str]


class McpClient:
    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._io = threading.Lock()
        self.list_changed = False
        self.tools: list[ToolSpec] = []

    @property
    def fingerprint(self) -> tuple[str, str, tuple[str, ...]]:
        return (self.spec.name, self.spec.command, tuple(self.spec.args))

    @property
    def alive(self) -> bool:
        return bool(self._proc is not None and self._proc.poll() is None)

    def connect(self) -> list[ToolSpec]:
        self._proc = subprocess.Popen(
            [self.spec.command, *self.spec.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "clientInfo": {"name": "witty-agent", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})
        self.refresh_tools()
        logger.info("MCP 已连接 server=%s tools=%s", self.spec.name, len(self.tools))
        return list(self.tools)

    def refresh_tools(self) -> list[ToolSpec]:
        listed = self._rpc("tools/list", {})
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
        self.tools = tools
        self.list_changed = False
        logger.info("MCP 工具表 server=%s tools=%s", self.spec.name, len(tools))
        return list(tools)

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None
        logger.info("MCP 已断开 server=%s", self.spec.name)

    def drain(self, timeout: float = 0.0) -> bool:
        """抽走通知。timeout>0 时等到通知或超时。"""
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
        return self.list_changed

    def _stdout_ready(self) -> bool:
        if not self._proc or not self._proc.stdout:
            return False
        try:
            return bool(select.select([self._proc.stdout.fileno()], [], [], 0)[0])
        except (ValueError, OSError):
            return False

    def _write(self, payload: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("MCP 未连接")
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
        method = str(data.get("method") or "")
        if method == "notifications/tools/list_changed":
            self.list_changed = True
            logger.info("MCP 工具变更通知 server=%s", self.spec.name)
            return None
        return data

    def _make_caller(self, tool_name: str):
        def call(**kwargs: Any) -> str:
            result = self._rpc("tools/call", {"name": tool_name, "arguments": kwargs})
            return json.dumps(result, ensure_ascii=False)

        call.__name__ = tool_name
        return call

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict) -> dict:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("MCP 未连接")
        with self._io:
            expect = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": expect, "method": method, "params": params})
            while True:
                line = self._readline()
                if not line:
                    raise RuntimeError("MCP 无响应")
                data = self._ingest(line)
                if data is None:
                    continue
                if data.get("id") != expect:
                    continue
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                while self._stdout_ready():
                    extra = self._readline()
                    if not extra:
                        break
                    self._ingest(extra)
                return data.get("result") or {}


def _desired_specs() -> list[McpServerSpec]:
    table = load_runtime().get("mcp") or {}
    servers = table.get("servers") if isinstance(table, dict) else None
    if not isinstance(servers, list):
        servers = []
    from witty_agent.plugins.live import extra_mcp_servers

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in extra_mcp_servers() + list(servers):
        if not isinstance(row, dict) or not row.get("name") or not row.get("command"):
            continue
        key = str(row["name"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return [
        McpServerSpec(
            name=str(row["name"]),
            command=str(row["command"]),
            args=[str(item) for item in (row.get("args") or [])],
        )
        for row in rows
    ]


def reconcile_mcp(*, apply_close: bool = True) -> list[ToolSpec]:
    """愿望清单对账。apply_close=False 时不杀仍在跑的连接（对话中）。"""
    global _CACHE
    desired = _desired_specs()
    tools: list[ToolSpec] = []
    keep: dict[str, McpClient] = {}
    with _LOCK:
        for spec in desired:
            existing = _CLIENTS.get(spec.name)
            same = (
                existing is not None
                and existing.fingerprint == (spec.name, spec.command, tuple(spec.args))
                and existing.alive
            )
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
