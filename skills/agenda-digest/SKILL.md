---
name: agenda-digest
description: Summarize this Agent's upcoming timed jobs — next fire, paused, expired. Use when the user asks for 日程摘要, 今日日程, 下次触发, or an agenda digest. Not for creating or deleting jobs.
network: general
metadata:
  triggers: 日程摘要 今日日程 下次触发 agenda-digest
---

# Agenda Digest

只读汇总当前 Agent 的定时任务，不进内核循环，也不另起调度器。

## 何时用

用户说日程摘要、今天有哪些到点任务、下次什么时候触发。

## 做法

1. 先调 `agenda_digest`。没有这个工具再用 `schedule_list`。
2. 报告：启用几条、暂停几条、每条的下次触发和提示摘录。
3. 用户没要求改任务就不要 `schedule_write` / `schedule_delete`。
4. 不要自己轮询；扫一次仍走已有 `/tick`。

## 收口

短列表即可。没有任务就说没有。
