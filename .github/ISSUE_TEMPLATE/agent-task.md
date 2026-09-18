---
name: Agent task
about: A bounded change with explicit authority, boundaries and evidence
---

Use short answers; N/A needs a reason. Field guidance and examples:
[agent workflow](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/agent-workflow.md#task-packet).

## Outcome and supported domain
User-visible goal; approved issue/ADR; acceptance owner.
One observable change; identify the actual producer/contract of its inputs.
Required invariant and one forbidden counterexample.

## Baseline and scope
Repository and inspected base SHA; relevant prior PRs/comments.
Allowed behavior changes; non-goals; scope requiring further approval.

## Boundary proof and impact map
Canonical contracts and decision owners.
Smallest case a plausible wrong implementation could pass locally but fail end-to-end.
Producers → storage/state → consumers, including deployment and UI where affected.
Reuse existing test homes; listed paths are a starting map, not a file allowlist.

## Next operation and state distinctions
For persisted/lifecycle changes: success -> cleanup -> next ordinary action, not only failure -> retry.
Where multiple paths implement the same rule, compare equivalent allowed and forbidden inputs.
Applicable missing-data, crash, retry, concurrent-reader/writer, cache, and rollback cases.

## Verification plan
Existing test homes and a minimal failing reproducer.
Evidence that distinguishes correct behavior from plausible wrong implementations.
Required commands/tiers; prerequisites; unavailable checks and acceptance impact.
[Verification minimums](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/live-stack.md#verification-minimums).

## Safety, migration, and limits
Protected data/services; isolation; required permissions.
Compatibility decision, migration and rollback expectations, or N/A.
Observable acceptance conditions, evidence locations, and remaining gaps.
