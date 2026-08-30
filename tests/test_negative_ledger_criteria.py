"""证伪账本的判据：认失败按**报错文案的 prompt key**，认路径按工具自己的解析器。

改前两处都在代码里自己猜：
  判据 —— 一张关键词表 `不存在|不是普通文件|不是目录|not found|file access denied`。而文案
          本体在 `config/prompts.toml`，`read_not_found` 说的是「找不到 {path}」，表里没有
          「找不到」，于是**最常见的那种失败从来没入过账**。反过来 `file access denied` 在表
          里，沙箱越界拒绝被当成「路径不在」入了账——那是策略拒绝，证据永远不会变。
  路径 —— 自己拼 `workspace / path`，而工具走 `resolve_allowed`，沙箱开着时 `sandbox/…`
          会被映射到工作区**外面**。账本盯着幽灵路径，文件真出现了它也看不见，于是**挡住
          一个已经能跑的调用**。

真工具真文案实测（`/tmp/probe_ledger.py`，判据与路径两处**单独归因**过）：
    改前 调参集漏挡 2/5、留出集漏挡 3/6 误挡 1/6，沙箱往返建好后仍被挡
    改后 两集 0 漏 0 误，沙箱往返放行
判据那一处修好漏挡与越界误挡；路径那一处只修沙箱往返——两者互不覆盖，但必须同一刀落：
判据一修好，`read` 也开始入账，沙箱误挡的面就从 `ls` 扩到了最常用的那个工具。

放宽方向才是危险方向（放宽 = 挡住本来能跑的调用），所以每条否决都单独压住：
名单外的失败一律不入账、退化模板不许生效、占位符不许跨行连、非 is_error 不入账。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.negative_ledger import (
    _MIN_LITERAL,
    _PATH_TOOLS,
    _missing_patterns,
    _template_pattern,
    attempt_key,
    current_evidence,
    gate_attempt,
    record_failure,
)
from witty_agent.prompts import get_prompt, load_prompts
from witty_agent.runtime import ledger_settings
from witty_agent.types import AgentMessage, ToolCallBlock

# 名单里**不能**有的：这些失败重试是对的，挡住就是把路堵死。
MUST_STAY_RETRYABLE = (
    "read_not_text",
    "read_offset_oob",
    "edit_not_found",
    "edit_not_unique",
    "edit_span_too_wide",
    "edit_is_symlink",
    "edit_no_change",
    "fs_not_observed_edit",
    "fs_not_observed_write",
    "fs_stale_version",
    "sandbox_denied_outside",
    "sandbox_denied_venv",
    "denied_tool",
)


def _call(name: str, **arguments: object) -> ToolCallBlock:
    return ToolCallBlock(id="c1", name=name, arguments=arguments)


def _error(text: str, tool: str = "read") -> AgentMessage:
    """按 `loop._error_result` 的形状造回执：content 就是 `str(exc)`。"""
    return AgentMessage(role="toolResult", content=text, is_error=True, tool_name=tool)


class TemplatePatternTest(unittest.TestCase):
    """模板 → 正则。这一层决定了「改措辞自动跟着走」成不成立。"""

    def test_placeholder_matches_any_value(self) -> None:
        pattern = _template_pattern("错误：找不到 {path}。")
        self.assertTrue(pattern.search("错误：找不到 a/b/c.txt。"))
        self.assertTrue(pattern.search("错误：找不到 带空格 的名字.txt。"))

    def test_literal_parts_must_match(self) -> None:
        pattern = _template_pattern("错误：找不到 {path}。")
        self.assertIsNone(pattern.search("错误：读不了 a.txt。"))

    def test_regex_metacharacters_in_the_template_are_literal(self) -> None:
        """文案里真有括号和点（`(empty directory)`），不转义会让模板变成另一个意思。"""
        pattern = _template_pattern("错误：{path} 不是 a.b（普通文件）。")
        self.assertTrue(pattern.search("错误：x 不是 a.b（普通文件）。"))
        self.assertIsNone(pattern.search("错误：x 不是 aXb（普通文件）。"))

    def test_placeholder_cannot_span_lines(self) -> None:
        """用 `.*` 的话，两段字面隔着占位符能跨行连起来算命中。"""
        pattern = _template_pattern("错误：{path} 不是普通文件。")
        self.assertIsNone(pattern.search("错误：开头\n中间\n结尾 不是普通文件。"))

    def test_degenerate_template_is_rejected(self) -> None:
        """只剩占位符的模板能匹配任何东西，宁可不生效也不能全挡。"""
        self.assertIsNone(_template_pattern("{path}"))
        self.assertIsNone(_template_pattern("{path}{other}"))

    def test_whitespace_does_not_count_toward_the_literal_floor(self) -> None:
        """`  {path} 。` 只有 2 个实字，不去空白就够 6 个「长度」，于是退化模板混过闸门。"""
        self.assertIsNone(_template_pattern("  {path}    。"))
        self.assertIsNone(_template_pattern("{path}\n\n\n\n\n\n"))

    def test_a_degenerate_template_is_skipped_not_used(self) -> None:
        """判据里混进一条能匹配任何东西的模板 = 所有失败都被当成路径不在，全面误挡。"""
        from unittest.mock import patch

        table = dict(load_prompts())
        table["read_not_found"] = "{path}"
        with patch("witty_agent.prompts.load_prompts", return_value=table):
            patterns = _missing_patterns()
            self.assertFalse(
                any(p.search(get_prompt("sandbox_denied_outside", path="../x")) for p in patterns),
                "退化模板生效了，任何回执都会被认成路径不在",
            )
            self.assertFalse(any(p.search("随便一句话") for p in patterns))

    def test_every_configured_template_clears_the_literal_floor(self) -> None:
        table = load_prompts()
        for name in ledger_settings()["missing_prompts"]:
            with self.subTest(key=name):
                template = table.get(name, "")
                self.assertTrue(template, f"{name} 不在 prompts.toml 里")
                self.assertIsNotNone(
                    _template_pattern(template),
                    f"{name} 去掉占位符后不足 {_MIN_LITERAL} 字，会匹配上任何东西",
                )


class ConfiguredListTest(unittest.TestCase):
    """名单本身。这是这一轮的判据，得有测试盯着别被改宽。"""

    def test_listed_keys_all_exist(self) -> None:
        table = load_prompts()
        for name in ledger_settings()["missing_prompts"]:
            with self.subTest(key=name):
                self.assertIn(name, table, f"{name} 在名单里但 prompts.toml 里没有")

    def test_the_four_known_missing_shapes_are_listed(self) -> None:
        listed = set(ledger_settings()["missing_prompts"])
        self.assertEqual(
            listed,
            {"read_not_found", "read_not_file", "ls_not_dir", "fs_not_found_edit"},
        )

    def test_the_config_file_carries_the_list_itself(self) -> None:
        """名单被整段删掉时 `ledger_settings()` 会退回代码里的默认值，读出来一模一样。

        于是「配置说了算」这件事没有断言能证明——删掉 `[ledger]` 段所有测试照样绿。这里直接
        读 TOML：判据必须写在配置里，代码里那份只是配置缺失时的兜底。
        """
        from witty_agent.runtime import load_runtime

        table = load_runtime().get("ledger")
        self.assertIsInstance(table, dict, "config/runtime.toml 缺 [ledger] 段")
        self.assertEqual(
            table.get("missing_prompts"),
            ["read_not_found", "read_not_file", "ls_not_dir", "fs_not_found_edit"],
        )

    def test_the_code_default_matches_the_config(self) -> None:
        """兜底名单和配置里那份必须一致，否则配置一缺账本就换了套判据。"""
        from unittest.mock import patch

        from witty_agent.runtime import ledger_settings as reader

        with patch("witty_agent.runtime.load_runtime", return_value={}):
            self.assertEqual(
                reader()["missing_prompts"],
                ["read_not_found", "read_not_file", "ls_not_dir", "fs_not_found_edit"],
            )

    def test_retryable_failures_are_not_recognised(self) -> None:
        """名单之外的失败一个都不能被认成「路径不在」——这是放宽方向的闸门。"""
        patterns = _missing_patterns()
        for name in MUST_STAY_RETRYABLE:
            text = get_prompt(name, path="p.txt", offset="9", total="3", count="2", max="80", tool_name="write")
            with self.subTest(key=name):
                self.assertFalse(
                    any(pattern.search(text) for pattern in patterns),
                    f"{name} 被认成路径不在了：{text[:60]}",
                )

    def test_a_missing_key_in_the_list_does_not_crash(self) -> None:
        """名单写错字不该把工具回执的路弄崩，只该少一条判据。

        打在 `witty_agent.runtime` 上而不是 `negative_ledger` 上：`_missing_patterns` 是在
        函数体里 import 的（为了绕开 runtime↔prompts 的环），名字并不挂在账本模块上。
        """
        from unittest.mock import patch

        with patch("witty_agent.runtime.ledger_settings", return_value={"missing_prompts": ["no_such_key"]}):
            self.assertEqual(_missing_patterns(), [])


class ResolvedTargetTest(unittest.TestCase):
    """证据盯的必须是工具真去看的那个文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self._old = os.environ.get("WITTY_WORKSPACE")
        os.environ["WITTY_WORKSPACE"] = str(self.workspace)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("WITTY_WORKSPACE", None)
        else:
            os.environ["WITTY_WORKSPACE"] = self._old
        self._tmp.cleanup()

    def test_plain_path_stays_under_the_workspace(self) -> None:
        evidence = current_evidence("read", {"path": "notes.txt"}, workspace=self.workspace)
        self.assertEqual(Path(evidence["target"]).parent.resolve(), self.workspace.resolve())

    def test_sandbox_path_follows_the_tool_resolver(self) -> None:
        """沙箱开着时 `sandbox/…` 在工作区外面。自己拼会拼出个幽灵路径。"""
        from witty_agent.runtime import sandbox_settings
        from witty_agent.sandbox import resolve_allowed

        if not sandbox_settings()["enabled"]:
            self.skipTest("沙箱未启用")
        evidence = current_evidence("read", {"path": "sandbox/x.py"}, workspace=self.workspace)
        self.assertEqual(
            Path(evidence["target"]), resolve_allowed(str(self.workspace), "sandbox/x.py")
        )
        naive = (self.workspace / "sandbox/x.py").resolve()
        self.assertNotEqual(Path(evidence["target"]), naive)

    def test_two_spellings_of_one_file_share_a_key(self) -> None:
        absolute = str((self.workspace / "notes.txt").resolve())
        self.assertEqual(
            attempt_key("read", {"path": "notes.txt"}, workspace=self.workspace),
            attempt_key("read", {"path": absolute}, workspace=self.workspace),
        )

    def test_different_files_do_not_share_a_key(self) -> None:
        self.assertNotEqual(
            attempt_key("read", {"path": "a.txt"}, workspace=self.workspace),
            attempt_key("read", {"path": "b.txt"}, workspace=self.workspace),
        )

    def test_out_of_workspace_path_has_defined_evidence(self) -> None:
        """`resolve_allowed` 会抛越界，账本不能跟着抛。"""
        evidence = current_evidence("read", {"path": "../../etc/hosts"}, workspace=self.workspace)
        self.assertEqual(evidence["kind"], "path")
        self.assertIn("target", evidence)

    def test_key_without_a_workspace_still_works(self) -> None:
        """兼容没有工作区可谈的调用点：退回原始字符串，不抛。"""
        self.assertTrue(attempt_key("read", {"path": "notes.txt"}).startswith("read:"))


