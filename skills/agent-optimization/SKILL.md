---
name: agent-optimization
description: Improve an Agent State with versioned snapshots, a frozen Benchmark, and accept-or-rollback. Use when optimizing or self-improving an agent, or 优化智能体.
network: general
---

# Agent Optimization

Improve one Test Agent. Use public scores and traces only.

## Required inputs

Test Agent id, frozen Benchmark id, target score, positive `runs`, positive round limit.

## Paths

Resolve from the Environment data root (`WITTY_HOME` or `~/.witty/data`):

```text
PROJECT = <root>/<project_id>
TARGET = <project>/agents/<test_agent_id>
STATE = <target>/agent_state
SNAPSHOTS = <target>/snapshots
BENCHMARK = <target>/benchmarks/<benchmark_id>
```

## Loop

1. Confirm the Benchmark is **frozen** (`benchmark_config.toml` has `frozen = true`) and the current Agent State version is the Reference.
2. `save_snapshot(record)` before any edit.
3. Write a falsifiable hypothesis into `agent_state/evolution_log.md` (`append_hypothesis`).
4. Change only Test Agent State: `AGENTS.md`, a focused Skill, or safe `system_config.toml` fields.
5. Evaluate **every frozen Case**. Score the mean. Do not read `rubric/`.
6. Prefer `run_optimize_loop`：均分**严格更高**才接受，否则回滚。
7. 版本在评测中途变化则整轮作废（`version_changed`）。
8. 接受后才 `append_score`。到目标分或轮次上限停止。

Do not edit Project `.project_config.toml` or other Agents.
