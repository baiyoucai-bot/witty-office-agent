"""运行时配置：日志级别、技能目录、工具包。不含提示词正文。"""

from __future__ import annotations

import os
from witty_agent.tomlcompat import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from witty_agent.paths import project_root

_DEFAULT_RUNTIME_FILE = project_root() / "config" / "runtime.toml"
_ENV_RUNTIME_FILE = "WITTY_RUNTIME_FILE"


def runtime_file() -> Path:
    override = os.environ.get(_ENV_RUNTIME_FILE)
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_RUNTIME_FILE


@lru_cache(maxsize=4)
def _load_raw(path: str) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    with file_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}


def load_runtime() -> dict[str, Any]:
    return dict(_load_raw(str(runtime_file())))


def logging_level() -> str:
    table = load_runtime().get("logging") or {}
    if isinstance(table, dict) and isinstance(table.get("level"), str):
        return table["level"]
    return "INFO"


def skill_paths() -> list[Path]:
    table = load_runtime().get("skills") or {}
    raw: list[str] = []
    if isinstance(table, dict):
        configured = table.get("paths") or ["skills"]
        if isinstance(configured, list):
            raw.extend(str(item) for item in configured)
    extra = os.environ.get("WITTY_SKILLS_PATH")
    if extra:
        raw.extend(part for part in extra.split(os.pathsep) if part)
    resolved: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = project_root() / path
        resolved.append(path.resolve())
    from witty_agent.plugins.live import extra_skill_paths

    seen = {str(item) for item in resolved}
    for extra in extra_skill_paths():
        text = str(extra)
        if text not in seen:
            resolved.append(extra)
            seen.add(text)
    return resolved


def tool_packages() -> list[str]:
    table = load_runtime().get("tools") or {}
    raw: list[str] = []
    if isinstance(table, dict) and isinstance(table.get("packages"), list):
        raw.extend(str(item) for item in table["packages"])
    extra = os.environ.get("WITTY_TOOLS_PACKAGES")
    if extra:
        raw.extend(part.strip() for part in extra.split(os.pathsep) if part.strip())
    from witty_agent.kernel_surface import KERNEL_TOOL_PACKAGE
    from witty_agent.plugins.live import disabled_packages, extra_packages

    for item in extra_packages():
        if item and item not in raw:
            raw.append(item)
    blocked = disabled_packages()
    names = raw or [KERNEL_TOOL_PACKAGE]
    kept: list[str] = []
    for item in names:
        if item == KERNEL_TOOL_PACKAGE or item.startswith(f"{KERNEL_TOOL_PACKAGE}."):
            kept.append(item)
            continue
        if item in blocked:
            continue
        kept.append(item)
    return kept or [KERNEL_TOOL_PACKAGE]


def model_settings() -> dict[str, Any]:
    table = load_runtime().get("model") or {}
    if not isinstance(table, dict):
        return {}
    timeout = os.environ.get("WITTY_TIMEOUT_SEC") or table.get("timeout_sec") or 3600
    tokens = os.environ.get("WITTY_MAX_TOKENS") or table.get("max_tokens") or 2048
    return {
        "base_url": os.environ.get("WITTY_BASE_URL") or table.get("base_url") or "",
        "model_id": os.environ.get("WITTY_MODEL_ID") or table.get("model_id") or "",
        "timeout_sec": int(timeout),
        "max_tokens": int(tokens),
        "api_key": os.environ.get("WITTY_API_KEY") or os.environ.get("OPENAI_API_KEY") or "",
    }


def schedule_settings() -> dict[str, Any]:
    table = load_runtime().get("schedule") or {}
    if not isinstance(table, dict):
        table = {}
    raw = os.environ.get("WITTY_SCHEDULE_TICK_S") or table.get("tick_interval_s", 15)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        interval = 15.0
    return {"tick_interval_s": max(0.0, interval)}


