---
name: skill-porting
description: Port an external Agent Skill into this repo as a SKILL.md directory that the harness can load. Use when porting an external skill, 外部技能, or SKILL.md.
network: general
---

# Skill Porting

把外部技能迁到本仓库的 `skills/<name>/`，不要改核心循环。

## 规则

1. 目录名必须是小写+数字+单连字符，且与 frontmatter `name` 一致。
2. 至少有 `SKILL.md`（YAML frontmatter + 正文）。可选 `scripts/`、`references/`、`assets/`。
3. 发给模型的指令只写在 `SKILL.md`，不要抄进 `.py`。
4. 路径用 `WITTY_HOME` / 当前工作区，不要写死上游项目的家目录。
5. 装之前通读正文。不要把密钥、专用业务规则写进通用技能。
6. 迁完用 `list_skills` 确认能被发现，`load_skill(name)` 能读到正文。
7. frontmatter 写 `network: intranet` / `public` / `general`（内网 / 外网 / 通用）。缺省按通用。不要把依赖公网 API 的技能标成内网。
