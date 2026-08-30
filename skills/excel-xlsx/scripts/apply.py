"""在已有 .xlsx 上按 JSON spec 改格子，默认不覆盖公式。

    <沙箱 Python> apply.py --input 原稿.xlsx --output 改过.xlsx --spec 改动.json
    <沙箱 Python> apply.py --help-spec

只改目标工作表 XML，其余部件按原顺序复制。不要用 pandas.to_excel 覆盖整本。

退出码 0 写出成功；1 spec 拒绝（公式会被覆盖、格子不合法）；2 读不进来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

from xlsx_parts import (
    NS,
    col_index,
    load_workbook,
    parse_xml,
    qn,
    split_ref,
    write_zip,
)

SPEC_HELP = """spec JSON：

{
  "sheet": "汇总",          // 表名，或缺省第一张
  "sets": [
    {"cell": "B2", "value": 10},
    {"cell": "D2", "formula": "B2*C2"},
    {"cell": "A1", "value": "项目"},
    {"cell": "E2", "value": 99, "overwrite_formula": true}
  ]
}

value 与 formula 只能写一个。公式不要带前导 =（带了会剥掉）。
原稿格子里有公式时，默认拒绝写成 value，必须显式 overwrite_formula=true。
"""


class ApplyError(Exception):
    """spec 不合法或会毁掉公式。"""


def _text(tag: str, body: str) -> Element:
    node = ET.Element(qn(tag))
    node.text = body
    return node


def _inline_text(text: str) -> Element:
    cell = ET.Element(qn("c"), {"t": "inlineStr"})
    is_el = ET.SubElement(cell, qn("is"))
    t_el = ET.SubElement(is_el, qn("t"))
    t_el.text = text
    return cell


def _number_cell(value: str) -> Element:
    cell = ET.Element(qn("c"))
    cell.append(_text("v", value))
    return cell


def _formula_cell(formula: str) -> Element:
    cell = ET.Element(qn("c"))
    cell.append(_text("f", formula))
    return cell


def _strip_eq(formula: str) -> str:
    body = formula.strip()
    return body[1:] if body.startswith("=") else body


def _cell_has_formula(node: Element) -> bool:
    return node.find("m:f", NS) is not None


def _row_map(sheet_root: Element) -> dict[int, Element]:
    out: dict[int, Element] = {}
    data = sheet_root.find("m:sheetData", NS)
    if data is None:
        data = ET.SubElement(sheet_root, qn("sheetData"))
    for row in list(data):
        if row.tag != qn("row"):
            continue
        ref = row.get("r")
        if ref and ref.isdigit():
            out[int(ref)] = row
    return out


def _sorted_insert(parent: Element, child: Element, key: int, existing: dict[int, Element], attr: str) -> None:
    if key in existing:
        old = existing[key]
        parent.remove(old)
    siblings = [node for node in list(parent) if node.tag == child.tag]
    placed = False
    for node in siblings:
        raw = node.get(attr) or ""
        letters = re.sub(r"\d+", "", raw) if attr == "r" and re.search(r"[A-Za-z]", raw) else ""
        if attr == "r" and letters:
            other = col_index(letters)
        elif raw.isdigit():
            other = int(raw)
        else:
            continue
        if other > key:
            parent.insert(list(parent).index(node), child)
            placed = True
            break
    if not placed:
        parent.append(child)
    existing[key] = child


def _put_cell(sheet_root: Element, ref: str, new_cell: Element) -> None:
    letters, row_no = split_ref(ref)
    new_cell.set("r", f"{letters}{row_no}")
    data = sheet_root.find("m:sheetData", NS)
    if data is None:
        data = ET.SubElement(sheet_root, qn("sheetData"))
        # sheetData 通常紧跟 sheetFormatPr / dimension，插到末尾也够用
    rows = _row_map(sheet_root)
    row = rows.get(row_no)
    if row is None:
        row = ET.Element(qn("row"), {"r": str(row_no)})
        _sorted_insert(data, row, row_no, rows, "r")
    cells: dict[int, Element] = {}
    for node in list(row):
        if node.tag != qn("c"):
            continue
        addr = node.get("r") or ""
        if _REF_OK(addr):
            cells[col_index(split_ref(addr)[0])] = node
    col = col_index(letters)
    if col in cells:
        row.remove(cells[col])
        del cells[col]
    _sorted_insert(row, new_cell, col, cells, "r")


def _REF_OK(ref: str) -> bool:
    return bool(re.match(r"^[A-Za-z]+\d+$", ref or ""))


def _build_cell(item: dict, existing: Element | None) -> Element:
    if "formula" in item and "value" in item:
        raise ApplyError(f"{item.get('cell')} 同时写了 value 和 formula")
    if "formula" in item:
        formula = _strip_eq(str(item["formula"]))
        if not formula:
            raise ApplyError(f"{item.get('cell')} 公式是空的")
        return _formula_cell(formula)
    if "value" not in item:
        raise ApplyError(f"{item.get('cell')} 既没有 value 也没有 formula")
    if existing is not None and _cell_has_formula(existing) and not item.get("overwrite_formula"):
        raise ApplyError(
            f"{item.get('cell')} 原稿是公式。要改成值必须 overwrite_formula=true，"
            "否则就是 pandas/openpyxl 那种把公式写死的静默事故。"
        )
    value = item["value"]
    if isinstance(value, bool):
        cell = ET.Element(qn("c"), {"t": "b"})
        cell.append(_text("v", "1" if value else "0"))
        return cell
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number_cell(str(value))
    text = "" if value is None else str(value)
    return _inline_text(text)


def apply_spec(path: Path, spec: dict) -> tuple[list, dict[str, bytes]]:
    book, infos, data = load_workbook(path)
    sheet_name = str(spec.get("sheet") or "")
    target = book.sheet(sheet_name) if sheet_name else (book.sheets[0] if book.sheets else None)
    if target is None:
        raise ApplyError(f"没有工作表 {sheet_name or '(空)'}")
    raw = data.get(target.part)
    if raw is None:
        raise ApplyError(f"缺部件 {target.part}")
    root = parse_xml(raw)
    rows = _row_map(root)
    sets = spec.get("sets")
    if not isinstance(sets, list) or not sets:
        raise ApplyError("spec.sets 必须是非空数组")
    for item in sets:
        if not isinstance(item, dict) or not item.get("cell"):
            raise ApplyError("sets 里每一项都要有 cell")
        ref = str(item["cell"]).strip().upper()
        letters, row_no = split_ref(ref)
        existing = None
        row = rows.get(row_no)
        if row is not None:
            for node in row.findall("m:c", NS):
                if (node.get("r") or "").upper() == f"{letters}{row_no}":
                    existing = node
                    break
        new_cell = _build_cell(item, existing)
        _put_cell(root, ref, new_cell)
        rows = _row_map(root)
    data[target.part] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return infos, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 spec 改 xlsx，默认不覆盖公式")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--spec")
    parser.add_argument("--help-spec", action="store_true")
    args = parser.parse_args(argv)
    if args.help_spec:
        print(SPEC_HELP)
        return 0
    if not args.input or not args.output or not args.spec:
        print("需要 --input --output --spec，或 --help-spec", file=sys.stderr)
        return 2
    src = Path(args.input)
    spec_path = Path(args.spec)
    if not src.is_file():
        print(f"找不到文件: {src}", file=sys.stderr)
        return 2
    if not spec_path.is_file():
        print(f"找不到 spec: {spec_path}", file=sys.stderr)
        return 2
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"spec 不是 JSON: {exc}", file=sys.stderr)
        return 2
    try:
        infos, data = apply_spec(src, spec)
    except (ApplyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"读不进来: {exc}", file=sys.stderr)
        return 2
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_zip(dest, infos, data)
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