def loop_settings() -> dict[str, Any]:
    table = load_runtime().get("loop") or {}
    if not isinstance(table, dict):
        table = {}
    execution = table.get("tool_execution") or "sequential"
    if execution not in {"sequential", "parallel"}:
        execution = "sequential"
    thresholds = table.get("repeat_thresholds") or [3, 5, 8]
    if not isinstance(thresholds, list):
        thresholds = [3, 5, 8]
    return {
        "tool_execution": execution,
        "auto_parallel": bool(table.get("auto_parallel", True)),
        "retry_attempts": int(table.get("retry_attempts") or 3),
        "max_turns": int(table.get("max_turns") or -1),
        "tool_timeout_ms": int(table.get("tool_timeout_ms") or 0),
        "repeat_thresholds": [int(item) for item in thresholds],
        "repeat_stop": int(table["repeat_stop"]) if table.get("repeat_stop") is not None else None,
        "stall_limit": int(table.get("stall_limit") or 3),
        "fail_strategy": bool(table.get("fail_strategy", True)),
        "answer_now": bool(table.get("answer_now", True)),
        "recalled_answer": bool(table.get("recalled_answer", True)),
        "recalled_cover_min": int(table.get("recalled_cover_min") or 5),
        "recalled_verify_auto": bool(table.get("recalled_verify_auto", True)),
        "browse_read_auto": bool(table.get("browse_read_auto", True)),
        "evidence_gate": bool(table.get("evidence_gate", True)),
        "ask_gate": bool(table.get("ask_gate", True)),
        "todo_gate": bool(table.get("todo_gate", True)),
        "plan_gate": bool(table.get("plan_gate", True)),
        "auto_plan": bool(table.get("auto_plan", True)),
        "thin_tools": bool(table.get("thin_tools", True)),
        "auto_skill": bool(table.get("auto_skill", True)),
        "auto_skill_min_score": int(table.get("auto_skill_min_score") or 4),
        "auto_skill_limit": int(table.get("auto_skill_limit") or 1),
    }


def todo_settings() -> dict[str, Any]:
    table = load_runtime().get("todo") or {}
    if not isinstance(table, dict):
        table = {}
    return {
        "allow_parallel_in_progress": bool(table.get("allow_parallel_in_progress", True)),
    }


def time_context_settings() -> dict[str, Any]:
    table = load_runtime().get("time_context") or {}
    if not isinstance(table, dict):
        table = {}
    return {
        "enabled": bool(table.get("enabled", True)),
        "time_zone": str(table.get("time_zone") or "Asia/Shanghai"),
        "refresh_interval_ms": int(table.get("refresh_interval_ms") or 0),
    }


def fs_observe_settings() -> dict[str, Any]:
    """read 后才覆盖写 / edit。关了退回无条件写。"""
    table = load_runtime().get("fs") or {}
    if not isinstance(table, dict):
        table = {}
    enabled = bool(table.get("observe", True))
    flag = os.environ.get("WITTY_FS_OBSERVE")
    if flag is not None:
        enabled = flag.strip().lower() not in {"0", "false", "no", "off", ""}
    return {"observe": enabled}


def sandbox_settings() -> dict[str, Any]:
    table = load_runtime().get("sandbox") or {}
    if not isinstance(table, dict):
        table = {}
    packages = table.get("packages")
    if not isinstance(packages, list):
        packages = [
            "numpy",
            "pandas",
            "openpyxl",
            "python-pptx",
            "python-docx",
            "pypdf",
            "pdfplumber",
            "rapidocr-onnxruntime",
            "matplotlib",
            "pillow",
            "lxml",
            "pyyaml",
            "python-dateutil",
        ]
    env_packages = os.environ.get("WITTY_SANDBOX_PACKAGES")
    if env_packages is not None:
        packages = [part.strip() for part in env_packages.split(",") if part.strip()]
    index = (
        os.environ.get("WITTY_SANDBOX_INDEX")
        or table.get("index_url")
        or "https://pypi.tuna.tsinghua.edu.cn/simple"
    )
    enabled = bool(table.get("enabled", True))
    flag = os.environ.get("WITTY_SANDBOX_ENABLED")
    if flag is not None:
        enabled = flag.strip().lower() not in {"0", "false", "no", "off", ""}
    return {
        "enabled": enabled,
        "packages": [str(item).strip() for item in packages if str(item).strip()],
        "index_url": str(index),
    }


