---
name: benchmark-design
description: Design and freeze a multi-case capability Benchmark and record a formal baseline. Does not change the Test Agent. Use when designing a benchmark, 评测题, or freezing test cases.
network: general
---

# Benchmark Design

只改 Benchmark，不改 Test Agent。评测用 `run_subagent` 并要求工人使用 `agent-evaluation`。基线写完就停，不要开始优化。

## 路径

```text
TARGET = <root>/<project_id>/agents/<test_agent_id>
BENCHMARK = <target>/benchmarks/<benchmark_id>
```

```text
<benchmark_id>/
  benchmark_config.toml
  scoreboard.yaml
  CASE-<nnn>-<name>/
    statement/README.md
    rubric/README.md
```

`statement/` 对 Test Agent 公开。`rubric/` 私有，禁止把金标写进 statement。

## 流程

1. 确认 Test Agent、目标能力、期望基线分、Pilot 轮次上限。
2. 写 Capability Contract：可观察过程、常见捷径、希望训练出的 Agent State 行为。
3. 规划 Case 与分值。每题 100 分，区分「该有的行为」和「捷径」。
4. 每题 1 run 组成 Pilot。未入选的 Pilot 不要进 scoreboard。
5. 用轨迹改题，不改 Test Agent。漏题检查：公开文件不得暴露金标。
6. 达到期望分或轮次用尽后冻结。完整有效的 Pilot 记为 Formal Baseline。
7. 用 `append_score` 追加基线。不要重跑入选矩阵。

`run_subagent` 和 `agent-evaluation` 缺一个就停。
