---
name: benchmark-checklist
description: Vet a measured performance or latency number (limiter, tuning, errors, repeatability, relevance, whether the work ran) before reporting or acting on it, within this repository's bench, harness and resource rules.
---

Read the [pinned checklist](../../vendor/pstack-skills/benchmark-checklist/SKILL.md).
Apply these repository-specific interpretations where it differs:

- Gated numbers keep their owners. Bench, the L3 harness, the load tier and eval
  baselines follow [benchmarks and harness invariants](../../../docs/testing.md#benchmarks):
  one gate, one baseline, one environment; a re-baseline is a dedicated PR; never
  compare across environments or merge GPU numbers into runner baselines.
- Local, Kind and Colab-sandbox runs produce lab numbers. Report the venue, models,
  precision and concurrency with every number; none of them is a production or
  site latency claim.
- Stay inside the resource boundary of the applicable
  [verification minimums](../../../docs/live-stack.md#verification-minimums) and the
  [local environment](../../../docs/live-stack.md#local-environment) notes: one heavy
  job at a time, memory-capped. Paid or GPU sessions run only with approved budgets;
  when the approved runs are fewer than the checklist's five per side, report the
  actual count and range, and call a gap inside that spread inconclusive.
- Profile with tools the environment already provides. Never install or download
  tooling at runtime or in the air gap.
- The upstream "Perf issue", "Hillclimb" and "Opening a PR" playbooks are not
  installed. Record the primary number, run count, spread and limiter in the PR
  evidence per the [branch workflow](../../../docs/agent-workflow.md#git-workflow);
  raw private evidence stays outside git and contains no secrets or manual text.
