from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from witty_agent import hooks
from witty_agent.http_api import STATE, configure_api, handle_request
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.loop_control import apply_loop, loop_schedule_name, parse_loop_args
from witty_agent.schedule import (
    MIN_PERIOD_MS,
    Scheduler,
    ScheduleDefinition,
    delete_schedule,
    next_slot_at,
    parse_period,
    parse_schedule_file,
    set_schedule_enabled,
    write_schedule,
)
from witty_agent.session import create_agent, create_session
from witty_agent.skills import list_skills
from witty_agent.prompts import get_prompt
from witty_agent.sandbox import bash_argv
from witty_agent.tools.command import CommandSessionManager, exec_command, input_command, list_commands


class CommandSessionTests(unittest.TestCase):
    def tearDown(self) -> None:
        hooks.reset()

    def test_bash_argv_is_non_login(self) -> None:
        argv = bash_argv("printf hi")
        self.assertEqual(argv, ["bash", "--noprofile", "--norc", "-c", "printf hi"])
        self.assertNotIn("-l", argv)
        self.assertNotIn("-lc", argv)

    def test_short_command_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WITTY_WORKSPACE"] = tmp
            hooks.command_manager = CommandSessionManager()
            text = exec_command("printf hi", yield_time_ms=2000)
            self.assertIn("hi", text)
            self.assertIn("exit=0", text)

    def test_background_then_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WITTY_WORKSPACE"] = tmp
            hooks.command_manager = CommandSessionManager()
            first = exec_command("sleep 0.4; printf done", yield_time_ms=50)
            self.assertIn("process_id", first)
            process_id = first.split("process_id ", 1)[1].split(";", 1)[0].strip()
            listed = list_commands()
            self.assertIn(process_id, listed)
            later = input_command(process_id, yield_time_ms=2000)
            self.assertIn("done", later)
            self.assertIn("exit=0", later)

    def test_exec_denies_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WITTY_WORKSPACE"] = tmp
            hooks.command_manager = CommandSessionManager()
            with self.assertRaisesRegex(ValueError, r"\[sandbox: file access denied\]"):
                exec_command("cat ../secret.txt", yield_time_ms=500)
            with self.assertRaises(ValueError) as caught:
                exec_command("cat ../secret.txt", yield_time_ms=500)
            self.assertEqual(
                str(caught.exception),
                get_prompt("sandbox_denied_outside", path="../secret.txt"),
            )
            listed = list_commands()
            self.assertEqual(listed, "(no command sessions)")


