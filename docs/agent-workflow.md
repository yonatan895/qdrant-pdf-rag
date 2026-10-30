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
| reader-lifetime | [Serving and cache](agent.md#serving-contract) | [Serving](../src/mainframe_rag/agent/serving.py), [HTTP routes](../src/mainframe_rag/agent/app.py), [console](../src/mainframe_rag/webui/routes.py), [ADR-0003](adr/0003-zowe-mcp-read.md), [ADR-0004](adr/0004-operator-console-htmx.md), [retrieval](../src/mainframe_rag/retrieve/query.py), [writer](../src/mainframe_rag/ingest/run_ingest.py) | [Gate tests](../tests/test_serving_gate.py), [API tests](../tests/test_agent_api.py), [POST set](../tests/test_mcp_agent.py) |
| answers | [Evidence and answer states](agent.md#answer-contract) | [Prompt and parser](../src/mainframe_rag/agent/answer.py), [shared core](../src/mainframe_rag/agent/answer_core.py), [SSE](../src/mainframe_rag/agent/sse.py), [browser](../src/mainframe_rag/webui/static/js/console.js), [Open WebUI Pipe](../integrations/openwebui/docs_pipe.py), [answer eval](../src/mainframe_rag/eval/answers.py) | [Answer tests](../tests/test_agent_api.py), [console tests](../tests/test_webui.py) |
| http-model | [Transport and lifecycle](agent.md#http-model-contract) | [Model client](../src/mainframe_rag/agent/answer.py), [tokenizer](../src/mainframe_rag/agent/tokenizer.py), [gateway probe](../scripts/probe_gateway.py), [rerank](../src/mainframe_rag/retrieve/rerank.py) | [Transport tests](../tests/test_agent_api.py), [gateway tests](../tests/test_probe_gateway.py) |
| team-tool auth | [Caller verification](team-tools.md#caller-verification) | [Library and middleware](../src/mainframe_rag/mcp/tool_auth.py), team-tool MCP servers (#1075) | [Attack tests](../tests/test_mcp_tool_auth.py) |
| retrieval | [Retrieval contracts](retrieval.md) | [Query](../src/mainframe_rag/retrieve/query.py), [screen](../src/mainframe_rag/retrieve/screen.py), [embed](../src/mainframe_rag/ingest/embed.py), [answer core](../src/mainframe_rag/agent/answer_core.py) | [Evaluation](eval.md), [query tests](../tests/test_query_filters.py) |
| configuration | [Configuration propagation](deploy.md#configuration-contract) | [Example](../airgap.env.example), [overrides and validation](../scripts/airgap/common.sh), [preflight](../scripts/airgap/validate.sh), [agent render](../scripts/airgap/deploy.sh), [ingest render](../scripts/airgap/ingest.sh), [Task wrapper](../taskfiles/airgap.yml), [CI](../.github/workflows/e2e.yml), [bundle/bootstrap](install_and_ops.md), [runtime settings](../src/mainframe_rag/config.py), [gateway handoff](../scripts/run_local_gateway.sh) | [Air-gap tests](../tests/test_airgap_validate_sh.py), [settings tests](../tests/test_config.py) |
| deployment | [Deployment policy](deploy.md#deployment-policy) | [Install/bootstrap](install_and_ops.md), [real-corpus recovery](local-real-corpus.md), [release gate](release-verification.md), [pins](../images.txt), [licensing](licensing.md) | [Release record](release-record.md), [CI inventory](deploy.md#ci-policy) |
| docs front end (Kind) | [Dev environment and acceptance lane](openwebui-dev.md#acceptance-lane) | [Open WebUI chart templates](../charts/mainframe-rag/templates/openwebui-deployment.yaml), [Docs Pipe](../integrations/openwebui/docs_pipe.py), [dev scripts](../scripts/openwebui/dev.sh), [e2e lane](../.github/workflows/e2e.yml) | [Acceptance rules](../tests/test_openwebui_acceptance.py), [Pipe tests](../tests/test_openwebui_docs_pipe.py) |
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
`<issue>-<short>` from it. Preserve another task's workspace and use an isolated
worktree: link the main checkout's `.tools`, then run `dev:setup`. In the gap branch from the GitLab fork's `main`
([ADR-0009](adr/0009-gap-fork.md)); do not require internet.
Do not reuse an already-merged branch. Do not switch a tree used by running
spawn workers. Experiments and temporary artifacts belong outside the tree.

Create every PR as a draft (`gh pr create --draft`). Only the human maintainer
`yonatan895` marks it ready, formally requests changes, or merges; agents leave
it in draft and never run `gh pr ready`, submit APPROVE/REQUEST_CHANGES or
merge, even on the maintainer's account, and passing CI authorizes none of
these. Findings are comments, not maintainer
decisions. Shared credentials make this a working agreement, not account-level
enforcement. Repository access/ruleset changes remain maintainer-owned.

Keep one bounded behavior, the smallest that delivers the outcome, and its
necessary tests/docs in one PR. No application
fixes in a docs-only PR. Rebase on the approved current main before requesting
review; no merge commits unless requested. Force-push a feature branch only
after rebase and before review comments exist; never force-push main. Commits
use imperative wording and explain non-obvious reasons.

Update the PR/MR body in the same push as the code. Include issue, outcome,
air-gap/copyright impact, and every changed default, constant, timeout, retry,
limit or chunk size. Search the diff/callers for counterexamples to broad claims.
A stale or false acceptance claim blocks readiness. Planners/reviewers work on
design and review; application/test/CI implementation needs the task's authority.
New product scope requires an issue decision, not a silent expansion.

<a id="task-packet"></a>
<a id="author-packet"></a>
## Task packet

File work with the [Task form](../.github/ISSUE_TEMPLATE/task.yml) (or its fields in
GitLab): justification, one-sentence goal, requirements with a forbidden outcome, out
of scope, completion criteria naming their evidence, and size (one PR or up to
three). Coordination uses the [Tracker form](../.github/ISSUE_TEMPLATE/tracker.yml):
child Tasks only, no goals of their own.

**Scope lock.** Goal and completion criteria are fixed once work starts. A new goal
is a new linked issue, never an edit to an open one; status comments report against
the criteria. An issue whose criteria cannot be met as written closes as superseded
and links its replacement.

Before implementation, the impact note answers (short answers for small fixes;
`N/A` needs a reason):

```text
Boundary proof: for each changed guarantee, the smallest case a plausible wrong
implementation passes locally but fails end-to-end.
Next operation: for persisted/lifecycle changes, success -> cleanup -> next
ordinary action, not only failure -> retry.
State/transport distinction: where several paths implement one rule, compare
equivalent allowed and forbidden inputs; keep deliberate policy differences explicit.
Evidence and limits: test and candidate; executed proof vs static reasoning vs
unavailable evidence; compatibility decisions recorded separately.
```

No mandatory checklist of every failure mode for a trivial change. Settle design first: take each trade-off against its owner contracts to the maintainer before code. Settled decisions link their existing owner; reopen only a specific demonstrated conflict. An agent must not independently choose deletion, retention, permission, or publication semantics while generating tests.

<a id="process-budget"></a>
## Process budget

Agents optimize what the loop measures, so the loop measures value and caps its
own overhead. The active quality tracker is
[#1118](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118) (POC hardening,
opened 2026-10-27); it owns the scoreboard and budget figures, while this section
owns the mechanics:

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

<a id="parallel-work"></a>
## Parallel work

Maintainer request (2026-10-23): parallelize as much work as possible; hand a
large task to as many agents as it needs to finish quickly.

- Split a large task into independent pieces, each with explicit file ownership
  and its own worktree or branch. Pieces that touch one file or contract stay
  with one agent.
- Run the pieces in parallel subagents; integrate and verify centrally, and
  batch the resulting PRs as merge trains.
- Shared limits still bind: host RAM and the heavy lock, the serialized test
  runner, one G4 session at a time, and the trusted/Verifier steps. Parallelism
  never bypasses them or the [branch workflow](#git-workflow) authority rules.

`scripts/parallel_guard.py` wraps a command with a host-memory floor and a
`systemd-run` memory cap; `--help` and the script docstring list its defaults
and flags.

<a id="review-handoff"></a>
## Independent review and handoff

Use the [PR form](../.github/pull_request_template.md) or its fields in GitLab:
scope/base/outcome; contract/impact (defaults, API/schema, identities, operational
requirements, rollback); evidence table (claim/counterexample, test/command,
tested SHA, result, location); limits/issue owners; self-review. Distinguish
observed results, static reasoning, proposed tests and checks not run. Group
related evidence; no artifact is required for every trivial assertion. Tests carry their
[admission](testing.md#test-admission) evidence.
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
No findings is a valid outcome. Group common-root-cause findings. A finding
blocks only when it is in scope and changes a user-visible outcome, data
integrity or a security boundary; record every other finding in the parking-lot
issue (#588). Report at most three blockers; if there seem to be more, the scope
or design needs rethinking: stop and say so. Re-review runs only after blocking
fixes, at most twice, and marks the prior blockers alone: it files no new
blocker, and a fix that regresses stays unresolved. A blocker unresolved after two re-reviews goes to
the maintainer as a decision (#785).

Maintainer rule (2026-10-23): every change gets a review through the
`repo-code-review` skill (both axes): each PR, each merge train, and each fix
pushed after review. Post it as a comment on that PR; a train PR also gets a
summary linking each member review, its fixes and re-reviews. Post issue-level
outcomes (a criterion met, a decision) on the issue. Each comment carries its
evidence: head and base SHAs, the commands run with results or exit codes,
findings in the fields above, and the verdicts. Re-reviews follow the limits above.
If time/tool limits leave material review incomplete, report incomplete rather
than approval by default.

Consume valid current-candidate CI once. Run focused experiments to answer a
specific uncertainty, not to duplicate the full suite for the appearance of
independence. Keep exploratory changes out of the candidate and its evidence.
Do not install arbitrary packages, contact private/live systems, widen
permissions, change baselines, merge, or modify the author's implementation.
Return one concise review summary with supporting detail and remaining limits.
```

GitHub-side acceptance machinery — maintainer decisions, native evidence,
verifier-update and catalogue-anchor decisions, and recurring-failure triage —
is [acceptance](acceptance.md). Read it when the task reaches those steps.

## Engineering skills

The [skill index](../.agents/skills/index.md) routes engineering, measurement and
lifecycle skills through repository-owned entry points to pinned local upstream
copies. Each provider record ([engineering](../.agents/mattpocock-skills-provenance.md),
[pstack](../.agents/pstack-skills-provenance.md)) owns its allowlist, license and
updates. Reach for the matching skill at each step it covers, unprompted; their
compatibility rules retain existing testing, coverage and review contracts. Each
entry point is registered for Claude Code by a name-and-description wrapper in
`.claude/skills` (code-review as `repo-code-review`, distinct from the built-in),
kept in sync by `qa:context` (#787). No upstream workflow bundle or automatic
instruction chain is installed.

<a id="qdrant-skills"></a>
## Vendored Qdrant skills

Qdrant directories in [.agents/skills](../.agents/skills) form a curated,
intentionally incomplete subset for server operations that are hard to reconstruct
offline. Its first-party [index](../.agents/skills/index.md) and the pin,
allowlist and notices in
[.agents/qdrant-skills-provenance.md](../.agents/qdrant-skills-provenance.md)
own the contents. Skills are invoked or read on demand, never part of the automatic
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


## Instruction maintenance

Budgets, the loader audit, fresh-context exercises and the offline check format
live in [agent maintenance](agent-maintenance.md). Run
`sh scripts/tools/run-task.sh qa:context` after any instruction or skill edit.
