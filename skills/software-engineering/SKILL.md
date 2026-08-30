---
name: software-engineering
description: Investigate and review code, implement fixes and features with minimal scope, validate changes, and report verified outcomes. Use when reviewing, debugging, or implementing code, a bug, module, 代码, or 模块.
network: general
---

# Software Engineering

完成调查、评审、修复、实现和验证交付。业务规则不要写进底座循环，只改当前工作区。

## 开始前

没有具体任务就先问要调查、评审还是改代码。解释/评审默认不改文件。

## 工作方式

1. 以当前工作区为项目，原地改。不要回滚别人的改动，不要用破坏性 git 命令，除非用户明确要求。
2. 先读仓库里的 `AGENTS.md` / 构建测试约定。改之前看实现、调用方和测试。
3. 修 bug 时尽量先复现或找到失败测试。
4. 改已有文件先 `read` 再 `edit`/`write`。改符号多处用 `edit(..., replace_all=true)`，不要整文件 write。不要为了过测试而削弱测试。
5. 用仓库自己的命令做相称验证。没跑过的检查不要声称通过。
6. 结束前看 `git diff`，清掉临时文件。用户没要求就不要 commit。

## 交接

简短说明改了什么、实际跑了哪些检查、还剩什么限制。
