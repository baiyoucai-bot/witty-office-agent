from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent.http_api import configure_api, handle_request
from witty_agent.net_policy import assert_fetchable, host_allowed, is_public_default_host
from witty_agent.prompts import get_prompt
from witty_agent.runtime import clear_runtime_cache, model_settings, save_web_overlay, web_settings
from witty_agent.tools.web import web_fetch


class NetPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_runtime_cache()

    def tearDown(self) -> None:
        os.environ.pop("WITTY_WEB_DENY_PUBLIC", None)
        clear_runtime_cache()

    def test_default_allows_public(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "0"
        clear_runtime_cache()
        self.assertFalse(web_settings()["deny_public"])
        self.assertEqual(web_settings()["mode"], "public")
        self.assertEqual(web_settings()["search_provider"], "anysearch")
        self.assertTrue(host_allowed("https://example.com/x"))
        self.assertTrue(host_allowed("https://downloads.claude.ai/claude-code-releases/latest"))
        self.assertFalse(host_allowed("http://169.254.169.254/latest/meta-data"))

    def test_private_and_loopback_allowed(self) -> None:
        self.assertTrue(host_allowed("http://127.0.0.1:8765/health"))
        self.assertTrue(host_allowed("http://192.168.1.100:8000/v1"))
        self.assertTrue(host_allowed("http://10.0.0.8/oa"))
        self.assertTrue(host_allowed("http://localhost:8765/"))

    def test_public_ip_and_link_local_denied(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "1"
        clear_runtime_cache()
        self.assertFalse(host_allowed("https://8.8.8.8/dns"))
        self.assertFalse(host_allowed("http://1.1.1.1/"))
        self.assertFalse(host_allowed("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(host_allowed("https://example.com/x"))
        self.assertFalse(host_allowed("https://api.openai.com/v1/models"))

    def test_mixed_dns_denied(self) -> None:
        import socket

        os.environ["WITTY_WEB_DENY_PUBLIC"] = "1"
        clear_runtime_cache()
        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
        ]
        with patch("witty_agent.net_policy.socket.getaddrinfo", return_value=mixed):
            self.assertFalse(host_allowed("http://evil.example/ssrf"))

    def test_web_fetch_refuses_public_without_network(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "1"
        clear_runtime_cache()
        with self.assertRaises(ValueError) as caught:
            web_fetch("https://example.com/note")
        self.assertIn("example.com", str(caught.exception))
        with self.assertRaises(ValueError):
            assert_fetchable("http://169.254.169.254/latest/meta-data")

    def test_overlay_switches_to_intranet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("WITTY_WEB_DENY_PUBLIC", None)
            save_web_overlay(True, root=Path(tmp))
            settings = web_settings(root=Path(tmp))
            self.assertTrue(settings["deny_public"])
            self.assertEqual(settings["mode"], "intranet")


class WebApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_put_switches_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("WITTY_WEB_DENY_PUBLIC", None)
            configure_api(root=Path(tmp))
            status, body = await handle_request("GET", "/v1/web")
            self.assertEqual(status, 200)
            self.assertEqual(body["mode"], "public")
            status, body = await handle_request("PUT", "/v1/web", {"mode": "intranet"})
            self.assertEqual(status, 200)
            self.assertTrue(body["deny_public"])
            self.assertEqual(body["mode"], "intranet")
            status, body = await handle_request("PUT", "/v1/web", {"mode": "public"})
            self.assertEqual(status, 200)
            self.assertFalse(body["deny_public"])

    def test_model_default_is_not_public_openai(self) -> None:
        self.assertFalse(is_public_default_host(str(model_settings().get("base_url") or "")))
        self.assertNotIn("openai.com", str(model_settings().get("base_url") or "").casefold())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.toml"
            path.write_text("[model]\nbase_url = \"\"\nmodel_id = \"\"\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"WITTY_RUNTIME_FILE": str(path), "WITTY_BASE_URL": "", "WITTY_MODEL_ID": ""},
                clear=False,
            ):
                clear_runtime_cache()
                try:
                    settings = model_settings()
                    self.assertEqual(settings["base_url"], "")
                    self.assertNotIn("openai.com", settings["base_url"])
                    self.assertTrue(get_prompt("web_fetch_denied", host="example.com"))
                finally:
                    clear_runtime_cache()


if __name__ == "__main__":
    unittest.main()
