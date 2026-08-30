from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.tools.fs import bind_workspace, edit, read


class EditReplaceAllTests(unittest.TestCase):
    def test_unique_replace_and_replace_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "mod.py"
            target.write_text("foo = 1\nfoo = 2\nbar = foo\n", encoding="utf-8")
            bind_workspace(str(workspace))
            os.environ["WITTY_WORKSPACE"] = str(workspace)
            read("mod.py")
            with self.assertRaisesRegex(ValueError, "replace_all"):
                edit("mod.py", old_text="foo", new_text="qux")
            with self.assertRaisesRegex(ValueError, "找不到"):
                edit("mod.py", old_text="missing", new_text="x")
            result = edit("mod.py", old_text="foo", new_text="qux", replace_all=True)
            self.assertTrue(result.startswith("edited mod.py (count=3 +3 -3)"))
            self.assertIn("1|qux = 1", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "qux = 1\nqux = 2\nbar = qux\n")
            once = edit("mod.py", old_text="qux = 1", new_text="qux = 9")
            self.assertTrue(once.startswith("edited mod.py (count=1 +1 -1)"))
            self.assertIn("1|qux = 9", once)
            self.assertIn("qux = 9", target.read_text(encoding="utf-8"))
