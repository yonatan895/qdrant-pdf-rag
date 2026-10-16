---
name: blast-radius
description: Find what a change can break beyond its diff (consumers, persisted state, wire formats, configuration and launch paths) and prove the one fact its safety rests on by running real code.
---

Read the [pinned workflow](../../vendor/pstack-skills/blast-radius/SKILL.md).
Apply these repository-specific interpretations where it differs:

- It produces the evidence behind the impact note in [AGENTS.md](../../../AGENTS.md)
  and the [review protocol](../../../docs/agent-workflow.md#review-handoff) question
  "what forbids the counterexample?". Start at the decision owner in the
  [context map](../../../docs/agent-workflow.md#context-map), then inspect callers,
  consumers, persistence, configuration and launch paths.
- The upstream `how` and `why` skills are not installed. Use CodeGraph before
  reading indexed code when `.codegraph/` exists, then git history and `gh` for the
  PRs and issues behind the code. Do not query external chat, tracker or
  observability services.
- The upstream `arena` multi-model fan-out is not installed. Delegate only bounded
  investigations under the [agent workflow](../../../docs/agent-workflow.md)'s
  parallel-work rule.
- Proof scripts follow [test design](../../../docs/testing.md#evidence-design):
  synthetic documents and faithful fakes for hermetic proof, live services only in
  their tier, exact round-trips for transported identifiers. A proof that guards a
  confirmed risk becomes a regression test in an existing behavior-focused suite.
- The upstream `unslop` skill is not installed; write plainly. Protected manual
  text, secrets and private paths never reach issues, PRs or logs.
- A risk blocks only when it is in scope and changes a user-visible outcome, data
  integrity or a security boundary; record others in the parking-lot issue named
  in [AGENTS.md](../../../AGENTS.md).
