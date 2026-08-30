"""记忆关联：同轮共现连边，检索时带上邻居。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from witty_agent.logging import get_logger

logger = get_logger("memory")
LINKS_NAME = "LINKS.jsonl"


def add_cooccurrence_links(directory: Path, slugs: list[str], *, reason: str = "same-turn") -> int:
    names = sorted({item for item in slugs if item})
    if len(names) < 2:
        return 0
    known = {(item["from"], item["to"]) for item in load_links(directory)}
    added = 0
    path = directory / LINKS_NAME
    with path.open("a", encoding="utf-8") as fh:
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                pair = (left, right)
                if pair in known:
                    continue
                fh.write(
                    json.dumps(
                        {"from": left, "to": right, "reason": reason, "at": date.today().isoformat()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                known.add(pair)
                added += 1
    if added:
        logger.info("记忆连边 added=%s", added)
    return added


def load_links(directory: Path) -> list[dict[str, str]]:
    path = directory / LINKS_NAME
    if not path.is_file():
        return []
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("from") and item.get("to"):
            rows.append(
                {
                    "from": str(item["from"]),
                    "to": str(item["to"]),
                    "reason": str(item.get("reason") or ""),
                    "at": str(item.get("at") or ""),
                }
            )
    return rows


def neighbors(directory: Path, slug: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for item in load_links(directory):
        other = ""
        if item["from"] == slug:
            other = item["to"]
        elif item["to"] == slug:
            other = item["from"]
        if other and other not in seen:
            seen.add(other)
            found.append(other)
    return found
