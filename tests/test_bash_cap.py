from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.tools.fs import (
    _MAX_BYTES,
    _clip_tool_output,
    _format_bash_result,
    _join_streams,
    _signal_marker,
    bash,
    bind_workspace,
)


class BashCapTests(unittest.TestCase):
    def test_clip_keeps_short_output(self) -> None:
        self.assertEqual(_clip_tool_output("ok\n"), "ok\n")

    def test_clip_empty_is_no_output(self) -> None:
        self.assertEqual(_clip_tool_output(""), get_prompt("bash_no_output"))
        self.assertEqual(_clip_tool_output(" \n"), " \n")

    def test_bash_ignores_bash_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bind_workspace(tmp)
            trap = Path(tmp) / "trap.sh"
            trap.write_text("echo TRAPPED\n", encoding="utf-8")
            saved = {key: os.environ.get(key) for key in ("BASH_ENV", "ENV")}
            os.environ["BASH_ENV"] = str(trap)
            os.environ["ENV"] = str(trap)
            try:
                result = bash("printf ok")
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertIn("ok", result)
            self.assertNotIn("TRAPPED", result)

    def test_bash_runs_non_login_argv(self) -> None:
        from witty_agent.sandbox import bash_argv

        self.assertEqual(
            bash_argv("printf hi"),
            ["bash", "--noprofile", "--norc", "-c", "printf hi"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            bind_workspace(tmp)
            result = bash("printf %s \"$0\"")
            self.assertIn("bash", result)
            self.assertNotIn("/bin/sh", result)

    def test_bash_true_uses_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bind_workspace(tmp)
            result = bash("true")
            self.assertEqual(result, f"exit=0\n{get_prompt('bash_no_output')}")

    def test_bash_denies_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bind_workspace(tmp)
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                bash("cat ../secret.txt")
            marker = get_prompt("sandbox_denied_outside", path="../secret.txt")
            with self.assertRaises(ValueError) as caught:
                bash("cat ../secret.txt")
            self.assertEqual(str(caught.exception), marker)
            bash("printf x>/tmp/witty-jail-out")
            saved_home = os.environ.get("HOME")
            os.environ["HOME"] = str(Path(tmp).parent)
            try:
                bash("cat $HOME/outside.txt")
            finally:
                if saved_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved_home
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                bash("echo x>$VIRTUAL_ENV/lib/x")
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                bash("rm -rf $VIRTUAL_ENV")
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                bash("cat $PWD/../secret.txt")
            bash("cd /tmp && printf x")

    def test_join_streams_optional_stderr(self) -> None:
        header = get_prompt("bash_stderr_header")
        self.assertEqual(_join_streams("out\n", ""), "out\n")
        self.assertEqual(_join_streams("", "err\n"), f"{header}\nerr\n")
        self.assertEqual(_join_streams("out", "err\n"), f"out\n{header}\nerr\n")
        self.assertEqual(_join_streams("out\n", "err\n"), f"out\n{header}\nerr\n")

    def test_bash_stderr_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bind_workspace(tmp)
            result = bash("printf out; printf err >&2")
            header = get_prompt("bash_stderr_header")
            self.assertEqual(result, f"exit=0\nout\n{header}\nerr")
            stdout_only = bash("printf out")
            self.assertEqual(stdout_only, "exit=0\nout")
            self.assertNotIn(header, stdout_only)

    def test_signal_marker(self) -> None:
        self.assertEqual(_signal_marker(0), "")
        self.assertEqual(_signal_marker(1), "")
        self.assertEqual(
            _signal_marker(-15),
            get_prompt("bash_killed_by_signal", signal="SIGTERM"),
        )
        self.assertEqual(
            _signal_marker(-9),
            get_prompt("bash_killed_by_signal", signal="SIGKILL"),
        )
        self.assertEqual(
            _format_bash_result(-15, "", ""),
            "exit=-15\n"
            + get_prompt("bash_no_output")
            + "\n"
            + get_prompt("bash_killed_by_signal", signal="SIGTERM"),
        )
        self.assertEqual(_format_bash_result(0, "ok\n", ""), "exit=0\nok\n")

    def test_bash_killed_by_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bind_workspace(tmp)
            result = bash("kill -s TERM $$")
            self.assertIn(get_prompt("bash_killed_by_signal", signal="SIGTERM"), result)
            self.assertIn("exit=-15\n", result)

    def test_clip_footer_with_spill(self) -> None:
        flag = os.environ.get("WITTY_SCRATCHPAD")
        with tempfile.TemporaryDirectory() as tmp:
            pad = Path(tmp)
            os.environ["WITTY_SCRATCHPAD"] = str(pad)
            try:
                body = "h" + ("x" * _MAX_BYTES)
                page = _clip_tool_output(body)
                spilled = pad / "truncated-tool-output.txt"
                footer = get_prompt(
                    "bash_footer_capped",
                    shown=str(_MAX_BYTES),
                    total=str(len(body)),
                    path=str(spilled),
                )
                self.assertTrue(page.endswith(footer))
                self.assertNotIn("[truncated]", page)
                self.assertFalse(page.startswith("h"))
                self.assertEqual(spilled.read_text(encoding="utf-8"), body)
            finally:
                if flag is None:
                    os.environ.pop("WITTY_SCRATCHPAD", None)
                else:
                    os.environ["WITTY_SCRATCHPAD"] = flag

    def test_clip_footer_without_spill(self) -> None:
        flag = os.environ.get("WITTY_SCRATCHPAD")
        os.environ.pop("WITTY_SCRATCHPAD", None)
        try:
            body = "y" * (_MAX_BYTES + 8)
            page = _clip_tool_output(body)
            footer = get_prompt(
                "bash_footer_capped",
                shown=str(_MAX_BYTES),
                total=str(len(body)),
                path=get_prompt("bash_footer_unavailable"),
            )
            self.assertTrue(page.endswith(footer))
            self.assertNotIn("[truncated]", page)
        finally:
            if flag is None:
                os.environ.pop("WITTY_SCRATCHPAD", None)
            else:
                os.environ["WITTY_SCRATCHPAD"] = flag
