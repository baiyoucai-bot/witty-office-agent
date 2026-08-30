---
name: pdf-extract
description: 从本地 PDF 抽文字层、抽表格、扫描件 OCR、合并页面、填写 AcroForm。用户点名 pdf、抽PDF、读pdf、pdftotext、OCR扫描件、合并PDF、填PDF表单 时使用。不从零排版生成 PDF。
network: intranet
metadata:
  triggers: pdf抽取 抽PDF 读pdf pdftotext pdf-extract 扫描件 OCR扫描件 合并PDF 填PDF表单
---

# PDF Extract

内核 `read` 会拒绝二进制，必须走本技能。不从零排版新 PDF。

## 文字层

```bash
<沙箱 Python> <技能目录>/scripts/extract.py --input 文件.pdf --output 文件.txt
<沙箱 Python> <技能目录>/scripts/extract.py --input 文件.pdf --pages 1-5
<沙箱 Python> <技能目录>/scripts/extract.py --input 文件.pdf --check
```

优先本机 `pdftotext -layout`，没有再试沙箱 `pypdf`。`--check` 查加密、0 页、整页没有文字层。抽到 0 字按扫描件处理，走 OCR，不要假装抽到了字。

## 表格

```bash
<沙箱 Python> <技能目录>/scripts/tables.py --input 文件.pdf --output 表.md
```

只认有线框或对齐结构的文字层表格。沙箱预装 pdfplumber。

## 扫描件 OCR

先 `--check`：有文字层就不要 OCR。

```bash
<沙箱 Python> <技能目录>/scripts/ocr.py --input 扫描.pdf --output 扫描.txt --pages 1-3
```

RapidOCR 离线认字，PDF 转图要本机 `pdftoppm`。认出来的金额、编号必须人工核对，不要直接当来源账。

## 合并 / 填表

```bash
<沙箱 Python> <技能目录>/scripts/compose.py --merge 甲.pdf 乙.pdf --output 合订.pdf
<沙箱 Python> <技能目录>/scripts/compose.py --list 表单.pdf
<沙箱 Python> <技能目录>/scripts/compose.py --fill 表单.pdf --spec 字段.json --output 填好.pdf
```

填表只改 AcroForm 域值。没有可填域的扫描件填不了。

## 长文

抽出的 txt / 表 md 可以经 `long-document` 的 `import_source.py` 登记进 `sources.toml`。不要直接把 PDF 当可编辑稿。

边界见 `references/pdf-traps.md`。
