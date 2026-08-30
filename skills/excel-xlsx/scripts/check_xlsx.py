"""只读校验 .xlsx：公式被值覆盖、错误缓存、空表名、合并区压格。

    <沙箱 Python> check_xlsx.py --input 表.xlsx
    <沙箱 Python> check_xlsx.py --input 改过.xlsx --original 原稿.xlsx

--original 的判据是「原稿里有公式的格子，现在还必须有公式」。pandas.to_excel
和 openpyxl 用 data_only 再保存，都会把公式变成死数字，文件照样能打开，
所以这是本技能要抓的静默失败。

退出码 0 无 FAIL；1 有 FAIL；2 读不进来。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from xlsx_parts import Workbook, load_workbook

Finding = tuple[str, str, str]  # level, where, message


def check_names(book: Workbook) -> list[Finding]:
    found: list[Finding] = []
    names = [sheet.name for sheet in book.sheets]
    if not names:
        found.append(("FAIL", "-", "工作簿里一张表都没有"))
        return found
    blanks = [index + 1 for index, name in enumerate(names) if not name.strip()]
    if blanks:
        found.append(("FAIL", "-", f"第 {','.join(map(str, blanks))} 张表没有名字"))
    seen: dict[str, int] = {}
    for name in names:
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        if seen[name] == 2:
            found.append(("FAIL", name, "工作表重名"))
    return found


def check_errors(book: Workbook) -> list[Finding]:
    found: list[Finding] = []
    for sheet in book.sheets:
        for ref in sheet.error_refs:
            cell = sheet.cells[ref]
            found.append(("FAIL", f"{sheet.name}!{ref}", f"缓存值是错误 {cell.value}"))
    return found


def check_merged(book: Workbook) -> list[Finding]:
    found: list[Finding] = []
    for sheet in book.sheets:
        for region in sheet.merged:
            found.append(("WARN", f"{sheet.name}!{region}", "合并区：改格子时只能写左上角，否则 Word/WPS 里看不见"))
    return found


def check_formulas_kept(original: Workbook, current: Workbook) -> list[Finding]:
    found: list[Finding] = []
    current_by_name = {sheet.name: sheet for sheet in current.sheets}
    for sheet in original.sheets:
        now = current_by_name.get(sheet.name)
        if now is None:
            found.append(("FAIL", sheet.name, "原稿有这张表，现在没了"))
            continue
        for ref, cell in sheet.cells.items():
            if not cell.has_formula and not cell.shared_formula:
                continue
            other = now.cells.get(ref)
            if other is None:
                found.append(("FAIL", f"{sheet.name}!{ref}", "原稿是公式，现在格子没了"))
                continue
            if not other.has_formula and not other.shared_formula:
                found.append(
                    (
                        "FAIL",
                        f"{sheet.name}!{ref}",
                        f"原稿是公式 ={cell.formula or 'shared'}，现在是 {other.kind} {other.value!r}：公式被值覆盖了",
                    )
                )
    return found


def render(findings: list[Finding]) -> str:
    if not findings:
        return "OK  0 FAIL  0 WARN"
    fails = sum(1 for item in findings if item[0] == "FAIL")
    warns = sum(1 for item in findings if item[0] == "WARN")
    lines = [f"{fails} FAIL  {warns} WARN"]
    for level, where, message in findings:
        lines.append(f"{level}  {where}  {message}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读校验 xlsx")
    parser.add_argument("--input", required=True)
    parser.add_argument("--original", default="", help="对照原稿，查公式有没有被值覆盖")
    parser.add_argument("--strict-merge", action="store_true", help="合并区升为 FAIL")
    args = parser.parse_args(argv)
    path = Path(args.input)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2
    try:
        book, _, _ = load_workbook(path)
    except (ValueError, OSError) as exc:
        print(f"读不进来: {exc}", file=sys.stderr)
        return 2
    findings = check_names(book) + check_errors(book) + check_merged(book)
    if args.strict_merge:
        findings = [
            ("FAIL", where, message) if level == "WARN" and "合并区" in message else (level, where, message)
            for level, where, message in findings
        ]
    if args.original:
        origin = Path(args.original)
        if not origin.is_file():
            print(f"找不到原稿: {origin}", file=sys.stderr)
            return 2
        try:
            old, _, _ = load_workbook(origin)
        except (ValueError, OSError) as exc:
            print(f"原稿读不进来: {exc}", file=sys.stderr)
            return 2
        findings.extend(check_formulas_kept(old, book))
    print(render(findings))
    return 1 if any(item[0] == "FAIL" for item in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
