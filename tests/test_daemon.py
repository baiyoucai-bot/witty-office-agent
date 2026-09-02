"""serve 进程活性：pidfile / 心跳 / status 判级 / stop。不真起 serve，全部对着文件和打桩。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from witty_agent import daemon
from witty_agent.prompts import get_prompt


class DaemonFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_pidfile_roundtrip_and_clear_only_own(self) -> None:
        daemon.write_pidfile("127.0.0.1", 8765, root=self.root)
        info = daemon.read_pidfile(self.root)
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(info["port"], 8765)
        # 别人的 pidfile 不清
        daemon.pid_path(self.root).write_text(json.dumps({"pid": 999999, "host": "h", "port": 1}), encoding="utf-8")
        daemon.clear_pidfile(self.root)
        self.assertTrue(daemon.pid_path(self.root).is_file())
        daemon.write_pidfile("127.0.0.1", 8765, root=self.root)
        daemon.clear_pidfile(self.root)
        self.assertFalse(daemon.pid_path(self.root).is_file())

    def test_status_stopped_when_no_pidfile(self) -> None:
        state = daemon.status(self.root)
        self.assertFalse(state.running)
        self.assertEqual(daemon.render_status(state), get_prompt("daemon_status_stopped"))

    def test_status_stopped_when_pid_is_dead(self) -> None:
        daemon.pid_path(self.root).write_text(json.dumps({"pid": 999999, "host": "h", "port": 1}), encoding="utf-8")
        with patch.object(daemon, "_pid_alive", return_value=False):
            self.assertFalse(daemon.status(self.root).running)

    def test_status_running_with_fresh_heartbeat(self) -> None:
        daemon.write_pidfile("127.0.0.1", 8765, root=self.root)
        daemon.write_heartbeat(self.root)
        state = daemon.status(self.root)
        self.assertTrue(state.running)
        self.assertFalse(state.stale)
        self.assertIn(str(os.getpid()), daemon.render_status(state))

    def test_status_stale_when_heartbeat_old(self) -> None:
        """进程活着但心跳过期 = 卡死，这是 pid 探活分不出来的那一档。"""
        daemon.write_pidfile("127.0.0.1", 8765, root=self.root)
        old = time.time() - daemon.HEARTBEAT_STALE_SEC - 5
        daemon.heartbeat_path(self.root).write_text(json.dumps({"pid": os.getpid(), "at": old}), encoding="utf-8")
        state = daemon.status(self.root)
        self.assertTrue(state.running)
        self.assertTrue(state.stale)
        self.assertIn("心跳", daemon.render_status(state))

    def test_heartbeat_thread_refreshes(self) -> None:
        beat = daemon.Heartbeat(self.root, interval=0.05)
        beat.start()
        try:
            first = daemon.read_heartbeat(self.root)
            time.sleep(0.2)
            second = daemon.read_heartbeat(self.root)
        finally:
            beat.stop()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreater(second, first)

    def test_stop_without_running_returns_1(self) -> None:
        with patch("builtins.print"):
            self.assertEqual(daemon.stop(self.root), 1)

    def test_stop_sends_sigterm_and_waits(self) -> None:
        daemon.pid_path(self.root).write_text(
            json.dumps({"pid": 424242, "host": "127.0.0.1", "port": 8765, "started_at": time.time()}),
            encoding="utf-8",
        )
        daemon.heartbeat_path(self.root).write_text(json.dumps({"pid": 424242, "at": time.time()}), encoding="utf-8")
        alive = {"value": True}

        def fake_alive(pid: int) -> bool:
            return alive["value"]

        def fake_kill(pid: int, sig: int) -> None:
            self.assertEqual(pid, 424242)
            alive["value"] = False

        with patch.object(daemon, "_pid_alive", side_effect=fake_alive), patch.object(daemon.os, "kill", side_effect=fake_kill), patch("builtins.print"):
            code = daemon.stop(self.root, timeout=2)
        self.assertEqual(code, 0)
        self.assertFalse(daemon.pid_path(self.root).exists())


class ServeCliTests(unittest.TestCase):
    def test_status_flag_exit_code_follows_liveness(self) -> None:
        from witty_agent import _serve_cli

        stopped = daemon.ServeStatus(False, None, "", 0, None, None, False)
        with patch.object(daemon, "status", return_value=stopped), patch("builtins.print"):
            self.assertEqual(_serve_cli(["--status"]), 1)
        running = daemon.ServeStatus(True, 1, "127.0.0.1", 8765, time.time(), time.time(), False)
        with patch.object(daemon, "status", return_value=running), patch("builtins.print"):
            self.assertEqual(_serve_cli(["--status"]), 0)

    def test_foreground_refuses_when_already_running(self) -> None:
        from witty_agent import _serve_cli

        running = daemon.ServeStatus(True, 1, "127.0.0.1", 8765, time.time(), time.time(), False)
        with patch.object(daemon, "status", return_value=running), patch("builtins.print") as shown:
            self.assertEqual(_serve_cli([]), 1)
        self.assertIn("已在运行", shown.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
