from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.runtime import clear_runtime_cache
from witty_agent.skills import (
    SkillMeta,
    install_user_skill,
    list_skills,
    list_user_skills,
    load_skill,
    match_relevant_skills,
    network_label,
    normalize_network,
)
from witty_agent.tools import list_tools, tool
from witty_agent.tools.skill import skill_names_for_turn
from witty_agent.types import AgentMessage


class SkillsToolsTests(unittest.TestCase):
    def test_normalize_network_aliases(self) -> None:
        self.assertEqual(normalize_network("内网"), "intranet")
        self.assertEqual(normalize_network("offline"), "intranet")
        self.assertEqual(normalize_network("外网"), "public")
        self.assertEqual(normalize_network("internet"), "public")
        self.assertEqual(normalize_network(""), "general")
        self.assertEqual(normalize_network("whatever"), "general")
        self.assertEqual(network_label("intranet"), "内网")
        self.assertEqual(network_label("public"), "外网")
        self.assertEqual(network_label("general"), "通用")

    def test_builtin_skill_network_tags(self) -> None:
        by_name = {item.name: item for item in list_skills()}
        self.assertEqual(by_name["witty-ppt-skills"].network, "intranet")
        self.assertEqual(by_name["excel-xlsx"].network, "intranet")
        self.assertEqual(by_name["pdf-extract"].network, "intranet")
        self.assertEqual(by_name["long-document"].network, "general")
        self.assertEqual(by_name["mail-desk"].network, "intranet")
        self.assertEqual(by_name["daily-diary"].network, "intranet")
        self.assertEqual(by_name["link-box"].network, "intranet")
        self.assertEqual(by_name["software-engineering"].network, "general")
        self.assertEqual(by_name["slides"].network, "general")
        self.assertEqual(by_name["llm-wiki"].network, "public")
        loaded = load_skill("witty-ppt-skills")
        self.assertEqual(loaded.network, "intranet")

    def test_install_user_skill_from_dir_file_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox" / "note-clip"
            inbox.mkdir(parents=True)
            (inbox / "SKILL.md").write_text(
                "---\nname: note-clip\ndescription: Clip notes.\nnetwork: intranet\n---\n# Clip\n",
                encoding="utf-8",
            )
            (inbox / "scripts").mkdir()
            (inbox / "scripts" / "ok.py").write_text("print(1)\n", encoding="utf-8")
            meta = install_user_skill(inbox, project_id="grid-base", agent_id="coder", root=root)
            self.assertEqual(meta.name, "note-clip")
            self.assertEqual(meta.origin, "user")
            self.assertEqual(meta.network, "intranet")
            dest = Path(meta.path)
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertTrue((dest / "scripts" / "ok.py").is_file())
            self.assertTrue(
                any(item.name == "note-clip" for item in list_user_skills("grid-base", "coder", root=root))
            )
            with self.assertRaises(FileExistsError):
                install_user_skill(inbox, project_id="grid-base", agent_id="coder", root=root)
            again = install_user_skill(
                inbox, project_id="grid-base", agent_id="coder", root=root, overwrite=True
            )
            self.assertEqual(again.name, "note-clip")
            lone = root / "Downloads" / "SKILL.md"
            lone.parent.mkdir()
            lone.write_text(
                "---\nname: lone-file\ndescription: From a loose file.\n---\n# Lone\n",
                encoding="utf-8",
            )
            loose = install_user_skill(lone, project_id="grid-base", agent_id="coder", root=root)
            self.assertEqual(loose.name, "lone-file")
            self.assertEqual(loose.path.name, "lone-file")
            typed = install_user_skill(
                text="---\nname: typed-in\ndescription: From editor text.\n---\n# Text\n",
                project_id="grid-base",
                agent_id="coder",
                root=root,
            )
            self.assertEqual(typed.name, "typed-in")
            self.assertTrue((Path(typed.path) / "SKILL.md").is_file())
            with self.assertRaises(FileNotFoundError):
                install_user_skill(root / "missing", project_id="grid-base", agent_id="coder", root=root)
            empty = root / "empty-dir"
            empty.mkdir()
            with self.assertRaises(ValueError):
                install_user_skill(empty, project_id="grid-base", agent_id="coder", root=root)

    def test_load_agentskills_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "pdf-extract"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: pdf-extract
description: Extract text from PDF files. Use when the user mentions PDF.
metadata:
  author: witty
---
# PDF Extract

