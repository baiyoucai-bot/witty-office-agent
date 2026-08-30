"""HTTP 令牌鉴权：不设 token 全放行（本机模式不变），设了必须带对的 Bearer 头。"""

from __future__ import annotations

import os
import unittest

from witty_agent.http_api import request_authorized
from witty_agent.prompts import get_prompt


class _Headers(dict):
    pass


class RequestAuthorizedTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("WITTY_API_TOKEN", None)

    def test_no_token_configured_allows_everything(self) -> None:
        self.assertTrue(request_authorized(_Headers()))
        self.assertTrue(request_authorized(_Headers({"Authorization": "Bearer whatever"})))

    def test_token_set_requires_matching_bearer(self) -> None:
        os.environ["WITTY_API_TOKEN"] = "secret-1"
        self.assertFalse(request_authorized(_Headers()))
        self.assertFalse(request_authorized(_Headers({"Authorization": "Bearer wrong"})))
        self.assertTrue(request_authorized(_Headers({"Authorization": "Bearer secret-1"})))

    def test_x_api_token_header_also_accepted(self) -> None:
        os.environ["WITTY_API_TOKEN"] = "secret-2"
        self.assertTrue(request_authorized(_Headers({"X-API-Token": "secret-2"})))
        self.assertFalse(request_authorized(_Headers({"X-API-Token": "nope"})))

    def test_whitespace_token_means_disabled(self) -> None:
        os.environ["WITTY_API_TOKEN"] = "   "
        self.assertTrue(request_authorized(_Headers()))

    def test_reject_message_names_the_fix(self) -> None:
        self.assertIn("WITTY_API_TOKEN", get_prompt("http_unauthorized"))


if __name__ == "__main__":
    unittest.main()
