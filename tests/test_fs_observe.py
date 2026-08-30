from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from witty_agent.tools.fs import (
    _is_disproportionate_match,
    apply_patch,
    bind_workspace,
    edit,
    read,
    write,
)


class FsObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._flag = os.environ.get("WITTY_FS_OBSERVE")
        os.environ.pop("WITTY_FS_OBSERVE", None)

    def tearDown(self) -> None:
        if self._flag is None:
            os.environ.pop("WITTY_FS_OBSERVE", None)
        else:
            os.environ["WITTY_FS_OBSERVE"] = self._flag

    def test_write_and_edit_add_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            write("plain.txt", "hello")
            self.assertEqual((workspace / "plain.txt").read_bytes(), b"hello\n")
            write("keep.txt", "ok\n")
            self.assertEqual((workspace / "keep.txt").read_bytes(), b"ok\n")
            write("empty.txt", "")
            self.assertEqual((workspace / "empty.txt").read_bytes(), b"")
            write("crlf.txt", "a\r\nb")
            self.assertEqual((workspace / "crlf.txt").read_bytes(), b"a\r\nb\r\n")
            raw = workspace / "edit-me.txt"
            raw.write_bytes(b"old")
            read("edit-me.txt")
            edit("edit-me.txt", old_text="old", new_text="new")
            self.assertEqual(raw.read_bytes(), b"new\n")

    def test_apply_patch_add_file_only(self) -> None:
        from witty_agent.approval import is_dangerous
        from witty_agent.kernel_surface import KERNEL_TOOLS
        from witty_agent.plan_mode import MUTATING_TOOLS
        from witty_agent.tool_surface import CORE_TOOLS

        self.assertIn("apply_patch", KERNEL_TOOLS)
        self.assertIn("apply_patch", MUTATING_TOOLS)
        self.assertIn("apply_patch", CORE_TOOLS)
        self.assertTrue(is_dangerous("apply_patch"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            receipt = apply_patch(
                "*** Begin Patch\n"
                "*** Add File: pkg/hello.py\n"
                "+x = 1\n"
                "+y = 2\n"
                "*** End Patch\n"
            )
            self.assertIn("created", receipt)
            self.assertEqual(
                (workspace / "pkg" / "hello.py").read_text(encoding="utf-8"),
                "x = 1\ny = 2\n",
            )
            with self.assertRaises(ValueError) as existed:
                apply_patch(
                    "*** Begin Patch\n*** Add File: pkg/hello.py\n+z = 3\n*** End Patch\n"
                )
            self.assertIn("已存在", str(existed.exception))
            receipt = apply_patch("*** Add File: other.py\n+nope\n")
            self.assertIn("created", receipt)
            self.assertEqual((workspace / "other.py").read_text(encoding="utf-8"), "nope\n")
            with self.assertRaises(ValueError) as frame:
                apply_patch("not a patch at all")
            self.assertIn("Add File", str(frame.exception))

    def test_apply_patch_file_mode(self) -> None:
        import stat

        from witty_agent.patch_text import parse_file_mode

        self.assertEqual(parse_file_mode("755"), 0o755)
        self.assertEqual(parse_file_mode("100755"), 0o755)
        self.assertEqual(parse_file_mode("0644"), 0o644)
        with self.assertRaises(ValueError):
            parse_file_mode("bad")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            apply_patch(
                "*** Add File: bin/run.sh\n"
                "*** Mode: 755\n"
                "+#!/bin/sh\n"
                "+echo hi\n"
            )
            script = workspace / "bin" / "run.sh"
            self.assertEqual(script.stat().st_mode & stat.S_IMODE(0o777), 0o755)
            write("plain.sh", "echo\n")
            read("plain.sh")
            apply_patch("*** Update File: plain.sh\n*** Mode: 755\n")
            self.assertEqual((workspace / "plain.sh").stat().st_mode & 0o777, 0o755)
            with self.assertRaises(ValueError) as bad:
                apply_patch("*** Add File: x.sh\n*** Mode: xyz\n+hi\n")
            self.assertIn("Mode", str(bad.exception))
            read("plain.sh")
            with self.assertRaises(ValueError):
                apply_patch(
                    "*** Update File: plain.sh\n"
                    "*** Mode: 644\n"
                    "*** Update File: missing.txt\n"
                    "@@\n"
                    "-no\n"
                    "+yes\n"
                )
            self.assertEqual((workspace / "plain.sh").stat().st_mode & 0o777, 0o755)

    def test_apply_patch_implicit_frame_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            write("note.txt", "alpha\n")
            read("note.txt")
            receipt = apply_patch(
                "*** Update File: note.txt\n"
                "@@\n"
                "-alpha\n"
                "+beta\n"
            )
            self.assertIn("edited", receipt)
            self.assertEqual((workspace / "note.txt").read_text(encoding="utf-8"), "beta\n")

    def test_apply_patch_add_file_no_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            apply_patch(
                "*** Begin Patch\n"
                "*** Add File: raw.txt\n"
                "+hello\n"
                "*** End of File\n"
                "*** End Patch\n"
            )
            self.assertEqual((workspace / "raw.txt").read_bytes(), b"hello")
            apply_patch(
                "*** Begin Patch\n"
                "*** Add File: raw2.txt\n"
                "+world\n"
                "\\ No newline at end of file\n"
                "*** End Patch\n"
            )
            self.assertEqual((workspace / "raw2.txt").read_bytes(), b"world")
            apply_patch(
                "*** Begin Patch\n"
                "*** Add File: padded.txt\n"
                "+ok\n"
                "*** End Patch\n"
            )
            self.assertEqual((workspace / "padded.txt").read_bytes(), b"ok\n")

    def test_apply_patch_update_file_one_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "pkg" / "hello.py"
            target.parent.mkdir()
            target.write_text("x = 1\ny = 2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            with self.assertRaises(ValueError) as unread:
                apply_patch(
                    "*** Begin Patch\n"
                    "*** Update File: pkg/hello.py\n"
                    "@@\n"
                    " x = 1\n"
                    "-y = 2\n"
                    "+y = 9\n"
                    "*** End Patch\n"
                )
            self.assertIn("必须先 read", str(unread.exception))
            read("pkg/hello.py")
            receipt = apply_patch(
                "*** Begin Patch\n"
                "*** Update File: pkg/hello.py\n"
                "@@\n"
                " x = 1\n"
                "-y = 2\n"
                "+y = 9\n"
                "*** End Patch\n"
            )
            self.assertIn("edited", receipt)
            self.assertEqual(target.read_text(encoding="utf-8"), "x = 1\ny = 9\n")

    def test_apply_patch_update_end_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "note.txt"
            target.write_text("alpha\nalpha\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("note.txt")
            receipt = apply_patch(
                "*** Begin Patch\n"
                "*** Update File: note.txt\n"
                "@@\n"
                "-alpha\n"
                "+omega\n"
                "*** End of File\n"
                "*** End Patch\n"
            )
            self.assertTrue(receipt)
            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nomega\n")
            with self.assertRaises(ValueError) as mid:
                apply_patch(
                    "*** Begin Patch\n"
                    "*** Update File: note.txt\n"
                    "@@\n"
                    "-alpha\n"
                    "+nope\n"
                    "*** End of File\n"
                    "*** End Patch\n"
                )
            self.assertIn("末尾", str(mid.exception))
            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nomega\n")

    def test_apply_patch_change_file_is_update(self) -> None:
        from witty_agent.patch_text import parse_apply_patch

        hunks = parse_apply_patch(
            "*** Change File: note.txt\n@@\n-old\n+new\n"
        )
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].action, "update")
        self.assertEqual(hunks[0].path, "note.txt")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "note.txt"
            target.write_text("old\n", encoding="utf-8")
            bind_workspace(str(workspace))
            with self.assertRaises(ValueError) as unseen:
                apply_patch("*** Change File: note.txt\n@@\n-old\n+new\n")
            self.assertIn("先", str(unseen.exception))
            read("note.txt")
            receipt = apply_patch("*** Change File: note.txt\n@@\n-old\n+new\n")
            self.assertTrue(receipt)
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_patch_update_file_multi_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "pkg" / "hello.py"
            target.parent.mkdir()
            target.write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("pkg/hello.py")
            receipt = apply_patch(
                "*** Begin Patch\n"
                "*** Update File: pkg/hello.py\n"
                "@@\n"
                "-x = 1\n"
                "+x = 8\n"
                "@@\n"
                " y = 2\n"
                "-z = 3\n"
                "+z = 9\n"
                "*** End Patch\n"
            )
            self.assertEqual(receipt.count("edited"), 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "x = 8\ny = 2\nz = 9\n")

    def test_apply_patch_plus_only_eof_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "note.txt"
            target.write_text("keep\n", encoding="utf-8")
            bind_workspace(str(workspace))
            with self.assertRaisesRegex(ValueError, "没有可定位"):
                apply_patch("*** Update File: note.txt\n@@\n+tail\n")
            read("note.txt")
            with self.assertRaisesRegex(ValueError, "没有可定位"):
                apply_patch("*** Update File: note.txt\n@@\n+tail\n")
            receipt = apply_patch(
                "*** Update File: note.txt\n@@\n+tail\n*** End of File\n"
            )
            self.assertIn("updated", receipt)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\ntail\n")

    def test_apply_patch_plus_only_anchor_inserts(self) -> None:
        from witty_agent.patch_text import parse_apply_patch, update_replacements

        hunks = parse_apply_patch("*** Update File: mod.py\n@@ def foo\n+    extra()\n")
        self.assertEqual(update_replacements(hunks[0]), [("", "    extra()", "def foo")])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "mod.py"
            target.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            receipt = apply_patch("*** Update File: mod.py\n@@ def foo\n+    extra()\n")
            self.assertIn("updated", receipt)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "def foo():\n    extra()\n    return 1\n\ndef bar():\n    return 2\n",
            )
            with self.assertRaisesRegex(ValueError, "找不到"):
                apply_patch("*** Update File: mod.py\n@@ missing\n+nope\n")
            target.write_text("def foo():\n    extra()\ndef foo():\n    pass\n", encoding="utf-8")
            read("mod.py")
            with self.assertRaisesRegex(ValueError, "出现"):
                apply_patch("*** Update File: mod.py\n@@ def foo\n+    x\n")

    def test_apply_patch_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            keep = workspace / "keep.py"
            drop = workspace / "drop.py"
            keep.write_text("a = 1\n", encoding="utf-8")
            drop.write_text("bye\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("keep.py")
            read("drop.py")
            receipt = apply_patch(
                "*** Begin Patch\n"
                "*** Add File: new.py\n"
                "+ok = True\n"
                "*** Update File: keep.py\n"
                "@@\n"
                "-a = 1\n"
                "+a = 2\n"
                "*** Delete File: drop.py\n"
                "*** End Patch\n"
            )
            self.assertIn("created", receipt)
            self.assertIn("edited", receipt)
            self.assertIn("deleted", receipt)
            self.assertEqual((workspace / "new.py").read_text(encoding="utf-8"), "ok = True\n")
            self.assertEqual(keep.read_text(encoding="utf-8"), "a = 2\n")
            self.assertFalse(drop.exists())

    def test_apply_patch_rolls_back_on_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            keep = workspace / "keep.py"
            keep.write_text("a = 1\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("keep.py")
            with self.assertRaises(ValueError) as failed:
                apply_patch(
                    "*** Begin Patch\n"
                    "*** Add File: new.py\n"
                    "+ok = True\n"
                    "*** Update File: keep.py\n"
                    "@@\n"
                    "-no such line\n"
                    "+a = 9\n"
                    "*** End Patch\n"
                )
            self.assertIn("已回滚", str(failed.exception))
            self.assertFalse((workspace / "new.py").exists())
            self.assertEqual(keep.read_text(encoding="utf-8"), "a = 1\n")

    def test_apply_patch_rollback_removes_created_parents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            keep = workspace / "keep.py"
            keep.write_text("a = 1\n", encoding="utf-8")
            (workspace / "pkg").mkdir()
            bind_workspace(str(workspace))
            read("keep.py")
            with self.assertRaises(ValueError) as failed:
                apply_patch(
                    "*** Begin Patch\n"
                    "*** Add File: pkg/new/deep/hello.py\n"
                    "+ok = True\n"
                    "*** Update File: keep.py\n"
                    "@@\n"
                    "-no such line\n"
                    "+a = 9\n"
                    "*** End Patch\n"
                )
            self.assertIn("已回滚", str(failed.exception))
            self.assertFalse((workspace / "pkg" / "new").exists())
            self.assertTrue((workspace / "pkg").is_dir())
            self.assertEqual(keep.read_text(encoding="utf-8"), "a = 1\n")

    def test_apply_patch_delete_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "gone.py"
            target.write_text("drop me\n", encoding="utf-8")
            bind_workspace(str(workspace))
            with self.assertRaises(ValueError) as unread:
                apply_patch(
                    "*** Begin Patch\n*** Delete File: gone.py\n*** End Patch\n"
                )
            self.assertIn("必须先 read", str(unread.exception))
            self.assertTrue(target.is_file())
            read("gone.py")
            receipt = apply_patch(
                "*** Begin Patch\n*** Delete File: gone.py\n*** End Patch\n"
            )
            self.assertIn("deleted", receipt)
            self.assertFalse(target.exists())
            with self.assertRaises(ValueError) as again:
                apply_patch(
                    "*** Begin Patch\n*** Delete File: gone.py\n*** End Patch\n"
                )
            self.assertIn("不存在", str(again.exception))

    def test_apply_patch_move_file(self) -> None:
        from witty_agent.patch_text import apply_patch_paths

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            src = workspace / "pkg" / "hello.py"
            dest = workspace / "pkg" / "renamed.py"
            src.parent.mkdir()
            src.write_text("x = 1\ny = 2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            patch = (
                "*** Begin Patch\n"
                "*** Update File: pkg/hello.py\n"
                "*** Move to: pkg/renamed.py\n"
                "@@\n"
                "-x = 1\n"
                "+x = 8\n"
                "*** End Patch\n"
            )
            self.assertEqual(
                apply_patch_paths(patch),
                ["pkg/hello.py", "pkg/renamed.py"],
            )
            with self.assertRaises(ValueError) as unread:
                apply_patch(patch)
            self.assertIn("必须先 read", str(unread.exception))
            read("pkg/hello.py")
            receipt = apply_patch(patch)
            self.assertIn("moved", receipt)
            self.assertFalse(src.exists())
            self.assertEqual(dest.read_text(encoding="utf-8"), "x = 8\ny = 2\n")
            dest.write_text("taken\n", encoding="utf-8")
            src.write_text("again\n", encoding="utf-8")
            read("pkg/hello.py")
            with self.assertRaises(ValueError) as clash:
                apply_patch(
                    "*** Begin Patch\n"
                    "*** Update File: pkg/hello.py\n"
                    "*** Move to: pkg/renamed.py\n"
                    "*** End Patch\n"
                )
            self.assertIn("已存在", str(clash.exception))

    def test_apply_patch_rename_from_to(self) -> None:
        from witty_agent.patch_text import apply_patch_paths, parse_apply_patch

        parsed = parse_apply_patch(
            "*** Rename from: pkg/hello.py\n*** Rename to: pkg/renamed.py\n"
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].action, "move")
        self.assertEqual(parsed[0].path, "pkg/hello.py")
        self.assertEqual(parsed[0].dest, "pkg/renamed.py")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            src = workspace / "pkg" / "hello.py"
            dest = workspace / "pkg" / "renamed.py"
            src.parent.mkdir()
            src.write_text("x = 1\n", encoding="utf-8")
            bind_workspace(str(workspace))
            patch = (
                "*** Rename File: pkg/hello.py\n"
                "*** Rename to: pkg/renamed.py\n"
                "@@\n"
                "-x = 1\n"
                "+x = 8\n"
            )
            self.assertEqual(apply_patch_paths(patch), ["pkg/hello.py", "pkg/renamed.py"])
            with self.assertRaises(ValueError) as unread:
                apply_patch(patch)
            self.assertIn("必须先 read", str(unread.exception))
            read("pkg/hello.py")
            receipt = apply_patch(patch)
            self.assertIn("moved", receipt)
            self.assertFalse(src.exists())
            self.assertEqual(dest.read_text(encoding="utf-8"), "x = 8\n")

    def test_write_create_without_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            result = write("new.txt", "hello\n")
            self.assertTrue(result.startswith("wrote new.txt (created +1)"))
            self.assertIn("1|hello", result)
            self.assertEqual((workspace / "new.txt").read_text(encoding="utf-8"), "hello\n")

    def test_overwrite_requires_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("old\n", encoding="utf-8")
            bind_workspace(str(workspace))
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                write("note.txt", "new\n")
            with self.assertRaisesRegex(ValueError, "不算观察"):
                write("note.txt", "new\n")
            read("note.txt")
            result = write("note.txt", "new\n")
            self.assertTrue(result.startswith("wrote note.txt (updated +1 -1)"))
            self.assertIn("1|new", result)
            self.assertEqual((workspace / "note.txt").read_text(encoding="utf-8"), "new\n")

    def test_observation_is_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("old\n", encoding="utf-8")
            bind_workspace(str(workspace), "sess-a")
            read("note.txt")
            write("note.txt", "from-a\n")
            bind_workspace(str(workspace), "sess-b")
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                write("note.txt", "from-b\n")
            bind_workspace(str(workspace), "sess-a")
            write("note.txt", "again-a\n")
            self.assertEqual((workspace / "note.txt").read_text(encoding="utf-8"), "again-a\n")

    def test_forget_session_drops_observations(self) -> None:
        from witty_agent.fs_observe import (
            _MAX_OWNERS,
            forget_session,
            observation_owner_count,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("old\n", encoding="utf-8")
            bind_workspace(str(workspace), "sess-a")
            read("note.txt")
            self.assertGreaterEqual(forget_session("sess-a"), 1)
            bind_workspace(str(workspace), "sess-a")
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                write("note.txt", "after-forget\n")
            before = observation_owner_count()
            for index in range(_MAX_OWNERS + 4):
                bind_workspace(str(workspace), f"cap-{index}")
            self.assertLessEqual(observation_owner_count(), _MAX_OWNERS)
            self.assertLessEqual(observation_owner_count(), before + _MAX_OWNERS)

    def test_edit_requires_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("foo = 1\n", encoding="utf-8")
            bind_workspace(str(workspace))
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                edit("mod.py", old_text="foo = 1", new_text="foo = 2")
            with self.assertRaisesRegex(ValueError, "不要混 edit"):
                edit("mod.py", old_text="foo = 1", new_text="foo = 2")
            read("mod.py")
            result = edit("mod.py", old_text="foo = 1", new_text="foo = 2")
            self.assertTrue(result.startswith("edited mod.py (count=1 +1 -1)"))
            self.assertIn("1|foo = 2", result)
            self.assertEqual((workspace / "mod.py").read_text(encoding="utf-8"), "foo = 2\n")

    def test_edit_line_trimmed_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("    foo = 1\n    bar = 2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            result = edit(
                "mod.py",
                old_text="foo = 1\nbar = 2\n",
                new_text="    foo = 3\n    bar = 2\n",
            )
            self.assertTrue(result.startswith("edited mod.py (count=1"))
            self.assertEqual(
                (workspace / "mod.py").read_text(encoding="utf-8"),
                "    foo = 3\n    bar = 2\n",
            )
            with self.assertRaisesRegex(ValueError, "找不到这段 old_text"):
                edit("mod.py", old_text="missing = 0\n", new_text="x = 1\n")

    def test_edit_block_anchor_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text(
                "def process():\n    x = 1\n    y = 2\n    return x + y\n"
                "def process():\n    a = 9\n    b = 8\n    return x + y\n",
                encoding="utf-8",
            )
            bind_workspace(str(workspace))
            read("mod.py")
            result = edit(
                "mod.py",
                old_text="def process():\n    x = 1\n    y=2\n    return x + y\n",
                new_text="def process():\n    x = 3\n    y = 2\n    return x + y\n",
            )
            self.assertTrue(result.startswith("edited mod.py (count=1"))
            self.assertEqual(
                (workspace / "mod.py").read_text(encoding="utf-8"),
                "def process():\n    x = 3\n    y = 2\n    return x + y\n"
                "def process():\n    a = 9\n    b = 8\n    return x + y\n",
            )
            with self.assertRaisesRegex(ValueError, "首尾行锚定"):
                edit(
                    "mod.py",
                    old_text="foo = 1\nbar = 2\n",
                    new_text="foo = 0\nbar = 2\n",
                )

    def test_edit_whitespace_normalized_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("foo   =   1\nbar\t=\t2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            result = edit(
                "mod.py",
                old_text="foo = 1\nbar = 2\n",
                new_text="foo = 3\nbar = 2\n",
            )
            self.assertTrue(result.startswith("edited mod.py (count=1"))
            self.assertEqual(
                (workspace / "mod.py").read_text(encoding="utf-8"),
                "foo = 3\nbar = 2\n",
            )
            with self.assertRaisesRegex(ValueError, "空白归一"):
                edit("mod.py", old_text="missing = 0\n", new_text="x = 1\n")

    def test_edit_escape_normalized_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("foo = 1\nbar = 2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            result = edit(
                "mod.py",
                old_text="foo = 1\\nbar = 2\\n",
                new_text="foo = 3\nbar = 2\n",
            )
            self.assertTrue(result.startswith("edited mod.py (count=1"))
            self.assertEqual(
                (workspace / "mod.py").read_text(encoding="utf-8"),
                "foo = 3\nbar = 2\n",
            )
            with self.assertRaisesRegex(ValueError, "字面转义"):
                edit("mod.py", old_text="missing = 0\\n", new_text="x = 1\n")

    def test_edit_trimmed_boundary_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("keep\nfoo = 1\nbar = 2\nkeep\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            result = edit(
                "mod.py",
                old_text="\n\nfoo = 1\nbar = 2\n\n",
                new_text="foo = 3\nbar = 2",
            )
            self.assertTrue(result.startswith("edited mod.py (count=1"))
            self.assertEqual(
                (workspace / "mod.py").read_text(encoding="utf-8"),
                "keep\nfoo = 3\nbar = 2\nkeep\n",
            )
            with self.assertRaisesRegex(ValueError, "整块首尾空白"):
                edit("mod.py", old_text="\nmissing = 0\n", new_text="x = 1\n")

    def test_edit_context_aware_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text(
                "def process():\n    x = 1\n    y = 2\n"
                "    totally_different_call()\n    another_unrelated()\n    return x\n",
                encoding="utf-8",
            )
            bind_workspace(str(workspace))
            read("mod.py")
            result = edit(
                "mod.py",
                old_text=(
                    "def process():\n    x = 1\n    y = 2\n"
                    "    z = 3\n    w = 4\n    return x\n"
                ),
                new_text=(
                    "def process():\n    x = 3\n    y = 2\n"
                    "    z = 3\n    w = 4\n    return x\n"
                ),
            )
            self.assertTrue(result.startswith("edited mod.py (count=1"))
            self.assertEqual(
                (workspace / "mod.py").read_text(encoding="utf-8"),
                "def process():\n    x = 3\n    y = 2\n"
                "    z = 3\n    w = 4\n    return x\n",
            )
            with self.assertRaisesRegex(ValueError, "上下文半数对齐"):
                edit(
                    "mod.py",
                    old_text="def missing():\n    a = 1\n    return a\n",
                    new_text="def missing():\n    a = 2\n    return a\n",
                )

    def test_edit_refuses_disproportionate_span(self) -> None:
        self.assertTrue(_is_disproportionate_match("a\n" * 8, "a\nb"))
        self.assertFalse(_is_disproportionate_match("foo = 1\n", "foo = 1"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("a" + " " * 600 + "\nb\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            with self.assertRaisesRegex(ValueError, "远大于 old_text"):
                edit("mod.py", old_text="a\nb", new_text="c\nd")

    def test_bash_forgets_changed_observations(self) -> None:
        from witty_agent.prompts import get_prompt
        from witty_agent.tools.fs import bash

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "note.txt"
            target.write_text("v1\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("note.txt")
            result = bash("printf v2\\\\n > note.txt")
            self.assertIn(get_prompt("fs_bash_changed", paths="note.txt"), result)
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                edit("note.txt", old_text="v2", new_text="v3")
            read("note.txt")
            edited = edit("note.txt", old_text="v2", new_text="v3")
            self.assertIn("edited", edited)
            quiet = bash("true")
            self.assertNotIn("须再 read", quiet)

    def test_stale_after_external_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "note.txt"
            target.write_text("v1\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("note.txt")
            time.sleep(0.02)
            target.write_text("v2-changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "已变化"):
                write("note.txt", "v3\n")
            with self.assertRaisesRegex(ValueError, "已变化"):
                edit("note.txt", old_text="v2-changed", new_text="v3")
            read("note.txt")
            edited = edit("note.txt", old_text="v2-changed", new_text="v3")
            self.assertTrue(edited.startswith("edited note.txt (count=1 +1 -1)"))
            self.assertIn("1|v3", edited)

    def test_deleted_after_read_is_stale_then_recreate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / "note.txt"
            target.write_text("v1\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("note.txt")
            target.unlink()
            with self.assertRaisesRegex(ValueError, "已变化"):
                write("note.txt", "v2\n")
            with self.assertRaisesRegex(ValueError, "已变化"):
                edit("note.txt", old_text="v1", new_text="v2")
            with self.assertRaisesRegex(ValueError, "找不到"):
                read("note.txt")
            with self.assertRaisesRegex(ValueError, "不存在"):
                edit("note.txt", old_text="v1", new_text="v2")
            created = write("note.txt", "v2\n")
            self.assertTrue(created.startswith("wrote note.txt (created +1)"))
            self.assertEqual(target.read_text(encoding="utf-8"), "v2\n")

    def test_read_rejects_nul_and_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "ok.txt").write_text("你好\n", encoding="utf-8")
            (workspace / "nul.bin").write_bytes(b"pre\x00post")
            (workspace / "bad.bin").write_bytes(b"\xff\xfe binary")
            (workspace / "empty.txt").write_bytes(b"")
            bind_workspace(str(workspace))
            text = read("ok.txt")
            self.assertIn("你好", text)
            self.assertIn("1|", text)
            self.assertEqual(read("empty.txt").strip().split("\n")[0], "(文件结束，共 0 行)")
            with self.assertRaisesRegex(ValueError, "不是 UTF-8 文本"):
                read("nul.bin")
            with self.assertRaisesRegex(ValueError, "不是 UTF-8 文本"):
                read("bad.bin")
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                write("nul.bin", "plain\n")
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                edit("bad.bin", old_text="x", new_text="y")

    def test_read_directory_lists_one_level(self) -> None:
        from witty_agent.prompts import get_prompt

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("hi\n", encoding="utf-8")
            (workspace / "sub").mkdir()
            (workspace / "sub" / "deep.txt").write_text("nope\n", encoding="utf-8")
            bind_workspace(str(workspace))
            listing = read(".")
            self.assertIn("note.txt", listing)
            self.assertIn("sub/", listing)
            self.assertNotIn("deep.txt", listing)
            self.assertIn(get_prompt("read_dir_footer", path=".", count="2"), listing)
            empty = workspace / "void"
            empty.mkdir()
            self.assertIn(get_prompt("read_dir_empty"), read("void"))
            with self.assertRaisesRegex(ValueError, "必须先 read"):
                write("note.txt", "overwrite\n")

    def test_missing_read_then_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bind_workspace(str(workspace))
            with self.assertRaisesRegex(ValueError, "找不到"):
                read("gone.txt")
            with self.assertRaisesRegex(ValueError, "不存在"):
                edit("gone.txt", old_text="a", new_text="b")
            created = write("gone.txt", "ok\n")
            self.assertTrue(created.startswith("wrote gone.txt (created +1)"))
            self.assertIn("1|ok", created)

    def test_write_reports_line_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("a\nb\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("note.txt")
            added = write("note.txt", "a\nb\nc\n")
            self.assertTrue(added.startswith("wrote note.txt (updated +1 -0)"))
            self.assertIn("3|c", added)
            shrunk = write("note.txt", "a\n")
            self.assertTrue(shrunk.startswith("wrote note.txt (updated +0 -2)"))
            self.assertIn("1|a", shrunk)

    def test_edit_reports_line_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "mod.py").write_text("foo = 1\nbar = 2\n", encoding="utf-8")
            bind_workspace(str(workspace))
            read("mod.py")
            inserted = edit("mod.py", old_text="foo = 1\n", new_text="foo = 1\nbaz = 3\n")
            self.assertTrue(inserted.startswith("edited mod.py (count=1 +1 -0)"))
            self.assertIn("2|baz = 3", inserted)
            removed = edit("mod.py", old_text="baz = 3\n", new_text="")
            self.assertTrue(removed.startswith("edited mod.py (count=1 +0 -1)"))
            self.assertIn("1|foo = 1", removed)

    def test_edit_context_card_neighbors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            body = "".join(f"L{i}\n" for i in range(1, 10))
            (workspace / "note.txt").write_text(body, encoding="utf-8")
            bind_workspace(str(workspace))
            read("note.txt")
            result = edit("note.txt", old_text="L5", new_text="L5-changed")
            self.assertTrue(result.startswith("edited note.txt (count=1 +1 -1)"))
            self.assertIn("2|L2", result)
            self.assertIn("5|L5-changed", result)
            self.assertIn("8|L8", result)
            self.assertNotIn("1|L1", result)
            self.assertNotIn("9|L9", result)
            self.assertNotIn("-L5", result)

    def test_disable_env(self) -> None:
        os.environ["WITTY_FS_OBSERVE"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("old\n", encoding="utf-8")
            bind_workspace(str(workspace))
            written = write("note.txt", "new\n")
            self.assertTrue(written.startswith("wrote note.txt (updated +1 -1)"))
            edited = edit("note.txt", old_text="new", new_text="done")
            self.assertTrue(edited.startswith("edited note.txt (count=1 +1 -1)"))