def library_settings() -> dict[str, Any]:
    """库/后台入口默认权限。不改桌面与 HTTP 的 always-ask。"""
    table = load_runtime().get("library") or {}
    if not isinstance(table, dict):
        table = {}
    permission = os.environ.get("WITTY_LIBRARY_PERMISSION") or table.get("permission") or "ask"
    timeout = os.environ.get("WITTY_LIBRARY_TIMEOUT_SEC") or table.get("timeout_sec") or 30
    on_timeout = os.environ.get("WITTY_LIBRARY_ON_TIMEOUT") or table.get("on_timeout") or "allow"
    log_level = os.environ.get("WITTY_LOG_LEVEL") or table.get("log_level") or "WARNING"
    try:
        wait = float(timeout)
    except (TypeError, ValueError):
        wait = 30.0
    return {
        "permission": str(permission),
        "timeout_sec": max(0.0, wait),
        "on_timeout": str(on_timeout),
        "log_level": str(log_level),
    }


def llmwiki_settings() -> dict[str, Any]:
    """业务 wiki 接入开关。关了工具仍登记，但拒绝动手。"""
    table = load_runtime().get("llmwiki") or {}
    if not isinstance(table, dict):
        table = {}
    enabled = bool(table.get("enabled", True))
    flag = os.environ.get("WITTY_LLMWIKI_ENABLED")
    if flag is not None:
        enabled = flag.strip().lower() not in {"0", "false", "no", "off", ""}
    return {"enabled": enabled}


def nl2sql_settings() -> dict[str, Any]:
    """自然语言问数接入。关了工具仍登记，但拒绝连库。"""
    table = load_runtime().get("nl2sql") or {}
    if not isinstance(table, dict):
        table = {}
    enabled = bool(table.get("enabled", True))
    flag = os.environ.get("WITTY_NL2SQL_ENABLED")
    if flag is not None:
        enabled = flag.strip().lower() not in {"0", "false", "no", "off", ""}
    sources = table.get("sources")
    return {
        "enabled": enabled,
        "default_limit": max(1, int(table.get("default_limit") or 1000)),
        "max_limit": max(1, int(table.get("max_limit") or 10000)),
        "max_tables": max(1, int(table.get("max_tables") or 12)),
        "conf_threshold": float(table.get("conf_threshold") or 0.6),
        "sources": list(sources) if isinstance(sources, list) else [],
    }


def novel_settings() -> dict[str, Any]:
    """长篇小说状态库。默认值与 `config/runtime.toml` 那份一致——缺 `[novel]` 段时仍该工作。

    阈值直接喂给 `plugins.novel_kit.check.Thresholds`：什么算线索沉寂、什么算角色
    失踪，各家书的节奏不一样，这属于配置不属于代码。
    """
    table = load_runtime().get("novel") or {}
    if not isinstance(table, dict):
        table = {}
    enabled = bool(table.get("enabled", True))
    flag = os.environ.get("WITTY_NOVEL_ENABLED")
    if flag is not None:
        enabled = flag.strip().lower() not in {"0", "false", "no", "off", ""}
    return {
        "enabled": enabled,
        "dormant_thread_chapters": max(1, int(table.get("dormant_thread_chapters") or 3)),
        "absent_character_chapters": max(1, int(table.get("absent_character_chapters") or 5)),
        "main_character_min_appearances": max(1, int(table.get("main_character_min_appearances") or 3)),
        "stalled_run_chapters": max(2, int(table.get("stalled_run_chapters") or 3)),
        "context_budget_chars": max(512, int(table.get("context_budget_chars") or 6000)),
        "expand_hops": max(0, int(table.get("expand_hops") or 1)),
    }


def diary_settings() -> dict[str, Any]:
    """每日日记。默认值与 `config/runtime.toml` 那份一致——缺 `[diary]` 段时仍该工作。"""
    table = load_runtime().get("diary") or {}
    if not isinstance(table, dict):
        table = {}
    return {
        "enabled": bool(table.get("enabled", True)),
        "summary_min_entries": max(1, int(table.get("summary_min_entries") or 12)),
        "summary_max_days": max(1, int(table.get("summary_max_days") or 3)),
    }


