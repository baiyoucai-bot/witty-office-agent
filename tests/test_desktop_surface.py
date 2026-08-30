from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.catalog import load_catalog, set_skill_enabled, set_tool_enabled
from witty_agent.http_api import _event_public, configure_api, handle_request, iter_run_stream_events
from witty_agent.types import AgentEvent, AgentMessage
from witty_agent.llm import ScriptedLLM, text_reply
from witty_agent.runtime import clear_runtime_cache
from witty_agent.tools.registry import ToolSpec, register_tool


class DesktopSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def test_glass_theme_and_settings_polish_are_wired(self) -> None:
        """琉璃主题（毛玻璃）三处接线 + 设置页导航图标，缺一处主题选择器就会出幽灵项。"""
        root = Path(__file__).resolve().parents[1] / "apps" / "desktop"
        html = (root / "renderer" / "index.html").read_text(encoding="utf-8")
        app = (root / "renderer" / "app.js").read_text(encoding="utf-8")
        css = (root / "renderer" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('<option value="glass">琉璃</option>', html)
        self.assertIn('class="theme-swatch" data-theme="glass"', html)
        self.assertIn('html[data-theme="glass"]', css)
        self.assertIn("backdrop-filter: blur", css)
        self.assertIn('"glass"', app)
        self.assertIn("琉璃", app)
        # 设置页导航是 JS 画的：图标 + 文案两段结构
        self.assertIn("set-ico", app)
        self.assertIn("set-copy", app)
        self.assertIn("#settings-nav .set-ico", css)
        self.assertIn("#view-settings .settings-card::before", css)

    def test_habit_pages_are_wired(self) -> None:
        root = Path(__file__).resolve().parents[1] / "apps" / "desktop"
        html = (root / "renderer" / "index.html").read_text(encoding="utf-8")
        app = (root / "renderer" / "app.js").read_text(encoding="utf-8")
        preload = (root / "preload.js").read_text(encoding="utf-8")
        api = (root / "api.js").read_text(encoding="utf-8")
        css = (root / "renderer" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-panel="network"', html)
        self.assertIn("function latestTurnHasAssistant", app)
        self.assertIn("!latestTurnHasAssistant()", app)
        self.assertNotIn(".bubble.assistant:last-of-type", app)
        self.assertIn("function loadWebSettings", app)
        self.assertIn("/v1/web", api)
        self.assertIn('id="view-links"', html)
        self.assertIn('id="view-wiki"', html)
        self.assertIn('data-view="wiki"', html)
        self.assertIn("function loadWiki", app)
        self.assertIn("function addWikiSource", app)
        self.assertIn("/v1/wiki", api)
        self.assertIn("getWiki:", preload)
        self.assertIn('id="view-diary"', html)
        self.assertIn('id="view-mail"', html)
        self.assertIn('data-view="links"', html)
        self.assertIn("getLinks:", preload)
        self.assertIn("getMail:", preload)
        self.assertIn("writeDiary:", preload)
        self.assertIn("/v1/links", api)
        self.assertIn("/v1/diary", api)
        self.assertIn("/v1/mail", api)
        self.assertIn("/v1/workspace", api)
        self.assertIn("function loadLinks", app)
        self.assertIn("function loadDiary", app)
        self.assertIn("function loadMail", app)
        self.assertIn("function sessionRefToken", app)
        self.assertIn('row.className = `session-item${current ? " active" : ""}`', app)
        self.assertNotIn('button.className = `session-item${current ? " active" : ""}`', app)
        self.assertIn("async function removeSession", app)
        self.assertIn("/not found|404/i", app)
        self.assertIn("encodeURIComponent(sessionId)", api)
        self.assertIn("function fileRefToken", app)
        self.assertIn("fileCite", app)
        self.assertIn("fileDirCite", app)
        self.assertIn("file:docs", app)
        self.assertIn("rel.endsWith(\"/\")", app)
        main = (root / "main.js").read_text(encoding="utf-8")
        self.assertIn('full.replace(/\\\\/g, "/") + "/"', main)
        self.assertIn("session-cite", app)
        self.assertIn("session:${id}", app)
        self.assertIn("file:${path}", app)
        self.assertIn(".session-cite", css)
        recall = (root / "renderer" / "recall.js").read_text(encoding="utf-8")
        self.assertIn("function recallHitsLayer", recall)
        self.assertIn("旧笔记", recall)
        self.assertIn("hit.slug || hit.id || hit.locator", recall)
        self.assertIn("memory-mixed-note", app)
        self.assertIn("混层：工作集优先，旧笔记不要当当前偏好", app)
        self.assertIn("memory-archive-browse", app)
        self.assertIn("重叠归档可浏览，不是本轮源头", app)
        self.assertIn('id="scene-dock"', html)
        self.assertIn("function fillScene", app)
        self.assertIn("function pickWorkspaceDir", app)
        self.assertIn('id="workspace-pick"', html)
        self.assertIn("send-btn", html)
        self.assertIn("function actionRoot", app)
        self.assertIn("say.user", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("flex-end", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("pickDirectory", (root / "preload.js").read_text(encoding="utf-8"))
        self.assertIn("function saveEmailFrom", app)
        self.assertIn("function renderMailStatus", app)
        self.assertIn("function bindAllSplits", app)
        self.assertIn("split-handle", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("split-handle-rail", css)
        self.assertIn("function bindRailResize", app)
        self.assertIn("拖动调整宽度", app)
        self.assertIn("还没配齐", app)
        self.assertNotIn('status.textContent = (payload && payload.text)', app)
        self.assertIn('id="mail-status"', html)
        self.assertNotIn('<pre id="mail-status"', html)
        self.assertIn("habit-check", html)
        self.assertIn("function openLocalPath", app)
        self.assertIn('id="side-toggle"', html)
        self.assertIn('id="rail-toggle"', html)
        self.assertIn('class="rail-pack"', html)
        self.assertIn("rail-pack", html)
        pack = html[html.find("<details class=\"rail-pack\">") : html.find("</details>")]
        self.assertIn("<summary>", pack)
        self.assertIn('class="glyph"', pack)
        self.assertIn("更多", pack)
        self.assertIn('id="open-mail-desk"', html)
        self.assertNotIn('class="rail-btn" data-view="mail"', html)
        self.assertNotIn('data-view="files"', html)
        self.assertNotIn('id="view-files"', html)
        self.assertNotIn('data-art="files"', html)
        self.assertNotIn('data-art="memory"', html)
        self.assertNotIn('id="ctx-memory"', html)
        self.assertNotIn('id="ctx-materials"', html)
        self.assertIn('id="artifact-list"', html)
        self.assertIn("这一会话写出来的文件", html)
        self.assertIn("sideCollapseAt", app)
        self.assertIn("split-handle-reopen", html)
        self.assertIn("workArea", (root / "main.js").read_text(encoding="utf-8"))
        self.assertIn("work.width - 24", (root / "main.js").read_text(encoding="utf-8"))
        self.assertIn("endResize", app)
        mdjs = (root / "renderer" / "markdown.js").read_text(encoding="utf-8")
        self.assertIn("raw.length > 4000", mdjs)
        self.assertIn("text.length > 24000", mdjs)
        self.assertIn("function parseFenceOpen", mdjs)
        self.assertIn("function mermaidSequence", mdjs)
        self.assertIn("function mermaidXy", mdjs)
        self.assertIn("md-diagram", css)
        self.assertIn("index % 8 === 0", app)
        self.assertIn("fs.promises.readdir", main)
        self.assertNotIn("readdirSync", (root / "main.js").read_text(encoding="utf-8"))
        self.assertIn("refreshWorkspaceFiles().catch", app)
        self.assertIn("function bindRailResize", app)
        self.assertIn("pointer-events: none !important", css)
        self.assertIn(".settings-shell", css)
        self.assertNotIn("188px) 8px minmax", css)
        self.assertIn("grid-template-columns: 240px minmax(0, 1fr)", css)
        self.assertIn("#approval-dock:not(:empty)", css)
        self.assertIn("approval-head", app)
        self.assertIn("question-opt", app)
        self.assertIn('switchView("chat");\n      await hydrateMessages();', app)
        self.assertIn("#view-chat {\n  position: relative;", css)
        self.assertIn("side-collapsed", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("rail-collapsed", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("function setSideOpen", app)
        self.assertIn("function setRailOpen", app)
        self.assertIn("rail_open", app)
        self.assertIn('data-panel="schedule"', html)
        self.assertIn("/loop 5m", html)
        self.assertIn('name: "loop"', app)
        self.assertIn('id="schedule-save"', html)
        self.assertIn('id="schedule-prompt"', html)
        self.assertIn('id="schedule-period"', html)
        self.assertIn('id="schedule-end"', html)
        self.assertIn("function loadSchedules", app)
        self.assertIn("function saveScheduleFromForm", app)
        self.assertIn("next_fire_at", app)
        self.assertIn("/v1/schedules/tick", api)
        self.assertIn('request("PUT", "/v1/schedules"', api)
        self.assertIn('request("PATCH"', api)
        self.assertIn("saveSchedule:", preload)
        self.assertIn("setScheduleEnabled:", preload)
        self.assertIn("deleteSchedule:", preload)
        self.assertIn("function noteArtifactsFromTool", app)
        self.assertIn("function finishWorkProcess", app)
        self.assertIn("function collapseToolRows", app)
        self.assertIn("node.removeAttribute(\"open\")", app)
        self.assertIn("function parkWorkNote", app)
        self.assertIn("已执行本地命令", app)
        self.assertIn("wp-io", app)
        self.assertIn("wp-note", css)
        self.assertIn("workProcessOutside", app)
        self.assertIn("function attachCopyAction", app)
        self.assertIn("function iconButton", app)
        self.assertIn("bubble-copy", app)
        self.assertIn("bubbleCopy", app)
        self.assertIn('aria-label", label', app)
        self.assertIn("外观：浅色", html)
        # 主题+连接状态收在左栏底部（rail-foot），不再是横贯窗口的独立底栏：
        # 参考图里没有那条通栏灰带，它还白占一行高度。
        self.assertIn('class="rail-foot"', html)
        self.assertGreater(html.find('class="rail-foot"'), html.find('class="rail"'))
        self.assertLess(html.find('class="rail-foot"'), html.find("</nav>"))
        self.assertGreater(html.find('class="theme-rail"'), html.find('class="rail-foot"'))
        self.assertGreater(html.find('id="status"'), html.find('class="theme-rail"'))
        self.assertNotIn("rail-dock", html)
        self.assertNotIn('class="app-foot"', html)
        self.assertIn(".rail-foot", css)
        # 底栏不再占据整块网格行，grid-template-areas 里不该再有 foot。
        self.assertNotIn("foot foot", css)
        self.assertNotIn("grid-area: foot", css)
        self.assertIn("function attachRateActions", app)
        self.assertIn("bubble-up", app)
        self.assertIn("bubbleRate", app)
        self.assertIn("witty.feedback.v1", app)
        # 「导出」按钮已删（2026-08-27）；transcriptMarkdown 留给 UI 自检用。
        self.assertNotIn('id="export-chat"', html)
        self.assertIn("function transcriptMarkdown", app)
        self.assertIn("exportShown", app)
        self.assertIn("bubble-retry", app)
        self.assertIn("bubbleRetry", app)
        self.assertIn("再发这条", app)
        self.assertIn("function forkCurrentSession", app)
        self.assertIn("bubble-fork", app)
        self.assertIn("/v1/sessions/", api)
        self.assertIn("/fork", api)
        self.assertIn("forkSession:", preload)
        self.assertIn("artifactsSessionOnly", app)
        self.assertIn("function renderTurnFiles", app)
        self.assertIn("turnFilesShown", app)
        self.assertIn("本轮产物", app)
        self.assertIn('id="queue-dock"', html)
        self.assertIn('id="plan-dock"', html)
        self.assertIn("先规划", html)
        self.assertIn("暂不改文件", html)
        self.assertIn('id="queue-dock"', html)
        self.assertIn("function enqueueFromComposer", app)
        self.assertIn("function isTerminalEvent", app)
        self.assertIn("function endRunChrome", app)
        self.assertIn('name: generation-ui', (root.parents[1] / "skills" / "generation-ui" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("调整方向", app)
        self.assertIn("function drainQueue", app)
        self.assertIn("queueShown", app)
        self.assertIn(".turn-files", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertNotIn("ARTIFACT_EXT.test(String(full", app)
        self.assertIn("wpAfter.contains(missEv)", app)
        self.assertIn("shell:openPath", (root / "main.js").read_text(encoding="utf-8"))
        self.assertIn("openPath:", preload)
        self.assertIn("saveMail:", preload)
        self.assertIn("saveMail", api)
        self.assertIn('id="email-imap-host"', html)
        self.assertIn('id="mail-imap-host"', html)
        self.assertIn('data-panel="email"', html)
        self.assertIn(".prompt-split[hidden]", (root / "renderer" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("report.linkFill", app)
        self.assertIn("link-url", app)
        self.assertIn('network: "intranet"', app)
        self.assertIn('network: "public"', app)
        self.assertIn('network: "general"', app)
        self.assertIn("skill-badge net-", app)
        self.assertIn("skill-status", app)
        self.assertIn("作者 ·", app)
        self.assertIn("内网 / 外网 / 通用", html)
        self.assertIn('id="skill-add"', html)
        self.assertIn('id="skill-mount"', html)
        self.assertIn('id="skill-reload"', html)
        self.assertIn('id="skill-install-status"', html)
        self.assertIn("function installLocalSkill", app)
        self.assertIn("function postInstallSkill", app)
        self.assertIn("function mountSkillDir", app)
        self.assertIn("function reloadPluginSurface", app)
        self.assertIn("uninstallSkill", app)
        self.assertNotIn("帮我安装一个本地技能", app)
        self.assertNotIn('promptEl.value = "新建一个技能"', app)
        self.assertIn("function createDraftSkill", app)
        self.assertIn("picked.brief", app)
        self.assertIn("body.brief", app)
        self.assertIn("一句话生成技能", html)
        self.assertIn("installSkill:", preload)
        self.assertIn("uninstallSkill:", preload)
        self.assertIn("reloadPlugins:", preload)
        self.assertIn("getPlugins:", preload)
        self.assertIn("function getPlugins", api)
        self.assertIn("skillWatchTimer", app)
        self.assertIn("skill_generation", app)
        self.assertIn("attachSkillPath:", preload)
        self.assertIn("pickSkill:", preload)
        self.assertIn("function installSkill", api)
        self.assertIn("function uninstallSkill", api)
        self.assertIn('request("POST", "/v1/skills"', api)
        self.assertIn('request("DELETE", `/v1/skills/', api)
        self.assertIn('request("POST", "/v1/plugins/reload"', api)
        self.assertIn("shell:pickSkill", (root / "main.js").read_text(encoding="utf-8"))

    def test_timeline_event_forwards_tool_error(self) -> None:
        event = AgentEvent(
            type="tool_execution_end",
            tool_call_id="c1",
            tool_name="read",
            message=AgentMessage(
                role="toolResult",
                content="boom",
                tool_call_id="c1",
                tool_name="read",
                is_error=True,
            ),
        )
        body = _event_public(event)
        self.assertTrue(body.get("is_error"))
        self.assertEqual(body.get("text"), "boom")

    def test_timeline_event_forwards_todos(self) -> None:
        event = AgentEvent(
            type="todos",
            args={"todos": [{"content": "split tokens", "status": "in_progress"}]},
        )
        body = _event_public(event)
        self.assertEqual(body.get("type"), "todos")
        self.assertEqual(body["args"]["todos"][0]["content"], "split tokens")

    async def test_mail_settings_roundtrip_hides_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = os.environ.get("WITTY_HOME")
            os.environ["WITTY_HOME"] = tmp
            configure_api(root=root)
            try:
                status, body = await handle_request(
                    "PUT",
                    "/v1/mail",
                    {
                        "project_id": "grid-base",
                        "agent_id": "coder",
                        "imap_host": "mail.intranet.grid",
                        "imap_port": 993,
                        "smtp_host": "mail.intranet.grid",
                        "smtp_port": 465,
                        "username": "zhangsan",
                        "mailbox": "INBOX",
                        "imap_password": "secret-token",
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(body["configured"])
                self.assertEqual(body["imap_host"], "mail.intranet.grid")
                self.assertIs(body["imap_password"], True)
                self.assertNotIn("secret-token", str(body))
                status, again = await handle_request(
                    "GET",
                    "/v1/mail?project_id=grid-base&agent_id=coder",
                )
                self.assertEqual(status, 200)
                self.assertEqual(again["username"], "zhangsan")
                self.assertIs(again["imap_password"], True)
                self.assertNotIn("secret-token", str(again))
                overlay = (root / "grid-base" / "agents" / "coder" / "agent_state" / "email.toml").read_text(
                    encoding="utf-8"
                )
                self.assertIn("mail.intranet.grid", overlay)
                self.assertNotIn("secret-token", overlay)
            finally:
                if previous is None:
                    os.environ.pop("WITTY_HOME", None)
                else:
                    os.environ["WITTY_HOME"] = previous

    async def test_skills_and_tools_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "desk-demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: desk-demo\ndescription: Desktop catalog fixture.\n---\n# Demo\n\nBody.\n",
                encoding="utf-8",
            )
            os.environ["WITTY_SKILLS_PATH"] = str(root / "skills")
            clear_runtime_cache()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("ok")]))
            try:
                status, body = await handle_request(
                    "GET",
                    "/v1/skills?project_id=grid-base&agent_id=coder",
                )
                self.assertEqual(status, 200)
                names = {item["name"] for item in body["skills"]}
                self.assertIn("desk-demo", names)
                demo = next(item for item in body["skills"] if item["name"] == "desk-demo")
                self.assertEqual(demo["network"], "general")
                self.assertEqual(demo["network_label"], "通用")
                ppt = next(item for item in body["skills"] if item["name"] == "witty-ppt-skills")
                self.assertEqual(ppt["network"], "intranet")
                self.assertEqual(ppt["network_label"], "内网")
                status, detail = await handle_request("GET", "/v1/skills/desk-demo")
                self.assertEqual(status, 200)
                self.assertIn("Body", detail["body"])
                self.assertTrue(detail["enabled"])
                self.assertEqual(detail["network"], "general")
                self.assertEqual(detail["network_label"], "通用")
                status, updated = await handle_request(
                    "PUT",
                    "/v1/skills/desk-demo",
                    {"enabled": False, "project_id": "grid-base", "agent_id": "coder"},
                )
                self.assertEqual(status, 200)
                self.assertFalse(updated["enabled"])
                catalog = load_catalog("grid-base", "coder", root=root)
                self.assertFalse(catalog.skill_enabled("desk-demo"))
            finally:
                os.environ.pop("WITTY_SKILLS_PATH", None)
                clear_runtime_cache()

    async def test_install_user_skill_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox" / "desk-pack"
            inbox.mkdir(parents=True)
            (inbox / "SKILL.md").write_text(
                "---\nname: desk-pack\ndescription: Installed from desktop.\nnetwork: intranet\n---\n# Pack\n",
                encoding="utf-8",
            )
            (inbox / "scripts").mkdir()
            (inbox / "scripts" / "ok.py").write_text("print(1)\n", encoding="utf-8")
            configure_api(root=root)
            status, body = await handle_request(
                "POST",
                "/v1/skills",
                {"source": str(inbox), "project_id": "grid-base", "agent_id": "coder"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["name"], "desk-pack")
            self.assertEqual(body["origin"], "user")
            self.assertEqual(body["network"], "intranet")
            self.assertTrue(Path(body["path"], "scripts", "ok.py").is_file())
            status, err = await handle_request(
                "POST",
                "/v1/skills",
                {"source": str(inbox), "project_id": "grid-base", "agent_id": "coder"},
            )
            self.assertEqual(status, 409)
            self.assertEqual(err["name"], "desk-pack")
            status, listed = await handle_request(
                "GET",
                "/v1/skills?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertTrue(any(item["name"] == "desk-pack" for item in listed["user"]))
            status, typed = await handle_request(
                "POST",
                "/v1/skills",
                {
                    "text": "---\nname: typed-pack\ndescription: From SKILL.md text.\n---\n# Text\n",
                    "project_id": "grid-base",
                    "agent_id": "coder",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(typed["name"], "typed-pack")
            status, missing = await handle_request(
                "POST",
                "/v1/skills",
                {"source": str(root / "nope"), "project_id": "grid-base", "agent_id": "coder"},
            )
            self.assertEqual(status, 404)

    async def test_list_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            status, body = await handle_request("GET", "/v1/commands")
            self.assertEqual(status, 200)
            names = {item["name"] for item in body["commands"]}
            self.assertEqual(names, {"abort", "compact", "loop", "plan"})
            self.assertTrue(all(item["kernel"] for item in body["commands"]))
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {"project_id": "grid-base", "agent_id": "coder"},
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, live = await handle_request("GET", f"/v1/commands?session_id={sid}")
            self.assertEqual(status, 200)
            live_names = {item["name"] for item in live["commands"]}
            self.assertEqual(live_names, {"abort", "compact", "create-skill", "loop", "plan", "refine"})
            self.assertFalse(next(item["kernel"] for item in live["commands"] if item["name"] == "create-skill"))
            self.assertFalse(next(item["kernel"] for item in live["commands"] if item["name"] == "refine"))

    async def test_kernel_tool_cannot_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            status, body = await handle_request("GET", "/v1/tools")
            self.assertEqual(status, 200)
            self.assertTrue(any(item["kernel"] and item["name"] == "write" for item in body["tools"]))
            status, err = await handle_request("PUT", "/v1/tools/write", {"enabled": False})
            self.assertEqual(status, 400)
            self.assertIn("内核", err["error"])

    async def test_business_tool_can_disable(self) -> None:
        def ping() -> str:
            return "pong"

        register_tool(
            ToolSpec(
                name="surface_ping",
                description="catalog test tool",
                parameters={"type": "object", "properties": {}},
                func=ping,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            status, updated = await handle_request(
                "PUT",
                "/v1/tools/surface_ping",
                {"enabled": False, "project_id": "grid-base", "agent_id": "coder"},
            )
            self.assertEqual(status, 200)
            self.assertFalse(updated["enabled"])
            catalog = load_catalog("grid-base", "coder", root=root)
            self.assertFalse(catalog.tool_enabled("surface_ping"))
            set_tool_enabled("surface_ping", True, "grid-base", "coder", root=root)

    async def test_list_sessions_and_stream_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("stream-hi")]))
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, listed = await handle_request(
                "GET",
                "/v1/sessions?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertTrue(any(item["session_id"] == sid for item in listed["sessions"]))
            status, started = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "hello", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["text"], "stream-hi")
            kinds = {item.get("type") for item in done["timeline"]}
            self.assertIn("text_delta", kinds)
            self.assertIn("done", kinds)

    async def test_iter_run_stream_events_replays_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("stream-hi")]))
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, started = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "hello", "approval_mode": "allow-all", "wait": False},
            )
            self.assertEqual(status, 202)
            frames = list(iter_run_stream_events(sid, wait=0.01))
            kinds = [item.get("type") for item in frames]
            self.assertIn("text_delta", kinds)
            self.assertEqual(kinds[-1], "done")
            done = next(item for item in frames if item.get("type") == "done")
            self.assertEqual(done.get("text") or started.get("text") or "stream-hi", "stream-hi")

    async def test_sessions_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("a"), text_reply("b")]))
            first = await handle_request(
                "POST",
                "/v1/sessions",
                {"project_id": "grid-base", "agent_id": "coder", "workspace_dir": str(workspace)},
            )
            self.assertEqual(first[0], 200)
            sid_old = first[1]["session_id"]
            await handle_request(
                "POST",
                f"/v1/sessions/{sid_old}/messages",
                {"prompt": "old", "approval_mode": "allow-all"},
            )
            await asyncio.sleep(0.05)
            second = await handle_request(
                "POST",
                "/v1/sessions",
                {"project_id": "grid-base", "agent_id": "coder", "workspace_dir": str(workspace)},
            )
            sid_new = second[1]["session_id"]
            await handle_request(
                "POST",
                f"/v1/sessions/{sid_new}/messages",
                {"prompt": "new", "approval_mode": "allow-all"},
            )
            status, listed = await handle_request(
                "GET",
                "/v1/sessions?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            ids = [item["session_id"] for item in listed["sessions"]]
            self.assertLess(ids.index(sid_new), ids.index(sid_old))
            titles = {item["session_id"]: item["title"] for item in listed["sessions"]}
            self.assertEqual(titles[sid_new], "new")
            self.assertEqual(titles[sid_old], "old")

    async def test_reasoning_timeline_and_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(
                root=root,
                stream_factory=lambda: ScriptedLLM(
                    [text_reply("final-ok", reasoning="because two")]
                ),
            )
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, started = await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {
                    "prompt": "why",
                    "approval_mode": "allow-all",
                    "think_level": "long",
                    "wait": False,
                },
            )
            self.assertEqual(status, 202)
            done = None
            for _ in range(200):
                status, run = await handle_request("GET", f"/v1/sessions/{sid}/run")
                self.assertEqual(status, 200)
                if run["status"] in {"done", "error"}:
                    done = run
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(done)
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["reasoning"], "because two")
            kinds = {item.get("type") for item in done["timeline"]}
            self.assertIn("reasoning_delta", kinds)
            status, body = await handle_request("GET", f"/v1/sessions/{sid}/messages")
            self.assertEqual(status, 200)
            assistant = [item for item in body["messages"] if item["role"] == "assistant"]
            self.assertTrue(assistant)
            self.assertEqual(assistant[-1]["reasoning"], "because two")

    async def test_set_skill_enabled_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = set_skill_enabled("office-document", False, "grid-base", "coder", root=root)
            self.assertFalse(catalog.skill_enabled("office-document"))
            catalog = set_skill_enabled("office-document", True, "grid-base", "coder", root=root)
            self.assertTrue(catalog.skill_enabled("office-document"))

    async def test_system_and_user_skills_split(self) -> None:
        from witty_agent.layout import skills_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = skills_dir("grid-base", "coder", root=root)
            user_dir.mkdir(parents=True)
            skill = user_dir / "my-note"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: my-note\ndescription: User owned skill.\n---\n# Note\n",
                encoding="utf-8",
            )
            configure_api(root=root)
            status, body = await handle_request(
                "GET",
                "/v1/skills?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertIn("system", body)
            self.assertIn("user", body)
            self.assertTrue(any(item["name"] == "my-note" for item in body["user"]))
            self.assertTrue(all(item["origin"] == "user" for item in body["user"]))
            self.assertTrue(all(item["origin"] == "system" for item in body["system"]))

    async def test_multiple_models_and_activate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = os.environ.get("WITTY_API_KEY")
            os.environ.pop("WITTY_API_KEY", None)
            configure_api(root=root)
            try:
                status, created = await handle_request(
                    "PUT",
                    "/v1/models",
                    {
                        "name": "flash",
                        "model_id": "deepseek-v4-flash-0731",
                        "base_url": "http://192.168.1.100:8000/v1",
                        "display_name": "Flash",
                        "api_key": "sk-flash-secret",
                        "project_id": "grid-base",
                        "agent_id": "coder",
                    },
                )
                self.assertEqual(status, 200)
                status, created = await handle_request(
                    "PUT",
                    "/v1/models",
                    {
                        "name": "pro",
                        "model_id": "deepseek-v4-pro",
                        "base_url": "http://192.168.1.100:8000/v1",
                        "display_name": "Pro",
                        "api_key": "sk-pro-secret",
                        "activate": False,
                        "project_id": "grid-base",
                        "agent_id": "coder",
                    },
                )
                self.assertEqual(status, 200)
                names = {item["name"] for item in created["models"]}
                self.assertIn("flash", names)
                self.assertIn("pro", names)
                self.assertNotIn("sk-flash-secret", str(created))
                status, switched = await handle_request(
                    "POST",
                    "/v1/models/pro/activate",
                    {"project_id": "grid-base", "agent_id": "coder"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(switched["active"], "pro")
                status, current = await handle_request(
                    "GET",
                    "/v1/model?project_id=grid-base&agent_id=coder",
                )
                self.assertEqual(current["model_id"], "deepseek-v4-pro")
            finally:
                os.environ.pop("WITTY_API_KEY", None)
                if previous is not None:
                    os.environ["WITTY_API_KEY"] = previous

    async def test_model_name_with_dots_is_normalized_not_rejected(self) -> None:
        """用户手输 gpt-5.6 这种带点的目录名要折成 gpt-5-6 存下，而不是 400 赶人。"""
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            status, body = await handle_request(
                "PUT",
                "/v1/models",
                {
                    "name": "GPT-5.6",
                    "model_id": "gpt-5.6-sol",
                    "base_url": "http://192.168.1.100:8000/v1",
                    "api_key": "sk-dot-secret",
                },
            )
            self.assertEqual(status, 200)
            names = {item["name"] for item in body["models"]}
            self.assertIn("gpt-5-6", names)
            saved = next(item for item in body["models"] if item["name"] == "gpt-5-6")
            self.assertEqual(saved["model_id"], "gpt-5.6-sol")

    async def test_model_name_that_cannot_be_slugged_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configure_api(root=Path(tmp))
            status, body = await handle_request(
                "PUT",
                "/v1/models",
                {"name": "旗舰模型", "model_id": "x", "base_url": "http://h/v1"},
            )
            self.assertEqual(status, 400)
            self.assertIn("模型名不合法", body["error"])

    async def test_delete_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("bye")]))
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            sid = session["session_id"]
            await handle_request(
                "POST",
                f"/v1/sessions/{sid}/messages",
                {"prompt": "hi", "approval_mode": "allow-all"},
            )
            status, deleted = await handle_request(
                "DELETE",
                f"/v1/sessions/{sid}?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertTrue(deleted["ok"])
            status, listed = await handle_request(
                "GET",
                "/v1/sessions?project_id=grid-base&agent_id=coder",
            )
            self.assertFalse(any(item["session_id"] == sid for item in listed["sessions"]))
            status, missing = await handle_request(
                "DELETE",
                f"/v1/sessions/{sid}?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertTrue(missing["ok"])
            self.assertFalse(missing["existed"])

    async def test_delete_new_session_without_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root, stream_factory=lambda: ScriptedLLM([text_reply("bye")]))
            status, session = await handle_request(
                "POST",
                "/v1/sessions",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                },
            )
            self.assertEqual(status, 200)
            sid = session["session_id"]
            status, listed = await handle_request(
                "GET",
                "/v1/sessions?project_id=grid-base&agent_id=coder",
            )
            self.assertTrue(any(item["session_id"] == sid for item in listed["sessions"]))
            status, deleted = await handle_request(
                "DELETE",
                f"/v1/sessions/{sid}?project_id=grid-base&agent_id=coder",
            )
            self.assertEqual(status, 200)
            self.assertTrue(deleted["ok"])
            status, listed = await handle_request(
                "GET",
                "/v1/sessions?project_id=grid-base&agent_id=coder",
            )
            self.assertFalse(any(item["session_id"] == sid for item in listed["sessions"]))

    async def test_model_key_not_echoed_and_approval_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = os.environ.get("WITTY_API_KEY")
            os.environ.pop("WITTY_API_KEY", None)
            configure_api(root=root)
            try:
                status, before = await handle_request("GET", "/v1/model")
                self.assertEqual(status, 200)
                self.assertFalse(before["has_key"])
                self.assertIn("always-ask", before["approval_modes"])
                self.assertIn("max_tokens", before)
                self.assertIn("timeout_sec", before)
                status, saved = await handle_request(
                    "PUT",
                    "/v1/model",
                    {
                        "api_key": "sk-test-should-not-echo",
                        "base_url": "http://192.168.1.100:8000/v1",
                        "model_id": "deepseek-v4-flash-0731",
                        "project_id": "default_project",
                        "agent_id": "default_agent",
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(saved["has_key"])
                raw = str(saved)
                self.assertNotIn("sk-test-should-not-echo", raw)
                status, bad = await handle_request(
                    "POST",
                    "/v1/sessions/nope/messages",
                    {"prompt": "x", "approval_mode": "yolo"},
                )
                self.assertEqual(status, 404)
                status, created = await handle_request(
                    "POST",
                    "/v1/sessions",
                    {"project_id": "grid-base", "agent_id": "coder"},
                )
                status, rejected = await handle_request(
                    "POST",
                    f"/v1/sessions/{created['session_id']}/messages",
                    {"prompt": "x", "approval_mode": "yolo"},
                )
                self.assertEqual(status, 400)
            finally:
                os.environ.pop("WITTY_API_KEY", None)
                if previous is not None:
                    os.environ["WITTY_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
