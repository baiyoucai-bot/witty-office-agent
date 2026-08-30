"""novel-to-video 技能与两个脚本的验收测试。

脚本按文件路径加载：它们是技能资产，不在 `witty_agent` 包里，正常用法是沙箱解释器直接跑。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from witty_agent.paths import project_root
from witty_agent.skills import list_skills, load_skill, match_relevant_skills

SKILL_NAME = "novel-to-video"
SKILL_DIR = project_root() / "skills" / SKILL_NAME


def _load(script: str) -> ModuleType:
    path = SKILL_DIR / "scripts" / script
    name = f"n2v_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # @dataclass 要能从 sys.modules 回查本模块
    spec.loader.exec_module(module)
    return module


init_project = _load("init_project.py")
check_prompts = _load("check_prompts.py")


GOOD_FRAME = """# FRAME 001

## 模型
Seedance-2.0-Mini | 9s | 480P | 16:9 | 动漫风格

## 出镜
人物: @陈浩, @赵倩
场景: @校园空地
物品:

## 文案
【场景：大学校园空地】
[首帧画面] 陈浩站在校园空地上，赵倩站在他正前方。
[尾帧画面] 陈浩手停在空着的口袋边。

## 视频描述词
[0-5.8秒]: 白天，@校园空地 中景拍摄。@陈浩 皱眉按着太阳穴；@赵倩 站在他正前方嚷道。
【@赵倩台词】："陈浩，你是不是聋了？"

[5.8-9秒]: 镜头切至 @陈浩 近景并缓慢推近。他神色骤然消暗，下意识摸向口袋却摸空。

