# Agent workflow and context routing

Policy authority: [issue #397](https://github.com/yonatan895/qdrant-pdf-rag/issues/397)
(15 September 2026) replaces the old exclusive-rung, single-module,
stop-all-on-red and unconditional-file-precedence instructions.
[AGENTS.md](../AGENTS.md) is the entry point. This guide coordinates work;
technical contracts and operating recipes stay with their owners below.

<a id="context-map"></a>
## Context map

Read the applicable owner section, then trace the named boundaries, including
unchanged consumers. At each boundary ask: **What false implementation could
satisfy our current local assertion?** The map is a starting point, not a file
allowlist. Read relevant [roadmap decisions](../ROADMAP.md) and
[ADRs](adr/0001-baseline-decisions.md); do not load every historical entry.

<!-- context-map:start -->
| Contract | Canonical owner | Boundaries to inspect | Evidence home |
|---|---|---|---|
| architecture | [Module and state map](architecture.md#boundary-map) | [Protocols](../src/mainframe_rag/ports.py), [settings](../src/mainframe_rag/config.py) | [Test design](testing.md#evidence-design) |
| identity | [Identity contract](ingest.md#identity-contract) | [Identity](../src/mainframe_rag/ingest/identity.py), [chunk keys](../src/mainframe_rag/ingest/chunk.py), [completion](../src/mainframe_rag/ingest/completion.py), [filters](../src/mainframe_rag/retrieve/filters.py), [citations](../src/mainframe_rag/agent/cites.py) | [Revision tests](../tests/test_ingest_revisions.py), [identity tests](../tests/test_ingest_identity.py) |
| publication | [Coverage and publication](ingest.md#publication-contract) | [Orchestrator](../src/mainframe_rag/ingest/run_ingest.py), [publish](../src/mainframe_rag/ingest/publish.py), [completion](../src/mainframe_rag/ingest/completion.py), [Qdrant IO](../src/mainframe_rag/ingest/qdrant_io.py), [serving](../src/mainframe_rag/agent/serving.py) | [Publication tests](../tests/test_ingest_publish.py), [completion tests](../tests/test_ingest_completion.py) |
| exact-evidence design | [Identity, access and retirement](evidence-contract.md#evidence-contract) | [Publication](../src/mainframe_rag/ingest/publish.py), [serving](../src/mainframe_rag/agent/serving.py), [evidence service](../src/mainframe_rag/agent/evidence.py), [MCP adapter](../src/mainframe_rag/mcp/knowledge.py), [core](../src/mainframe_rag/agent/answer_core.py), [ports](../src/mainframe_rag/ports.py) | [Stored-payload profile](evidence-contract.md#stored-payload-profile), [evidence tests](../tests/test_evidence_service.py), [MCP tests](../tests/test_mcp_knowledge.py), [four design walks](evidence-contract.md#four-design-walks-and-implementation-witnesses); remaining proof stays with #405/#391/#373/#360 |
| metadata | [Representation states](ingest.md#metadata-contract) | [Representation](../src/mainframe_rag/ingest/representation.py), [inventory](../src/mainframe_rag/ingest/inventory.py), [serving gate](../src/mainframe_rag/agent/serving.py) | [Representation tests](../tests/test_representation_gate.py) |
| reader-lifetime | [Serving and cache](agent.md#serving-contract) | [Serving](../src/mainframe_rag/agent/serving.py), [HTTP routes](../src/mainframe_rag/agent/app.py), [console](../src/mainframe_rag/webui/routes.py), [retrieval](../src/mainframe_rag/retrieve/query.py), [writer](../src/mainframe_rag/ingest/run_ingest.py) | [Gate tests](../tests/test_serving_gate.py), [API tests](../tests/test_agent_api.py) |
| answers | [Evidence and answer states](agent.md#answer-contract) | [Prompt and parser](../src/mainframe_rag/agent/answer.py), [shared core](../src/mainframe_rag/agent/answer_core.py), [SSE](../src/mainframe_rag/agent/sse.py), [browser](../src/mainframe_rag/webui/static/js/console.js), [answer eval](../src/mainframe_rag/eval/answers.py) | [Answer tests](../tests/test_agent_api.py), [console tests](../tests/test_webui.py) |
| http-model | [Transport and lifecycle](agent.md#http-model-contract) | [Model client](../src/mainframe_rag/agent/answer.py), [tokenizer](../src/mainframe_rag/agent/tokenizer.py), [gateway probe](../scripts/probe_gateway.py), [rerank](../src/mainframe_rag/retrieve/rerank.py) | [Transport tests](../tests/test_agent_api.py), [gateway tests](../tests/test_probe_gateway.py) |
| retrieval | [Retrieval contracts](retrieval.md) | [Query](../src/mainframe_rag/retrieve/query.py), [screen](../src/mainframe_rag/retrieve/screen.py), [embed](../src/mainframe_rag/ingest/embed.py), [answer core](../src/mainframe_rag/agent/answer_core.py) | [Evaluation](eval.md), [query tests](../tests/test_query_filters.py) |
| configuration | [Configuration propagation](deploy.md#configuration-contract) | [Example](../airgap.env.example), [overrides and validation](../scripts/airgap/common.sh), [preflight](../scripts/airgap/validate.sh), [agent render](../scripts/airgap/deploy.sh), [ingest render](../scripts/airgap/ingest.sh), [Task wrapper](../taskfiles/airgap.yml), [CI](../.github/workflows/e2e.yml), [bundle/bootstrap](install_and_ops.md), [runtime settings](../src/mainframe_rag/config.py), [gateway handoff](../scripts/run_local_gateway.sh) | [Air-gap tests](../tests/test_airgap_validate_sh.py), [settings tests](../tests/test_config.py) |
| deployment | [Deployment policy](deploy.md#deployment-policy) | [Install/bootstrap](install_and_ops.md), [real-corpus recovery](local-real-corpus.md), [CRC release](crc-release-verification.md), [pins](../images.txt), [licensing](licensing.md) | [Release record](crc-release-record.md), [CI inventory](deploy.md#ci-policy) |
| verification | [Required minimums](live-stack.md#verification-minimums) | [Operating modes](live-stack.md#operating-modes), [Task entry](../Taskfile.yml), [quality tasks](../taskfiles/quality.yml), [task policy](task-runner.md#scope), [test design](testing.md#evidence-design) | [Evidence rules](testing.md#evidence-design) |
<!-- context-map:end -->

<a id="conflicts"></a>
## Conflicts and contract status

Identify the kind of statement before following it:

- **Policy:** a hard boundary or approved decision; implementation can violate it.
- **Current verified behavior:** inspected code/test or a dated run, with its limits.
- **Required acceptance:** intended invariant, possibly still unimplemented.
- **Historical observation:** a result at a particular SHA/environment, never current
  release acceptance by itself.

For a material conflict, record both sources, the affected step, the risk, and
who must decide. Continue independent safe work. An approved issue may replace
implementation/workflow text; it cannot silently weaken security policy. Resolve
shared concepts at one helper/owner. Sweep sibling call sites after a fix. Sharing
clients, pools or mutable state is a runtime change even in a refactor.

Contract owners use this compact structure when an invariant changes:

```text
Contract / scope:
Status: implemented with evidence | partially implemented | required, not yet implemented
Source of authority: approved issue/ADR and decision date, where applicable
Decision owner: module/symbol and document section
Inputs and identities:
Producers -> persisted state -> consumers:
Allowed states and transitions:
Preconditions / permitted failures / forbidden outcomes:
Concurrency, mutation, caching, and lifetime assumptions:
Existing evidence: exact test or run; what it does and does not prove
Known gaps and issue owners:
```

Do not copy a hash-field list out of its code owner or keep full PR run logs in
contract docs. Link stable sections/tests and dated records. A documentation
change cannot close runtime acceptance under #391, #361, #365 or other owners.

<a id="git-workflow"></a>
## Branch and review workflow

Inspect `git status`, current branch and base SHA before editing. On a clean
connected checkout, fetch `origin/main` and create `feat/`, `fix/` or `docs/`
`<issue>-<short>` from it. If the workspace belongs to another task, preserve it
and use an isolated worktree. In the gap use the approved transferred history
and baseline from [bootstrap](install_and_ops.md); do not require internet.
Do not reuse an already-merged branch. Do not switch a tree used by running
spawn workers. Experiments and temporary artifacts belong outside the tree.

Create every PR as a draft (`gh pr create --draft`). Only the human maintainer
`yonatan895` may mark it ready for review, submit a formal request for changes,
or merge it. Agents leave PRs in draft after verification and report evidence
and findings; passing CI never authorizes a lifecycle transition. Agents must
not use `gh pr ready`, submit APPROVE/REQUEST_CHANGES reviews, or merge, even
when using the maintainer's GitHub account. Findings and suggested corrections
are allowed as comments; they are not a maintainer decision. Shared credentials
cannot distinguish a human action from an agent action at the GitHub API, so
this working agreement is not represented as an account-level enforcement
mechanism. Repository access/ruleset changes remain maintainer-owned.

Keep a bounded behavior and its necessary tests/docs in one PR. No application
fixes in a docs-only PR. Rebase on the approved current main before requesting
review; no merge commits unless requested. Force-push a feature branch only
after rebase and before review comments exist; never force-push main. Commits
use imperative wording and explain why when it is not obvious.

Update the PR/MR body in the same push as the code. Include issue, outcome,
air-gap/copyright impact, and every changed default, constant, timeout, retry,
limit or chunk size. Search the diff/callers for counterexamples to broad claims.
A stale or false acceptance claim blocks readiness. Planners/reviewers work on
design and review; application/test/CI implementation needs the task's authority.
New product scope requires an issue decision, not a silent expansion.

<a id="task-packet"></a>
<a id="author-packet"></a>
## Task packet

Use the [issue form](../.github/ISSUE_TEMPLATE/agent-task.md), or these same fields
in a GitLab issue. Before implementation, authors record the compact 6-question
author input packet. Value comes first: correctness rigor on the wrong change is
still waste. For a small fix, short answers and evidence links suffice (`N/A` needs a one-sentence reason):

```text
Outcome metric:
The quality-tracker scoreboard number or robustness item this moves, its
baseline and expected direction; otherwise the escaped defect or maintainer
request that justifies the work.

Outcome and supported domain:
One observable change; identify the actual producer/contract of its inputs.

Boundary proof:
For each material changed guarantee, select the smallest case that a plausible
wrong implementation could pass locally but fail end-to-end. Reuse existing tests.

Next operation:
For persisted/lifecycle changes, show success -> cleanup -> next ordinary action,
not only failure -> retry. Include only relevant reset/restart/rollback interactions.

State/transport distinction:
Where multiple paths implement the same rule, compare equivalent allowed and
forbidden inputs. Keep deliberately different retry/emission policies explicit.

Evidence and limits:
Name the test and exact candidate; distinguish executed proof, static reasoning,
and required evidence not available. Record any compatibility decision separately.
```

No mandatory checklist of every failure mode for a trivial change. One end-to-end invariant may need several files; a PR is not too broad merely because it updates the necessary consumer and documentation. Settled decisions link their existing owner; reopen only a specific demonstrated conflict. An agent must not independently choose deletion, retention, permission, or publication semantics while generating tests.

<a id="process-budget"></a>
## Process budget

Agents optimize what the loop measures, so the loop measures value and caps its
own overhead. The active quality tracker (#582 completed 2026-10-01; name the
successor when one is designated) owns the scoreboard and
budget figures; this section owns the mechanics:

- Process work (agent instructions, CI/verifier/acceptance layers, workflow docs)
  links an escaped defect, a measured cost or a maintainer request. Target: at
  most 20% of merged PRs per two-week window are process-only; the window's
  share is posted on the tracker.
- Instruction changes are batched (at most weekly) and keep loop-critical wording
  flat unless an incident requires more. Rules that no review or incident invoked
  for 30 days are pruning candidates.
- Extraction/ranking changes include the stored-content census required by
  [verification minimums](live-stack.md#verification-minimums).
- Lane results, counts and SHAs come from the acceptance summary and CI receipts;
  PR prose states the claim, the counterexample considered and the limits.

<a id="review-handoff"></a>
## Independent review and handoff

Use the [PR form](../.github/pull_request_template.md) or its fields in GitLab:
scope/base/outcome; contract/impact (defaults, API/schema, identities, operational
requirements, rollback); evidence table (claim/counterexample, test/command,
tested SHA, result, location); limits/issue owners; self-review. Distinguish
observed results, static reasoning, proposed tests and checks not run. Group
related evidence; no artifact is required for every trivial assertion. Map old
test cases to retained behavior or a justified implementation-pin retirement.
Do not write blanket `Fixes`/`Closes` for a partly addressed parent.

Independent reviewer prompt:

```text
Review the actual candidate against the approved outcome and acceptance policy.
Record head SHA, base SHA, and execution SHA; distinguish head from test-merge code.
Read the relevant owner contract and prior findings, then trace the affected
unchanged producers, persisted state, callers, launch paths, and consumers.
Do not treat the PR summary, new comments, or passing tests as the authority
for a changed invariant.

Before adopting the author's helper names or test cases, derive the expected
outcome and supported input domain from the approved contract and actual producer.
For each material changed boundary, inspect one discriminating case the submitted
examples might miss. Check a real round-trip, the next operation after success,
or equivalent transport/state paths when those distinctions apply.
Exercise missing/corrupt data, interruption, retry, reader/writer overlap,
rollback, cached validation, scope/authorization, and configuration only where
applicable. Do not invent findings or certify untested production behavior.
Prefer the public operation or real boundary that can expose the failure;
helper tests and generic health checks establish narrower facts.
Distinguish executed results, static reasoning, and checks not run.

For each material finding, record:
ID | location | preconditions and original counterexample | impact
   | expected boundary result | exact evidence and minimal test
   | disposition | authorized decision link if scope/behavior is accepted instead.

On re-review, preserve the original input/preconditions and expected outcome.
A narrower passing case does not close the original finding. Neither a comment
nor a test that expects the forbidden behavior proves the invariant.
Mark each prior material finding:
- fixed-and-verified: only when the original counterexample is prevented and the
  relevant proof ran.
- disproven-with-evidence: when the counterexample genuinely cannot occur under
  the supported contract (requires contract/producer evidence, not a claim that
  the current examples do not contain it).
- accepted-by-authorized-owner: only for an actual scoped decision by that owner;
  retain the limitation and do not call the code fixed.
- unresolved: otherwise ('explicitly declined' by author is not acceptance).

Distinguish a new regression, an incomplete prior fix, a pre-existing limitation,
and unavailable verification. Do not inflate defect counts or severity by mixing
them. Likewise, safe refusal can still violate a promised usable input/recovery path.

Report separately:
- Code assessment: acceptable / changes_required / incomplete.
- Required verification: complete / incomplete / failed.
- Candidate currentness: current / stale / unverified.
- Merge readiness: ready_for_maintainer / not_ready.

Ready requires acceptable code, complete required verification, current candidate
attribution, and no unresolved material contract conflict. Missing required
checks are not successes; report them without inventing a product defect.
No findings is a valid outcome. Summarize high-impact findings first, but never
hide a verified blocker to meet a quota. Group common-root-cause findings.
Style preferences and unrelated improvements do not block a useful change.
A finding blocks only when it is in scope and changes a user-visible outcome,
data integrity or a security boundary. Record other findings in the parking-lot
issue (#588); after two review rounds, remaining non-blocking items move there
rather than holding the PR.
If time/tool limits leave material review incomplete, report incomplete rather
than approval by default.

Consume valid current-candidate CI once. Run focused experiments to answer a
specific uncertainty, not to duplicate the full suite for the appearance of
independence. Keep exploratory changes out of the candidate and its evidence.
Do not install arbitrary packages, contact private/live systems, widen
permissions, change baselines, merge, or modify the author's implementation.
Return one concise review summary with supporting detail and remaining limits.
```

### Maintainer decisions and technical evidence

Use GitHub's draft/ready, review and merge controls for the PR lifecycle. The
human maintainer `yonatan895` owns those decisions; no JSON review comment,
manual commit-ID transcription, or second approval table is required. A ready
PR means the maintainer requested review, not that CI has approved the code.
Agents may provide findings and evidence as comments but never submit formal
APPROVE/REQUEST_CHANGES reviews, mark ready, or merge on the maintainer's behalf.
Optional structured model-review artifacts are diagnostic only and cannot grant
or revoke technical verification or a human decision. Historical findings remain
visible for the maintainer to assess; CI does not parse comment history as votes.

### Native evidence and consumer rollout (#411)

`scripts/ci_evidence.py` records the actual native job invocation and its PR
head, base and execution identity. Test lanes count actual test
records, reject zero tests, skips and xfails, and refuse pre-existing reports. Receipts
use attempt-specific artifacts. Packaging receipts identify the checkout only;
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
and hazard results must bind that tested execution. The current candidate and
maintainer verifier decision still bind the exact current test-merge SHA.
ZIP members are read in memory; they are never extracted, imported, or executed.

The approved base supplies policy, producer and workflow bytes, Task dispatch
(the root Taskfile, included modules, wrapper and binary pin), and the critical
hazard runner/catalogue. Unit coverage additionally binds the pytest selector,
root configuration/conftest and locked preparation inputs. Each shard retains an
independent full collection and actual executed node IDs; the consumer compares
raw JUnit identities and proves the two shards form a disjoint complete union.
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
### Approving verifier implementation updates

A PR that changes a protected verifier or native producer workflow needs a
separate trust decision. Marking it ready does not grant that trust. After
reviewing the actual changes, **only the human maintainer yonatan895** runs
Actions → **Verifier update decision** → Run workflow, selects **main**, enters
the PR number, and selects **approve**. No SHA or JSON is entered manually.
Agents must never dispatch or rerun this workflow on the maintainer's behalf.
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

Any base, head, test-merge or verifier-byte change invalidates approval. Rerun the
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
main. Hazard-catalogue changes remain excluded; incompatible evidence schemas
still fail. Forks cannot use this path.
Normal PRs with unchanged verifier inputs require no manual decision.

For unit-layout changes, merge approved-main compatibility support before
activating a different producer matrix. The consumer supports only the complete
two- and four-job layouts read from independently approved workflow bytes, and
checks every shard's exact execution plus the complete disjoint union. An exact
verifier decision authorizes producer bytes, not a smaller receipt-declared
coverage obligation. Refresh activation PRs after the compatibility merge and
obtain a new exact-candidate decision; old approvals do not transfer.

For the selector-approval bootstrap, the consumer and trust validator change
without changing the selector, catalogue, native producer or receipt format.
The current consumer can verify that bootstrap under its existing policy. After
the maintainer merges it, refresh dependent selector PRs against the new base
and record a new exact-candidate decision. An approval issued against the old
base/head/test-merge cannot transfer to the refreshed candidate.

Bootstrap: this implementation changes the consumer and adds its decision
workflow, without changing any existing protected producer inputs. It can pass
the current consumer before deployment. The maintainer reviews and merges this
PR first. Only then does the new workflow become available on main. Afterwards,
refresh prerequisite PRs against that approved base and use the decision workflow
for each exact verifier candidate. No ruleset bypass or temporary disabling of
required checks is part of this procedure.

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
base must agree. No new SHAs are substituted into old evidence. `--all-open`
paginates candidates; file pagination must match the changed-file count and
retain both rename names verbatim.

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
Qdrant/Jaeger and a deterministic model stand-in. Its receipt must contain each
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
Publication is serialized. API snapshots and events are not an atomic merge
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

### Triage a recurring baseline/environment failure once

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

<a id="qdrant-skills"></a>
## Vendored Qdrant skills

[.agents/skills](../.agents/skills) is a curated, intentionally incomplete subset
of upstream Qdrant skills for server operations that are hard to reconstruct
offline. Its first-party [index](../.agents/skills/index.md) and the pin,
allowlist and notices in
[.agents/qdrant-skills-provenance.md](../.agents/qdrant-skills-provenance.md)
own the contents. Skills are on-demand reads, never part of the automatic
instruction chain, and never override repository contracts, approved issues,
pinned runtime behavior, security policy or executed evidence. Do not fetch
`skills.qdrant.tech`, `/llms.txt`, snippet APIs, Cloud console or `qcloud-cli`.
A missing skill needs an owner decision; adding or removing a category needs an
approved scope decision. Frontmatter grants no additional permissions.

| Qdrant server operation | Read |
|---|---|
| Version upgrade | [qdrant-version-upgrade](../.agents/skills/qdrant-version-upgrade/SKILL.md) |
| RAM/disk/node sizing | [qdrant-sizing](../.agents/skills/qdrant-sizing/SKILL.md) |
| Shards, nodes, QPS, latency | [qdrant-scaling](../.agents/skills/qdrant-scaling/SKILL.md) |
| Metrics, health, production debugging | [qdrant-monitoring](../.agents/skills/qdrant-monitoring/SKILL.md) |
| Indexing, HNSW, memory, search-speed tuning | [qdrant-performance-optimization](../.agents/skills/qdrant-performance-optimization/SKILL.md) |

Application-level changes need no skill read; use the project owner and the
pinned client/server behavior:

| Change | Owner |
|---|---|
| Collections, named vectors, embedding model, publication | [collection policy](deploy.md#collection-policy), [publication contract](ingest.md#publication-contract), [representation states](ingest.md#metadata-contract) |
| Hybrid, quantization, ranking | [retrieval contracts](retrieval.md) |
| Helm, PVC, replicas, storage | [deployment policy](deploy.md#deployment-policy) (self-hosted only; no Docker/Cloud defaults) |
| Client API | pinned `qdrant-client` source/types and behavior tests (no Cloud inference) |

<a id="instruction-loading"></a>
## Instruction loading audit

Repository maintenance budgets: root **8,192 UTF-8 bytes**; each supported
first-party automatic directory chain **24,576 bytes including two newline
separator bytes between files**, and below any smaller effective client limit.
These are project budgets, not universal model limits. Keep ordinary docs on
demand. A new nested instruction file needs a directory-specific reason and a
fresh loader audit; arbitrary siblings are not automatically loaded.

The [official Codex guide](https://developers.openai.com/codex/guides/agents-md/)
describes a default 32 KiB project limit, root-to-working-directory discovery,
`AGENTS.override.md` precedence and configurable fallback names. These are
Codex-specific defaults; verify the actual version/configuration. Files read
later through tools are not proof of automatic discovery. Raising a personal
limit is a temporary diagnostic option, not this repository's solution.

<!-- instruction-chains:start -->
| Invocation directory | First-party automatic chain |
|---|---|
| `.` | [root](../AGENTS.md) |
| `src/mainframe_rag` | [root](../AGENTS.md) |
| `tests` | [root](../AGENTS.md) |
<!-- instruction-chains:end -->

Audit inventory on 15 September 2026, baseline `9fece72df92ca5da414bc8f5b05cb2f89fcd18c8`:
only root `AGENTS.md` is present as a first-party instruction file; no nested
AGENTS/overrides, CLAUDE/GEMINI/CONTEXT, Cursor or Copilot instructions were found.
The GitHub opencode workflow contains an inline review prompt and a comment-agent
entry point. Vendored skills are on-demand third-party guidance, not a replacement
workflow. The original root was 45,644 bytes; that creates a loading risk, not
proof this particular conversation was truncated.

| Agent/tool and version | Invocation / directory | Discovered instruction paths | Effective relevant byte limit | Overrides/fallbacks | Explicit follow-up reads | Evidence / unknowns |
|---|---|---|---|---|---|---|
| Current Codex hosted session | repository root | User-supplied AGENTS excerpt and skill catalog visible; automatic loader paths unverified | Unavailable in hosted diagnostics | User-supplied excerpt is not a loader log | Entire baseline AGENTS and contract owners | Session owner must verify hosted loader; no truncation claim |
| Local Codex CLI 0.154.0 | fresh `codex debug prompt-input` from root and `src/mainframe_rag` | Complete root AGENTS, including its tail, present byte-for-byte in model-visible input | Config limit unset; documented default 32 KiB; complete 7,246-byte entry observed in both diagnostics | No global AGENTS/override or project config found; fallback key unset | Fresh semantic exercises recorded in the implementation PR | Diagnostic exit 0 in both directories; redacted metadata only retained. This proves local CLI loading, not hosted or CI loading |
| GitHub OpenCode 1.18.25 (workflow pin) | `github run` in workflow checkout; no working-directory override | Root AGENTS expected from root/subdirectory under the pinned discovery code; actual runner input not captured | No byte cap in pinned instruction read/assembly; effective model/request limit unverified | No tracked OpenCode config; workflow sets no instruction override and caches only the binary; runtime global/remote inputs unverified | Review prompt names this guide; ordinary Markdown links are not automatically followed | Official docs and pinned source reviewed below; CI maintainer owns remaining fresh root/subdirectory runtime audit |

The OpenCode pinned-source loading audit is a dated record:
[OpenCode instruction-loading audit](records/2026-10-01-opencode-instruction-loading-audit.md).

### Fresh-context exercises

Run bounded read-only planning/review tasks through the actual setup, supplying
the task packet and using the context map. Record sources read, omissions and
semantic decisions, not exact wording:

- Representation/publication: find completion, publish, cache and configuration;
  identify unmarked-data and active-reader cases; names/markers are not proof.
- Required configuration: trace example, override, preflight, both renders and
  runtime; use gateway handoff; no invented production value/attestation bypass.
- Test consolidation: preserve independent outcomes/faults and fake
  projection/capability semantics; no universal fake or weakened product tests.

Keep the dated evidence in the implementation PR. These exercises cannot prove
all future tasks succeed. For a small later sample, use existing PR records to
note review iterations, setup/context failures and escaped-defect categories.
No fixed ten-PR study or invented percentage improvement blocks this issue.

<a id="context-check-format"></a>
## Offline check format and prerequisite diagnosis

`sh scripts/tools/run-task.sh qa:context` checks deterministic structure only. The marked context-map
and instruction-chains tables above are the curated input. Each contract row
has one canonical owner link; other cells name boundary/evidence links. Supported
links are ordinary `[label]` followed by `(relative/path)`, with an optional `#explicit-id` whose
target declares `<a id="explicit-id"></a>`. Paths are relative to the containing
file, with no spaces, URLs, titles or reference-style indirection in checked
local links. Documentation-to-documentation links stay relative. GitHub-facing
templates under `.github/` are copied into issue/PR bodies, so they must use
explicit canonical repository-file URLs
(`https://github.com/yonatan895/qdrant-pdf-rag/blob/main/...` with an explicit
`#anchor`); the checker maps that prefix offline to the current checkout and
validates scheme, host, repository and ref, then applies the shared containment
and explicit-anchor checks, including decoded paths and symlinks, without network
access. Relative local links inside `.github/` templates are rejected.
Unrelated external links are not crawled. Inspect a rendered body preview when
changing template links; before merge, use a head-pinned equivalent for
click-through, since a new file/anchor may not exist on `main` yet.
The checker does not establish semantic consistency or agent understanding.

`sh scripts/tools/run-task.sh dev:doctor` defaults to `unit`; `PROFILE=sim` and `PROFILE=deploy` add
CLI/pin prerequisites. Run `python3 scripts/agent_doctor.py --profile sim
--probe-docker` for the optional local-socket probe (one command). Exit 0 means
verified prerequisites, 2 means missing or unable-to-verify prerequisites with
distinct labels, and 1 means internal checker failure. It is read-only,
standard-library-only and performs no
service probe by default. An explicitly requested bounded Docker probe may
report unavailable access. Results distinguish `ready`, `missing prerequisite`
and `unable to verify`; readiness is not test/application/production acceptance.
It never sources `.env`, reads secret values, installs, launches or repairs.
Use the tier's existing runbook after diagnosing prerequisites.
