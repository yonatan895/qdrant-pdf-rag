# Task runner (Make→Task migration, issue #402)

This file owns the command-interface contract for the migration. The single
verification policy stays in [live-stack](live-stack.md#verification-minimums);
test design stays in [testing](testing.md#evidence-design).

<a id="scope"></a>
## Scope: increments A, B1, B2, B3 and B4

`Taskfile.yml` (+ `taskfiles/quality.yml`, `taskfiles/dev.yml`,
`taskfiles/artifacts.yml`, `taskfiles/eval.yml`, `taskfiles/local.yml`,
`taskfiles/airgap.yml`) is the entry point for **discovery, doctor, context,
quality (A), artifacts (B1), evaluation (B2), local simulation (B3) and
air-gap stages (B4)**. Remaining: dev completion, the Make→Task shim,
offline Task handoff, and CI/docs cutover (B5/C/D). No product behavior
changes here.

All documented invocations assume the pinned `task` on `PATH` (session-local;
the installer prints the exact export). `task` with no task name prints the
task list and builds/installs/launches nothing.

<a id="installation"></a>
## Installation and pin

- Pin record: `scripts/tools/task-pin.txt` (version, asset, SHA-256, origin,
  license). Current: go-task **v3.53.1**, `task_linux_amd64.tar.gz`
  (`a54a408f…82fc7`), MIT. **linux-amd64 only** (repo Linux/WSL2 baseline,
  all CI assets are linux-amd64).
- Connected install (explicit opt-in only):
  `sh scripts/tools/install-task.sh [--bin-dir DIR]` (default
  `.tools/bin`). Verifies platform, SHA-256 and `task --version` before use;
  no sudo, no global `PATH`/profile mutation, no `curl | sh`, no `latest`.
- Discovery, doctor and verification **never auto-install**. A missing,
  corrupt, wrong-architecture or foreign `task` fails closed with remediation.
- Offline/air-gap delivery of the Task binary is increment C work and must
  not call the installer (no network in the gap).

<a id="inputs"></a>
## Variables, arguments, cwd and exit codes

| Input | Form | Semantics |
|---|---|---|
| `PY` | `task <t> PY=python3.14` | Interpreter for `qa:context`/`dev:*`. Default `python3`. Explicit empty is preserved and fails in the script (never silent fallback); `false`/`0`-like strings pass through. |
| `PROFILE` | `task dev:doctor PROFILE=sim` | `unit` (default) / `sim` / `deploy`, owned by `agent_doctor.py`. Same empty/false/zero rules as `PY`. |
| Focused tests | `task qa:unit -- tests/test_agent_context.py -q` | Replaces the default `pytest tests -v` argv once (shell word-splitting applies; trusted developer input only, never untrusted free text). Empty selection keeps the default. |
| `TASK_BIN` | env for `tests/test_taskfile_contracts.py` | Override for the Task binary under test; `.tools/bin/task`, then `PATH`. |
| `EMBED_MODE` | `task eval:retrieval EMBED_MODE=vllm` or `EMBED_MODE=vllm task eval:retrieval` | Eval-family default `hash`; both forms converge, CLI wins. Script-read, so bridged under its exact name per task (never global). Explicit empty is preserved (script fails); baselines still derive from the effective mode via `coalesce`. |
| `VENUE` | `task eval:retrieval VENUE=rc` | Eval-family default `dev`; same form rules as `EMBED_MODE`. `eval:holdout` forces `rc` (caller input ignored, same as Make). |
| `$(or)`-style knobs (`N`, `RESTORE`, `REPEATS`, `GOLDEN`, `OUT`, `REPORT`, `BASELINE`, `BASE`, `CURRENT`, `BENCH_REPEATS`) | `task eval:answers N=5` | Empty falls back to the Make `$(or)` default via shell `:-`; non-empty passes through exactly. |
| `AGENT_URL`, `CONCURRENCY`, `DURATION`, `REQUEST_TIMEOUT`, `HARNESS_L3_BASELINE` | `task eval:harness:l3 AGENT_URL=…` | `?=`-style with defaults (`:8080`, `8`/`30`/`30`, mode-keyed L3 baseline); explicit empty preserved. |
| `$(if)`-style optional flags (`QUERY`, `LIMIT`, …) | `task local:query QUERY="text" LIMIT=5` | Unset/empty inputs are omitted entirely; set values accumulate through positional parameters so free text arrives as single argv entries and executes nothing. (Quotes nested inside `${VAR:+...}` do not survive outer field splitting on any POSIX shell — that idiom is forbidden and pinned by test.) |
| `$(if)`-style optional environment (`CORPUS_DIR`, `UI_ENABLED`, …) | `task local:stack CORPUS_DIR=/data` | Forwarded via conditional `export` in the same command only when set and non-empty; otherwise the script default applies. |
| Secrets (`GATEWAY_MASTER_KEY`, per-leg keys) | caller environment only, e.g. `GATEWAY_MASTER_KEY=… task local:gateway:up` | Never Task vars or CLI values: Task never echoes bridged values into `--dry`/logs, and process-table entries carry no secrets. Scripts mint per-start keys when absent. |
| `BUDGET_PYTHON` | (fixed by the task) | Always `$PWD/.venv/bin/python` at the repo root for Budget resolution (same as Make `$(CURDIR)`); never exported globally, never caller-overridable. |
| Operator keys (`INTERNAL_REGISTRY`, `NAMESPACE`, … — full `OPERATOR_ENV_KEYS`) | `task airgap:deploy INTERNAL_REGISTRY=x` or `INTERNAL_REGISTRY=x task airgap:deploy` | Bridged per air-gap task with no Task-side defaults, so `common.sh` precedence holds exactly: explicit values beat the env file, empty stays unset, unset applies file then script defaults. `airgap:dryrun` instead pins stand-ins that beat everything (same as the Make recipe environment). |

- Values reach scripts through `env:` bridges and quoted `"$VAR"` expansion,
  never re-parsed template interpolation (a `$(…)` value stays a literal
  filename and fails closed instead of executing).
- No root `dotenv:`, no `airgap.env` loading, no global `EMBED_MODE`/`VENUE`
  (eval scoping stays target-scoped in later increments).
- Included tasks run at the repository root (`dir: .` on includes) from root
  or subdirectories; caller-relative paths in `CLI_ARGS` resolve from root.
- `task <t>` exits nonzero on any failure (Task-level codes); contracted
  script codes need the documented passthrough: `task --exit-code dev:doctor`
  preserves `agent_doctor.py`'s 0/1/2.
- `check` runs lint→typecheck→unit sequentially via direct task calls, never
  parallel `deps`. No `sources:`/`status:` caching on verification; only
  `dev:setup` skips when `.venv/bin/python` exists (a failed preparation
  leaves no executable, so it never caches green).

<a id="safety"></a>
## Safety boundaries

Verification diagnoses a missing `.venv` and points at `task dev:setup`; it
never creates one (deliberate change vs Make's order-only setup). `dev:setup`
is the only setup task that installs, only on explicit request, connected
host only; CI uses its prepared interpreter. No secrets in CLI/examples/logs;
`silent: true` only on discovery aliases. `clean`, services, evaluation and
air-gap behavior are unchanged (still Make-owned).

<a id="freshness"></a>
## Artifact freshness (B1)

`artifacts:wheelhouse` and `artifacts:bm25` prove freshness with a completion
stamp (`.task-complete`) recording every input that affects outputs —
lockfile/manifest/fetcher content, model selection, interpreter version,
platform — re-verified by `status:` on every run and written only after the
recipe succeeds. Consequences, all covered by runner-boundary tests:

- Changed inputs rebuild; mtime-only touches do not; a missing stamp rebuilds
  even when the output directory survives (deliberate improvement over Make's
  directory-mtime shortcut, which can skip after a failed partial build).
- No `sources:`/`method:` fingerprints anywhere: Task computes those even for
  `--list --json`, which would break side-effect-free discovery. Verification
  tasks carry no freshness state at all.

<a id="inventory"></a>
## Inventory (increment A dispositions)

`old target → new task → retained owner → prerequisites → disposition`.

| Make target | Task | Owner | Disposition |
|---|---|---|---|
| `help` (default) | `default` / `help` / `task --list [--json]`, per-task `--summary` | `Taskfile.yml` | Migrated |
| `venv` / `.venv` | `dev:setup` (+ internal presence check) | venv recipe, `requirements.lock.txt` | Migrated with recorded interface change (setup/verify split) |
| `agent-doctor` (`PROFILE`) | `dev:doctor`, root `agent-doctor` | `scripts/agent_doctor.py` (+ new Task-identity finding) | Migrated |
| `lint` / `typecheck` / `test` / `check` | `qa:lint` / `qa:typecheck` / `qa:unit` / `qa:check`, root aliases | ruff / mypy / pytest + `pyproject.toml` | Migrated |
| `check-context` | `qa:context`, root `check-context` | `scripts/check_agent_context.py` | Migrated |
| `wheelhouse`, `bm25-weights`, `chart`, `pull-chart`, `helm-template`, `helm-lint`, `build-images` | `artifacts:wheelhouse/bm25/chart-check/chart-fetch/helm-render/helm-lint/images` | pip / fetch script / helm / docker | `.venv` diagnosed (builds), chart presence verified, sequential preparation | `BUNDLE_DIR`, `BM25_MODEL`, `IMAGE_TAG`, image names | bundles output, images, chart fetch | **Migrated (B1)** with completion-stamp freshness ([#freshness](#freshness)) |
| `sim*`, `loadtest-mock`, `bench*`, `loadtest` | `qa:sim/load`, `eval:bench*` | existing suites | Deferred to B3/B2 |
| `eval*`, `gate-l1`, `harness-*`, `verify-golden`, `capture-pool`, reports | `eval:*` | existing scripts/baselines | **Migrated (B2)** with per-task mode/venue scoping ([#inputs](#inputs)) |
| `query-demo`, `ask`, `local-*`, `run-agent`, `test-vllm-e2e` | `local:*`, `qa:sim/load/vllm-e2e` | existing launchers/scripts | **Migrated (B3)**; `run_local_stack.sh` calls `scripts/sim_qdrant.sh` directly (no runner) |
| `airgap-*`, `airgap-dryrun` | `airgap:*` | `scripts/airgap/*` | **Migrated (B4)** thin wrappers; operator keys bridged per task (no defaults); `bootstrap.sh` offline wording stays for C |
| `e2e-demo-pdfs`, `clean` | `dev:demo-pdfs`, `dev:clean` | existing scripts | Deferred to B5 (retention contract) |

Executable consumer inventory (all found by searching first-party `make`
invocations): `scripts/run_local_stack.sh:157` (`make -C … sim-qdrant`);
`agent-context.yml:70`, `load.yml:65`, `e2e.yml` (wheelhouse/build-images +
13 `airgap-*` calls); `scripts/airgap/*.sh` next-step messages ending in
`make airgap-validate`; doc/runbook tables (`README.md`, `install_and_ops.md`,
`testing.md`, `live-stack.md`). `src/`, `ci.yml`, `bench.yml`,
`opencode.yml` (exec paths) and `.gitlab-ci.yml` invoke Python directly and
need no A change; `review_tooling.py` + `opencode.yml` + `testing.md`
(#412/#413) are inventoried with no behavior change, and #411's eventual
consumer is updated once in increment C (no parallel acceptance system).
`#371` owns the full Python lock; only the Task artifact identity is pinned
here. Runtime consumers migrate in B/C; historical `make` commands in dated
records stay as history.

<a id="shim-exit"></a>
## Compatibility-shim exit milestone (strict D-gate)

No shim is added in increment A (Makefile untouched, Task additive). Broad
porting (B) may add a one-way Make→Task forwarding shim for migrated targets
only. The Makefile/shim is removed **after**: every in-repository executable
consumer has moved, the signed offline bootstrap works without Make, agent
fresh-context acceptance is recorded, and required candidate checks pass.
Retired Make-specific assertions map to retained behavioral coverage in
`tests/test_taskfile_contracts.py` (see its wiring test); old signed bundles
keep their bundled interface as history.
