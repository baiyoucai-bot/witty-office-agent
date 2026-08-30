"""Python 3.11+ 用标准库 tomllib；3.10 用 tomli。"""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib  # type: ignore[no-redef]
