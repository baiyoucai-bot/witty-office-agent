---
name: generation-ui
description: Keep chat generation chrome honest. Use when fixing send/stop/busy, SSE streaming, 生成中, 状态没变, stop button stuck, or work-process after a reply.
network: general
metadata:
  triggers: generation ui streaming busy 生成中 停止 状态没变 SSE
---

# 生成态界面

对话输入区的发送/停止必须跟这一轮是否还在跑一致。这是技能，不是内核循环。

对照社区里常见的前端技能（[addyosmani frontend-ui-engineering](https://github.com/addyosmani/agent-skills)、[Awesome Frontend Agent Skills](https://github.com/finfin/awesome-frontend-skills)）：可见状态只有一份真相。

## 何时用

用户说还在转、停不下来、回复完了还显示生成中、排队钮还在、工作过程不出现。

## 状态机

只允许这些态：`idle` → `streaming` / `tools` / `gated` → `idle`。

1. 点发送：立刻 `setBusy(true)`，发送钮变停止。
2. 收到 `done` 或 `error`：立刻 `setBusy(false)`，停止钮变回发送。不要等 HTTP 流断开。
3. SSE 读到终态就停循环。流还开着也不能把界面钉在生成中。
4. 批准/提问是 `gated`，不是结束。答完再回 `streaming`。
5. 助手正文已经画出来，就不能再假装在生成。

## 不要

- 终态事件到了还 `while (busy) read()`。
- 因为 `!busy` 提前 `return`，把收口逻辑跳过。
- 用「排队」这种词当主按钮。生成中发送=先记下一条；「调整方向」才改这一轮。

## 收口

改完后核对：正文出现 → 红钮消失；空发送不再当停止（除非仍在跑）。
