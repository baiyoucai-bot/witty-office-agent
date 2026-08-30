"""持久 Python 解释器。

这些测试盯的不是「能不能跑代码」——那是最容易过的一条。盯的是**状态的诚实性**：
变量该在的时候在，不该在的时候必须有人明说不在。一个静默重启的空解释器比一个报错的
解释器危险得多，因为模型会拿着 `status=ok` 继续用已经不存在的变量。
"""

from __future__ import annotations

import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path

from witty_agent.prompts import get_prompt
from witty_agent.repl import DRIVER_NAME, CELL_END, ReplHost, _clip
from witty_agent.runtime import clear_runtime_cache, repl_settings

_HOME: tempfile.TemporaryDirectory | None = None
_PREV: dict[str, str | None] = {}


def setUpModule() -> None:
    """整个模块共用一个沙箱 venv，并且把预装包清空。

    `[sandbox].packages` 默认十三个（pandas / matplotlib / rapidocr…），每个测试各建一份
    venv 会把这个文件跑成分钟级。解释器的语义跟装了什么包无关，所以这里装零个。
    """
    global _HOME
    _HOME = tempfile.TemporaryDirectory()
    root = Path(_HOME.name)
    runtime = root / "runtime.toml"
    runtime.write_text(
        '[sandbox]\nenabled = true\npackages = []\n[repl]\nenabled = true\ntimeout_sec = 60\nmax_output_chars = 16384\n',
        encoding="utf-8",
    )
    for key, value in (("WITTY_HOME", str(root / "data")), ("WITTY_RUNTIME_FILE", str(runtime))):
        _PREV[key] = os.environ.get(key)
        os.environ[key] = value
    clear_runtime_cache()


def tearDownModule() -> None:
    for key, value in _PREV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    clear_runtime_cache()
    if _HOME is not None:
        _HOME.cleanup()


