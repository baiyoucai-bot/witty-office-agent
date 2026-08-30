from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from witty_agent.evolution.improve import run_scoring_loop, score_summary
from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.http_api import configure_api, handle_request
from witty_agent.guard import needs_memory_browse
from witty_agent.memory import (
    SessionMemory,
    _should_skip,
    append_unique_bullets,
    attach_retrieval,
    extra_topics,
    format_hit_list,
    hit_is_archive,
    hits_have_scopes,
    hits_layer,
    order_hits_working_first,
    public_memory,
    read_topic,
    resolve_session_memory,
    retrieve_for_query,
    retrieve_hits,
    topic_body,
    write_topic,
)
from witty_agent.system_prompt import (
    format_memory_section,
    format_recalled_text,
    is_placeholder_profile,
)
from witty_agent.memory_config import load_memory_settings
from witty_agent.prompts import get_prompt
from witty_agent.memory_graph import add_cooccurrence_links
from witty_agent.memory_harvest import (
    _worth_keeping,
    harvest_assistant_notes,
    harvest_tool_facts,
    harvest_user_text,
    scrub_transient_domain,
)
from witty_agent.session import create_agent, create_session
from witty_agent.system_prompt import build_system_prompt
from witty_agent.timeline import extract_dated_events, harvest_timeline


def _keep_domain(lines, _text, _settings):
    return [("domain", line) for line in lines]


def _drop_all(lines, _text, _settings):
    return []


