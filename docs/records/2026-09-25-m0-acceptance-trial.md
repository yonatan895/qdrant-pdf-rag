# M0 acceptance trial — 25 September 2026

Status: **partial acceptance evidence; M0 V1 remains open**.
Owner: [#482](https://github.com/yonatan895/qdrant-pdf-rag/issues/482),
with [#411](https://github.com/yonatan895/qdrant-pdf-rag/issues/411).
Contract: [current-candidate acceptance](../agent-workflow.md).

The disposable [PR #491](https://github.com/yonatan895/qdrant-pdf-rag/pull/491)
uses approved base `d390f0cc9ad6de4566ff02cc97da82529253bc57` (#490).
It must not be merged. Deliberate failing tests, empty discovery hooks,
skipped-job conditions and policy relaxation were removed afterward; at
`73515ac4c6f584b3e52b63a3fa448c250e58791e` its only net change is a
seven-line synthetic Markdown trial document. No repository settings changed.

## Observed native publication

Each row refers to an actual GitHub publisher report/check and, where relevant,
the native producer receipt. A failed publisher workflow can contain a successful
check for one PR and a refused check for another; the per-candidate report is the
authority for these observations. Early generic `unavailable_or_changed` reports
were not counted as proof of specific rejection reasons.

| Trial | Candidate head | Observed result and evidence |
|---|---|---|
| Docs-only positive | `9a956dd3d72fcbbb61d80ab2056e7ae49a997ba4` | Native context executed 106 tests; the maintainer's [independent structured review](https://github.com/yonatan895/qdrant-pdf-rag/pull/491#issuecomment-5822648661) supplied current review. [Publisher 36062603335](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36062603335), check `107844893142`: `ALL_MET`, success. |
| Draft with otherwise valid review | Same head | [Publisher 36062750848](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36062750848): draft true, same review, not ready. |
| Latest attempt cancelled | Same head | [Context attempt 2](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36056110713/attempts/2) cancelled. [Publisher 36062824310](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36062824310), check `107845619156`: current valid review, ready PR, required context cancelled; older successful attempt 1 was not used. |
| Ordinary retry after cancellation | Same head | Context attempt 3 succeeded. [Publisher 36063043435](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36063043435), check `107846324518`: success with attempt 3 and the same review. |
| Head movement and mixed storage/deployment paths | `ffc33a69800d8f195ce7a111f52aa26b5e593ad7` | [Publisher 36063546102](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36063546102), check `107848044125`: full profile; old review not ready. Actual collection reported exact head/execution mismatch. All eleven contract-selected lanes were required, including packaging, load and HA. |
| Required test fails | `3ecd80e30d5ec41428ea27a0afabba780bc3125b` | [Context 36063840043](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36063840043): 107 executed, one intentional assertion failure, zero errors/skips, exit 1. [Publisher 36064080503](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064080503), check `107849798389`: context `SELECTED_FAILED`. |
| Zero executed tests | `1394a44cd9c06d0515266426557319d66ee1c11a` | [Context 36064304296](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064304296): zero executed/failed/errors/skips, but receipt `passed:false`, exit 1. [Publisher 36064368834](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064368834), check `107850660476`: context `SELECTED_FAILED`. |
| Cleanup after failing/zero-test hooks | `14cc36ac8efd5afef824aff5e0857f3059b68bf1` | [Context 36064544709](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064544709): ordinary suite restored, 106 tests passed. |
| Required native job explicitly skipped | `9bad19115c21be99a3a16019f5b9095be729852c` | [Context 36064640715](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064640715): job skipped. [Publisher 36064692529](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064692529), check `107851695831`: context `SELECTED_SKIPPED`, not ready. |
| Candidate removes required lanes | `d4e50009013bdf3f9606bc8a7525c1710deb4ab2` | Candidate `required_lanes` returned an empty set. Approved-base actual-blob comparison refused changed policy bytes before receipt claims. [Publisher 36064875002](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064875002), check `107852254843`: failure. The public fixed error alone does not identify the policy mismatch; the read-only collector establishes that cause. |
| Policy/workflow cleanup | `73515ac4c6f584b3e52b63a3fa448c250e58791e` | [Context 36064977781](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36064977781): 106 tests passed after exact restoration to approved base. |

The mixed-path trial selects context, lint/types, units, simulation, hazards,
load, HA, L1, packaging, agent probes and reviewer. It does not select semantic
retrieval evaluation: its known test/deployment paths do not change semantic
representation. Scheduling/selection is the claim; superseded in-progress unit
runs are not represented as completed verification.

## Controlled corruption of real evidence

These are **normalizer boundary tests using real API records and an actual
artifact**, not actual wrong-actor executions or published forged evidence.
The original receipt passed normalization before each independent corruption.
Approved-base code ran without candidate imports or artifact-provided commands.

Source: context run `36064977781`, attempt 1, job `107852514169`, artifact
`10836002882`, ZIP digest
`sha256:6db115a94877fc81bb611c295fd0df1a6690741320ef9b46c021effa81f6898a`.
Head `73515ac4c6f584b3e52b63a3fa448c250e58791e`, execution
`43e81f9678d35b56bc7d84315a5354862078bebc`; its native report has 106 executed
and zero failed/error/skipped tests.

All nine corruptions were refused with `ValueError` by `normalize_native`:

- Change only API actor ID; change only triggering actor login.
- Change job run ID; change job attempt number.
- Change artifact workflow-run ID; replace its ZIP digest with a mismatching digest.
- Change expected current base SHA.
- Rebuild the ZIP and recompute its digest after changing the receipt to zero
  executed tests, while retaining claimed success.
- Rebuild the ZIP and recompute its digest after setting one skipped test,
  while retaining claimed success.

Local execution: `/tmp/m0-trial-identity-probes.py` using the prepared CPython
3.14 environment, exit 0; report `/tmp/m0-trial-identity-probes.json`.
These temporary paths are session-local, not durable downloadable artifacts.
Native receipts and publisher reports are in the linked Actions run artifacts
(subject to repository retention). No vendor corpus or private content was used.

## Remaining acceptance

Live wrong-actor/run/artifact and marker cases, an authorized current
`changes_required` verdict despite a green review-signal job, actual base
movement, and maintainer-only dedicated-App/ruleset activation and rejected-merge
proof remain outstanding. Controlled corruption does not close the live cases.
The Actions-token check remains advisory. The shared-account human review path
worked, but GitHub identity alone cannot distinguish human and agent use of the
same credentials. Agents submitted no approval and did not merge trial work.

A reviewed merge of this evidence record can provide an ordinary base change
for the next trial; it is not itself proof that base-movement handling works.
Do not mark M0 complete from this record or from green native CI alone.