def repl_settings() -> dict[str, Any]:
    """持久解释器：单元格超时、输出上限、是否公示这个工具。

    `timeout_sec` 到点先 SIGINT 不直接 kill——命名空间是这个工具的全部价值，为一次超时清掉
    它代价太大。真正杀进程只在中断都不理时发生。
    """
    table = load_runtime().get("repl") or {}
    if not isinstance(table, dict):
        table = {}
    # 0 是「不限」，是配置里写明的合法值；负数是写错了，回默认而不是当成不限——把没上限
    # 当成配置意图，方向正好错在会撑爆上下文的那一侧。
    cap = int(table.get("max_output_chars") or 16384)
    return {
        "enabled": bool(table.get("enabled", True)),
        "timeout_sec": max(1, int(table.get("timeout_sec") or 60)),
        "max_output_chars": cap if cap >= 0 else 16384,
    }


def refine_settings() -> dict[str, Any]:
    """/refine 沉淀：轨迹预算、单次条数上限、义务台账前置检查、复盘员模型。

    `model_id` 留空用会话主模型。复盘一段轨迹跟当判官一样不需要主模型的本事，
    生产上指到小快模型（同 `[goal].judge_model_id` 的道理）。
    """
    table = load_runtime().get("refine") or {}
    if not isinstance(table, dict):
        table = {}
    return {
        "enabled": bool(table.get("enabled", True)),
        "transcript_chars": max(1000, int(table.get("transcript_chars") or 12000)),
        "max_items": max(1, int(table.get("max_items") or 5)),
        "run_gates": bool(table.get("run_gates", True)),
        "model_id": str(table.get("model_id") or ""),
    }


def spill_settings() -> dict[str, Any]:
    table = load_runtime().get("spill") or {}
    if not isinstance(table, dict):
        table = {}
    return {"max_inline_bytes": int(table.get("max_inline_bytes") or 0)}


def ledger_settings() -> dict[str, Any]:
    """证伪账本：哪些报错文案（按 prompt key）算「路径不在」。

    默认值刻意跟 `config/runtime.toml` 里那份一致——配置缺 `[ledger]` 段时账本仍该工作，
    但两处得同时改。谁多谁少由 `test_bundled_data_tracks_repo` 之外的断言盯着。
    """
    table = load_runtime().get("ledger") or {}
    if not isinstance(table, dict):
        table = {}
    raw = table.get("missing_prompts")
    keys = raw if isinstance(raw, list) else ["read_not_found", "read_not_file", "ls_not_dir", "fs_not_found_edit"]
    out: list[str] = []
    seen: set[str] = set()
    for item in keys:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return {"missing_prompts": out}


_DEFAULT_SAFE_FONTS = [
    "Microsoft YaHei", "微软雅黑",
    "SimSun", "宋体",
    "SimHei", "黑体",
    "DengXian", "等线",
    "FangSong", "仿宋",
    "KaiTi", "楷体",
    "Arial", "Calibri", "Times New Roman",
]


def pptx_settings() -> dict[str, Any]:
    """PPT 生成设置。safe_fonts 是域内放映机默认装的字体清单，
    默认值与 `config/runtime.toml` 那份一致——缺 `[pptx]` 段时检查仍该工作。"""
    table = load_runtime().get("pptx") or {}
    if not isinstance(table, dict):
        table = {}
    raw = table.get("safe_fonts")
    fonts = raw if isinstance(raw, list) else list(_DEFAULT_SAFE_FONTS)
    out: list[str] = []
    seen: set[str] = set()
    for item in fonts:
        name = str(item or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return {"safe_fonts": out}


def _instruction_file_name(raw: object) -> str:
    """候选项必须是同目录文件名，忽略空、. / .. 和带分隔符的项。"""
    name = str(raw or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return ""
    return name


def _name_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = _instruction_file_name(item)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def context_settings() -> dict[str, Any]:
    """指令上下文设置：总字节上限 / 单源上限 / 候选文件名 / 项目根标记。0 = 不限制。"""
    table = load_runtime().get("context") or {}
    if not isinstance(table, dict):
        table = {}

    def _int(name: str, default: int) -> int:
        raw = table.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    return {
        "max_chars": _int("max_chars", 65536),
        "max_source_bytes": _int("max_source_bytes", 1_048_576),
        "instruction_files": _name_list(table.get("instruction_files")),
        "local_instruction_files": _name_list(table.get("local_instruction_files")),
        "project_root_markers": _name_list(table.get("project_root_markers")),
    }


def web_overlay_path(*, root: Path | None = None) -> Path:
    from witty_agent.layout import data_root

    return (root or data_root()) / "web.toml"


def load_web_overlay(*, root: Path | None = None) -> dict[str, Any]:
    path = web_overlay_path(root=root)
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}


def save_web_overlay(deny_public: bool, *, root: Path | None = None) -> Path:
    path = web_overlay_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"deny_public = {'true' if deny_public else 'false'}\n", encoding="utf-8")
    return path


