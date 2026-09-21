# Codex/Astra → OpenCode Go/DeepSeek Runbook

Operational reference for automated, bounded implementation handoffs and
independent review using native subscriptions. Policy authority: issue #468.
Canonical agent rules and context routing remain in
[AGENTS.md](../AGENTS.md) and [docs/agent-workflow.md](agent-workflow.md).

<a id="overview"></a>
## Overview and operating boundaries

The development handoff couples two native coding environments without
introducing hosted orchestration frameworks, external vector stores, or runtime
dependencies into `mainframe_rag`:

- **Codex / Astra (Controller & Reviewer):** Analyzes requirements, inspects
  canonical owners, resolves architectural decisions, drafts compact task
  packets, freezes candidates, runs deterministic verification, and conducts
  independent final reviews against Schema v1 contracts.
- **OpenCode Go / DeepSeek V4.1 Flash (Implementation Worker):** Implements
  settled contracts within an isolated Git workspace using existing repository
  conventions. Shell access, network fetching, and secondary agents are denied
  by default.
- **Trusted Local Controller (`scripts/ai_worker.py`):** Standard-library
  orchestrator managing workspace isolation, process groups, event parsing,
  state transitions, candidate capture, and attributable verification evidence.

```text
Maintainer-approved outcome and constraints
                    |
                    v
Codex / Astra: inspect owners → resolve design → write task packet
                    |
                    v
Trusted local adapter: validate authority, workspace, model, budget
                    |
                    v
OpenCode Go / DeepSeek: implement one bounded concern
                    |
                    v
Freeze candidate → relevant deterministic verification
                    |
                    v
Fresh Astra review context: contract + actual diff + valid evidence
                    |
          +---------+----------+
          |                    |
  precise corrections    acceptable/current/verified
          |                    |
  same recorded worker    existing acceptance path
  session, bounded        + maintainer decision
```

<a id="roles"></a>
## Role responsibilities

| Role | Default owner | Permitted operations | Forbidden operations |
|---|---|---|---|
| Architecture & planning | Codex / Astra | Scope decomposition, contract drafting, counterexample definition | Unsettled roadmap expansion; speculative refactors |
| Implementation | OpenCode Go / DeepSeek | File edits within approved write scope; synthetic test authoring | Redesigning contracts; changing baselines or protected paths; commits; pushes |
| Verification & state | Controller (`ai_worker.py`) | Worktree setup, event logging, candidate freezing, Task test execution | Accepting unverified claims; silent route fallback |
| Independent review | Fresh Codex review | Schema v1 evaluation; dispositioning findings; plan defect detection | Self-approval by author; downgrading required evidence |

<a id="subscription-routing"></a>
## Subscription routing and client setup

Keep subscriptions within their native clients. Do not export session tokens or
cross-wire credentials.

### OpenCode Go

- **Provider model selector:** `opencode-go/deepseek-v4.1-flash`. The `opencode-go/`
  prefix is required; do not substitute a Zen or direct-provider route.
- **Console balance safeguard:** In the OpenCode console, ensure **"Use balance"**
  is **disabled**. This guarantees that exhausted Go subscription limits fail closed
  instead of silently drawing against metered Zen credits.
- **Worker agent:** Defined in `.opencode/agents/rag-implementer.md` as `mode: primary`.
  OpenCode v1.18.25+ falls back to the default agent if an agent is missing or marked
  as a subagent. The adapter validates primary mode during preflight.

### Codex

- **Authentication status:** Checked via `codex login status`. Must report
  active ChatGPT subscription authentication.
- **Execution mode:** Non-interactive reviews use `codex exec` with `--sandbox read-only`
  and `--output-last-message` to prevent conflict between `--base` and custom prompts.

<a id="run-directory"></a>
## Durable run directory structure

Run state is persisted outside tracked Git trees (default `dist/ai_worker_runs` or
via `AI_WORKER_RUN_ROOT`):

```text
$RUN_ROOT/<issue>/<run-id>/
  contract.md                 approved outcome and implementation boundaries
  state.json                  controller-owned identity, status, limits, session
  attempts/
    01/
      events.jsonl            raw incremental JSON event stream
      stderr.log              process stderr
      worker_report.md        extracted worker completion text
  evidence/                   command receipts, exit codes, and test outputs
  reviews/                    Schema v1 structured review results
```

### State transitions

State transitions are written atomically to `state.json`:

```text
planned -> running -> candidate_frozen -> verification -> review
              |                              |            |
              +------ blocked/failed --------+            |
                                                          +-> correction
                                                          +-> ready_for_maintainer
```

- `planned`: Task packet validated; workspace and base commit locked.
- `running`: Worker subprocess executing under monitored process group.
- `candidate_frozen`: Worker exited; git status inspected; candidate committed/tagged; scope validated.
- `verification`: Deterministic test suite executed; receipts saved to `evidence/`.
- `review`: Independent review context evaluating candidate and evidence.
- `correction`: Defect or test failure detected; bounded correction attempt launched.
- `ready_for_maintainer`: Verified candidate with acceptable Schema v1 review ready for review.
- `blocked` / `failed`: Preflight failure, timeout, quota exhaustion, route mismatch, or protected path violation.

<a id="cli-interface"></a>
## Task commands and CLI usage

Dispatch is integrated with the pinned Task runner (`scripts/tools/run-task.sh`):

### 1. Diagnose prerequisites (read-only)

```sh
sh scripts/tools/run-task.sh dev:ai-doctor
```

Checks:
- Python 3.14 GIL runtime.
- Git repository cleanliness and branch status.
- OpenCode version, Go model catalog (`opencode-go/deepseek-v4.1-flash`), and primary agent (`rag-implementer`).
- Codex CLI presence and ChatGPT sign-in status (`codex login status`).

### 2. Dispatch implementation worker

```sh
sh scripts/tools/run-task.sh dev:ai-run CONTRACT=path/to/contract.md [WORKTREE=path/to/worktree]
```

Or via direct script:

```sh
.venv/bin/python scripts/ai_worker.py run \
  --contract path/to/contract.md \
  --worktree dist/worktrees/task-branch \
  --issue 468
```

### 3. Resume session for correction

```sh
sh scripts/tools/run-task.sh dev:ai-resume RUN_ID=<run_id> CORRECTION=path/to/correction.md
```

Resumes the recorded OpenCode session ID (`--session <session_id>`) for bounded
repair (at most 2 correction rounds by default).

### 4. Inspect status

```sh
sh scripts/tools/run-task.sh dev:ai-status RUN_ID=<run_id>
```

Displays run progress, attempt count, candidate SHA, verification receipts, and
current state.

<a id="safety-rules"></a>
## Safety invariants and protected paths

1. **Candidate scope enforcement:** The worker may only modify files permitted by
   the contract. Changes to protected paths fail closed immediately:
   - `.opencode/**` (agent configs are executable policy)
   - `scripts/ai_worker.py` (controller self-protection)
   - `taskfiles/**` and `Taskfile.yml` (dispatch definitions)
   - `.github/workflows/**` (CI configurations)
   - `pyproject.toml`, `requirements*`, `images.txt` (dependencies and base images)
2. **Deterministic verification:** Worker claims of test success are disregarded;
   only receipts executed by the controller in `evidence/` establish verification.
3. **Process isolation:** Worker runs in a separate process group (`start_new_session=True`).
   On deadline expiration or cancellation, `SIGTERM` followed by `SIGKILL` cleans up
   all subprocesses cleanly.
4. **No silent route fallback:** Any agent fallback warning or provider mismatch aborts
   the run without accepting the candidate.
