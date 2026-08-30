"""在已有 .docx 上留痕改稿（w:ins / w:del），以及接受 / 拒绝修订。

公文要走起草→审核→会签→签发，审核阶段的改动必须能在 Word 的「审阅」里看见、
能逐条接受或拒绝。本脚本直接改 zip 里的 word/document.xml，其余部件原样复制，
所以纸张、版心、固定行距、run 上的字距、w:eastAsia 都不会掉。

用沙箱解释器跑（纯标准库，不依赖 python-docx）：

    <沙箱 Python> revise.py --input 原稿.docx --output 送审稿.docx --spec 改动.json
    <沙箱 Python> revise.py --input 送审稿.docx --list
    <沙箱 Python> revise.py --input 送审稿.docx --output 定稿.docx --accept
    <沙箱 Python> revise.py --input 送审稿.docx --output 退回稿.docx --reject

--help-spec 打印 spec 字段。

**不要用 pandoc --track-changes=accept 代替 --accept。** 实测（pandoc 3.9.0.2）
它是「解析成 AST 再重新生成」，公文过一遍：29 段→20 段、9 个起间隔作用的空段全没了、
sectPr 整个丢失（纸张/版心/网格全无）、11 个压字距的 run 归零、25 处 w:eastAsia 归零。
接受修订必须在 XML 上做手术，不能靠转换。

退出码 0 表示写出成功；1 表示 spec 里有定位失败的条目；2 表示文件读不进来。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
DOC_PART = "word/document.xml"
SETTINGS_PART = "word/settings.xml"

# 默认作者/日期。日期必须是 ISO 8601 带 Z，Word 对格式挑，缺 Z 会显示成「未知时间」。
DEFAULT_AUTHOR = "审核人"
DEFAULT_DATE = "2026-01-01T00:00:00Z"

# 能透明穿过、里面的 run 仍算「可见」的容器。w:del 不在里面：它里面的字已是删除态。
TRANSPARENT = ("w:ins", "w:hyperlink")


def qn(name: str) -> str:
    prefix, local = name.split(":", 1)
    if prefix == "w":
        return f"{{{W}}}{local}"
    if prefix == "xml":
        return "{http://www.w3.org/XML/1998/namespace}" + local
    raise ValueError(name)


def el(name: str, **attrs: str) -> ET.Element:
    node = ET.Element(qn(name))
    for key, value in attrs.items():
        node.set(qn(key.replace("_", ":")), value)
    return node


class ReviseError(Exception):
    """定位失败或改不了的结构，带人能看懂的原因。"""


# ---------------------------------------------------------------- zip 读写


def register_ns(raw: bytes) -> None:
    """把原文件声明过的前缀注册回 ElementTree，否则写出来全是 ns0: ns1:。

    Word 能认 ns0:，但文件 diff 会整个变红，别人拿去手改也看不懂。
    """
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9_.\-]+)="([^"]+)"', raw[:4096]):
        ET.register_namespace(prefix.decode(), uri.decode())


def read_docx(path: Path) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            return infos, {info.filename: zf.read(info.filename) for info in infos}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReviseError(f"读不了 {path}：{exc}") from exc


def serialize(root: ET.Element) -> bytes:
    """按 Word 的写法出字节：自己写声明，standalone="yes"。"""
    body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body


def write_docx(path: Path, infos: list[zipfile.ZipInfo], parts: dict[str, bytes]) -> None:
    """按原顺序、原压缩方式写回，只有被改过的部件换内容。

    保持顺序是为了 [Content_Types].xml 仍在第一项——有些解析器只认这一种排法。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        seen = set()
        for info in infos:
            seen.add(info.filename)
            fresh = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            fresh.compress_type = info.compress_type
            fresh.external_attr = info.external_attr
            zf.writestr(fresh, parts[info.filename])
        for name, data in parts.items():
            if name not in seen:
                zf.writestr(name, data)


# ---------------------------------------------------------------- 段与 run


def run_text(run: ET.Element) -> str:
    return "".join(t.text or "" for t in run.findall(qn("w:t")))


def set_run_text(run: ET.Element, text: str) -> None:
    for old in run.findall(qn("w:t")):
        run.remove(old)
    node = ET.SubElement(run, qn("w:t"))
    node.text = text
    node.set(qn("xml:space"), "preserve")


