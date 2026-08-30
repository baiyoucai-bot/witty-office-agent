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

## 提交与推送（用户要求提交时）

固定三步，不即兴发挥：

```bash
git add -A          # 站在仓库根收全部改动（含删除）
git status --short  # 复核暂存清单再落笔
git commit -m "一句话说清改了什么、为什么"
git push            # upstream 已绑定就不带 -u；首次推送才 git push -u origin <branch>
```

排除靠 `.gitignore` 兜底，不靠手挑：

- 暂存清单里出现**成百上千个文件、构建暂存目录、二进制大块头、密钥/.env、私有账本**，先停下补 `.gitignore` 再提交——大文件一旦推上公开仓，历史里就抠不掉了。
- 新增会产生本地产物的脚本（构建、staging、缓存）时，`.gitignore` 同一笔提交里配套补上。
- 发版本才打 tag：`git tag v0.x.y && git push origin v0.x.y`；安装器等大产物走 Release 附件，不进 git。

简短说明改了什么、实际跑了哪些检查、还剩什么限制。