Read the file then extract text.
""",
                encoding="utf-8",
            )
            (skill_dir / "scripts").mkdir()
            os.environ["WITTY_SKILLS_PATH"] = str(root)
            clear_runtime_cache()
            try:
                metas = list_skills()
                self.assertIn("pdf-extract", [item.name for item in metas])
                skill = load_skill("pdf-extract")
                self.assertIn("extract text", skill.body.lower())
                self.assertIsNotNone(skill.scripts_dir)
                self.assertEqual(skill.metadata["author"], "witty")
            finally:
                os.environ.pop("WITTY_SKILLS_PATH", None)
                clear_runtime_cache()

    def test_skip_invalid_skill_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "BadName"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: BadName\ndescription: invalid\n---\nbody\n",
                encoding="utf-8",
            )
            os.environ["WITTY_SKILLS_PATH"] = str(root)
            clear_runtime_cache()
            try:
                names = {item.name for item in list_skills()}
                self.assertNotIn("BadName", names)
            finally:
                os.environ.pop("WITTY_SKILLS_PATH", None)
                clear_runtime_cache()

    def test_tool_schema_and_builtin_discovery(self) -> None:
        @tool
        def add_numbers(left: int, right: int = 1) -> int:
            """Add two integers.

            Args:
                left: First number
                right: Second number
            """
            return left + right

        spec = add_numbers._witty_tool
        self.assertEqual(spec.name, "add_numbers")
        self.assertEqual(spec.parameters["required"], ["left"])
        self.assertEqual(spec.parameters["properties"]["left"]["type"], "integer")
        self.assertIs(spec.parameters["additionalProperties"], False)
        self.assertEqual(spec.func(2, 3), 5)

        tools = {item.name: item for item in list_tools()}
        self.assertIn("list_available_skills", tools)
        result = tools["list_available_skills"].func()
        self.assertIsInstance(result, list)

    def test_match_relevant_skills_hits_description(self) -> None:
        names = [item.name for item in match_relevant_skills("做一份幻灯片")]
        self.assertEqual(names, ["slides"])
        csv = [item.name for item in match_relevant_skills("analyze this csv data")]
        self.assertEqual(csv, ["data-analysis"])
        report = [item.name for item in match_relevant_skills("write a report memo")]
        self.assertEqual(report, ["office-document"])
        zh_report = [item.name for item in match_relevant_skills("写一份报告")]
        self.assertEqual(zh_report, ["office-document"])
        self.assertEqual([item.name for item in match_relevant_skills("做个PPT")], ["witty-ppt-skills"])
        self.assertEqual([item.name for item in match_relevant_skills("出一份汇报材料")], ["slides"])
        self.assertEqual([item.name for item in match_relevant_skills("看一下这个 csv")], ["data-analysis"])
        self.assertEqual([item.name for item in match_relevant_skills("写会议记录")], ["office-document"])
        self.assertEqual([item.name for item in match_relevant_skills("review this module")], ["software-engineering"])
        self.assertEqual([item.name for item in match_relevant_skills("帮我看下这段代码")], ["software-engineering"])
        self.assertEqual([item.name for item in match_relevant_skills("修一个 bug")], ["software-engineering"])
        self.assertEqual([item.name for item in match_relevant_skills("create an agent")], ["agent-creation"])
        self.assertEqual([item.name for item in match_relevant_skills("新建一个智能体")], ["agent-creation"])
        self.assertEqual([item.name for item in match_relevant_skills("做一份pptx")], ["witty-ppt-skills"])
        self.assertEqual([item.name for item in match_relevant_skills("用witty-ppt-skills出稿")], ["witty-ppt-skills"])
        self.assertEqual([item.name for item in match_relevant_skills("看一下收件箱")], ["mail-desk"])
        self.assertEqual([item.name for item in match_relevant_skills("帮我回一封邮件")], ["mail-desk"])
        self.assertEqual([item.name for item in match_relevant_skills("维护我的链接库")], ["link-box"])
        self.assertEqual([item.name for item in match_relevant_skills("写下今天的行为日记")], ["daily-diary"])

    def test_match_relevant_skills_ignores_weak_overlap(self) -> None:
        self.assertEqual(match_relevant_skills("hello"), [])
        self.assertEqual(match_relevant_skills("你好"), [])
        self.assertEqual(match_relevant_skills("read note.txt"), [])
        self.assertEqual(match_relevant_skills("what does missing.txt contain?"), [])
        self.assertEqual(match_relevant_skills("what does note.txt contain?"), [])
        self.assertNotIn("doc-qa", [item.name for item in match_relevant_skills("missing.txt")])
        self.assertIn("doc-qa", [item.name for item in match_relevant_skills("质检 missing headings")])
        self.assertEqual(match_relevant_skills("read README"), [])
        self.assertEqual(match_relevant_skills("read LICENSE"), [])
        probe = SkillMeta(
            name="heading-check",
            description="Inspect README headings in documents",
            path=Path("."),
            skill_file=Path("SKILL.md"),
        )
        self.assertEqual(match_relevant_skills("read README", skills=[probe]), [])
        self.assertEqual(match_relevant_skills("README", skills=[probe]), [])
        self.assertEqual(
            [item.name for item in match_relevant_skills("质检 readme headings", skills=[probe])],
            ["heading-check"],
        )
        self.assertEqual(match_relevant_skills("I have data"), [])
        self.assertEqual(match_relevant_skills("review this later"), [])
        self.assertEqual(match_relevant_skills("talk about an agent"), [])
        self.assertEqual(match_relevant_skills("create a file"), [])

    def test_match_relevant_skills_ignores_zh_generic_words(self) -> None:
        """通用实词单独命中不算意图：一个二字词曾等于阈值，闲聊 32% 被劫持。

        「今天天气不错」只靠 `今天` 撞 daily-diary 的「提到今天做了什么」，
        「没问题」只靠 `问题` 撞 nl2sql 的「回答问题的完整流水线」。
        """
        for prompt in (
            "今天天气不错",
            "今天几号",
            "我今天有点累",
            "没问题",
            "有个问题",
            "处理一下",
            "打开一下",
            "时间不够",
            "用户说了什么",
            "完整看一下",
            "今天有什么安排",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(match_relevant_skills(prompt), [])

    def test_declared_triggers_beat_function_char_discount(self) -> None:
        """triggers: 里点名的整词按整词计分，不按虚词字打折。

        「开会」的「会」在 _ZH_FUNCTION 里，二字词只值 2 分，够不到阈值——
        作者明写的触发词此前永远单独命中不了，只能靠 `今天` 这类通用词蹭分。
        """
        probe = SkillMeta(
            name="meeting-log",
            description="记录会议",
            path=Path("."),
            skill_file=Path("SKILL.md"),
            metadata={"triggers": "开会"},
        )
        self.assertEqual(
            [item.name for item in match_relevant_skills("今天开会了，记一下", skills=[probe])],
            ["meeting-log"],
        )
        bare = SkillMeta(
            name="meeting-log",
            description="记录会议 开会",
            path=Path("."),
            skill_file=Path("SKILL.md"),
        )
        self.assertEqual(match_relevant_skills("今天开会了，记一下", skills=[bare]), [])

    def test_zh_prompts_reach_english_described_skills(self) -> None:
        """描述是英文的技能也得能被中文提问路由到（llm-wiki 此前中文完全不可达）。"""
        for prompt in (
            "帮我搭一个个人知识库，把这些资料都归档进去交叉引用",
            "把这篇文章归档进我的知识库",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    [item.name for item in match_relevant_skills(prompt)],
                    ["llm-wiki"],
                )
        self.assertEqual(
            [item.name for item in match_relevant_skills("set up a personal knowledge base wiki")],
            ["llm-wiki"],
        )

    def test_slash_skill_wins_over_auto_match(self) -> None:
        names = skill_names_for_turn(
            [AgentMessage(role="user", content="/agent-creation please make slides")],
            "please make slides",
            reserved={"plan", "abort"},
        )
        self.assertEqual(names, ["agent-creation"])
        auto = skill_names_for_turn(
            [AgentMessage(role="user", content="please make slides")],
            "please make slides",
            reserved={"plan", "abort"},
        )
        self.assertEqual(auto, ["slides"])
        off = skill_names_for_turn(
            [AgentMessage(role="user", content="please make slides")],
            "please make slides",
            auto=False,
        )
        self.assertEqual(off, [])
        planned = skill_names_for_turn(
            [AgentMessage(role="user", content="please make slides")],
            "please make slides",
            reserved={"plan", "abort"},
            plan_active=True,
        )
        self.assertEqual(planned, [])
        slash_plan = skill_names_for_turn(
            [AgentMessage(role="user", content="/slides please make slides")],
            "please make slides",
            reserved={"plan", "abort"},
            plan_active=True,
        )
        self.assertEqual(slash_plan, ["slides"])


if __name__ == "__main__":
    unittest.main()
