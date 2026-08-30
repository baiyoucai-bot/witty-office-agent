"""长篇小说状态库：类型化记录 + 折叠出的当前状态 + 章节安全检索 + 确定性校验。

业务插件的库层，不注册工具、不进内核循环，也不调模型——这一层跑起来免费、瞬时、
可重复，所以能当 goal 模式的客观 gate。工具包装见 plugins/novel.py。
"""

from witty_agent.plugins.novel_kit.check import (
    Coverage,
    Finding,
    Thresholds,
    coverage,
    load_dismissals,
    run_checks,
    save_dismissal,
    worst_severity,
)
from witty_agent.plugins.novel_kit.layout import BookPaths
from witty_agent.plugins.novel_kit.records import (
    RECORD_TYPES,
    RecordError,
    append_records,
    load_records,
    max_chapter,
    normalize,
    story_ch,
    truncate_records,
    validate,
)
from witty_agent.plugins.novel_kit.registry import (
    Registry,
    build_alias_map,
    fold,
    load_registry,
    write_registry,
)
from witty_agent.plugins.novel_kit.retrieve import Bm25Index, build_index, context_pack, expand

__all__ = [
    "Bm25Index",
    "BookPaths",
    "Coverage",
    "Finding",
    "RECORD_TYPES",
    "RecordError",
    "Registry",
    "Thresholds",
    "append_records",
    "build_alias_map",
    "build_index",
    "context_pack",
    "coverage",
    "expand",
    "fold",
    "load_dismissals",
    "load_records",
    "load_registry",
    "max_chapter",
    "normalize",
    "run_checks",
    "save_dismissal",
    "story_ch",
    "truncate_records",
    "validate",
    "worst_severity",
    "write_registry",
]
