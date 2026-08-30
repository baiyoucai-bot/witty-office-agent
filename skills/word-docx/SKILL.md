---
name: word-docx
description: 生成和校验 Word / WPS 里能改的 .docx，版式按 GB/T 9704-2012 党政机关公文格式对尺子。用户点名 word、docx、公文、红头文件、发文格式、版心、仿宋 时使用。不要用于 md / html / 纯文本草稿。
network: intranet
metadata:
  triggers: word docx 公文 红头文件 发文字号 发文 公文格式 版心 仿宋 word-docx 全文抽取
---

# Word DOCX

产出 **Word / WPS 里能改字、能重排的 `.docx`**，且版式经得起量。

要 md / html / 纯文本草稿走 `office-document`；可研 / 详设 / 分章长文先走 `long-document` 再回这里导出；要可编辑 pptx 走 `witty-ppt-skills`；要网页演示走 `slides`。本技能只出 `.docx`。

## 开始前

确认文种、发文机关、输出路径，以及要不要按公文格式排。缺这几项会改变版式才问。正文缺料标「待核实」，不要替用户编制度条文和数据。

## 约定

版式数字一律查 `references/gongwen-format.md`，不要凭印象填，也不要照抄网上的「公文模板参数」——常见的两个参数是错的，见下。

**版式错看不出来。** 字体名写对了但渲染成宋体、每行少一个字、每面少一行，文档打开都像是对的，评审也过。所以先看校验脚本的退出码，不看肉眼。

**但退出码 0 也不等于版式对。** 校验只读 XML，读不出渲染结果——实测有过 XML 写着「左空二字」、渲染出来是五字的情况。缩进、对齐这类**位置**要求，改完必须转 PDF 量一遍，见「核查」。

## 生成

公文（红头文件、通知、请示、批复、函）一条命令：

```bash
<沙箱 Python> <技能目录>/scripts/gongwen.py --spec <要素.json> --output <路径.docx>
```

`--help-spec` 打印 spec 字段，`--demo` 出一份带全要素的样例对照。层次序数不用自己指定字体：写成「一、」「（一）」「1.」「（1）」开头，脚本按 7.3.3 自动套黑体 / 楷体 / 仿宋。字体名按本机装的字库用 `--font-zhengwen` 一类参数覆盖。

不按公文排的**普通长文**（可研、详设、方案）不要手搓 python-docx：

```bash
<沙箱 Python> <技能目录>/scripts/report.py --project <long-document 工程> --output <路径.docx> --toc
<沙箱 Python> <技能目录>/scripts/outline.py --input <路径.docx>
<沙箱 Python> <技能目录>/scripts/extract_text.py --input <路径.docx> --output 稿.md --media 图片目录
```

`report.py` 打 Heading 1/2/3、宋体正文、Markdown 表、图片、SEQ 题注、REF 交叉引用、TOC 目录域。章节里的约定：

- 图片：`![题注](assets/图.png){#fig:标签}` 单独一行
- 表格前一行：`表: 题注 {#tbl:标签}`
- 正文引用：`[@fig:标签]` / `[@tbl:标签]`
- 关键数字：`[num:id]`（来自工程 `ledger.toml`）

看别人给的稿：目录用 `outline.py`，全文用 `extract_text.py`（内核 `read` 读不了二进制）。单页函件仍可用沙箱 python-docx，但 `set_font` / `fixed_line` / `indent_chars` / `page_field` 照抄 `gongwen.py`，见 `references/docx-traps.md`。

## 改已有文档

改别人给的稿、或者改自己上一版，**一律带修订留痕**，不要直接覆盖原文：

```bash
<沙箱 Python> <技能目录>/scripts/revise.py --input 原稿.docx --output 送审稿.docx --spec 改动.json
```

`--help-spec` 打印 spec 字段。支持 replace / delete / delete_para / insert_after / insert_before，
按原文串定位，脚本负责切 run、包 `w:ins` / `w:del`、抄版式。`--list` 列出现有留痕。

