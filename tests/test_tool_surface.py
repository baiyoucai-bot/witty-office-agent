from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.llm import text_reply
from witty_agent.memory import resolve_session_memory, write_topic
from witty_agent.prompts import get_prompt
from witty_agent.session import create_agent, create_session
from witty_agent.system_prompt import build_system_prompt
from witty_agent.tools.registry import list_tools
from witty_agent.tool_surface import CORE_TOOLS, select_advertised_names


class ToolSurfaceTests(unittest.TestCase):
    def test_plain_turn_hides_situational_tools(self) -> None:
        names = select_advertised_names("say hi", sorted(KERNEL_TOOLS))
        self.assertNotIn("read", names)
        self.assertNotIn("write", names)
        self.assertNotIn("bash", names)
        self.assertNotIn("todo_write", names)
        self.assertNotIn("schedule_write", names)
        self.assertNotIn("run_fanout", names)
        self.assertNotIn("job_kill", names)
        self.assertNotIn("session_query", names)
        self.assertEqual(names, [])
        task = select_advertised_names("review the auth module", sorted(KERNEL_TOOLS))
        self.assertIn("read", task)
        self.assertIn("write", task)
        self.assertIn("ask_user_question", task)
        self.assertNotIn("web_fetch", task)
        self.assertNotIn("todo_write", task)
        self.assertNotIn("memory_read", task)
        self.assertNotIn("memory_write", task)
        self.assertNotIn("memory_status", task)
        self.assertIn("skill", task)
        self.assertIn("list_available_skills", task)
        self.assertNotIn("schedule_write", task)
        bare = select_advertised_names("write hello.py", sorted(KERNEL_TOOLS))
        self.assertIn("write", bare)
        self.assertNotIn("skill", bare)
        self.assertNotIn("list_available_skills", bare)
        self.assertLess(len(task), len(KERNEL_TOOLS))
        choice = select_advertised_names("OAuth2 还是 JWT？", sorted(KERNEL_TOOLS))
        self.assertIn("ask_user_question", choice)
        self.assertIn("read", choice)
        self.assertNotIn("write", choice)
        self.assertNotIn("edit", choice)
        self.assertNotIn("bash", choice)
        mixed = select_advertised_names("帮我写一份报告，用青绿模板还是简约风？", sorted(KERNEL_TOOLS))
        self.assertIn("ask_user_question", mixed)
        self.assertIn("write", mixed)
        follow = select_advertised_names(
            "做一下",
            sorted(KERNEL_TOOLS),
            prior_text="OAuth2 还是 JWT？",
        )
        self.assertIn("ask_user_question", follow)
        self.assertIn("write", follow)
        write_after = select_advertised_names(
            "write hello.py",
            sorted(KERNEL_TOOLS),
            prior_text="OAuth2 还是 JWT？",
        )
        self.assertIn("write", write_after)
        mail = select_advertised_names(
            "看一下收件箱",
            sorted(KERNEL_TOOLS) + ["mail_status", "mail_list", "mail_read", "mail_analyze", "mail_draft", "mail_send"],
        )
        self.assertIn("mail_list", mail)
        self.assertIn("mail_analyze", mail)
        self.assertIn("mail_draft", mail)
        self.assertNotIn(
            "mail_send",
            select_advertised_names(
                "review the auth module",
                sorted(KERNEL_TOOLS) + ["mail_status", "mail_list", "mail_send"],
            ),
        )
        web = select_advertised_names("fetch https://example.com/note", sorted(KERNEL_TOOLS))
        self.assertIn("web_fetch", web)
        self.assertIn("read", web)
        web_follow = select_advertised_names(
            "做一下",
            sorted(KERNEL_TOOLS),
            prior_text="打开网页 https://example.com",
        )
        self.assertIn("web_fetch", web_follow)
        reused_web = select_advertised_names(
            "review the auth module",
            sorted(KERNEL_TOOLS),
            used_names=["web_fetch"],
        )
        self.assertIn("web_fetch", reused_web)
        multi = select_advertised_names(
            "review the auth module and report risks",
            sorted(KERNEL_TOOLS),
        )
        self.assertIn("todo_write", multi)
        self.assertIn("read", multi)
        todo_follow = select_advertised_names(
            "继续",
            sorted(KERNEL_TOOLS),
            prior_text="review the auth module and report risks",
        )
        self.assertIn("todo_write", todo_follow)
        reused_todo = select_advertised_names(
            "review the auth module",
            sorted(KERNEL_TOOLS),
            used_names=["todo_write"],
        )
        self.assertIn("todo_write", reused_todo)
        slides = select_advertised_names("做一份幻灯片", sorted(KERNEL_TOOLS))
        self.assertIn("skill", slides)
        self.assertIn("list_available_skills", slides)
        skill_follow = select_advertised_names(
            "做一下",
            sorted(KERNEL_TOOLS),
            prior_text="做一份幻灯片",
        )
        self.assertIn("skill", skill_follow)
        listed = select_advertised_names("有哪些技能", sorted(KERNEL_TOOLS))
        self.assertIn("list_available_skills", listed)
        self.assertIn("skill", listed)
        mem = select_advertised_names("简短回复偏好是什么", sorted(KERNEL_TOOLS))
        self.assertIn("memory_read", mem)
        self.assertIn("memory_write", mem)
        self.assertIn("memory_status", mem)
        self.assertIn("read", mem)
        mem_follow = select_advertised_names(
            "做一下",
            sorted(KERNEL_TOOLS),
            prior_text="简短回复偏好是什么",
        )
        self.assertIn("memory_read", mem_follow)
        reused_mem = select_advertised_names(
            "review the auth module",
            sorted(KERNEL_TOOLS),
            used_names=["memory_read"],
        )
        self.assertIn("memory_read", reused_mem)
        lattice = select_advertised_names("打开九宫格", sorted(KERNEL_TOOLS))
        self.assertIn("memory_status", lattice)
        self.assertIn("memory_read", lattice)
        remember = select_advertised_names("remember that I prefer short replies", sorted(KERNEL_TOOLS))
        self.assertIn("memory_write", remember)
        fix = select_advertised_names("修正记忆", sorted(KERNEL_TOOLS))
        self.assertIn("memory_write", fix)
        self.assertIn("memory_read", fix)
        wrong = select_advertised_names("那些记忆不对", sorted(KERNEL_TOOLS))
        self.assertIn("memory_write", wrong)
        confirm = select_advertised_names(
            "全部删掉",
            sorted(KERNEL_TOOLS),
            prior_text="那些记忆不对，assets 里两条都删",
        )
        self.assertIn("memory_write", confirm)
        confirm_used = select_advertised_names(
            "全部删掉",
            sorted(KERNEL_TOOLS),
            used_names=["memory_read"],
        )
        self.assertIn("memory_write", confirm_used)
        self.assertNotIn(
            "memory_write",
            select_advertised_names("全部删掉", sorted(KERNEL_TOOLS)),
        )
        empty = {
            "reason": "no_overlap",
            "populated": [{"id": "prefs", "count": 1}],
            "archive": [{"id": "archive/domain", "count": 2}],
        }
        miss = select_advertised_names(
            "旧施工图在哪里？",
            sorted(KERNEL_TOOLS),
            memory_empty=empty,
        )
        self.assertIn("memory_read", miss)
        self.assertNotIn("memory_write", miss)
        self.assertNotIn("memory_status", miss)
        prefs_only = {"reason": "no_overlap", "populated": [{"id": "prefs", "title": "个人偏好", "count": 1}]}
        self.assertNotIn(
            "memory_read",
            select_advertised_names("量子纠缠超导是什么？", sorted(KERNEL_TOOLS), memory_empty=prefs_only),
        )
        self.assertNotIn(
            "memory_read",
            select_advertised_names("旧施工图在哪里？", sorted(KERNEL_TOOLS), memory_empty=prefs_only),
        )
        overlapped = {
            "reason": "no_overlap",
            "archive": [
                {
                    "id": "archive/domain",
                    "title": "归档·domain",
                    "kind": "archive",
                    "overlap": True,
                    "excerpt": "旧施工图在柜里",
                }
            ],
        }
        self.assertIn(
            "memory_read",
            select_advertised_names("施工图在哪里？", sorted(KERNEL_TOOLS), memory_empty=overlapped),
        )
        self.assertNotIn(
            "memory_write",
            select_advertised_names("施工图在哪里？", sorted(KERNEL_TOOLS), memory_empty=overlapped),
        )
        self.assertNotIn(
            "memory_read",
            select_advertised_names("旧施工图在哪里？", sorted(KERNEL_TOOLS)),
        )
        self.assertNotIn(
            "memory_read",
            select_advertised_names(
                "review the auth module",
                sorted(KERNEL_TOOLS),
                memory_empty=empty,
            ),
        )
        self.assertNotIn(
            "memory_read",
            select_advertised_names(
                "note.txt 里写了什么？",
                sorted(KERNEL_TOOLS),
                memory_empty=empty,
            ),
        )
        self.assertNotIn(
            "memory_read",
            select_advertised_names(
                "旧施工图在哪里？",
                sorted(KERNEL_TOOLS),
                memory_empty={"reason": "too_generic", "populated": empty["populated"]},
            ),
        )

    def test_prompt_unlocks_matching_groups(self) -> None:
        spawn = select_advertised_names("delegate review to a subagent", sorted(KERNEL_TOOLS))
        self.assertIn("run_subagent", spawn)
        self.assertIn("run_fanout", spawn)
        sched = select_advertised_names("add job tomorrow", sorted(KERNEL_TOOLS))
        self.assertIn("schedule_write", sched)
        self.assertIn("job_list", sched)
        plan = select_advertised_names("hello", sorted(KERNEL_TOOLS), plan_active=True)
        self.assertIn("exit_plan_mode", plan)
        self.assertIn("plan_read", plan)
        self.assertNotIn("read", plan)
        self.assertNotIn("grep", plan)
        self.assertNotIn("write", plan)
        self.assertNotIn("edit", plan)
        self.assertNotIn("bash", plan)
        self.assertNotIn("run_subagent", plan)
        planning = select_advertised_names("review the auth module", sorted(KERNEL_TOOLS), plan_active=True)
        self.assertIn("read", planning)
        self.assertIn("exit_plan_mode", planning)
        self.assertNotIn("write", planning)
        self.assertNotIn("skill", planning)
        self.assertNotIn("list_available_skills", planning)
        named = select_advertised_names("有哪些技能", sorted(KERNEL_TOOLS), plan_active=True)
        self.assertIn("skill", named)
        self.assertIn("list_available_skills", named)
        reused = select_advertised_names(
            "hello",
            sorted(KERNEL_TOOLS),
            plan_active=True,
            used_names=["write", "bash"],
        )
        self.assertNotIn("write", reused)
        self.assertNotIn("bash", reused)

    def test_used_and_unknown_names_stay(self) -> None:
        names = select_advertised_names(
            "continue",
            ["read", "schedule_write", "mcp-search"],
            used_names=["schedule_write"],
        )
        self.assertIn("schedule_write", names)
        self.assertIn("mcp-search", names)
        self.assertTrue(CORE_TOOLS)

    def test_prior_user_text_keeps_situational_tools(self) -> None:
        follow = select_advertised_names(
            "做一下",
            sorted(KERNEL_TOOLS),
            prior_text="明天加一个定时任务",
        )
        self.assertIn("schedule_write", follow)
        spawn = select_advertised_names(
            "继续",
            sorted(KERNEL_TOOLS),
            prior_text="delegate review to a subagent",
        )
        self.assertIn("run_subagent", spawn)
        bare = select_advertised_names("做一下", sorted(KERNEL_TOOLS), prior_text="你好")
        self.assertNotIn("schedule_write", bare)
        self.assertNotIn("run_fanout", bare)

    def test_cheap_lookup_advertises_lookup_tools_only(self) -> None:
        names = select_advertised_names("read note.txt", sorted(KERNEL_TOOLS))
        self.assertIn("read", names)
        self.assertIn("grep", names)
        self.assertIn("ls", names)
        self.assertNotIn("write", names)
        self.assertNotIn("bash", names)
        light = select_advertised_names("read foo.py then summarize", sorted(KERNEL_TOOLS))
        self.assertIn("read", light)
        self.assertNotIn("write", light)
        self.assertNotIn("run_subagent", light)
        self.assertNotIn("ask_user_question", names)
        self.assertNotIn("run_subagent", names)
        self.assertNotIn("schedule_write", names)
        self.assertNotIn("todo_write", names)
        self.assertNotIn("memory_write", names)
        follow = select_advertised_names(
            "read note.txt",
            sorted(KERNEL_TOOLS),
            prior_text="明天加一个定时任务",
        )
        self.assertIn("read", follow)
        self.assertIn("schedule_write", follow)
        reused = select_advertised_names(
            "read note.txt",
            sorted(KERNEL_TOOLS),
            used_names=["write"],
        )
        self.assertIn("write", reused)
        task = select_advertised_names("review the auth module", sorted(KERNEL_TOOLS))
        self.assertIn("write", task)
        self.assertIn("bash", task)
        self.assertNotIn("memory_read", task)
        self.assertNotIn("memory_write", task)

    def test_recalled_hits_hide_lookup_tools(self) -> None:
        hits = [{"slug": "prefs", "text": "我喜欢简短回复", "scope": "user", "score": 7}]
        covered = select_advertised_names(
            "简短回复偏好是什么？",
            sorted(KERNEL_TOOLS),
            memory_hits=hits,
        )
        self.assertNotIn("read", covered)
        self.assertNotIn("grep", covered)
        self.assertNotIn("ls", covered)
        self.assertNotIn("find", covered)
        self.assertNotIn("write", covered)
        self.assertNotIn("edit", covered)
        self.assertNotIn("bash", covered)
        self.assertIn("memory_read", covered)
        weak = select_advertised_names(
            "简短回复偏好是什么？",
            sorted(KERNEL_TOOLS),
            memory_hits=[{"slug": "prefs", "text": "我喜欢简短回复", "scope": "user", "score": 3}],
        )
        self.assertIn("read", weak)
        self.assertIn("write", weak)
        uncovered = select_advertised_names("简短回复偏好是什么？", sorted(KERNEL_TOOLS))
        self.assertIn("read", uncovered)
        self.assertIn("write", uncovered)
        file_ask = select_advertised_names(
            "what does note.txt contain?",
            sorted(KERNEL_TOOLS),
            memory_hits=hits,
        )
        self.assertIn("read", file_ask)
        reused = select_advertised_names(
            "简短回复偏好是什么？",
            sorted(KERNEL_TOOLS),
            memory_hits=hits,
            used_names=["read", "write"],
        )
        self.assertIn("read", reused)
        self.assertIn("write", reused)

    def test_disabled_returns_all(self) -> None:
        names = select_advertised_names("hi", ["read", "schedule_write"], enabled=False)
        self.assertEqual(names, ["read", "schedule_write"])


