---
name: agent-evaluation
description: Run one Test Agent on one frozen Benchmark case and return a 0-100 score. Use when scoring or evaluating an agent, or 评测.
network: general
---

# Agent Evaluation

Run the specified Test Agent on one Case exactly once, then score that single execution.

Return only a YAML document:

```yaml
status: ok
case_id: <case_id>
run: <1_based_run>
score: <0..100>
session_id: <id>
```

Do not print narration. Stop if the Agent State version changed or the Benchmark is invalid.
