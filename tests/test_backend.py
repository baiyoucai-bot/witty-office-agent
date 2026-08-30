from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.compaction import (
    COMPACTION_CHECKPOINT_SOURCE,
    CompactionBusy,
    CompactionLock,
    CompactionSettings,
    compact_messages,
    compact_now,
    compact_region,
    parse_compact_range,
    should_compact,
)
from witty_agent.llm import ScriptedLLM, text_reply
from witty_agent.prompts import get_prompt
from witty_agent.session import create_agent, create_session
from witty_agent.store import load_messages
from witty_agent.system_prompt import build_system_prompt, clip_skill_description, guideline_keys
from witty_agent.tools.fs import bind_workspace
from witty_agent.tools.search import grep, ls
from witty_agent.types import AgentMessage


class BackendCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_all_prompt_keys_exist(self) -> None:
        for key in (
            "system_default",
            "harness_system",
            "skills_intro",
            "skills_intro_matched",
            "skills_idle",
            "skills_plan",
            "skills_miss",
            "compaction_system",
            "command_desc_compact",
            "compaction_ok",
            "compaction_ok_region",
            "compaction_noop",
            "compaction_busy",
            "compaction_bad_range",
            "denied_tool",
            "tool_snippet_read",
            "tools_attached",
            "goal_round",
            "goal_wrap_up",
            "tool_desc_todo_write_parallel",
            "plan_policy",
            "plan_block_mutating",
            "plan_auto_enter",
            "tool_timeout",
            "tool_not_started",
            "tool_outcome_unknown",
            "tool_desc_session_health",
            "session_health_report",
            "tool_desc_doc_qa",
            "doc_qa_report",
            "tool_desc_agenda_digest",
            "agenda_digest_report",
            "tool_desc_week_digest",
            "week_digest_report",
            "tool_desc_table_qa",
            "table_qa_report",
            "edit_ok",
            "edit_not_unique",
            "fs_not_observed_edit",
            "fs_not_observed_write",
            "fs_stale_version",
            "fs_bash_changed",
            "fs_not_found_edit",
            "read_not_found",
            "read_not_file",
            "read_dir_empty",
            "read_dir_footer",
            "read_offset_oob",
            "read_footer_capped",
            "read_footer_window",
            "read_footer_eof",
            "read_line_truncated",
            "search_footer_capped",
            "bash_footer_capped",
            "bash_footer_unavailable",
            "bash_no_output",
            "bash_stderr_header",
            "bash_killed_by_signal",
            "sandbox_denied_outside",
            "sandbox_denied_venv",
            "write_ok_create",
            "write_ok_update",
            "tool_snippet_apply_patch",
            "apply_patch_bad_frame",
            "apply_patch_bad_mode",
            "apply_patch_mode_ok",
            "apply_patch_mode_missing",
            "apply_patch_unsupported",
            "apply_patch_exists",
            "apply_patch_empty_hunk",
            "apply_patch_need_context",
            "apply_patch_anchor_missing",
            "apply_patch_anchor_ambiguous",
            "apply_patch_not_eof",
            "apply_patch_deleted",
            "apply_patch_delete_missing",
            "apply_patch_moved",
            "apply_patch_move_missing",
            "apply_patch_move_same",
            "apply_patch_rolled_back",
            "fs_context_line",
            "fs_context_card",
            "command_desc_create_skill",
            "create_skill_ok",
            "skill_scaffold_markdown",
            "tool_desc_wiki_init",
            "tool_desc_wiki_search",
            "tool_desc_wiki_add",
            "tool_desc_wiki_remove",
            "tool_desc_wiki_sources",
            "wiki_search_report",
            "wiki_lint_report",
            "wiki_stats_report",
            "wiki_add_ok",
            "wiki_remove_ok",
            "llmwiki_disabled",
            "wiki_schema_body",
            "schedule_delete_ok",
            "schedule_delete_missing",
            "time_context_step1",
            "time_now_section",
            "host_now_section",
            "host_context_once",
            "host_label_macos",
            "host_label_windows",
            "host_label_linux",
            "host_weekday_monday",
            "host_git_none",
            "host_net_intranet",
            "host_net_open",
            "host_sandbox_ready_yes",
            "host_sandbox_ready_no",
            "host_sandbox_policy",
            "guideline_host",
            "guideline_timeline",
            "guideline_use_memory",
            "guideline_decide",
            "guideline_tools",
            "guideline_tools_cheap",
            "guideline_use_bash_explore",
            "guideline_dispatch",
            "guideline_ask",
            "guideline_stop",
            "guideline_cite",
            "guideline_thin",
            "evidence_gate",
            "evidence_gate_browse",
            "evidence_gate_loaded",
            "evidence_gate_loaded_batch",
            "evidence_gate_loaded_partial",
            "evidence_gate_status",
            "evidence_gate_found",
            "evidence_gate_found_batch",
            "ask_gate",
            "ask_gate_posed",
            "ask_gate_answered",
            "todo_gate",
            "plan_present_gate",
            "plan_approved",
            "evidence_seal",
            "trace_reason_tools",
            "trace_reason_memory",
            "trace_reason_both",
            "trace_reason_skill",
            "trace_reason_none",
            "trace_reason_browse",
            "stall_stop",
            "fail_strategy",
            "fail_strategy_sandbox",
            "empty_lookup",
            "answer_now",
            "recalled_answer",
            "recalled_answer_archive",
            "recalled_answer_mixed",
            "recalled_layer_mixed",
            "recalled_archive_browse",
            "tool_result_pruned",
            "recalled_verify",
            "recalled_verify_batch",
            "recalled_verify_loaded",
            "recalled_verify_missed",
            "recalled_verify_partial",
            "recalled_verify_located",
            "recalled_verify_relocated",
            "repeat_stop",
            "dispatch_refuse_trivial",
            "dispatch_refuse_fanout",
            "dispatch_reason_stay_serial",
            "dispatch_hint_serial",
            "dispatch_hint_serial_cheap",
            "dispatch_hint_serial_batch",
            "dispatch_hint_fanout",
            "todo_section",
            "todo_section_done",
            "todo_item",
            "memory_tool_fact",
            "memory_assistant_note",
            "memory_assistant_prefix",
            "memory_judge_system",
            "memory_judge_user",
            "memory_empty_miss",
            "memory_user_thin",
            "email_capability",
            "link_capability",
            "tool_desc_mail_status",
            "tool_desc_mail_analyze",
            "tool_desc_mail_draft",
            "tool_desc_link_habits",
            "tool_desc_diary_write",
            "tool_desc_pptx_create",
            "tool_desc_pptx_render",
            "tool_desc_pptx_from_html",
            "tool_desc_pptx_themes",
            "tool_desc_pptx_check",
            "tool_desc_pptx_replace_slide",
            "tool_desc_pptx_list_boxes",
            "tool_desc_pptx_edit_box",
            "tool_desc_pptx_add_page",
            "pptx_render_ok",
            "pptx_render_ok_macro",
            "pptx_lint_clean",
            "pptx_replace_ok",
            "pptx_bad_kind",
            "diary_skipped",
            "pptx_empty",
            "web_param_url",
            "session_query_param_query",
            "session_query_param_event_type",
            "session_reference",
            "session_reference_item",
            "session_reference_omitted",
            "file_reference",
            "file_reference_item",
            "file_reference_omitted",
            "file_reference_dir_omitted",
            "file_reference_image",
            "library_approval_pending",
            "library_approval_timeout",
            "library_timeout_allow",
            "library_timeout_deny",
            "instruction_updated",
            "instruction_updated_omitted",
            "instruction_removed",
            "instruction_baseline_replace",
            "instruction_baseline_replace_empty",
            "instruction_additional",
            "instruction_budget",
            "instruction_budget_omit",
            "instruction_budget_trunc",
            "plan_param_plan",
            "skill_param_name",
            "ask_user_param_questions",
            "todo_param_todos",
            "todo_param_content",
            "todo_param_status",
            "link_url_required",
            "link_open_intent",
            "email_unset",
            "email_unspecified",
            "email_no_subject",
            "email_flag_yes",
            "email_flag_no",
            "email_password_set",
            "email_password_unset",
        ):
            self.assertTrue(get_prompt(key))

    def test_tool_param_descriptions_come_from_prompts(self) -> None:
        from witty_agent.tools import list_tools

        specs = {item.name: item for item in list_tools()}
        self.assertEqual(
            specs["web_fetch"].parameters["properties"]["url"]["description"],
            get_prompt("web_param_url"),
        )
        self.assertEqual(
            specs["skill"].parameters["properties"]["name"]["description"],
            get_prompt("skill_param_name"),
        )
        self.assertEqual(
            specs["exit_plan_mode"].parameters["properties"]["plan"]["description"],
            get_prompt("plan_param_plan"),
        )
        self.assertEqual(
            specs["todo_write"].parameters["properties"]["todos"]["description"],
            get_prompt("todo_param_todos"),
        )
        self.assertNotIn("http or https URL to fetch", specs["web_fetch"].parameters["properties"]["url"]["description"])

    def test_system_prompt_includes_tools_and_skills(self) -> None:
        text = build_system_prompt(".", tool_names=["read", "write", "bash", "grep"])
        self.assertIn("read", text)
        self.assertIn("agent-optimization", text)
        self.assertIn("当前工作目录", text)
        self.assertIn("## 时钟", text)
        self.assertIn("今天：", text)
        self.assertIn("## 本机环境", text)
        self.assertIn("Git：", text)
        self.assertIn("网络：", text)
        self.assertIn("ask_user_question", text)
        self.assertIn(get_prompt("guideline_stop"), text)
        self.assertIn(get_prompt("guideline_dispatch"), text)
        self.assertIn(get_prompt("guideline_cite"), text)
        self.assertIn("须先 read", text)
        self.assertIn("看脚注", text)
        self.assertIn("不要编造", text)
        self.assertIn("which/npm/brew", text)
        self.assertIn("回执 card 不是全文", text)
        self.assertIn("回执 card 不是全文", get_prompt("tool_snippet_write"))
        self.assertIn("回执 card 不是全文", get_prompt("tool_snippet_edit"))
        self.assertIn("可以操作这台电脑", get_prompt("tool_snippet_bash"))
        self.assertIn("不要绕过", get_prompt("tool_snippet_write"))
        self.assertNotIn("You are an expert coding assistant", text)
        self.assertNotIn("If you have no tool result and no recalled fact", text)
        self.assertNotIn("If the task involves sequence, history", text)
        start = text.index("指引：")
        end = text.find("\n\n", start)
        block = text[start:end] if end > start else text[start:]
        bullets = [line for line in block.splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(bullets), 9)
        self.assertLess(sum(len(line) for line in bullets), 720)
        self.assertIn("<available_skills>", text)
        self.assertNotIn("<location>", text)
        self.assertNotIn("Do not edit Project", text)
        self.assertNotIn("Read the full skill file when the task matches", text)
        self.assertLessEqual(len(clip_skill_description("a " * 80)), 89)
        named = build_system_prompt(
            ".",
            tool_names=["read", "write", "bash", "grep"],
            prompt="给我把vscode的模型设置成opus5，现在模型一直不回复",
        )
        self.assertIn("不是当前工作区", named)
        self.assertIn("确认是本仓库的活再动手", named)
        core = (Path(__file__).resolve().parents[1] / "src" / "witty_agent" / "system_prompt.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("vscode", core.lower())

    def test_idle_prompt_omits_skill_catalog(self) -> None:
        idle = build_system_prompt(".", tool_names=["read", "write", "bash", "grep"], prompt="你好")
        self.assertIn(get_prompt("skills_idle"), idle)
        self.assertNotIn("<available_skills>", idle)
        self.assertNotIn("agent-optimization", idle)
        self.assertNotIn("agent-creation", idle)
        hello = build_system_prompt(".", tool_names=["read", "skill"], prompt="say hi")
        self.assertIn(get_prompt("skills_idle"), hello)
        self.assertNotIn("<available_skills>", hello)
        task = build_system_prompt(".", tool_names=["read", "skill"], prompt="做一份幻灯片")
        self.assertIn("<available_skills>", task)
        self.assertIn("slides", task)
        self.assertIn(get_prompt("skills_intro_matched"), task)
        self.assertNotIn("agent-optimization", task)
        self.assertNotIn("agent-creation", task)
        self.assertNotIn(get_prompt("skills_idle"), task)
        miss = build_system_prompt(".", tool_names=["read", "skill"], prompt="read note.txt")
        self.assertIn(get_prompt("skills_miss"), miss)
        self.assertNotIn("<available_skills>", miss)
        self.assertNotIn("table-qa", miss)
        self.assertNotIn("<name>slides</name>", miss)
        self.assertNotIn("agent-optimization", miss)
        self.assertEqual(
            guideline_keys(prompt="你好", tool_names=["read", "write", "bash", "grep"]),
            ["guideline_concise", "guideline_decide", "guideline_stop"],
        )
        thanks = build_system_prompt(".", tool_names=["read", "write", "bash", "grep"], prompt="好的，谢谢")
        self.assertIn(get_prompt("skills_idle"), thanks)
        self.assertNotIn(get_prompt("guideline_dispatch"), thanks)
        self.assertNotIn(get_prompt("guideline_cite"), thanks)
        self.assertNotIn("ask_user_question", thanks)
        self.assertEqual(
            guideline_keys(prompt="谢谢你", tool_names=["read", "write", "bash", "grep"]),
            ["guideline_concise", "guideline_decide", "guideline_stop"],
        )
        self.assertEqual(
            guideline_keys(prompt="我爱吃冰淇淋", tool_names=["read", "write", "bash", "grep"]),
            ["guideline_concise", "guideline_decide", "guideline_stop"],
        )
        self.assertEqual(
            guideline_keys(prompt="read note.txt", tool_names=["read", "skill"]),
            [
                "guideline_concise",
                "guideline_decide",
                "guideline_tools_cheap",
                "guideline_stop",
                "guideline_cite",
            ],
        )
        self.assertNotIn(get_prompt("guideline_dispatch"), miss)
        self.assertNotIn("ask_user_question", miss)
        self.assertNotIn(get_prompt("guideline_use_memory"), miss)
        self.assertNotIn(get_prompt("guideline_show_paths"), miss)
        self.assertNotIn(get_prompt("guideline_tools"), miss)
        self.assertIn(get_prompt("guideline_cite"), miss)
        self.assertIn(get_prompt("guideline_tools_cheap"), miss)
        self.assertNotIn(get_prompt("guideline_dispatch"), idle)
        self.assertNotIn(get_prompt("guideline_cite"), idle)
        self.assertNotIn(get_prompt("guideline_use_memory"), idle)
        self.assertNotIn(get_prompt("guideline_tools"), idle)
        self.assertIn(get_prompt("guideline_concise"), idle)
        self.assertIn("不要编造", idle)
        self.assertIn(get_prompt("guideline_stop"), idle)
        self.assertIn(get_prompt("guideline_dispatch"), task)
        self.assertNotIn(get_prompt("guideline_use_memory"), task)
        review = build_system_prompt(".", tool_names=["read", "write"], prompt="review the auth module")
        self.assertNotIn(get_prompt("guideline_use_memory"), review)
        mem = build_system_prompt(
            ".",
            tool_names=["read", "memory_read"],
            prompt="简短回复偏好是什么",
        )
        self.assertIn(get_prompt("guideline_use_memory"), mem)
        self.assertIn("guideline_use_memory", guideline_keys(prompt="简短回复偏好是什么", tool_names=["read", "memory_read"]))
        self.assertNotIn(
            "guideline_use_memory",
            guideline_keys(prompt="review the auth module", tool_names=["read", "write"]),
        )
        self.assertEqual(
            guideline_keys(
                prompt="refactor the auth module",
                tool_names=["read", "exit_plan_mode"],
                plan_active=True,
            ),
            [
                "guideline_concise",
                "guideline_decide",
                "guideline_cite",
            ],
        )
        planned = build_system_prompt(
            ".",
            tool_names=["read", "exit_plan_mode"],
            prompt="refactor the auth module",
            plan_active=True,
            plan_section=get_prompt("plan_policy"),
        )
        self.assertIn("计划模式", planned)
        self.assertIn(get_prompt("guideline_cite"), planned)
        self.assertIn(get_prompt("skills_plan"), planned)
        self.assertNotIn("<available_skills>", planned)
        self.assertNotIn("software-engineering", planned)
        self.assertNotIn(get_prompt("guideline_dispatch"), planned)
        self.assertNotIn(get_prompt("guideline_tools"), planned)
        self.assertNotIn("ask_user_question", planned)
        self.assertNotIn(get_prompt("guideline_stop"), planned)
        self.assertNotIn(get_prompt("guideline_show_paths"), planned)
        self.assertEqual(
            guideline_keys(prompt="你好", tool_names=["exit_plan_mode"], plan_active=True),
            ["guideline_concise", "guideline_decide", "guideline_stop"],
        )

    def test_thin_tools_skip_snippet_list(self) -> None:
        listed = build_system_prompt(
            ".",
            tool_names=["read", "schedule_write"],
            list_snippets=True,
        )
        self.assertIn(get_prompt("tool_snippet_read"), listed)
        self.assertIn("schedule_write", listed)
        thin = build_system_prompt(
            ".",
            tool_names=["read", "schedule_write"],
            list_snippets=False,
        )
        self.assertIn(get_prompt("tools_attached"), thin)
        self.assertNotIn(get_prompt("tool_snippet_read"), thin)
        self.assertNotIn("schedule_write", thin)

    def test_compaction_keeps_tail(self) -> None:
        messages = [AgentMessage(role="user", content="x" * 80) for _ in range(40)]
        settings = CompactionSettings(
            enabled=True, context_window=100, reserve_tokens=10, keep_recent_tokens=20
        )
        self.assertTrue(should_compact(messages, settings))
        compacted = compact_messages(messages, settings)
        self.assertLess(len(compacted), len(messages))
        self.assertIn("[compaction]", compacted[0].text())
        self.assertEqual(compacted[0].source, COMPACTION_CHECKPOINT_SOURCE)

    def test_compact_now_below_pressure_and_lock(self) -> None:
        settings = CompactionSettings(
            enabled=True, context_window=100000, reserve_tokens=10, keep_recent_tokens=20
        )
        messages = [AgentMessage(role="user", content="x" * 80) for _ in range(40)]
        self.assertFalse(should_compact(messages, settings))
        self.assertIs(compact_messages(messages, settings), messages)
        forced = compact_now(messages, settings, force=True)
        self.assertIsNotNone(forced)
        assert forced is not None
        self.assertLess(len(forced), len(messages))
        self.assertEqual(forced[0].source, COMPACTION_CHECKPOINT_SOURCE)
        empty = compact_now(
            [AgentMessage(role="user", content="hi")],
            CompactionSettings(keep_recent_tokens=20000),
            force=True,
        )
        self.assertIsNone(empty)
        lock = CompactionLock()
        lock.acquire()
        with self.assertRaises(CompactionBusy):
            lock.acquire()
        lock.release()
        with lock:
            self.assertTrue(lock.busy)
        self.assertFalse(lock.busy)

    def test_compact_region_keeps_sides_and_pairs(self) -> None:
        from witty_agent.types import ToolCallBlock

        prefix = [AgentMessage(role="user", content="keep-head")]
        region = [AgentMessage(role="user", content=f"mid {index} " + ("x" * 40)) for index in range(6)]
        suffix = [AgentMessage(role="user", content="keep-tail")]
        messages = [*prefix, *region, *suffix]
        self.assertEqual(parse_compact_range(""), None)
        self.assertEqual(parse_compact_range("1-6"), (1, 6))
        self.assertEqual(parse_compact_range("1:6"), (1, 6))
        with self.assertRaises(ValueError):
            parse_compact_range("nope")
        compacted = compact_region(messages, 1, 6)
        self.assertIsNotNone(compacted)
        assert compacted is not None
        self.assertEqual(compacted[0].text(), "keep-head")
        self.assertEqual(compacted[1].source, COMPACTION_CHECKPOINT_SOURCE)
        self.assertEqual(compacted[1].meta.get("keep_before"), 1)
        self.assertEqual(compacted[-1].text(), "keep-tail")
        self.assertEqual(len(compacted), 3)
        call = AgentMessage(
            role="assistant",
            content=[ToolCallBlock(id="c1", name="read", arguments={"path": "a.txt"})],
        )
        result = AgentMessage(role="toolResult", content="BODY", tool_call_id="c1", tool_name="read")
        split = [prefix[0], call, *region, result, suffix[0]]
        self.assertIsNone(compact_region(split, 2, 7))
        self.assertIsNone(compact_region(messages, 0, 0))
        self.assertIsNone(compact_region(messages, 3, 20))

    def test_compaction_keeps_oversized_latest_tool_result(self) -> None:
        messages = [
            AgentMessage(role="user", content="x" * 80),
            AgentMessage(role="assistant", content="calling"),
            AgentMessage(role="toolResult", content="Z" * 400, tool_call_id="c1", tool_name="read"),
        ]
        settings = CompactionSettings(
            enabled=True, context_window=40, reserve_tokens=8, keep_recent_tokens=16
        )
        self.assertTrue(should_compact(messages, settings))
        compacted = compact_messages(messages, settings)
        self.assertIn("[compaction]", compacted[0].text())
        self.assertEqual(compacted[-1].role, "toolResult")
        self.assertIn("calling", compacted[-2].text())

    def test_compaction_keeps_tool_call_with_result(self) -> None:
        from witty_agent.types import ToolCallBlock

        pad = [AgentMessage(role="user", content="x" * 80) for _ in range(12)]
        call = AgentMessage(
            role="assistant",
            content=[ToolCallBlock(id="c1", name="read", arguments={"path": "a.txt"})],
        )
        hint = AgentMessage(role="user", content="file-hint", source="plugin:file-reference")
        result = AgentMessage(role="toolResult", content="TOOL-BODY", tool_call_id="c1", tool_name="read")
        last = AgentMessage(role="user", content="next")
        messages = [*pad, call, hint, result, last]
        settings = CompactionSettings(
            enabled=True, context_window=80, reserve_tokens=20, keep_recent_tokens=8
        )
        compacted = compact_messages(messages, settings)
        self.assertIn("[compaction]", compacted[0].text())
        roles = [item.role for item in compacted[1:]]
        if "toolResult" in roles:
            tool_at = roles.index("toolResult")
            self.assertIn("assistant", roles[: tool_at + 1])
            self.assertTrue(
                any(
                    isinstance(block, ToolCallBlock) and block.id == "c1"
                    for item in compacted[1 : tool_at + 1]
                    if isinstance(item.content, list)
                    for block in item.content
                )
            )

    async def test_session_resume_from_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            agent = create_agent("grid-base", "coder", root=root)
            session = create_session(agent, workspace_dir=workspace, session_id="s1")
            await session.run("hi", stream_fn=ScriptedLLM([text_reply("hello")]))
            stored = load_messages(session._store_path())
            self.assertGreaterEqual(len(stored), 2)
            again = create_session(agent, workspace_dir=workspace, session_id="s1")
            await again.run("next", stream_fn=ScriptedLLM([text_reply("again")]))
            stored2 = load_messages(again._store_path())
            roles = [item.role for item in stored2]
            self.assertGreaterEqual(roles.count("user"), 2)

    def test_ls_and_grep(self) -> None:
        previous = os.environ.get("WITTY_HOME")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["WITTY_HOME"] = tmp
                root = Path(tmp) / "ws"
                root.mkdir()
                (root / "a.py").write_text("alpha = 1\n", encoding="utf-8")
                bind_workspace(str(root))
                listing = ls(".", 50)
                self.assertIn("a.py", listing)
                self.assertIn("sandbox/", listing)
                hits = grep("alpha", ".", "*.py")
                self.assertIn("alpha", hits)
        finally:
            if previous is None:
                os.environ.pop("WITTY_HOME", None)
            else:
                os.environ["WITTY_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
