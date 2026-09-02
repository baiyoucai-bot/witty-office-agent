"""MCP Streamable HTTP 传输 + resources / prompts 合成工具。起一个本机假服务器，不出网。"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from witty_agent.kernel_surface import is_kernel_tool
from witty_agent.mcp import McpClient, McpServerSpec, mcp_server_trusted, reconcile_mcp, reset_mcp_cache
from witty_agent.prompts import get_prompt

SESSION = "sess-abc"
SEEN: list[dict] = []


class FakeMcp(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        SEEN.append({"headers": dict(self.headers), "body": body})
        method = body.get("method")
        if "id" not in body:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rid = body["id"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            }
            self._json(rid, result, session=True)
            return
        if method == "tools/list":
            self._json(
                rid,
                {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]},
            )
            return
        if method == "tools/call":
            # 用 SSE 回包，顺手夹一条通知，验证解析器能跳过通知取到本请求的响应
            payload = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "echo:" + str(body["params"]["arguments"].get("q"))}]}}
            notice = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
            raw = ("event: message\ndata: " + json.dumps(notice) + "\n\n" + "event: message\ndata: " + json.dumps(payload) + "\n\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if method == "resources/list":
            self._json(rid, {"resources": [{"uri": "kb://policy", "name": "制度", "mimeType": "text/plain"}, {"uri": "kb://logo", "name": "标志", "mimeType": "image/png"}]})
            return
        if method == "resources/read":
            uri = body["params"]["uri"]
            if uri == "kb://logo":
                self._json(rid, {"contents": [{"uri": uri, "mimeType": "image/png", "blob": "AAAA" * 30}]})
            else:
                self._json(rid, {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "差旅报销当月提交"}]})
            return
        if method == "prompts/list":
            self._json(rid, {"prompts": [{"name": "summarize", "description": "摘要", "arguments": [{"name": "topic", "required": True}]}]})
            return
        if method == "prompts/get":
            topic = body["params"]["arguments"].get("topic")
            self._json(rid, {"description": "摘要提示", "messages": [{"role": "user", "content": {"type": "text", "text": f"请摘要 {topic}"}}]})
            return
        self._json(rid, {}, error={"code": -32601, "message": "unknown"})

    def _json(self, rid: object, result: dict, *, session: bool = False, error: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "id": rid}
        if error:
            payload["error"] = error
        else:
            payload["result"] = result
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if session:
            self.send_header("Mcp-Session-Id", SESSION)
        self.end_headers()
        self.wfile.write(raw)


class McpHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcp)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/mcp"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        SEEN.clear()
        reset_mcp_cache()

    def tearDown(self) -> None:
        reset_mcp_cache()

    def _spec(self, **extra) -> McpServerSpec:
        return McpServerSpec(name="fake", url=self.url, headers={"X-Token": "t1"}, **extra)

    def test_connect_lists_tools_and_synthesizes_resource_prompt_tools(self) -> None:
        client = McpClient(self._spec())
        try:
            tools = client.connect()
        finally:
            client.close()
        names = [item.name for item in tools]
        self.assertEqual(
            names,
            [
                "mcp__fake__echo",
                "mcp__fake__list_resources",
                "mcp__fake__read_resource",
                "mcp__fake__list_prompts",
                "mcp__fake__get_prompt",
            ],
        )
        self.assertFalse(any(is_kernel_tool(name) for name in names))

    def test_session_id_and_custom_headers_travel_with_every_request(self) -> None:
        client = McpClient(self._spec())
        try:
            client.connect()
        finally:
            client.close()
        methods = [row["body"].get("method") for row in SEEN]
        self.assertEqual(methods[:3], ["initialize", "notifications/initialized", "tools/list"])
        for row in SEEN[1:]:
            self.assertEqual(row["headers"].get("Mcp-Session-Id"), SESSION)
        for row in SEEN:
            self.assertEqual(row["headers"].get("X-Token"), "t1")
            self.assertIn("text/event-stream", row["headers"].get("Accept", ""))

    def test_tool_call_parses_sse_response_and_skips_notifications(self) -> None:
        client = McpClient(self._spec())
        try:
            tools = {item.name: item for item in client.connect()}
            out = tools["mcp__fake__echo"].func(q="hi")
        finally:
            client.close()
        self.assertIn("echo:hi", out)

    def test_resources_and_prompts_render_text(self) -> None:
        client = McpClient(self._spec())
        try:
            tools = {item.name: item for item in client.connect()}
            listed = tools["mcp__fake__list_resources"].func()
            self.assertIn("kb://policy", listed)
            self.assertIn("[text/plain]", listed)
            text = tools["mcp__fake__read_resource"].func(uri="kb://policy")
            self.assertEqual(text, "差旅报销当月提交")
            blob = tools["mcp__fake__read_resource"].func(uri="kb://logo")
            self.assertIn("二进制资源", blob)
            self.assertNotIn("AAAA", blob)
            prompts = tools["mcp__fake__list_prompts"].func()
            self.assertIn("summarize(topic*)", prompts)
            rendered = tools["mcp__fake__get_prompt"].func(name="summarize", arguments={"topic": "周报"})
            self.assertIn("摘要提示", rendered)
            self.assertIn("user: 请摘要 周报", rendered)
        finally:
            client.close()

    def test_reconcile_from_runtime_rows_and_trust_flag(self) -> None:
        rows = [{"name": "fake", "url": self.url, "headers": {"X-Token": "t1"}, "trusted": True}]
        with patch("witty_agent.mcp.load_runtime", return_value={"mcp": {"servers": rows, "timeout_sec": 5}}), patch(
            "witty_agent.plugins.live.extra_mcp_servers", return_value=[]
        ):
            tools = reconcile_mcp(apply_close=True)
            names = {item.name for item in tools}
            self.assertIn("mcp__fake__echo", names)
            self.assertIn("mcp__fake__read_resource", names)
            self.assertTrue(mcp_server_trusted("fake"))
            self.assertFalse(mcp_server_trusted("nope"))

    def test_http_failure_is_a_readable_error(self) -> None:
        client = McpClient(McpServerSpec(name="dead", url="http://127.0.0.1:9/mcp", timeout_sec=1))
        with self.assertRaises(RuntimeError) as caught:
            client.connect()
        self.assertIn("MCP HTTP 请求失败", str(caught.exception))
        self.assertEqual(get_prompt("mcp_http_failed", url="u", reason="r"), "MCP HTTP 请求失败 url=u：r")

    def test_stdio_only_server_gets_no_synthetic_tools(self) -> None:
        """服务端没声明 resources / prompts 能力就不合成，既有 stdio 假服务器的断言靠这条保住。"""
        client = McpClient(self._spec())
        client.capabilities = {"tools": {}}
        self.assertEqual(client._synthetic_tools(), [])


if __name__ == "__main__":
    unittest.main()