class ReplStateTests(unittest.TestCase):
    """一个解释器活着的时候，变量必须真的留着。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._work = tempfile.TemporaryDirectory()
        cls.host = ReplHost(workspace=cls._work.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.host.close()
        cls._work.cleanup()

    def test_variables_survive_between_calls(self) -> None:
        """这就是整个工具存在的理由：上一格的结果下一格直接拿，不落盘也不重读。"""
        self.host.run("carried = list(range(1000))", restart=True)
        result = self.host.run("len(carried)")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "1000")

    def test_only_what_you_print_comes_back(self) -> None:
        """一万行留在变量里，回到上下文的只有你要的那一行。"""
        self.host.run("rows = ['line %d' % i for i in range(10000)]", restart=True)
        result = self.host.run("[r for r in rows if r.endswith('7777')]")
        self.assertEqual(result.output, "['line 7777']")
        self.assertLess(len(result.output), 200)

    def test_trailing_expression_echoes_and_binds_underscore(self) -> None:
        self.host.run("a = 6\nb = 7\na * b", restart=True)
        self.assertEqual(self.host.run("_").output, "42")

    def test_statements_only_say_no_output(self) -> None:
        result = self.host.run("quiet = 1")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, get_prompt("repl_no_output"))

    def test_none_is_not_echoed(self) -> None:
        self.assertEqual(self.host.run("print('shown')\nNone").output, "shown")

    def test_error_keeps_the_interpreter_and_the_variables(self) -> None:
        self.host.run("kept = 'still here'", restart=True)
        failed = self.host.run("1 / 0")
        self.assertEqual(failed.status, "error")
        self.assertIn("ZeroDivisionError", failed.output)
        after = self.host.run("kept")
        self.assertTrue(after.ok)
        self.assertEqual(after.output, "'still here'")

    def test_traceback_hides_the_driver_frames(self) -> None:
        """报错里出现框架的文件名，模型会去查一个不存在的框架问题。"""
        output = self.host.run("def outer():\n    return missing_name\nouter()").output
        self.assertIn("NameError", output)
        self.assertIn("<cell>", output)
        self.assertNotIn(DRIVER_NAME, output)
        self.assertNotIn("in run", output)

    def test_syntax_error_reports_position_and_survives(self) -> None:
        broken = self.host.run("def (:")
        self.assertEqual(broken.status, "error")
        self.assertIn("SyntaxError", broken.output)
        self.assertNotIn(DRIVER_NAME, broken.output)
        self.assertTrue(self.host.run("1 + 1").ok)

    def test_cell_cannot_forge_the_end_marker(self) -> None:
        """哨兵带 nonce，正是为了让单元格打印不出一个能骗过框架的结束标记。

        没有 nonce 的话，一格打印了标记文本就会被当成本格结束，后面真正的输出串到下一格。
        """
        result = self.host.run(f"print('{CELL_END} 0 1 ok')\nprint('after the fake')\n'done'")
        self.assertTrue(result.ok)
        self.assertIn("after the fake", result.output)
        self.assertIn("'done'", result.output)

    def test_non_ascii_code_does_not_desync_the_pipe(self) -> None:
        """长度头是字符数不是字节数。搞错了这一格会少读几个字，后面每一格都跟着错位。"""
        result = self.host.run("标签 = '办公场景'\nlen(标签)")
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.output, "4")
        self.assertTrue(self.host.run("'ok'").ok)

    def test_cwd_is_the_workspace(self) -> None:
        (Path(self._work.name) / "marker.txt").write_text("hi\n", encoding="utf-8")
        result = self.host.run("open('marker.txt').read().strip()")
        self.assertEqual(result.output, "'hi'")

    def test_big_output_keeps_the_tail(self) -> None:
        result = self.host.run("for i in range(4000):\n    print('row', i)", timeout_sec=30)
        settings = repl_settings()
        self.assertLessEqual(len(result.output), int(settings["max_output_chars"]) + 200)
        self.assertIn("row 3999", result.output)
        self.assertNotIn("row 0\n", result.output)

    def test_restart_clears_the_namespace_and_says_so(self) -> None:
        self.host.run("gone_soon = 1")
        result = self.host.run("'gone_soon' in dir()", restart=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "False")
        self.assertTrue(result.restarted)

    def test_concurrent_cells_do_not_interleave_on_the_pipe(self) -> None:
        """两个线程同时递单元格，锁必须让它们排队；否则长度头和正文会串。"""
        self.host.run("import time", restart=True)
        seen: list[str] = []

        def cell(name: str) -> None:
            seen.append(self.host.run(f"time.sleep(0.05)\n'{name}'").output)

        threads = [threading.Thread(target=cell, args=(f"t{i}",)) for i in range(4)]
        for item in threads:
            item.start()
        for item in threads:
            item.join()
        self.assertEqual(sorted(seen), ["'t0'", "'t1'", "'t2'", "'t3'"])


class ReplLossTests(unittest.TestCase):
    """状态没了的每一条路径，都必须有人明说。"""

    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self.host = ReplHost(workspace=self._work.name)

    def tearDown(self) -> None:
        self.host.close()
        self._work.cleanup()

    def test_external_kill_is_announced_not_swallowed(self) -> None:
        """最坏的一种成功：换了个空解释器却回一句 ok。

        新解释器照样能把这一格跑成功，所以只看 status 是发现不了变量已经没了的。
        警告必须出现在输出**最前面**，模型是从上往下读的。
        """
        self.host.run("precious = 1", restart=True)
        process = self.host.process
        assert process is not None
        os.kill(process.proc.pid, signal.SIGKILL)
        process.proc.wait(timeout=5)
        result = self.host.run("1 + 1")
        self.assertTrue(result.restarted)
        self.assertTrue(result.output.startswith(get_prompt("repl_state_lost")))
        self.assertIn("2", result.output)
        self.assertEqual(self.host.run("'precious' in dir()").output, "False")

    def test_self_exit_is_announced(self) -> None:
        """`os._exit` 绕过所有 except，驱动接不住，只能靠父进程发现。"""
        self.host.run("keeper = 1", restart=True)
        died = self.host.run("import os\nos._exit(3)")
        self.assertEqual(died.status, "dead")
        self.assertIn(get_prompt("repl_state_lost"), died.output)

    def test_timeout_interrupts_but_keeps_the_namespace(self) -> None:
        """超时先 SIGINT 不直接 kill：命名空间是这个工具的全部价值。"""
        self.host.run("import time\nbefore_sleep = 'intact'", restart=True)
        result = self.host.run("time.sleep(30)", timeout_sec=1)
        self.assertEqual(result.status, "timeout")
        self.assertIn(get_prompt("repl_timeout_kept", timeout_sec="1"), result.output)
        self.assertIn("KeyboardInterrupt", result.output)
        survivor = self.host.run("before_sleep")
        self.assertTrue(survivor.ok)
        self.assertEqual(survivor.output, "'intact'")

    def test_partial_output_before_the_timeout_still_comes_back(self) -> None:
        """卡住之前打出来的那几行是唯一的诊断线索，不能因为没读到哨兵就丢掉。"""
        result = self.host.run(
            "import time\nprint('reached step one')\ntime.sleep(30)",
            timeout_sec=1,
        )
        self.assertEqual(result.status, "timeout")
        self.assertIn("reached step one", result.output)

    def test_uninterruptible_cell_is_killed_and_reported_as_lost(self) -> None:
        """连 SIGINT 都不理时才升级到 kill，并且必须说变量没了。"""
        self.host.run("shortlived = 1", restart=True)
        result = self.host.run(
            "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(60)",
            timeout_sec=1,
        )
        self.assertEqual(result.status, "dead")
        self.assertIn(get_prompt("repl_timeout_killed", timeout_sec="1"), result.output)
        self.assertIsNone(self.host.process)

    def test_status_tells_down_from_up(self) -> None:
        self.assertEqual(self.host.status(), get_prompt("repl_status_down"))
        self.host.run("1")
        self.assertEqual(self.host.status(), get_prompt("repl_status_up", cells="1"))
        self.host.close()
        self.assertEqual(self.host.status(), get_prompt("repl_status_down"))


class ReplIsolationTests(unittest.TestCase):
    def test_two_hosts_do_not_share_variables(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = ReplHost(workspace=one)
            second = ReplHost(workspace=two)
            try:
                first.run("mine = 'first'")
                self.assertEqual(second.run("'mine' in dir()").output, "False")
                self.assertEqual(first.run("mine").output, "'first'")
            finally:
                first.close()
                second.close()

    def test_close_reaps_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            host = ReplHost(workspace=work)
            host.run("1")
            process = host.process
            assert process is not None
            host.close()
            self.assertIsNone(host.process)
            self.assertFalse(process.alive)


class ReplClipTests(unittest.TestCase):
    def test_empty_becomes_no_output(self) -> None:
        self.assertEqual(_clip("", 100), get_prompt("repl_no_output"))
        self.assertEqual(_clip("\n\n", 100), get_prompt("repl_no_output"))

    def test_under_the_cap_is_untouched(self) -> None:
        self.assertEqual(_clip("short", 100), "short")

    def test_over_the_cap_keeps_the_tail(self) -> None:
        clipped = _clip("A" * 50 + "TAIL", 10)
        self.assertTrue(clipped.endswith("A" * 6 + "TAIL"))
        self.assertIn("54", clipped)

    def test_zero_cap_means_unlimited(self) -> None:
        body = "B" * 5000
        self.assertEqual(_clip(body, 0), body)


class ReplSettingsTests(unittest.TestCase):
    def test_shipped_runtime_declares_repl(self) -> None:
        from witty_agent.paths import project_root

        text = (project_root() / "config" / "runtime.toml").read_text(encoding="utf-8")
        self.assertIn("[repl]", text)
        self.assertIn("timeout_sec", text.split("[repl]", 1)[1])

    def test_missing_table_still_yields_working_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.toml"
            path.write_text("[logging]\nlevel = \"INFO\"\n", encoding="utf-8")
            previous = os.environ.get("WITTY_RUNTIME_FILE")
            os.environ["WITTY_RUNTIME_FILE"] = str(path)
            clear_runtime_cache()
            try:
                settings = repl_settings()
                self.assertTrue(settings["enabled"])
                self.assertGreaterEqual(int(settings["timeout_sec"]), 1)
                self.assertGreater(int(settings["max_output_chars"]), 0)
            finally:
                if previous is None:
                    os.environ.pop("WITTY_RUNTIME_FILE", None)
                else:
                    os.environ["WITTY_RUNTIME_FILE"] = previous
                clear_runtime_cache()

    def test_nonsense_values_are_floored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.toml"
            path.write_text("[repl]\ntimeout_sec = 0\nmax_output_chars = -5\n", encoding="utf-8")
            previous = os.environ.get("WITTY_RUNTIME_FILE")
            os.environ["WITTY_RUNTIME_FILE"] = str(path)
            clear_runtime_cache()
            try:
                settings = repl_settings()
                self.assertEqual(int(settings["timeout_sec"]), 60)
                self.assertEqual(int(settings["max_output_chars"]), 16384)
            finally:
                if previous is None:
                    os.environ.pop("WITTY_RUNTIME_FILE", None)
                else:
                    os.environ["WITTY_RUNTIME_FILE"] = previous
                clear_runtime_cache()


class ReplSurfaceTests(unittest.TestCase):
    """执行类工具的权限位置，错一个就等于默认放行。"""

    def test_running_code_needs_approval_reading_status_does_not(self) -> None:
        from witty_agent.approval import is_dangerous

        self.assertTrue(is_dangerous("python_repl"))
        self.assertFalse(is_dangerous("python_repl_status"))

    def test_both_are_kernel_tools(self) -> None:
        from witty_agent.kernel_surface import is_kernel_tool

        self.assertTrue(is_kernel_tool("python_repl"))
        self.assertTrue(is_kernel_tool("python_repl_status"))

    def test_plan_mode_blocks_running_code(self) -> None:
        from witty_agent.plan_mode import MUTATING_TOOLS

        self.assertIn("python_repl", MUTATING_TOOLS)
        self.assertNotIn("python_repl_status", MUTATING_TOOLS)

    def test_repeat_guard_treats_a_cell_as_state_changing(self) -> None:
        from witty_agent.guard import changes_state

        self.assertTrue(changes_state("python_repl"))
        self.assertFalse(changes_state("python_repl_status"))

    def test_not_advertised_on_an_unrelated_turn(self) -> None:
        """长驻解释器不进 CORE_TOOLS：说「你好」的那一轮不该看见它。"""
        from witty_agent.tool_surface import select_advertised_names

        names = ["read", "bash", "python_repl", "python_repl_status"]
        self.assertNotIn("python_repl", select_advertised_names("你好", names))

    def test_advertised_when_the_turn_is_about_data(self) -> None:
        from witty_agent.tool_surface import select_advertised_names

        names = ["read", "bash", "python_repl", "python_repl_status"]
        advertised = select_advertised_names("把这个 csv 里的数据统计一下", names)
        self.assertIn("python_repl", advertised)

    def test_advertised_tools_carry_a_one_liner(self) -> None:
        self.assertTrue(get_prompt("tool_snippet_python_repl").strip())


class ReplToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from witty_agent import hooks

        self._work = tempfile.TemporaryDirectory()
        self._prev_workspace = os.environ.get("WITTY_WORKSPACE")
        os.environ["WITTY_WORKSPACE"] = self._work.name
        hooks.repl_host = None

    def tearDown(self) -> None:
        from witty_agent import hooks

        if hooks.repl_host is not None:
            hooks.repl_host.close()
            hooks.repl_host = None
        if self._prev_workspace is None:
            os.environ.pop("WITTY_WORKSPACE", None)
        else:
            os.environ["WITTY_WORKSPACE"] = self._prev_workspace
        self._work.cleanup()

    def test_empty_code_is_rejected_before_spawning(self) -> None:
        from witty_agent import hooks
        from witty_agent.tools.repl import python_repl

        with self.assertRaises(ValueError):
            python_repl("   ")
        self.assertIsNone(hooks.repl_host)

    def test_timeout_out_of_range_is_rejected(self) -> None:
        from witty_agent.tools.repl import python_repl

        with self.assertRaises(ValueError):
            python_repl("1", timeout=-1)
        with self.assertRaises(ValueError):
            python_repl("1", timeout=3601)

    def test_result_carries_the_status(self) -> None:
        from witty_agent.tools.repl import python_repl

        self.assertTrue(python_repl("value = 3\nvalue").startswith("status=ok"))
        self.assertIn("status=error", python_repl("1/0"))

    def test_one_host_is_reused_across_calls(self) -> None:
        from witty_agent import hooks
        from witty_agent.tools.repl import python_repl

        python_repl("shared = 'yes'")
        host = hooks.repl_host
        self.assertIsNotNone(host)
        self.assertIn("'yes'", python_repl("shared"))
        self.assertIs(hooks.repl_host, host)

    def test_changing_workspace_swaps_the_interpreter(self) -> None:
        """cwd 和相对路径的含义跟着工作区走，沿用旧进程会让路径悄悄指错地方。"""
        from witty_agent import hooks
        from witty_agent.tools.repl import python_repl

        python_repl("marker = 'first workspace'")
        first = hooks.repl_host
        with tempfile.TemporaryDirectory() as other:
            os.environ["WITTY_WORKSPACE"] = other
            self.assertIn("False", python_repl("'marker' in dir()"))
            self.assertIsNot(hooks.repl_host, first)
        assert first is not None
        self.assertFalse(first.process.alive if first.process else False)

    def test_interpreter_outlives_a_turn_boundary(self) -> None:
        """长任务的核心承诺：用户说完一句、模型答完一轮，变量还在。

        每轮 `session.run` 都会重新 `hooks.bind`。如果 bind 顺手把解释器清了，那这个工具
        对长任务就毫无意义——只剩一次调用之内有效，跟 bash 没差别。
        """
        from witty_agent import hooks
        from witty_agent.tools.repl import python_repl

        python_repl("across_turns = 'survived'")
        host = hooks.repl_host
        hooks.bind(
            stream_fn=None,  # type: ignore[arg-type]
            approve=None,
            project_id="p",
            workspace=self._work.name,
            root=None,
        )
        self.assertIs(hooks.repl_host, host)
        self.assertIn("'survived'", python_repl("across_turns"))

    def test_hooks_reset_reaps_the_interpreter(self) -> None:
        """会话收尾不收进程就是漏进程。"""
        from witty_agent import hooks
        from witty_agent.tools.repl import python_repl

        python_repl("1")
        process = hooks.repl_host.process
        assert process is not None
        hooks.reset()
        self.assertIsNone(hooks.repl_host)
        self.assertFalse(process.alive)

    def test_status_tool_reports_without_starting_one(self) -> None:
        from witty_agent.tools.repl import python_repl_status

        self.assertEqual(python_repl_status(), get_prompt("repl_status_down"))


if __name__ == "__main__":
    unittest.main()