## 全局执行规则
见 项目卡.md
"""


class SkillMetadataTests(unittest.TestCase):
    def test_skill_is_discoverable(self) -> None:
        by_name = {item.name: item for item in list_skills()}
        self.assertIn(SKILL_NAME, by_name)
        self.assertEqual(by_name[SKILL_NAME].network, "general")

    def test_skill_body_and_references_load(self) -> None:
        skill = load_skill(SKILL_NAME)
        self.assertIn("小说转即梦视频", skill.body)
        self.assertIsNotNone(skill.references_dir)
        self.assertIsNotNone(skill.scripts_dir)
        references = {path.name for path in skill.references_dir.glob("*.md")}
        self.assertEqual(references, {"seedance-prompt.md", "assets.md"})
        scripts = {path.name for path in skill.scripts_dir.glob("*.py")}
        self.assertEqual(scripts, {"init_project.py", "check_prompts.py"})

    def test_matches_chinese_prompts(self) -> None:
        for prompt in ("把这段小说转视频，拆分镜", "帮我生成即梦视频提示词", "梳理一下角色关系再写文案"):
            names = [item.name for item in match_relevant_skills(prompt, limit=3)]
            self.assertIn(SKILL_NAME, names, prompt)

    def test_directory_name_matches_frontmatter(self) -> None:
        self.assertTrue(SKILL_DIR.is_dir())
        self.assertEqual(load_skill(SKILL_NAME).name, SKILL_DIR.name)


class InitProjectTests(unittest.TestCase):
    def test_scaffold_then_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = ["--name", "校园重生", "--root", tmp, "--frames", "2"]
            self.assertEqual(init_project.main(args), 0)
            project = Path(tmp) / "校园重生"
            for name in ("项目卡.md", "人物.md", "场景.md", "物品.md", "关系.md", "分镜.md"):
                self.assertTrue((project / name).exists(), name)
            self.assertTrue((project / "提示词" / "FRAME-002.md").exists())
            self.assertTrue((project / "参考图" / "人物").is_dir())

            (project / "人物.md").write_text("# 人物台账\n\n## 陈浩\n- 锚点: 手写的\n", encoding="utf-8")
            self.assertEqual(init_project.main(args + ["--frames", "3"]), 0)
            self.assertIn("手写的", (project / "人物.md").read_text(encoding="utf-8"))
            self.assertTrue((project / "提示词" / "FRAME-003.md").exists())

    def test_params_land_in_project_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_project.main(["--name", "竖屏", "--root", tmp, "--duration", "5", "--ratio", "9:16", "--style", "3D写实"])
            card = (Path(tmp) / "竖屏" / "项目卡.md").read_text(encoding="utf-8")
            self.assertIn("单帧时长: 5s", card)
            self.assertIn("9:16", card)
            self.assertIn("3D写实", card)
            self.assertIn("无字幕", card.replace("无任何字幕", "无字幕"))

    def test_bad_params_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(init_project.main(["--name", "x", "--root", tmp, "--ratio", "16x9"]), 2)
            self.assertEqual(init_project.main(["--name", "x", "--root", tmp, "--duration", "0"]), 2)
            self.assertEqual(init_project.main(["--name", "x", "--root", str(Path(tmp) / "nope")]), 2)


class CheckPromptsTests(unittest.TestCase):
    def _project(self, tmp: str, frame: str = GOOD_FRAME) -> Path:
        init_project.main(["--name", "p", "--root", tmp, "--frames", "0"])
        project = Path(tmp) / "p"
        (project / "人物.md").write_text(
            "# 人物台账\n\n## 陈浩\n- 锚点: 短寸黑发\n\n## 赵倩\n- 锚点: 高马尾\n", encoding="utf-8"
        )
        (project / "场景.md").write_text("# 场景台账\n\n## 校园空地\n- 锚点: 红砖教学楼\n", encoding="utf-8")
        (project / "物品.md").write_text("# 物品台账\n\n## 粉红礼盒\n- 锚点: 掌宽方盒\n", encoding="utf-8")
        (project / "分镜.md").write_text(
            "# 分镜索引\n\n| 帧 | 秒 |\n|---|---|\n| 001 | 0-9 |\n", encoding="utf-8"
        )
        (project / "提示词").mkdir(exist_ok=True)
        (project / "提示词" / "FRAME-001.md").write_text(frame, encoding="utf-8")
        return project

    def _run(self, project: Path, *extra: str) -> int:
        return check_prompts.main(["--project", str(project), *extra])

    def test_clean_frame_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(self._project(tmp)), 0)

    def test_speaker_suffix_is_not_an_asset_name(self) -> None:
        """`【@赵倩台词】` 里的 @ 指的是赵倩，不是一个叫「赵倩台词」的素材。"""
        assets = {"赵倩": "人物"}
        self.assertEqual(check_prompts.at_refs("【@赵倩台词】：\"喂\"", assets), {"赵倩"})
        self.assertEqual(check_prompts.at_refs("@赵倩 站着", assets), {"赵倩"})

    def test_unknown_at_ref_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, GOOD_FRAME.replace("@陈浩 近景", "@陈皓 近景"))
            self.assertEqual(self._run(project), 1)

    def test_dialogue_over_budget_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            long_line = "陈浩，你是不是聋了，怎么不说话，我从昨天晚上就一直在这里等你回来"
            project = self._project(tmp, GOOD_FRAME.replace("陈浩，你是不是聋了？", long_line))
            self.assertEqual(self._run(project), 1)

    def test_timeline_gap_and_overflow_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gap = self._project(tmp, GOOD_FRAME.replace("[5.8-9秒]", "[7-9秒]"))
            self.assertEqual(self._run(gap), 1)
        with tempfile.TemporaryDirectory() as tmp:
            over = self._project(tmp, GOOD_FRAME.replace("[5.8-9秒]", "[5.8-12秒]"))
            self.assertEqual(self._run(over), 1)

    def test_banned_text_element_fails_but_negation_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._project(tmp, GOOD_FRAME.replace("他神色骤然消暗", "画面下方叠加字幕「三年前」"))
            self.assertEqual(self._run(bad), 1)
        with tempfile.TemporaryDirectory() as tmp:
            ok = self._project(tmp, GOOD_FRAME.replace("他神色骤然消暗", "他神色骤然消暗，画面无字幕、无背景音乐"))
            self.assertEqual(self._run(ok), 0)

    def test_cut_words_counted_once(self) -> None:
        """`镜头切至` 不能既算 `镜头切至` 又算 `切至`。"""
        report = check_prompts.Report()
        body = "## 视频描述词\n镜头切至 A，镜头切至 B\n"
        check_prompts.check_cuts(body, report, "f")
        self.assertEqual(report.fails, [])
        self.assertEqual(report.warns, [])

    def test_cut_limit_fails(self) -> None:
        report = check_prompts.Report()
        body = "## 视频描述词\n镜头切至 A，切到 B，转场 C，画面切换 D\n"
        check_prompts.check_cuts(body, report, "f")
        self.assertEqual(len(report.fails), 1)

    def test_at_ref_missing_from_cast_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, GOOD_FRAME.replace("人物: @陈浩, @赵倩", "人物: @陈浩"))
            self.assertEqual(self._run(project), 1)

    def test_duplicate_asset_name_across_slots_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            (project / "物品.md").write_text(
                "# 物品台账\n\n## 校园空地\n- 锚点: 重名\n", encoding="utf-8"
            )
            self.assertEqual(self._run(project), 1)

    def test_indexed_frame_without_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            with (project / "分镜.md").open("a", encoding="utf-8") as fh:
                fh.write("| 002 | 0-9 |\n")
            self.assertEqual(self._run(project), 1)
            # 单帧模式不查索引
            self.assertEqual(self._run(project, "--frame", "001"), 0)

    def test_unreadable_project_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_prompts.main(["--project", str(Path(tmp) / "nope")]), 2)
            init_project.main(["--name", "empty", "--root", tmp, "--frames", "0"])
            self.assertEqual(self._run(Path(tmp) / "empty"), 2)


if __name__ == "__main__":
    unittest.main()