class PromptToolSurfaceTests(unittest.TestCase):
    """提示词正文和 schema 公示必须是同一张表：正文不许点名本轮调不到的业务工具。

    schema 那侧早就门控住了（`select_advertised_names`），但**正文没门控**过：
    角色提示词里硬写了 19 个业务工具名，于是每一轮——包括「你好」——都在告诉
    模型 18 个它这轮调不到的工具，把「只公示相关工具」抵掉一半。
    """

    def _business_names(self) -> list[str]:
        # 业务工具名从注册表现取，不在测试里抄一份——插件增删后抄的那份会漂。
        names = [item.name for item in list_tools()]
        biz = [name for name in names if re.match(r"(?:mail|link|diary|pptx|wiki|sql)_", name)]
        biz.append("web_fetch")
        return sorted(set(biz) & set(names))

    def test_prompt_body_names_no_unadvertised_business_tool(self) -> None:
        available = [item.name for item in list_tools()]
        business = self._business_names()
        self.assertGreater(len(business), 20, "注册表里该有一批业务工具，否则这条断言是空的")
        probes = (
            "你好",
            "谢谢",
            "今天天气不错",
            "read src/witty_agent/loop.py",
            "帮我把这季度的情况写成一份报告",
            "看一下今天的邮件，有要紧的挑出来",
            "做一份汇报幻灯片",
            "抓一下 http://192.168.0.10/x 的内容",
            "把这个链接收进去",
            "记一下我今天做了什么，顺便看看邮件",
        )
        for prompt in probes:
            with self.subTest(prompt=prompt):
                advertised = select_advertised_names(prompt, available)
                text = build_system_prompt(
                    ".",
                    tool_names=advertised,
                    skills=[],
                    context_files=[],
                    list_snippets=False,
                    prompt=prompt,
                )
                ghosts = [
                    name
                    for name in business
                    if re.search(rf"(?<![a-z_]){name}(?![a-z_])", text)
                    and name not in advertised
                ]
                self.assertEqual(ghosts, [], f"正文点名了本轮调不到的工具：{ghosts}")

    def test_role_prompt_carries_no_business_tool_name(self) -> None:
        """角色提示词只讲角色。业务能力走工具门控段，这是它自己写下的规矩。"""
        role = get_prompt("harness_system")
        leaked = [name for name in self._business_names() if name in role]
        self.assertEqual(leaked, [], f"角色提示词泄了业务工具名：{leaked}")

    def test_cross_plugin_bridge_needs_both_sides(self) -> None:
        """邮件→日记这句桥只在两边工具都在手上时才注入。"""
        bridge = get_prompt("email_diary_bridge").strip()
        mail_only = build_system_prompt(
            ".",
            tool_names=["mail_list", "mail_read"],
            skills=[],
            context_files=[],
            list_snippets=False,
            prompt="看一下收件箱",
        )
        self.assertNotIn(bridge, mail_only)
        both = build_system_prompt(
            ".",
            tool_names=["mail_list", "mail_read", "diary_write"],
            skills=[],
            context_files=[],
            list_snippets=False,
            prompt="看一下收件箱",
        )
        self.assertIn(bridge, both)

    def test_capability_gate_sets_match_advertised_groups(self) -> None:
        """能力段的门控表和公示分组必须是同一批工具名，否则会公示了工具不给指引。"""
        from witty_agent.system_prompt import _MAIL_TOOLS, _PPTX_TOOLS
        from witty_agent.tool_surface import _GROUPS

        groups = [group for _pattern, group in _GROUPS]
        mail_group = next(g for g in groups if "mail_send" in g)
        pptx_group = next(g for g in groups if "pptx_render" in g)
        self.assertEqual(set(mail_group), set(_MAIL_TOOLS))
        self.assertEqual(set(pptx_group), set(_PPTX_TOOLS))

    def test_web_capability_is_gated_on_web_fetch(self) -> None:
        body = get_prompt("web_capability").strip()
        without = build_system_prompt(
            ".",
            tool_names=["read"],
            skills=[],
            context_files=[],
            list_snippets=False,
            prompt="读一下这个文件",
        )
        self.assertNotIn(body, without)
        with_web = build_system_prompt(
            ".",
            tool_names=["read", "web_fetch"],
            skills=[],
            context_files=[],
            list_snippets=False,
            prompt="抓一下 http://192.168.0.10/x",
        )
        self.assertIn(body, with_web)


class ToolSurfaceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_omits_snippet_table_when_thin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            seen: list[str] = []
            tools: list[list[str]] = []

            async def stream(ctx):
                seen.append(ctx.system_prompt)
                tools.append([getattr(item, "name", "") for item in ctx.tools])
                return text_reply("hi")

            await session.run("say hi", stream_fn=stream, approval_mode="allow-all")
            self.assertTrue(seen)
            text = seen[0]
            self.assertIn(get_prompt("tools_attached"), text)
            self.assertNotIn(get_prompt("skills_idle"), text)
            self.assertNotIn("<available_skills>", text)
            self.assertNotIn("agent-optimization", text)
            self.assertNotIn(get_prompt("guideline_dispatch"), text)
            self.assertNotIn(get_prompt("guideline_cite"), text)
            self.assertNotIn(get_prompt("tool_snippet_read"), text)
            self.assertNotIn("schedule_write", text)
            self.assertNotIn("run_fanout", text)
            self.assertTrue(tools)
            shown = set(tools[0])
            self.assertNotIn("write", shown)
            self.assertNotIn("bash", shown)
            self.assertNotIn("run_subagent", shown)
            self.assertNotIn("todo_write", shown)

    async def test_session_cheap_lookup_hides_spawn_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            seen: list[list[str]] = []

            async def stream(ctx):
                seen.append([getattr(item, "name", "") for item in ctx.tools])
                return text_reply("ok")

            await session.run("read note.txt", stream_fn=stream, approval_mode="allow-all")
            self.assertTrue(seen)
            names = set(seen[0])
            self.assertIn("read", names)
            self.assertNotIn("run_subagent", names)
            self.assertNotIn("run_fanout", names)
            self.assertNotIn("schedule_write", names)
            self.assertNotIn("write", names)

    async def test_session_recalled_miss_skips_unrelated_memory_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 我喜欢简短回复",
            )
            seen: list[set[str]] = []

            async def stream(ctx):
                seen.append({getattr(item, "name", "") for item in ctx.tools})
                return text_reply("未核实")

            await session.run("旧施工图在哪里？", stream_fn=stream, approval_mode="allow-all")
            self.assertTrue(seen)
            self.assertNotIn("memory_read", seen[0])
            self.assertNotIn("memory_write", seen[0])
            review: list[set[str]] = []

            async def review_stream(ctx):
                review.append({getattr(item, "name", "") for item in ctx.tools})
                return text_reply("ok")

            await session.run("review the auth module", stream_fn=review_stream, approval_mode="allow-all")
            self.assertTrue(review)
            self.assertNotIn("memory_read", review[0])
            named: list[set[str]] = []

            async def named_stream(ctx):
                named.append({getattr(item, "name", "") for item in ctx.tools})
                return text_reply("简短")

            await session.run("简短回复偏好是什么", stream_fn=named_stream, approval_mode="allow-all")
            self.assertTrue(named)
            self.assertIn("memory_read", named[0])
            self.assertNotIn("read", named[0])
            self.assertNotIn("grep", named[0])
            self.assertNotIn("write", named[0])
            self.assertNotIn("bash", named[0])

    async def test_session_recalled_hit_hides_lookup_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 我喜欢简短回复",
            )
            seen: list[set[str]] = []

            async def stream(ctx):
                seen.append({getattr(item, "name", "") for item in ctx.tools})
                return text_reply("简短")

            await session.run("简短回复偏好是什么？", stream_fn=stream, approval_mode="allow-all")
            self.assertTrue(seen)
            self.assertNotIn("read", seen[0])
            self.assertNotIn("grep", seen[0])
            self.assertNotIn("ls", seen[0])
            self.assertNotIn("write", seen[0])
            self.assertNotIn("bash", seen[0])
            self.assertIn("memory_read", seen[0])
