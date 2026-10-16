---
name: code-review
description: Review a branch, PR or work in progress since a fixed point along two separate axes, Standards and Spec, as input to the repository's canonical review protocol.
---

Read the [pinned two-axis review](../../vendor/mattpocock-skills/code-review/SKILL.md).
The repository's [review protocol](../../../docs/agent-workflow.md#review-handoff)
stays authoritative; this skill supplies two of its inputs:

- **Spec:** the originating GitHub issue, read with `gh`, plus its acceptance
  comments and the [verification minimums](../../../docs/live-stack.md#verification-minimums)
  for the actual impact. No `/setup-matt-pocock-skills` tracker setup is needed.
- **Standards:** the owner contracts selected through the
  [context map](../../../docs/agent-workflow.md#context-map),
  [test design](../../../docs/testing.md), [AGENTS.md](../../../AGENTS.md) and the
  configured lint and type checks, plus the upstream smell baseline as labeled
  judgement calls. Skip what ruff or mypy already enforce.
- Record each material finding with the protocol's fields, then report its
  code assessment, verification, currentness and readiness verdicts. A finding,
  smell or standard breach blocks only under the protocol's blocking test;
  record others in the parking-lot issue it names.
- Run both axes in parallel only under the agent workflow's delegation limits;
  otherwise run them in sequence and keep their reports separate.
