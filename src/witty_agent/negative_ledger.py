"""证伪账本：失败路径带证据，证据没变则不再执行。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.types import AgentMessage, ToolCallBlock

logger = get_logger("ledger")

LEDGER_NAME = "negative.jsonl"
LEDGER_CAP = 80
_PATH_TOOLS = frozenset({"read", "write", "edit", "ls"})
# 报错文案里的 `{path}` 这类占位符。`get_prompt` 就是按这个形状做替换的。
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
# 模板去掉占位符后至少要剩这么多字面，否则它能匹配上任何东西。
# `read_not_found`（最短的一条）剩 8 个字，所以 6 既拦得住退化模板又不碰真配置。
_MIN_LITERAL = 6


def ledger_path(directory: Path) -> Path:
    return directory / LEDGER_NAME


def _template_pattern(template: str) -> re.Pattern[str] | None:
    """把报错文案模板变成正则：占位符换成同一行内的任意文字，其余按字面。

    写 `[^\\n]*?` 而不是 `.*`：这两个**当前完全等价**（`.` 本来就不含 `\\n`，除非开
    `re.DOTALL`）——变异测试确认换成 `.*` 一个断言都不动。写成字符类是把「不跨行」这件事
    写在表达式里，将来谁给这里加个 `re.DOTALL`（比如为了让某条多行文案好匹配），保证不会跟着
    悄悄失效：跨行拼接会让模板里两段字面把八竿子打不着的两行连起来算命中。
    """
    literal = _PLACEHOLDER.sub("", template)
    if len(literal.strip()) < _MIN_LITERAL:
        return None
    parts = [re.escape(part) for part in _PLACEHOLDER.split(template)]
    return re.compile("[^\n]*?".join(parts))


def _missing_patterns() -> list[re.Pattern[str]]:
    """「路径不在」这一类失败的判据：读配置点名的那几条**报错文案模板**。

    此前这里是代码里的一张关键词表（`不存在|不是普通文件|不是目录|not found|…`），而文案
    本体在 `config/prompts.toml`。两处对不上就会静默失效——实测 `read_not_found` 说的是
    「找不到 {path}」，表里没有「找不到」，于是**最常见的那种失败从来没入过账**（真工具
    真文案实测：该挡的只挡住 2/8）。反过来 `file access denied` 在表里，于是沙箱越界拒绝
    被当成「路径不在」入了账——那是策略拒绝，证据永远不会变。

    改成按 key 认：账本读的就是工具将要抛的那一条模板，改措辞自动跟着走。名单在
    `[ledger] missing_prompts`，判据（哪些失败重试没意义）因此和文案放在了一起。
    每轮工具调用最多走一次，`re.compile` 自带缓存，所以不额外缓存——好处是改完配置
    调 `clear_prompt_cache()` 就生效，不用重启。
    """
    from witty_agent.prompts import load_prompts
    from witty_agent.runtime import ledger_settings

    table = load_prompts()
    patterns: list[re.Pattern[str]] = []
    for name in ledger_settings()["missing_prompts"]:
        template = table.get(name)
        if not template:
            # 不抛：账本记账不该把工具回执的路弄崩。名单与文案是否对齐由测试盯着。
            logger.warning("证伪账本名单里的提示词不存在 key=%s", name)
            continue
        pattern = _template_pattern(template)
        if pattern is None:
            logger.warning("证伪账本跳过退化模板 key=%s", name)
            continue
        patterns.append(pattern)
    return patterns


def _resolved_target(arguments: dict[str, Any] | None, *, workspace: str | Path) -> Path:
    """工具**实际**会去看的那个路径。

    走 `resolve_allowed`（工具自己用的那一个），不要自己拼 `workspace / path`：沙箱开着时
    `sandbox/x.py` 会被映射到工作区**外面**的沙箱工作目录，自己拼出来的是个幽灵路径。
    后果是误挡而不是漏挡——`ls sandbox/新目录` 失败入账，目录真建起来了，账本盯着的幽灵
    路径依旧不存在，于是**挡住一个已经能跑的调用**（实测沙箱往返：建好之后仍被挡）。

    越界/venv 这类拒绝会让 `resolve_allowed` 抛异常。那种失败不在 `missing_prompts` 里，
    本来就不该入账；`fingerprint_target` 那时退回朴素拼接，只为让查账的证据形状保持有定义。
    转圈检测认「同一次调用」用的是同一个函数——两处对路径的看法必须一致。
    """
    args = arguments or {}
    raw = str(args.get("path") or ".").strip() or "."
    from witty_agent.sandbox import fingerprint_target

    return fingerprint_target(str(workspace), raw)


def attempt_key(name: str, arguments: dict[str, Any] | None, *, workspace: str | Path | None = None) -> str:
    """同一次尝试的指纹。

    给了 `workspace` 就按解析后的绝对路径算，于是 `sandbox/x.py` 和它的绝对写法是同一笔账。
    不给则退回原始字符串——只为兼容没有工作区可谈的调用点。
    """
    args = arguments or {}
    if name in _PATH_TOOLS:
        if workspace is None:
            payload = str(args.get("path") or ".").strip()
        else:
            payload = str(_resolved_target(args, workspace=workspace))
    else:
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(f"{name}\n{payload}".encode("utf-8")).hexdigest()[:16]
    return f"{name}:{digest}"


def current_evidence(name: str, arguments: dict[str, Any] | None, *, workspace: str | Path) -> dict[str, Any]:
    args = arguments or {}
    cwd = str(Path(workspace).resolve())
    raw = str(args.get("path") or ".").strip() or "."
    target = _resolved_target(args, workspace=workspace)
    try:
        resolved = target.resolve()
        exists = resolved.exists()
        mtime = int(resolved.stat().st_mtime) if exists else 0
    except OSError:
        exists = False
        mtime = 0
    # `target` 只为查账时看得见账本盯的是哪个文件，不参与 `_evidence_changed` 比较：
    # 沙箱 space id 变了会让它整片变化，那不是「文件出现/改了」的意思。
    return {"kind": "path", "cwd": cwd, "path": raw, "target": str(target), "exists": exists, "mtime": mtime}


def record_failure(
    directory: Path,
    call: ToolCallBlock,
    result: AgentMessage,
    *,
    workspace: str | Path,
) -> None:
    if str(result.source or "").startswith("plugin:"):
        return
    if call.name not in _PATH_TOOLS:
        return
    if not result.is_error:
        return
    if not _is_missing(result):
        return
    entry = {
        "key": attempt_key(call.name, call.arguments, workspace=workspace),
        "tool": call.name,
        "outcome": _outcome(result),
        "evidence": current_evidence(call.name, call.arguments, workspace=workspace),
        "preview": result.text()[:240],
    }
    path = ledger_path(directory)
    rows = [item for item in _load(path) if item.get("key") != entry["key"]]
    rows.append(entry)
    _write(path, rows[-LEDGER_CAP:])
    logger.info("证伪入账 tool=%s key=%s", call.name, entry["key"])


def gate_attempt(
    directory: Path,
    call: ToolCallBlock,
    *,
    workspace: str | Path,
) -> AgentMessage | None:
    if call.name not in _PATH_TOOLS:
        return None
    key = attempt_key(call.name, call.arguments, workspace=workspace)
    now = current_evidence(call.name, call.arguments, workspace=workspace)
    for item in _load(ledger_path(directory)):
        if item.get("key") != key:
            continue
        if _evidence_changed(item.get("evidence") if isinstance(item.get("evidence"), dict) else {}, now):
            return None
        text = get_prompt(
            "negative_ledger_block",
            tool=call.name,
            outcome=str(item.get("outcome") or ""),
            preview=str(item.get("preview") or "")[:200],
        )
        logger.info("证伪挡住 tool=%s key=%s", call.name, key)
        return AgentMessage(
            role="toolResult",
            content=text,
            tool_call_id=call.id,
            tool_name=call.name,
            is_error=True,
            source="plugin:negative-ledger",
        )
    return None


def workspace_ledger_dir() -> Path | None:
    raw = os.environ.get("WITTY_MEMORY_WORKSPACE")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() or path.parent.is_dir() else None


def _is_missing(result: AgentMessage) -> bool:
    # 不另外挡空回执：判据模板去掉占位符后至少剩 `_MIN_LITERAL` 个字面，空串命不中任何一条。
    return any(pattern.search(result.text() or "") for pattern in _missing_patterns())


def _outcome(result: AgentMessage) -> str:
    """入账的失败按构造只有一类：路径不在（`missing_prompts` 决定了这一点）。

    留着 `result` 形参是为了名单哪天分出第二类（比如「不是那种东西」）时有地方读回执。
    """
    del result
    return "file_missing"


def _evidence_changed(old: dict[str, Any], now: dict[str, Any]) -> bool:
    if old.get("kind") == "path":
        return bool(old.get("exists")) != bool(now.get("exists")) or int(old.get("mtime") or 0) != int(
            now.get("mtime") or 0
        )
    return False


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("key"):
            rows.append(item)
    return rows


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows),
        encoding="utf-8",
    )
