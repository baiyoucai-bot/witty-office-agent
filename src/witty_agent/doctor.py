"""`witty-agent doctor`：装完一键体检，配置哪不对一眼看出来。

一次性命令，纯同步；网络探测走 urllib + 超时，不进内核循环。
所有输出文案在 config/prompts.toml 的 doctor_* 键里。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.request import Request, urlopen

from witty_agent.layout import data_root
from witty_agent.logging import get_logger
from witty_agent.paths import project_root
from witty_agent.prompts import get_prompt, load_prompts
from witty_agent.runtime import model_settings, web_settings
from witty_agent.skills import list_skills
from witty_agent.vault import load_vault

logger = get_logger("doctor")

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_PROBE_TIMEOUT_SEC = 5.0
_MISSING_KEYS_SHOWN = 10
_GET_PROMPT_RE = re.compile(r"""get_prompt\(\s*["']([a-z0-9_]+)["']""")


@dataclass
class CheckResult:
    name_key: str
    status: str
    detail: str


def _resolved_model() -> dict[str, str]:
    """env 优先、保险柜兜底，对齐 http_api._hydrate_model_secrets 的顺序。"""
    settings = model_settings()
    try:
        vault = load_vault()
    except Exception as exc:  # 保险柜文件损坏不该挡住体检
        logger.warning("doctor 读保险柜失败 err=%s", exc)
        vault = {}
    return {
        "base_url": str(settings.get("base_url") or vault.get("WITTY_BASE_URL") or ""),
        "model_id": str(settings.get("model_id") or vault.get("WITTY_MODEL_ID") or ""),
        "api_key": str(settings.get("api_key") or vault.get("WITTY_API_KEY") or ""),
    }


def check_model_config(resolved: dict[str, str]) -> CheckResult:
    problems: list[str] = []
    missing_fields = [name for name in ("base_url", "model_id") if not resolved[name]]
    if missing_fields:
        problems.append(get_prompt("doctor_model_missing_config", fields=" / ".join(missing_fields)))
    if not resolved["api_key"]:
        problems.append(get_prompt("doctor_model_missing_key"))
    if problems:
        return CheckResult("doctor_name_model", FAIL, "；".join(problems))
    detail = get_prompt("doctor_model_ok", base_url=resolved["base_url"], model_id=resolved["model_id"])
    return CheckResult("doctor_name_model", OK, detail)


def check_model_connectivity(resolved: dict[str, str]) -> CheckResult:
    base_url = resolved["base_url"].rstrip("/")
    reachable_scheme = base_url.startswith(("http://", "https://"))
    if not base_url or not resolved["api_key"] or not reachable_scheme:
        return CheckResult("doctor_name_connect", OK, get_prompt("doctor_connect_skipped"))
    url = f"{base_url}/models"
    request = Request(url, headers={"Authorization": f"Bearer {resolved['api_key']}"})
    try:
        with urlopen(request, timeout=_PROBE_TIMEOUT_SEC) as response:  # noqa: S310 - 上面已限定 http/https
            code = int(getattr(response, "status", 200) or 200)
    except Exception as exc:  # 探测失败只降级为 WARN，网络环境千差万别
        return CheckResult("doctor_name_connect", WARN, get_prompt("doctor_connect_warn", url=url, reason=exc))
    return CheckResult("doctor_name_connect", OK, get_prompt("doctor_connect_ok", url=url, code=code))


def audit_prompt_keys(scan_root: Path, defined: set[str] | None = None) -> tuple[set[str], set[str]]:
    """扫 scan_root 下所有 .py 的字面量 get_prompt 引用，返回 (引用的键, 缺定义的键)。"""
    if defined is None:
        defined = set(load_prompts())
    referenced: set[str] = set()
    for path in sorted(scan_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        referenced.update(_GET_PROMPT_RE.findall(text))
    return referenced, referenced - defined


def check_prompt_keys(scan_root: Path | None = None) -> CheckResult:
    root = scan_root if scan_root is not None else project_root() / "src" / "witty_agent"
    if not root.is_dir():
        # wheel 安装时 project_root() 指向包内 data/，没有源码可扫
        return CheckResult("doctor_name_prompts", OK, get_prompt("doctor_prompts_skipped"))
    referenced, missing = audit_prompt_keys(root)
    if missing:
        listed = ", ".join(sorted(missing)[:_MISSING_KEYS_SHOWN])
        if len(missing) > _MISSING_KEYS_SHOWN:
            listed += " …"
        return CheckResult("doctor_name_prompts", FAIL, get_prompt("doctor_prompts_missing", count=len(missing), keys=listed))
    return CheckResult("doctor_name_prompts", OK, get_prompt("doctor_prompts_ok", count=len(referenced)))


def check_skills() -> CheckResult:
    try:
        count = len(list_skills())
    except Exception as exc:
        logger.warning("doctor 技能加载失败 err=%s", exc)
        return CheckResult("doctor_name_skills", FAIL, get_prompt("doctor_skills_fail", error=exc))
    if count <= 0:
        return CheckResult("doctor_name_skills", WARN, get_prompt("doctor_skills_empty"))
    return CheckResult("doctor_name_skills", OK, get_prompt("doctor_skills_ok", count=count))


def check_uv() -> CheckResult:
    found = shutil.which("uv")
    if found:
        return CheckResult("doctor_name_uv", OK, get_prompt("doctor_uv_ok", path=found))
    return CheckResult("doctor_name_uv", WARN, get_prompt("doctor_uv_missing"))


def check_npx() -> CheckResult:
    found = shutil.which("npx")
    if found:
        return CheckResult("doctor_name_npx", OK, get_prompt("doctor_npx_ok", path=found))
    return CheckResult("doctor_name_npx", WARN, get_prompt("doctor_npx_missing"))


def check_web_search() -> CheckResult:
    settings = web_settings()
    provider = str(settings.get("search_provider") or "")
    if provider == "tavily":
        key = (os.environ.get("WITTY_SEARCH_API_KEY") or os.environ.get("TAVILY_API_KEY") or "").strip()
        if not key:
            return CheckResult("doctor_name_search", WARN, get_prompt("doctor_search_warn_tavily"))
    elif provider == "searxng" and not settings.get("search_base_url"):
        return CheckResult("doctor_name_search", WARN, get_prompt("doctor_search_warn_searxng"))
    return CheckResult("doctor_name_search", OK, get_prompt("doctor_search_ok", provider=provider))


def check_home_writable() -> CheckResult:
    root = data_root()
    probe = root / ".doctor_probe"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("doctor_name_home", FAIL, get_prompt("doctor_home_fail", path=root, error=exc))
    return CheckResult("doctor_name_home", OK, get_prompt("doctor_home_ok", path=root))


def run_checks(*, scan_root: Path | None = None) -> list[CheckResult]:
    resolved = _resolved_model()
    return [
        check_model_config(resolved),
        check_model_connectivity(resolved),
        check_prompt_keys(scan_root),
        check_skills(),
        check_uv(),
        check_npx(),
        check_web_search(),
        check_home_writable(),
    ]


def run_doctor(*, scan_root: Path | None = None, stream: TextIO | None = None) -> int:
    out = stream if stream is not None else sys.stdout
    print(get_prompt("doctor_header"), file=out)
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for item in run_checks(scan_root=scan_root):
        counts[item.status] += 1
        status = f"[{item.status}]".ljust(6)
        print(get_prompt("doctor_line", status=status, name=get_prompt(item.name_key), detail=item.detail), file=out)
    failed = counts[FAIL] > 0
    summary_key = "doctor_summary_fail" if failed else "doctor_summary_pass"
    print(get_prompt(summary_key, ok=counts[OK], warn=counts[WARN], fail=counts[FAIL]), file=out)
    logger.info("doctor 完成 ok=%s warn=%s fail=%s", counts[OK], counts[WARN], counts[FAIL])
    return 1 if failed else 0