class MemoryEvolveTests(unittest.IsolatedAsyncioTestCase):
    def test_user_and_workspace_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="default_project",
                agent_id="default_agent",
                workspace=workspace,
                root=root,
            )
            write_topic(memory.user_dir, "prefers-brief", description="user likes brief answers", body="Be short.")
            again = resolve_session_memory(
                project_id="default_project",
                agent_id="default_agent",
                workspace=workspace,
                root=root,
            )
            self.assertIn("prefers-brief", again.user_index)
            prompt = build_system_prompt(workspace, tool_names=["read", "memory_write"], memory=again)
            self.assertIn("用户记忆目录", prompt)
            self.assertIn("工作区记忆目录", prompt)
            self.assertIn("本轮召回", prompt)
            self.assertNotIn("## 用户画像", prompt)
            self.assertNotIn("尚未记录", prompt)
            self.assertNotIn("Nine-cell lattice", prompt)
            self.assertNotIn("prefers-brief", prompt)
            self.assertIn("prefers-brief", again.user_index)

    def test_workspace_notes_are_recalled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.workspace_dir,
                "auth-notes",
                description="this repo auth module",
                body="- 本仓认证走 OAuth2 授权码，回调在 /callback",
            )
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 我喜欢简短回复",
            )
            attached = attach_retrieval(memory, "OAuth2 授权码回调")
            recalled = attached.retrieved
            self.assertIn("OAuth2", recalled)
            self.assertIn("工作区", recalled)
            self.assertIn("auth-notes", recalled)
            slugs = [str(item.get("slug")) for item in attached.hits]
            self.assertIn("auth-notes", slugs)
            self.assertNotIn("简短", recalled)
            brief = attach_retrieval(memory, "简短回复偏好").retrieved
            self.assertIn("简短", brief)
            self.assertNotIn("OAuth2", brief)

    def test_tool_facts_land_in_workspace_not_user_prefs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            report = harvest_tool_facts(
                memory.workspace_dir,
                [
                    {
                        "kind": "tool",
                        "source": "read",
                        "locator": "auth.py",
                        "excerpt": "本仓认证走 OAuth2 授权码，回调在 /callback",
                        "ok": True,
                    },
                    {
                        "kind": "tool",
                        "source": "write",
                        "locator": "out.py",
                        "excerpt": "should not harvest mutations",
                        "ok": True,
                    },
                    {
                        "kind": "memory",
                        "source": "memory_read",
                        "locator": "prefs",
                        "excerpt": "我喜欢简短回复",
                        "ok": True,
                    },
                ],
            )
            self.assertTrue(report["skipped"])
            self.assertEqual(int(report["added"]), 0)
            self.assertEqual(topic_body(memory.workspace_dir, "auth-py").strip(), "")
            prefs = topic_body(memory.user_dir, "prefs")
            self.assertNotIn("OAuth2", prefs)

    def test_skip_needles_ignore_compound_words(self) -> None:
        settings = load_memory_settings()
        self.assertFalse(_should_skip("alpha-source-token in note", settings))
        self.assertFalse(_should_skip("use the tokenizer here", settings))
        self.assertTrue(_should_skip("token=abc123", settings))
        self.assertTrue(_should_skip("password is hunter2", settings))
        self.assertTrue(_should_skip("sk-live-abcdef", settings))
        self.assertTrue(_should_skip("这里写了密钥和口令", settings))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            kept = harvest_tool_facts(
                memory.workspace_dir,
                [
                    {
                        "kind": "tool",
                        "source": "read",
                        "locator": "note.txt",
                        "excerpt": "alpha-source-token marks the sample",
                        "ok": True,
                    }
                ],
            )
            self.assertEqual(int(kept["added"]), 0)
            self.assertNotIn("alpha-source-token", topic_body(memory.workspace_dir, "note-txt"))
            missed = harvest_tool_facts(
                memory.workspace_dir,
                [
                    {
                        "kind": "tool",
                        "source": "grep",
                        "locator": "zzz-no-such",
                        "excerpt": "(no matches)",
                        "ok": True,
                    }
                ],
            )
            self.assertEqual(int(missed["added"]), 0)
            blocked = harvest_tool_facts(
                memory.workspace_dir,
                [
                    {
                        "kind": "tool",
                        "source": "read",
                        "locator": "secrets.env",
                        "excerpt": "token=abc123",
                        "ok": True,
                    }
                ],
            )
            self.assertEqual(int(blocked["added"]), 0)

    async def test_session_harvests_read_into_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "note.txt").write_text("alpha-source-line\n", encoding="utf-8")
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            await session.run(
                "what is in note.txt",
                stream_fn=ScriptedLLM(
                    [
                        tool_reply("read", {"path": "note.txt"}, call_id="r1"),
                        text_reply("the note says alpha-source-line"),
                    ]
                ),
                approval_mode="allow-all",
            )
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            body = topic_body(memory.workspace_dir, "note-txt")
            self.assertNotIn("alpha-source-line", body)
            recalled = attach_retrieval(memory, "alpha-source-line").retrieved
            self.assertNotIn("alpha-source-line", recalled)

    def test_assistant_decisions_stay_off_user_prefs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            liked = harvest_assistant_notes(memory.workspace_dir, "我喜欢简短回复，默认用列表。")
            self.assertEqual(int(liked["added"]), 0)
            self.assertNotIn("简短", topic_body(memory.user_dir, "prefs"))
            report = harvest_assistant_notes(
                memory.workspace_dir,
                "已决定采用 OAuth2 授权码。下次跟进回调路径。",
            )
            self.assertGreaterEqual(int(report["added"]), 1)
            self.assertIn("decisions", report["cells"])
            self.assertIn("OAuth2", topic_body(memory.workspace_dir, "decisions"))
            self.assertIn("助手记录", topic_body(memory.workspace_dir, "decisions"))
            self.assertNotIn("OAuth2", topic_body(memory.user_dir, "prefs"))
            self.assertNotIn("OAuth2", topic_body(memory.user_dir, "decisions"))
            recalled = attach_retrieval(memory, "OAuth2 授权码").retrieved
            self.assertIn("OAuth2", recalled)

    async def test_session_harvests_assistant_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace)
            await session.run(
                "就这么办",
                stream_fn=ScriptedLLM([text_reply("已决定采用 OAuth2 授权码。")]),
                approval_mode="allow-all",
            )
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            self.assertIn("OAuth2", topic_body(memory.workspace_dir, "decisions"))
            self.assertNotIn("OAuth2", topic_body(memory.user_dir, "prefs"))

    def test_nine_grid_harvest_and_taxonomy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            liked = harvest_user_text(memory.user_dir, "我爱吃冰淇淋")
            self.assertIn("prefs", liked["cells"])
            report = harvest_user_text(
                memory.user_dir,
                "我喜欢简短回复。这次农配网台区改造要先看施工图，下次记得跟进竣工资料。",
            )
            self.assertGreaterEqual(int(report["added"]), 1)
            self.assertIn("prefs", report["cells"])
            self.assertIn("rural-distribution", report["taxonomy"])
            snapshot = public_memory(memory.user_dir)
            self.assertEqual(len(snapshot["cells"]), 9)
            prefs = next(item for item in snapshot["cells"] if item["id"] == "prefs")
            self.assertIn("简短", prefs["body"])
            self.assertTrue(any(item["id"] == "rural-distribution" for item in snapshot["taxonomy"]))
            self.assertIn("对话轮次", snapshot["profile"])
            domain_before = next(item for item in snapshot["cells"] if item["id"] == "domain")
            self.assertNotIn("农配网", domain_before["body"])
            leftover = harvest_user_text(
                memory.user_dir,
                "这个工程资料明天要交甲方，里面有隐蔽验收记录。",
                judge_fn=_keep_domain,
            )
            self.assertIn("domain", leftover["cells"])
            domain = next(item for item in public_memory(memory.user_dir)["cells"] if item["id"] == "domain")
            self.assertIn("工程资料", domain["body"])
            snapshot = public_memory(memory.user_dir)
            self.assertTrue(snapshot["links"])
            recalled = attach_retrieval(memory, "农配网台区").retrieved
            self.assertTrue(recalled)
            self.assertIn("农配网", recalled)

    def test_retrieve_matching_bullets_not_neighbor_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "domain",
                description="领域要点",
                lines=["农配网台区改造先看施工图", "甲方要英文周报每周一交"],
            )
            append_unique_bullets(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                lines=["我喜欢简短回复"],
            )
            add_cooccurrence_links(memory.user_dir, ["domain", "prefs"], reason="same-turn")
            recalled = retrieve_for_query(memory.user_dir, "农配网台区施工图")
            self.assertIn("施工图", recalled)
            self.assertNotIn("英文周报", recalled)
            self.assertNotIn("简短", recalled)
            empty = retrieve_for_query(memory.user_dir, "这个 需要 一下")
            self.assertEqual(empty, "")
            weak = retrieve_for_query(memory.user_dir, "回复")
            self.assertNotIn("简短", weak)
            brief = retrieve_for_query(memory.user_dir, "简短回复")
            self.assertIn("简短", brief)

    def test_public_memory_empty_state_browse_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                lines=["我喜欢简短回复"],
            )
            append_unique_bullets(
                memory.user_dir / "archive",
                "domain",
                description="archived domain",
                lines=["2025-01-01 旧施工图在柜里"],
            )
            generic = public_memory(memory.user_dir, query="这个 需要 一下")
            self.assertEqual(generic.get("hits") or [], [])
            empty = generic["empty"]
            self.assertEqual(empty["reason"], "too_generic")
            self.assertEqual(list(empty["tokens"] or []), [])
            self.assertTrue(any(item["id"] == "prefs" and int(item["count"]) >= 1 for item in empty["populated"]))
            self.assertGreaterEqual(int(empty["archive_count"]), 1)
            self.assertTrue(any(item.get("id") == "archive/domain" for item in empty.get("archive") or []))
            miss = public_memory(memory.user_dir, query="量子纠缠超导")
            self.assertEqual(miss["empty"]["reason"], "no_overlap")
            self.assertTrue(miss["empty"]["tokens"])
            self.assertTrue(any(item["id"] == "prefs" for item in miss["empty"]["populated"]))
            self.assertFalse(
                any(item.get("overlap") for item in miss["empty"].get("archive") or [] if item.get("id") == "archive/domain")
            )
            drawing = public_memory(memory.user_dir, query="施工图在哪里")
            self.assertEqual(drawing["empty"]["reason"], "no_overlap")
            arch = next(item for item in drawing["empty"].get("archive") or [] if item.get("id") == "archive/domain")
            self.assertTrue(arch.get("overlap"))
            self.assertIn("施工图", str(arch.get("excerpt") or ""))
            hit = public_memory(memory.user_dir, query="简短回复")
            self.assertTrue(hit.get("hits"), hit)
            self.assertEqual(hit["empty"]["reason"], "")
            idle = public_memory(memory.user_dir)
            self.assertEqual(idle["empty"]["reason"], "")
            self.assertTrue(any(item["id"] == "prefs" for item in idle["empty"]["populated"]))
            write_topic(
                memory.workspace_dir,
                "decisions",
                description="已做决定",
                body="- 助手记录：已决定采用 OAuth2 授权码",
            )
            from witty_agent.memory import attach_workspace_public

            branded = attach_workspace_public(
                public_memory(memory.user_dir, query="OAuth2"),
                memory.workspace_dir,
                query="OAuth2",
            )
            self.assertTrue(
                any("OAuth2" in str(item.get("text") or "") for item in branded.get("hits") or []),
                branded,
            )
            self.assertEqual(branded["empty"]["reason"], "")

    def test_session_recalled_empty_lists_populated_slugs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                lines=["我喜欢简短回复"],
            )
            miss = attach_retrieval(memory, "量子纠缠超导")
            self.assertEqual(miss.retrieved, "")
            self.assertFalse(miss.hits)
            self.assertEqual(miss.empty.get("reason"), "no_overlap")
            self.assertTrue(any(item.get("id") == "prefs" for item in miss.empty.get("populated") or []))
            hint = format_recalled_text(miss)
            self.assertIn("prefs", hint)
            self.assertNotIn("简短回复", hint)
            section = format_memory_section(miss)
            self.assertIn("prefs (1)", section)
            self.assertIn("没有重叠笔记", section)
            self.assertNotIn("Nine-cell lattice", section)
            self.assertNotIn("| --- |", section)
            self.assertNotIn("# 九宫格记忆", section)
            generic = attach_retrieval(memory, "这个 需要 一下")
            self.assertEqual(generic.empty.get("reason"), "too_generic")
            self.assertEqual(format_recalled_text(generic), "")
            hit = attach_retrieval(memory, "简短回复")
            self.assertIn("简短", hit.retrieved)
            self.assertEqual(hit.empty.get("reason") or "", "")
            self.assertIn("简短", format_recalled_text(hit))
            self.assertLessEqual(max((len(line) for line in format_recalled_text(hit).splitlines()), default=0), 160)

    def test_placeholder_profile_stays_out_of_system_prompt(self) -> None:
        self.assertTrue(is_placeholder_profile(get_prompt("memory_profile_body", turns="0", who="尚未记录", prefs="尚未记录", assets="尚未记录", followups="无")))
        self.assertFalse(is_placeholder_profile("- 偏好：我喜欢简短回复"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(memory.user_dir, "prefs", description="个人偏好", lines=["我喜欢简短回复"])
            from witty_agent.memory import write_profile

            write_profile(memory.user_dir, turns=1)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            section = format_memory_section(memory)
            self.assertIn("## 用户画像", section)
            self.assertIn("简短回复", section)
            self.assertFalse(is_placeholder_profile(memory.profile))

    def test_tool_name_does_not_recall_unrelated_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.workspace_dir,
                "auth-py",
                description="auth.py",
                body="- read auth.py: 本仓认证走 OAuth2 授权码，回调在 /callback",
            )
            write_topic(
                memory.workspace_dir,
                "note-txt",
                description="note.txt",
                body="- read note.txt: alpha-source-line marks the sample",
            )
            by_tool = attach_retrieval(memory, "please read the file")
            self.assertNotIn("OAuth2", by_tool.retrieved)
            self.assertNotIn("alpha-source-line", by_tool.retrieved)
            by_locator = attach_retrieval(memory, "auth.py 里写了什么")
            self.assertIn("OAuth2", by_locator.retrieved)
            self.assertNotIn("alpha-source-line", by_locator.retrieved)
            by_excerpt = attach_retrieval(memory, "OAuth2 授权码回调")
            self.assertIn("OAuth2", by_excerpt.retrieved)
            self.assertNotIn("alpha-source-line", by_excerpt.retrieved)

    def test_taxonomy_bonus_requires_bullet_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "engineering-project",
                description="工程项目",
                lines=["农配网台区改造先看施工图", "甲方要英文周报每周一交"],
            )
            recalled = retrieve_for_query(memory.user_dir, "施工图在哪")
            self.assertIn("施工图", recalled)
            self.assertNotIn("英文周报", recalled)

    def test_leftover_domain_does_not_link_prefs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            report = harvest_user_text(
                memory.user_dir,
                "我喜欢简短回复。这个工程资料明天要交甲方里面有隐蔽验收记录。",
                judge_fn=_keep_domain,
            )
            self.assertIn("prefs", report["cells"])
            self.assertIn("domain", report["cells"])
            pairs = {(item["from"], item["to"]) for item in public_memory(memory.user_dir)["links"]}
            self.assertNotIn(("domain", "prefs"), pairs)
            self.assertNotIn(("prefs", "domain"), pairs)

    def test_dated_cjk_fact_survives_date_strip(self) -> None:
        self.assertTrue(_worth_keeping("2025-01-01 旧施工图在柜里"))
        self.assertTrue(_worth_keeping("旧施工图在柜里"))
        self.assertFalse(_worth_keeping("2025-01-01 你好"))
        self.assertFalse(_worth_keeping("ok note"))
        self.assertFalse(_worth_keeping("施工图在哪里"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "domain",
                description="领域要点",
                lines=["2025-01-01 旧施工图在柜里"],
            )
            append_unique_bullets(
                memory.user_dir / "archive",
                "domain",
                description="archived domain",
                lines=["2024-12-01 旧验收单在柜里"],
            )
            self.assertEqual(scrub_transient_domain(memory.user_dir), 0)
            self.assertIn("旧施工图", topic_body(memory.user_dir, "domain"))
            self.assertIn("旧验收单", topic_body(memory.user_dir / "archive", "domain"))

    def test_task_prompts_are_not_domain_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            for prompt in (
                "refactor the auth module",
                "write hello.py",
                "review the auth module and report risks",
                "重构认证模块并补测试",
                "note.txt 里写了什么？",
            ):
                report = harvest_user_text(memory.user_dir, prompt)
                self.assertNotIn("domain", report.get("cells") or [])
            self.assertNotIn("refactor", topic_body(memory.user_dir, "domain"))
            self.assertNotIn("hello.py", topic_body(memory.user_dir, "domain"))
            fact = harvest_user_text(
                memory.user_dir,
                "这个工程资料明天要交甲方，里面有隐蔽验收记录。",
                judge_fn=_keep_domain,
            )
            self.assertIn("domain", fact["cells"])
            self.assertIn("工程资料", topic_body(memory.user_dir, "domain"))

    def test_capability_questions_are_not_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            for prompt in (
                "你现在能做ppt吗",
                "你有loop功能吗",
                "你是不会跟我申请权限啊",
                "你有自己的沙箱环境吗",
                "你现在有沙箱环境了吗",
                "主图，某集团人工智能大会，剩下的你自己定，开始做吧",
            ):
                report = harvest_user_text(memory.user_dir, prompt)
                self.assertNotIn("domain", report.get("cells") or [], prompt)
            body = topic_body(memory.user_dir, "domain")
            self.assertNotIn("ppt", body)
            self.assertNotIn("loop", body)
            self.assertNotIn("沙箱", body)
            self.assertNotIn("开始做吧", body)
            planted = [
                "2026-08-18 你现在有沙箱环境了吗",
                "2026-08-17 这个工程资料明天要交甲方，里面有隐蔽验收记录",
            ]
            append_unique_bullets(memory.user_dir, "domain", description="领域要点", lines=planted)
            from witty_agent.memory_harvest import scrub_transient_domain

            dropped = scrub_transient_domain(memory.user_dir)
            self.assertGreaterEqual(dropped, 1)
            cleaned = topic_body(memory.user_dir, "domain")
            self.assertNotIn("沙箱环境", cleaned)
            self.assertIn("工程资料", cleaned)
            self.assertNotIn("沙箱环境", topic_body(memory.user_dir / "archive", "domain"))
            append_unique_bullets(
                memory.user_dir / "archive",
                "domain",
                description="archived domain",
                lines=["2026-08-18 你有loop功能吗"],
            )
            extra = scrub_transient_domain(memory.user_dir)
            self.assertGreaterEqual(extra, 1)
            self.assertNotIn("loop", topic_body(memory.user_dir / "archive", "domain"))

    def test_leftover_follows_model_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            skipped = harvest_user_text(
                memory.user_dir,
                "这个工程资料明天要交甲方，里面有隐蔽验收记录。",
                judge_fn=_drop_all,
            )
            self.assertNotIn("domain", skipped.get("cells") or [])
            kept = harvest_user_text(
                memory.user_dir,
                "这个工程资料明天要交甲方，里面有隐蔽验收记录。",
                judge_fn=_keep_domain,
            )
            self.assertIn("domain", kept["cells"])
            self.assertIn("工程资料", topic_body(memory.user_dir, "domain"))
            from witty_agent.memory_harvest import _parse_judge

            parsed = _parse_judge(
                '[{"cell":"domain","text":"电压等级 10kV"}]',
                {"domain", "prefs"},
            )
            self.assertEqual(parsed, [("domain", "电压等级 10kV")])
            self.assertEqual(_parse_judge("[]", {"domain"}), [])
            self.assertEqual(_parse_judge('[{"cell":"nope","text":"x"}]', {"domain"}), [])

    def test_leftover_judge_cannot_keep_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )

            def keep_all(lines, _text, _settings):
                return [("domain", line) for line in lines]

            report = harvest_user_text(
                memory.user_dir,
                "你现在能做ppt吗？电压等级是 10kV。农配网项目是什么？",
                judge_fn=keep_all,
            )
            self.assertIn("domain", report.get("cells") or [])
            body = topic_body(memory.user_dir, "domain")
            self.assertIn("10kV", body)
            self.assertNotIn("ppt", body)
            self.assertNotIn("是什么", body)
            self.assertNotIn("农配网项目是什么", topic_body(memory.user_dir, "rural-distribution"))

    def test_leftover_without_judge_does_not_dump_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            settings = replace(load_memory_settings(), judge_leftover=False)
            dumped = harvest_user_text(
                memory.user_dir,
                "这个工程资料明天要交甲方，里面有隐蔽验收记录。",
                settings=settings,
            )
            self.assertNotIn("domain", dumped.get("cells") or [])
            self.assertNotIn("隐蔽验收", topic_body(memory.user_dir, "domain"))
            seen: list[list[str]] = []

            def see_only_facts(lines, _text, _settings):
                seen.append(list(lines))
                return [("domain", line) for line in lines]

            harvest_user_text(
                memory.user_dir,
                "你现在能做ppt吗？电压等级是 10kV。",
                judge_fn=see_only_facts,
                settings=settings,
            )
            self.assertTrue(seen)
            self.assertTrue(all("ppt" not in line and "吗" not in line for line in seen[0]))
            self.assertIn("10kV", topic_body(memory.user_dir, "domain"))

    def test_pref_same_slot_replaces_and_silence_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            harvest_user_text(memory.user_dir, "我爱吃桃子。我喜欢简短回复。")
            harvest_user_text(memory.user_dir, "我喜欢吃苹果。")
            prefs = topic_body(memory.user_dir, "prefs")
            self.assertIn("苹果", prefs)
            self.assertNotIn("桃子", prefs)
            self.assertIn("简短", prefs)
            archived = topic_body(memory.user_dir / "archive", "prefs")
            self.assertIn("桃子", archived)
            peach = retrieve_hits(memory.user_dir, "喜欢吃桃子")
            self.assertFalse(any("桃子" in str(item.get("text") or "") for item in peach), peach)
            harvest_user_text(
                memory.user_dir,
                "这个工程资料明天要交甲方，里面有隐蔽验收记录。",
                judge_fn=_keep_domain,
            )
            still = topic_body(memory.user_dir, "prefs")
            self.assertIn("苹果", still)
            self.assertIn("简短", still)

    def test_pref_retract_archives_without_needing_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            harvest_user_text(memory.user_dir, "我爱吃桃子。")
            harvest_user_text(memory.user_dir, "不吃桃子了。")
            self.assertNotIn("桃子", topic_body(memory.user_dir, "prefs"))
            self.assertIn("桃子", topic_body(memory.user_dir / "archive", "prefs"))

    def test_process_sentences_are_not_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            report = harvest_user_text(
                memory.user_dir,
                "我先用 grep 查了农配网台区。刚才看了施工图。",
            )
            self.assertNotIn("domain", report.get("cells") or [])
            self.assertNotIn("rural-distribution", report.get("taxonomy") or [])
            self.assertNotIn("grep", topic_body(memory.user_dir, "domain"))
            self.assertNotIn("施工图", topic_body(memory.user_dir, "rural-distribution"))

    def test_working_set_archives_old_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            lines = [f"事实{index:02d} 这是一条足够长的领域说明" for index in range(16)]
            append_unique_bullets(memory.user_dir, "domain", description="领域要点", lines=lines)
            live = topic_body(memory.user_dir, "domain")
            archived = topic_body(memory.user_dir / "archive", "domain")
            live_n = len([line for line in live.splitlines() if line.startswith("- ")])
            self.assertEqual(live_n, 12)
            self.assertIn("事实00", archived)
            self.assertNotIn("事实00", live)
            self.assertIn("事实15", live)
            spilled = retrieve_hits(memory.user_dir, "事实00 领域说明")
            self.assertFalse(
                any("事实00" in str(item.get("text") or "") for item in spilled),
                spilled,
            )
            self.assertFalse(any(str(item.get("slug") or "").startswith("archive-") for item in spilled))
            live_hits = retrieve_hits(memory.user_dir, "事实15 领域说明")
            self.assertTrue(any("事实15" in str(item.get("text") or "") for item in live_hits), live_hits)
            archived = read_topic(memory.user_dir, "archive/domain")
            self.assertIn("事实00", archived)
            public = public_memory(memory.user_dir)
            self.assertTrue(any(item.get("id") == "archive/domain" for item in public.get("archive") or []))

    def test_archive_slug_readable_when_recalled_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                lines=["我喜欢简短回复"],
            )
            append_unique_bullets(
                memory.user_dir / "archive",
                "domain",
                description="archived domain",
                lines=["旧施工图在柜里"],
            )
            self.assertIn("旧施工图", read_topic(memory.user_dir, "archive/domain"))
            miss = attach_retrieval(memory, "旧施工图在柜里")
            self.assertTrue(any("旧施工图" in str(item.get("text") or "") for item in miss.hits), miss.hits)
            self.assertTrue(
                any(item.get("slug") == "archive/domain" and item.get("layer") == "archive" for item in miss.hits),
                miss.hits,
            )
            hint = format_recalled_text(miss)
            self.assertIn("archive/domain", hint)
            self.assertIn("旧施工图", hint)
            closed = retrieve_hits(
                memory.user_dir,
                "旧施工图在柜里",
                replace(load_memory_settings(), retrieve_archive=False),
            )
            self.assertFalse(closed)

    def test_archive_recall_needs_higher_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir / "archive",
                "prefs",
                description="archived prefs",
                lines=["喜欢吃桃子"],
            )
            weak = retrieve_hits(memory.user_dir, "喜欢吃西瓜")
            self.assertFalse(weak)
            browse = public_memory(memory.user_dir, query="喜欢吃西瓜")
            arch = next(
                item
                for item in (browse.get("empty") or {}).get("archive") or []
                if item.get("id") == "archive/prefs"
            )
            self.assertTrue(arch.get("overlap"), arch)
            strong = retrieve_hits(memory.user_dir, "喜欢吃桃子")
            self.assertTrue(
                any(item.get("slug") == "archive/prefs" and item.get("layer") == "archive" for item in strong),
                strong,
            )
            write_topic(memory.user_dir, "prefs", description="个人偏好", body="- 喜欢吃桃子\n")
            working = retrieve_hits(memory.user_dir, "喜欢吃西瓜")
            self.assertTrue(
                any(item.get("slug") == "prefs" and item.get("layer") == "working" for item in working),
                working,
            )

    def test_mixed_recalled_marks_archive_layer(self) -> None:
        mixed = [
            {
                "slug": "prefs",
                "title": "个人偏好",
                "text": "我喜欢简短回复",
                "score": 7,
                "layer": "working",
            },
            {
                "slug": "archive/prefs",
                "title": "个人偏好",
                "text": "喜欢吃桃子",
                "score": 6,
                "layer": "archive",
            },
        ]
        self.assertEqual(hits_layer(mixed), "mixed")
        banner = get_prompt("recalled_layer_mixed")
        listed = format_hit_list(mixed, excerpt_limit=80)
        self.assertTrue(listed.startswith(banner), listed)
        self.assertIn("归档·个人偏好", listed)
        self.assertIn("`prefs`", listed)
        self.assertIn("`archive/prefs`", listed)
        self.assertNotIn(banner, format_hit_list(mixed[:1], excerpt_limit=80))
        self.assertNotIn(banner, format_hit_list(mixed[1:], excerpt_limit=80))
        recalled = format_recalled_text(SessionMemory(user_dir=Path("."), user_index="", hits=tuple(mixed)))
        self.assertTrue(recalled.startswith(banner), recalled)
        self.assertLessEqual(max((len(line) for line in recalled.splitlines()), default=0), 160)
        reversed_hits = list(reversed(mixed))
        listed_rev = format_hit_list(reversed_hits, excerpt_limit=80)
        body = listed_rev.splitlines()
        self.assertTrue(body[0] == banner, listed_rev)
        self.assertIn("`prefs`", body[1])
        self.assertIn("`archive/prefs`", body[2])
        self.assertLess(listed_rev.find("`prefs`"), listed_rev.find("`archive/prefs`"))

    def test_user_hits_outrank_workspace_notes(self) -> None:
        mixed = [
            {
                "slug": "decisions",
                "title": "已做决定",
                "text": "本目录用空格缩进",
                "score": 8,
                "scope": "workspace",
                "layer": "working",
            },
            {
                "slug": "prefs",
                "title": "个人偏好",
                "text": "我喜欢 tab 缩进",
                "score": 6,
                "scope": "user",
                "layer": "working",
            },
        ]
        self.assertTrue(hits_have_scopes(mixed))
        ordered = order_hits_working_first(mixed)
        self.assertEqual([str(item.get("slug")) for item in ordered], ["prefs", "decisions"])
        banner = get_prompt("recalled_scope_mixed")
        listed = format_hit_list(mixed, excerpt_limit=80)
        self.assertTrue(listed.startswith(banner), listed)
        self.assertLess(listed.find("`prefs`"), listed.find("`decisions`"))
        self.assertIn("工作区·已做决定", listed)
        self.assertNotIn(banner, format_hit_list(mixed[:1], excerpt_limit=80))
        self.assertNotIn(get_prompt("recalled_layer_mixed"), listed)

    def test_working_hits_keep_archive_as_browse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir,
                "domain",
                description="领域要点",
                lines=["新施工图在桌上"],
            )
            append_unique_bullets(
                memory.user_dir / "archive",
                "domain",
                description="archived domain",
                lines=["旧施工图在柜里"],
            )
            attached = attach_retrieval(memory, "施工图")
            slugs = [str(item.get("slug") or "") for item in attached.hits]
            self.assertIn("domain", slugs)
            self.assertFalse(any(slug.startswith("archive/") for slug in slugs), slugs)
            archive_rows = attached.empty.get("archive") or []
            self.assertTrue(
                any(
                    item.get("id") == "archive/domain" and item.get("overlap")
                    for item in archive_rows
                ),
                attached.empty,
            )
            hint = format_recalled_text(attached)
            self.assertIn("`domain`", hint)
            self.assertIn(get_prompt("recalled_archive_browse", slugs="archive/domain"), hint)
            self.assertNotIn("`archive/domain`", hint.splitlines()[0] if hint else "")
            self.assertFalse(any(hit_is_archive(item) for item in attached.hits))
            self.assertNotEqual(hits_layer(attached.hits), "mixed")
            self.assertFalse(needs_memory_browse("施工图在哪", attached.empty))

    def test_mixed_recalled_working_hits_come_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            append_unique_bullets(
                memory.user_dir / "archive",
                "prefs",
                description="archived prefs",
                lines=["喜欢吃桃子"],
            )
            append_unique_bullets(
                memory.workspace_dir,
                "decisions",
                description="已做决定",
                lines=["喜欢吃桃子的方案已定"],
            )
            attached = attach_retrieval(memory, "喜欢吃桃子")
            slugs = [str(item.get("slug") or "") for item in attached.hits]
            self.assertTrue(any(slug.startswith("archive/") for slug in slugs), attached.hits)
            self.assertTrue(any(not slug.startswith("archive/") for slug in slugs), attached.hits)
            self.assertFalse(slugs[0].startswith("archive/"), slugs)
            self.assertLess(attached.retrieved.find("`decisions`"), attached.retrieved.find("`archive/prefs`"))

    def test_stale_dated_bullets_lose_to_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "domain",
                description="领域要点",
                body=(
                    "- 2024-01-01 农配网台区施工图在旧资料包\n"
                    "- 2026-08-16 农配网台区施工图在新资料包"
                ),
            )
            hits = retrieve_hits(
                memory.user_dir,
                "农配网台区施工图",
                today=date(2026, 8, 17),
            )
            texts = [str(item.get("text") or "") for item in hits]
            self.assertTrue(any("新资料包" in text for text in texts), texts)
            self.assertFalse(any("旧资料包" in text for text in texts), texts)
            write_topic(
                memory.user_dir,
                "prefs",
                description="个人偏好",
                body="- 2024-01-01 我喜欢简短回复",
            )
            prefs = retrieve_hits(
                memory.user_dir,
                "简短回复",
                today=date(2026, 8, 17),
            )
            self.assertTrue(any("简短回复" in str(item.get("text") or "") for item in prefs))

    def test_timeline_extracts_and_sorts(self) -> None:
        from datetime import date

        events = extract_dated_events(
            "2024年3月1日开工。昨天验收。2025-12-20 投产。",
            today=date(2026, 8, 14),
        )
        days = [item[0] for item in events]
        self.assertIn("2024-03-01", days)
        self.assertIn("2026-08-13", days)
        self.assertIn("2025-12-20", days)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harvest_timeline(
                root,
                "农配网工程 2024年6月10日批复，2025-01-08 开工。",
                today=date(2026, 8, 14),
            )
            from witty_agent.timeline import list_timeline_events, render_timeline

            text = render_timeline(root)
            self.assertIn("2024-06-10", text)
            self.assertTrue(text.index("2024-06-10") < text.index("2025-01-08"))
            events = list_timeline_events(root)
            self.assertEqual(events[0]["date"], "2024-06-10")
            self.assertIn("批复", events[0]["text"])
            snapshot = public_memory(root)
            self.assertEqual(snapshot["timeline_events"][0]["date"], "2024-06-10")

    async def test_memory_http_returns_lattice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            status, body = await handle_request(
                "GET",
                "/v1/memory?project_id=grid-base&agent_id=coder&scope=user",
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(body["cells"]), 9)
            self.assertIn("lattice", body)
            self.assertEqual(body.get("retrieved") or "", "")

    async def test_memory_http_post_clears_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure_api(root=root)
            workspace = root / "ws"
            workspace.mkdir()
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.user_dir,
                "assets",
                description="项目与资产",
                body="- 什么是数字化审计\n",
            )
            status, body = await handle_request(
                "POST",
                "/v1/memory",
                {
                    "project_id": "grid-base",
                    "agent_id": "coder",
                    "workspace_dir": str(workspace),
                    "slug": "assets",
                    "body": "",
                    "scope": "user",
                },
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(topic_body(memory.user_dir, "assets"), "")
            status, shown = await handle_request(
                "GET",
                "/v1/memory?project_id=grid-base&agent_id=coder&scope=user",
            )
            self.assertEqual(status, 200)
            assets = next(item for item in shown["cells"] if item["id"] == "assets")
            self.assertEqual(int(assets["count"]), 0)

    async def test_memory_http_query_fills_retrieved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            harvest_user_text(memory.user_dir, "这次农配网台区改造要先看施工图。")
            status, body = await handle_request(
                "GET",
                "/v1/memory?project_id=grid-base&agent_id=coder&scope=user&q=农配网台区施工图&workspace_dir="
                + str(workspace),
            )
            self.assertEqual(status, 200)
            self.assertTrue(body.get("retrieved"), body)
            self.assertIn("农配网", body["retrieved"])
            hits = body.get("hits") or []
            self.assertTrue(hits, body)
            self.assertTrue(any(item.get("slug") == "rural-distribution" or "农配网" in str(item.get("text") or "") for item in hits))
            ranked = retrieve_hits(memory.user_dir, "农配网台区施工图")
            self.assertTrue(ranked)
            self.assertTrue(all("slug" in item and "text" in item for item in ranked))
            rural = next(item for item in body["taxonomy"] if item["id"] == "rural-distribution")
            self.assertGreaterEqual(int(rural["count"]), 1)

    async def test_memory_http_workspace_query_fills_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            write_topic(
                memory.workspace_dir,
                "repo-layout",
                description="this workspace layout",
                body="- 本仓入口是 src/witty_agent/session.py",
            )
            status, body = await handle_request(
                "GET",
                "/v1/memory?project_id=grid-base&agent_id=coder&scope=workspace&q=session.py入口&workspace_dir="
                + str(workspace),
            )
            self.assertEqual(status, 200)
            self.assertEqual(body.get("scope"), "workspace")
            self.assertTrue(body.get("retrieved"), body)
            self.assertIn("session.py", body["retrieved"])
            self.assertTrue(any(item.get("slug") == "repo-layout" for item in body.get("hits") or []))
            domain = next(item for item in body["cells"] if item["id"] == "domain")
            self.assertNotIn("农配网台区", domain.get("body") or "")

    async def test_user_memory_http_includes_workspace_topics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            configure_api(root=root)
            memory = resolve_session_memory(
                project_id="grid-base",
                agent_id="coder",
                workspace=workspace,
                root=root,
            )
            harvest_assistant_notes(memory.workspace_dir, "已决定采用 OAuth2 授权码。")
            write_topic(
                memory.workspace_dir,
                "note-txt",
                description="note.txt",
                body="- alpha-source-line",
            )
            extras = extra_topics(memory.workspace_dir)
            self.assertTrue(any(item["id"] == "note-txt" for item in extras))
            status, body = await handle_request(
                "GET",
                "/v1/memory?project_id=grid-base&agent_id=coder&scope=user&q=OAuth2&workspace_dir="
                + str(workspace),
            )
            self.assertEqual(status, 200)
            topics = body.get("workspace_topics") or []
            slugs = {item.get("id") for item in topics}
            self.assertIn("decisions", slugs)
            self.assertIn("note-txt", slugs)
            self.assertTrue(any("OAuth2" in str(item.get("text") or "") for item in body.get("hits") or []), body)

    def test_rubric_matches_reference_example(self) -> None:
        good = "Aurora is a realtime platform. Rewrite cut p99 to 120ms.\n\n- 2 million events\n- 55% storage cut\n- Q3 2026 multi-region\n"
        result = score_summary(good)
        self.assertGreaterEqual(result.score, 4)

    async def test_scoring_loop_keeps_improvement(self) -> None:
        weak = "Aurora exists.\n"
        strong = (
            "Aurora is realtime analytics. p99 fell to 120ms.\n\n"
            "- 2 million events\n- 55% if retention drops\n- Q3 2026 multi-region\n"
        )
        llm = ScriptedLLM(
            [
                tool_reply("write", {"path": "summary.md", "content": weak}, call_id="a1"),
                text_reply("done"),
                tool_reply("write", {"path": "summary.md", "content": strong}, call_id="a2"),
                text_reply("done"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_scoring_loop(
                root=Path(tmp) / "data",
                workspace=Path(tmp) / "ws",
                stream_fn=llm,
            )
            self.assertGreater(result["after"], result["before"])
            self.assertTrue(result["keep"])


if __name__ == "__main__":
    unittest.main()
