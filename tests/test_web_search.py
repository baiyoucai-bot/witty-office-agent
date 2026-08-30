"""web_search：服务商分发、key 门槛、网络策略、结果排版。全部打桩，不出网。"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

from witty_agent.prompts import get_prompt
from witty_agent.runtime import clear_runtime_cache
from witty_agent.tools import web as web_mod


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _resp(payload: dict) -> _Resp:
    return _Resp(json.dumps(payload).encode("utf-8"))


class WebSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "0"
        clear_runtime_cache()

    def tearDown(self) -> None:
        for key in ("WITTY_WEB_DENY_PUBLIC", "WITTY_SEARCH_API_KEY", "TAVILY_API_KEY"):
            os.environ.pop(key, None)
        clear_runtime_cache()

    def test_tavily_formats_title_url_snippet(self) -> None:
        os.environ["WITTY_SEARCH_API_KEY"] = "tvly-test"
        rows = {
            "results": [
                {"title": "甲文档", "url": "https://a.example/1", "content": "第一条摘要"},
                {"title": "乙文档", "url": "https://b.example/2", "content": "第二条摘要"},
            ]
        }
        with patch.object(web_mod, "urlopen", return_value=_resp(rows)) as mocked:
            text = web_mod.web_search("办公 agent")
        self.assertIn("1. 甲文档", text)
        self.assertIn("https://a.example/1", text)
        self.assertIn("2. 乙文档", text)
        request = mocked.call_args[0][0]
        self.assertIn("api.tavily.com", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer tvly-test")

    def test_missing_key_names_the_config_path(self) -> None:
        with self.assertRaises(ValueError) as caught:
            web_mod.web_search("任意问题")
        self.assertEqual(str(caught.exception), get_prompt("web_search_no_key"))

    def test_fallback_env_var_is_accepted(self) -> None:
        os.environ["TAVILY_API_KEY"] = "tvly-alt"
        with patch.object(web_mod, "urlopen", return_value=_resp({"results": []})):
            text = web_mod.web_search("空结果")
        self.assertEqual(text, get_prompt("web_search_no_results", query="空结果"))

    def test_searxng_uses_base_url_without_key(self) -> None:
        settings = {
            "max_body_bytes": 65536,
            "timeout_sec": 15,
            "allow_hosts": [],
            "allow_private": True,
            "deny_public": False,
            "mode": "public",
            "search_provider": "searxng",
            "search_base_url": "http://192.168.1.100:8888",
            "search_max_results": 5,
        }
        rows = {"results": [{"title": "内网条目", "url": "http://wiki.local/x", "content": "摘要"}]}
        with patch.object(web_mod, "web_settings", return_value=settings):
            with patch.object(web_mod, "urlopen", return_value=_resp(rows)) as mocked:
                text = web_mod.web_search("制度 检索")
        self.assertIn("内网条目", text)
        request = mocked.call_args[0][0]
        self.assertIn("192.168.1.100:8888/search?q=", request.full_url)

    def test_searxng_without_base_url_refuses(self) -> None:
        settings = {
            "max_body_bytes": 65536,
            "timeout_sec": 15,
            "allow_hosts": [],
            "allow_private": True,
            "deny_public": False,
            "mode": "public",
            "search_provider": "searxng",
            "search_base_url": "",
            "search_max_results": 5,
        }
        with patch.object(web_mod, "web_settings", return_value=settings):
            with self.assertRaises(ValueError) as caught:
                web_mod.web_search("x")
        self.assertEqual(str(caught.exception), get_prompt("web_search_no_base_url"))

    def test_intranet_mode_blocks_tavily(self) -> None:
        os.environ["WITTY_SEARCH_API_KEY"] = "tvly-test"
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "1"
        clear_runtime_cache()
        with self.assertRaises(ValueError) as caught:
            web_mod.web_search("公网问题")
        self.assertIn("api.tavily.com", str(caught.exception))

    def test_empty_query_refuses(self) -> None:
        with self.assertRaises(ValueError):
            web_mod.web_search("  ")

    def test_max_results_is_clamped(self) -> None:
        os.environ["WITTY_SEARCH_API_KEY"] = "tvly-test"
        many = {"results": [{"title": f"t{i}", "url": f"https://e/{i}", "content": "s"} for i in range(20)]}
        with patch.object(web_mod, "urlopen", return_value=_resp(many)):
            text = web_mod.web_search("q", max_results=99)
        self.assertIn("10. t9", text)
        self.assertNotIn("11. ", text)

    def test_tool_is_registered(self) -> None:
        from witty_agent.tools import list_tools

        names = {spec.name for spec in list_tools()}
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)


if __name__ == "__main__":
    unittest.main()
