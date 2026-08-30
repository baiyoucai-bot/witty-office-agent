"""从 JSON spec 写一本新的 .xlsx（inlineStr + 公式，不经 pandas）。

    <沙箱 Python> write.py --spec 表.json --output 表.xlsx
    <沙箱 Python> write.py --help-spec

公式写成字符串，不要先算成数字再写入。交付前用 check_xlsx / recalc 核一遍。

退出码 0 写出成功；2 spec 不合法。
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from xlsx_parts import SSML, col_letters, qn

PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

SPEC_HELP = """spec JSON：

{
  "sheets": [
    {
      "name": "汇总",
      "rows": [
        ["项目", "数量", "单价", "金额"],
        ["甲", 2, 10, "=B2*C2"]
      ]
    }
  ]
}

以 = 开头的格子写成公式（XML 里不带 =）。其它字符串走 inlineStr，数字走 <v>。
"""

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="1"><xf xfId="0"/></cellXfs>
</styleSheet>
"""


def _is_formula(value: object) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _cell(ref: str, value: object) -> ET.Element:
    if _is_formula(value):
        node = ET.Element(qn("c"), {"r": ref})
        formula = ET.SubElement(node, qn("f"))
        formula.text = str(value)[1:]
        return node
    if isinstance(value, bool):
        node = ET.Element(qn("c"), {"r": ref, "t": "b"})
        v_el = ET.SubElement(node, qn("v"))
        v_el.text = "1" if value else "0"
        return node
    if isinstance(value, (int, float)):
        node = ET.Element(qn("c"), {"r": ref})
        v_el = ET.SubElement(node, qn("v"))
        v_el.text = str(value)
        return node
    node = ET.Element(qn("c"), {"r": ref, "t": "inlineStr"})
    is_el = ET.SubElement(node, qn("is"))
    t_el = ET.SubElement(is_el, qn("t"))
    t_el.text = "" if value is None else str(value)
    return node


def _sheet_xml(rows: list) -> bytes:
    root = ET.Element(qn("worksheet"))
    data = ET.SubElement(root, qn("sheetData"))
    for r_index, row in enumerate(rows, start=1):
        if not isinstance(row, list):
            raise ValueError(f"第 {r_index} 行不是数组")
        row_el = ET.SubElement(data, qn("row"), {"r": str(r_index)})
        for c_index, value in enumerate(row, start=1):
            if value is None:
                continue
            ref = f"{col_letters(c_index)}{r_index}"
            row_el.append(_cell(ref, value))
    ET.register_namespace("", SSML)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _workbook_xml(names: list[str]) -> bytes:
    sheets: list[str] = []
    for index, name in enumerate(names, start=1):
        safe = (
            str(name)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace('"', "&quot;")
        )
        sheets.append(f'<sheet name="{safe}" sheetId="{index}" r:id="rId{index}"/>')
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{SSML}" xmlns:r="{REL_NS}">'
        f'<sheets>{"".join(sheets)}</sheets></workbook>'
    )
    return xml.encode("utf-8")


def _workbook_rels(count: int) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Relationships xmlns="{PKG_REL}">',
    ]
    for index in range(1, count + 1):
        parts.append(
            f'<Relationship Id="rId{index}" Type="{OFFICE_REL}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    parts.append(
        f'<Relationship Id="rId{count + 1}" Type="{OFFICE_REL}/styles" Target="styles.xml"/>'
    )
    parts.append("</Relationships>")
    return "\n".join(parts)


def _content_types(count: int) -> str:
    body = CONTENT_TYPES.replace(
        "</Types>",
        "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, count + 1)
        )
        + "</Types>",
    )
    return body


def write_spec(spec: dict, dest: Path) -> None:
    sheets = spec.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("spec.sheets 必须是非空数组")
    names: list[str] = []
    payloads: dict[str, bytes] = {}
    for index, sheet in enumerate(sheets, start=1):
        if not isinstance(sheet, dict):
            raise ValueError(f"sheets[{index}] 不是对象")
        name = str(sheet.get("name") or f"Sheet{index}")
        rows = sheet.get("rows")
        if not isinstance(rows, list):
            raise ValueError(f"{name}.rows 必须是数组")
        names.append(name)
        payloads[f"xl/worksheets/sheet{index}.xml"] = _sheet_xml(rows)
    payloads["[Content_Types].xml"] = _content_types(len(names)).encode("utf-8")
    payloads["_rels/.rels"] = ROOT_RELS.encode("utf-8")
    payloads["xl/workbook.xml"] = _workbook_xml(names)
    payloads["xl/_rels/workbook.xml.rels"] = _workbook_rels(len(names)).encode("utf-8")
    payloads["xl/styles.xml"] = STYLES.encode("utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, payload in payloads.items():
            zout.writestr(name, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 spec 写新 xlsx")
    parser.add_argument("--spec")
    parser.add_argument("--output")
    parser.add_argument("--help-spec", action="store_true")
    args = parser.parse_args(argv)
    if args.help_spec:
        print(SPEC_HELP)
        return 0
    if not args.spec or not args.output:
        print("需要 --spec --output，或 --help-spec", file=sys.stderr)
        return 2
    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"找不到 spec: {spec_path}", file=sys.stderr)
        return 2
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        write_spec(spec, Path(args.output))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
