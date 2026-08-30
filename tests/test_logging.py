from __future__ import annotations

import unittest

from witty_agent.logging import get_logger, get_trace_id, redact, set_trace_id, setup_logging


class LoggingTests(unittest.TestCase):
    def test_get_logger_uses_package_prefix(self) -> None:
        setup_logging(level="INFO", force=True)
        logger = get_logger("skills")
        self.assertEqual(logger.logger.name, "witty_agent.skills")

    def test_trace_id_roundtrip(self) -> None:
        set_trace_id("abc123")
        self.assertEqual(get_trace_id(), "abc123")
        set_trace_id(None)
        self.assertIsNone(get_trace_id())

    def test_redact_secrets(self) -> None:
        self.assertEqual(redact("api_key=sk-test"), "<redacted>")
        self.assertEqual(redact("count=3"), "count=3")


if __name__ == "__main__":
    unittest.main()
