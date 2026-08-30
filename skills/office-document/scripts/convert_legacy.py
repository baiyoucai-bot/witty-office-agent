"""老格式 Office 文件转新格式：.doc/.wps/.rtf → .docx，.xls/.et → .xlsx，.ppt/.dps → .pptx。

    <沙箱 Python> convert_legacy.py --input 老.doc
    <沙箱 Python> convert_legacy.py --input 甲.doc 乙.xls --outdir 目录

用本机 LibreOffice headless 转（本机没有 soffice，命令是 libreoffice）。
转出的新文件再交给 word-docx / excel-xlsx / witty-ppt-skills 的脚本处理；
本脚本只转格式，不改内容，转完自己核对一眼。

退出码 0 全部转出；2 没有 LibreOffice / 不认识的扩展名 / 转换失败。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TARGETS = {
    ".doc": "docx",
    ".wps": "docx",
    ".rtf": "docx",
    ".xls": "xlsx",
    ".et": "xlsx",
    ".ppt": "pptx",
    ".dps": "pptx",
}


def find_office() -> str | None:
    for name in ("libreoffice", "soffice"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def convert(exe: str, src: Path, outdir: Path, profile: Path) -> Path:
    fmt = TARGETS[src.suffix.lower()]
    cmd = [
        exe,
        "--headless",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to",
        fmt,
        "--outdir",
        str(outdir),
        str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    dest = outdir / f"{src.stem}.{fmt}"
    if proc.returncode != 0 or not dest.is_file():
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"{src.name} 转换失败: {err or '没有产出文件'}")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="老格式 Office 转新格式")
    parser.add_argument("--input", nargs="+", required=True)
    parser.add_argument("--outdir", default="", help="默认写到各输入文件旁边")
    args = parser.parse_args(argv)
    sources = [Path(item) for item in args.input]
    for src in sources:
        if not src.is_file():
            print(f"找不到文件: {src}", file=sys.stderr)
            return 2
        if src.suffix.lower() not in TARGETS:
            supported = " ".join(sorted(TARGETS))
            print(f"不认识的扩展名 {src.suffix}（只认 {supported}）", file=sys.stderr)
            return 2
    exe = find_office()
    if not exe:
        print("本机没有 libreoffice / soffice，转不了老格式", file=sys.stderr)
        return 2
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        for src in sources:
            outdir = Path(args.outdir) if args.outdir else src.parent
            outdir.mkdir(parents=True, exist_ok=True)
            try:
                dest = convert(exe, src, outdir, profile)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(str(exc), file=sys.stderr)
                failed = True
                continue
            print(dest)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
