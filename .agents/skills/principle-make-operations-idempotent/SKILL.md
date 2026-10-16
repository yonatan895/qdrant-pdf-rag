---
name: principle-make-operations-idempotent
description: Design commands, lifecycle steps and processing loops to converge to the same end state after repeats, crashes and retries, judging leftovers only by evidence the owner contract defines.
---

Read the [pinned principle](../../vendor/pstack-skills/principle-make-operations-idempotent/SKILL.md).
Apply these repository-specific interpretations where it differs:

- Owner contracts decide convergence: the
  [identity](../../../docs/ingest.md#identity-contract) and
  [publication](../../../docs/ingest.md#publication-contract) contracts for ingest,
  publication, completion and retirement; the
  [Colab playground](../../../docs/colab-playground.md) for run state and cleanup.
  UUID5 chunk-key identity is protected; convergence work never changes it
  without its dedicated approved concern.
- Repository precedence: treat a leftover as stale only on the evidence its owner
  defines. When a process, port, lock or residue cannot be proven stale, fail
  closed and report it; never adopt or delete it automatically.
- Prove convergence with run-twice and crash-at-each-point cases using faithful
  fakes, real storage semantics in the simulation tier, and the publication and
  lifecycle rows of the
  [verification minimums](../../../docs/live-stack.md#verification-minimums). Prove
  the next ordinary operation after successful cleanup, not only failure and retry.
