"""只读：列出 sheet、公式、合并区、命名区域、错误缓存值。

    <沙箱 Python> inspect.py --input 表.xlsx
    <沙箱 Python> inspect.py --input 表.xlsx --sheet 汇总 --json

退出码 0 读成功；2 文件读不进来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xlsx_parts import load_workbook


def report(path: Path, sheet_name: str = "") -> dict:
    book, _, _ = load_workbook(path)
    sheets = []
    for sheet in book.sheets:
        if sheet_name and sheet.name != sheet_name:
            continue
        formulas = [
            {"ref": cell.ref, "formula": cell.formula or "(shared)", "cached": cell.value}
            for cell in sorted(sheet.cells.values(), key=lambda item: item.ref)
            if cell.has_formula or cell.shared_formula
        ]
        sheets.append(
            {
                "name": sheet.name,
                "part": sheet.part,
                "cells": len(sheet.cells),
                "formulas": len(formulas),
                "merged": sheet.merged,
                "dimension": sheet.dimension,
                "errors": sheet.error_refs,
                "formula_list": formulas[:200],
            }
        )
    return {
        "file": str(path),
        "sheets": sheets,
        "defined_names": book.defined_names,
    }


def render(payload: dict) -> str:
    lines = [f"file: {payload['file']}", f"sheets: {len(payload['sheets'])}"]
    if payload["defined_names"]:
        lines.append("names: " + ", ".join(payload["defined_names"]))
    for sheet in payload["sheets"]:
        merged = f"  merged={len(sheet['merged'])}" if sheet["merged"] else ""
        errors = f"  errors={len(sheet['errors'])}" if sheet["errors"] else ""
        lines.append(
            f"- {sheet['name']}  cells={sheet['cells']}  formulas={sheet['formulas']}"
            f"{merged}{errors}  dim={sheet['dimension'] or '-'}"
        )
        for item in sheet["formula_list"][:20]:
            cached = f"  cached={item['cached']}" if item["cached"] else ""
            lines.append(f"    {sheet['name']}!{item['ref']} ={item['formula']}{cached}")
        extra = sheet["formulas"] - min(sheet["formulas"], 20)
        if extra > 0:
            lines.append(f"    … {extra} more formulas")
        if sheet["errors"]:
            lines.append("    errors: " + ", ".join(sheet["errors"][:20]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查 .xlsx 结构")
    parser.add_argument("--input", required=True, help="xlsx 路径")
    parser.add_argument("--sheet", default="", help="只看这一张")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    try:
        payload = report(path, args.sheet)
    except (ValueError, KeyError, OSError) as exc:
        print(f"读不进来: {exc}", file=sys.stderr)
        return 2
    if args.sheet and not payload["sheets"]:
        print(f"没有名为 {args.sheet} 的工作表", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else render(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
