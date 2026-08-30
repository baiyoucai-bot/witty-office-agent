---
name: table-qa
description: Read-only check of a local CSV/TSV for empty headers, duplicate columns, ragged rows, and placeholder cells. Use when the user asks for 表格质检, CSV 质检, 空列, or table QA. Not for analyzing or rewriting the sheet.
network: general
metadata:
  triggers: 表格质检 CSV质检 空列 重复表头 table-qa
---

# Table QA

只读检查本地 CSV/TSV，不进内核循环，也不改文件。

## 何时用

用户说表格质检、CSV 有没有空列、表头是不是重复。

## 做法

1. 先调 `table_qa`，传入路径。
2. 报告：空表头、重复列名、行列数不一致、整列为空、占位格。
3. 用户没要求改表就不要 `write` / `edit`。
4. `.xlsx` 工作簿走 `excel-xlsx` 的 `inspect.py` / `check_xlsx.py`，不要另存 CSV 再质检——会丢掉公式。

## 收口

短列表即可。通过就说通过。
