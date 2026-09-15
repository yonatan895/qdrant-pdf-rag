---
name: Agent task
about: A bounded change with explicit authority, boundaries and evidence
---

Use short answers; N/A needs a reason. Field guidance and examples:
[agent workflow](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/agent-workflow.md#task-packet).

## Outcome and authority
User-visible goal; approved issue/ADR; acceptance owner.
Required invariant and one forbidden outcome.

## Baseline and scope
Repository and inspected base SHA; relevant prior PRs/comments.
Allowed behavior changes; non-goals; scope requiring further approval.

## Read first / impact map
Canonical contracts and decision owners.
Producers → storage/state → consumers, including deployment and UI where affected.
Listed paths are a starting map, not a prohibition on examining affected callers.

## Assumptions and counterexamples
Supported modes/topology and who may mutate shared state.
Applicable missing-data, crash, retry, concurrent-reader/writer, cache,
rollback, and configuration cases. Explain exclusions.

## Verification plan
Existing test homes and a minimal failing reproducer.
Evidence that distinguishes the correct behavior from a plausible wrong one.
Required commands/tiers; prerequisites; unavailable checks and acceptance impact.
[Verification minimums](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/live-stack.md#verification-minimums).

## Safety, migration, and rollback
Protected data/services; isolation; required permissions.
Migration and rollback expectations, or N/A.

## Completion
Observable acceptance conditions, evidence locations, and remaining gaps.
