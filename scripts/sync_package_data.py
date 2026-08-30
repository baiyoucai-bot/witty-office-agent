"""把仓库 config/*.toml 与各技能目录同步进包内 data/。源文件仍以仓库根为准。"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src" / "witty_agent" / "data"
# 不进 wheel 的本地垃圾
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".gitkeep")


def main() -> int:
    dest_config = DEST / "config"
    dest_skills = DEST / "skills"
    if dest_config.exists():
        shutil.rmtree(dest_config)
    dest_config.mkdir(parents=True, exist_ok=True)
    for path in sorted((ROOT / "config").glob("*.toml")):
        shutil.copy2(path, dest_config / path.name)
    if dest_skills.exists():
        shutil.rmtree(dest_skills)
    dest_skills.mkdir(parents=True, exist_ok=True)
    # 整目录拷：SKILL.md 会引用同技能的 scripts/ references/ assets/，只拷正文会拷出死链接
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        shutil.copytree(skill.parent, dest_skills / skill.parent.name, ignore=IGNORE)
    (DEST / "SOURCE.txt").write_text(
        "Generated from repo config/ and skills/*/ (SKILL.md plus scripts, references, assets). "
        "Run: uv run python scripts/sync_package_data.py\n",
        encoding="utf-8",
    )
    print(f"synced -> {DEST}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
