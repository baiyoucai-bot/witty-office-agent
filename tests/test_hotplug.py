from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.http_api import configure_api, handle_request
from witty_agent.kernel_surface import KERNEL_TOOL_PACKAGE
from witty_agent.mcp import McpClient, McpServerSpec, mcp_clients
from witty_agent.plugins.effects import EffectStack
from witty_agent.plugins.live import (
    attach_mcp,
    attach_package,
    attach_skill_path,
    detach_mcp,
    detach_package,
    detach_skill_path,
    flush_pending,
    public_live,
    reset_live,
    set_busy_probe,
)
from witty_agent.runtime import clear_runtime_cache, tool_packages
from witty_agent.skills import list_skills
from witty_agent.tools.registry import list_tools


def _write_skill(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: hotplug {name}\nnetwork: general\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return folder


def _mcp_script(path: Path, *, notify: bool = False) -> Path:
    flag = "True" if notify else "False"
    path.write_text(
        "import json, sys\n"
        f"NOTIFY = {flag}\n"
        "listed = 0\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    req = json.loads(line)\n"
        "    if 'id' not in req:\n"
        "        continue\n"
        "    method = req.get('method')\n"
        "    mid = req['id']\n"
        "    if method == 'initialize':\n"
        "        result = {'protocolVersion': '2024-11-05', 'capabilities': "
        "{'tools': {'listChanged': True}}, 'serverInfo': {'name': 'fake', 'version': '0'}}\n"
        "    elif method == 'tools/list':\n"
        "        listed += 1\n"
        "        tools = [{'name': 'ping', 'description': 'ping', "
        "'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}]\n"
        "        if listed > 1:\n"
        "            tools.append({'name': 'pong', 'description': 'pong', "
        "'inputSchema': {'type': 'object', 'properties': {}}})\n"
        "        result = {'tools': tools}\n"
        "    else:\n"
        "        result = {'content': [{'type': 'text', 'text': 'pong'}]}\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': result}) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    if NOTIFY and method == 'tools/call':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', "
        "'method': 'notifications/tools/list_changed'}) + '\\n')\n"
        "        sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return path


class HotplugTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        set_busy_probe(None)
        reset_live(persist=False)

    def test_skill_path_attach_and_detach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            reset_live(persist=False)
            extra = root / "extra-skills"
            _write_skill(extra, "hot-alpha")
            before = {item.name for item in list_skills()}
            self.assertNotIn("hot-alpha", before)
            attached = attach_skill_path(extra)
            self.assertTrue(attached["reloaded"])
            self.assertIn("hot-alpha", {item.name for item in list_skills()})
            detach_skill_path(extra)
            self.assertNotIn("hot-alpha", {item.name for item in list_skills()})

    def test_cannot_detach_kernel_package(self) -> None:
        with self.assertRaises(ValueError):
            detach_package(KERNEL_TOOL_PACKAGE)
        with self.assertRaises(ValueError):
            attach_package(KERNEL_TOOL_PACKAGE)

    def test_business_package_detach_and_attach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            reset_live(persist=False)
            self.assertIn("witty_agent.plugins", tool_packages())
            names = {item.name for item in list_tools()}
            self.assertIn("mail_status", names)
            detach_package("witty_agent.plugins")
            self.assertNotIn("witty_agent.plugins", tool_packages())
            self.assertNotIn("mail_status", {item.name for item in list_tools()})
            attach_package("witty_agent.plugins")
            self.assertIn("witty_agent.plugins", tool_packages())
            self.assertIn("mail_status", {item.name for item in list_tools()})

    async def test_http_hotplug_and_uninstall_user_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            reset_live(persist=False)
            extra = root / "more-skills"
            _write_skill(extra, "hot-beta")
            status, mounted = await handle_request("POST", "/v1/plugins/paths", {"path": str(extra)})
            self.assertEqual(status, 200)
            self.assertIn(str(extra.resolve()), mounted["extra_skill_paths"])
            status, listed = await handle_request("GET", "/v1/skills")
            self.assertEqual(status, 200)
            self.assertIn("hot-beta", {item["name"] for item in listed["skills"]})
            status, created = await handle_request(
                "POST",
                "/v1/skills",
                {
                    "text": "---\nname: hot-user\ndescription: user drop\nnetwork: general\n---\n\n# u\n"
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(created["origin"], "user")
            status, gone = await handle_request("DELETE", "/v1/skills/hot-user")
            self.assertEqual(status, 200)
            self.assertTrue(gone["removed"])
            status, after = await handle_request("GET", "/v1/skills")
            self.assertNotIn("hot-user", {item["name"] for item in after["skills"]})
            status, blocked = await handle_request(
                "DELETE", "/v1/plugins/packages", {"package": KERNEL_TOOL_PACKAGE}
            )
            self.assertEqual(status, 400)
            status, reloaded = await handle_request("POST", "/v1/plugins/reload", {})
            self.assertEqual(status, 200)
            self.assertTrue(reloaded["hotplug"])
            status, plugins = await handle_request("GET", "/v1/plugins")
            self.assertEqual(status, 200)
            self.assertTrue(plugins["hotplug"])
            self.assertTrue(plugins["kernel_locked"])

    def test_effects_unwind_lifo(self) -> None:
        order: list[int] = []
        stack = EffectStack()
        stack.push("demo", lambda: order.append(1), "first")
        stack.push("demo", lambda: order.append(2), "second")
        self.assertEqual(stack.unwind("demo"), 2)
        self.assertEqual(order, [2, 1])
        self.assertEqual(stack.unwind("demo"), 0)

    def test_busy_defers_mcp_close(self) -> None:
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            reset_live(persist=False)
            script = _mcp_script(root / "server.py")
            attached = attach_mcp("fake", sys.executable, [str(script)], force=True)
            self.assertTrue(attached["applied"])
            self.assertIn("fake", mcp_clients())
            self.assertTrue(mcp_clients()["fake"].alive)
            set_busy_probe(lambda: True)
            detached = detach_mcp("fake")
            self.assertTrue(detached["deferred"])
            self.assertTrue(public_live()["pending"])
            self.assertIn("fake", mcp_clients())
            self.assertTrue(mcp_clients()["fake"].alive)
            set_busy_probe(lambda: False)
            flushed = flush_pending()
            self.assertIsNotNone(flushed)
            self.assertTrue(flushed["applied"])
            self.assertNotIn("fake", mcp_clients())

    def test_mcp_list_changed_refreshes_tools(self) -> None:
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            script = _mcp_script(Path(tmp) / "server.py", notify=True)
            client = McpClient(McpServerSpec(name="fake", command=sys.executable, args=[str(script)]))
            try:
                tools = client.connect()
                self.assertEqual([item.name for item in tools], ["mcp__fake__ping"])
                self.assertIn("pong", tools[0].func(q="hi"))
                self.assertTrue(client.list_changed or client.drain(timeout=0.5))
                refreshed = client.refresh_tools()
                self.assertEqual(
                    [item.name for item in refreshed],
                    ["mcp__fake__ping", "mcp__fake__pong"],
                )
            finally:
                client.close()

    def test_package_detach_cascades_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            reset_live(persist=False)
            result = detach_package("witty_agent.plugins.mail")
            self.assertIn("witty_agent.plugins.mail", result["cascade"])
            self.assertNotIn("mail_status", {item.name for item in list_tools()})
            attach_package("witty_agent.plugins.mail")
            self.assertIn("mail_status", {item.name for item in list_tools()})

    def test_reconcile_only_touches_delta(self) -> None:
        from witty_agent.plugins.live import last_reconcile, mounted_units

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            reset_live(persist=False)
            first = root / "skills-a"
            second = root / "skills-b"
            _write_skill(first, "delta-one")
            _write_skill(second, "delta-two")
            attach_skill_path(first)
            attach_skill_path(second)
            added = last_reconcile()["added"]
            self.assertTrue(any(str(item).endswith(str(second.resolve())) or "delta" in str(item) or str(second.resolve()) in str(item) for item in added) or f"skill:{second.resolve()}" in added)
            self.assertIn(f"skill:{second.resolve()}", mounted_units())
            detach_skill_path(first)
            self.assertIn(f"skill:{first.resolve()}", last_reconcile()["removed"])
            self.assertNotIn(f"skill:{first.resolve()}", mounted_units())
            self.assertIn(f"skill:{second.resolve()}", mounted_units())

    def test_watch_bumps_generation_on_new_skill(self) -> None:
        from witty_agent.plugins.watch import poll_once, reset_watch, skill_generation

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            reset_live(persist=False)
            extra = root / "watched"
            extra.mkdir()
            attach_skill_path(extra)
            reset_watch()
            poll_once()
            before = skill_generation()
            _write_skill(extra, "watched-new")
            snap = poll_once()
            self.assertTrue(snap["skills_changed"])
            self.assertGreater(skill_generation(), before)

    def test_allowed_tools_and_compatibility(self) -> None:
        from witty_agent.skill_guard import allowlist_for_skills, skill_compatible, tool_permitted
        from witty_agent.skills import SkillMeta, bind_skill_scope, install_user_skill, list_user_skills, reset_skill_scope

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            reset_live(persist=False)
            token = bind_skill_scope("default_project", "default_agent", root)
            self.addCleanup(lambda: reset_skill_scope(token))
            install_user_skill(
                text="---\nname: tight-read\ndescription: only read\nallowed-tools: Read Grep\nnetwork: general\n---\n\n# t\n",
                project_id="default_project",
                agent_id="default_agent",
                root=root,
            )
            allow = allowlist_for_skills(["tight-read"])
            self.assertIsNotNone(allow)
            self.assertTrue(tool_permitted("read", allow))
            self.assertTrue(tool_permitted("grep", allow))
            self.assertFalse(tool_permitted("write", allow))
            self.assertTrue(tool_permitted("skill", allow))
            self.assertTrue(tool_permitted("ask_user_question", allow))
            blocked = SkillMeta(
                name="need-net",
                description="x",
                path=root,
                skill_file=root / "SKILL.md",
                compatibility="requires-internet",
            )
            # 外网技能只在「内网」策略下不兼容；默认放行外网，所以这段自己声明策略。
            os.environ["WITTY_WEB_DENY_PUBLIC"] = "1"
            clear_runtime_cache()
            self.addCleanup(clear_runtime_cache)
            self.addCleanup(lambda: os.environ.pop("WITTY_WEB_DENY_PUBLIC", None))
            self.assertFalse(skill_compatible(blocked))
            install_user_skill(
                text="---\nname: need-net\ndescription: wan\ncompatibility: 需要外网\nnetwork: general\n---\n\n# n\n",
                project_id="default_project",
                agent_id="default_agent",
                root=root,
            )
            names = {item.name for item in list_user_skills("default_project", "default_agent", root=root)}
            self.assertIn("tight-read", names)
            self.assertNotIn("need-net", names)
