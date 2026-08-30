from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.tools.fs import _MAX_BYTES, _MAX_LINE, bind_workspace, read


class ReadWindowTests(unittest.TestCase):
    def test_eof_and_window_and_oob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
            bind_workspace(str(workspace))
            full = read("note.txt")
            self.assertIn("1|a", full)
            self.assertIn("4|d", full)
            self.assertIn(get_prompt("read_footer_eof", total="4"), full)
            self.assertNotIn("[truncated]", full)
            page = read("note.txt", offset=2, limit=2)
            self.assertIn("2|b", page)
            self.assertIn("3|c", page)
            self.assertNotIn("1|a", page)
            self.assertIn(
                get_prompt("read_footer_window", start="2", end="3", total="4", next="4"),
                page,
            )
            last = read("note.txt", offset=4, limit=2)
            self.assertIn("4|d", last)
            self.assertIn(get_prompt("read_footer_eof", total="4"), last)
            with self.assertRaisesRegex(ValueError, "超出"):
                read("note.txt", offset=9)

    def test_empty_file_and_long_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "empty.txt").write_text("", encoding="utf-8")
            (workspace / "long.txt").write_text("x" * (_MAX_LINE + 20) + "\n", encoding="utf-8")
            bind_workspace(str(workspace))
            empty = read("empty.txt")
            self.assertEqual(empty, get_prompt("read_footer_eof", total="0"))
            long = read("long.txt")
            self.assertIn(get_prompt("read_line_truncated", max=str(_MAX_LINE)), long)
            self.assertIn(get_prompt("read_footer_eof", total="1"), long)

    def test_counts_all_lines_then_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fat = "x" * 900
            body = "\n".join(f"{fat}{index:03d}" for index in range(400)) + "\n"
            self.assertGreater(len(body.encode("utf-8")), _MAX_BYTES)
            (workspace / "fat.txt").write_text(body, encoding="utf-8")
            bind_workspace(str(workspace))
            page = read("fat.txt", offset=1, limit=3)
            self.assertIn("1|" + fat + "000", page)
            self.assertIn("3|" + fat + "002", page)
            self.assertNotIn("4|" + fat + "003", page)
            self.assertIn(
                get_prompt("read_footer_window", start="1", end="3", total="400", next="4"),
                page,
            )
            self.assertNotIn("已截断", page)

    def test_window_byte_cap_still_counts_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            line = "y" * 800
            count = (_MAX_BYTES // 800) + 8
            body = "\n".join(line for _ in range(count)) + "\n"
            (workspace / "wide.txt").write_text(body, encoding="utf-8")
            bind_workspace(str(workspace))
            page = read("wide.txt", offset=1, limit=count)
            self.assertIn("已截断", page)
            self.assertIn("1|" + line, page)
            self.assertNotIn(f"total={count}", page)
