---
name: tdd
description: Implement features and bug fixes one behavior at a time using failing tests, independent expected results, and the project's existing verification contracts.
---

Read the [pinned TDD reference](../../vendor/mattpocock-skills/tdd/SKILL.md),
including its testing and mocking examples when relevant. Resolve differences
through the repository's [test-design contract](../../../docs/testing.md):

- An approved issue or existing owner contract can establish the testing interface;
  existing authorization does not need another confirmation. Ask only when a
  material interface or expected behavior remains undecided.
- Prefer existing behavior-focused suites and independent expected results.
  Stored-content inspection, transport calls/order and lifecycle assertions are
  appropriate when they prove the actual contract. Public-operation checks alone
  do not replace the required persistence or next-operation proof.
- Hermetic tests use faithful boundary fakes; live storage belongs in the relevant
  integration tier. Choose the union of applicable
  [verification minimums](../../../docs/live-stack.md#verification-minimums),
  rather than imposing one testing interface on every layer.
- Preserve regression coverage during refactoring; map consolidated cases to
  their retained owner or explain retirement of an implementation-only pin.
  Use [codebase-design](../codebase-design/SKILL.md) when interface design is needed.
- Use the local [review protocol](../../../docs/agent-workflow.md#review-handoff)
  instead of the upstream reference to an uninstalled `code-review` skill.