def splittable(run: ET.Element) -> bool:
    """只有「rPr + 若干 w:t」的 run 能按字符切开。

    带 w:tab / w:br / w:drawing / 域字符的 run 切开会改变渲染，宁可报错也不猜。
    """
    allowed = {qn("w:rPr"), qn("w:t")}
    return all(child.tag in allowed for child in run)


def visible_runs(para: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    """段内现在还看得见的 run，按文档顺序返回 (run, 直接父元素)。"""
    out: list[tuple[ET.Element, ET.Element]] = []
    for child in list(para):
        if child.tag == qn("w:r"):
            out.append((child, para))
        elif child.tag in {qn(name) for name in TRANSPARENT}:
            for sub in list(child):
                if sub.tag == qn("w:r"):
                    out.append((sub, child))
    return out


def para_text(para: ET.Element) -> str:
    return "".join(run_text(run) for run, _ in visible_runs(para))


def split_at(para: ET.Element, pos: int) -> None:
    """在可见文字第 pos 个字符处切出 run 边界。正好落在边界上就什么都不做。"""
    if pos <= 0:
        return
    for run, parent in visible_runs(para):
        text = run_text(run)
        if pos < len(text):
            if not splittable(run):
                raise ReviseError(
                    f"要切的位置落在一个含制表符/换行/图形的 run 里，切不了：{text!r}"
                )
            right = copy.deepcopy(run)
            set_run_text(run, text[:pos])
            set_run_text(right, text[pos:])
            parent.insert(list(parent).index(run) + 1, right)
            return
        pos -= len(text)
        if pos == 0:
            return


def isolate(para: ET.Element, start: int, end: int) -> list[tuple[ET.Element, ET.Element]]:
    """把 [start,end) 这段可见文字变成若干整 run，返回它们和各自父元素。"""
    split_at(para, end)
    split_at(para, start)
    picked: list[tuple[ET.Element, ET.Element]] = []
    offset = 0
    for run, parent in visible_runs(para):
        width = len(run_text(run))
        if offset >= start and offset + width <= end and width:
            picked.append((run, parent))
        offset += width
    return picked


def first_rpr(para: ET.Element) -> ET.Element | None:
    """段内第一个 run 的 rPr，用来给新插入的文字套同样的字体字号字距。

    公文正文的 run 上挂着 w:spacing w:val="-5"（压 0.25pt 才排得下 28 字）。
    新插入的 run 不抄这个 rPr，那一行就只排 27 字，而且没有任何提示。
    """
    for run, _ in visible_runs(para):
        rpr = run.find(qn("w:rPr"))
        if rpr is not None:
            return copy.deepcopy(rpr)
    return None


# 7.3.3 的四层序数。层次标题各有自己的字体（黑体/楷体/仿宋/仿宋），
# 新增段抄错模板就会出现「仿宋的一级标题」——见 template_para()。
LEVELS = (
    re.compile(r"^[一二三四五六七八九十百]+、"),
    re.compile(r"^（[一二三四五六七八九十百]+）"),
    re.compile(r"^\d+[．.]\s*\S"),
    re.compile(r"^（\d+）"),
)


def level_of(text: str) -> int:
    """层次序数第几层，0 表示普通正文段。"""
    stripped = text.strip()
    for depth, pattern in enumerate(LEVELS, 1):
        if pattern.match(stripped):
            return depth
    return 0


def template_para(
    paras: list[ET.Element], anchor: ET.Element, forward: bool, new_text: str
) -> ET.Element:
    """新增段抄哪一段的格式。

    两条规则，按顺序：

    1. 新段本身是层次标题（「四、」「（二）」…）就抄**同层**的现成标题。这样拿到的是
       本文档实际在用的字体，不用在脚本里写死「黑体」——生成时用 --font-* 换过字体的
       文档也跟着对。抄成正文的话会出现一段仿宋的一级标题，只有 --mode gongwen 查得出。
    2. 否则：定位段本身是正文就抄它；定位段是层次标题或空段，就往插入的那一侧找最近的
       正文段。无脑抄定位段的话，在「一、巡检范围」后插一段正文会抄到 3 号黑体。
    """
    def text_of(para: ET.Element) -> str:
        return para_text(para).strip()

    index = paras.index(anchor)
    want = level_of(new_text)
    if want:
        same = [step for step, para in enumerate(paras) if level_of(text_of(para)) == want]
        if same:
            return paras[min(same, key=lambda step: abs(step - index))]

    def usable(para: ET.Element) -> bool:
        text = text_of(para)
        return bool(text) and not level_of(text)

    if usable(anchor):
        return anchor
    order = range(index + 1, len(paras)) if forward else range(index - 1, -1, -1)
    for step in order:
        if usable(paras[step]):
            return paras[step]
    return anchor


def make_run(text: str, rpr: ET.Element | None) -> ET.Element:
    run = el("w:r")
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    set_run_text(run, text)
    return run


def parent_within(para: ET.Element, node: ET.Element) -> ET.Element:
    for holder in para.iter():
        for sub in holder:
            if sub is node:
                return holder
    return para


# ---------------------------------------------------------------- 留痕原语


class Marker:
    """发号器。w:id 在整篇里唯一，接着原有最大号往上排。"""

    def __init__(self, root: ET.Element, author: str, date: str) -> None:
        used = [
            int(node.get(qn("w:id")))
            for node in root.iter()
            if node.get(qn("w:id"), "").isdigit()
        ]
        self.next = max(used, default=0) + 1
        self.author = author
        self.date = date

    def make(self, kind: str) -> ET.Element:
        node = el(f"w:{kind}", w_id=str(self.next), w_author=self.author, w_date=self.date)
        self.next += 1
        return node


def mark_deleted(run: ET.Element, parent: ET.Element, marker: Marker) -> None:
    """把一个 run 包进 w:del，并把 w:t 换成 w:delText。

    w:del 里必须是 w:delText。留成 w:t 的话 Word 打开会把这段字当成还在，
    接受修订时删不掉——校验脚本 --mode revise 查的就是这条。
    """
    index = list(parent).index(run)
    parent.remove(run)
    for node in run.findall(qn("w:t")):
        node.tag = qn("w:delText")
    wrapper = marker.make("del")
    wrapper.append(run)
    parent.insert(index, wrapper)


def mark_inserted(run: ET.Element, parent: ET.Element, index: int, marker: Marker) -> None:
    wrapper = marker.make("ins")
    wrapper.append(run)
    parent.insert(index, wrapper)


def insertion_point(
    para: ET.Element, parent: ET.Element, anchor: ET.Element, marker: Marker
) -> tuple[ET.Element, int]:
    """新插入的 w:ins 该挂在哪、排第几。

    anchor 落在 w:ins / w:hyperlink 里时不能直接往容器里塞：w:ins 套 w:ins 是非法的
    （多轮会签就会撞上——第二个人改第一个人插入的字），塞进 w:hyperlink 又会让新字
    也变成链接的一部分。做法是把容器在 anchor 前切开，新内容放到容器外面。
    """
    if parent is para:
        return para, list(para).index(anchor)
    grand = parent_within(para, parent)
    kids = list(parent)
    at = kids.index(anchor)
    if at > 0:                       # anchor 前面还有兄弟，先把容器劈成两半
        head = copy.deepcopy(parent)
        for extra in list(head)[at:]:
            head.remove(extra)
        for moved in kids[:at]:
            parent.remove(moved)
        if head.tag == qn("w:ins"):  # 劈出来的那半要换新号，w:id 全篇唯一
            head.set(qn("w:id"), str(marker.next))
            marker.next += 1
        grand.insert(list(grand).index(parent), head)
    return grand, list(grand).index(parent)


def mark_para_end(para: ET.Element, marker: Marker, kind: str) -> None:
    """给段落标记本身打 ins / del。

    删掉段落标记 = 「本段并入下一段」。所以整段删除 = 每个 run 包 w:del + 这一下。
    只包 run 不管段落标记，接受修订后会剩一个空段，自动编号里就是一个空项。

    w:del / w:ins 必须是 rPr 的第一个子元素，schema 里这个顺序是强制的，
    放错位置 Word 会当整个 rPr 无效。
    """
    ppr = para.find(qn("w:pPr"))
    if ppr is None:
        ppr = el("w:pPr")
        para.insert(0, ppr)
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = el("w:rPr")
        # rPr 在 pPr 里排得很后，但前面那些兄弟都是可选的，直接 append 即可；
        # 唯一要守的是 rPr 自己内部 w:del/w:ins 在最前。
        ppr.append(rpr)
    for stale in rpr.findall(qn(f"w:{kind}")):
        rpr.remove(stale)
    rpr.insert(0, marker.make(kind))


# ---------------------------------------------------------------- 操作


def find_hits(paras: list[ET.Element], needle: str) -> list[tuple[int, int]]:
    """在每一段的可见文字里找 needle，返回 (段序号, 段内偏移)。"""
    hits: list[tuple[int, int]] = []
    for index, para in enumerate(paras):
        text = para_text(para)
        start = text.find(needle)
        while start >= 0:
            hits.append((index, start))
            start = text.find(needle, start + 1)
    return hits


def pick(hits: list[tuple[int, int]], op: dict, needle: str) -> list[tuple[int, int]]:
    if not hits:
        raise ReviseError(f"找不到 {needle!r}。整串要落在同一个自然段里，跨段找不到")
    if op.get("all"):
        return hits
    nth = int(op.get("nth", 1))
    if nth < 1 or nth > len(hits):
        raise ReviseError(f"{needle!r} 全篇出现 {len(hits)} 次，取不到第 {nth} 次")
    return [hits[nth - 1]]


def apply_op(root: ET.Element, op: dict, marker: Marker) -> str:
    kind = op.get("op")
    needle = op.get("find")
    if not isinstance(needle, str) or not needle:
        raise ReviseError(f"{kind}: find 必须是非空字符串")
    body = root.find(qn("w:body"))
    paras = list(root.iter(qn("w:p")))
    targets = pick(find_hits(paras, needle), op, needle)
    done = []

    for para_index, start in reversed(targets):   # 从后往前改，前面的偏移才不会被带跑
        para = paras[para_index]
        if kind in {"replace", "delete"}:
            text = op.get("text", "") if kind == "replace" else ""
            if kind == "replace" and not isinstance(text, str):
                raise ReviseError("replace: text 必须是字符串")
            runs = isolate(para, start, start + len(needle))
            if not runs:
                raise ReviseError(f"{needle!r} 切不出独立 run，可能夹在域或图形里")
            anchor, parent = runs[0]
            if text:
                holder, at = insertion_point(para, parent, anchor, marker)
                mark_inserted(make_run(text, first_rpr(para)), holder, at, marker)
            for run, run_parent in runs:
                mark_deleted(run, run_parent, marker)
            done.append(f"{kind} {needle!r}→{text!r}" if text else f"delete {needle!r}")

        elif kind == "delete_para":
            runs = visible_runs(para)
            if not runs:
                raise ReviseError(f"{needle!r} 所在段没有可见文字，不用删")
            gone = para_text(para)[:20]      # 先记下来：包完 w:del 就读不到可见文字了
            for run, parent in runs:
                mark_deleted(run, parent, marker)
            mark_para_end(para, marker, "del")
            done.append(f"delete_para {gone!r}")

        elif kind in {"insert_after", "insert_before"}:
            lines = op.get("text")
            lines = [lines] if isinstance(lines, str) else list(lines or [])
            if not lines:
                raise ReviseError(f"{kind}: text 不能为空")
            parent = parent_of(root, para)
            if parent is None:
                raise ReviseError("定位段找不到父元素")
            base = list(parent).index(para) + (1 if kind == "insert_after" else 0)
            like = op.get("like")
            fixed = None
            if isinstance(like, str) and like:
                spots = find_hits(paras, like)
                if not spots:
                    raise ReviseError(f"like 指定的 {like!r} 找不到")
                fixed = paras[spots[0][0]]
            models = []
            for offset, line in enumerate(lines):
                # 每行单独挑模板：一次插进去的几段可能层次不同（标题 + 正文）。
                model = fixed or template_para(paras, para, kind == "insert_after", line)
                models.append(para_text(model)[:10])
                fresh = el("w:p")
                ppr = model.find(qn("w:pPr"))
                if ppr is not None:       # 抄版式：左空二字、固定行距、对齐全在这里
                    fresh.append(copy.deepcopy(ppr))
                mark_inserted(make_run(line, first_rpr(model)), fresh, len(list(fresh)), marker)
                mark_para_end(fresh, marker, "ins")
                parent.insert(base + offset, fresh)
            done.append(f"{kind} {len(lines)} 段（格式抄自 {'、'.join(repr(m) for m in models)}）")

        else:
            raise ReviseError(f"不认识的 op：{kind!r}")

    if body is None:
        raise ReviseError("document.xml 里没有 w:body")
    return "；".join(reversed(done))


def parent_of(root: ET.Element, child: ET.Element) -> ET.Element | None:
    for node in root.iter():
        for sub in node:
            if sub is child:
                return node
    return None


# ---------------------------------------------------------------- 接受 / 拒绝


def parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {sub: node for node in root.iter() for sub in node}


def merge_into_next(para: ET.Element, pmap: dict[ET.Element, ET.Element]) -> bool:
    """段落标记被删（或被拒的插入）时，把本段内容并进下一段，本段消失。

    下一段的段落属性留着——Word 就是这样，存活的是后一个段落标记。
    同一父元素里没有下一段（比如表格最后一格）就并不了，返回 False，只把标记去掉。
    """
    parent = pmap.get(para)
    if parent is None:
        return False
    siblings = list(parent)
    index = siblings.index(para)
    nxt = next((node for node in siblings[index + 1:] if node.tag == qn("w:p")), None)
    if nxt is None:
        return False
    keep = [child for child in list(para) if child.tag != qn("w:pPr")]
    for offset, child in enumerate(keep):
        para.remove(child)
        nxt.insert(offset + (1 if nxt.find(qn("w:pPr")) is not None else 0), child)
    parent.remove(para)
    return True


def strip_revisions(
    node: ET.Element, drop: str, keep: str, accept: bool, stat: dict[str, int]
) -> None:
    """递归处理 run 级留痕：drop 那种整块扔掉，keep 那种拆掉外壳留下内容。

    必须递归、且先处理里层。多轮会签会套起来：第一个人插入的字被第二个人删掉，
    结构就是 w:ins > w:del。按「取一份子元素快照再遍历」的平铺写法，
    外层 w:ins 拆开后里层 w:del 就落到已经遍历过的位置上，再也不会被处理——
    接受修订后那段本该消失的字会重新出现在正文里，而且看不出是哪儿来的。
    """
    for child in list(node):
        if child.tag == drop:
            node.remove(child)
            stat["del" if accept else "ins"] += 1
        elif child.tag == keep:
            strip_revisions(child, drop, keep, accept, stat)   # 先掏干净里层
            if not accept:                                     # 拒绝：删除的字要恢复
                for text in child.iter(qn("w:delText")):
                    text.tag = qn("w:t")
            index = list(node).index(child)
            for offset, sub in enumerate(list(child)):
                node.insert(index + offset, sub)
            node.remove(child)
            stat["ins" if accept else "del"] += 1
        elif child.tag != qn("w:rPr"):
            # 跳过 w:rPr：那里的 w:del/w:ins 是段落标记的标记，不是内容包装，
            # 当普通删除抹掉的话第二步就看不到了，被删的段落会留下一个空段。
            strip_revisions(child, drop, keep, accept, stat)


def resolve(root: ET.Element, accept: bool) -> dict[str, int]:
    """在 XML 上直接接受或拒绝全部修订，其他一切不动。"""
    stat = {"ins": 0, "del": 0, "para": 0, "fmt": 0}
    pmap = parent_map(root)

    # 一、run 级：接受=删 w:del 留 w:ins 内容；拒绝=删 w:ins 留 w:del 内容。
    drop, keep = (qn("w:del"), qn("w:ins")) if accept else (qn("w:ins"), qn("w:del"))
    strip_revisions(root, drop, keep, accept, stat)
    if not accept:
        for text in list(root.iter(qn("w:delText"))):    # 兜底：不在 w:del 里的残留
            text.tag = qn("w:t")

    # 二、段落标记级：并段还是留段，取决于「删」这个动作有没有被采纳。
    for para in list(root.iter(qn("w:p"))):
        rpr = para.find("./w:pPr/w:rPr", NS)
        if rpr is None:
            continue
        marked_del = rpr.find(qn("w:del"))
        marked_ins = rpr.find(qn("w:ins"))
        for stale in (marked_del, marked_ins):
            if stale is not None:
                rpr.remove(stale)
        join = (marked_del is not None) if accept else (marked_ins is not None)
        if join:
            stat["para"] += 1
            merge_into_next(para, pmap)

    # 三、格式修订：pPrChange / rPrChange 记的是「改之前」的属性。
    # 接受就把记录扔掉，拒绝就用它把属性还原回去。
    #
    # 还原时不能把 w:pPr 整个清空再填——`w:pPrChange` 里那份 `w:pPr` 是 schema 里的
    # `CT_PPrBase`，**装不下 `w:rPr` 和 `w:sectPr`**。清空重填会顺手抹掉段落标记自己的
    # 字体字号，段落是节里最后一段时还会把 `w:sectPr` 一起抹掉——纸张版心网格全丢，
    # 就是 `docx-traps.md` 里 pandoc 那条毛病，只不过换成自己犯。
    # 这两个要留下，且按 schema 顺序（基础属性…, w:rPr, w:sectPr）放回去。
    keep_last = (qn("w:rPr"), qn("w:sectPr"))
    for holder, change, inner in (
        (qn("w:pPr"), qn("w:pPrChange"), qn("w:pPr")),
        (qn("w:rPr"), qn("w:rPrChange"), qn("w:rPr")),
    ):
        for props in list(root.iter(holder)):
            record = props.find(change)
            if record is None:
                continue
            stat["fmt"] += 1
            props.remove(record)
            if accept:
                continue
            survivors = [c for c in props if c.tag in keep_last]
            for child in list(props):
                props.remove(child)
            old = record.find(inner)
            for child in list(old) if old is not None else []:
                if child.tag not in keep_last:
                    props.append(child)
            for child in survivors:
                props.append(child)
    return stat


# ---------------------------------------------------------------- 列出


def list_revisions(root: ET.Element) -> list[str]:
    lines: list[str] = []
    for para_index, para in enumerate(root.iter(qn("w:p")), 1):
        # 整段删除时可见文字为空，上下文要连 w:delText 一起读，否则只显示「（空段）」。
        context = "".join(
            node.text or ""
            for node in para.iter()
            if node.tag in {qn("w:t"), qn("w:delText")}
        )[:24] or "（空段）"
        for node in para.iter():
            if node.tag not in {qn("w:ins"), qn("w:del")}:
                continue
            who = node.get(qn("w:author")) or "?"
            when = (node.get(qn("w:date")) or "?")[:10]
            rid = node.get(qn("w:id")) or "?"
            text = "".join(
                t.text or ""
                for t in list(node.iter(qn("w:t"))) + list(node.iter(qn("w:delText")))
            )
            kind = "插入" if node.tag == qn("w:ins") else "删除"
            if not text:
                kind += "段落标记"
                text = f"（本段并入下一段）第{para_index}段：{context}"
            lines.append(f"#{rid:>3} {kind} {who} {when}  {text}")
    return lines


# ---------------------------------------------------------------- 入口

SPEC_HELP = """spec 是 UTF-8 JSON：

{
  "author": "审核人",                 // 可省，默认「审核人」
  "date": "2026-08-20T20:00:00Z",     // 可省。ISO 8601 带 Z，缺 Z 的 Word 认不出
  "ops": [
    {"op": "replace",       "find": "甲方",       "text": "乙方"},
    {"op": "replace",       "find": "本市",       "text": "全市", "all": true},
    {"op": "replace",       "find": "现将",       "text": "兹将", "nth": 2},
    {"op": "delete",        "find": "另行通知。"},
    {"op": "delete_para",   "find": "这段整段删掉"},
    {"op": "insert_after",  "find": "定位到这段", "text": ["新增一段", "再一段"]},
    {"op": "insert_before", "find": "定位到这段", "text": "插在它前面"}
  ]
}

find 是要找的原文，整串必须落在同一个自然段里。同一串出现多次时默认改第一次，
用 nth 指定第几次（1 开始），或 all:true 全改。
delete_para / insert_* 的 find 只用来定位段落，不必是整段。

insert_* 新段会抄定位段的 w:pPr 和第一个 run 的 w:rPr，所以左空二字、固定行距、
压字距都跟着走。不抄的话新段会用 Normal 样式，那一行只排 27 字。
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="在已有 .docx 上留痕改稿，或接受/拒绝修订",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", help="原 .docx")
    parser.add_argument("--output", help="输出 .docx")
    parser.add_argument("--spec", help="改动 JSON，见 --help-spec")
    parser.add_argument("--author", help="留痕作者，覆盖 spec 里的 author")
    parser.add_argument("--date", help="留痕时间，ISO 8601 带 Z")
    parser.add_argument("--accept", action="store_true", help="接受全部修订")
    parser.add_argument("--reject", action="store_true", help="拒绝全部修订")
    parser.add_argument("--list", action="store_true", help="列出现有修订，不改文件")
    parser.add_argument(
        "--no-track-future",
        action="store_true",
        help="不往 settings.xml 写 w:trackChanges（默认写，让审核人接着改也留痕）",
    )
    parser.add_argument("--help-spec", action="store_true", help="打印 spec 字段")
    args = parser.parse_args(argv)

    if args.help_spec:
        print(SPEC_HELP)
        return 0
    if not args.input:
        parser.error("要 --input")
    if sum(bool(x) for x in (args.spec, args.accept, args.reject, args.list)) != 1:
        parser.error("--spec / --accept / --reject / --list 选且只选一个")
    if not args.list and not args.output:
        parser.error("要 --output")

    try:
        infos, parts = read_docx(Path(args.input))
        raw = parts.get(DOC_PART)
        if raw is None:
            raise ReviseError(f"{args.input} 里没有 {DOC_PART}，不是 .docx")
        register_ns(raw)
        root = ET.fromstring(raw)
    except ReviseError as exc:
        print(f"读不进来：{exc}", file=sys.stderr)
        return 2
    except ET.ParseError as exc:
        print(f"document.xml 解析失败：{exc}", file=sys.stderr)
        return 2

    if args.list:
        lines = list_revisions(root)
        print("\n".join(lines) if lines else "没有修订留痕")
        return 0

    try:
        if args.spec:
            spec = json.loads(Path(args.spec).read_text("utf-8"))
            marker = Marker(
                root,
                args.author or spec.get("author") or DEFAULT_AUTHOR,
                args.date or spec.get("date") or DEFAULT_DATE,
            )
            ops = spec.get("ops") or []
            if not ops:
                raise ReviseError("spec 里 ops 是空的")
            # 先全做完再打印：有一条定位失败就整份不写出，半份留痕比不留痕更难查。
            applied = [f"  {i}. {apply_op(root, op, marker)}" for i, op in enumerate(ops, 1)]
            print("\n".join(applied))
            if not args.no_track_future:
                set_track_changes(parts)
        else:
            stat = resolve(root, accept=args.accept)
            verb = "接受" if args.accept else "拒绝"
            print(
                f"  {verb}：插入 {stat['ins']} 处、删除 {stat['del']} 处、"
                f"段落标记 {stat['para']} 处、格式 {stat['fmt']} 处"
            )
    except ReviseError as exc:
        print(f"改不了：{exc}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, OSError) as exc:
        print(f"spec 读不了：{exc}", file=sys.stderr)
        return 2

    parts[DOC_PART] = serialize(root)
    write_docx(Path(args.output), infos, parts)
    print(f"写出 {args.output}")
    return 0


def set_track_changes(parts: dict[str, bytes]) -> None:
    """往 settings.xml 加 w:trackChanges，让接手的人继续改也自动留痕。

    这个开关的意思是「以后的编辑要跟踪」，不是「文档里有修订」。不加也能看见
    已有的 w:ins/w:del，但审核人手改一句就是无痕的，痕迹链断在这里且没有提示。
    """
    raw = parts.get(SETTINGS_PART)
    if raw is None or b"<w:trackChanges" in raw:
        return
    # w:trackChanges 在 CT_Settings 里排得很靠前，插在根元素开标签之后最稳。
    match = re.search(rb"<w:settings\b[^>]*>", raw)
    if match:
        parts[SETTINGS_PART] = (
            raw[: match.end()] + b"<w:trackChanges/>" + raw[match.end():]
        )


if __name__ == "__main__":
    sys.exit(main())
