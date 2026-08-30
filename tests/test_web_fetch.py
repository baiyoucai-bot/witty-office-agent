"""web_fetch：编码自适应、HTML 抽正文、raw 原文、截断容错。全部打桩，不出网。"""

from __future__ import annotations

import io
import os
import unittest
from email.message import Message
from unittest.mock import patch

from witty_agent.prompts import get_prompt
from witty_agent.runtime import clear_runtime_cache
from witty_agent.tools import web as web_mod


class _Resp(io.BytesIO):
    def __init__(self, payload: bytes, content_type: str = "") -> None:
        super().__init__(payload)
        self.headers = Message()
        if content_type:
            self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _settings(max_body_bytes: int) -> dict:
    return {
        "max_body_bytes": max_body_bytes,
        "timeout_sec": 15,
        "allow_hosts": [],
        "allow_private": True,
        "deny_public": False,
        "mode": "public",
        "search_provider": "tavily",
        "search_base_url": "",
        "search_max_results": 5,
    }


class WebFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "0"
        clear_runtime_cache()
        recorder = patch("witty_agent.links.record_opened_url", return_value=None)
        recorder.start()
        self.addCleanup(recorder.stop)

    def tearDown(self) -> None:
        os.environ.pop("WITTY_WEB_DENY_PUBLIC", None)
        clear_runtime_cache()

    def _fetch(self, payload: bytes, content_type: str = "", **kwargs) -> str:
        with patch.object(web_mod, "urlopen", return_value=_Resp(payload, content_type)):
            return web_mod.web_fetch("https://example.com/page", **kwargs)

    def test_gbk_html_meta_charset_no_mojibake(self) -> None:
        html = (
            "<!DOCTYPE html><html><head>"
            '<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'
            "<title>内网公告</title></head>"
            "<body><p>电网基建项目资料分类通知</p></body></html>"
        )
        text = self._fetch(html.encode("gbk"), content_type="text/html")
        self.assertIn("电网基建项目资料分类通知", text)
        self.assertIn("内网公告", text)
        self.assertNotIn("\ufffd", text)

    def test_header_charset_wins(self) -> None:
        text = self._fetch("办公自动化平台".encode("gbk"), content_type="text/plain; charset=gbk")
        self.assertEqual(text, "办公自动化平台")

    def test_gbk_without_declaration_falls_back_to_gb18030(self) -> None:
        text = self._fetch("会议纪要：电网审计部季度总结".encode("gbk"), content_type="text/plain")
        self.assertEqual(text, "会议纪要：电网审计部季度总结")

    def test_utf8_html_ok(self) -> None:
        html = "<html><head><meta charset=\"utf-8\"></head><body><p>你好，办公助手</p></body></html>"
        text = self._fetch(html.encode("utf-8"), content_type="text/html; charset=utf-8")
        self.assertIn("你好，办公助手", text)
        self.assertNotIn("\ufffd", text)

    def test_html_extracts_text_drops_script_style(self) -> None:
        html = (
            "<!doctype html><html><head><style>.nav{color:red}</style>"
            "<script>var secret=1;</script></head>"
            "<body><noscript>请启用JS</noscript>"
            "<h1>标题一</h1><p>正文第一段 A &amp; B</p>"
            "<p>符号 &lt;标签&gt; 与&nbsp;空格</p></body></html>"
        )
        text = self._fetch(html.encode("utf-8"), content_type="text/html; charset=utf-8")
        self.assertNotIn("var secret", text)
        self.assertNotIn(".nav", text)
        self.assertNotIn("请启用JS", text)
        self.assertNotIn("<p>", text)
        self.assertIn("标题一", text)
        self.assertIn("正文第一段 A & B", text)
        self.assertIn("符号 <标签> 与 空格", text)

    def test_sniffs_html_without_content_type(self) -> None:
        html = "<HTML><body><script>alert(1)</script><p>无头字段页面</p></body></HTML>"
        text = self._fetch(html.encode("utf-8"))
        self.assertIn("无头字段页面", text)
        self.assertNotIn("alert(1)", text)

    def test_raw_returns_original_html(self) -> None:
        html = "<html><body><script>var x=1;</script><p>原文</p></body></html>"
        text = self._fetch(html.encode("utf-8"), content_type="text/html; charset=utf-8", raw=True)
        self.assertIn("<script>var x=1;</script>", text)
        self.assertIn("<p>原文</p>", text)

    def test_json_passthrough(self) -> None:
        payload = '{"name": "报表", "rows": [1, 2]}'
        text = self._fetch(payload.encode("utf-8"), content_type="application/json")
        self.assertEqual(text, payload)

    def test_truncation_mid_multibyte_char_does_not_crash(self) -> None:
        limit = 32  # 不是 3 的倍数：utf-8 汉字会被切成半个
        payload = ("汉" * 40).encode("utf-8")
        with patch.object(web_mod, "web_settings", return_value=_settings(limit)):
            with patch.object(web_mod, "urlopen", return_value=_Resp(payload, "text/plain")):
                text = web_mod.web_fetch("https://example.com/big")
        self.assertIn("汉" * 10, text)
        self.assertNotIn("\ufffd", text)
        self.assertIn(get_prompt("web_fetch_truncated", limit=str(limit)), text)

    def test_truncation_mid_gbk_char_with_meta_tolerates(self) -> None:
        # 结尾全是 2 字节 GBK 汉字，limit 取奇数偏移，保证切在半个汉字上
        html = '<html><head><meta charset="gb2312"></head><body><p>截断容错测试' + "正文" * 30
        payload = html.encode("gbk")
        limit = len(payload) - 3
        with patch.object(web_mod, "web_settings", return_value=_settings(limit)):
            with patch.object(web_mod, "urlopen", return_value=_Resp(payload, "text/html")):
                text = web_mod.web_fetch("https://example.com/gbk")
        self.assertIn("截断容错测试", text)
        self.assertIn(get_prompt("web_fetch_truncated", limit=str(limit)), text)

    def test_schema_has_raw_param(self) -> None:
        from witty_agent.tools import list_tools

        spec = next(item for item in list_tools() if item.name == "web_fetch")
        self.assertIn("raw", spec.parameters["properties"])
        self.assertEqual(spec.parameters["properties"]["raw"]["type"], "boolean")
        self.assertNotIn("raw", spec.parameters["required"])


if __name__ == "__main__":
    unittest.main()
