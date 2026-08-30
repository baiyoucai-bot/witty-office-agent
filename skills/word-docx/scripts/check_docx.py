"""校验 .docx 版式：通用陷阱 + GB/T 9704-2012 公文格式。

只读，不改输入文件。判定线见同技能 references/gongwen-format.md。

用沙箱解释器跑：

    <沙箱 Python> check_docx.py --input 通知.docx
    <沙箱 Python> check_docx.py --input 通知.docx --mode gongwen
    <沙箱 Python> check_docx.py --input 别人给的.docx --mode basic

--mode basic（默认）只查任何 docx 都算错的东西：中文字体缺 w:eastAsia、
页码写死成数字、行距被字号带跑、run 碎片化。
--mode gongwen 追加公文版式：纸张、版心、边距、字号、行距行数、层次字体、版记。
--mode revise 追加修订留痕；--mode comment 追加批注结构。
文档里只要有批注痕迹，批注结构就一直查——孤立锚点这种错和模式无关。

退出码 0 表示没有 FAIL；1 表示有 FAIL；2 表示读不进来。
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}

# 批注相关的部件与命名空间。w15 记回复串和「已解决」，w16cid / w16cex 是 Word 2016+
# 自己加的两个部件——本技能的 comment.py 不写它们，但别人给的稿里会有，得能查。
CMT_PART = "word/comments.xml"
EXT_PART = "word/commentsExtended.xml"
IDS_PART = "word/commentsIds.xml"
CEX_PART = "word/commentsExtensible.xml"
RELS_PART = "word/_rels/document.xml.rels"
CT_PART = "[Content_Types].xml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
REL_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"

# GB/T 9704-2012 4.1，与 gongwen.py 同源
PAGE_W_MM, PAGE_H_MM = 210.0, 297.0
TOP_MM, LEFT_MM = 37.0, 28.0
BANXIN_W_MM, BANXIN_H_MM = 156.0, 225.0
RIGHT_MM = PAGE_W_MM - BANXIN_W_MM - LEFT_MM
BOTTOM_MM = PAGE_H_MM - BANXIN_H_MM - TOP_MM
MARGIN_TOL_MM = 1.0          # 标准写的是 ±1mm
LINES_PER_PAGE = 22
CHARS_PER_LINE = 28
BODY_PT = 16.0               # 3 号
TITLE_PT = 22.0              # 2 号
PT_MM = 25.4 / 72
TWIP_MM = PT_MM / 20
CJK = re.compile(r"[一-鿿]")

LEVELS = (
    ("^[一二三四五六七八九十百]+、", "黑体", 1),
    ("^（[一二三四五六七八九十百]+）", "楷体", 2),
    (r"^\d+[．.]\s*\S", "仿宋", 3),
    (r"^（\d+）", "仿宋", 4),
)

# 同一个字族在不同系统里名字不一样：仿宋 / 仿宋_GB2312 / FangSong / STFangsong 都算仿宋。
# 按顺序匹配，先长后短，否则 STFangsong 会被「宋」抢先判成宋体。
FONT_FAMILY = (
    ("小标宋", ("小标宋", "xiaobiaosong")),
    ("楷体", ("楷体", "kaiti", "kai")),
    ("仿宋", ("仿宋", "fangsong", "fang")),
    ("黑体", ("黑体", "heiti", "simhei", "hei")),
    ("宋体", ("宋体", "simsun", "songti", "song")),
)


def font_family(name: str) -> str:
    """把具体字体名归到公文用的字族，认不出就原样返回。"""
    folded = (name or "").casefold()
    if not folded:
        return ""
    for family, keys in FONT_FAMILY:
        if any(key.casefold() in folded for key in keys):
            return family
    return name



def tag(name: str) -> str:
    return f"{{{W}}}{name}"


def attr(node, name: str, default=None):
    if node is None:
        return default
    return node.get(tag(name), default)


def section(title: str) -> None:
    print()
    print(f"【{title}】")


def head(items, limit: int = 4, sep: str = ", ") -> str:
    """截断列举。报「6 处」却只列 5 个又不说还有，读的人会以为自己数错了。"""
    shown = sep.join(str(item) for item in items[:limit])
    return f"{shown} 等" if len(items) > limit else shown


class Report:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def fail(self, text: str) -> None:
        self.rows.append(f"FAIL {text}")
        print(f"  FAIL {text}")

    def warn(self, text: str) -> None:
        self.rows.append(f"WARN {text}")
        print(f"  WARN {text}")

    def ok(self, text: str) -> None:
        print(f"  OK   {text}")

    @property
    def failed(self) -> int:
        return sum(row.startswith("FAIL") for row in self.rows)

    @property
    def warned(self) -> int:
        return sum(row.startswith("WARN") for row in self.rows)


def read_parts(path: Path) -> dict[str, ElementTree.Element]:
    """读进 word/ 下的 xml，外加 rels 和 [Content_Types].xml。

    后两个不在 `word/*.xml` 里（一个后缀是 .rels，一个在包根），但批注少了它们
    就是「部件在 zip 里躺着没人引用」——Word 打开一句不提示，批注直接不显示。
    要查这一类就得把它们也读进来。
    """
    parts: dict[str, ElementTree.Element] = {}
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ValueError("包里没有 word/document.xml，不是 Word 文档")
        wanted = {RELS_PART, CT_PART}
        for name in names:
            if not ((name.startswith("word/") and name.endswith(".xml")) or name in wanted):
                continue
            try:
                parts[name] = ElementTree.fromstring(zf.read(name))
            except ElementTree.ParseError:
                continue
    return parts


def paragraph_text(para) -> str:
    return "".join(node.text or "" for node in para.iter(tag("t")))


def has_text(run) -> bool:
    """不要写 any(run.iter(tag('t')))：ElementTree 里没有子元素的元素本身为假，
    空的 <w:t> 会被判成不存在，run 就全数漏掉。只能显式判 None。"""
    return next(run.iter(tag("t")), None) is not None


def text_runs(para) -> list:
    return [run for run in para.iter(tag("r")) if has_text(run)]


def style_eastasia(parts: dict[str, ElementTree.Element]) -> dict[str, str]:
    """样式和 docDefaults 里声明的中文字体，用来判断 run 是不是靠继承拿到字体。"""
    found: dict[str, str] = {}
    styles = parts.get("word/styles.xml")
    if styles is None:
        return found
    defaults = styles.find(".//w:docDefaults//w:rPrDefault//w:rPr//w:rFonts", NS)
    if attr(defaults, "eastAsia"):
        found["__default__"] = attr(defaults, "eastAsia")
    for style in styles.iter(tag("style")):
        rfonts = style.find("./w:rPr/w:rFonts", NS)
        name = attr(style, "styleId", "")
        if name and attr(rfonts, "eastAsia"):
            found[name] = attr(rfonts, "eastAsia")
    return found


def check_fonts(doc, parts, report: Report) -> None:
    """中文字体必须显式给 w:eastAsia，否则 Word 拿主题默认字渲染，XML 看着对、打开是宋体。"""
    inherited = style_eastasia(parts)
    total = bad = inherit = 0
    samples: list[str] = []
    for para in doc.iter(tag("p")):
        pstyle = attr(para.find("./w:pPr/w:pStyle", NS), "val", "")
        for run in para.iter(tag("r")):
            text = "".join(node.text or "" for node in run.iter(tag("t")))
            if not CJK.search(text):
                continue
            total += 1
            rfonts = run.find("./w:rPr/w:rFonts", NS)
            east = attr(rfonts, "eastAsia")
            latin = attr(rfonts, "ascii") or attr(rfonts, "hAnsi")
            if east:
                continue
            if latin:
                # 最典型的 python-docx 事故：只写了 ascii/hAnsi，中文没跟上
                bad += 1
                if len(samples) < 3:
                    samples.append(f"{text[:14]}… 只声明了 {latin}")
            elif not (inherited.get(pstyle) or inherited.get("__default__")):
                inherit += 1
                if len(samples) < 3:
                    samples.append(f"{text[:14]}… 无 rFonts 且样式链没有 eastAsia")

    if not total:
        report.warn("没有中文文本 run，字体检查跳过")
        return
    if bad:
        report.fail(f"{bad}/{total} 个中文 run 写了西文字体却没写 w:eastAsia：{'；'.join(samples)}")
    elif inherit:
        report.warn(f"{inherit}/{total} 个中文 run 靠继承取字体，样式链里没有 eastAsia：{'；'.join(samples)}")
    else:
        report.ok(f"{total} 个中文 run 都有 w:eastAsia")


def check_page_field(parts, report: Report) -> None:
    """页码要用 PAGE 域，写死数字的文档一改分页就全错。"""
    footers = {name: node for name, node in parts.items() if "footer" in name}
    if not footers:
        report.warn("没有页脚，无法确认页码")
        return
    fields = 0
    literal: list[str] = []
    for name, node in footers.items():
        instr = " ".join(item.text or "" for item in node.iter(tag("instrText")))
        text = "".join(item.text or "" for item in node.iter(tag("t")))
        if "PAGE" in instr.upper():
            fields += 1
        elif re.search(r"\d", text):
            literal.append(f"{name.split('/')[-1]}：{text.strip()[:20]}")
    if fields:
        report.ok(f"{fields}/{len(footers)} 个页脚用了 PAGE 域")
    if literal:
        report.fail(f"页脚里是写死的数字，不是 PAGE 域：{'；'.join(literal)}")
    elif not fields:
        report.warn("页脚里没有 PAGE 域也没有数字，可能就是没排页码")


def check_runs_fragmented(doc, report: Report) -> None:
    """Word 会把一句话切成多个 run，查找替换按整句匹配会失效。生成侧应尽量合并。"""
    worst = (0, "")
    for para in doc.iter(tag("p")):
        runs = text_runs(para)
        if len(runs) > worst[0]:
            worst = (len(runs), paragraph_text(para)[:20])
    if worst[0] >= 8:
        report.warn(f"有段落被切成 {worst[0]} 个 run（「{worst[1]}…」），"
                    f"按可见文字做查找替换会漏，先合并 run")
    else:
        report.ok(f"run 碎片化可接受（单段最多 {worst[0]} 个）")


def check_line_spacing(doc, report: Report, *, strict: bool) -> None:
    """固定行距才撑得住每面固定行数。"""
    rules: dict[str, int] = {}
    exact_pt: dict[float, int] = {}
    for para in doc.iter(tag("p")):
        spacing = para.find("./w:pPr/w:spacing", NS)
        rule = attr(spacing, "lineRule", "auto") if spacing is not None else "none"
        rules[rule] = rules.get(rule, 0) + 1
        # 只把带正文的段落算进行距统计。分隔线那种空段会故意压到 1pt，
        # 算进来会报一条无意义的「1pt × 22 行装得下」。
        if rule == "exact" and attr(spacing, "line") and CJK.search(paragraph_text(para)):
            pt = int(attr(spacing, "line")) / 20
            exact_pt[pt] = exact_pt.get(pt, 0) + 1
    total = sum(rules.values()) or 1
    desc = "，".join(f"{k}×{v}" for k, v in sorted(rules.items(), key=lambda kv: -kv[1]))
    if not strict:
        report.ok(f"行距规则分布：{desc}")
        return
    if rules.get("exact", 0) / total < 0.9:
        report.fail(f"公文要固定行距，实际 {desc}；auto/atLeast 会随字号变，撑不住 22 行/面")
        # 不要在这里 return：行距值本身也许还踩了 22×29pt 超版心那个坑，
        # 一次跑完两条都报出来，省一轮往返。
    report.ok(f"固定行距占 {rules['exact']}/{total}")
    if not exact_pt:
        report.warn("没有带正文的固定行距段，跳过行数核算")
        return
    # 只核算用得最多的那个行距，它决定每面能排几行。
    main = max(exact_pt, key=lambda k: exact_pt[k])
    used = LINES_PER_PAGE * main
    limit = BANXIN_H_MM / PT_MM
    if used > limit:
        report.fail(f"行距 {main}pt × {LINES_PER_PAGE} 行 = {used:.2f}pt，"
                    f"超版心高 {limit:.2f}pt，每面只排得下 {int(limit // main)} 行")
    else:
        report.ok(f"主行距 {main}pt × {LINES_PER_PAGE} 行 = {used:.2f}pt ≤ 版心 {limit:.2f}pt")
    for pt in sorted(k for k in exact_pt if k != main):
        report.warn(f"另有 {exact_pt[pt]} 段用 {pt}pt 行距，混行距会打乱每面 22 行")


def check_layout(doc, report: Report) -> None:
    sect = doc.find(".//w:sectPr", NS)
    if sect is None:
        report.fail("没有 sectPr，读不到纸张和页边距")
        return
    size = sect.find("./w:pgSz", NS)
    width = int(attr(size, "w", 0) or 0) * TWIP_MM
    height = int(attr(size, "h", 0) or 0) * TWIP_MM
    if abs(width - PAGE_W_MM) > 0.5 or abs(height - PAGE_H_MM) > 0.5:
        report.fail(f"纸张 {width:.1f}×{height:.1f}mm，公文要 A4 {PAGE_W_MM:.0f}×{PAGE_H_MM:.0f}mm")
    else:
        report.ok(f"纸张 A4 {width:.1f}×{height:.1f}mm")

    mar = sect.find("./w:pgMar", NS)
    got = {k: int(attr(mar, k, 0) or 0) * TWIP_MM for k in ("top", "bottom", "left", "right")}
    want = {"top": TOP_MM, "bottom": BOTTOM_MM, "left": LEFT_MM, "right": RIGHT_MM}
    label = {"top": "天头", "bottom": "地脚", "left": "订口", "right": "切口"}
    for key in ("top", "bottom", "left", "right"):
        delta = got[key] - want[key]
        line = f"{label[key]} {got[key]:.1f}mm（标准 {want[key]:.0f}±{MARGIN_TOL_MM:.0f}mm）"
        if abs(delta) > MARGIN_TOL_MM:
            report.fail(f"{line}，差 {delta:+.1f}mm")
        else:
            report.ok(line)

    bw = width - got["left"] - got["right"]
    bh = height - got["top"] - got["bottom"]
    for name, real, target in (("版心宽", bw, BANXIN_W_MM), ("版心高", bh, BANXIN_H_MM)):
        if abs(real - target) > 2 * MARGIN_TOL_MM:
            report.fail(f"{name} {real:.1f}mm，标准 {target:.0f}mm")
        else:
            report.ok(f"{name} {real:.1f}mm")

    grid = sect.find("./w:docGrid", NS)
    if grid is None or attr(grid, "type") not in {"linesAndChars", "snapToChars"}:
        report.warn("没设文档网格（w:docGrid type=linesAndChars），Word 里看不到 22×28 的稿纸设置")
    else:
        report.ok(f"文档网格 type={attr(grid, 'type')} linePitch={attr(grid, 'linePitch')}")


def check_chars_per_line(doc, report: Report) -> None:
    """每行 28 字要靠压字距，不压就是 27 字，且没有任何报错。"""
    need = CHARS_PER_LINE * BODY_PT
    limit = BANXIN_W_MM / PT_MM
    over = need - limit
    body_runs = squeezed = 0
    for para in doc.iter(tag("p")):
        ind = para.find("./w:pPr/w:ind", NS)
        # firstLine 是磅、firstLineChars 是字。用磅缩进的文档也要查，
        # 否则「每自然段左空二字」写成 Pt(32) 的文档会被整段跳过。
        if ind is None or not (attr(ind, "firstLineChars") or attr(ind, "firstLine")):
            continue
        for run in para.iter(tag("r")):
            text = "".join(node.text or "" for node in run.iter(tag("t")))
            if not CJK.search(text):
                continue
            size = run.find("./w:rPr/w:sz", NS)
            if size is not None and abs(int(attr(size, "val", 0)) / 2 - BODY_PT) > 0.1:
                continue
            body_runs += 1
            spacing = run.find("./w:rPr/w:spacing", NS)
            val = int(attr(spacing, "val", 0) or 0)
            if val * -1 * (1 / 20) * CHARS_PER_LINE >= over:
                squeezed += 1
    if not body_runs:
        report.warn(f"没找到 {BODY_PT:.0f}pt 且首行缩进的正文段，跳过每行字数检查")
        return
    if squeezed == body_runs:
        report.ok(f"{body_runs} 个正文 run 都压了字距，每行排得下 {CHARS_PER_LINE} 字")
    else:
        report.fail(
            f"{body_runs - squeezed}/{body_runs} 个正文 run 没压字距："
            f"{CHARS_PER_LINE}×{BODY_PT:.0f}pt = {need:.0f}pt 超版心 {limit:.2f}pt "
            f"共 {over:.2f}pt，每行只排 {int(limit // BODY_PT)} 字"
        )


def check_indent_units(doc, report: Report) -> None:
    """缩进不能用 w:*Chars 那套字单位属性，实测渲染不到标准要求的位置。

    量法：同一段文字分别用两套属性写，转 PDF 后 pdftotext -bbox 量首行和回行的左边界。
    `leftChars=500 hangingChars=300`（本意首行 2 字、回行 5 字）渲染成首行 5 字、回行 8 字
    ——悬挂缩进加到了回行上，方向反了；且 `leftChars=100` 配 14pt 的 run 渲染成 1.15 字
    = 1 × 16/14，字宽取的是 Normal 样式的字号，不是本 run 的。两套都写时字单位优先。

    所以这条查的是「有没有用字单位」，不是「磅算得对不对」——后者要看渲染，XML 里看不出来。
    """
    CHAR_ATTRS = ("leftChars", "rightChars", "firstLineChars", "hangingChars")
    hits: dict[str, int] = {}
    samples: list[str] = []
    total = bad = 0
    for para in doc.iter(tag("p")):
        ind = para.find("./w:pPr/w:ind", NS)
        if ind is None:
            continue
        total += 1
        used = [name for name in CHAR_ATTRS if attr(ind, name)]
        if not used:
            continue
        bad += 1
        for name in used:
            hits[name] = hits.get(name, 0) + 1
        if len(samples) < 3:
            text = paragraph_text(para).strip()[:12] or "（空段）"
            samples.append(f"「{text}」用了 {'/'.join('w:' + n for n in used)}")
    if not total:
        report.warn("没有段落设了 w:ind，跳过缩进单位检查")
        return
    if bad:
        detail = "，".join(f"w:{k}×{v}" for k, v in sorted(hits.items(), key=lambda kv: -kv[1]))
        report.fail(
            f"{bad}/{total} 段用字单位缩进（{detail}）：{'；'.join(samples)}；"
            f"实测 hangingChars 方向反了、字宽按 Normal 样式算，要改成 w:left/w:hanging 磅值"
        )
    else:
        report.ok(f"{total} 段 w:ind 都用磅值，没用 w:*Chars")


def check_levels(doc, report: Report) -> None:
    """层次序数决定字体：一、黑体　（一）楷体　1. 和（1）仿宋。"""
    wrong: list[str] = []
    checked = 0
    for para in doc.iter(tag("p")):
        text = paragraph_text(para).strip()
        if not text:
            continue
        for pattern, want, level in LEVELS:
            if not re.match(pattern, text):
                continue
            run = next(iter(text_runs(para)), None)
            east = attr(run.find("./w:rPr/w:rFonts", NS) if run is not None else None, "eastAsia", "")
            checked += 1
            got = font_family(east)
            if got != want:
                wrong.append(f"层{level}「{text[:12]}」用了 {east or '继承'}"
                             f"{f'（判为{got}）' if got and got != east else ''}，应为{want}")
            break
    if not checked:
        report.warn("没识别到「一、」「（一）」这类层次序数，跳过层次字体检查")
    elif wrong:
        report.fail(f"{len(wrong)}/{checked} 处层次字体不对：{head(wrong, 3, '；')}")
    else:
        report.ok(f"{checked} 处层次序数的字体都对")


def check_body_size(doc, report: Report) -> None:
    sizes: dict[float, int] = {}
    for run in doc.iter(tag("r")):
        text = "".join(node.text or "" for node in run.iter(tag("t")))
        if not CJK.search(text):
            continue
        size = run.find("./w:rPr/w:sz", NS)
        if size is None:
            continue
        pt = int(attr(size, "val", 0)) / 2
        sizes[pt] = sizes.get(pt, 0) + len(text)
    if not sizes:
        report.warn("run 上没有显式字号，跳过字号检查")
        return
    main = max(sizes, key=lambda k: sizes[k])
    desc = "，".join(f"{k}pt×{v}字" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1]))
    if abs(main - BODY_PT) > 0.1:
        report.fail(f"正文主字号 {main}pt，公文要 3 号 {BODY_PT:.0f}pt；分布 {desc}")
    else:
        report.ok(f"正文主字号 {main}pt（3 号）；分布 {desc}")
    if not any(abs(k - TITLE_PT) < 0.1 for k in sizes):
        report.warn(f"没有 2 号 {TITLE_PT:.0f}pt 的文字，标题字号可能不对")


def check_banji(doc, report: Report) -> None:
    borders = []
    for para in doc.iter(tag("p")):
        bottom = para.find("./w:pPr/w:pBdr/w:bottom", NS)
        if bottom is None:
            continue
        borders.append((attr(bottom, "color", "auto"), int(attr(bottom, "sz", 0) or 0)))
    red = [b for b in borders if b[0].upper() == "FF0000"]
    black = [b for b in borders if b[0].upper() != "FF0000"]
    if red:
        report.ok(f"有红色分隔线 {len(red)} 条（sz={red[0][1]}/8 磅）")
    else:
        report.warn("没有红色分隔线，发文机关标志下缺分隔线（无发文机关标志则可忽略）")
    if not black:
        report.warn("没有版记分隔线（无抄送/印发机关则可忽略）")
    elif len(black) >= 3 and black[0][1] > black[1][1]:
        report.ok(f"版记分隔线 {len(black)} 条，首末粗中间细（{[b[1] for b in black]}/8 磅）")
    else:
        report.warn(f"版记分隔线粗细为 {[b[1] for b in black]}/8 磅，标准要首末粗 0.35mm、中间细 0.25mm")


def check_revisions(doc, report: Report, *, author: str | None) -> None:
    """修订留痕的结构。查的是「Word 会不会认、接受后会不会剩垃圾」。

    这些错都不报异常：文档能打开、审阅窗格里也看得见几条痕迹，
    只有真去接受修订的时候才发现删不掉、或者多出一个空段。
    """
    revs = [node for node in doc.iter() if node.tag in (tag("ins"), tag("del"))]
    if not revs:
        report.warn("没有任何 w:ins / w:del。改稿要送审就得留痕，见 scripts/revise.py")
        return

    # w:del 里必须是 w:delText。留成 w:t，Word 会把这段字当成还在，接受修订时删不掉。
    wrong = [node for node in doc.iter(tag("del"))
             if next(node.iter(tag("t")), None) is not None]
    if wrong:
        report.fail(f"{len(wrong)} 处 w:del 里是 w:t 而不是 w:delText，接受修订时删不掉")
    else:
        report.ok(f"{len(revs)} 处留痕，w:del 里都是 w:delText")

    stray = [node for node in doc.iter(tag("delText"))]
    inside = [node for parent in doc.iter(tag("del")) for node in parent.iter(tag("delText"))]
    if len(stray) > len(inside):
        report.fail(f"{len(stray) - len(inside)} 处 w:delText 不在 w:del 里，那段字会直接消失")

    # rPr 里的 w:del / w:ins 必须排在最前，schema 强制。放错位置整个 rPr 可能被当无效。
    misordered = 0
    for rpr in doc.iter(tag("rPr")):
        kids = [child.tag for child in rpr]
        marks = {tag("del"), tag("ins")}
        if kids and set(kids) & marks and kids[0] not in marks:
            misordered += 1
    if misordered:
        report.fail(f"{misordered} 处 rPr 里 w:del/w:ins 没排在第一个，schema 里这个顺序是强制的")

    # 作者和时间。Word 靠它们分组和按人接受，缺了就全归到「未知作者」。
    naked = [node for node in revs if not attr(node, "author") or not attr(node, "date")]
    if naked:
        report.fail(f"{len(naked)} 处留痕缺 w:author 或 w:date，审阅窗格里分不出是谁改的")
    bad_date = [attr(node, "date") for node in revs
                if attr(node, "date") and not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ?$",
                                                       attr(node, "date"))]
    if bad_date:
        report.warn(f"{len(bad_date)} 处 w:date 不是 ISO 8601（如 {bad_date[0]}），Word 可能显示成未知时间")

    ids = [attr(node, "id") for node in revs]
    dupes = len(ids) - len(set(ids))
    if dupes:
        report.warn(f"{dupes} 个 w:id 重复，逐条接受/拒绝时可能连带处理")

    authors = sorted({attr(node, "author") for node in revs if attr(node, "author")})
    if author and [a for a in authors if a != author]:
        report.fail(f"留痕作者有 {authors}，要求只能是「{author}」")
    elif authors:
        report.ok(f"留痕作者：{'、'.join(authors)}")

    # 整段被删但段落标记没标删 → 接受后剩一个空段，自动编号里就是一个空项。
    orphan = []
    for para in doc.iter(tag("p")):
        visible = "".join(node.text or "" for node in para.iter(tag("t"))).strip()
        deleted = "".join(node.text or "" for node in para.iter(tag("delText"))).strip()
        marked = para.find("./w:pPr/w:rPr/w:del", NS) is not None
        if deleted and not visible and not marked:
            orphan.append(deleted[:16])
    if orphan:
        report.warn(
            f"{len(orphan)} 段文字全删了但段落标记没标删（{orphan[0]}…）："
            f"接受后会剩一个空段。整段删除要连 pPr/rPr/w:del 一起写，见 revise.py"
        )

    nested = [node for outer in doc.iter(tag("ins")) for node in outer.iter(tag("ins"))
              if node is not outer]
    if nested:
        report.fail(f"{len(nested)} 处 w:ins 套 w:ins，不合法。多轮会签要把外层容器切开")


def has_comment_traces(doc, parts) -> bool:
    """这份文档跟批注沾没沾边。沾了就查——孤立锚点这种错和用哪个模式无关。"""
    if CMT_PART in parts or EXT_PART in parts:
        return True
    marks = (tag("commentRangeStart"), tag("commentRangeEnd"), tag("commentReference"))
    return any(node.tag in marks for node in doc.iter())


def comment_bodies(parts) -> dict[str, dict]:
    """comments.xml 里每条批注：id → {paraId, 作者, 正文}。

    paraId 取**末段**上的 w14:paraId，因为 commentsExtended / commentsIds 就是按
    末段的 paraId 认这条批注的。取首段会导致多段批注全部对不上号。
    """
    root = parts.get(CMT_PART)
    out: dict[str, dict] = {}
    if root is None:
        return out
    for node in root.findall(tag("comment")):
        paras = node.findall(tag("p"))
        last = paras[-1] if paras else None
        out[attr(node, "id", "")] = {
            "para_id": (last.get(f"{{{W14}}}paraId", "") if last is not None else ""),
            "author": attr(node, "author", ""),
            "date": attr(node, "date", ""),
            "text": " ".join(paragraph_text(p) for p in paras).strip(),
            "paras": len(paras),
        }
    return out


def comment_anchors(doc) -> tuple[dict[str, int], dict[str, int], dict[str, int], list[str]]:
    """正文里的三件套，按文档顺序线性扫。

    不能按段落分别处理：Start 在一段、End 在下一段是合法的（跨段批注），
    按段落扫会把每一条跨段批注都误判成「Start 没配 End」。
    顺序也要查——Reference 排到 End 前面，Word 里那个可点的小标记会落在错的字上。
    """
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    refs: dict[str, int] = {}
    order: list[str] = []
    for node in doc.iter():
        if node.tag == tag("commentRangeStart"):
            cid = attr(node, "id", "")
            starts[cid] = starts.get(cid, 0) + 1
            order.append(f"S{cid}")
        elif node.tag == tag("commentRangeEnd"):
            cid = attr(node, "id", "")
            ends[cid] = ends.get(cid, 0) + 1
            order.append(f"E{cid}")
        elif node.tag == tag("commentReference"):
            cid = attr(node, "id", "")
            refs[cid] = refs.get(cid, 0) + 1
            order.append(f"R{cid}")
    return starts, ends, refs, order


def check_comments(doc, parts, report: Report) -> None:
    """批注结构。查的是「Word 打开会不会当没有批注 / 锚在错的字上」。

    这一整类错的共同点是**都不报错也不影响打开**：文件双击能开，正文一个字不差，
    只是审阅窗格里空的、或者批注锚在别的句子上。所以只能靠对着四个部件互相核。
    """
    bodies = comment_bodies(parts)
    starts, ends, refs, order = comment_anchors(doc)
    if not bodies and not starts and not ends and not refs:
        report.warn("没有批注。要提审核意见而不替人改字，见 scripts/comment.py")
        return

    # 一、正文锚点和批注正文要一一对上。
    body_ids = set(bodies)
    anchor_ids = set(starts) | set(ends) | set(refs)
    orphan_body = sorted(body_ids - anchor_ids, key=lambda s: (len(s), s))
    orphan_anchor = sorted(anchor_ids - body_ids, key=lambda s: (len(s), s))
    if orphan_body:
        sample = head([f"#{i}「{bodies[i]['text'][:14]}」" for i in orphan_body], 3, "；")
        report.fail(
            f"{len(orphan_body)} 条批注有正文没锚点（{sample}）："
            f"Word 里那条批注不显示，或者被当成文档级批注"
        )
    if orphan_anchor:
        report.fail(
            f"{len(orphan_anchor)} 处锚点没有对应正文（id {head(orphan_anchor)}）："
            f"正文里有个点不开的小标记，comments.xml 里查无此条"
        )
    if not orphan_body and not orphan_anchor and bodies:
        report.ok(f"{len(bodies)} 条批注，正文与锚点一一对应")

    # 二、Start / End / Reference 三件套齐不齐、有没有重复。
    trouble: list[str] = []
    for cid in sorted(anchor_ids | body_ids, key=lambda s: (len(s), s)):
        s, e, r = starts.get(cid, 0), ends.get(cid, 0), refs.get(cid, 0)
        if cid not in body_ids:
            continue                      # 上面已经按孤立锚点报过了，不重复报
        if not s and not e and not r:
            continue
        if s != 1 or e != 1 or r != 1:
            trouble.append(f"#{cid} Start×{s} End×{e} Reference×{r}")
    if trouble:
        report.fail(
            f"{len(trouble)} 条批注的三件套不是各一个（{head(trouble, sep='；')}）："
            f"少 Start/End 就不知道锚在哪，少 Reference 正文里就没有可点的标记，"
            f"多一个则锚点范围错乱"
        )
    elif bodies:
        report.ok(f"{len(bodies)} 条批注的 Start / End / Reference 各一个")

    # 三、顺序：同一个 id 必须是 S → E → R。
    seq: dict[str, str] = {}
    for token in order:
        seq[token[1:]] = seq.get(token[1:], "") + token[0]
    bad_order = [f"#{cid}({got})" for cid, got in seq.items()
                 if len(got) == 3 and got != "SER"]
    if bad_order:
        report.fail(
            f"{len(bad_order)} 条批注三件套顺序不对（{head(bad_order, sep='；')}，应为 S→E→R）："
            f"Reference 排在 End 前面，正文里的标记会落在锚点范围内侧"
        )

    # 四、w:id 不能重复。重复了 Word 按第一个算，第二条批注等于没有。
    raw = parts.get(CMT_PART)
    if raw is not None:
        ids = [attr(node, "id", "") for node in raw.findall(tag("comment"))]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1}, key=lambda s: (len(s), s))
            report.fail(f"comments.xml 里 w:id 重复（{', '.join(dupes)}）：重复的那几条只认第一个")

    # 五、作者和时间。跟留痕一个道理：不写谁提的，评审看不出是谁的意见。
    naked = [cid for cid, info in bodies.items() if not info["author"] or not info["date"]]
    if naked:
        report.fail(f"{len(naked)} 条批注缺 w:author 或 w:date（id {head(naked)}）")
    bad_date = [info["date"] for info in bodies.values()
                if info["date"] and not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", info["date"])]
    if bad_date:
        report.warn(f"{len(bad_date)} 条批注的 w:date 不是 ISO 8601（如 {bad_date[0]}），"
                    f"Word 显示成未知时间")

    check_comment_extended(parts, bodies, report)
    check_comment_wiring(parts, report)


def check_comment_extended(parts, bodies: dict[str, dict], report: Report) -> None:
    """commentsExtended / commentsIds / commentsExtensible 与 comments.xml 对不对得上。

    回复串和「已解决」**只**记在 commentsExtended 里，靠 w14:paraId 和 comments.xml
    挂钩。paraId 对不上时 Word 不报错：回复摊平成一堆独立批注，已解决标记全丢。
    """
    ext = parts.get(EXT_PART)
    if ext is None:
        if bodies:
            report.warn(
                f"没有 {EXT_PART}：{len(bodies)} 条批注全是平铺的，没有回复串也没有"
                f"「已解决」。python-docx 的 add_comment() 就是这样，见 comment.py 说明"
            )
        return

    body_pids = {info["para_id"] for info in bodies.values() if info["para_id"]}
    missing_pid = [cid for cid, info in bodies.items() if not info["para_id"]]
    if missing_pid:
        report.fail(
            f"{len(missing_pid)} 条批注的末段没有 w14:paraId（id {head(missing_pid)}）："
            f"commentsExtended 靠它认这条批注，对不上就丢回复串和已解决"
        )

    # 根元素是 w15:commentsEx，每条记录是 w15:commentEx——差一个 s，别写反。
    exs = ext.findall(f"{{{W15}}}commentEx")
    pids = [node.get(f"{{{W15}}}paraId", "") for node in exs]
    dangling = sorted(set(pids) - body_pids - {""})
    if dangling:
        report.fail(
            f"commentsExtended 里 {len(dangling)} 个 paraId 在 comments.xml 里找不到"
            f"（{head(dangling)}）：这几条的已解决/回复关系是废的"
        )
    uncovered = sorted(body_pids - set(pids))
    if uncovered:
        report.warn(
            f"{len(uncovered)} 条批注没进 commentsExtended（{head(uncovered)}）："
            f"它们不会有「已解决」状态，Word 里当未解决处理"
        )

    parents = [node.get(f"{{{W15}}}paraIdParent") for node in exs]
    bad_parent = sorted({p for p in parents if p and p not in body_pids})
    if bad_parent:
        report.fail(
            f"{len(bad_parent)} 条回复的 w15:paraIdParent 指向不存在的批注"
            f"（{head(bad_parent)}）：Word 里这几条回复会摊平成独立批注"
        )
    self_parent = [node.get(f"{{{W15}}}paraId") for node in exs
                   if node.get(f"{{{W15}}}paraIdParent")
                   and node.get(f"{{{W15}}}paraIdParent") == node.get(f"{{{W15}}}paraId")]
    if self_parent:
        report.fail(f"{len(self_parent)} 条批注自己回复自己（paraId {self_parent[0]}）")

    replies = sum(1 for p in parents if p)
    resolved = sum(1 for node in exs if node.get(f"{{{W15}}}done") == "1")
    if not (dangling or bad_parent or self_parent):
        report.ok(f"commentsExtended 对得上：{len(exs)} 条记录，{replies} 条回复，{resolved} 条已解决")

    # w16cid / w16cex 是可选的。本技能的 comment.py 不写，但只要有一个就得两个都齐——
    # commentsExtensible 靠 durableId 挂回 commentsIds，缺一半就是一堆指不到的 durableId。
    ids_root, cex_root = parts.get(IDS_PART), parts.get(CEX_PART)
    if ids_root is None and cex_root is None:
        return
    durable_ids = {n.get(f"{{{W16CID}}}durableId", "")
                   for n in (ids_root.iter(f"{{{W16CID}}}commentId") if ids_root is not None else [])}
    id_pids = {n.get(f"{{{W16CID}}}paraId", "")
               for n in (ids_root.iter(f"{{{W16CID}}}commentId") if ids_root is not None else [])}
    cex_ids = {n.get(f"{{{W16CEX}}}durableId", "")
               for n in (cex_root.iter(f"{{{W16CEX}}}commentExtensible") if cex_root is not None else [])}
    lost_pid = sorted(id_pids - body_pids - {""})
    if lost_pid:
        report.fail(f"commentsIds 里 {len(lost_pid)} 个 paraId 在 comments.xml 里找不到"
                    f"（{head(lost_pid)}）")
    lost_durable = sorted(cex_ids - durable_ids - {""})
    if lost_durable:
        report.fail(f"commentsExtensible 里 {len(lost_durable)} 个 durableId 在 commentsIds 里"
                    f"找不到（{head(lost_durable)}）")
    if not lost_pid and not lost_durable:
        report.ok(f"commentsIds / commentsExtensible 对得上（{len(durable_ids)} 个 durableId）")


def check_comment_wiring(parts, report: Report) -> None:
    """rels 和 [Content_Types].xml 里有没有引到批注部件。

    这两处缺任何一处，Word 打开**一句话都不提示**，就是看不见批注——部件在 zip 里
    躺着没人引用。查出来的成本远低于「文件打开了但批注没了，回头一个个部件对」。
    """
    rels = parts.get(RELS_PART)
    ct = parts.get(CT_PART)
    if rels is None or ct is None:
        report.warn(f"读不到 {RELS_PART} 或 {CT_PART}，跳过批注挂载检查")
        return

    targets = {(rel.get("Target") or "").lstrip("/").removeprefix("word/")
               for rel in rels.iter(f"{{{PKG_REL}}}Relationship")}
    overrides = {(node.get("PartName") or "").lstrip("/")
                 for node in ct.iter() if node.tag.endswith("Override")}

    for part, rel_type, label in (
        (CMT_PART, REL_COMMENTS, "批注正文"),
        (EXT_PART, REL_EXTENDED, "回复串/已解决"),
    ):
        if part not in parts:
            continue
        leaf = part.removeprefix("word/")
        if leaf not in targets:
            report.fail(f"{part} 在包里，但 {RELS_PART} 里没有指向它的关系"
                        f"（Type 应为 {rel_type}）：Word 打开会当没有{label}")
        if part not in overrides:
            report.fail(f"{part} 在包里，但 {CT_PART} 里没有 Override："
                        f"部分阅读器直接当整个文件损坏")
        if leaf in targets and part in overrides:
            report.ok(f"{part} 的 rels 和 Content_Types 都在")


def check_untracked(doc, original: Path, report: Report) -> None:
    """有没有「改了但没留痕」的地方。

    判据是：把新稿的修订全部拒绝，应当逐字回到原稿。回不去的差异就是无痕改动——
    这种改动在「显示最终状态」下和留痕改动长得一模一样，评审看不出来。

    比按作者名查更硬：作者名只能证明「这个人的改动包了 w:ins」，
    证明不了「没有人绕过留痕直接改字」。
    """
    try:
        with zipfile.ZipFile(original) as zf:
            base = ElementTree.fromstring(zf.read("word/document.xml"))
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        report.fail(f"原稿 {original} 读不了：{exc}")
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from revise import resolve            # 复用同一份拒绝逻辑，免得两边算法漂移
    except ImportError as exc:
        report.warn(f"找不到 revise.py，跳过无痕改动比对：{exc}")
        return

    clone = copy.deepcopy(doc)
    resolve(clone, accept=False)
    now = [paragraph_text(para) for para in clone.iter(tag("p"))]
    was = [paragraph_text(para) for para in base.iter(tag("p"))]
    if now == was:
        report.ok(f"拒绝全部修订后与 {original.name} 逐段一致，没有无痕改动")
        return

    marks = any(node.tag in (tag("ins"), tag("del")) for node in doc.iter())
    if not marks:
        # 接受过修订的定稿和「绕过留痕直接改字」的稿，在文件里是同一个样子——
        # 痕迹已经被接受动作抹掉了。这里报不出区别，只能把两种可能都摊开。
        report.fail(
            f"与 {original.name} 内容不同，但一处留痕都没有。要么这份是接受过修订的定稿"
            f"（那就别拿 --original 比，比 --mode gongwen），要么有人绕过留痕直接改了字"
        )
    else:
        report.fail(
            f"拒绝全部修订后与原稿不一致（{len(was)} 段 → {len(now)} 段），说明有改动没留痕"
        )
    shown = 0
    for index, (old, new) in enumerate(zip(was, now), 1):
        if old != new and shown < 3:
            report.warn(f"  第{index}段 原「{old[:24]}」→ 现「{new[:24]}」")
            shown += 1
    if len(was) != len(now):
        report.warn(f"  段数也不一样，可能有整段被无痕增删")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 docx 版式与 GB/T 9704-2012 公文格式")
    parser.add_argument("--input", required=True, help="待校验的 .docx")
    parser.add_argument("--mode", choices=("basic", "gongwen", "revise", "comment"),
                        default="basic",
                        help="basic 只查通用陷阱；gongwen 追加公文版式；"
                             "revise 追加修订留痕；comment 追加批注结构")
    parser.add_argument("--original", help="原稿 .docx，比对有没有改了不留痕的地方")
    parser.add_argument("--author", help="要求所有留痕都是这个作者")
    args = parser.parse_args()

    path = Path(args.input)
    try:
        parts = read_parts(path)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        print(f"FAIL 读不了 {path}：{exc}", file=sys.stderr)
        return 2
    doc = parts["word/document.xml"]

    print(f"文件：{path}")
    print(f"模式：{args.mode}")
    report = Report()

    section("通用陷阱")
    check_fonts(doc, parts, report)
    check_page_field(parts, report)
    check_runs_fragmented(doc, report)
    check_line_spacing(doc, report, strict=args.mode == "gongwen")

    if args.mode == "gongwen":
        section("版面（GB/T 9704-2012 4.1）")
        check_layout(doc, report)
        section("字号与层次（7.3）")
        check_body_size(doc, report)
        check_chars_per_line(doc, report)
        check_indent_units(doc, report)
        check_levels(doc, report)
        section("分隔线（7.2 / 7.4）")
        check_banji(doc, report)

    if args.mode == "revise" or args.original or args.author:
        section("修订留痕")
        check_revisions(doc, report, author=args.author)
        if args.original:
            check_untracked(doc, Path(args.original), report)
        else:
            report.warn("没给 --original，查不出「改了但没包 w:ins/w:del」的地方")

    # 显式要查，或者这份文档已经沾了批注。后一种情况不看模式：孤立锚点、rels 缺失
    # 这些错跟「你打算查什么」无关，碰见了就得报，不然默认模式跑一遍全绿反而误导。
    if args.mode == "comment" or has_comment_traces(doc, parts):
        section("批注")
        check_comments(doc, parts, report)

    section("结论")
    if report.failed:
        print(f"  FAIL {report.failed} 项，WARN {report.warned} 项。先修 FAIL。")
        return 1
    print(f"  通过，无 FAIL；WARN {report.warned} 项，逐条确认是否可接受。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
