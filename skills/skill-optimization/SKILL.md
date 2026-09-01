---
name: skill-optimization
description: 审计并优化 Agent Skill 的元数据、触发路由、正文工作流、工具边界和附属资源，用冻结 Benchmark、路由正负例与回归证据决定是否接受改动。Use when optimizing, reviewing, tuning, or desloping a skill, or 优化技能、技能路由、技能正文、skill quality、skill routing。
network: general
metadata:
  triggers: 技能优化 优化技能 技能路由 技能正文 skill optimization skill quality skill routing deslop skill
---

# Skill Optimization

把技能当作可测试的配置资产优化，而不是凭感觉改提示词。每次只处理一个目标技能，保留可回滚的基线，用正例、负例和边界例验证路由与正文行为；没有证据就不宣布改进。

## 适用边界

- 目标可以是仓库 `skills/<name>/` 下的系统技能，或某个 Agent State 的用户技能。
- 只改目标技能及其直接资源。不要改核心循环、内核工具、其它技能、项目密钥或用户工作区中的无关文件。
- 要优化 Agent 的 `AGENTS.md`、模型策略或多个 Agent 的协作，请转用 `agent-optimization`；本技能只负责 Skill 资产。
- 系统技能的源文件是仓库 `skills/`；`src/witty_agent/data/skills/` 是生成的包内副本，接受改动后再同步，不要把副本当成源头。

## 开始前

1. 明确 `target_skill`、目标路径、使用它的 Agent、期望改善的指标和停止条件。目标不唯一时先停，不要猜。
2. 检查 `git status --short`。目标文件已有他人未提交改动时，先保留并把基线记录清楚；不要用回滚覆盖这些改动。
3. 通读目标 `SKILL.md` 全文及正文引用的 `scripts/`、`references/`、`assets/`。同时读取相邻的 `skills/README.md`、技能专属测试和实际工具名，避免只凭 frontmatter 下结论。
4. 运行仓库的技能规范校验器，记录 FAIL/WARN；至少跑 `tests.test_skills_tools` 和 `tests.test_check_skills`。用户技能则在其实际 `WITTY_SKILLS_PATH` 或 Agent State 根上做同等检查。

## 建立基线

先建立“改之前”的可复现记录，再动正文：

- **结构基线**：name 与目录一致、description 不超过规范上限、network 与 allowed-tools 有效、引用没有死链、正文长度适合渐进披露。
- **路由基线**：准备至少 3 个明确正例、3 个相邻负例、1 个边界例。记录 `match_relevant_skills` 的排序、命中分数（若可见）和是否误触发其它技能。正例要覆盖用户常用中文说法和英文/技能名说法。
- **行为基线**：选 2 个以上真实任务或冻结 Benchmark Case，记录输出文件、工具调用、失败处理和耗时/调用次数等可观察结果。不要把私有 rubric 或金标答案塞进公开 statement。
- **安全基线**：列出技能声明的工具、会写入的路径、需要联网的步骤、审批点和拒绝时的行为。技能没有声明 `allowed-tools` 不等于可以绕过内核审批。

若已有 Test Agent 和冻结 Benchmark，优先复用 `skills/benchmark-design/SKILL.md` 设计基线，再用 `skills/agent-optimization/SKILL.md` 的快照、全量评测、严格升分接受/回滚闭环。没有冻结基准时，可以先做结构与路由审计，但只能报告“候选改进”，不能声称质量分数提升。

## 审计清单

按证据逐项检查，发现问题才提出改动：

1. **触发精度**：description 说清“做什么 + 何时用 + 用户会说什么”；triggers 是有区分度的整词，不用泛词抢其它技能。
2. **渐进披露**：第一层 metadata 足以被路由发现；第二层正文只保留操作规则；长方法论、schema 和范例放第三层资源，并且链接可达。
3. **任务闭环**：正文包含输入确认、证据读取、执行顺序、输出契约、失败恢复和停止条件，而不是只有领域介绍。
4. **工具纪律**：每个工具都有使用前提和失败后的下一步；危险写入、执行、联网仍走内核审批；不把业务工具名伪装成内核工具。
5. **可验证产物**：输出路径、格式、命名和验收条件明确；能用确定性检查器验证的内容不要只靠模型自述。
6. **资源一致性**：脚本参数、引用路径、frontmatter、示例和测试彼此一致；不留下已删除文件的链接或过期命令。
7. **失败可诊断**：区分证据不足、工具失败、非法输出、超时和用户拒绝；说明重试、补证据、换路径或交还用户的动作。
8. **通用底座边界**：业务差异留在技能和配置中，不把某个行业、项目、账号或密钥写死进通用技能。

## 优化循环

每一轮只验证一个可证伪假设，例如“把触发词从泛词换成领域整词，会减少相邻负例误命中，同时保留全部正例”。按以下顺序执行：

1. **快照**：保存目标文件和直接资源的补丁/副本，记下基线版本、校验结果和工作区脏状态。快照前先确认没有把别人的未提交改动混进目标。
2. **写假设**：说明改哪一处、预期改善哪个指标、可能伤害什么、用哪些 Case 证伪。一次不要同时重写 description、流程和脚本。
3. **最小改动**：优先调整 metadata、章节顺序或一个失败分支；只有重复且稳定的逻辑才抽成脚本。不要为了“更完整”堆泛泛解释。
4. **先过硬门**：重新运行技能规范校验、引用检查和相关单元测试。任一 FAIL、导入错误、死链或 allowed-tools 无效都直接拒绝候选。
5. **跑完整矩阵**：对每一个冻结 Case、全部路由正例、全部负例和边界例各跑一次。不要只挑能证明假设的题；按批次落盘结果，失败也记录原因。
6. **检查副作用**：确认没有误触发相邻技能、工具权限没有扩大、网络标签没有放宽、输出契约没有破坏旧调用方，且源文件与包内副本仍可同步。
7. **严格决策**：只有目标指标严格变好、硬门全绿且没有回归才接受；持平、任何关键正例下降、负例增加或证据不完整都回滚。不要用“看起来更清楚”抵扣分数回退。
8. **记录结果**：保存 before/after 分数、每个 Case、路由差异、测试命令、接受/回滚原因和残留风险。接受系统技能时运行包数据同步，并再次加载源文件和副本确认一致。

## 常见反模式

- 用更宽的 description 或加入“万能、所有场景”等泛词来提高命中率；这通常只是把误触发转嫁给其它技能。
- 直接删除失败规则、降低校验门槛或把“模型自行判断”当成恢复策略。
- 同一轮同时修改技能正文、核心代码、提示词配置和测试，让分数变化无法归因。
- 只跑一条成功样例，跳过负例、拒绝路径、空输入、非法 JSON、缺依赖和超时。
- 把 rubric、金标答案、真实密钥或客户材料复制进技能正文、公开 Benchmark 或日志。
- 直接编辑包内生成副本，或用 `git restore`/删除目录覆盖工作区里未经确认的用户改动。

## 输出契约

结束时用短报告交付：

```text
target: <skill name and path>
hypothesis: <one falsifiable sentence>
baseline: <structure / routing / benchmark score>
candidate: <structure / routing / benchmark score>
decision: accepted | rolled_back | candidate_only
changes: <files and behavior>
verification: <commands and matrix summary>
risks: <remaining risks or none>
```

`candidate_only` 只适用于没有冻结基准或验证未完成的情况；此时不要把候选内容覆盖到正式技能。任何写入、执行或联网动作都遵守当前 Agent 的审批策略。
