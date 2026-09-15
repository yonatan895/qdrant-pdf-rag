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
| configuration | [Configuration propagation](deploy.md#configuration-contract) | [Example](../airgap.env.example), [overrides and validation](../scripts/airgap/common.sh), [preflight](../scripts/airgap/validate.sh), [agent render](../scripts/airgap/deploy.sh), [ingest render](../scripts/airgap/ingest.sh), [runtime settings](../src/mainframe_rag/config.py), [gateway handoff](../scripts/run_local_gateway.sh) | [Air-gap tests](../tests/test_airgap_validate_sh.py), [settings tests](../tests/test_config.py) |
| deployment | [Deployment policy](deploy.md#deployment-policy) | [Install/bootstrap](install_and_ops.md), [real-corpus recovery](local-real-corpus.md), [CRC release](crc-release-verification.md), [pins](../images.txt) | [Release record](crc-release-record.md), [CI inventory](deploy.md#ci-policy) |
| verification | [Required minimums](live-stack.md#verification-minimums) | [Operating modes](live-stack.md#operating-modes), [Make targets](../Makefile), [test design](testing.md#evidence-design) | [Evidence rules](testing.md#evidence-design) |
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
## Task packet

Use the [issue form](../.github/ISSUE_TEMPLATE/agent-task.md), or these same fields
in a GitLab issue. A small fix can use short answers; `N/A` needs a reason.

| Field | What the assignee needs |
|---|---|
| Outcome and authority | User-visible goal, approved issue/ADR, acceptance owner, invariant and one forbidden outcome |
| Baseline and scope | Repository/base SHA, relevant prior comments, permitted behavior changes, non-goals, approval boundary |
| Read first / impact map | Canonical owners; producers → state → consumers, including UI and deployment where affected |
| Assumptions and counterexamples | Modes/topology, mutation ownership; missing data, interruption, retry, writer/reader overlap, cache, rollback and configuration cases; explain exclusions |
| Verification plan | Existing test homes, minimal reproducer, independent expected result distinguishing a plausible wrong implementation, required tiers/prerequisites and unavailable checks |
| Safety, migration, rollback | Protected services/data, isolation, permissions and recovery expectations |
| Completion | Observable conditions, evidence locations, remaining gaps and owners |

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
Review the actual diff against the approved acceptance contract and base SHA.
Do not assume the author summary or green tests establish the claimed guarantee.
Trace the affected unchanged callers, persisted states, launch paths, and consumers.
Choose the smallest plausible counterexample at each relevant boundary.
Check whether tests would distinguish that counterexample from the intended outcome.
Check missing/corrupt metadata, interruption, cache lifetime, active readers,
writer overlap, rollback, and configuration only where they apply.
Report concrete preconditions, location, impact, evidence confidence, and a focused
regression for defects. Separate optional improvements and validation gaps.
A finding is not mandatory: explicitly state when no blocker was found in scope.
Do not merge, change permissions, weaken a gate, or broaden product scope.
```

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
AGENTS/overrides, CLAUDE/GEMINI, Cursor or Copilot instructions were found.
The GitHub opencode workflow contains an inline review prompt and a comment-agent
entry point. Vendored skills are on-demand third-party guidance, not a replacement
workflow. The original root was 45,644 bytes; that creates a loading risk, not
proof this particular conversation was truncated.

| Agent/tool and version | Invocation / directory | Discovered instruction paths | Effective relevant byte limit | Overrides/fallbacks | Explicit follow-up reads | Evidence / unknowns |
|---|---|---|---|---|---|---|
| Current Codex hosted session | repository root | User-supplied AGENTS excerpt and skill catalog visible; automatic loader paths unverified | Unavailable in hosted diagnostics | User-supplied excerpt is not a loader log | Entire baseline AGENTS and contract owners | Session owner must verify hosted loader; no truncation claim |
| Local Codex CLI 0.154.0 | fresh `codex debug prompt-input` from root and `src/mainframe_rag` | Complete root AGENTS, including its tail, present byte-for-byte in model-visible input | Config limit unset; documented default 32 KiB; complete 7,246-byte entry observed in both diagnostics | No global AGENTS/override or project config found; fallback key unset | Fresh semantic exercises recorded in the implementation PR | Diagnostic exit 0 in both directories; redacted metadata only retained. This proves local CLI loading, not hosted or CI loading |
| GitHub opencode 1.18.25 (workflow pin) | workflow checkout | Prompt explicitly names root guide; actual loader unverified | Not exposed by workflow | Runner/user defaults unverified | Review prompt routes through this guide | CI maintainer owns fresh root/subdirectory audit; do not claim CLI audit proves this entry point |

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

`make check-context` checks deterministic structure only. The marked context-map
and instruction-chains tables above are the curated input. Each contract row
has one canonical owner link; other cells name boundary/evidence links. Supported
links are ordinary `[label]` followed by `(relative/path)`, with an optional `#explicit-id` whose
target declares `<a id="explicit-id"></a>`. Paths are relative to the containing
file, with no spaces, URLs, titles or reference-style indirection in checked
local links. Templates use the same format. External links are not crawled.
The checker does not establish semantic consistency or agent understanding.

`make agent-doctor` defaults to `unit`; `PROFILE=sim` and `PROFILE=deploy` add
CLI/pin prerequisites. It is read-only, standard-library-only and performs no
service probe by default. An explicitly requested bounded Docker probe may
report unavailable access. Results distinguish `ready`, `missing prerequisite`
and `unable to verify`; readiness is not test/application/production acceptance.
It never sources `.env`, reads secret values, installs, launches or repairs.
Use the tier's existing runbook after diagnosing prerequisites.
