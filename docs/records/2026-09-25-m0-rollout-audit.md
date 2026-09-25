# M0 rollout audit after native maintainer decisions

Status: **M0 remains open: technical publisher deployed, merge enforcement absent**.
Owners: [#482](https://github.com/yonatan895/qdrant-pdf-rag/issues/482),
[#370](https://github.com/yonatan895/qdrant-pdf-rag/issues/370),
[#371](https://github.com/yonatan895/qdrant-pdf-rag/issues/371) and
[#411](https://github.com/yonatan895/qdrant-pdf-rag/issues/411).
Inspected main: `dcb28c4fc40ba792c22ca31c3becf93e25f1e0fb`.

## Approved scope and evidence reuse

The maintainer merged #494, #495, #492 and #497. The explicit decision in #497
replaces the required JSON-review gate with native GitHub decisions: every PR
starts draft; only the human `yonatan895` marks ready, formally requests changes
or merges. CI verifies technical evidence only. The previous requirement for a
human `changes_required` JSON verdict behind a green review-signal job, automatic
review templates and the draft-state veto is superseded, not passed. Do not ask
for another structured human review to finish M0.

[The earlier trial record](2026-09-25-m0-acceptance-trial.md) retains the actual
historical observations. Disposable #491 was closed unmerged at the maintainer's
request. Its old review-related refusals describe the old policy; artifact,
identity, selected-lane and policy-tampering trials remain relevant technical
proof. Shared account credentials cannot enforce the human/agent distinction.
The [canonical workflow](../agent-workflow.md#git-workflow) is the current owner.

## Requirement-by-requirement audit

| Requirement | Inspected evidence | Disposition and limits |
|---|---|---|
| V0: complete same-target dependency inventory | [#485](https://github.com/yonatan895/qdrant-pdf-rag/pull/485): complete 60-runtime/75-dev/4-build profiles, two fresh offline environments with equal observed inventories, both offline images and actual archive/SBOM inspection. Retained `/tmp/m0-clean-dev-{a,b}/inventory.json` compared again during this audit. | Demonstrated for Linux x86_64 CPython 3.14 GIL. Locks, dependency preparation/inventory scripts and Containerfiles are unchanged since the qualified merge. Not byte-identical-image or whole-OS inventory qualification. |
| V0: fail before testing or mutation on missing/tampered prerequisites | [#484](https://github.com/yonatan895/qdrant-pdf-rag/pull/484) and [#485](https://github.com/yonatan895/qdrant-pdf-rag/pull/485): pinned Helm/Task diagnosis; actual missing/tampered wheels exited 2 before environment creation. [#487](https://github.com/yonatan895/qdrant-pdf-rag/pull/487): missing/tampered cached BM25 stopped before pytest, digest-bound Qdrant launch with no implicit pull. [#489](https://github.com/yonatan895/qdrant-pdf-rag/pull/489) repaired explicit benchmark preparation. | Demonstrated supported development path. Local/GitHub/GitLab commands consume the same lock contract; actual internal GitLab runner/mirror and site transfer remain unexecuted, not claimed passed. |
| V1: selected execution, identity and artifact refusal | [Historical live matrix](2026-09-25-m0-acceptance-trial.md): failed, cancelled, skipped and zero-test runs; actor/run receipt tampering; genuine older-receipt replay; head/base movement; docs-only/mixed-path selection; candidate policy cannot relax approved-base obligations. #495 adds the live default-ref guard. | Technical implementation and negative evidence retained. Review comments cannot replace missing native execution. Native producers for selected `agent_probes`/`eval_retrieval` remain unavailable and those obligations remain missing, not waived. |
| V1: current native human lifecycle and publisher health | [#497 native CI](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36103989489) passed all selected technical jobs. [Deployed publisher 36106047135](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36106047135) ran at the inspected main and succeeded while emitting an incomplete per-candidate check (`107978764786`) for the remaining old candidate. | Bulk publication success is distinct from candidate success. The deployed draft/no-JSON positive trial below passed; current candidate evidence also remains in the audit PR body. |
| V1: selected acceptance cannot silently disappear | Read-only ruleset/environment inspection below. | **Not achieved.** Advisory check success/failure is not merge prevention. Dedicated source-bound required-check activation and actual rejected-merge tests remain maintainer work. |
| V2: selected critical hazards fail for the intended behavior | [#486](https://github.com/yonatan895/qdrant-pdf-rag/pull/486) introduced ten isolated mutation cases. Actual uploaded report from [#497 CI](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36103989489) has all ten baseline passes/intended kills at test-merge `f34811c1ba687d37ea5091979e5ca6b3e8a3a98e`. Catalogue and runner are unchanged since #486. | Demonstrated for the selected ten cases. Exact-reference/revocation cases remain explicitly owned by #405 until the product contract exists; no global mutation-coverage or production-fault guarantee is claimed. |

Historical temporary files are session-local corroboration; the linked PRs and
Actions artifacts identify the original executions and their retention limits.
This audit does not rerun unaffected product lanes merely to increase counts.

## Deployed draft/no-JSON positive trial

Ordinary audit [PR #498](https://github.com/yonatan895/qdrant-pdf-rag/pull/498)
was opened as a draft at head `27f76f4a0bcf8e6e3672adac72de15d6f0ac6149`,
base `dcb28c4fc40ba792c22ca31c3becf93e25f1e0fb`, test-merge
`39208c276e1a5b99aaf95a69ac9b005488e6d25d`.
[Context 36106373331](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36106373331)
executed 106 tests with zero failures/errors/skips and exit 0.
[Approved-main publisher 36106406910](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36106406910)
completed successfully and published check `107979908734` as success.
The actual downloaded report has schema 2, `draft:true`, `review:null`,
`verification_status:passed`, and the exact candidate identities above.
Only context was required; skipped product lanes were reported unselected.

Read-only issue-comment and review queries each returned zero records. The PR
remained draft after success, with maintainer merge decision pending. No agent
submitted a review, promoted the PR, or merged it. This proves deployed technical
acceptance without human JSON and without a lifecycle transition. It does not
prove source-bound required-check enforcement or a human ready/merge action.

## Actual remaining enforcement boundary

Read-only API inspection on 25 September 2026 found ruleset `21729258`
(`protect_main`) active with deletion, non-fast-forward and pull-request rules.
There were **no required status checks**, zero required approving reviews and
no required conversation resolution. Only environment `copilot` existed;
`acceptance-publisher` was absent and repository variables were empty.
The current workflow therefore uses its advisory Actions-token publisher.
No access, App, environment, secret or ruleset setting was changed by this audit.

The maintainer activation procedure remains in
[the canonical workflow](../agent-workflow.md#review-handoff): dedicated App with
Actions/contents/pull-requests read and checks write; its private key only in the
`main`-restricted `acceptance-publisher` environment; client-ID and enable
variables; require `current-candidate-acceptance` from that specific App.
Do not require impossible self-approval or substitute the advisory Actions App.
The human lifecycle contract remains in force before and after activation.

After activation, record native rejected-merge evidence for missing/stale
technical results and a same-name check from an untrusted source, then recovery
with fresh complete evidence from the required App. Only the maintainer performs
merge decisions; an agent must not attempt a merge to test a setting that might
allow it. A green workflow, a protected-branch flag, a local collector probe or a
ruleset JSON snapshot alone does not prove this enforcement. Until that evidence
exists, M0's end-to-end acceptance criterion remains unmet.
