"""配置与技能的根目录。开发看仓库，pip 安装看包内 data/。"""

from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# src/witty_agent/paths.py → 仓库根；site-packages/witty_agent/paths.py 则没有 config/
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_ROOT = _HERE / "data"
_ENV_ROOT = "WITTY_PROJECT_ROOT"


def _has_prompts(root: Path) -> bool:
    return (root / "config" / "prompts.toml").is_file()


def bundled_root() -> Path:
    return _BUNDLED_ROOT


def project_root() -> Path:
    override = os.environ.get(_ENV_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    if _has_prompts(_CHECKOUT_ROOT):
        return _CHECKOUT_ROOT
    if _has_prompts(_BUNDLED_ROOT):
        return _BUNDLED_ROOT
    return _CHECKOUT_ROOT
