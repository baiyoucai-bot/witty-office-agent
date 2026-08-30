"""在已有 .docx 上加批注（w:comment），支持回复串、已解决标记、列出和删除。

批注和修订留痕是两件事：留痕是「我替你改了」，批注是「这里我有话说，你自己定」。
审核意见里「请补充责任部门」这类不该直接替人写死的，就该走批注。

用沙箱解释器跑（纯标准库，不依赖 python-docx）：

    <沙箱 Python> comment.py --input 稿.docx --output 批注稿.docx --spec 批注.json
    <沙箱 Python> comment.py --input 批注稿.docx --list
    <沙箱 Python> comment.py --input 批注稿.docx --output 清理稿.docx --delete 3
    <沙箱 Python> comment.py --input 批注稿.docx --output 定稿.docx --strip

--help-spec 打印 spec 字段。

和 revise.py 一样只动该动的部件，其余按原顺序原压缩方式复制回去，所以纸张、版心、
固定行距、字距、w:eastAsia、以及已有的修订留痕都不会掉。

**沙箱 python-docx 1.2.0 有 add_comment()，但不够用**：它只能锚在整个 run 上
（要批注半句话得自己先切 run），且只写 word/comments.xml——不写
commentsExtended.xml，于是**回复串和「已解决」都没有**。它可以拿来当独立读取端
交叉验证（`Document(路径).comments`），写还是走本脚本。

退出码 0 表示写出成功；1 表示 spec 里有定位失败的条目；2 表示文件读不进来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from revise import (  # noqa: E402  复用同一套定位和切 run，免得两边算法漂移
    NS,
    ReviseError,
    el,
    find_hits,
    first_rpr,
    isolate,
    para_text,
    pick,
    qn,
    read_docx,
    register_ns,
    serialize,
    write_docx,
)

DOC_PART = "word/document.xml"
CMT_PART = "word/comments.xml"
EXT_PART = "word/commentsExtended.xml"
RELS_PART = "word/_rels/document.xml.rels"
CT_PART = "[Content_Types].xml"

# 命名空间。w15 是 2012 年加的扩展，回复串和「已解决」都在那里。
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

# 必须注册前缀，否则 ElementTree 写出来是 `ns0:commentsEx` / `ns0:paraId`。
# 实测那样写出的 commentsExtended.xml，Word 当整个部件无效——**不报错**，
# 就是回复串和「已解决」全丢。在根元素上塞一个 `xmlns:w15` 属性没有用：
# ElementTree 把它当普通属性写出去，前缀还是 ns0，反而多一个假的 xmlns。
ET.register_namespace("w15", W15)
ET.register_namespace("w14", W14)

# 关系类型和内容类型。这两串抄错不报错，只是 Word 打开时当没有批注——
# 实测值取自沙箱 python-docx 1.2.0 写出的文件，不是凭记忆写的。
REL_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
REL_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
CT_COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CT_EXTENDED = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
)

SPEC_HELP = """批注 spec 是一个 JSON 对象：

{
  "author": "李审核",              // 默认作者，每条可覆盖
  "initials": "LS",                // 批注框上显示的缩写，默认取 author 前两字
  "date": "2026-08-21T09:00:00Z",  // 默认时间，ISO 8601 带 Z
  "comments": [
    {"find": "有关事项", "text": "这里要写清责任部门"},
    {"find": "九月三十日", "text": "时间够吗", "nth": 2},
    {"find": "务求实效", "text": "同意", "reply_to": 1},
    {"find": "巡检范围", "text": "已按意见补充", "done": true},
    {"find": "各单位", "text": "抄送范围也要加", "all": true}
  ]
}

字段：
  find      要批注的原文串，必须落在同一个自然段里
  text      批注内容，多段用 \\n 分开
  nth       该串全篇出现多次时取第几次，默认 1
  all       true 表示所有出现处都批注，和 nth 互斥
  author    覆盖默认作者
  initials  覆盖默认缩写
  date      覆盖默认时间
  reply_to  回复哪一条批注（填 --list 里的 id），构成回复串
  done      true 表示这条标成「已解决」

