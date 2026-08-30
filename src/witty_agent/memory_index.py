"""九宫格召回的倒排索引和打分。分数仍落在原来那把整数尺子上。

分数尺子有三处读者（`runtime.toml` 的 `recalled_cover_min`、`budget_hits` 的
`min_score + 2`、`archive_min_score`），所以这儿**不换刻度**：门槛仍是 3，覆盖仍是 5，
换的只是「一个词值几分」的算法。

原算法按词长给死权重（长词 3、短词 2）。词长是「这个词有多специфи」的坏代理：中文里
绝大多数实词恰好两个字（老王、台账、电费、检修），一律 2 分，而门槛是 3——于是
**单个中文实词精确命中也召不回**，必须凑够两个。实测记忆里存着「以后都叫我老王」，
查 `老王` 返回空。

换成两条：
  * **IDF**：一个词在这份记忆里越少见越值钱。`老王` 只出现在一条里 → 高分；
    `需要` 到处都是 → 低分。这比手写 stopwords 表准，而且随记忆长自动重算。
  * **覆盖率闸门**：只命中一个实词时，要求这个词基本就是整个问句。`老王` 查 `老王`
    覆盖 100%，放行；`长篇小说推荐` 撞上记忆里的 `长篇铺垫` 只覆盖 33%，拦下。
    这道闸门守的正是 PROGRESS 里记的那个取舍——放宽到「沾一个短词」会让
    `生产者消费者` 撞出 `生产环境` 红线，实测假命中 2/32 → 11/32。闸门让「精确命中
    一个专名」和「长句蹭到一个碎片」分开，不必为了后者牺牲前者。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path


_LATIN_TOKEN = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RUN = re.compile(r"[A-Za-z0-9_]{2,}")
# IDF 把权重放大/缩小的区间。上限 2 让稀有词够得着覆盖线 5，下限 0.6 让烂大街的词掉下来。
_IDF_MIN = 0.6
_IDF_MAX = 2.0


@dataclass(frozen=True)
class IndexedBullet:
    slug: str
    title: str
    text: str
    terms: frozenset[str]


@dataclass
class MemoryIndex:
    """一份记忆目录的倒排索引。`signature` 变了就重建。"""

    bullets: tuple[IndexedBullet, ...] = ()
    postings: dict[str, set[int]] = field(default_factory=dict)
    signature: tuple = ()
    # 语料攒到多少条之后，df 才够格当「稀有」的判据。见 `_distinctive`。
    rare_corpus_min: int = 25
    rare_df_ratio: float = 0.05

    @property
    def total(self) -> int:
        return len(self.bullets)

    def candidates(self, tokens: list[str]) -> set[int]:
        found: set[int] = set()
        for token in tokens:
            hit = self.postings.get(token.casefold())
            if hit:
                found |= hit
        return found

    def doc_freq(self, term: str) -> int:
        return len(self.postings.get(term.casefold()) or ())


def query_tokens(query: str, stopwords: tuple[str, ...] = ()) -> list[str]:
    """问句切成候选词：拉丁串整取，中文出 2/3 字滑窗。

    滑窗会切出跨词边界的碎片（`长篇小说` 里的 `篇小`），这是没有分词器的代价。碎片由
    `score_bullet` 的「被更长命中词包住就不计分」和覆盖率闸门收拾，不在这儿处理。
    """
    stop = {item.casefold() for item in stopwords}
    tokens: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        key = item.casefold()
        if not key or key in stop or key in seen:
            return
        seen.add(key)
        tokens.append(item)

    for latin in _LATIN_RUN.findall(query or ""):
        add(latin)
    for block in _CJK.findall(query or ""):
        if 2 <= len(block) <= 4:
            add(block)
        for size in (3, 2):
            if len(block) < size:
                continue
            for index in range(0, len(block) - size + 1):
                add(block[index : index + size])
    return tokens


def _maximal(matched: list[str]) -> list[str]:
    """丢掉被更长命中词包住的碎片。

    一个 3 字词命中时它自己的两个 2-gram 也一定命中，各记一次的话**一个词的碎片顶过
    两个真词**。只数没被包住的。
    """
    return [
        item
        for item in matched
        if not any(item != other and item in other for other in matched)
    ]


def _base_weight(term: str) -> int:
    # 拉丁 3-4 字多是 csv / sql / api 这类泛标识，别和中文专名同权。
    if len(term) >= 5 or (len(term) >= 3 and not _LATIN_TOKEN.fullmatch(term)):
        return 3
    return 2


def _distinctive(matched: list[str], index: MemoryIndex | None) -> bool:
    """命中的词是不是**在这份记忆里独一份**，独到可以只凭它就算命中。

    这是词长准入唯一的例外，专治中文的头号召回失灵：绝大多数中文实词恰好两个字
    （老王、台账、电费、检修），一律 2 分够不到门槛 3，于是**精确报出一个专名也召不回**。
    可 `回复`、`内容` 同样是两个字，放行就是 PROGRESS 里记的那批假命中。

    分开两者的是 df，不是词长——但 df 要有统计意义得先有语料。所以这条只在记忆攒到
    `rare_corpus_min` 条以后才生效，且要求命中词稀有到 `rare_df_ratio` 以下。语料小的
    时候一切照旧（那时候本来也没什么可混淆的），语料越大这条越准：**记忆越多，召回越好，
    而不是越糊**。
    """
    if index is None or index.total < index.rare_corpus_min:
        return False
    ceiling = max(1, int(index.total * index.rare_df_ratio))
    return all(0 < index.doc_freq(term) <= ceiling for term in matched)


def _idf_factor(df: int, total: int) -> float:
    """把 BM25 的 IDF 压到一个乘数上，越稀有越大。

    语料只有一两条时 IDF 退化（每个词都「稀有」），这时返回 1.0 就是原来的按词长打分——
    记忆里统共没几条，也没什么可混淆的。
    """
    if total < 2 or df <= 0:
        return 1.0
    idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
    best = math.log(1 + (total - 1 + 0.5) / 1.5)
    if best <= 0:
        return 1.0
    scaled = (idf / best) * _IDF_MAX
    return max(_IDF_MIN, min(_IDF_MAX, scaled))


def score_bullet(
    text: str,
    tokens: list[str],
    *,
    index: MemoryIndex | None = None,
    floor: int = 0,
) -> int:
    """一条子弹对这个问句值几分。返回值仍在原来的整数尺子上。

    **准入仍由词长权重说了算，IDF 只能降不能升。** 先按老规矩算一遍基础分（长词 3、
    短词 2），够不到 `floor` 就是没命中——「单个 2 字词重叠不算命中」这条精确率保证
    是实测过的（放宽会让 `生产者消费者` 撞出 `生产环境` 红线，假命中 2/32 → 11/32），
    不能让 IDF 从后门把它捅开：语料小的时候每个词都「稀有」，`回复` 和 `老王` 一样高分。

    过了准入之后，IDF 才参与算分，作用是**排序和降权**：两个稀有词重叠冲到 8 分够得着
    `recalled_cover_min`，两个烂大街的词重叠掉到 2 分反而被滤掉。所以 IDF 在这儿只会
    让精确率更好，不会拿精确率换召回。
    """
    body = text.casefold()
    matched = _maximal([folded for token in tokens if (folded := token.casefold()) in body])
    if not matched:
        return 0
    base = sum(_base_weight(term) for term in matched)
    if floor and base < floor and not _distinctive(matched, index):
        return 0
    if index is None:
        return base
    total = index.total
    weighted = sum(
        _base_weight(term) * _idf_factor(index.doc_freq(term), total) for term in matched
    )
    return int(round(weighted))


def build_index(
    rows: list[tuple[str, str, str]],
    bullets_of,
    scoreable,
    *,
    signature: tuple = (),
    rare_corpus_min: int = 25,
    rare_df_ratio: float = 0.05,
) -> MemoryIndex:
    """从 (slug, title, body) 建索引。切分和清洗由调用方给，避免这儿再抄一份。"""
    items: list[IndexedBullet] = []
    postings: dict[str, set[int]] = {}
    for slug, title, body in rows:
        pieces = bullets_of(body) or ([body.strip()] if body.strip() else [])
        for piece in pieces:
            text = piece.strip()
            if not text:
                continue
            terms = _index_terms(scoreable(text))
            position = len(items)
            items.append(IndexedBullet(slug=slug, title=title, text=text, terms=terms))
            for term in terms:
                postings.setdefault(term, set()).add(position)
    return MemoryIndex(
        bullets=tuple(items),
        postings=postings,
        signature=signature,
        rare_corpus_min=rare_corpus_min,
        rare_df_ratio=rare_df_ratio,
    )


def _index_terms(text: str) -> frozenset[str]:
    """一条子弹进索引的词。和问句侧同一套切法，否则倒排表对不上。"""
    terms: set[str] = set()
    lowered = (text or "").casefold()
    for latin in _LATIN_RUN.findall(lowered):
        terms.add(latin)
    for block in _CJK.findall(lowered):
        if 2 <= len(block) <= 4:
            terms.add(block)
        for size in (3, 2):
            if len(block) < size:
                continue
            for index in range(0, len(block) - size + 1):
                terms.add(block[index : index + size])
    return frozenset(terms)


def directory_signature(directory: Path) -> tuple:
    """目录内容指纹：文件数 + 最新 mtime + 总字节。任一变化就该重建索引。

    mtime 取 `st_mtime_ns`，不是浮点秒。收割是「写完立刻检索」的节奏，浮点秒截到毫秒
    的话，同一毫秒内把一条子弹换成等长的另一条就撞出同一个指纹，索引不重建，检索读的
    还是旧内容。
    """
    if not directory or not directory.is_dir():
        return ()
    count = 0
    newest = 0
    size = 0
    for path in directory.glob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest = max(newest, stat.st_mtime_ns)
        size += stat.st_size
    return (count, newest, size)


def expand_aliases(query: str, aliases: dict[str, tuple[str, ...]]) -> str:
    """按配置表把同义说法拼进问句，纯字面召回也就能跨一点说法差异。

    只做**加法**：原句一个字不动，把命中的同义词接在后面，让它们的 n-gram 也进候选。
    这不是语义检索，只是把「本月 / 这个月 / 当月」这类固定说法在配置里打通。
    """
    text = query or ""
    if not text or not aliases:
        return text
    extra: list[str] = []
    for word, mates in aliases.items():
        if word and word in text:
            extra.extend(mate for mate in mates if mate and mate not in text)
    if not extra:
        return text
    return f"{text} {' '.join(dict.fromkeys(extra))}"
