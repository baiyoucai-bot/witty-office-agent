"""原子写：同目录临时文件 + rename，替换 inode 带明确 mode。"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

_DEFAULT_MODE = 0o644


def _resolve_mode(target: Path, mode: int | None) -> int:
    if mode is not None:
        return stat.S_IMODE(mode)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return _DEFAULT_MODE
    if stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    return _DEFAULT_MODE


def write_file_atomic(
    path: Path | str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    bits = _resolve_mode(target, mode)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    handle = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        handle = os.open(tmp, flags, bits)
        if hasattr(os, "fchmod"):
            os.fchmod(handle, bits)
        with os.fdopen(handle, "w", encoding=encoding) as stream:
            handle = None
            stream.write(content)
        os.replace(tmp, target)
    except Exception:
        if handle is not None:
            os.close(handle)
        tmp.unlink(missing_ok=True)
        raise
