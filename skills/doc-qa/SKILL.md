---
name: doc-qa
description: Inspect an existing local deck or markdown/text file for layout problems — empty pages, placeholders, too many bullets, tiny fonts, missing headings. Use when the user asks for 质检 or 版式检查. Not for writing a new file.
network: general
metadata:
  triggers: 质检 版式检查 空页 字号 doc-qa
---

# Document QA

只读检查已经写好的本地稿，不进内核循环，也不生成新文件。

## 何时用

用户说质检、版式检查、这份 PPT 空不空、字号会不会太小、Markdown 有没有标题。

## 做法

1. 先调 `doc_qa`，传入本机路径。没有这个工具再用 `pptx_outline` 或 `read` 自己对照。
2. 报告：空页/空文件、占位句、单页要点超过 7 条、小于 14pt 的字、缺标题。
3. 只列问题。用户没要求改稿就不要 `pptx_edit_slide` / `edit`。
4. 不访问公网模板，不下载字体。

## 收口

短列表：路径、问题条数、建议改哪一页。没有问题就说通过。