def _env_optional_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def web_settings(*, root: Path | None = None) -> dict[str, Any]:
    table = load_runtime().get("web") or {}
    if not isinstance(table, dict):
        table = {}
    allow = table.get("allow_hosts") or []
    if not isinstance(allow, list):
        allow = []
    extra = os.environ.get("WITTY_WEB_ALLOW_HOSTS") or ""
    allow.extend(part.strip() for part in extra.split(",") if part.strip())
    overlay = load_web_overlay(root=root)
    env_deny = _env_optional_bool("WITTY_WEB_DENY_PUBLIC")
    if env_deny is not None:
        deny_public = env_deny
    elif "deny_public" in overlay:
        deny_public = bool(overlay.get("deny_public"))
    else:
        deny_public = bool(table.get("deny_public", False))
    return {
        "max_body_bytes": int(table.get("max_body_bytes") or 65536),
        "timeout_sec": int(table.get("timeout_sec") or 15),
        "allow_hosts": [str(item).strip().casefold() for item in allow if str(item).strip()],
        "allow_private": bool(table.get("allow_private", True)),
        "deny_public": deny_public,
        "mode": "intranet" if deny_public else "public",
        "search_provider": str(table.get("search_provider") or "anysearch").strip().casefold(),
        "search_base_url": str(table.get("search_base_url") or "").strip(),
        "search_max_results": int(table.get("search_max_results") or 5),
    }


