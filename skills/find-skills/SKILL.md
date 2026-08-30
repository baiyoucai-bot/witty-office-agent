---
name: find-skills
description: Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a skill that can...", or express interest in extending capabilities. Use when the user is looking for functionality that might exist as an installable skill. 用户想找技能、装技能、扩展能力时使用。
license: MIT
network: public
metadata:
  triggers: skills.sh find-skills 找技能 装技能 搜技能 安装技能 技能市场
---

# Find Skills

This skill helps you discover and install skills from the open agent skills ecosystem.

> 来源：[vercel-labs/skills](https://github.com/vercel-labs/skills)（MIT，Copyright (c) Vercel, Inc.，全文见 `references/UPSTREAM-LICENSE.txt`）。
> 本项目附注：需要本机 Node.js（`npx`）与公网访问；内网模式下本技能会被网络策略过滤。
> **装到哪**（witty-office-agent 的加载目录与通用目录不同）：
> - 装给本项目所有人用：把技能目录放进仓库 `skills/`，跑 `uv run python scripts/check_skills.py` 过校验；
> - 只装给自己：`npx skills add <owner/repo@skill> -g` 装到 `~/.agents/skills/` 后，`export WITTY_SKILLS_PATH=~/.agents/skills` 让加载器认它；
> - 桌面端也可在「能力中心 → 添加技能」里选本地 SKILL.md 安装。
> 第三方技能装前先读正文和脚本（供应链安全），写/执行类工具照旧走审批。

## When to Use This Skill

Use this skill when the user:

- Asks "how do I do X" where X might be a common task with an existing skill
- Says "find a skill for X" or "is there a skill for X"
- Asks "can you do X" where X is a specialized capability
- Expresses interest in extending agent capabilities
- Wants to search for tools, templates, or workflows
- Mentions they wish they had help with a specific domain (design, testing, deployment, etc.)

## What is the Skills CLI?

The Skills CLI (`npx skills`) is the package manager for the open agent skills ecosystem. Skills are modular packages that extend agent capabilities with specialized knowledge, workflows, and tools.

**Key commands:**

- `npx skills find [query] [--owner]` - Search for skills interactively or by keyword, optionally scoped to a GitHub owner
- `npx skills add <source>` - Install a skill from GitHub or other sources
- `npx skills update` - Update all installed skills

**Browse skills at:** https://skills.sh/

## How to Help Users Find Skills

### Step 1: Understand What They Need

When a user asks for help with something, identify:

1. The domain (e.g., React, testing, design, deployment)
2. The specific task (e.g., writing tests, creating animations, reviewing PRs)
3. Whether this is a common enough task that a skill likely exists

### Step 2: Check the Leaderboard First

Before running a CLI search, check the [skills.sh leaderboard](https://skills.sh/) to see if a well-known skill already exists for the domain. The leaderboard ranks skills by total installs, surfacing the most popular and battle-tested options.

### Step 3: Search for Skills

If the leaderboard doesn't cover the user's need, run the find command:

```bash
npx skills find [query] [--owner <owner>]
```

For example:

- User asks "how do I make my React app faster?" → `npx skills find react performance`
- User asks "can you help me with PR reviews?" → `npx skills find pr review`
- User asks "I need to create a changelog" → `npx skills find changelog`

### Step 4: Verify Quality Before Recommending

**Do not recommend a skill based solely on search results.** Always verify:

1. **Install count** — Prefer skills with 1K+ installs. Be cautious with anything under 100.
2. **Source reputation** — Official sources (`vercel-labs`, `anthropics`, `microsoft`) are more trustworthy than unknown authors.
3. **GitHub stars** — Check the source repository. A skill from a repo with <100 stars should be treated with skepticism.

### Step 5: Present Options to the User

When you find relevant skills, present them to the user with:

1. The skill name and what it does
2. The install count and source
3. The install command they can run
4. A link to learn more at skills.sh

### Step 6: Offer to Install

If the user wants to proceed, you can install the skill for them:

```bash
npx skills add <owner/repo@skill> -g -y
```

The `-g` flag installs globally (user-level) and `-y` skips confirmation prompts.
装完提醒用户：全局目录要用 `WITTY_SKILLS_PATH` 挂进来才会被本底座加载（见文首「装到哪」）。

## Common Skill Categories

When searching, consider these common categories:

| Category | Example Queries |
| --------------- | ---------------------------------------- |
| Web Development | react, nextjs, typescript, css, tailwind |
| Testing | testing, jest, playwright, e2e |
| DevOps | deploy, docker, kubernetes, ci-cd |
| Documentation | docs, readme, changelog, api-docs |
| Code Quality | review, lint, refactor, best-practices |
| Design | ui, ux, design-system, accessibility |
| Productivity | workflow, automation, git |

## Tips for Effective Searches

1. **Use specific keywords**: "react testing" is better than just "testing"
2. **Try alternative terms**: If "deploy" doesn't work, try "deployment" or "ci-cd"
3. **Check popular sources**: Many skills come from `vercel-labs/agent-skills` or `ComposioHQ/awesome-claude-skills`

## When No Skills Are Found

If no relevant skills exist:

1. Acknowledge that no existing skill was found
2. Offer to help with the task directly using your general capabilities
3. Suggest the user could create their own skill (`npx skills init`，或直接用本项目的 `/create-skill`)
