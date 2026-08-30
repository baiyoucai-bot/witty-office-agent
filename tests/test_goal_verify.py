"""目标模式的判据：谁说了算，以及哪条路径能把「没验过」误判成「做完了」。

改前 `run_goal_loop` 的终止条件只有一条：模型自己往 GOAL.yaml 里写 `status: complete`。
申报即判据，于是最贵的那类失败（跑一百轮然后自称完成）零成本发生。改后判据是三样：
客观 gate 的退出码、回归义务台账、一个不带工具的判官。

放宽方向才是危险方向——「放宽」在这里指把没有权威证据的目标收成 complete。所以每条
否决单独压住：判官读不懂要按没完成算、gate 红着不许完成、过了的判据被弄坏要算回归、
瞬时错误不许清目标。收紧方向（多跑一轮、多跑一次 gate）只花钱，不单独立测。
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from witty_agent.goal import (
    GoalVerdict,
    classify_failure,
    parse_verdict,
    read_goal_status,
    render_transcript,
    run_goal_loop,
)
from witty_agent.types import AgentMessage, TextBlock, ToolCallBlock
from witty_agent.verify import (
    GateResult,
    GateRunner,
    GateSpec,
    Obligation,
    ObligationLedger,
    merge_specs,
    worktree_fingerprint,
)

PASS = "exit 0"
FAIL = "exit 1"


def assistant(text: str = "", *, calls: tuple[str, ...] = ()) -> AgentMessage:
    content: list = [TextBlock(text=text)] if text else []
    content += [ToolCallBlock(id=f"c{i}", name=name, arguments={}) for i, name in enumerate(calls)]
    return AgentMessage(role="assistant", content=content or "")


def run(coro):
    return asyncio.run(coro)


class VerdictParseTests(unittest.TestCase):
    """判官的输出是自由文本。读不懂的每一种形状都必须落到「没完成」。"""

    def test_strict_json(self) -> None:
        verdict = parse_verdict('{"score": 0.9, "complete": true, "impossible": false, "missing": ""}')
        self.assertTrue(verdict.parsed)
        self.assertTrue(verdict.complete)
        self.assertAlmostEqual(verdict.score, 0.9)

    def test_json_embedded_in_prose(self) -> None:
        """模型爱加围栏和前言。裁定还在里面，就该读出来，而不是白跑一轮。"""
        raw = 'Here is my ruling:\n```json\n{"score": 0.2, "complete": false, "missing": "no tests ran"}\n```\n'
        verdict = parse_verdict(raw)
        self.assertTrue(verdict.parsed)
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.missing, "no tests ran")

    def test_unreadable_output_is_not_complete(self) -> None:
        for raw in ("", "   ", "looks done to me", "{not json", '"a string"', "[1, 2]"):
            with self.subTest(raw=raw):
                verdict = parse_verdict(raw)
                self.assertFalse(verdict.parsed)
                self.assertFalse(verdict.complete)
                self.assertFalse(verdict.impossible)
                self.assertEqual(verdict.score, 0.0)
                self.assertTrue(verdict.missing, "读不懂也要给下一轮留话，不能空着")

    def test_score_is_clamped_and_survives_garbage(self) -> None:
        self.assertEqual(parse_verdict('{"score": 9}').score, 1.0)
        self.assertEqual(parse_verdict('{"score": -3}').score, 0.0)
        self.assertEqual(parse_verdict('{"score": "high"}').score, 0.0)

    def test_complete_needs_the_field_not_just_a_high_score(self) -> None:
        """0.99 分不是完成。判官必须显式说 complete，否则「差一点」会被读成「过了」。"""
        self.assertFalse(parse_verdict('{"score": 0.99}').complete)


class TranscriptTests(unittest.TestCase):
    def test_tool_names_survive(self) -> None:
        """判官区分「跑了测试」和「说自己跑了测试」全靠工具名，裁掉就没判据了。"""
        text = render_transcript([assistant("running", calls=("bash",))], limit=4000)
        self.assertIn("bash", text)

    def test_bounded_and_keeps_the_tail(self) -> None:
        """超预算留新的丢旧的：最新一轮才是这次要判的证据。"""
        messages = [assistant(f"line-{i}" + "x" * 200) for i in range(50)]
        text = render_transcript(messages, limit=1000)
        self.assertLessEqual(len(text), 1200)
        self.assertIn("line-49", text)
        self.assertNotIn("line-0x", text)


class FailureClassTests(unittest.TestCase):
    """限流当致命 = 把眼看要做完的活扔掉。这张表的宽窄两头都要压。"""

    def test_fatal_classes(self) -> None:
        for exc in (
            RuntimeError("401 Unauthorized"),
            RuntimeError("invalid api key"),
            RuntimeError("insufficient_quota: please check billing"),
            RuntimeError("This model's maximum context length is 128000 tokens"),
            RuntimeError("The model `gpt-4-vision-preview` has been decommissioned"),
            RuntimeError("The model `x` does not exist or you do not have access to it"),
            RuntimeError("model_not_found"),
        ):
            with self.subTest(exc=str(exc)):
                self.assertEqual(classify_failure(exc), "fatal")

    def test_transient_classes(self) -> None:
        for exc in (
            RuntimeError("429 rate limit exceeded, retry after 3s"),
            RuntimeError("503 overloaded"),
            TimeoutError("read timeout"),
            ConnectionResetError("connection reset by peer"),
            RuntimeError("Internal server error"),
        ):
            with self.subTest(exc=str(exc)):
                self.assertEqual(classify_failure(exc), "transient")

    def test_ambiguous_availability_wording_stays_transient(self) -> None:
        """"model ... unavailable / overloaded" 是过载的说法，不是模型下线的说法。

        收进致命表的代价是不对称的：判错方向会在一次过载里把整个目标清掉，而判成瞬时
        最多多等一轮。所以这类含糊措辞一律留在瞬时侧。
        """
        for exc in (
            RuntimeError("The model is temporarily unavailable, please retry"),
            RuntimeError("model is currently overloaded"),
        ):
            with self.subTest(exc=str(exc)):
                self.assertEqual(classify_failure(exc), "transient")


class FingerprintTests(unittest.TestCase):
    """指纹只在 gate 能读到的东西动过时才动。内容盲的指纹会跳过一个本来会过的 gate。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)

    def init_repo(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        (self.root / "a.py").write_text("print(1)\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def test_stable_when_nothing_moves(self) -> None:
        self.init_repo()
        self.assertEqual(worktree_fingerprint(self.root), worktree_fingerprint(self.root))

    def test_editing_an_already_modified_file_moves_it(self) -> None:
        """`git status --porcelain` 单看不够：改一个已经是 M 的文件，状态行一模一样。

        这是缓存失败 gate 最容易踩的那条——第二次改动会被当成没改动，于是跳过一个
        现在本来会过的 gate，任务就此卡死在一个已经修好的判据上。
        """
        self.init_repo()
        target = self.root / "a.py"
        target.write_text("print(2)\n", encoding="utf-8")
        once = worktree_fingerprint(self.root)
        target.write_text("print(3)\n", encoding="utf-8")
        self.assertNotEqual(once, worktree_fingerprint(self.root))

    def test_untracked_file_content_counts(self) -> None:
        """新文件还没 add 就是 `?? name` 一行。只看名字的话，写内容进去等于没写。"""
        self.init_repo()
        new = self.root / "b.py"
        new.write_text("", encoding="utf-8")
        once = worktree_fingerprint(self.root)
        new.write_text("print(4)\n", encoding="utf-8")
        self.assertNotEqual(once, worktree_fingerprint(self.root))

    def test_commit_moves_it(self) -> None:
        self.init_repo()
        before = worktree_fingerprint(self.root)
        (self.root / "a.py").write_text("print(5)\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "second")
        self.assertNotEqual(before, worktree_fingerprint(self.root))

    def test_non_git_workspace_still_fingerprints(self) -> None:
        """工作区不是 git 仓库是常态（临时目录、别人给的一包文件），不能退化成常量。"""
        (self.root / "note.txt").write_text("one", encoding="utf-8")
        before = worktree_fingerprint(self.root)
        self.assertTrue(before)
        (self.root / "note.txt").write_text("one plus more bytes", encoding="utf-8")
        self.assertNotEqual(before, worktree_fingerprint(self.root))


class GateRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_exit_code_is_the_verdict(self) -> None:
        report = GateRunner(self.root).run([GateSpec("ok", PASS), GateSpec("bad", FAIL)])
        self.assertFalse(report.ok)
        self.assertEqual([item.name for item in report.failures()], ["bad"])
        self.assertEqual([item.name for item in report.passed()], ["ok"])

    def test_output_reaches_the_model(self) -> None:
        """gate 的红字就是下一轮的指引。丢掉输出，模型只知道「没过」，不知道差什么。"""
        report = GateRunner(self.root).run([GateSpec("bad", "echo needle 1>&2; exit 2")])
        failure = report.failures()[0]
        self.assertEqual(failure.exit_code, 2)
        self.assertIn("needle", failure.output)
        self.assertIn("needle", failure.line())

    def test_failed_gate_is_not_rerun_while_the_tree_stands_still(self) -> None:
        """计数文件故意落在工作区里：真 gate 就是这样的。

        `pytest` 会写覆盖率文件和缓存，构建会写 dist。缓存键要是取「开跑前」的指纹，
        gate 自己的副作用就会让下一轮的指纹对不上，缓存永远命中不了——这个机制等于没做。
        所以键取的是跑完之后的指纹。
        """
        counter = self.root / "runs.txt"
        spec = GateSpec("bad", f"echo x >> {counter}; exit 1")
        runner = GateRunner(self.root)
        runner.run([spec])
        runner.run([spec])
        self.assertEqual(len(counter.read_text(encoding="utf-8").split()), 1)
        self.assertTrue(runner.run([spec]).failures()[0].skipped)

    def test_failed_gate_is_rerun_once_the_tree_moves(self) -> None:
        """缓存只在「什么都没变」时成立。变了还不重跑，就是永远不承认修好了。"""
        spec = GateSpec("bad", FAIL)
        runner = GateRunner(self.root)
        runner.run([spec])
        (self.root / "changed.txt").write_text("moved", encoding="utf-8")
        self.assertFalse(runner.run([spec]).failures()[0].skipped)

    def test_passing_gate_is_always_rerun(self) -> None:
        """过了的 gate 缓存起来就等于关掉回归检查——而回归正是长任务的主要死法。"""
        counter = self.root / "runs.txt"
        spec = GateSpec("ok", f"echo x >> {counter}; exit 0")
        runner = GateRunner(self.root)
        runner.run([spec])
        runner.run([spec])
        self.assertEqual(len(counter.read_text(encoding="utf-8").split()), 2)

    def test_a_gate_that_stops_passing_reports_failure(self) -> None:
        flag = self.root / "flag"
        flag.write_text("ok", encoding="utf-8")
        spec = GateSpec("flag", f"test -f {flag}")
        runner = GateRunner(self.root)
        self.assertTrue(runner.run([spec]).ok)
        flag.unlink()
        self.assertFalse(runner.run([spec]).ok)

    def test_timeout_is_a_failure_not_a_hang(self) -> None:
        report = GateRunner(self.root).run([GateSpec("slow", "sleep 5", timeout_sec=1)])
        self.assertFalse(report.ok)
        self.assertEqual(report.failures()[0].exit_code, -1)

    def test_no_specs_means_no_fingerprint_work(self) -> None:
        self.assertEqual(GateRunner(self.root).run([]).fingerprint, "")


class ObligationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip_and_stamp(self) -> None:
        ledger = ObligationLedger(self.root)
        ledger.record(Obligation(name="tests", command="pytest"))
        loaded = ledger.load()
        self.assertEqual([item.name for item in loaded], ["tests"])
        self.assertTrue(loaded[0].recorded_at, "没有时间戳就答不出「什么时候验过的」")

    def test_later_row_supersedes_earlier(self) -> None:
        """台账只追加。同名后写的赢，但旧行留着，事后还能翻出当时是哪条命令验的。"""
        ledger = ObligationLedger(self.root)
        ledger.record(Obligation(name="tests", command="pytest -x"))
        ledger.record(Obligation(name="tests", command="pytest -q"))
        self.assertEqual([item.command for item in ledger.load()], ["pytest -q"])
        self.assertEqual(len(ledger.path.read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_corrupt_lines_are_skipped(self) -> None:
        ledger = ObligationLedger(self.root)
        ledger.record(Obligation(name="tests", command="pytest"))
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n\n{}\n")
        self.assertEqual([item.name for item in ledger.load()], ["tests"])

    def test_missing_ledger_is_empty_not_an_error(self) -> None:
        self.assertEqual(ObligationLedger(self.root / "nope").load(), [])

    def test_ledger_dir_is_agent_level_and_workspace_keyed(self) -> None:
        """台账落 `agent_state/criteria/<workspace_key>/`，不落会话草稿区。

        两条判据都要成立，而且方向相反：
        * **挂 Agent** —— 验过的判据换个会话仍然成立。落在 `scratchpad/<session_id>/` 的话，
          每开一个新会话回归防护就从零开始，等于只在一次长任务内有效。
        * **按工作区分** —— `pytest -q` 在 A 仓库验过，拿去卡 B 仓库既无意义又会红（那条命令
          在 B 里可能根本不存在），是纯误挡。跟证伪账本同一个键法。
        """
        from witty_agent.layout import criteria_dir, scratchpad_dir
        from witty_agent.memory import workspace_memory_key

        key = workspace_memory_key(self.root)
        here = criteria_dir(key, "default_project", "default_agent", root=self.root)
        pad = scratchpad_dir("sess-1", "default_project", "default_agent", root=self.root)
        self.assertNotIn("scratchpad", here.parts)
        self.assertNotIn(pad.name, here.parts)
        self.assertIn("agent_state", here.parts)
        self.assertEqual(here.name, key)
        other = criteria_dir(
            workspace_memory_key(self.root / "another"), "default_project", "default_agent", root=self.root
        )
        self.assertNotEqual(here, other)

    def test_workspace_key_survives_awkward_directory_names(self) -> None:
        """目录名再怪，key 也得过 layout.assert_id。

        mkdtemp 偶尔生成 `tmpxxxx_` 这种下划线结尾的名字，旧 key 会拼出 `_-` 相邻被
        layout 拒绝——这就是本套件此前那个「低频偶发、疑似串扰」的 ERROR 的真身。
        真实用户目录叫 `my_project_` 同样中招，所以按形状锁死，不锁具体名字。
        """
        from witty_agent.layout import criteria_dir
        from witty_agent.memory import workspace_memory_key

        for name in ("tmpv2sfvyg_", "my_project_", "a__b", "--x--", "_", "项目"):
            key = workspace_memory_key(self.root / name)
            path = criteria_dir(key, "default_project", "default_agent", root=self.root)
            self.assertEqual(path.name, key, name)
        # 已合法的 key 不许换形状：换了等于把存量工作区的记忆和台账丢在旧目录里
        legal = workspace_memory_key(self.root / "my-repo")
        self.assertTrue(legal.startswith("my-repo-"), legal)
        underscore = workspace_memory_key(self.root / "my_repo")
        self.assertTrue(underscore.startswith("my_repo-"), underscore)

    def test_a_second_session_sees_what_the_first_one_proved(self) -> None:
        """跨会话累积就是这条：新开一本台账读到的是上一次验过的判据。"""
        ObligationLedger(self.root).record(Obligation(name="tests", command="pytest -q"))
        self.assertEqual([item.name for item in ObligationLedger(self.root).load()], ["tests"])

    def test_merge_prefers_the_configured_gate(self) -> None:
        """同名不许一轮跑两遍，且当轮的权威命令来自配置，不来自台账里的旧记录。"""
        merged = merge_specs(
            [Obligation(name="tests", command="pytest -x", timeout_sec=10)],
            [GateSpec("tests", "pytest -q", timeout_sec=99), GateSpec("lint", "ruff check")],
        )
        by_name = {spec.name: spec for spec in merged}
        self.assertEqual(len(merged), 2)
        self.assertEqual(by_name["tests"].command, "pytest -q")
        self.assertEqual(by_name["tests"].timeout_sec, 99)


class GoalLoopTests(unittest.TestCase):
    """判据接线：申报、gate、判官、台账在一个循环里谁压谁。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.prompts: list[str] = []

    def runner_factory(self, *, declare: str | None = None, calls: tuple[str, ...] = ("bash",)):
        async def runner(prompt: str) -> list[AgentMessage]:
            self.prompts.append(prompt)
            if declare:
                from witty_agent.goal import goal_path, write_goal

                write_goal(goal_path(self.scratch), "obj", status=declare, round_no=len(self.prompts))
            return [assistant("working", calls=calls)]

        return runner

    def judge_saying(self, *verdicts: GoalVerdict):
        seen: list[str] = []

        async def judge(objective: str, transcript: str) -> GoalVerdict:
            seen.append(transcript)
            return verdicts[min(len(seen) - 1, len(verdicts) - 1)]

        return judge, seen

    def test_self_declared_complete_does_not_survive_the_judge(self) -> None:
        """本次改动的核心。模型写 complete + 判官说没完成 => 必须继续跑，不能收成 complete。"""
        judge, _ = self.judge_saying(GoalVerdict(complete=False, missing="tests never ran"))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="complete"),
                max_rounds=2,
                judge=judge,
            )
        )
        self.assertEqual(state.status, "max_rounds")
        self.assertIn("tests never ran", self.prompts[1])

    def test_judge_completes_the_goal(self) -> None:
        judge, _ = self.judge_saying(GoalVerdict(complete=True, score=1.0))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(),
                max_rounds=5,
                judge=judge,
            )
        )
        self.assertEqual(state.status, "complete")
        self.assertEqual(state.round, 1)
        self.assertEqual(read_goal_status(state.path), "complete")

    def test_unparsed_verdict_keeps_going_instead_of_completing(self) -> None:
        """判官读不懂时多跑一轮只是费钱；当成完成才是把没验过的活交出去。"""
        judge, _ = self.judge_saying(GoalVerdict(parsed=False, missing="unreadable"))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="complete"),
                max_rounds=2,
                judge=judge,
            )
        )
        self.assertEqual(state.status, "max_rounds")

    def test_impossible_stops_early(self) -> None:
        judge, _ = self.judge_saying(GoalVerdict(impossible=True, missing="asks for a file that cannot exist"))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(),
                max_rounds=50,
                judge=judge,
            )
        )
        self.assertEqual(state.status, "impossible")
        self.assertEqual(state.round, 1)
        self.assertIn("cannot exist", state.reason)

    def test_without_a_judge_the_old_declaration_still_works(self) -> None:
        """老调用方不接判官也不接 gate，语义不能被这次改动改掉。"""
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="complete"),
                max_rounds=5,
            )
        )
        self.assertEqual(state.status, "complete")

    def test_red_gate_blocks_completion_and_feeds_the_output_back(self) -> None:
        judge, seen = self.judge_saying(GoalVerdict(complete=True))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="complete"),
                max_rounds=2,
                judge=judge,
                gates=[GateSpec("bad", "echo gate-needle 1>&2; exit 1")],
                workspace=self.work,
            )
        )
        self.assertEqual(state.status, "max_rounds")
        self.assertEqual(seen, [], "gate 红着就不该花钱叫判官")
        self.assertIn("gate-needle", self.prompts[1])

    def test_green_gate_becomes_an_obligation(self) -> None:
        judge, _ = self.judge_saying(GoalVerdict(complete=False, missing="more"), GoalVerdict(complete=True))
        run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(),
                max_rounds=2,
                judge=judge,
                gates=[GateSpec("ok", PASS)],
                workspace=self.work,
            )
        )
        self.assertEqual([item.name for item in ObligationLedger(self.scratch).load()], ["ok"])

    def test_ledger_dir_overrides_the_scratchpad(self) -> None:
        """`ledger_dir` 传了就落那里，草稿区一行不写——这是跨会话累积的落点。"""
        home = self.root / "criteria"
        judge, _ = self.judge_saying(GoalVerdict(complete=True))
        run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(),
                max_rounds=1,
                judge=judge,
                gates=[GateSpec("ok", PASS)],
                workspace=self.work,
                ledger_dir=home,
            )
        )
        self.assertEqual([item.name for item in ObligationLedger(home).load()], ["ok"])
        self.assertFalse(ObligationLedger(self.scratch).path.exists())

    def test_obligations_carry_over_into_a_later_run(self) -> None:
        """上一次运行验过的判据，这一次照样重跑；坏了就是回归。

        这是「跨会话」在循环层面的样子：台账目录不变，`run_goal_loop` 换一次也认账。
        """
        home = self.root / "criteria"
        ObligationLedger(home).record(Obligation(name="tests", command=FAIL))
        judge, _ = self.judge_saying(GoalVerdict(complete=True))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="complete"),
                max_rounds=2,
                judge=judge,
                workspace=self.work,
                ledger_dir=home,
            )
        )
        self.assertEqual(state.status, "max_rounds")
        # 指引回灌的是**下一轮**的提示词，所以看 prompts[1]。
        self.assertIn("goal-regression", self.prompts[1])

    def test_broken_obligation_is_reported_as_a_regression(self) -> None:
        """已经验过的判据被弄坏，指引里要说「回归」，不能和一条新判据没过混为一谈。"""
        ObligationLedger(self.scratch).record(Obligation(name="tests", command=FAIL))
        judge, _ = self.judge_saying(GoalVerdict(complete=True))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="complete"),
                max_rounds=2,
                judge=judge,
                workspace=self.work,
            )
        )
        self.assertEqual(state.status, "max_rounds")
        self.assertIn("goal-regression", self.prompts[1])

    def test_stall_hands_control_back_and_keeps_the_goal(self) -> None:
        """连着几轮只说话不动工具就停下。目标本身留着 active，下一条用户消息能接着跑。"""
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(calls=()),
                max_rounds=20,
                stall_rounds=2,
            )
        )
        self.assertEqual(state.status, "stalled")
        self.assertEqual(state.round, 2)
        self.assertEqual(read_goal_status(state.path), "active")

    def test_tool_use_resets_the_stall_counter(self) -> None:
        """一轮不动工具不算打转——想清楚再动手是正常的。只有连着几轮才算。"""
        script = [(), ("bash",), (), ("bash",), ()]

        async def runner(prompt: str) -> list[AgentMessage]:
            self.prompts.append(prompt)
            return [assistant("thinking", calls=script[len(self.prompts) - 1])]

        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=runner,
                max_rounds=5,
                stall_rounds=2,
            )
        )
        self.assertEqual(state.status, "max_rounds")

    def test_transient_failure_keeps_the_goal_alive(self) -> None:
        attempts: list[int] = []

        async def runner(prompt: str) -> list[AgentMessage]:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("429 rate limit exceeded")
            self.prompts.append(prompt)
            return [assistant("working", calls=("bash",))]

        judge, _ = self.judge_saying(GoalVerdict(complete=True))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=runner,
                max_rounds=4,
                judge=judge,
            )
        )
        self.assertEqual(state.status, "complete")
        self.assertIn("429", self.prompts[0], "重试那轮要告诉模型上一轮是怎么断的")

    def test_fatal_failure_stops_and_records_why(self) -> None:
        async def runner(prompt: str) -> list[AgentMessage]:
            raise RuntimeError("401 Unauthorized: invalid api key")

        state = run(
            run_goal_loop(objective="obj", scratch=self.scratch, runner=runner, max_rounds=9)
        )
        self.assertEqual(state.status, "error")
        self.assertEqual(state.round, 1)
        self.assertIn("401", state.reason)
        self.assertEqual(read_goal_status(state.path), "error")

    def test_blocked_declaration_still_stops(self) -> None:
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(declare="blocked"),
                max_rounds=9,
            )
        )
        self.assertEqual(state.status, "blocked")

    def test_budget_stops_before_max_rounds(self) -> None:
        judge, _ = self.judge_saying(GoalVerdict(complete=False, missing="more"))
        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=self.runner_factory(),
                max_rounds=50,
                budget=2,
                judge=judge,
            )
        )
        self.assertEqual(state.status, "budget")
        self.assertEqual(state.round, 2)

    def test_judge_is_skipped_when_the_round_produced_nothing(self) -> None:
        """没有轨迹就没有证据可评。这时候叫判官是拿空白让它猜。"""
        judge, seen = self.judge_saying(GoalVerdict(complete=True))

        async def runner(prompt: str) -> None:
            self.prompts.append(prompt)
            return None

        state = run(
            run_goal_loop(
                objective="obj",
                scratch=self.scratch,
                runner=runner,
                max_rounds=1,
                judge=judge,
            )
        )
        self.assertEqual(seen, [])
        self.assertEqual(state.status, "max_rounds")


class GateResultLineTests(unittest.TestCase):
    def test_line_carries_name_and_exit_code(self) -> None:
        line = GateResult(name="tests", ok=False, exit_code=2, output="boom").line()
        self.assertIn("tests", line)
        self.assertIn("2", line)
        self.assertIn("boom", line)


if __name__ == "__main__":
    unittest.main()
