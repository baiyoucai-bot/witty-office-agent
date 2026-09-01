"""chat 地址：已含 /chat/completions 的配置不再二次拼接。"""

from __future__ import annotations

import unittest

from witty_agent.llm import chat_completions_url


class ChatCompletionsUrlTests(unittest.TestCase):
    def test_appends_when_base_is_openai_root(self) -> None:
        self.assertEqual(
            chat_completions_url("http://host:8086/v1"),
            "http://host:8086/v1/chat/completions",
        )

    def test_keeps_full_endpoint(self) -> None:
        self.assertEqual(
            chat_completions_url("http://host:8086/v1/chat/completions"),
            "http://host:8086/v1/chat/completions",
        )

    def test_keeps_versioned_gateway_path(self) -> None:
        self.assertEqual(
            chat_completions_url("http://gw/api/llm/chat/completions/V2"),
            "http://gw/api/llm/chat/completions/V2",
        )

    def test_trailing_slash_on_full_endpoint(self) -> None:
        self.assertEqual(
            chat_completions_url("http://host/v1/chat/completions/"),
            "http://host/v1/chat/completions",
        )

    def test_empty(self) -> None:
        self.assertEqual(chat_completions_url(""), "")
        self.assertEqual(chat_completions_url("   "), "")
