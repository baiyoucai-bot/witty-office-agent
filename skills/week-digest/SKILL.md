---
name: week-digest
description: Summarize the last week of local diary entries. Use when the user asks for 周报摘要, 本周摘要, 这周干了啥, or a weekly digest. Not for writing today's diary.
network: general
metadata:
  triggers: 周报摘要 本周摘要 这周干了啥 weekly digest week-digest
---

# Week Digest

只读汇总最近几天的日记，不进内核循环，也不改日记文件。

## 何时用

用户说周报摘要、本周干了啥、把这周日记收成一段。

## 做法

1. 先调 `week_digest`。没有这个工具再用 `diary_list` / `diary_read`。
2. 按日列出条数和摘录。没有日记就说没有。
3. 用户没要求补记就不要 `diary_write`。
4. 不要编造没写过的行程。

## 收口

短列表即可。需要成文周报时再按摘要扩写，仍以日记原文为准。