def email_settings(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """IMAP/SMTP 内网通道。密码只走环境变量或 vault，不写进配置文件。"""
    from witty_agent.email_config import load_email_overlay, overlay_passwords

    table = load_runtime().get("email") or load_runtime().get("mail") or {}
    if not isinstance(table, dict):
        table = {}
    fallback = load_runtime().get("mail") or {}
    if not isinstance(fallback, dict):
        fallback = {}
    overlay = load_email_overlay(project_id, agent_id, root=root)
    secrets = overlay_passwords(project_id, agent_id, root=root)
    imap_password = (
        os.environ.get("WITTY_IMAP_PASSWORD")
        or os.environ.get("WITTY_MAIL_PASSWORD")
        or secrets.get("imap_password")
        or ""
    )
    smtp_password = (
        os.environ.get("WITTY_SMTP_PASSWORD")
        or secrets.get("smtp_password")
        or imap_password
    )
    mailbox = (
        os.environ.get("WITTY_MAIL_MAILBOX")
        or str(overlay.get("mailbox") or table.get("mailbox") or fallback.get("folder") or "INBOX")
    )
    return {
        "imap_host": os.environ.get("WITTY_IMAP_HOST")
        or os.environ.get("WITTY_MAIL_IMAP_HOST")
        or str(overlay.get("imap_host") or table.get("imap_host") or fallback.get("imap_host") or ""),
        "imap_port": int(
            os.environ.get("WITTY_IMAP_PORT")
            or os.environ.get("WITTY_MAIL_IMAP_PORT")
            or overlay.get("imap_port")
            or table.get("imap_port")
            or fallback.get("imap_port")
            or 993
        ),
        "imap_ssl": bool(overlay.get("imap_ssl", table.get("imap_ssl", fallback.get("use_ssl", True)))),
        "smtp_host": os.environ.get("WITTY_SMTP_HOST")
        or os.environ.get("WITTY_MAIL_SMTP_HOST")
        or str(overlay.get("smtp_host") or table.get("smtp_host") or fallback.get("smtp_host") or ""),
        "smtp_port": int(
            os.environ.get("WITTY_SMTP_PORT")
            or os.environ.get("WITTY_MAIL_SMTP_PORT")
            or overlay.get("smtp_port")
            or table.get("smtp_port")
            or fallback.get("smtp_port")
            or 465
        ),
        "smtp_ssl": bool(overlay.get("smtp_ssl", table.get("smtp_ssl", fallback.get("use_ssl", True)))),
        "smtp_starttls": bool(overlay.get("smtp_starttls", table.get("smtp_starttls", False))),
        "username": os.environ.get("WITTY_MAIL_USER")
        or str(overlay.get("username") or table.get("username") or fallback.get("username") or ""),
        "imap_password": imap_password,
        "smtp_password": smtp_password,
        "mailbox": mailbox,
        "drafts_dir": os.environ.get("WITTY_MAIL_DRAFTS") or str(table.get("drafts_dir") or ""),
        "timeout_sec": int(table.get("timeout_sec") or 20),
        "max_list": int(table.get("max_list") or 20),
        "max_body_chars": int(table.get("max_body_chars") or 8000),
    }


def mail_settings() -> dict[str, Any]:
    cfg = email_settings()
    return {
        "imap_host": cfg["imap_host"],
        "imap_port": cfg["imap_port"],
        "smtp_host": cfg["smtp_host"],
        "smtp_port": cfg["smtp_port"],
        "username": cfg["username"],
        "password": cfg["imap_password"],
        "use_ssl": cfg["imap_ssl"],
        "folder": cfg["mailbox"],
    }


def compaction_settings() -> dict[str, Any]:
    table = load_runtime().get("compaction") or {}
    if not isinstance(table, dict):
        table = {}
    return {
        "enabled": bool(table.get("enabled", True)),
        "use_model": bool(table.get("use_model", True)),
        "context_window": int(table.get("context_window") or 128000),
        "reserve_tokens": int(table.get("reserve_tokens") or 16384),
        "keep_recent_tokens": int(table.get("keep_recent_tokens") or 20000),
        "tool_result_threshold": int(table.get("tool_result_threshold") or 8192),
        "tool_result_head": int(table.get("tool_result_head") or 4096),
        "tool_result_tail": int(table.get("tool_result_tail") or 1024),
        "tool_call_arg_threshold": int(table.get("tool_call_arg_threshold") or 0),
        "tool_call_arg_head": int(table.get("tool_call_arg_head") or 2048),
        "tool_call_arg_tail": int(table.get("tool_call_arg_tail") or 512),
        "clear_at_least_chars": int(table.get("clear_at_least_chars") or 0),
        "prune_exclude_tools": _tool_name_list(table.get("prune_exclude_tools")),
    }


_DEFAULT_FATAL_ERRORS = [
    r"(?i)\b401\b|unauthorized|invalid[ _-]?api[ _-]?key|authentication failed",
    r"(?i)insufficient[ _-]?quota|quota exceeded|billing|out of credit",
    r"(?i)context (window|length)[^.]{0,24}exceed|prompt is too long|maximum context",
    r"(?i)\bmodel[^.\n]{0,64}(not[ _]found|does not exist|deprecat|decommission|retired)",
]


def goal_settings() -> dict[str, Any]:
    """目标模式：判官、空转停轮、gate 超时、致命错误判据。

    `fatal_error_patterns` 是**正则**，只列「自己不会好」的四类：凭据、额度、压缩也救不回的
    超窗、模型没了。限流 / 过载这类瞬时错误不进来——把它们当致命会把眼看要做完的活扔掉。
    """
    table = load_runtime().get("goal") or {}
    if not isinstance(table, dict):
        table = {}
    patterns = table.get("fatal_error_patterns")
    return {
        "judge": bool(table.get("judge", True)),
        "judge_model_id": str(table.get("judge_model_id") or ""),
        "stall_rounds": int(table.get("stall_rounds") or 0),
        "gate_timeout_sec": max(1, int(table.get("gate_timeout_sec") or 300)),
        "transcript_chars": int(table.get("transcript_chars") or 12000),
        "fatal_error_patterns": (
            [str(item) for item in patterns] if isinstance(patterns, list) else list(_DEFAULT_FATAL_ERRORS)
        ),
    }


def _tool_name_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def clear_runtime_cache() -> None:
    _load_raw.cache_clear()
