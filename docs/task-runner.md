# Repository Task runner

This file owns command discovery, installation, input handling and the #402
migration map. [Live-stack](live-stack.md#verification-minimums) alone owns
required verification; [testing](testing.md#evidence-design) owns test design.

<a id="scope"></a>
## Scope and entry point

`Taskfile.yml` and the local `taskfiles/` modules own all command dispatch:
quality, setup, artifacts, evaluation, local simulation and air-gap stages.
Scripts retain validation, process lifetime, rendering and deployment ordering.
Use the controlled entry from the checkout root:

```sh
sh scripts/tools/run-task.sh --list
sh scripts/tools/run-task.sh qa:unit --summary
sh scripts/tools/run-task.sh qa:unit -- tests/test_agent_context.py -q
```

The entry resolves the repository's verified `.tools/bin/task`, fixes the root
Taskfile and working directory, and disables remote Taskfile access. From a
subdirectory, invoke the entry by its relative or absolute script path; task
inputs still resolve from the repository root. Paths with spaces are supported.
No task name prints the list. Discovery and summaries install nothing, read no
private operator configuration and launch no service.

The launcher refuses inherited/local `.taskrc.yml` / `.taskrc.yaml` and XDG Task
configuration before executing Task; it does not read their contents or edit
user configuration. It clears Task runner controls/experiments that could change
dispatch. Use a host without those configuration files, or the documented owner
scripts directly when appropriate. It accepts discovery/summary/JSON/help/version,
`--exit-code`, command preview (`--dry`) and focused arguments after `--`;
path/global/remote/parallel/force runner options are unsupported and fail closed.
Raw `task` can be used for development after explicitly adding the pinned
`.tools/bin` directory to the session PATH; CI and offline runbooks use the
controlled entry. Bare `task` examples in summaries denote that pinned runner,
not an arbitrary system binary.

<a id="installation"></a>
## Connected installation and offline handoff

- `scripts/tools/task-pin.txt` owns go-task **v3.53.1**, its archive and executable
  hashes, source and MIT license. The approved artifact is
  `task_linux_amd64.tar.gz`; Linux/WSL2 **x86_64 only**. Other platforms fail
  closed until a separately reviewed pin is added.
- On a connected host, explicitly run `sh scripts/tools/install-task.sh`.
  The default destination is `.tools/bin`; `--bin-dir DIR` supports an explicit
  alternate destination. The controlled entry always uses the workspace copy.
  Installation verifies archive/executable identity and version. No sudo,
  global PATH/profile edits, `latest`, or `curl | sh`.
- New signed bundles carry `task_linux_amd64.tar.gz`, `task-pin.txt` and
  `task-LICENSE` under the existing member checksum/signature contract. The
  binary is a host operator tool, never a product-image dependency.
- In the air gap, run the transferred `sh bootstrap.sh` as documented in
  [installation](install_and_ops.md). Bootstrap requires Linux x86_64,
  POSIX shell/core utilities, git, tar/gzip and OpenSSL. It needs no Make,
  preinstalled Task, Go, application Python packages, curl or network.
  It verifies signatures/member hashes, the full manifest/bundle/workspace SHA,
  pin agreement and tracked installer integrity before installing Task from
  the verified archive into `<workspace>/.tools/bin/task`.
- The installer's explicit offline form is
  `sh scripts/tools/install-task.sh --archive /absolute/path/task_linux_amd64.tar.gz`.
  Archive/hash/platform failures stop before executing the candidate. Bootstrap
  supplies this path only after bundle verification; never download in the gap.
- Existing workspaces at a different SHA are refused. Select a fresh
  `AIRGAP_WORKSPACE`, or deliberately check out the approved bundle SHA before
  rerunning bootstrap. Reruns preserve operator `airgap.env` and retained
  artifacts. Old approved bundles use their own bundled bootstrap and Make
  contract in a separate workspace; do not mix old assets with a new checkout.

Deployment render tests require the existing Helm **4.3.0 linux-amd64** bytes
recorded in [the Helm pin](../scripts/tools/helm-pin.txt). Prepare them explicitly:

```sh
# Connected preparation; installs only under this checkout's .tools/bin.
sh scripts/tools/install-helm.sh
# Disconnected preparation; fails on missing/corrupt archive, never downloads.
sh scripts/tools/install-helm.sh --archive /approved/tools/helm-v4.3.0-linux-amd64.tar.gz
```

The installer verifies archive and binary hashes before execution and atomically
replaces the destination only after verification. The controlled Task entry
selects `.tools/bin` on PATH. For direct commands select that same directory on
PATH explicitly; the doctor hashes the actual PATH-selected Helm without running
it. Rendering uses the local chart and needs no cluster or chart repository.

`qa:prerequisites` runs the read-only doctor before `qa:unit` and before any
`qa:check` tools. Missing/foreign Helm fails before pytest collection. Python
must be **CPython 3.14 GIL with experimental JIT disabled**; a later minor is not
implicitly qualified. Prepared CI interpreters use
`python scripts/agent_doctor.py --python "$(command -v python)"` without creating
a local venv. Symlinks preserve virtualenv identity. GitHub unit shards run this
gate after explicit preparation; GitLab transfers both `CI_TASK_ARCHIVE` and
`CI_HELM_ARCHIVE` and installs them offline before the same gate.

The complete runtime/dev/build wheel profiles, offline preparation, actual
installed inventory checks and release SBOM reconciliation are owned by
[the dependency contract](dependencies.md). The unit doctor verifies the full
dev profile and editable source tree, plus Task/Helm bytes and the cached Task
archive used by artifact tests. Cached BM25 content and daemon/image availability
remain separate simulation prerequisites; a unit success does not attest them
or establish application acceptance.

Discovery, doctor and verification never provision tools. Missing or foreign
workspace binaries fail with remediation; installation is an explicit action.

<a id="inputs"></a>
## Variables, arguments, cwd and exit codes

`local:llm`, `local:embed` and `local:rerank` forward `SERVED_NAME` to
the model launcher. Set it explicitly when `MODEL` is an immutable local
directory: the served HTTP model ID must match the gateway configuration,
not the directory basename. Nonempty CLI values override the ambient environment;
without a CLI value the existing environment and launcher default apply.


In this table, invoke each task with `sh scripts/tools/run-task.sh <task>`.
CLI `NAME=value` wins over the caller environment. Per-task environment bridges
preserve script ownership; there is no root `dotenv:` or global mode/venue.

| Input | Task/example | Semantics |
|---|---|---|
| `PY` | `qa:context PY=python3.14`, `dev:*` | Default `python3`. Empty is preserved and fails; false/zero-like strings are not replaced. Explicit interpreter selection keeps preinstalled offline CI environments offline. |
| `PROFILE` | `dev:doctor PROFILE=sim` | `unit` default, `sim` or `deploy`; `agent_doctor.py` owns diagnosis. Same empty/false/zero rule as `PY`. |
| Focused argv | `qa:unit -- tests/test_agent_context.py -q` | Replaces default `pytest tests -v` once. Empty selection retains default integration exclusion. Shell word splitting applies to this trusted developer argument tail; never use it for untrusted text. |
| `TASK_BIN` | runner tests' environment | Selects the actual pinned runner under test; default workspace binary then PATH. This test input does not override the controlled production entry. |
| `EMBED_MODE` | `eval:retrieval EMBED_MODE=vllm` | Eval-family default `hash`; script-read and scoped to the selected task. Explicit empty is preserved and fails validation. Baselines derive from effective mode; mismatch/skipped gates are not passes. |
| `VENUE` | `eval:retrieval VENUE=rc` | Eval-family default `dev`; `eval:holdout` forces `rc`. Hash/dev settings never become air-gap defaults. |
| `N`, `RESTORE`, `REPEATS`, `GOLDEN`, `OUT`, `REPORT`, `BASELINE`, `BASE`, `CURRENT`, `BENCH_REPEATS` | `eval:answers N=5` | Empty uses the documented task fallback; nonempty values pass through exactly. This preserves the former Make `$(or)` behavior. |
| `AGENT_URL`, `CONCURRENCY`, `DURATION`, `REQUEST_TIMEOUT`, `HARNESS_L3_BASELINE` | `eval:harness:l3 AGENT_URL=…` | Declared defaults (including `:8080`, `8`/`30`/`30` and mode-keyed baseline); explicit empty is preserved. |
| Optional flags (`QUERY`, `LIMIT`, …) | `local:query QUERY="text" LIMIT=5` | Unset/empty flags are omitted. Quoted positional arguments preserve spaces, Unicode and shell metacharacters as literal data. |
| Optional environment (`CORPUS_DIR`, `UI_ENABLED`, …) | `local:stack CORPUS_DIR=/data` | Only set, nonempty values are exported for that operation; otherwise the script default applies. |
| Secrets (`GATEWAY_MASTER_KEY`, per-leg keys) | caller environment only | Never CLI assignments or Task variables. Script-owned private handoff paths and per-start key handling are unchanged. |
| `BUDGET_PYTHON` | fixed by local launch tasks | Always the repository's `.venv/bin/python`; not caller-overridable or globally exported. |
| Operator keys (`INTERNAL_REGISTRY`, `NAMESPACE`, …) | `airgap:deploy INTERNAL_REGISTRY=x` | No Task-side defaults. `common.sh` owns the full `OPERATOR_ENV_KEYS` list and env/file/default precedence: explicit nonempty values win, explicit empty stays unset. See [configuration](deploy.md#configuration-contract). |

Values use environment bridges and quoted expansion, never template text
reparsed as shell commands. The working directory is the repository root for
all included modules; caller-relative focused test paths resolve there too.
Use `sh scripts/tools/run-task.sh --exit-code dev:doctor` when the script's
exact 0/1/2 result is required; ordinary Task failures are nonzero Task codes.
Cancellation remains a failure; existing foreground owners perform cleanup.

`qa:check` runs lint → typecheck → unit sequentially, stopping at the first
failure. Required verification has no result cache. `dev:setup` alone skips a
completed installation only when `.venv/bin/python`, `.venv/.setup-complete`
and a fresh full inventory/source check agree; failed preparation cannot create
the completion marker. Separate
concurrent setup/build processes must not share one environment/output directory.

<a id="safety"></a>
## Safety boundaries

Verification diagnoses missing dependencies; it never creates `.venv` or
installs packages. `dev:setup` is explicit connected-host preparation, while
CI selects its prepared interpreter. Services, model calls, baseline recording,
cleanup and deployment remain explicit commands with their existing permissions.
`dev:clean` retains `*.tar*`, `.tools/` and external paths.

`airgap:dryrun` executes the existing pipeline's validation/rendering path with
fixed stand-ins for its declared inputs. It is an executed verification;
Task `--dry` merely previews commands and cannot establish a passing gate.
Other operator inputs remain owned by `common.sh`, `deploy.sh` and `ingest.sh`.

<a id="freshness"></a>
## Artifact freshness

`artifacts:wheelhouse` and `artifacts:bm25` check a `.task-complete` record of
content inputs, model selection, interpreter and platform on every run, and
write it only after success. Changed content or a missing stamp rebuilds;
mtime-only changes do not. A partial directory never counts as completed.
Verification never uses these stamps. No `sources:`/`method:` fingerprints
run during discovery, and there are no remote or optional includes.

<a id="inventory"></a>
## Older command reference

This map translates older run records; it does not rewrite what those records
executed. Prefix current tasks with `sh scripts/tools/run-task.sh`. Task
`desc`/`summary` remains the current input/default catalog.

| Former Make target(s) | Current Task command(s), in matching order | Retained owner |
|---|---|---|
| `help` (default) | `--list`, `<task> --summary` | root `Taskfile.yml` |
| `venv` / `.venv` | `dev:setup` | Explicit owned venv and complete dev/build/runtime hash locks |
| `agent-doctor`, `check-context` | `dev:doctor`, `qa:context` | `agent_doctor.py`, `check_agent_context.py` |
| `lint`, `typecheck`, `test`, `check` | `qa:lint`, `qa:typecheck`, `qa:unit`, `qa:check` | Ruff, mypy, pytest; root aliases retained |
| `wheelhouse`, `bm25-weights` | `artifacts:wheelhouse`, `artifacts:bm25` | artifact preparation and BM25 fetcher |
| `chart`, `pull-chart`, `helm-template`, `helm-lint`, `build-images` | `artifacts:chart-check`, `artifacts:chart-fetch`, `artifacts:helm-render`, `artifacts:helm-lint`, `artifacts:images` | Helm, Docker and artifact owners |
| `sim`, `loadtest-mock`, `test-vllm-e2e` | `qa:sim`, `qa:load`, `qa:vllm-e2e`, `qa:ha` (new multi-peer lane) | existing suites/scripts; `qa:ha` is the issue #360 three-peer fixture |
| `sim-qdrant`, `sim-clean` | `local:qdrant:up`, `local:qdrant:down` | `sim_qdrant.sh`, shared Qdrant helpers |
| `eval`, `eval-baseline`, `eval-draft`, `eval-holdout`, `eval-paraphrase` | `eval:retrieval`, `eval:baseline`, `eval:draft`, `eval:holdout`, `eval:paraphrase` | retrieval scripts, mode-keyed baselines |
| `verify-golden`, `gate-l1`, `capture-pool`, `eval-answers`, `eval-chat` | `eval:verify-golden`, `eval:gate-l1`, `eval:capture-pool`, `eval:answers`, `eval:chat` | existing evaluation scripts |
| `harness-gate`, `harness-baseline`, `harness-l2`, `harness-l3`, `harness-l3-baseline`, `harness-l4`, `harness-l4-record` | `eval:harness:gate`, `eval:harness:baseline`, `eval:harness:l2`, `eval:harness:l3`, `eval:harness:l3-baseline`, `eval:harness:l4`, `eval:harness:l4-record` | harness scripts and separate tier baselines |
| `eval-report`, `eval-html`, `eval-compare` | `eval:report`, `eval:html`, `eval:compare` | `render_report.py` |
| `bench`, `bench-baseline`, `bench-report`, `bench-html`, `bench-compare`, `loadtest` | `eval:bench`, `eval:bench-baseline`, `eval:bench-report`, `eval:bench-html`, `eval:bench-compare`, `eval:load` | benchmark/load/report scripts |
| `query-demo`, `ask`, `run-agent` | `local:query`, `local:ask`, `local:agent` | query script and agent |
| `local-vllm`, `local-vllm-embed`, `local-vllm-rerank`, `local-stack` | `local:llm`, `local:embed`, `local:rerank`, `local:stack` | existing launchers/supervisor |
| `local-gateway`, `local-gateway-stop`, `local-jaeger`, `local-jaeger-stop` | `local:gateway:up`, `local:gateway:down`, `local:jaeger:up`, `local:jaeger:down` | existing component owners |
| `airgap-pack`, `airgap-validate`, `airgap-load`, `airgap-deploy`, `airgap-ingest`, `airgap-smoke`, `airgap-pipeline`, `airgap-dryrun` | `airgap:pack`, `airgap:validate`, `airgap:load`, `airgap:deploy`, `airgap:ingest`, `airgap:smoke`, `airgap:pipeline`, `airgap:dryrun` | `scripts/airgap/` stages/pipeline |
| `e2e-demo-pdfs`, `clean` | `dev:demo-pdfs`, `dev:clean` | existing synthetic-document and cleanup owners |

The local-stack supervisor invokes `sim_qdrant.sh` directly. CI, operator
messages, active runbooks and context owners select Task or their existing
Python/script owner; none requires Task → Make delegation. Dated evidence and
old signed bundles retain their original command spelling. #371 continues to
own the full Python lock; #376 owns broader distribution inventory.

<a id="shim"></a>
<a id="shim-exit"></a>
## Compatibility window and removal milestone

#402 increment C made Task the canonical documented interface and supplied the
offline handoff. Increment D (this change) removes the Make adapter: the root
`Makefile` shim is deleted and no runtime Make dependency or Task → Make
delegation remains in supported workflows. The inventory table above stays as
the concise old → new command reference for readers of older records; dated
evidence and old signed bundles retain their original command spelling and
their own bundled interface, independent of new releases.

D-gate record: every in-repository executable consumer had moved to
`sh scripts/tools/run-task.sh` or its documented direct script owner, the
signed offline bootstrap works without Make, and the e2e airgap-acceptance and
kind-live lanes assert the Task bundle members explicitly. Required
published-artifact/topology evidence is recorded per release, not certified by
documentation alone.

Approved interface differences from old recipes: explicit setup separate from
verification; content-aware completion stamps; literal free-text forwarding;
explicit-empty simulation container/port fail closed; optional empty environment
inputs retain script defaults; stdlib Qdrant pin lookup uses `python3`; discovery
is a task list with per-command summaries. No model/baseline/schema/default or
operational permission change is authorized by the runner migration.


### Retained local Helm acceptance and maintenance

`local:check -- --agent URL --jaeger URL --query '...' --followup '...'
--report /private/new-report.json` checks existing listeners with real model
requests and fails on non-grounded answers or incomplete streams. It neither
starts services nor proves an exact release identity. `local:repair-staging --
plan|apply ...` dispatches the narrow backed-up legacy-residue repair module;
it requires the actual shared progress mount and writer credentials. These
commands accept trusted operator shell arguments, like `qa:unit`; never feed
untrusted text into Task CLI_ARGS. See [the owner runbook](local-real-corpus.md)
for prerequisites, approved plan handling, durable evidence and the required
next ordinary ingest. They are never implicit startup/cleanup operations.

Local model tasks (`local:llm`, `local:embed`, `local:rerank`) accept optional
`CONTAINER_NAME` as a Task argument or environment variable. The launcher passes
it as one runtime `--name` argument; a name collision fails instead of replacing
a container. Omitting it retains the existing anonymous container behavior.
