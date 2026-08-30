"""scripts/check_skills.py：技能规范校验。仓库自身必须 0 FAIL，坏技能要拦得住。"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType

from witty_agent.paths import project_root


def _load() -> ModuleType:
    path = project_root() / "scripts" / "check_skills.py"
    spec = importlib.util.spec_from_file_location("check_skills_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_skills = _load()


class RepoSkillsTests(unittest.TestCase):
    def test_repo_skills_have_no_fail(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = check_skills.main([])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("0 FAIL", buf.getvalue())


class BadSkillTests(unittest.TestCase):
    def test_missing_skill_md_and_name_mismatch_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty-dir").mkdir()
            bad = root / "bad-name"
            bad.mkdir()
            (bad / "SKILL.md").write_text(
                "---\nname: other-name\ndescription: 描述\n---\n\n正文\n", encoding="utf-8"
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check_skills.main(["--skills-dir", str(root)])
            out = buf.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("缺 SKILL.md", out)
            self.assertIn("frontmatter 不合法", out)

    def test_unknown_network_fails_dead_ref_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "my-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: my-skill\n"
                "description: 演示\n"
                "network: internal-typo\n"
                "metadata:\n  triggers: 演示\n"
                "---\n\n"
                "跑 `scripts/run.py`。\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check_skills.main(["--skills-dir", str(root)])
            out = buf.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("network='internal-typo' 不认识", out)
            self.assertIn("scripts/run.py 在任何技能目录里都不存在", out)

    def test_cross_skill_reference_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = root / "lib-skill"
            (lib / "references").mkdir(parents=True)
            (lib / "references" / "shared.md").write_text("共享材料\n", encoding="utf-8")
            (lib / "SKILL.md").write_text(
                "---\nname: lib-skill\ndescription: 材料库\nnetwork: general\n"
                "metadata:\n  triggers: 材料\n---\n\n见 `references/shared.md`。\n",
                encoding="utf-8",
            )
            user = root / "user-skill"
            user.mkdir()
            (user / "SKILL.md").write_text(
                "---\nname: user-skill\ndescription: 用别人材料\nnetwork: general\n"
                "metadata:\n  triggers: 用料\n---\n\n见 lib-skill 技能的 `references/shared.md`。\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check_skills.main(["--skills-dir", str(root)])
            self.assertEqual(code, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
