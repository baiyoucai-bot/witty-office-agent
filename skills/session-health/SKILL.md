---
name: session-health
description: Check the current session transcript for unfinished tools, interrupted outcomes, open todos, and plan mode. Use when the user asks for session health, 会话健康, 会话回顾, transcript review, or whether the last turn was interrupted.
network: general
metadata:
  triggers: session health transcript 会话健康 会话回顾 中断检查
---

# Session Health

检查当前会话是否还能安全继续。这是技能，不是内核循环。

## 何时用

用户说检查会话、刚才中断了吗、有没有没做完的工具、transcript / session health。

## 做法

1. 先调 `session_health`（只读）。没有这个工具再用 `session_query`。
2. 报告：轮次是否收口、未配对工具、`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`、未完成待办、是否在计划模式。
3. 未配对或结局未知的写工具：不要盲目重试。只读/幂等可以再跑；有副作用先核对工作区或问用户。
4. 不要改会话日志，不要假装跑过检查。

## 收口

短列表即可：异常、建议的下一步、还缺用户拍板的事。
