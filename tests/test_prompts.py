from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.http_api import handle_request
from witty_agent.prompts import (
    clear_prompt_cache,
    get_prompt,
    get_prompt_record,
    public_prompt_index,
    save_prompt,
)

_MINI = """# header

[prompts]
harness_system = "base line"
guideline_cite = "Cite {source}."
"""


class PromptConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "prompts.toml"
        self.path.write_text(_MINI, encoding="utf-8")
        self._old = os.environ.get("WITTY_PROMPTS_FILE")
        os.environ["WITTY_PROMPTS_FILE"] = str(self.path)
        clear_prompt_cache()

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("WITTY_PROMPTS_FILE", None)
        else:
            os.environ["WITTY_PROMPTS_FILE"] = self._old
        clear_prompt_cache()
        self._tmp.cleanup()

    def test_index_and_record(self) -> None:
        index = public_prompt_index()
        names = [item["name"] for item in index["prompts"]]
        self.assertEqual(names, ["harness_system", "guideline_cite"])
        self.assertEqual(index["prompts"][0]["chars"], len("base line"))
        record = get_prompt_record("guideline_cite")
        self.assertEqual(record["text"], "Cite {source}.")

    def test_save_roundtrip_and_placeholders(self) -> None:
        saved = save_prompt("guideline_cite", "Always cite {source}.")
        self.assertEqual(saved["chars"], len("Always cite {source}."))
        self.assertEqual(get_prompt("guideline_cite", source="prefs"), "Always cite prefs.")
        reloaded = self.path.read_text(encoding="utf-8")
        self.assertIn("Always cite {source}.", reloaded)
        self.assertIn("harness_system", reloaded)

    def test_save_rejects_unknown_and_empty(self) -> None:
        with self.assertRaises(KeyError):
            save_prompt("not_a_key", "x")
        with self.assertRaises(ValueError):
            save_prompt("harness_system", "   ")

    async def test_http_list_get_put(self) -> None:
        status, listed = await handle_request("GET", "/v1/prompts")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["prompts"]), 2)
        status, body = await handle_request("GET", "/v1/prompts/harness_system")
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "base line")
        status, saved = await handle_request(
            "PUT",
            "/v1/prompts/harness_system",
            {"text": "updated harness"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["text"], "updated harness")
        status, missing = await handle_request("GET", "/v1/prompts/nope")
        self.assertEqual(status, 404)
        status, empty = await handle_request("PUT", "/v1/prompts/harness_system", {"text": ""})
        self.assertEqual(status, 400)
