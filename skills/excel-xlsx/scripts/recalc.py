"""用 LibreOffice 重算公式缓存值。没有它就退出 2，不要假装算过。

本机没有 soffice，只有 libreoffice（与 word-docx 相同）。

    <沙箱 Python> recalc.py --input 表.xlsx --output 表-算过.xlsx

openpyxl / 本技能的 write.py 只写公式字符串，Excel/WPS 打开会自己算；
要在无 GUI 的环境里核缓存值，才需要这一步。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _bin() -> str | None:
    return shutil.which("libreoffice") or shutil.which("soffice")


def recalc(src: Path, dest: Path) -> None:
    exe = _bin()
    if not exe:
        raise RuntimeError("本机没有 libreoffice / soffice，无法重算。打开 Excel/WPS 也会自动算。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / src.name
        copied.write_bytes(src.read_bytes())
        cmd = [exe, "--headless", "--calc", "--convert-to", "xlsx", "--outdir", tmp, str(copied)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "libreoffice 失败")
        produced = Path(tmp) / copied.name
        if not produced.is_file():
            xlsx = list(Path(tmp).glob("*.xlsx"))
            if not xlsx:
                raise RuntimeError("libreoffice 没写出 xlsx")
            produced = xlsx[0]
        dest.write_bytes(produced.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LibreOffice 重算 xlsx 公式")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    src = Path(args.input)
    if not src.is_file():
        print(f"找不到文件: {src}", file=sys.stderr)
        return 2
    try:
        recalc(src, Path(args.output))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
