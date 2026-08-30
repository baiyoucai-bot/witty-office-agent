from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.runtime import sandbox_settings
from witty_agent.sandbox import (
    apply_exec_env,
    check_command_paths,
    command_path_tokens,
    display_path,
    ensure_sandbox,
    expand_jail_path,
    public_sandbox,
    resolve_allowed,
    rewrite_sandbox_tokens,
    rewrite_visible_paths,
    sandbox_python,
    sandbox_tmp,
    sandbox_venv,
    sandbox_work,
    space_key,
)
from witty_agent.tools.fs import bind_workspace, edit, read, write
from witty_agent.tools.search import find, grep, ls


class SandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "WITTY_SANDBOX_ENABLED",
                "WITTY_SANDBOX_PACKAGES",
                "WITTY_SANDBOX_INDEX",
                "WITTY_HOME",
                "WITTY_WORKSPACE",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_settings_env_override(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "0"
        os.environ["WITTY_SANDBOX_PACKAGES"] = "pyyaml, lxml"
        os.environ["WITTY_SANDBOX_INDEX"] = "https://example.invalid/simple"
        off = sandbox_settings()
        self.assertFalse(off["enabled"])
        self.assertEqual(off["packages"], ["pyyaml", "lxml"])
        self.assertEqual(off["index_url"], "https://example.invalid/simple")
        os.environ["WITTY_SANDBOX_ENABLED"] = "true"
        os.environ["WITTY_SANDBOX_PACKAGES"] = ""
        on = sandbox_settings()
        self.assertTrue(on["enabled"])
        self.assertEqual(on["packages"], [])

    def test_resolve_allowed_dual_root(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            workspace.mkdir()
            inside = resolve_allowed(str(workspace), "note.txt", root=home)
            self.assertEqual(inside, (workspace / "note.txt").resolve())
            boxed = resolve_allowed(str(workspace), "sandbox/demo.py", root=home)
            self.assertEqual(boxed, (sandbox_work(workspace=str(workspace), root=home) / "demo.py").resolve())
            tmp_boxed = resolve_allowed(str(workspace), "sandbox-tmp/scratch.txt", root=home)
            self.assertEqual(
                tmp_boxed,
                (sandbox_tmp(workspace=str(workspace), root=home) / "scratch.txt").resolve(),
            )
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), "../secret.txt", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), str(sandbox_venv(root=home) / "lib"), root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), "~/outside.txt", root=home)
            self.assertEqual(
                resolve_allowed(str(workspace), "$TMPDIR/scratch.txt", root=home),
                (sandbox_tmp(workspace=str(workspace), root=home) / "scratch.txt").resolve(),
            )
            self.assertEqual(
                resolve_allowed(str(workspace), "${WITTY_SANDBOX}/demo.py", root=home),
                (sandbox_work(workspace=str(workspace), root=home) / "demo.py").resolve(),
            )
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), "$VIRTUAL_ENV/lib", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), "$TMPDIR/../../../etc/passwd", root=home)

    def test_check_command_paths_denies_outside(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            workspace.mkdir()
            self.assertEqual(command_path_tokens("true"), [])
            self.assertEqual(command_path_tokens("cat note.txt"), [])
            self.assertIn("sandbox/demo.py", command_path_tokens("python sandbox/demo.py"))
            check_command_paths(str(workspace), "python sandbox/demo.py", root=home)
            check_command_paths(str(workspace), "cat sandbox-tmp/scratch.txt", root=home)
            check_command_paths(str(workspace), "ls .", root=home)
            nested = workspace / "sub"
            nested.mkdir()
            (workspace / "note.txt").write_text("ok\n", encoding="utf-8")
            check_command_paths(str(workspace), "cat ../note.txt", cwd=str(nested), root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "cat ../secret.txt", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), f"touch {sandbox_venv(root=home) / 'lib' / 'x'}", root=home)
            self.assertIn("/tmp/out", command_path_tokens("printf x>/tmp/out"))
            self.assertIn("/etc/passwd", command_path_tokens("cat note.txt>>/etc/passwd"))
            self.assertIn("../secret.txt", command_path_tokens("echo hi>../secret.txt"))
            check_command_paths(str(workspace), "printf x>/tmp/out", root=home)
            check_command_paths(str(workspace), "which claude >/dev/null", root=home)
            check_command_paths(str(workspace), "which claude 2>/dev/null; claude --version", root=home)
            check_command_paths(str(workspace), "npm --version", root=home)
            check_command_paths(str(workspace), "brew upgrade claude", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "echo hi>../secret.txt", root=home)
            check_command_paths(str(workspace), "printf x>note.txt", root=home)
            self.assertIn("$VIRTUAL_ENV", command_path_tokens("rm -rf $VIRTUAL_ENV"))
            self.assertIn("$TMPDIR/out", command_path_tokens("printf x>$TMPDIR/out"))
            self.assertIn("${WITTY_SANDBOX}/demo.py", command_path_tokens("python ${WITTY_SANDBOX}/demo.py"))
            check_command_paths(str(workspace), "printf x>$TMPDIR/out", root=home)
            check_command_paths(str(workspace), "cat ${WITTY_SANDBOX}/demo.py", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "echo x>$VIRTUAL_ENV/lib/x", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "rm -rf $VIRTUAL_ENV", root=home)
            saved_home = os.environ.get("HOME")
            user_home = home / "userhome"
            user_home.mkdir()
            os.environ["HOME"] = str(user_home)
            try:
                self.assertTrue(expand_jail_path("$HOME/secret.txt").endswith("secret.txt"))
                self.assertIn("$HOME/.ssh/id", command_path_tokens("cat $HOME/.ssh/id"))
                self.assertIn("${HOME}/.ssh/id", command_path_tokens("cat ${HOME}/.ssh/id"))
                check_command_paths(str(workspace), "cat $HOME/.ssh/id", root=home)
                with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                    resolve_allowed(str(workspace), "$HOME/secret.txt", root=home)
                os.environ["HOME"] = str(workspace)
                check_command_paths(str(workspace), "cat $HOME/note.txt", root=home)
            finally:
                if saved_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved_home

    def test_pwd_and_cd_jail(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            nested = workspace / "sub"
            nested.mkdir(parents=True)
            (workspace / "note.txt").write_text("ok\n", encoding="utf-8")
            self.assertEqual(
                resolve_allowed(str(workspace), "$PWD/note.txt", root=home),
                (workspace / "note.txt").resolve(),
            )
            self.assertEqual(
                resolve_allowed(str(workspace), "${PWD}/sub", root=home),
                nested.resolve(),
            )
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), "$PWD/../secret.txt", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                resolve_allowed(str(workspace), "$OLDPWD/note.txt", root=home)
            self.assertIn("$PWD/../secret.txt", command_path_tokens("cat $PWD/../secret.txt"))
            self.assertIn("$OLDPWD", command_path_tokens("cat $OLDPWD"))
            check_command_paths(str(workspace), "cat $PWD/note.txt", root=home)
            check_command_paths(str(workspace), "cd sub && cat ../note.txt", root=home)
            check_command_paths(str(workspace), "cd sub && cat $OLDPWD/note.txt", root=home)
            check_command_paths(str(workspace), "cd -- sub && cat $PWD/../note.txt", root=home)
            check_command_paths(str(workspace), "builtin cd sub && ls .", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "cat $PWD/../secret.txt", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "echo x>$PWD/../secret.txt", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "cd .. && cat secret.txt", root=home)
            check_command_paths(str(workspace), "cd /tmp && cat x", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "cat $OLDPWD/note.txt", root=home)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                check_command_paths(str(workspace), "cd -", root=home)

    def test_project_sandbox_dir_not_hijacked(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            local = workspace / "sandbox"
            local.mkdir(parents=True)
            (local / "keep.py").write_text("ok\n", encoding="utf-8")
            resolved = resolve_allowed(str(workspace), "sandbox/keep.py", root=home)
            self.assertEqual(resolved, (local / "keep.py").resolve())
            self.assertEqual(rewrite_sandbox_tokens("python sandbox/keep.py", workspace=str(workspace)), "python sandbox/keep.py")

    def test_spaces_are_per_workspace(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            one = home / "a"
            two = home / "b"
            one.mkdir()
            two.mkdir()
            self.assertNotEqual(space_key(str(one)), space_key(str(two)))
            self.assertNotEqual(
                sandbox_work(workspace=str(one), root=home),
                sandbox_work(workspace=str(two), root=home),
            )

    def test_rewrite_and_public(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        os.environ["WITTY_SANDBOX_PACKAGES"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            workspace.mkdir()
            work = sandbox_work(workspace=str(workspace), root=home)
            rewritten = rewrite_sandbox_tokens("python sandbox/demo.py", workspace=str(workspace), root=home)
            folded = rewritten.replace("\\", "/")
            self.assertIn(str(work.resolve()).replace("\\", "/"), folded)
            self.assertTrue(folded.endswith("/demo.py"))
            self.assertNotIn("sandbox/demo.py", rewritten)
            tmp_dir = sandbox_tmp(workspace=str(workspace), root=home)
            tmp_cmd = rewrite_sandbox_tokens(
                "cat sandbox-tmp/scratch.txt", workspace=str(workspace), root=home
            )
            self.assertIn(str(tmp_dir.resolve()).replace("\\", "/"), tmp_cmd.replace("\\", "/"))
            self.assertNotIn("sandbox-tmp/scratch.txt", tmp_cmd)
            env_cmd = rewrite_sandbox_tokens("cat $TMPDIR/scratch.txt", workspace=str(workspace), root=home)
            self.assertIn(str(tmp_dir.resolve()).replace("\\", "/"), env_cmd.replace("\\", "/"))
            self.assertNotIn("$TMPDIR", env_cmd)
            work_cmd = rewrite_sandbox_tokens(
                "python ${WITTY_SANDBOX}/demo.py", workspace=str(workspace), root=home
            )
            self.assertIn(str(work.resolve()).replace("\\", "/"), work_cmd.replace("\\", "/"))
            self.assertNotIn("WITTY_SANDBOX", work_cmd)
            box = public_sandbox(workspace=str(workspace), root=home)
            self.assertEqual(box["enabled"], "true")
            self.assertEqual(box["ready"], "false")
            self.assertEqual(box["work"], str(work))
            self.assertEqual(box["python"], str(sandbox_python(root=home)))
            shown = display_path(work / "demo.py", str(workspace), root=home)
            self.assertEqual(shown, "sandbox/demo.py")
            visible = rewrite_visible_paths(f"{work / 'demo.py'}:1:hi", str(workspace), root=home)
            self.assertIn("sandbox/demo.py", visible)
        os.environ["WITTY_SANDBOX_ENABLED"] = "0"
        self.assertEqual(rewrite_sandbox_tokens("python sandbox/demo.py"), "python sandbox/demo.py")
        self.assertEqual(public_sandbox()["enabled"], "false")

    def test_exec_env_uses_sandbox_python(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        os.environ["WITTY_SANDBOX_PACKAGES"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            workspace.mkdir()
            snap = ensure_sandbox(workspace=str(workspace), root=home)
            self.assertTrue(snap.python.is_file())
            self.assertTrue(snap.work.is_dir())
            self.assertTrue(snap.tmp.is_dir())
            env = apply_exec_env({"PATH": "/usr/bin", "WITTY_WORKSPACE": str(workspace)}, root=home)
            self.assertTrue(env["PATH"].startswith(str(snap.python.parent)))
            self.assertEqual(env["VIRTUAL_ENV"], str(snap.venv))
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            self.assertEqual(env["UV_NO_PROJECT"], "1")
            self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(snap.venv))
            self.assertEqual(env["WITTY_SANDBOX"], str(snap.work))
            self.assertEqual(env["TMPDIR"], str(sandbox_tmp(workspace=str(workspace), root=home)))
            self.assertEqual(public_sandbox(workspace=str(workspace), root=home)["ready"], "true")
        os.environ["WITTY_SANDBOX_ENABLED"] = "0"
        skipped = apply_exec_env({"PATH": "/usr/bin"}, root=home)
        self.assertEqual(skipped["PATH"], "/usr/bin")
        self.assertNotIn("WITTY_SANDBOX", skipped)

    def test_exec_env_strips_shell_rc(self) -> None:
        dirty = {"PATH": "/usr/bin", "BASH_ENV": "/tmp/trap.sh", "ENV": "/tmp/trap.sh"}
        os.environ["WITTY_SANDBOX_ENABLED"] = "0"
        off = apply_exec_env(dirty)
        self.assertEqual(off["PATH"], "/usr/bin")
        self.assertNotIn("BASH_ENV", off)
        self.assertNotIn("ENV", off)
        self.assertIn("BASH_ENV", dirty)
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        os.environ["WITTY_SANDBOX_PACKAGES"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / "ws"
            workspace.mkdir()
            on = apply_exec_env(
                {**dirty, "WITTY_WORKSPACE": str(workspace)},
                root=home,
            )
            self.assertNotIn("BASH_ENV", on)
            self.assertNotIn("ENV", on)
            self.assertTrue(on["PATH"])

    def test_write_replaces_symlink_does_not_follow(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            os.environ["WITTY_HOME"] = str(home)
            workspace = home / "ws"
            workspace.mkdir()
            inside = workspace / "target.txt"
            inside.write_text("keep\n", encoding="utf-8")
            outside = home / "secret.txt"
            outside.write_text("secret\n", encoding="utf-8")
            (workspace / "alias.txt").symlink_to(inside)
            (workspace / "leak.txt").symlink_to(outside)
            bind_workspace(str(workspace))
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                read("leak.txt")
            created = write("leak.txt", "boxed\n")
            self.assertTrue(created.startswith("wrote leak.txt (created"))
            self.assertEqual((workspace / "leak.txt").read_text(encoding="utf-8"), "boxed\n")
            self.assertFalse((workspace / "leak.txt").is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")
            replaced = write("alias.txt", "copy\n")
            self.assertTrue(replaced.startswith("wrote alias.txt (created"))
            self.assertEqual((workspace / "alias.txt").read_text(encoding="utf-8"), "copy\n")
            self.assertFalse((workspace / "alias.txt").is_symlink())
            self.assertEqual(inside.read_text(encoding="utf-8"), "keep\n")
            (workspace / "again.txt").symlink_to(inside)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                edit("again.txt", old_text="keep", new_text="nope")
            self.assertTrue((workspace / "again.txt").is_symlink())
            self.assertEqual(inside.read_text(encoding="utf-8"), "keep\n")

    def test_write_accepts_sandbox_env_aliases(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            os.environ["WITTY_HOME"] = str(home)
            workspace = home / "ws"
            workspace.mkdir()
            bind_workspace(str(workspace))
            created = write("$TMPDIR/scratch.txt", "tmp-token\n")
            self.assertTrue(created.startswith("wrote sandbox-tmp/scratch.txt (created"))
            self.assertEqual(
                (sandbox_tmp(workspace=str(workspace), root=home) / "scratch.txt").read_text(encoding="utf-8"),
                "tmp-token\n",
            )
            boxed = write("$WITTY_SANDBOX/demo.py", "work-token\n")
            self.assertTrue(boxed.startswith("wrote sandbox/demo.py (created"))
            self.assertEqual(
                (sandbox_work(workspace=str(workspace), root=home) / "demo.py").read_text(encoding="utf-8"),
                "work-token\n",
            )
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                write("$VIRTUAL_ENV/evil.py", "nope\n")
        os.environ["WITTY_SANDBOX_ENABLED"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            literal = resolve_allowed(str(workspace), "$TMPDIR/scratch.txt")
            self.assertEqual(literal, (workspace / "$TMPDIR" / "scratch.txt").resolve())

    def test_write_ls_find_grep_use_prefix(self) -> None:
        os.environ["WITTY_SANDBOX_ENABLED"] = "1"
        os.environ["WITTY_SANDBOX_PACKAGES"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            os.environ["WITTY_HOME"] = str(home)
            workspace = home / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("workspace-token\n", encoding="utf-8")
            bind_workspace(str(workspace))
            result = write("sandbox/demo.py", "sandbox-token = 1\n")
            self.assertTrue(result.startswith("wrote sandbox/demo.py (created +1)"))
            self.assertIn("1|sandbox-token = 1", result)
            listing = ls(".")
            self.assertIn("sandbox/", listing)
            self.assertIn("sandbox-tmp/", listing)
            self.assertIn("note.txt", listing)
            self.assertIn("sandbox/demo.py", find("*.py"))
            write("sandbox-tmp/scratch.txt", "tmp-token\n")
            self.assertIn("sandbox-tmp/scratch.txt", find("*.txt"))
            hits = grep("sandbox-token", ".", "*.py")
            self.assertIn("sandbox/demo.py", hits)
            self.assertIn("sandbox-token", hits)
            tmp_hits = grep("tmp-token", ".", "*.txt")
            self.assertIn("sandbox-tmp/scratch.txt", tmp_hits)
