---
name: diagnosing-bugs
description: Diagnose application bugs and performance regressions with a reproducible failure, targeted hypotheses, and a regression test at the affected contract.
---

Read the [pinned diagnostic workflow](../../vendor/mattpocock-skills/diagnosing-bugs/SKILL.md).
Apply these repository-specific interpretations where its guidance differs:

- Select the decision owner through the [context map](../../../docs/agent-workflow.md#context-map).
  Use CodeGraph before reading indexed code when `.codegraph/` exists.
- Choose the feedback loop and required checks through
  [verification minimums](../../../docs/live-stack.md#verification-minimums).
  Use the pinned Task entry; an unavailable prerequisite is distinct from a
  reproduced product failure. Continue independent safe work when one loop is blocked.
- Follow [test design](../../../docs/testing.md): synthetic documents and faithful
  fakes for hermetic tests; actual stored content, lifecycle transitions and the
  next ordinary operation when the changed guarantee requires them.
- Capture only sanitized diagnostics. Protected corpus text and credentials stay
  out of logs, reproduction artifacts and git; the HITL template captures only
  non-sensitive observations.
- For Qdrant server performance or operational diagnosis, also use the existing
  [Qdrant skill routing](../../../docs/agent-workflow.md#qdrant-skills).
  Finish with the repository's [review protocol](../../../docs/agent-workflow.md#review-handoff).
