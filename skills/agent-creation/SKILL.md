---
name: agent-creation
description: Create or configure an Agent State from a requirement by writing AGENTS.md, identity metadata, and only needed Skills. Use when creating a new agent, 新建智能体, or writing AGENTS.md.
network: general
---

# Agent Creation

把用户需求落成目标 Agent 目录里的文件。不要改核心循环。

## 路径

```text
ROOT = WITTY_HOME 或 ~/.witty/data
TARGET = <root>/<project_id>/agents/<agent_id>
STATE = <target>/agent_state
```

`STATE` 里有 `AGENTS.md`、`system_config.toml`、`skills/`、`memory/`。

## 步骤

1. 当前 Agent 是 Builder。未指定模型时沿用当前 Session，不要写进目标 Agent 的 `system_config.toml`。
2. 已有目标就只改身份和 `AGENTS.md`；新 id 才创建目录。不要覆盖同名 Agent。
3. `AGENTS.md` 只写角色、规则、约束。系统层提示词不要动。
4. 技能装到 `STATE/skills/<name>/SKILL.md`，目录名与 frontmatter `name` 一致。装之前读完全文。
5. `system_config.toml` 只设 `name`、`description`、`version = 1`。
6. 结束前确认 `AGENTS.md` 非空，已装技能可解析，没有改到其它 Agent。

评测闭环需要时再装 `benchmark-design`、`agent-evaluation`、`agent-optimization`。
