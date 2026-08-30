from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.host_context import host_environment, host_family, host_section, maybe_inject
from witty_agent.prompts import get_prompt
from witty_agent.runtime import clear_runtime_cache
from witty_agent.session_log import SessionLog
from witty_agent.system_prompt import build_system_prompt


class HostContextTests(unittest.TestCase):
    def test_family_mapping(self) -> None:
        self.assertEqual(host_family("Darwin"), "macos")
        self.assertEqual(host_family("Windows"), "windows")
        self.assertEqual(host_family("Linux"), "linux")
        self.assertEqual(host_family("win32"), "windows")

    def test_live_host_is_known(self) -> None:
        env = host_environment()
        self.assertIn(env["family"], {"macos", "windows", "linux"})
        self.assertEqual(env["label"], get_prompt(f"host_label_{env['family']}"))
        self.assertTrue(env["sep"])
        self.assertTrue(env["shell"])
        self.assertTrue(env["username"])
        self.assertIn("git", env)
        self.assertIn("network", env)
        self.assertIn(env["eol"], {"LF", "CRLF"})

    def test_section_and_prompt_use_detected_os(self) -> None:
        os.environ["WITTY_WEB_DENY_PUBLIC"] = "0"
        clear_runtime_cache()
        try:
            self._assert_section_and_prompt()
        finally:
            os.environ.pop("WITTY_WEB_DENY_PUBLIC", None)
            clear_runtime_cache()

    def _assert_section_and_prompt(self) -> None:
        mac = host_section(system="Darwin")
        self.assertIn("macOS", mac)
        self.assertIn("/", mac)
        self.assertIn("Git：", mac)
        self.assertIn("网络：", mac)
        self.assertIn(get_prompt("host_net_open"), mac)
        self.assertNotIn("默认禁止公网", mac)
        self.assertIn("代码沙箱：", mac)
        self.assertIn("沙箱 Python：", mac)
        self.assertIn("沙箱状态：", mac)
        self.assertIn(get_prompt("host_sandbox_policy"), mac)
        self.assertIn("which", mac)
        self.assertIn("试验代码", mac)
        self.assertIn("没有把 bash 锁死", get_prompt("host_sandbox_policy"))
        self.assertNotIn("workspace-write mode", get_prompt("sandbox_denied_outside", path="x"))
        once = get_prompt("host_context_once", **host_environment(system="Darwin"))
        self.assertIn("没有把 bash 锁死", once)
        self.assertNotIn("读写范围锁死", once)
        win = host_section(system="Windows")
        self.assertIn("Windows", win)
        self.assertIn("\\", win)
        text = build_system_prompt(".", tool_names=["read", "bash"])
        self.assertIn("## 本机环境", text)
        live = host_environment()
        self.assertIn(live["label"], text)

    def test_inject_once_per_session(self) -> None:
        log = SessionLog()
        first = maybe_inject(log, system="Darwin")
        self.assertIsNotNone(first)
        self.assertIn("macOS", first.content if first else "")
        self.assertIn(get_prompt("host_sandbox_policy"), first.content if first else "")
        log.append("user/message", {"text": first.content, "source": "plugin:host-context"})
        self.assertIsNone(maybe_inject(log, system="Darwin"))

    def test_tmp_dir_is_not_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = host_environment(cwd=Path(tmp))
            self.assertEqual(env["git"], get_prompt("host_git_none"))
            self.assertIn(get_prompt("host_git_none"), host_section(cwd=Path(tmp)))