class RecordAndGateTest(unittest.TestCase):
    """入账与查账，用真文案。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.memory = self.workspace / "mem"
        self.memory.mkdir()
        self._old = os.environ.get("WITTY_WORKSPACE")
        os.environ["WITTY_WORKSPACE"] = str(self.workspace)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("WITTY_WORKSPACE", None)
        else:
            os.environ["WITTY_WORKSPACE"] = self._old
        self._tmp.cleanup()

    def ledger(self, tool: str, args: dict, text: str, *, is_error: bool = True) -> bool:
        call = _call(tool, **args)
        record_failure(
            self.memory,
            call,
            AgentMessage(role="toolResult", content=text, is_error=is_error, tool_name=tool),
            workspace=self.workspace,
        )
        return gate_attempt(self.memory, call, workspace=self.workspace) is not None

    def test_read_not_found_is_ledgered(self) -> None:
        """改前这一条没入过账——而它是最常见的那种失败。"""
        self.assertTrue(self.ledger("read", {"path": "gone.txt"}, get_prompt("read_not_found", path="gone.txt")))

    def test_ls_not_dir_is_ledgered(self) -> None:
        self.assertTrue(self.ledger("ls", {"path": "nodir"}, get_prompt("ls_not_dir", path="nodir")))

    def test_edit_confirmed_absent_is_ledgered(self) -> None:
        self.assertTrue(
            self.ledger(
                "edit",
                {"path": "gone.txt", "old_text": "a", "new_text": "b"},
                get_prompt("fs_not_found_edit", path="gone.txt"),
            )
        )

    def test_sandbox_denial_is_not_ledgered(self) -> None:
        """策略拒绝不是证伪：证据永远不会变，挡住就永久堵死。旧关键词表把它收了。"""
        self.assertFalse(
            self.ledger("read", {"path": "x"}, get_prompt("sandbox_denied_outside", path="../x"))
        )

    def test_read_first_advice_is_not_ledgered(self) -> None:
        """「先 read」的正确动作是 read 完再来，不是别再来。"""
        self.assertFalse(
            self.ledger("write", {"path": "notes.txt"}, get_prompt("fs_not_observed_write", path="notes.txt"))
        )

    def test_non_error_result_is_not_ledgered(self) -> None:
        """回执里恰好抄了一句报错文案（比如 bash 输出被转述）不该算失败。"""
        self.assertFalse(
            self.ledger(
                "read", {"path": "notes.txt"}, get_prompt("read_not_found", path="notes.txt"), is_error=False
            )
        )

    def test_plugin_sourced_result_is_not_ledgered(self) -> None:
        """账本自己吐的阻断回执不能再入一次账，否则会滚雪球。"""
        call = _call("read", path="gone.txt")
        record_failure(
            self.memory,
            call,
            AgentMessage(
                role="toolResult",
                content=get_prompt("read_not_found", path="gone.txt"),
                is_error=True,
                tool_name="read",
                source="plugin:negative-ledger",
            ),
            workspace=self.workspace,
        )
        self.assertIsNone(gate_attempt(self.memory, call, workspace=self.workspace))

    def test_non_path_tools_are_untouched(self) -> None:
        for tool in ("bash", "grep", "find"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, _PATH_TOOLS)
                self.assertFalse(
                    self.ledger(tool, {"path": "gone.txt"}, get_prompt("read_not_found", path="gone.txt"))
                )

    def test_block_lifts_once_the_file_appears(self) -> None:
        call = _call("read", path="later.txt")
        record_failure(
            self.memory,
            call,
            _error(get_prompt("read_not_found", path="later.txt")),
            workspace=self.workspace,
        )
        self.assertIsNotNone(gate_attempt(self.memory, call, workspace=self.workspace))
        (self.workspace / "later.txt").write_text("now", encoding="utf-8")
        self.assertIsNone(gate_attempt(self.memory, call, workspace=self.workspace))

    def test_block_lifts_for_a_file_whose_mtime_is_epoch(self) -> None:
        """`exists` 看着被 `mtime` 盖住了（不在→mtime 0，出现→mtime 非 0），只有这一种情况不是：
        文件的 mtime 正好是 epoch 0。归档解包、`os.utime` 归零都能造出来。少了 `exists`
        这一条，这种文件建起来了账本仍然挡着。
        """
        import os as _os

        call = _call("read", path="epoch.txt")
        record_failure(
            self.memory,
            call,
            _error(get_prompt("read_not_found", path="epoch.txt")),
            workspace=self.workspace,
        )
        self.assertIsNotNone(gate_attempt(self.memory, call, workspace=self.workspace))
        target = self.workspace / "epoch.txt"
        target.write_text("now", encoding="utf-8")
        _os.utime(target, (0, 0))
        self.assertIsNone(gate_attempt(self.memory, call, workspace=self.workspace))

    def test_block_lifts_for_a_sandbox_path_too(self) -> None:
        """这一条是路径解析那一处的归因用例：改前建好了仍被挡。"""
        from witty_agent.runtime import sandbox_settings
        from witty_agent.sandbox import resolve_allowed

        if not sandbox_settings()["enabled"]:
            self.skipTest("沙箱未启用")
        call = _call("ls", path="sandbox/newdir")
        record_failure(
            self.memory,
            call,
            _error(get_prompt("ls_not_dir", path="sandbox/newdir"), tool="ls"),
            workspace=self.workspace,
        )
        self.assertIsNotNone(gate_attempt(self.memory, call, workspace=self.workspace))
        real = resolve_allowed(str(self.workspace), "sandbox/newdir")
        real.mkdir(parents=True, exist_ok=True)
        try:
            self.assertIsNone(gate_attempt(self.memory, call, workspace=self.workspace))
        finally:
            real.rmdir()

    def test_reworded_prompt_is_followed_without_touching_code(self) -> None:
        """这就是这一刀的要点：措辞改了，判据自动跟着走。

        同样打 `witty_agent.prompts.load_prompts`——账本是在函数体里 import 的。
        """
        from unittest.mock import patch

        reworded = dict(load_prompts())
        reworded["read_not_found"] = "错误：这个路径在工作区里查无此物：{path}。"
        with patch("witty_agent.prompts.load_prompts", return_value=reworded):
            patterns = _missing_patterns()
            self.assertTrue(
                any(p.search("错误：这个路径在工作区里查无此物：gone.txt。") for p in patterns)
            )
            self.assertFalse(any(p.search("错误：找不到 gone.txt。") for p in patterns))


class RealToolShapeTest(unittest.TestCase):
    """跑真工具，按 `loop` 的方式把异常变成回执，端到端确认判据接得上。

    合成文案能证明判据本身对，但证不了「工具真会抛这一条」。这一层专门盯那个接缝。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.memory = self.workspace / "mem"
        self.memory.mkdir()
        (self.workspace / "notes.txt").write_text("hello\nworld\n", encoding="utf-8")
        (self.workspace / "blob.bin").write_bytes(b"\x00\x01\x02")
        self._old = os.environ.get("WITTY_WORKSPACE")
        os.environ["WITTY_WORKSPACE"] = str(self.workspace)
        from witty_agent.fs_observe import clear_observations

        clear_observations()

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("WITTY_WORKSPACE", None)
        else:
            os.environ["WITTY_WORKSPACE"] = self._old
        self._tmp.cleanup()

    def blocked(self, tool: str, **args: object) -> bool:
        from witty_agent.tools.fs import edit, read, write
        from witty_agent.tools.search import ls

        fn = {"read": read, "write": write, "edit": edit, "ls": ls}[tool]
        call = _call(tool, **args)
        try:
            fn(**args)  # type: ignore[arg-type]
        except Exception as exc:  # loop.py 就是这么兜的
            record_failure(self.memory, call, _error(str(exc), tool=tool), workspace=self.workspace)
        return gate_attempt(self.memory, call, workspace=self.workspace) is not None

    def test_real_read_miss_is_blocked(self) -> None:
        self.assertTrue(self.blocked("read", path="gone.txt"))

    def test_real_ls_miss_is_blocked(self) -> None:
        self.assertTrue(self.blocked("ls", path="nodir"))

    def test_real_ls_on_a_file_is_blocked(self) -> None:
        self.assertTrue(self.blocked("ls", path="notes.txt"))

    def test_real_binary_read_is_not_blocked(self) -> None:
        self.assertFalse(self.blocked("read", path="blob.bin"))

    def test_real_offset_overrun_is_not_blocked(self) -> None:
        self.assertFalse(self.blocked("read", path="notes.txt", offset=999))

    def test_real_out_of_workspace_read_is_not_blocked(self) -> None:
        self.assertFalse(self.blocked("read", path="../../etc/hosts"))

    def test_real_unobserved_write_is_not_blocked(self) -> None:
        self.assertFalse(self.blocked("write", path="notes.txt", content="x"))

    def test_ls_error_comes_from_config_not_code(self) -> None:
        """`ls` 的报错以前是代码里的 f-string，账本按 key 认判据就必须搬进配置。

        只比对 `get_prompt` 的输出是不够的——代码里写一句一模一样的 f-string 也能通过
        （变异测试确认过：那个变异体逃逸了）。这里换一份真的配置文件再看工具跟不跟：
        走 `WITTY_PROMPTS_FILE`，因为 `get_prompt` 读的是 `_load_table(prompts_file())`，
        打 `load_prompts` 打不到它。
        """
        from witty_agent.prompts import _render_prompts_toml, clear_prompt_cache
        from witty_agent.tools.search import ls

        with self.assertRaises(ValueError) as caught:
            ls(path="nodir")
        self.assertEqual(str(caught.exception), get_prompt("ls_not_dir", path="nodir"))

        table = dict(load_prompts())
        table["ls_not_dir"] = "错误：{path} 这个位置不是一个目录。"
        override = self.workspace / "prompts_override.toml"
        override.write_text(_render_prompts_toml(table), encoding="utf-8")
        os.environ["WITTY_PROMPTS_FILE"] = str(override)
        clear_prompt_cache()
        try:
            with self.assertRaises(ValueError) as caught:
                ls(path="nodir")
            self.assertIn("这个位置不是一个目录", str(caught.exception))
        finally:
            os.environ.pop("WITTY_PROMPTS_FILE", None)
            clear_prompt_cache()


if __name__ == "__main__":
    unittest.main()