回复串和「已解决」靠 commentsExtended.xml 记，Word / WPS 才认。只写
comments.xml 的话回复会摊平成一堆独立批注，看不出是在回谁。
"""


# ---------------------------------------------------------------- 部件读写


def parse_part(parts: dict[str, bytes], name: str) -> ET.Element | None:
    raw = parts.get(name)
    if raw is None:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ReviseError(f"{name} 解析失败：{exc}") from exc


def empty_comments() -> ET.Element:
    return el("w:comments")


def empty_extended() -> ET.Element:
    return ET.Element(f"{{{W15}}}commentsEx")


def ensure_rel(parts: dict[str, bytes], target: str, rel_type: str) -> None:
    """document.xml.rels 里补一条关系，已经有了就不动。

    少这条关系时 Word 打开完全不提示，就是看不见批注——部件在 zip 里躺着没人引用。
    """
    root = parse_part(parts, RELS_PART)
    if root is None:
        raise ReviseError(f"缺 {RELS_PART}，不是正常的 .docx")
    tag = f"{{{PKG_REL}}}Relationship"
    for rel in root.findall(tag):
        if rel.get("Target") == target and rel.get("Type") == rel_type:
            return
    used = {rel.get("Id", "") for rel in root.findall(tag)}
    number = 1
    while f"rId{number}" in used:
        number += 1
    ET.SubElement(root, tag, {"Id": f"rId{number}", "Type": rel_type, "Target": target})
    ET.register_namespace("", PKG_REL)
    parts[RELS_PART] = serialize(root)


def ensure_content_type(parts: dict[str, bytes], part_name: str, ctype: str) -> None:
    """[Content_Types].xml 里补 Override。

    用字符串拼而不是解析成树再写回：这个部件的默认命名空间一改写法，
    有些解析器就不认了，而它的结构简单到不值得为此冒风险。
    """
    raw = parts.get(CT_PART)
    if raw is None:
        raise ReviseError(f"缺 {CT_PART}，不是正常的 .docx")
    text = raw.decode("utf-8")
    if f'PartName="/{part_name}"' in text:
        return
    entry = f'<Override PartName="/{part_name}" ContentType="{ctype}"/>'
    parts[CT_PART] = text.replace("</Types>", entry + "</Types>").encode("utf-8")


# ---------------------------------------------------------------- 批注内容


def next_id(comments: ET.Element) -> int:
    used = [int(c.get(qn("w:id"), "-1")) for c in comments.findall(qn("w:comment"))]
    return max(used, default=-1) + 1


def para_id(seed: int) -> str:
    """w14:paraId：8 位十六进制，全篇唯一，且不能是 00000000。

    commentsExtended 靠它认「这条回复在回谁」，所以批注正文的最后一段必须有。
    这里按序号推，不用随机数——同样的输入要出同样的文件，方便 diff 和复现。
    """
    value = (seed * 0x9E3779B1 + 0x1234567) & 0xFFFFFFFF
    return f"{value or 1:08X}"


def comment_body(text: str, rpr: ET.Element | None, index: int) -> list[ET.Element]:
    """批注正文：一段一个 w:p，第一段带 annotationRef（批注框里的引用标记）。

    末段挂 w14:paraId，commentsExtended 用它定位。
    """
    lines = text.split("\n") or [""]
    paras: list[ET.Element] = []
    for offset, line in enumerate(lines):
        para = el("w:p")
        ppr = ET.SubElement(para, qn("w:pPr"))
        ET.SubElement(ppr, qn("w:pStyle")).set(qn("w:val"), "CommentText")
        if offset == 0:
            ref = ET.SubElement(para, qn("w:r"))
            ref_rpr = ET.SubElement(ref, qn("w:rPr"))
            ET.SubElement(ref_rpr, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
            ET.SubElement(ref, qn("w:annotationRef"))
        run = ET.SubElement(para, qn("w:r"))
        if rpr is not None:
            run.append(rpr)
        node = ET.SubElement(run, qn("w:t"))
        node.text = line
        node.set(qn("xml:space"), "preserve")
        paras.append(para)
    paras[-1].set(f"{{{W14}}}paraId", para_id(index))
    return paras


def add_comment_part(
    comments: ET.Element, cid: int, text: str, who: str, initials: str, when: str
) -> str:
    node = ET.SubElement(
        comments,
        qn("w:comment"),
        {
            qn("w:id"): str(cid),
            qn("w:author"): who,
            qn("w:initials"): initials,
            qn("w:date"): when,
        },
    )
    for para in comment_body(text, None, cid):
        node.append(para)
    return node[-1].get(f"{{{W14}}}paraId", "")


def add_extended(ext: ET.Element, pid: str, parent_pid: str | None, done: bool) -> None:
    """commentsEx 里记一条：这段批注解决了没有、在回谁。

    w15:paraIdParent 缺了就不是回复，Word 会把它当一条独立批注平铺显示。
    """
    attrs = {f"{{{W15}}}paraId": pid, f"{{{W15}}}done": "1" if done else "0"}
    if parent_pid:
        attrs[f"{{{W15}}}paraIdParent"] = parent_pid
    ET.SubElement(ext, f"{{{W15}}}commentEx", attrs)


# ---------------------------------------------------------------- 锚点


def place_anchor(para: ET.Element, start: int, end: int, cid: int) -> str:
    """在正文里放 commentRangeStart / End / Reference 三件套。

    批注只能锚在整 run 上，所以先用 revise.py 的 isolate() 把要批注的字切成独立 run。
    Anthropic 那份技能是打印一段 XML 片段让人自己贴进 document.xml——那一步最容易
    贴错位置，贴错了批注就锚在别的字上，或者干脆不显示。这里自动做。

    三件套少任何一个的后果都不一样，都不报错：
      - 少 Start / End：批注不知道锚在哪，Word 当它是文档级批注或者不显示
      - 少 Reference：审阅窗格里有内容，正文里没有那个可点的小标记
    """
    runs = isolate(para, start, end)
    if not runs:
        raise ReviseError("要批注的字切不出独立 run，可能夹在域或图形里")
    first_run, first_parent = runs[0]
    last_run, last_parent = runs[-1]

    at = list(first_parent).index(first_run)
    first_parent.insert(at, el("w:commentRangeStart", **{"w:id": str(cid)}))

    at = list(last_parent).index(last_run) + 1
    last_parent.insert(at, el("w:commentRangeEnd", **{"w:id": str(cid)}))

    # Reference 要挂在一个 run 里，且这个 run 得在 End 后面。
    holder = el("w:r")
    rpr = ET.SubElement(holder, qn("w:rPr"))
    ET.SubElement(rpr, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
    ET.SubElement(holder, qn("w:commentReference"), {qn("w:id"): str(cid)})
    last_parent.insert(at + 1, holder)
    return para_text(para)[start:end]


def apply_comment(
    root: ET.Element,
    comments: ET.Element,
    ext: ET.Element,
    item: dict,
    defaults: dict,
    pid_by_id: dict[int, str],
) -> str:
    needle = item.get("find")
    text = item.get("text")
    if not isinstance(needle, str) or not needle:
        raise ReviseError("find 必须是非空字符串")
    if not isinstance(text, str) or not text:
        raise ReviseError(f"{needle!r}: text 必须是非空字符串")
    if item.get("all") and item.get("nth"):
        raise ReviseError(f"{needle!r}: all 和 nth 不能同时给")

    who = item.get("author") or defaults["author"]
    initials = item.get("initials") or defaults["initials"] or who[:2]
    when = item.get("date") or defaults["date"]

    parent_pid = None
    if item.get("reply_to") is not None:
        target = int(item["reply_to"])
        parent_pid = pid_by_id.get(target)
        if not parent_pid:
            known = ", ".join(str(k) for k in sorted(pid_by_id)) or "无"
            raise ReviseError(
                f"reply_to={target} 找不到对应批注（现有 id：{known}）。"
                "先用 --list 看 id，回复必须挂在已经存在的批注上"
            )

    paras = list(root.iter(qn("w:p")))
    targets = pick(find_hits(paras, needle), item, needle)
    done = []
    # 从后往前放锚点：先动后面的，前面的偏移才不会被切 run 带跑。
    for para_index, start in reversed(targets):
        para = paras[para_index]
        cid = next_id(comments)
        quoted = place_anchor(para, start, start + len(needle), cid)
        pid = add_comment_part(comments, cid, text, who, initials, when)
        add_extended(ext, pid, parent_pid, bool(item.get("done")))
        pid_by_id[cid] = pid
        label = f"回复#{item['reply_to']} " if parent_pid else ""
        done.append(f"{label}#{cid} {who}：{text.splitlines()[0][:20]} → 锚在 {quoted!r}")
    return "；".join(reversed(done))


# ---------------------------------------------------------------- 列出与删除


def list_comments(parts: dict[str, bytes]) -> list[str]:
    comments = parse_part(parts, CMT_PART)
    if comments is None or not len(comments):
        return []
    root = parse_part(parts, DOC_PART)
    ext = parse_part(parts, EXT_PART)

    anchored: dict[str, str] = {}
    if root is not None:
        anchored = anchor_text(root)

    state: dict[str, tuple[bool, str | None]] = {}
    if ext is not None:
        for node in ext.findall(f"{{{W15}}}commentEx"):
            state[node.get(f"{{{W15}}}paraId", "")] = (
                node.get(f"{{{W15}}}done") == "1",
                node.get(f"{{{W15}}}paraIdParent"),
            )
    pid_to_id = {}
    for node in comments.findall(qn("w:comment")):
        last = list(node)[-1] if len(node) else None
        if last is not None:
            pid_to_id[last.get(f"{{{W14}}}paraId", "")] = node.get(qn("w:id"), "?")

    lines = []
    for node in comments.findall(qn("w:comment")):
        cid = node.get(qn("w:id"), "?")
        who = node.get(qn("w:author"), "?")
        when = (node.get(qn("w:date")) or "")[:10]
        body = " ".join(
            "".join(t.text or "" for t in p.iter(qn("w:t"))) for p in node.findall(qn("w:p"))
        ).strip()
        last = list(node)[-1] if len(node) else None
        pid = last.get(f"{{{W14}}}paraId", "") if last is not None else ""
        resolved, parent = state.get(pid, (False, None))
        mark = "✓已解决 " if resolved else ""
        reply = f"回复#{pid_to_id.get(parent, '?')} " if parent else ""
        where = anchored.get(cid)
        at = f"锚在 {where!r}" if where else "★没有锚点★"
        lines.append(f"#{cid:>3} {mark}{reply}{who} {when}  {body[:40]}  {at}")
    return lines


def anchor_text(root: ET.Element) -> dict[str, str]:
    """每条批注锚住的那几个字，靠 Start / End 之间的可见文字算。

    锚点可以跨段（Start 在一段、End 在下一段），所以按文档顺序线性扫，
    不能按段落分别处理。
    """
    open_ids: dict[str, list[str]] = {}
    out: dict[str, str] = {}
    for node in root.iter():
        if node.tag == qn("w:commentRangeStart"):
            open_ids[node.get(qn("w:id"), "")] = []
        elif node.tag == qn("w:commentRangeEnd"):
            cid = node.get(qn("w:id"), "")
            out[cid] = "".join(open_ids.pop(cid, []))
        elif node.tag == qn("w:t"):
            for buf in open_ids.values():
                buf.append(node.text or "")
    for cid, buf in open_ids.items():          # Start 有 End 没有，也要报出来
        out[cid] = "".join(buf) + "（End 缺失）"
    return out


def drop_anchor(root: ET.Element, cid: str) -> int:
    """把某条批注的三件套从正文里摘掉。空壳 run 一起收走。"""
    removed = 0
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag in (qn("w:commentRangeStart"), qn("w:commentRangeEnd")):
                if child.get(qn("w:id")) == cid:
                    parent.remove(child)
                    removed += 1
            elif child.tag == qn("w:r"):
                refs = [
                    r
                    for r in child.findall(qn("w:commentReference"))
                    if r.get(qn("w:id")) == cid
                ]
                for ref in refs:
                    child.remove(ref)
                    removed += 1
                # 只剩 rPr 的空 run 留着会在正文里占一个空位置，收掉。
                if refs and all(sub.tag == qn("w:rPr") for sub in child):
                    parent.remove(child)
    return removed


def delete_comments(parts: dict[str, bytes], root: ET.Element, ids: set[str]) -> str:
    comments = parse_part(parts, CMT_PART)
    if comments is None:
        raise ReviseError("这份文档没有批注")
    ext = parse_part(parts, EXT_PART)

    gone, pids = [], set()
    for node in list(comments.findall(qn("w:comment"))):
        cid = node.get(qn("w:id"), "")
        if ids and cid not in ids:
            continue
        last = list(node)[-1] if len(node) else None
        if last is not None:
            pids.add(last.get(f"{{{W14}}}paraId", ""))
        comments.remove(node)
        drop_anchor(root, cid)
        gone.append(cid)

    if ids - set(gone):
        raise ReviseError(f"找不到批注 id：{', '.join(sorted(ids - set(gone)))}")

    if ext is not None:
        for node in list(ext.findall(f"{{{W15}}}commentEx")):
            if node.get(f"{{{W15}}}paraId") in pids:
                ext.remove(node)
            elif node.get(f"{{{W15}}}paraIdParent") in pids:
                # 父批注被删了，回复留着但不再是回复——否则指向一个不存在的 paraId。
                del node.attrib[f"{{{W15}}}paraIdParent"]
        parts[EXT_PART] = serialize(ext)
    parts[CMT_PART] = serialize(comments)
    return f"删掉 {len(gone)} 条批注（id {', '.join(gone)}）"


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在已有 .docx 上加 / 列 / 删批注")
    parser.add_argument("--input", help="输入 .docx")
    parser.add_argument("--output", help="输出 .docx")
    parser.add_argument("--spec", help="批注 JSON，见 --help-spec")
    parser.add_argument("--author", help="默认作者，覆盖 spec 里的 author")
    parser.add_argument("--date", help="默认时间，ISO 8601 带 Z")
    parser.add_argument("--list", action="store_true", help="列出现有批注，不改文件")
    parser.add_argument("--delete", help="删掉这些 id 的批注，逗号分开")
    parser.add_argument("--strip", action="store_true", help="删掉全部批注")
    parser.add_argument("--help-spec", action="store_true", help="打印 spec 字段")
    args = parser.parse_args(argv)

    if args.help_spec:
        print(SPEC_HELP)
        return 0
    if not args.input:
        parser.error("要 --input")
    modes = (args.spec, args.list, args.delete, args.strip)
    if sum(bool(x) for x in modes) != 1:
        parser.error("--spec / --list / --delete / --strip 选且只选一个")
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
        lines = list_comments(parts)
        print("\n".join(lines) if lines else "没有批注")
        return 0

    try:
        if args.delete or args.strip:
            ids = set()
            if args.delete:
                ids = {piece.strip() for piece in args.delete.split(",") if piece.strip()}
            note = delete_comments(parts, root, ids)
            applied = [f"  {note}"]
        else:
            applied = add_from_spec(parts, root, args)
    except ReviseError as exc:
        print(f"没写出任何文件：{exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"spec 读不了：{exc}", file=sys.stderr)
        return 2

    parts[DOC_PART] = serialize(root)
    write_docx(Path(args.output), infos, parts)
    print(f"{args.input} → {args.output}")
    print("\n".join(applied))
    return 0


def add_from_spec(parts: dict[str, bytes], root: ET.Element, args) -> list[str]:
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    items = spec.get("comments")
    if not isinstance(items, list) or not items:
        raise ReviseError("spec 里要有非空的 comments 数组")

    author = args.author or spec.get("author")
    if not author:
        raise ReviseError("要给 author：批注框上不写谁提的，评审看不出是谁的意见")
    defaults = {
        "author": author,
        "initials": spec.get("initials", ""),
        "date": args.date or spec.get("date") or "",
    }
    if not defaults["date"]:
        raise ReviseError("要给 date（ISO 8601 带 Z），不写 Word 显示成未知时间")

    # 不能写 `parse_part(...) or empty_comments()`：**没有子元素的 ElementTree 元素为假**，
    # 已有 comments.xml 但里面一条批注都没有时，`or` 会走右边换成一个新的空树。
    # 那样 rels 和 Content_Types 里的条目还指着旧部件，写出来的文件里批注对不上号。
    comments = parse_part(parts, CMT_PART)
    if comments is None:
        comments = empty_comments()
    ext = parse_part(parts, EXT_PART)
    if ext is None:
        ext = empty_extended()

    # 已有批注的 id → paraId，reply_to 要靠它找父批注。
    pid_by_id: dict[int, str] = {}
    for node in comments.findall(qn("w:comment")):
        last = list(node)[-1] if len(node) else None
        if last is not None:
            pid_by_id[int(node.get(qn("w:id"), "-1"))] = last.get(f"{{{W14}}}paraId", "")

    # 先全做完再写：有一条定位失败就整份不写出，半份批注比没批注更难查。
    applied = [
        f"  {i}. {apply_comment(root, comments, ext, item, defaults, pid_by_id)}"
        for i, item in enumerate(items, 1)
    ]

    parts[CMT_PART] = serialize(comments)
    parts[EXT_PART] = serialize(ext)
    ensure_rel(parts, "comments.xml", REL_COMMENTS)
    ensure_rel(parts, "commentsExtended.xml", REL_EXTENDED)
    ensure_content_type(parts, CMT_PART, CT_COMMENTS)
    ensure_content_type(parts, EXT_PART, CT_EXTENDED)
    ensure_styles(parts)
    return applied


def ensure_styles(parts: dict[str, bytes]) -> None:
    """styles.xml 里补 CommentReference / CommentText 两个样式。

    引用的样式不存在时 Word 不报错，按 Normal 渲染——公文的 Normal 是 3 号仿宋，
    批注框里就会出现一堆和正文一样大的字，挤得看不清。
    """
    raw = parts.get("word/styles.xml")
    if raw is None:
        return
    text = raw.decode("utf-8")
    add = []
    if 'w:styleId="CommentReference"' not in text:
        add.append(
            '<w:style w:type="character" w:styleId="CommentReference">'
            '<w:name w:val="annotation reference"/>'
            "<w:rPr><w:sz w:val=\"16\"/><w:szCs w:val=\"16\"/></w:rPr></w:style>"
        )
    if 'w:styleId="CommentText"' not in text:
        add.append(
            '<w:style w:type="paragraph" w:styleId="CommentText">'
            '<w:name w:val="annotation text"/>'
            '<w:pPr><w:spacing w:line="240" w:lineRule="auto"/></w:pPr>'
            "<w:rPr><w:sz w:val=\"18\"/><w:szCs w:val=\"18\"/></w:rPr></w:style>"
        )
    if add:
        parts["word/styles.xml"] = text.replace(
            "</w:styles>", "".join(add) + "</w:styles>"
        ).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
