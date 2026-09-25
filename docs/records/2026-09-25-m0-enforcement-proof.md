# M0 dedicated-App enforcement proof

Status: **GitHub technical merge enforcement demonstrated; maintainer review of this record pending**.

Owners: [#482](https://github.com/yonatan895/qdrant-pdf-rag/issues/482) V1 and
[#411](https://github.com/yonatan895/qdrant-pdf-rag/issues/411).
Approved main: `633806e1f03ede7950f37db2f826aef2a922f6a1`.
Trial: [#499](https://github.com/yonatan895/qdrant-pdf-rag/pull/499), disposable,
**never merge**. No production code, corpus, default or baseline changed.

## Maintainer activation

The maintainer created environment `acceptance-publisher`, restricted it to the
branch `main`, installed the publisher App, stored its private key in that
environment and enabled the existing workflow. A client-ID variable typo and
missing repository installation were diagnosed and corrected before qualification.
No secret value was retrieved or committed; the agent changed no access settings.

Observed publisher identity: **`rag-acceptance`, App ID `5070649`**.
Its actual permissions are Actions/contents/pull-requests/metadata read and checks
write. The default Actions-token advisory path is disabled. The maintainer then
updated active ruleset `21729258` (`protect_main`) with no bypass actors:

- Required context: `current-candidate-acceptance`.
- Required integration: `5070649` (the dedicated App).
- Strict up-to-date branch policy: enabled.

The existing human lifecycle contract remains: agents start PRs as drafts; only
human `yonatan895` marks ready, formally requests changes or merges. The App's
technical result is not human review. Shared credentials do not establish a
technical human/agent distinction. No JSON review or impossible self-approval
was required.

## Actual negative and positive observations

Initial head `59ee5370a525c7030fb7b4467b79dac056ebade5` used test-merge
`8826b698b7a1368012218d94b6e62fc6c3c31b33` against the approved main above.

| Case | Actual evidence and result |
|---|---|
| Dedicated-App positive on draft | [Publisher 36108682157](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36108682157) succeeded; check `107987013174` from App `5070649` succeeded. Downloaded report: draft true, review null, technical verification passed. [Context 36108368767 attempt 1](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36108368767/attempts/1): 106 executed, zero failed/errors/skips, exit 0; artifact `10851439862`. |
| Cancelled latest execution cannot reuse old green | Context run `36108368767` was rerun and attempt 2 cancelled, preserving successful attempt 1. [Publisher 36109889284](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36109889284) selected attempt 2, context cancelled/SELECTED_SKIPPED, verification incomplete; App check `107990790473` failed. |
| Same-name user success cannot override failed required App | Synthetic commit status `54913542716`, created by user credentials, deliberately reported success for `current-candidate-acceptance`. It was explicitly labeled a negative probe, not verification or review. GitHub reported `mergeStateStatus: BLOCKED`. |
| Block is independent of draft state | The maintainer marked #499 ready at `2026-09-25T07:54:26Z`. Read-only API then showed `isDraft:false` and `mergeStateStatus:BLOCKED`; the maintainer confirmed in the task conversation: “Made ready for review, it still blocks.” No agent performed this transition or attempted a merge. |
| Missing required source, with only wrong-source green | New head `10cf59c158f4d40e12274353740a35700f8ccdee` retained the same file tree. Synthetic successful commit status `54913678819` used the required context name. Two observations bracketed GitHub's PR-state read with check-run lists: neither contained a check from App `5070649`. After initial UNKNOWN recalculation, the ready PR was BLOCKED at that exact new head. Old-head App success and current-head user success did not fill the missing required source. |

The wrong-source signals above are actual **commit statuses**, not check runs
from a second App. The user-token check-run creation attempt was refused with
HTTP 403 (App authentication required); no permissions were widened. Native
GraphQL state and the maintainer's disabled-merge observation establish actual
merge blocking. No merge API request was made, and no rejected HTTP merge request
or attempted bypass is claimed.

## Recovery and completion boundary

Ordinary CI on the fresh head completed the recovery test: the seven-line
synthetic document is the only net diff and no test/policy/workflow hook needs
cleanup. [Context 36110223345](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36110223345)
executed 106 tests with zero failures/errors/skips and exit 0, binding new head
`10cf59c158f4d40e12274353740a35700f8ccdee` to test-merge
`87f30e1a680a1de965aadceaa331b35142d85ea2` and the same approved base.
[Publisher 36110285956](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/36110285956)
completed successfully; required-App check `107992147990` succeeded. Downloaded
report confirms current technical verification passed. GitHub then reported
`isDraft:false`, `mergeStateStatus:CLEAN`. The wrong-source status alone had not
unblocked the PR; fresh successful evidence from the required App did.
The maintainer retains all merge authority; the disposable PR must never merge.

[V0/V2 and the earlier V1 matrix](2026-09-25-m0-rollout-audit.md) retain their
qualified scope and evidence: equal same-target dependency inventories,
prerequisite refusal before testing/mutation, ten intended historical hazard
kills, and live native artifact/identity/policy negatives. This record addresses
the previously missing GitHub maintainer-enforcement boundary; it does not
qualify actual internal GitLab runners, site deployments or future product
contracts. Temporary local snapshots corroborate the linked native evidence;
Actions artifacts remain subject to retention. Subsequent code/settings changes
require their own applicable verification.
