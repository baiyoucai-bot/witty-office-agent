# 技能规范

本目录下的每个子目录是一个技能，遵循 [Agent Skills](https://agentskills.io) 开放规范。
提交前跑 `uv run python scripts/check_skills.py`，CI 也会跑同一份判据。

## 布局

```
skills/
  my-skill/            目录名 = 技能名：小写字母/数字/单连字符，最长 64
    SKILL.md           必须（大小写敏感）
    scripts/           可选：确定性脚本（校验、导出、批处理）
    references/        可选：第三层披露材料（方法论、schema、范例）
    assets/            可选：静态资源
```

规范外的子目录加载器不认。二进制大文件不要入库。

## SKILL.md 写法

```markdown
---
name: my-skill
description: 一句话说清干什么 + 什么时候用（用户会说哪些词）。不要超过 1024 字符。
network: general
metadata:
  triggers: 触发词 空格分隔 整词命中
allowed-tools: read grep bash
---

# My Skill

正文：给模型的操作指令。
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 与目录名一致，否则加载报错 |
| `description` | 是 | 参与技能路由打分：写「干什么 + 何时用 + 用户会点名的词」，不要写营销话术 |
| `network` | 建议 | `general` / `intranet` / `public`。内网部署会按它过滤外网技能 |
| `metadata.triggers` | 建议 | 作者点名的整词表，空格分隔；路由对整词命中给高分，比 description 撞词可靠 |
| `allowed-tools` | 可选 | 一旦声明就是**真收权**：技能激活期间模型只能用清单内工具（外加 skill / ask 等保底）。名字必须是已注册工具 |
| `license` / `compatibility` | 可选 | 规范字段，照写即可 |

## 三层披露（写技能最重要的一条）

1. **第一层**：启动时系统提示只带 `name` + `description`。描述写不准，技能等于不存在。
2. **第二层**：用户的话命中后才注入 `SKILL.md` 正文。正文写操作步骤，不要塞资料——超过约 1.2 万字符校验会提醒拆分。
3. **第三层**：大块材料（方法论、schema、长范例）放 `references/`，正文里给路径让模型按需去读。

## 纪律

- **业务归技能，内核归内核。** 不要为某个技能改核心循环；需要新原语先在 `AGENTS.md` 层面讨论。
- **提示词不进代码。** 技能正文本身就是配置；脚本里不要再写死一份给模型的话术。
- **脚本要有退出码。** `scripts/` 下的校验/导出脚本按 0（干净）/ 1（有 FAIL）/ 2（跑不起来）返回，这样能直接当目标模式的客观 gate。
- **正文里引用的路径必须存在。** `scripts/x.py`、`references/y.md` 写了就得有，校验脚本查死链。
- **技能名不得占用内核工具/命令名**（`witty_agent.kernel_surface`），注册时会被拒绝。
- **改了技能要跑路由回归**：`uv run python -m unittest tests.test_skills_tools`。改 description 最容易把别的技能的触发词抢走，历史上出过这种回归。
- 发 wheel 前跑 `uv run python scripts/sync_package_data.py`，把 `skills/` 同步进包数据。

## 本地扩展

不改仓库也能加技能：`WITTY_SKILLS_PATH=/path/a:/path/b` 指向额外目录，或在桌面「能力中心」安装本地 SKILL.md。
