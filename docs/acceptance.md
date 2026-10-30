# GitHub acceptance and verifier decisions

Owner for the GitHub-side machinery behind the [review protocol](agent-workflow.md#review-handoff):
maintainer lifecycle decisions, native execution evidence, verifier implementation and
catalogue-anchor decisions, and one-time triage of a recurring baseline failure. Read it
when a task reaches those steps; it is reference, not entry-path reading.

## Maintainer decisions and technical evidence

Use GitHub's draft/ready, review and merge controls for the PR lifecycle. The
human maintainer `yonatan895` owns those decisions; no JSON review comment,
manual commit-ID transcription, or second approval table is required. A ready
PR means the maintainer requested review, not that CI has approved the code.
Agents may provide findings and evidence as comments but never submit formal
APPROVE/REQUEST_CHANGES reviews, mark ready, or merge on the maintainer's behalf.
Optional structured model-review artifacts are diagnostic only and cannot grant
or revoke technical verification or a human decision. Historical findings remain
visible for the maintainer to assess; CI does not parse comment history as votes.

## Native evidence and consumer rollout (#411)

`scripts/ci_evidence.py` records the actual native job invocation and its PR
head, tested base and execution identity ([merge ref](testing.md#native-ci-execution-evidence)). Test lanes count actual test
records, reject zero tests, skips and xfails, and refuse pre-existing reports. Receipts
use attempt-specific artifacts. After "re-run failed jobs", the latest
attempt's record decides each job; a job carried unchanged from an earlier
attempt binds that attempt's run, job and artifact, never the copy's.
Packaging receipts identify the checkout only;
the native packaging job must finish successfully before they can contribute
acceptance. A receipt is never a code review or permission to merge.

`scripts/acceptance_evidence.py` owns native job/artifact mappings and bounded,
data-only receipt validation. The consumer must fetch current native run, job,
attempt, artifact and commit records, paginate collections, and compare the
artifact digest and actual raw test records. Both the tested execution and
GitHub's current PR test merge must have the exact ordered current base/head
parents. GitHub may regenerate that merge with a different timestamp and SHA;
the consumer accepts native evidence across those SHAs only when independently
fetched commit records also have identical valid Git tree IDs. Matching parents
alone cannot authorize a different merge tree; receipt-supplied tree claims are
not trusted. Native records and the check summary retain the actual tested SHA,
and hazard results must bind that tested execution. Maintainer verifier decisions
accept a regenerated test merge by this same rule (#753).
ZIP members are read in memory; they are never extracted, imported, or executed.

The approved base supplies the Verifier-owned bytes (`verification_inputs` in
`scripts/acceptance.py`): policy, producer and workflow bytes, Task dispatch
(the root Taskfile, included modules, wrapper and binary pin), and the critical
hazard runner/catalogue. Unit coverage additionally binds the pytest selector,
root configuration/conftest and locked preparation inputs. Each shard retains an
independent full collection and actual executed node IDs; the consumer compares
raw JUnit identities and proves the shards form a disjoint complete union.
New tests change the expected set through actual collection, not a count update.
See [the unit coverage contract](testing.md#unit-coverage).
Their candidate bytes are read independently from the commit API; receipt-supplied hashes alone
do not attest which source was executed. Hazard reports must also contain the
complete approved challenge set exactly once, bind its catalogue/runner hashes
and candidate execution, and record each expected baseline pass and intended
behavioral kill. A valid artifact digest over an empty, reduced or surviving
challenge report cannot establish acceptance. A candidate's
workflow cannot approve its own replacement verifier. Producer/bootstrap changes
use the exact-candidate workflow below before the consumer can trust those
bytes. This does not waive their tests. The privileged
publisher, when enabled, must execute approved-base code only and recheck PR
currentness before publishing. Native API provenance and valid receipts do not
substitute for the maintainer's review and merge decision.

<a id="verifier-update-decision"></a>
## Approving verifier implementation updates

A PR that changes a Verifier-owned path needs a
separate trust decision. Marking it ready does not grant that trust. After
reviewing the actual changes, **only the human maintainer yonatan895** runs
Actions → **Verifier update decision** → Run workflow, selects **main**, enters
the PR number, leaves **kind: implementation**, and selects **approve**. No SHA
or JSON is entered manually.
Agents must never dispatch or rerun this workflow on the maintainer's behalf.

**PR comment path.** The same decision can be made on the PR itself: the
maintainer comments `/verifier approve <head-sha> [catalogue-anchors]` (at
least 7 lowercase hex characters of the head they reviewed; kind defaults to
implementation) or `/verifier revoke [catalogue-anchors]`. The same workflow
file runs on `issue_comment` **created** from main, so PR code cannot change
it and is never checked out. It acts only for a `yonatan895` comment with
author_association OWNER on a PR, re-reads the live comment (unedited, same PR,
same text), and refuses unless the named SHA is the PR's current head, so a
push between viewing and commenting is not approved. The recorded evidence is
the dispatch record plus a `comment` binding (id, creation time, head SHA);
the consumer re-reads that comment and requires it to still match the current
head. Because the comment itself updates the PR timestamp, the head-SHA
binding replaces the dispatch path's "updated before dispatch" rule. The run
stays one read-only `record-decision` job (the consumer accepts exactly one
successful job); completing it re-evaluates candidate-acceptance exactly as a
dispatched run does. A separate `verifier-feedback.yml`, triggered when that
run completes, reacts to the comment and replies with the recorded SHA, or
reports that nothing was recorded; it never records or validates a decision. Deleting
or editing the comment withdraws the approval. Comment runs by anyone else are
skipped and ignored. Dispatch remains the fallback.
The workflow does not mark ready, submit a PR review, merge, or change settings.

Approved-main code reads the current same-repository PR and records its exact
base, head, GitHub test-merge SHA, and verifier hashes as a native artifact. It
never checks out or executes the candidate. The publisher refuses snapshots of
a PR updated at or after the dispatch, so a queued run cannot approve a newer
state than the human click. Unrelated PR metadata changes can conservatively
require another dispatch. The decision workflow's success means the snapshot
was recorded; the acceptance check determines whether it can be used.
The publisher validates the native
run, attempt, actor, successful job, artifact digest and contents against current
API state. The newest decision for this PR is authoritative; a pending, failed,
cancelled, expired, malformed or **revoke** decision blocks fallback to an older
approval. Use the same workflow with **revoke** to withdraw trust. The publisher
runs after workflow completion and on its existing periodic reconciliation;
revocation is not an atomic merge lock. An already-running merge is not undone.

Any base, head, test-merge tree or verifier-byte change invalidates approval. Rerun the
workflow only after reviewing the new candidate. Human identity is still a
working-agreement boundary: shared account credentials cannot distinguish an
agent from a human. The actor check does not claim otherwise.

This path changes which verifier implementation bytes may produce evidence.
The publisher still executes approved-main code and applies the approved-main
lane selector, complete hazard catalogue and receipt schema. Native jobs must
pass, their raw evidence must validate, and candidate/run/decision identity is
rechecked before publication. An exact decision can also authorize the selector
bytes executed by candidate jobs, including `scripts/review_tooling.py`. Native
receipts must identify those approved candidate bytes. This is execution
provenance only: the publisher never imports the candidate selector and still
derives all required lanes from approved-main code. A candidate that omits an
old-policy-required lane still fails, even if its proposed policy would omit
that lane. The reported policy digest continues to identify the approved-base
selection policy; the approved input map separately identifies execution bytes.
The proposed selection rules take effect only after maintainer adoption into
main. Implementation decisions exclude hazard-catalogue changes; incompatible
evidence schemas still fail. Forks cannot use this path.
Normal PRs with unchanged Verifier-owned paths need no decision.

For unit-layout changes, merge approved-main compatibility support before
activating a different producer matrix. The consumer supports only the complete
layouts `unit_producers` accepts, read from independently approved workflow bytes, and
checks every shard's exact execution plus the complete disjoint union. An exact
verifier decision authorizes producer bytes, not a smaller receipt-declared
coverage obligation. Refresh activation PRs after the compatibility merge and
obtain a new exact-candidate decision; old approvals do not transfer.

## Approving catalogue anchors

#718: only the human maintainer runs **Verifier update decision** on **main**
with **kind: catalogue-anchors** and **approve** or **revoke**. Receipts bind exact
repository/base/head/test-merge, raw catalogue hashes and changed IDs. Only
nonempty, distinct `before`/`after` strings may change; all other typed fields,
membership/order, targets, tests, assertions, occurrence counts and deferred
obligations remain identical. Other changes remain unsupported.
Ordinary approval is insufficient; mixed changes require both decisions.

Approved-main fetches candidate data through the API without execution, retains
main obligations, and validates complete native baselines/kills against approved
anchors. Old, mixed or incomplete reports fail. Each kind's latest decision is
rechecked immediately before publication; revocation retains the limits above.

Bootstrap keeps `critical.json` unchanged: adopt reviewed consumer support,
refresh dependent PRs, then obtain new exact-candidate decisions against adopted
main. Base/head/test-merge tree changes invalidate approvals. No bypasses
or disabled checks.

Both GitHub and GitLab L1 comment publishers append historical reports with
candidate/run attribution. They never acquire ownership of an existing comment
from its marker. Comments remain navigation aids; native evidence is the gate
input. GitLab reports an unavailable target SHA explicitly when its pipeline does
not supply one, rather than substituting the diff base as the tested target.

`scripts/acceptance.py` assembles current native **technical verification**.
Its read-only entry point is
`python -m scripts.acceptance --repository OWNER/REPO --pr NUMBER` from the
approved base checkout. It exits nonzero for unmet, unavailable or changing
technical evidence. The `current-candidate-acceptance` check name is retained,
but it reports technical obligations only. Machine summary schema v2 exposes
`verification_status` (`passed`/`incomplete`) instead of a readiness recommendation.
There is no reviewer lane, required review JSON,
comment-history parsing or draft-state veto. Required missing/skipped/failed
execution still fails; passing checks on a draft do not mark it ready.

`--publish` creates a pending check before collection and publishes its result
only after rereading candidate and latest run/attempt identities and the live
default-branch ref. PR merge metadata can lag a base push; matching old snapshots
alone do not establish currentness. The approved checkout, live ref and candidate
base must agree; a base behind the live tip reports `candidate_base_not_current`
with both SHAs and the fix (update the branch). No new SHAs are substituted into old evidence. `--all-open`
paginates candidates; file pagination must match the changed-file count and
retain both rename names verbatim.

A required lane with no evidence leaves the check in progress and non-passing
only while at least one native run bound to the current candidate is queued or
in progress. The run must match the current head and PR, have been created after
the current test merge, and its completed jobs' receipts must validate; forged
or corrupt evidence fails immediately. A missing lane from a completed run, a
failed, skipped or cancelled lane, an unverified receipt, or a run that cannot be
attributed to the current head/test merge completes as failure. A completed
neutral check would satisfy the required check on this repository, so pending is
never published as success or completed neutral: it stays in progress and
non-passing while the next native workflow event or the periodic reconciliation
re-evaluates it.

In bulk publication, each candidate gets its own success/failure check. A
successfully completed reconciliation exits zero even when some candidates have
unmet technical obligations. Errors preventing candidate listing or check publication still fail the workflow;
per-candidate evidence lookup failures remain red candidate checks.
Thus the publisher's process status is not another PR's verification result.
Read-only and single-PR commands retain nonzero exits for unmet obligations.

Automatic review-template comments and CI review-template artifacts are retired.
No comment-writing permission is needed by the publisher. Legacy explicit
`--review-template` tooling remains optional for old integrations; generated or
submitted JSON has no role in the native verification check. Its identity lookup
still refuses stale candidates. Selected `agent_probes`/`eval_retrieval` remain
technical obligations. The dedicated `agent-probes.yml` producer runs
`tests/live_agent_probes.py` against real loopback HTTP, disposable pinned
Qdrant/OpenObserve and a deterministic model stand-in. Its receipt must contain each
of the four named transport/lifecycle tests exactly once: live contracts and a
fresh trace, fixed overlong error, buffered/streamed final integrity, and upstream
cancellation followed by a successful ordinary request. Scripted refusal checks
propagation and citation labeling, not semantic model security or quality.
`eval_retrieval` is supplied by the dedicated `ci.yml` `eval-retrieval`
producer, which runs the same `scripts/gate_l1.py` L1 retrieval eval the gate
lane runs (synthetic fixtures, disposable Qdrant, hash-mode pipeline integrity:
ingest, indexing, prefetch filters, RRF, scoring — not live-model semantic
quality). Its receipt carries the structured per-class report. A review comment
never supplies execution evidence or waives a lane.

Evaluation code under `src/mainframe_rag/eval/` is tooling, including code moved
from the evaluation scripts under #508. Package and `scripts/eval_retrieval.py`
changes (including retired delegate paths) select context, lint/types,
unit/hazard evidence, simulation and
the synthetic L1 gate. Package Markdown alone does not select service lanes.
The existing native CI producers dispatch these obligations from the selector;
no new evidence format or human attestation is involved. Mixed diffs retain the
union of obligations: production retrieval/embedding changes still select real
semantic `eval_retrieval`, deployment changes retain packaging, and unknown
source paths still fail closed to the full profile. Review actual behavior
against the verification minimums; a tooling location never waives a semantic
change's evidence requirements.

This classification takes effect only after its policy is adopted into the
approved base and dependent PRs are refreshed. An exact maintainer decision
can authorize candidate selector bytes as execution provenance, as described
[above](#verifier-update-decision); it cannot make the publisher apply the
candidate's proposed lane-selection rules before adoption.
The separate missing real-model producer remains unresolved for changes that
actually select `eval_retrieval`.

`acceptance.yml` schedules on PR metadata, native workflow activity and base
pushes, with periodic reconciliation for missed signals. Review/comment activity
is not an input to technical verification. Both publisher paths check out `main`
explicitly with no persisted git
credentials, install no project dependencies, and never execute candidate code
or artifact commands. The obsolete review-signal workflow is removed.
Publication is serialized per candidate head SHA: PR events and workflow_run
events of `pull_request` runs evaluate only the open PRs at that head, in one
group per SHA. Base pushes, the periodic sweep, manual dispatch and non-PR
workflow runs share one group and reconcile every open PR. An event for a
superseded head selects nothing. API snapshots and events are not an atomic merge
transaction; a state change can occur after the final read, and GitHub scheduling
can be delayed. The chosen review policy, branch currency and conversation
resolution must be enforced by the maintainer's repository configuration.

The default Actions-token check is **advisory**: another candidate workflow can
imitate its check name/App source. Do not configure this advisory source as an
enforced acceptance guarantee. The optional dedicated App path uses the pinned
`actions/create-github-app-token` v3.2.0 action with repository-scoped Actions,
contents and pull requests read access, and checks write access. Its
short-lived token is revoked by the action after the job.

Maintainer activation, after the disposable-PR transition proof:

1. Install a dedicated publisher App on this repository with those permissions.
   Create environment `acceptance-publisher`, restrict its deployment branches to
   `main` only, and store `ACCEPTANCE_APP_PRIVATE_KEY` there, never as a broadly
   available repository secret. Set `ACCEPTANCE_APP_CLIENT_ID` and then repository
   variable `ACCEPTANCE_APP_ENABLED=true`. The optional environment job is disabled
   until this explicit activation.
2. Require `current-candidate-acceptance` from that specific App and configure
   branch currency/conversation resolution according to maintainer policy.
   This check proves technical prerequisites, never human approval. The human
   maintainer alone marks ready, requests changes and merges. Shared-account
   credentials cannot distinguish an agent from that human; do not claim an
   account-level restriction can enforce this distinction or require impossible
   self-approval. Record actual rejected-merge tests for missing/stale technical
   evidence and a same-name untrusted check before claiming enforcement.
3. Keep the App key unavailable to candidate workflows and preserve human
   approval for future publisher/policy changes. A candidate cannot authorize
   new producer/workflow bytes merely by supplying matching hashes; bootstrap
   changes retain the explicit maintainer review path above.

GitHub rollout evidence: the maintainer activated the dedicated publisher and
source-bound required check; [the dated enforcement proof](records/2026-09-25-m0-enforcement-proof.md)
records actual cancelled/stale and wrong-source refusal, human ready transition,
merge blocking and ordinary recovery. This is evidence for that configuration,
not a waiver of activation/testing for another repository or changed policy.
No helper changes rulesets, repository permissions or environments. GitLab
verification remains independent; GitHub evidence does not establish GitLab
execution or enforcement.

## Triage a recurring baseline/environment failure once

The repeated `test_reasoning_server_flags_come_from_budget` discrepancy in reviews is a reason
to assign one focused diagnosis, not silently suppress it each time.

Record the exact command, interpreter/dependency/config identity, candidate result, and controlled
base comparison. Reuse that record only when the relevant environment and code still match; rerun
when they change. Link a bounded follow-up to the appropriate fixture/tooling/dependency owner.
Do not log credentials, private paths or corpus content.

A pre-existing failure is not automatically a new PR defect, but it is also not a passing required test.
Use the approved #411 verification policy for any equivalent evidence/exception and obtain an authorized
decision where required. No blanket skip, xfail, widened ignore, arbitrary dependency install, or
fabricated all-green assessment.

Keep a short continuation note in the PR/draft/task record: base/current SHA,
goal/invariant, decisions with links, touched boundaries, completed evidence,
current failures, unresolved questions, next safe action and owned temporary
resources. No reasoning transcript, secret/config dump or conversation copy.

When parallel work is explicitly assigned, one integration owner controls shared
interfaces/state. Delegate bounded investigations or separate worktrees; never
let independent agents mutate the same workspace or redefine one contract.
Supply each subagent its relevant task packet explicitly. No orchestration
service is required by this workflow.