审定后再落定稿，接受或退回：

```bash
<沙箱 Python> <技能目录>/scripts/revise.py --input 送审稿.docx --output 定稿.docx --accept
```

**不要用 `pandoc --track-changes=accept` 代替。** 实测公文过一遍纸张版心网格全丢、
起间隔的空段全没、中文字体退回宋体，而文字看着都在。见 `references/docx-traps.md`。

留痕的结构要求和接受/拒绝的语义见 `references/tracked-changes.md`，动手前先看。

## 提意见但不替人改（批注）

「请补充责任部门」「这个时间来得及吗」这类**没资格替他定**的意见，走批注，不要留痕改字：

```bash
<沙箱 Python> <技能目录>/scripts/comment.py --input 稿.docx --output 批注稿.docx --spec 批注.json
```

`--help-spec` 打印 spec 字段。按原文串定位，脚本负责切 run、放锚点三件套、挂回复串
（`reply_to`）和已解决（`done`），六个联动部件一起写。`--list` 列出现有批注和它锚在哪几个字，
`--delete` 删指定几条，`--strip` 清空。

沙箱 python-docx 的 `add_comment()` 只能锚在整个 run 上，且**没有回复串也没有「已解决」**，
不要用它写。批注结构和验证手段见 `references/comments.md`。

## 校验

```bash
<沙箱 Python> <技能目录>/scripts/check_docx.py --input <路径.docx> --mode gongwen
```

`--mode basic`（默认）只查任何 docx 都算错的：中文缺 `w:eastAsia`、页码写死数字、行距被字号带跑、run 碎片化。`--mode gongwen` 追加纸张、版心、边距、字号、每面行数、每行字数、层次字体、分隔线。别人给的文件也能查，脚本只读。

改过稿的再加一道：

```bash
<沙箱 Python> <技能目录>/scripts/check_docx.py --input 送审稿.docx --mode revise --original 原稿.docx
```

`--original` 的判据是「全部拒绝后应当逐字回到原稿」，回不去就是有改动没留痕——
这种改动在「显示最终状态」下和留痕改动长得一模一样，评审看不出来。
`--author` 还能要求所有留痕都是同一个人。

带批注的再加 `--mode comment`：查孤立批注/孤立锚点、三件套缺项和顺序、`w:id` 重复、
四个部件的 `paraId` 对不上、rels 或 Content_Types 缺项。这些错**文件都能正常打开**，
只是批注不显示或锚在别的字上。文档里有批注痕迹时这一段会自动查，不用指定模式。

退出码 0 才算过。FAIL 必修；WARN 逐条判断能不能接受，不要一律忽略。

## 核查

再看一眼实物，不要只信 XML：

```bash
libreoffice --headless --convert-to pdf --outdir <目录> <路径.docx>
```

本机**没有 `soffice`**，只有 `libreoffice`。转完：

- `pdftoppm -jpeg -r 80` 出图看红线位置、页码、字体。
- `pdftotext -layout` 数每行字数和每面行数。数的时候用 Python `len()`，不要用 `awk length`——那个数的是字节，一个汉字算三个。
- `pdftotext -bbox` 量缩进和对齐：按 `yMin` 归行，`(xMin − 28mm) / 字宽` 就是左空几字，对着 `references/gongwen-format.md` 的要求核。这是唯一能查出「左空二字」有没有真的空二字的办法。

**批注和留痕在 PDF 里都看不到**，这条路数不了它们。批注改用 `--convert-to fodt` 数
`<office:annotation[ >]`，或者用 python-docx 当独立读取端，见 `references/comments.md`。

量出来的数不对，先怀疑量法。量到空结果、量出整行 81 个字这种数，是脚本错了，不是结论。

交付时报输出路径、校验结论、未决项。不要复述过程。
