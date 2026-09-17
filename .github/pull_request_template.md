## Scope
Issue/acceptance item; base SHA; one-sentence outcome.

## Contract and impact
Invariant changed or preserved; affected boundaries.
Defaults, API/schema, identities, operational requirements, and rollback impact.

## Evidence
| Claim/counterexample | Test or command | Tested SHA | Result | Evidence |
|---|---|---|---|---|

Distinguish observed results, static reasoning, proposed tests, and checks not run.
For test consolidation: old case → retained behavior/new owner, or retirement reason.
Group related evidence; no artifact is required for every trivial assertion.
[Verification minimums](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/live-stack.md#verification-minimums).

## Limits and follow-ups
Remaining gaps, blocked validation, adjacent discoveries, and their issue owners.
No blanket Fixes/Closes reference for a parent whose acceptance is only partially met.

## Self-review
Diff and affected callers inspected; public/private-data boundaries preserved;
canonical docs updated without conflicting copies; reviewer decision still required.
[Review and handoff](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/agent-workflow.md#review-handoff).

## Acceptance record
The author supplies candidate attribution and evidence; the reviewer supplies independent
assessment; the maintainer records explicit merge authorization. Do not tick a field as
someone else's approval. When scope narrows or a stacked PR is retargeted, update title, scope,
dependencies, evidence, and limits before readiness. Preserve historical review/run links;
do not rewrite an old failed attempt as a success. A new head invalidates automatic readiness
until reconciled. The final record must name the actual candidate.

### Candidate attribution and author claims
- **Candidate head SHA**: `<commit-sha>`
- **Base SHA**: `<base-commit-sha>`
- **Author claim**: [Invariant or behavior claimed complete]
- **Execution SHA / test-merge**: `<sha or not run>`
- **Known limitations**: [not implemented vs not verified; owning issue]

### Independent reviewer assessment
- **Reviewed candidate SHA**: `<evaluated-commit-sha>`
- **Code assessment**: `acceptable` | `changes_required` | `incomplete`
- **Required verification**: `complete` | `incomplete` | `failed`
- **Candidate currentness**: `current` | `stale` | `unverified`
- **Merge readiness**: `ready_for_maintainer` | `not_ready`
- **Material findings**: [IDs -> dispositions (fixed-and-verified / disproven-with-evidence / accepted-by-authorized-owner / unresolved), or none]
- [Canonical review protocol](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/agent-workflow.md#review-handoff)

### Verification run links
- **Check-context & lint/types**: [Link to run/log]
- **Targeted suite / reproducer**: [Link to test execution]
- **Risk tier selected**: [prose-only | test/tool-only | publication/retirement lifecycle | extraction/ranking | HTTP/lifecycle | packaging/deploy | release promotion]
- [Verification minimums](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/live-stack.md#verification-minimums)

### Maintainer merge decision
- **Decision**: `approved` | `changes_requested` | `rejected`
- **Maintainer**: `@username`
- **Rationale**: [Explicit merge decision; agents never self-merge or alter repository access]
