"""会话的活性：一轮跑完/跑崩/被中止之后，下一轮都必须还能发出去。

这一组盯的是同一个故障面——`STATE.runs[sid]["status"]` 卡在 `running` 再也不落地。它一旦
发生，`POST /messages` 会一直返回 409 "run in progress"，这个会话就废了，用户只能新开一个。
表面症状离根因很远，所以每条路径都单独钉住：

  * 后台养护跨轮：上一轮的 `asyncio.run` 关掉循环时会取消绑在它上面的任务，下一轮 await
    它拿到的是 `CancelledError`。那是 `BaseException`，`except Exception` 接不住。
  * worker 兜底：任何漏出来的 `BaseException` 都不许让 run 留在非终态。
  * 中止：中止是协作式的，worker 可能还卡在长模型调用里，但界面已经显示「已停止生成」，
    服务端必须当场把 run 落终态，否则用户接着发就被挡。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from witty_agent.http_api import STATE, configure_api, handle_request
from witty_agent.llm import ScriptedLLM, text_reply


def _sandbox() -> tempfile.TemporaryDirectory:
    """后台养护线程会活过这一轮，收尾时可能还在往里写——清不干净不算测试失败。"""
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def _new_session(root: Path, workspace: Path) -> str:
    status, _ = await handle_request(
        "POST", "/v1/agents", {"project_id": "grid-base", "agent_id": "coder"}
    )
    assert status == 200
    status, session = await handle_request(
        "POST",
        "/v1/sessions",
        {"project_id": "grid-base", "agent_id": "coder", "workspace_dir": str(workspace)},
    )
    assert status == 200
    return str(session["session_id"])


async def _settle(sid: str, tries: int = 200) -> dict:
    for _ in range(tries):
        _status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
        if run["status"] in {"done", "error"}:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"run 一直没落终态：{run}")


class RunLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_turn_is_not_poisoned_by_the_first_turns_upkeep(self) -> None:
        """第一轮会派后台养护（首轮必然触发 GC 清扫），第二轮开跑前要等它。

        养护若绑在第一轮的事件循环上，循环一关它就成了 cancelled task；第二轮 await 它
        拿到 `CancelledError`，一路漏穿 worker 把线程静默做掉——第二轮永远停在 running。
        """
        with _sandbox() as tmp:
            root, workspace = Path(tmp), Path(tmp) / "ws"
            workspace.mkdir()
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM([text_reply("第一轮"), text_reply("第二轮")]),
            )
            sid = await _new_session(root, workspace)

            status, _started = await handle_request(
                "POST", f"/v1/sessions/{sid}/messages", {"prompt": "一", "wait": False}
            )
            self.assertEqual(status, 202)
            first = await _settle(sid)
            self.assertEqual(first["status"], "done")

            status, second = await handle_request(
                "POST", f"/v1/sessions/{sid}/messages", {"prompt": "二", "wait": False}
            )
            self.assertEqual(status, 202, f"第二轮被挡：{second}")
            self.assertEqual((await _settle(sid))["status"], "done")

    async def test_base_exception_still_lands_a_verdict(self) -> None:
        """`CancelledError` 不是 `Exception`。它漏穿 worker 的话 run 会永远停在 running。"""

        class Exploding:
            think_level = "short"

            async def __call__(self, _context):
                raise asyncio.CancelledError()

        with _sandbox() as tmp:
            root, workspace = Path(tmp), Path(tmp) / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=Exploding)
            sid = await _new_session(root, workspace)

            status, _started = await handle_request(
                "POST", f"/v1/sessions/{sid}/messages", {"prompt": "炸", "wait": False}
            )
            self.assertEqual(status, 202)
            self.assertEqual((await _settle(sid))["status"], "error")

            # 落了终态，这个会话就还能用
            status, again = await handle_request(
                "POST", f"/v1/sessions/{sid}/messages", {"prompt": "再来", "wait": False}
            )
            self.assertEqual(status, 202, f"会话被之前的崩溃卡死了：{again}")

    async def test_abort_frees_the_session_immediately(self) -> None:
        """中止要当场放行。worker 认账要等到下一个回合边界，用户不该陪它等。"""
        held = threading.Event()

        class Stalling:
            think_level = "short"

            async def __call__(self, _context):
                await asyncio.to_thread(held.wait, 10)
                return text_reply("终于回来了")

        with _sandbox() as tmp:
            root, workspace = Path(tmp), Path(tmp) / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=Stalling)
            sid = await _new_session(root, workspace)
            try:
                status, _started = await handle_request(
                    "POST", f"/v1/sessions/{sid}/messages", {"prompt": "慢活", "wait": False}
                )
                self.assertEqual(status, 202)
                for _ in range(100):
                    if STATE.runs.get(sid, {}).get("status") == "running":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(STATE.runs[sid]["status"], "running")

                status, ack = await handle_request("POST", f"/v1/sessions/{sid}/abort", {})
                self.assertEqual(status, 200)
                self.assertTrue(ack["aborted"])
                self.assertEqual(STATE.runs[sid]["status"], "done")

                # 关键：模型还没返回，但守卫已经放开，用户能接着发
                status, again = await handle_request(
                    "POST", f"/v1/sessions/{sid}/messages", {"prompt": "你还在吗", "wait": False}
                )
                self.assertEqual(status, 202, f"停止生成之后仍然发不出去：{again}")
            finally:
                held.set()

    async def test_abort_on_an_idle_session_is_a_noop(self) -> None:
        with _sandbox() as tmp:
            root, workspace = Path(tmp), Path(tmp) / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("好")]))
            sid = await _new_session(root, workspace)
            status, ack = await handle_request("POST", f"/v1/sessions/{sid}/abort", {})
            self.assertEqual(status, 200)
            self.assertFalse(ack["aborted"])


class SettleHarvestTests(unittest.IsolatedAsyncioTestCase):
    """`_settle_harvest` 是每轮开跑前的必经点，它抛什么都会变成「整轮发不出去」。"""

    def _session(self, pending: object):
        from witty_agent.session import Session

        holder = Session.__new__(Session)
        holder._harvest_pending = pending
        return holder

    async def test_background_failure_does_not_escape(self) -> None:
        from concurrent.futures import Future

        from witty_agent.session import Session

        failed: Future = Future()
        failed.set_exception(RuntimeError("判官挂了"))
        await Session._settle_harvest(self._session(failed))

    async def test_background_cancellation_does_not_escape(self) -> None:
        from concurrent.futures import Future

        from witty_agent.session import Session

        cancelled: Future = Future()
        cancelled.cancel()
        await Session._settle_harvest(self._session(cancelled))

    async def test_slow_background_is_kept_for_the_next_turn(self) -> None:
        from concurrent.futures import Future

        from witty_agent.session import Session

        slow: Future = Future()
        holder = self._session(slow)
        await Session._settle_harvest(holder)
        self.assertIs(holder._harvest_pending, slow)
        slow.set_result({"added": 0})


if __name__ == "__main__":
    unittest.main()
