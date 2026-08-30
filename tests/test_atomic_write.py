from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from witty_agent.atomic_write import write_file_atomic
from witty_agent.tools.fs import bind_workspace, read, write


class AtomicWriteTests(unittest.TestCase):
    def test_replace_leaves_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            write_file_atomic(path, "one\n")
            write_file_atomic(path, "two\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "two\n")
            leftovers = [item.name for item in Path(tmp).iterdir() if item.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_nested_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b.txt"
            write_file_atomic(path, "ok")
            self.assertEqual(path.read_text(encoding="utf-8"), "ok")

    def test_write_tool_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            write("new.txt", "hello\n")
            self.assertEqual((workspace / "new.txt").read_text(encoding="utf-8"), "hello\n")
            leftovers = [item.name for item in workspace.iterdir() if ".tmp" in item.name]
            self.assertEqual(leftovers, [])
            read("new.txt")
            write("new.txt", "world\n")
            self.assertEqual((workspace / "new.txt").read_text(encoding="utf-8"), "world\n")
            leftovers = [item.name for item in workspace.iterdir() if ".tmp" in item.name]
            self.assertEqual(leftovers, [])

    def test_new_file_mode_644(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            write_file_atomic(path, "x\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_preserves_existing_regular_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.sh"
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
            write_file_atomic(path, "#!/bin/sh\necho hi\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertEqual(path.read_text(encoding="utf-8"), "#!/bin/sh\necho hi\n")

    def test_explicit_mode_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.txt"
            write_file_atomic(path, "k\n", mode=0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            write_file_atomic(path, "k2\n", mode=0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_write_tool_preserves_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            script = workspace / "run.sh"
            script.write_text("old\n", encoding="utf-8")
            script.chmod(0o755)
            bind_workspace(str(workspace))
            read("run.sh")
            write("run.sh", "new\n")
            self.assertEqual(script.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)

    def test_failed_open_does_not_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            write_file_atomic(path, "keep\n")
            os.chmod(tmp, 0o555)
            try:
                with self.assertRaises(OSError):
                    write_file_atomic(path, "lost\n")
            finally:
                os.chmod(tmp, 0o755)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep\n")
