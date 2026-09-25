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
| metadata | [Representation states](ingest.md#metadata-contract) | [Representation](../src/mainframe_rag/ingest/representation.py), [inventory](../src/mainframe_rag/ingest/inventory.py), [serving gate](../src/mainframe_rag/agent/serving.py) | [Representation tests](../tests/test_representation_gate.py) |
| reader-lifetime | [Serving and cache](agent.md#serving-contract) | [Serving](../src/mainframe_rag/agent/serving.py), [HTTP routes](../src/mainframe_rag/agent/app.py), [console](../src/mainframe_rag/webui/routes.py), [retrieval](../src/mainframe_rag/retrieve/query.py), [writer](../src/mainframe_rag/ingest/run_ingest.py) | [Gate tests](../tests/test_serving_gate.py), [API tests](../tests/test_agent_api.py) |
| answers | [Evidence and answer states](agent.md#answer-contract) | [Prompt and parser](../src/mainframe_rag/agent/answer.py), [shared core](../src/mainframe_rag/agent/answer_core.py), [SSE](../src/mainframe_rag/agent/sse.py), [browser](../src/mainframe_rag/webui/static/js/console.js), [answer eval](../scripts/eval_answers.py) | [Answer tests](../tests/test_agent_api.py), [console tests](../tests/test_webui.py) |
| http-model | [Transport and lifecycle](agent.md#http-model-contract) | [Model client](../src/mainframe_rag/agent/answer.py), [tokenizer](../src/mainframe_rag/agent/tokenizer.py), [gateway probe](../scripts/probe_gateway.py), [rerank](../src/mainframe_rag/retrieve/rerank.py) | [Transport tests](../tests/test_agent_api.py), [gateway tests](../tests/test_probe_gateway.py) |
| retrieval | [Retrieval contracts](retrieval.md) | [Query](../src/mainframe_rag/retrieve/query.py), [screen](../src/mainframe_rag/retrieve/screen.py), [embed](../src/mainframe_rag/ingest/embed.py), [answer core](../src/mainframe_rag/agent/answer_core.py) | [Evaluation](eval.md), [query tests](../tests/test_query_filters.py) |
| configuration | [Configuration propagation](deploy.md#configuration-contract) | [Example](../airgap.env.example), [overrides and validation](../scripts/airgap/common.sh), [preflight](../scripts/airgap/validate.sh), [agent render](../scripts/airgap/deploy.sh), [ingest render](../scripts/airgap/ingest.sh), [Task wrapper](../taskfiles/airgap.yml), [CI](../.github/workflows/e2e.yml), [bundle/bootstrap](install_and_ops.md), [runtime settings](../src/mainframe_rag/config.py), [gateway handoff](../scripts/run_local_gateway.sh) | [Air-gap tests](../tests/test_airgap_validate_sh.py), [settings tests](../tests/test_config.py) |
| deployment | [Deployment policy](deploy.md#deployment-policy) | [Install/bootstrap](install_and_ops.md), [real-corpus recovery](local-real-corpus.md), [CRC release](crc-release-verification.md), [pins](../images.txt) | [Release record](crc-release-record.md), [CI inventory](deploy.md#ci-policy) |
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
in a GitLab issue. Before implementation, authors record the compact 5-question
author input packet. For a small fix, short answers and evidence links suffice (`N/A` needs a one-sentence reason):

```text
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

### Worked planning example: representation/publication (#391)

**Planning/review only; synthetic inputs; no implementation prescribed.**
At the task's recorded base, suppose physical `synthetic-A` contains a marked
current document and an unmarked old point. A publisher walks only the current
document. Required: every searchable point belongs to verified generation
coverage; forbidden: calling the whole target current because no stale marker
was found. Owner: [publication](ingest.md#publication-contract), with
[metadata](ingest.md#metadata-contract) and [reader lifetime](agent.md#serving-contract).

Trace `run_ingest` → completion/representation stores → final publication check
→ alias → serving cache → query → answer and console. Inspect the unchanged
consumers too. Five minimal counterexamples belong in the task packet:

1. Unmarked old searchable point survives a walk that verifies every marked doc.
2. Final manifest is missing/unreadable while per-document records look complete.
3. An active reader queries during forced same-representation delete/upsert repair.
4. A warm validation cache outlives a mutation to its physical target/metadata.
5. Two publishers resolve the same alias, prepare, then verify/swap out of order,
   including different progress paths and hosts.

Proposed evidence: retain independent expected point membership; exercise existing
completion/publication/gate suites with faithful projection/upsert/alias fakes;
add a disposable server check where real client behavior matters. Select the
union of affected minimums from [live-stack](live-stack.md#verification-minimums)
and interaction checks. Inspect current #391 comments before deciding a design.
No private corpus/live endpoint or automatic cleanup is part of this exercise.
Counts, names, marker absence and a progress lock each prove only their own
property. Required draining/immutability/serialization remain acceptance questions
until the runtime owner supplies implementation and evidence.

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
rollback, scope/authorization, and configuration only where applicable.
Prefer the public operation or real boundary that can expose the failure;
helper tests and generic health checks establish narrower facts.
Distinguish executed results, static reasoning, and checks not run.

For each material finding, record:
ID | original counterexample | expected boundary result | exact evidence
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
head, base and execution identity. Test lanes retain counts from actual test
records, reject zero tests and skips, and refuse pre-existing reports. Receipts
use attempt-specific artifacts. Packaging receipts identify the checkout only;
the native packaging job must finish successfully before they can contribute
acceptance. A receipt is never a code review or permission to merge.

`scripts/acceptance_evidence.py` owns native job/artifact mappings and bounded,
data-only receipt validation. The consumer must fetch current native run, job,
attempt, artifact and commit records, paginate collections, and compare the
artifact digest and actual raw test records. The execution must be GitHub's
current PR test merge with the exact current base/head parents. Matching parent
names alone cannot authorize a different merge tree. ZIP members are read in
memory; they are never extracted, imported, or executed.

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
rechecked before publication. Selection-policy and hazard-catalogue changes are
excluded; incompatible evidence schemas still fail. Forks cannot use this path.
Normal PRs with unchanged verifier inputs require no manual decision.

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
`eval_retrieval` remains missing until an approved runner, model endpoint and
evaluation corpus are supplied; mock probes cannot satisfy it. A review comment
never supplies execution evidence or waives a lane.

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

The complete snapshot is [.agents/skills](../.agents/skills), pinned by
[vendor/qdrant-skills.sha](../vendor/qdrant-skills.sha). Do not fetch
`skills.qdrant.tech`, `/llms.txt`, snippet APIs, Cloud console or `qcloud-cli`.
A missing required skill needs an owner decision. Frontmatter grants no additional
permissions. Dedicated pin bumps replace the snapshot at a pinned SHA without
local vendor edits or developer-machine-only installs.

| Change | Read before changing |
|---|---|
| Collections, named vectors, model | `qdrant-model-migration`, `qdrant-search-quality` |
| Hybrid, quantization, HNSW | `qdrant-search-quality`, `qdrant-performance-optimization` |
| Helm, PVC, replicas, storage | `qdrant-sizing`, `qdrant-scaling`, `qdrant-deployment-options` (self-hosted only; no Docker/Cloud defaults) |
| Client SDK | `qdrant-clients-sdk` (REST, no Cloud inference or product `qdrant-client[fastembed]`) |

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

### OpenCode documentation and pinned-source audit

The [OpenCode rules guide](https://opencode.ai/docs/rules/) describes project
`AGENTS.md`, global `~/.config/opencode/AGENTS.md`, Claude fallbacks and additional
`instructions` paths/globs/URLs. Ordinary Markdown links do not automatically
load their targets. Keep this repository's technical docs on demand.

For the workflow's **1.18.25** pin, [instruction.ts](https://github.com/anomalyco/opencode/blob/v1.18.25/packages/opencode/src/session/instruction.ts)
tries project names `AGENTS.md`, `CLAUDE.md` (unless disabled), then deprecated
`CONTEXT.md`; it takes upward matches for the first matching name. Its
[findUp helper](https://github.com/anomalyco/opencode/blob/v1.18.25/packages/core/src/fs-util.ts)
collects matches from the invocation directory through the worktree root.
Global rules prefer the OpenCode config directory over the Claude fallback.
Additional configured instructions are combined; local read failures become
empty content. The read/assembly code contains no instruction-byte truncation.
This is not a claim of unlimited model input or proof that a runner loaded a file.
`AGENTS.override.md` is a Codex convention, not an OpenCode override in this pin.
The offline checker inventories the union of known first-party names for audit;
it does not emulate each client's precedence or load global configuration.

[OpenCode's GitHub integration](https://opencode.ai/docs/github/) runs in Actions.
[GitHub documents fresh hosted runners](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners);
our [workflow](../.github/workflows/opencode.yml) uses `ubuntu-latest`, restores
only the pinned binary directory and writes no instruction configuration.
Together with the current file inventory, this supports the **inference** that
root AGENTS is the project instruction from both audited directories.
[OpenCode configuration](https://opencode.ai/docs/config/) can also merge remote,
global and environment inputs; workflow text alone does not observe those inputs
or the final model request. Keep the fresh runner audit open. Public documentation
also cannot reveal this hosted Codex session's effective loader configuration;
the verified local CLI evidence and documented Codex default remain separate.

For each used entry point, record fresh root and representative-subdirectory
invocations, tool version, exact tested SHA, loader diagnostics (or the specific
missing capability), byte limit and discovered paths. Record redacted metadata
only; never dump a client config, tokens, transcript or private paths. Do not
change global config, `CODEX_HOME`, sandbox or approvals for an audit. Existing
owner overrides remain in place. Unknown loading acceptance stays open under
#397 while independent repository work proceeds.

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