class ScheduleTests(unittest.TestCase):
    def test_parse_and_min_period(self) -> None:
        self.assertEqual(parse_period("30m"), 30 * 60_000)
        self.assertGreaterEqual(parse_period("5m") or 0, MIN_PERIOD_MS)
        bad = parse_schedule_file(
            "nightly",
            'prompt = "x"\nenabled = true\nstart_at = "2026-08-13T00:00:00+00:00"\nperiod = "1m"\n',
        )
        self.assertFalse(bad.ok)

    def test_one_shot_fires_after_first_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "coder", root=root)
            start = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
            write_schedule(
                ScheduleDefinition(
                    name="once",
                    prompt="ping",
                    enabled=True,
                    start_at=start.isoformat(),
                    start_at_ms=int(start.timestamp() * 1000),
                ),
                "grid-base",
                "coder",
                root=root,
            )
            clock = {"now": int((start - timedelta(minutes=1)).timestamp() * 1000)}
            scheduler = Scheduler(root, now_ms=lambda: clock["now"])
            self.assertEqual(scheduler.tick(), [])
            clock["now"] = int((start + timedelta(seconds=1)).timestamp() * 1000)
            fires = scheduler.tick()
            self.assertEqual(len(fires), 1)
            self.assertIn("[scheduled_task]", fires[0].prompt)
            self.assertIn("ping", fires[0].prompt)
            self.assertEqual(scheduler.tick(), [])

    def test_end_at_stops_and_delete_forgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "coder", root=root)
            start = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
            end = start + timedelta(minutes=20)
            write_schedule(
                ScheduleDefinition(
                    name="pulse",
                    prompt="nudge",
                    enabled=True,
                    start_at=start.isoformat(),
                    start_at_ms=int(start.timestamp() * 1000),
                    period="5m",
                    period_ms=5 * 60_000,
                    end_at=end.isoformat(),
                    end_at_ms=int(end.timestamp() * 1000),
                ),
                "grid-base",
                "coder",
                root=root,
            )
            clock = {"now": int((start - timedelta(minutes=1)).timestamp() * 1000)}
            scheduler = Scheduler(root, now_ms=lambda: clock["now"])
            self.assertEqual(scheduler.tick(), [])
            clock["now"] = int((start + timedelta(seconds=1)).timestamp() * 1000)
            self.assertEqual(len(scheduler.tick()), 1)
            clock["now"] = int((end + timedelta(minutes=6)).timestamp() * 1000)
            self.assertEqual(scheduler.tick(), [])
            self.assertEqual(scheduler.task_status("grid-base", "coder", "pulse"), "done")
            self.assertTrue(delete_schedule("pulse", "grid-base", "coder", root=root))
            self.assertFalse(delete_schedule("pulse", "grid-base", "coder", root=root))
            forgotten = Scheduler(root, now_ms=lambda: clock["now"])
            self.assertEqual(forgotten.task_status("grid-base", "coder", "pulse"), "active")

    def test_next_slot_and_pause(self) -> None:
        start = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        period_ms = 5 * 60_000
        definition = ScheduleDefinition(
            name="pulse",
            prompt="nudge",
            enabled=True,
            start_at=start.isoformat(),
            start_at_ms=start_ms,
            period="5m",
            period_ms=period_ms,
            end_at=(start + timedelta(minutes=20)).isoformat(),
            end_at_ms=int((start + timedelta(minutes=20)).timestamp() * 1000),
        )
        before = next_slot_at(definition, start_ms - 60_000)
        self.assertEqual(before, start_ms)
        after_start = next_slot_at(definition, start_ms + 1_000)
        self.assertEqual(after_start, start_ms + period_ms)
        due = next_slot_at(definition, start_ms + period_ms + 1_000, last_slot_ms=start_ms)
        self.assertEqual(due, start_ms + period_ms)
        after_end = next_slot_at(definition, start_ms + 30 * 60_000)
        self.assertIsNone(after_end)
        one = ScheduleDefinition(
            name="once",
            prompt="ping",
            enabled=True,
            start_at=start.isoformat(),
            start_at_ms=start_ms,
        )
        self.assertEqual(next_slot_at(one, start_ms - 1), start_ms)
        self.assertIsNone(next_slot_at(one, start_ms + 1))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "coder", root=root)
            write_schedule(definition, "grid-base", "coder", root=root)
            paused = set_schedule_enabled("pulse", False, "grid-base", "coder", root=root)
            self.assertFalse(paused.enabled)
            clock = {"now": start_ms + 1_000}
            scheduler = Scheduler(root, now_ms=lambda: clock["now"])
            self.assertIsNone(scheduler.next_fire_iso("grid-base", "coder", paused))
            self.assertEqual(scheduler.tick(), [])
            resumed = set_schedule_enabled("pulse", True, "grid-base", "coder", root=root)
            self.assertTrue(resumed.enabled)
            self.assertTrue(scheduler.next_fire_iso("grid-base", "coder", resumed))


class ScheduleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_writes_schedule_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            llm = ScriptedLLM(
                [
                    tool_reply(
                        "schedule_write",
                        {
                            "name": "morning",
                            "prompt": "stand-up",
                            "start_at": "2026-08-14T01:00:00+00:00",
                            "enabled": False,
                        },
                        call_id="s1",
                    ),
                    text_reply("ok"),
                ]
            )

            async def allow(name: str, call_id: str, args: dict) -> str:
                return "allow"

            result = await session.run("add job", stream_fn=llm, approve=allow)
            tools = [item for item in result.messages if item.role == "toolResult"]
            self.assertTrue(tools)
            self.assertIn("wrote schedule", tools[0].text())

    async def test_http_delete_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "coder", root=root)
            start = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
            write_schedule(
                ScheduleDefinition(
                    name="once",
                    prompt="ping",
                    enabled=False,
                    start_at=start.isoformat(),
                    start_at_ms=int(start.timestamp() * 1000),
                    end_at=(start + timedelta(hours=1)).isoformat(),
                    end_at_ms=int((start + timedelta(hours=1)).timestamp() * 1000),
                ),
                "grid-base",
                "coder",
                root=root,
            )
            configure_api(root=root)
            status, body = await handle_request(
                "GET",
                "/v1/schedules?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["schedules"][0]["name"], "once")
            self.assertTrue(body["schedules"][0]["end_at"])
            status, deleted = await handle_request(
                "DELETE",
                "/v1/schedules/once?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertTrue(deleted["deleted"])
            status, listed = await handle_request(
                "GET",
                "/v1/schedules?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(listed["schedules"], [])

    async def test_http_create_pause_shows_next_fire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_agent("grid-base", "coder", root=root)
            configure_api(root=root)
            # 窗口必须相对当下：写死日期的日程一到期，next_fire_at 永远是 None。
            start = datetime.now(timezone.utc) + timedelta(minutes=1)
            status, created = await handle_request(
                "PUT",
                "/v1/schedules",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "name": "standup",
                    "prompt": "站会摘要",
                    "start_at": start.isoformat(),
                    "period": "30m",
                    "end_at": (start + timedelta(hours=2)).isoformat(),
                    "enabled": True,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(created["name"], "standup")
            self.assertTrue(created["next_fire_at"])
            status, listed = await handle_request(
                "GET",
                "/v1/schedules?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            row = listed["schedules"][0]
            self.assertEqual(row["name"], "standup")
            self.assertEqual(row["prompt"], "站会摘要")
            self.assertTrue(row["next_fire_at"])
            status, paused = await handle_request(
                "PATCH",
                "/v1/schedules/standup?project_id=grid-base&agent_id=coder",
                {"enabled": False},
            )
            self.assertEqual(status, 200)
            self.assertFalse(paused["enabled"])
            self.assertIsNone(paused["next_fire_at"])
            status, listed = await handle_request(
                "GET",
                "/v1/schedules?project_id=grid-base&agent_id=coder",
            )
            self.assertFalse(listed["schedules"][0]["enabled"])
            self.assertIsNone(listed["schedules"][0]["next_fire_at"])

    def test_office_skills_present(self) -> None:
        names = {item.name for item in list_skills()}
        self.assertTrue({"data-analysis", "office-document", "slides", "software-engineering"} <= names)


class LoopCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_loop_args(self) -> None:
        now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_loop_args("").action, "status")
        self.assertEqual(parse_loop_args("off").action, "stop")
        bad = parse_loop_args("1m")
        self.assertEqual(bad.action, "error")
        start = parse_loop_args("5m until 3h 继续改循环", now=now)
        self.assertEqual(start.action, "start")
        self.assertEqual(start.period, "5m")
        self.assertEqual(start.prompt, "继续改循环")
        self.assertEqual(start.end_at_ms, int((now + timedelta(hours=3)).timestamp() * 1000))
        soon = parse_loop_args("5m until 4m", now=now)
        self.assertEqual(soon.action, "error")

    async def test_slash_loop_writes_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="looptest01")
            now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
            started = apply_loop(session, "5m until 3h", now=now)
            self.assertIn("5m", started.text)
            name = loop_schedule_name(session.session_id)
            parsed = parse_schedule_file(
                name,
                (
                    root
                    / "grid-base"
                    / "agents"
                    / "coder"
                    / "agent_state"
                    / "schedule"
                    / f"{name}.toml"
                ).read_text(encoding="utf-8"),
            )
            self.assertTrue(parsed.ok)
            self.assertEqual(parsed.definition.session_id, session.session_id)
            self.assertEqual(parsed.definition.period, "5m")
            status = apply_loop(session, "")
            self.assertIn("5m", status.text)
            idle = apply_loop(session, "off")
            self.assertIn("停止", idle.text)
            missing = apply_loop(session, "off")
            self.assertIn("没有循环", missing.text)

    async def test_armed_loop_tick_runs_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="loopfire01")
            apply_loop(session, "5m")
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("loop-did-work")]))
            STATE.sessions[session.session_id] = session
            status, body = await handle_request("POST", "/v1/schedules/tick")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["fires"]), 1)
            self.assertEqual(body["fires"][0]["session_id"], session.session_id)
            self.assertEqual(body["started"][0]["session_id"], session.session_id)
            deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
            while datetime.now(timezone.utc) < deadline:
                run = STATE.runs.get(session.session_id) or {}
                if run.get("status") in {"done", "error"}:
                    break
                await asyncio.sleep(0.05)
            run = STATE.runs[session.session_id]
            self.assertEqual(run.get("status"), "done")
            self.assertIn("loop-did-work", run.get("text") or "")

    async def test_session_run_loop_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            result = await session.run(
                "/loop 5m until 3h",
                stream_fn=ScriptedLLM([text_reply("should-not-run")]),
            )
            self.assertIn("开启循环", result.messages[0].text())
            listed = await session.run(
                "/loop",
                stream_fn=ScriptedLLM([text_reply("should-not-run")]),
            )
            self.assertIn("进行中", listed.messages[0].text())


if __name__ == "__main__":
    unittest.main()
