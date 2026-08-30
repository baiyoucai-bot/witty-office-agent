"""从配置文件加载提示词。正文禁止写在本模块。"""

from __future__ import annotations

import json
import os
from witty_agent.tomlcompat import tomllib
from functools import lru_cache
from pathlib import Path

from witty_agent.logging import get_logger
from witty_agent.paths import project_root

_DEFAULT_PROMPTS_FILE = project_root() / "config" / "prompts.toml"
_ENV_PROMPTS_FILE = "WITTY_PROMPTS_FILE"
_HEADER = (
    "# 所有发给模型的文本都放这里。代码只引用 key，不要把正文写进 .py。\n"
    "# 可用环境变量 WITTY_PROMPTS_FILE 指向另一份 toml 覆盖本文件。\n"
    "\n"
    "[prompts]\n"
)
_PREVIEW = 72

logger = get_logger("prompts")


def prompts_file() -> Path:
    override = os.environ.get(_ENV_PROMPTS_FILE)
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_PROMPTS_FILE


@lru_cache(maxsize=4)
def _load_table(path: str) -> dict[str, str]:
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"提示词配置不存在: {file_path}")
    with file_path.open("rb") as fh:
        data = tomllib.load(fh)
    table = data.get("prompts")
    if not isinstance(table, dict) or not table:
        raise ValueError(f"提示词配置缺少 [prompts] 表: {file_path}")
    loaded: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"提示词 {key!r} 必须是字符串: {file_path}")
        text = value.strip()
        if not text:
            raise ValueError(f"提示词 {key!r} 为空: {file_path}")
        loaded[key] = text
    return loaded


def load_prompts() -> dict[str, str]:
    return dict(_load_table(str(prompts_file())))


def get_prompt(name: str, /, **params: object) -> str:
    """按 key 取提示词；params 用于 str.format 占位符。

    key 走位置参数（`/`）：否则 `{name}` 这种占位符没法用 `name=` 传，会撞上本函数的形参。
    """
    table = _load_table(str(prompts_file()))
    try:
        template = table[name]
    except KeyError as exc:
        known = ", ".join(sorted(table)) or "(空)"
        raise KeyError(f"未配置提示词 {name!r}，已有: {known}") from exc
    rendered = template
    for key, value in params.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def clear_prompt_cache() -> None:
    _load_table.cache_clear()


def _preview(text: str) -> str:
    clipped = " ".join(text.split())
    if len(clipped) <= _PREVIEW:
        return clipped
    return f"{clipped[: _PREVIEW - 1]}…"


def _encode_toml_value(text: str) -> str:
    if "\n" in text and '"""' not in text:
        return f'"""\n{text.rstrip()}\n"""'
    return json.dumps(text, ensure_ascii=False)


def _render_prompts_toml(table: dict[str, str]) -> str:
    lines = [_HEADER]
    for key, value in table.items():
        lines.append(f"{key} = {_encode_toml_value(value)}\n")
    return "".join(lines)


def public_prompt_index() -> dict[str, object]:
    table = load_prompts()
    return {
        "file": str(prompts_file()),
        "prompts": [
            {"name": name, "chars": len(text), "preview": _preview(text)}
            for name, text in table.items()
        ],
    }


def get_prompt_record(name: str) -> dict[str, object]:
    table = load_prompts()
    try:
        text = table[name]
    except KeyError as exc:
        known = ", ".join(sorted(table)) or "(空)"
        raise KeyError(f"未配置提示词 {name!r}，已有: {known}") from exc
    return {"name": name, "text": text, "chars": len(text)}


def save_prompt(name: str, text: str) -> dict[str, object]:
    key = name.strip()
    body = text.strip()
    if not key:
        raise ValueError("提示词 key 为空")
    if not body:
        raise ValueError("提示词正文为空")
    table = load_prompts()
    if key not in table:
        known = ", ".join(sorted(table)) or "(空)"
        raise KeyError(f"未配置提示词 {key!r}，已有: {known}")
    table[key] = body
    path = prompts_file()
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(_render_prompts_toml(table), encoding="utf-8")
    tmp.replace(path)
    clear_prompt_cache()
    logger.info("保存提示词 name=%s chars=%s", key, len(body))
    return {"name": key, "text": body, "chars": len(body)}
