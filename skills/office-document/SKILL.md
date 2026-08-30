---
name: office-document
description: Draft and revise office documents — reports, memos, minutes, tables — in the requested path and format. Use when writing 报告, 纪要, 文书, 公函, 会议记录, 起草, memos, or office documents.
network: general
---

# Office Document

写或改**短**办公文书：纪要、函、一页报告、表。只产出被要求的文件，不要发明制度条文或未提供的数据。

可研、详设、概设、分章长文走 `long-document`。红头公文走 `word-docx`。
用户要把已写好的 Word/可研做成可编辑汇报 PPT：切到 `ppt-master`（政企题材默认 grid，整稿一次渲，不要连环追问页数）。
老格式 `.doc` / `.xls` / `.ppt` / `.wps` / `.et` / `.dps` / `.rtf` 先转再交给对应技能：

```bash
<沙箱 Python> <技能目录>/scripts/convert_legacy.py --input 老.doc --outdir 目录
```

用本机 `libreoffice --headless`（没有 `soffice`）。转完自己核一眼，再走 `word-docx` / `excel-xlsx` / `ppt-master`。

## 开始前

确认文种、读者、必须包含的章节、输出路径和格式（md / html / csv / 纯文本）。缺关键事实会改变正文时才问。

## 写法

1. 先列提纲再写。标题、结论、依据分开。数字带来源，没有来源就标明「待核实」。
2. 表格优先用结构清晰的 markdown 或 csv。不要把表塞进一段话里。
3. 沿用用户已有模板的标题层级和称谓。没有模板就用短标题 + 条目。
4. 改已有文件时用 `read` 再 `edit`，保持未改章节原样。

## 结束前

打开写出的文件核对：路径、文种、必含章节、明显事实错误。只报告文件路径和未决问题。
