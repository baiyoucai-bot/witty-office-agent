"""本地 CSV/TSV 只读质检：空表、空列、重复表头、列数不一。不进内核循环。"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from witty_agent import hooks
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool

_PLACEHOLDER = re.compile(r"^\s*(TODO|TBD|N/?A|xxx|待补充|占位)\s*$", re.IGNORECASE)
_SUFFIX = {".csv": ",", ".tsv": "\t"}


def _resolve(path: str) -> Path:
    target = Path(path).expanduser()
    if target.is_file():
        return target
    workspace = str(hooks.current_workspace or "").strip()
    if workspace and not target.is_absolute():
        alt = Path(workspace) / path
        if alt.is_file():
            return alt
    return target


def _inspect(target: Path, delim: str) -> list[tuple[str, str]]:
    raw = target.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return [("-", get_prompt("table_qa_empty_file"))]
    rows = list(csv.reader(raw.splitlines(), delimiter=delim))
    if not rows:
        return [("-", get_prompt("table_qa_empty_file"))]
    headers = [str(cell).strip() for cell in rows[0]]
    findings: list[tuple[str, str]] = []
    if not any(headers):
        findings.append(("-", get_prompt("table_qa_no_header")))
    blanks = [str(index + 1) for index, name in enumerate(headers) if not name]
    if blanks:
        findings.append(("-", get_prompt("table_qa_blank_header", cols=",".join(blanks))))
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for name in headers:
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        if seen[name] == 2:
            dupes.append(name)
    if dupes:
        findings.append(("-", get_prompt("table_qa_dup_header", names="、".join(dupes))))
    width = len(headers)
    empty_cols = [True] * width if width else []
    placeholders = 0
    for index, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            findings.append(
                (
                    get_prompt("table_qa_row", index=str(index)),
                    get_prompt("table_qa_ragged", got=str(len(row)), want=str(width)),
                )
            )
        for col, cell in enumerate(row):
            text = str(cell).strip()
            if col < width and text:
                empty_cols[col] = False
            if _PLACEHOLDER.match(text):
                placeholders += 1
    empty_idx = [str(i + 1) for i, flag in enumerate(empty_cols) if flag]
    if empty_idx and len(rows) > 1:
        findings.append(("-", get_prompt("table_qa_empty_col", cols=",".join(empty_idx))))
    if placeholders:
        findings.append(("-", get_prompt("table_qa_placeholder", count=str(placeholders))))
    return findings


def table_qa(path: str) -> str:
    """只读检查本地 CSV/TSV 的表头和空列。"""
    target = _resolve(path)
    if not target.is_file():
        return get_prompt("table_qa_missing", path=str(target))
    delim = _SUFFIX.get(target.suffix.lower())
    if delim is None:
        return get_prompt("table_qa_unsupported", path=str(target), kind=target.suffix or "(none)")
    findings = _inspect(target, delim)
    if not findings:
        return get_prompt("table_qa_ok", path=str(target))
    rows = "\n".join(
        get_prompt("table_qa_item", where=where, issue=issue) for where, issue in findings
    )
    return get_prompt("table_qa_report", path=str(target), count=str(len(findings)), rows=rows)


register_tool(
    ToolSpec(
        name="table_qa",
        description=get_prompt("tool_desc_table_qa"),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": get_prompt("table_qa_param_path")},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        func=table_qa,
    )
)
