"""书目录布局。一本书就是一个工作区目录，git 可管、可回滚。

`state/records.jsonl` 是唯一事实源，`state/registry.json` 完全由它折叠而来。
这样「改了第 12 章要级联失效下游」退化成「截断到第 12 章再重放」，
不用写失效传播，也天然满足中途崩了能续跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BOOK_FILE = "book.toml"
STATE_DIR = "state"
RECORDS_NAME = "records.jsonl"
REGISTRY_NAME = "registry.json"
DISMISSALS_NAME = "dismissals.json"
MANUSCRIPT_DIR = "manuscript"
OUTLINE_DIR = "outline"
CHAPTER_OUTLINE_DIR = "ch"
REPORTS_DIR = "reports"


def chapter_stem(chapter: int) -> str:
    return f"{int(chapter):04d}"


@dataclass(frozen=True)
class BookPaths:
    root: Path

    @classmethod
    def at(cls, root: str | Path) -> BookPaths:
        return cls(Path(root).expanduser().resolve())

    @property
    def book_file(self) -> Path:
        return self.root / BOOK_FILE

    @property
    def state(self) -> Path:
        return self.root / STATE_DIR

    @property
    def records(self) -> Path:
        return self.state / RECORDS_NAME

    @property
    def registry(self) -> Path:
        return self.state / REGISTRY_NAME

    @property
    def dismissals(self) -> Path:
        return self.state / DISMISSALS_NAME

    @property
    def manuscript(self) -> Path:
        return self.root / MANUSCRIPT_DIR

    @property
    def outline(self) -> Path:
        return self.root / OUTLINE_DIR

    @property
    def chapter_outlines(self) -> Path:
        return self.outline / CHAPTER_OUTLINE_DIR

    @property
    def reports(self) -> Path:
        return self.root / REPORTS_DIR

    def chapter_file(self, chapter: int) -> Path:
        return self.manuscript / f"{chapter_stem(chapter)}.md"

    def chapter_outline_file(self, chapter: int) -> Path:
        return self.chapter_outlines / f"{chapter_stem(chapter)}.md"

    def report_file(self, chapter: int) -> Path:
        return self.reports / f"{chapter_stem(chapter)}-continuity.md"

    def existing_chapters(self) -> list[int]:
        """磁盘上实际有正文的章号。用来对账「写了但没入库」。"""
        if not self.manuscript.is_dir():
            return []
        out: list[int] = []
        for path in self.manuscript.glob("*.md"):
            if path.stem.isdigit():
                out.append(int(path.stem))
        return sorted(out)

    def is_book(self) -> bool:
        return self.state.is_dir() or self.book_file.is_file()

    def ensure(self) -> None:
        for path in (self.state, self.manuscript, self.chapter_outlines, self.reports):
            path.mkdir(parents=True, exist_ok=True)
