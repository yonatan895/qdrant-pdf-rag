# Bounded Colab playground (#706)

This is connected development tooling. The application, persistent Qdrant,
ingestion, real pinned LiteLLM gateway, gateway Postgres and Jaeger stay local;
one explicitly owned Colab session supplies model HTTP endpoints. Production
serving remains platform-owned. CPU/fake lifecycle evidence does not establish
GPU fit, full-corpus quality, deployment acceptance or production parity.

## Reference profiles and prerequisites

| Profile | Assignment | Purpose |
| --- | --- | --- |
| `COLAB_CPU_TRANSPORT` | CPU | Tiny health servers; no inference install or model download |
| `COLAB_G4_BF16` | RTX PRO 6000 Blackwell, 96 GB | Primary two-model reference, measured commissioning required |
| `COLAB_A100_80GB_BF16` | A100, 80 GB | Separate reduced-concurrency commissioning; never an automatic fallback |

The BF16 reference pins `Qwen/Qwen3.8-27B` at
`1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` and
`Qwen/Qwen3-Embedding-8B` at
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`, with native 4096-dimensional
embeddings. Reference inference uses Python 3.12, vLLM 0.28.0, Transformers
5.15.0 and CUDA 13.0. The application keeps its existing Python 3.14 environment.
The profile owns model argv; do not add guessed vLLM flags. A100 40 GB is
ineligible for this pair. FP8 requires its own approved identity and evidence.
These envelopes are estimates until actual co-residency is measured.

Prepare the repository's pinned Task runner and development environment using
[the existing setup](task-runner.md). Provision the official
`google-colab-cli` 0.7.4 and required SSH/Docker/kubectl tools explicitly.
The maintainer completes OAuth in their own terminal before a live run; the
controller uses explicit `--auth oauth2` and does not log in automatically.
Inspect the intended account and its available CU balance privately. Do not
place credentials in Task variables, command previews or evidence for Git.

GPU runs additionally require a supplied immutable inference requirements lock
outside Git. Every dependency must have an exact version and SHA256 wheel hash;
include the complete compatible transitive set and the reference versions above.
The remote Python 3.12 interpreter must already exist. Installation uses only
binary wheels with required hashes, no dependency resolution fallback or source
build. The prepared profile records the lock digest. No usable approved lock
means no GPU commissioning. Observed GPU identity, VRAM, available RAM, disk and
driver 580 or newer must pass admission before downloading either pinned model.
The existing preflight records OS/freedesktop metadata, architecture, glibc and
kernel Python separately from an isolated, three-second probe of preinstalled
`python3.12`. GPU preflight refuses missing, failed or incompatible CPython 3.12
evidence before inference setup and retains the measured facts in private
`preflight.json`. CPU transport records interpreter availability without requiring
it. These facts do not establish wheel/import/CUDA compatibility; the inference
stage still verifies the installed environment. Collect metadata in this native
preflight/runtime path: Colab can reject a second active SSH connection.

## Keep the existing host resource owner

Use the already deployed `guarded.sh` through an absolute `LOCAL_RESOURCE_GUARD`
path. Reuse the shared host serialization lock at
`$HOME/.cache/agent-tmp/mainframe-rag-heavy.lock`; do not create another general
resource runner. The caller holds this lock around the entire heavy controller
workload. The controller's Colab ownership lock has a different purpose and must
not reacquire the host lock. A busy host lock is a refusal, not permission to
run another lane. Keep unrelated heavy work stopped.

The following CPU example uses the existing 2048 MiB guard envelope, a private
TMPDIR, and a finite outer timeout with cleanup allowance. A full local stack
needs a separately admitted host memory/disk envelope; this CPU envelope is not
a claim that the stack fits. Preserve the existing guard's cgroup and admission
checks. No command below installs or replaces it.

## CPU transport before paid GPU

WP5 must be merged before the approved live CPU rehearsal. Choose a fresh private
run directory outside every checkout, and the gateway owner's existing digest
pin. Preparation writes local private files only; it does not allocate a runtime.

```sh
export COLAB_STATE="$HOME/.cache/mainframe-rag/colab/cpu-run-unique"
export COLAB_GATEWAY_IMAGE='registry.example/gateway@sha256:REPLACE_WITH_EXISTING_DIGEST'
sh scripts/tools/run-task.sh local:colab:prepare \
  COLAB_PROFILE=COLAB_CPU_TRANSPORT COLAB_DATASET=synthetic \
  COLAB_DEADLINE_SECONDS=180 COLAB_CU_CAP=1 COLAB_CU_FLOOR=10
```

Set `LOCAL_RESOURCE_GUARD` to the existing guard, create a private verification
TMPDIR outside Git, then approve only this run's prepared cap. Do not persistently
export `COLAB_APPROVE_CU` in a shell profile or automation.

```sh
flock -n -E 75 "$HOME/.cache/agent-tmp/mainframe-rag-heavy.lock" \
  sh "$LOCAL_RESOURCE_GUARD" 2048 -- \
  timeout --signal=TERM --kill-after=5 240 \
  env TMPDIR="$HOME/.cache/agent-tmp/issue706-verification/colab-cpu" \
  sh scripts/tools/run-task.sh local:colab:run COLAB_APPROVE_CU=1
```

The controller first verifies an exact owned session endpoint, then one SSH
connection with four model-port forwards: host loopback and the selected local
Docker-to-host address. Native Docker uses its bridge gateway by default.
Docker Desktop's bridge can belong to a separate VM and be unavailable to the
WSL host. In that case, explicitly pass `COLAB_DOCKER_GATEWAY` during preparation
with a numeric RFC1918 IPv4 address on the controller host, after proving that
the pinned gateway container can reach that specific listener. The address is
frozen in the run configuration; later environment changes cannot redirect it.
There is no automatic discovery or fallback. Wildcard, loopback, public,
link-local addresses and hostnames are refused as explicit overrides. Existing
preallocation bind checks and actual gateway-network probes remain required;
the successful CPU receipt binds this effective address for GPU admission.
Fresh known-hosts state and a private SSH key belong to this run.
Every CLI operation uses the run's private `colab-sessions.json` store, including
runtime execution, SSH and the printed recovery command. SSH uses unnamed proxy
mode: CLI 0.7.4 resolves the sole live record in that isolated store and refuses
an empty, pruned or ambiguous store. Its named proxy mode can create a missing
session and is therefore unsuitable here. A fresh run refuses a pre-existing
private store; global CLI sessions are neither imported nor adopted.
Do not use an unreviewed public tunnel or call an SSH operation that could
silently allocate a replacement session. Both host probes and probes from the
actual digest-pinned gateway container network path must pass. The remote runtime
waits for explicit `/content/colab-transport-ok` before inference installation or
model downloads. A CPU run never enters those GPU steps.

Retain the successful private transport receipt and exact cleanup evidence.
GPU preparation's `COLAB_TRANSPORT_RECEIPT` must identify that successful CPU
rehearsal for the same controller/transport identity, rather than an arbitrary
existing JSON file. A fake receipt or an unverified session release is not this
gate. Receipt production/validation and its identity contract must be verified
before declaring Slice A complete.

Preflight records preinstalled kernel pip; GPU setup requires its `--python`
support (22.3 or newer). Setup creates a fresh environment using preinstalled
Python 3.12 and `--without-pip`, proves its interpreter/prefix and tests kernel pip against
that target before downloads. The same target owns hash-locked binary installation
and dependency checking. Inherited pip recursion/destination/configuration
overrides are removed. No apt, get-pip, installer upgrade or Python download runs.
After installation, dependency and software checks pass, inference children put
that verified environment's `bin` first on `PATH`, retaining the provider's
system-tool path (or `os.defpath` when absent/empty). Nested tools such as
FlashInfer's pinned `ninja` then resolve from the qualified environment without
changing the host or CPU transport environment.

GPU startup measures offline pooling/tokenizer behavior after the qualified
install and pinned snapshots, before either server starts. It uses one existing
supervised child, bounded by120seconds and the remaining runtime deadline, and
adds no connection. The frozen candidate SHA comes from the host manifest; the
producer digest hashes the exact executed UTF-8 `-c` program, including its
leading/trailing newlines. Native ModelConfig, embedding-request tokenization and
fast-tokenizer probes supply the facts; tensors and manual text are not read.
The existing private diagnostics/archive retain `pooling_observation` through
later failures, labelled `trust: unreviewed` and `acceptance_certified: false`.
This generates evidence for separate producer review/adoption; it cannot approve
itself or replace the production admission manifest. First commissioning can use
the explicitly selected synthetic lane while production prerequisites remain
unmet. CPU skips this stage. Actual target-library execution and GPU fit still
require the separately approved live run; unit packets prove integration only.

## Prepare the full-corpus GPU run

Use originals already present on the host. Qdrant snapshots alone do not provide
the original PDFs or an independently grounded evaluation set. Keep all manuals,
questions, answers, vectors, snapshots, private paths and raw logs outside Git.
The retained data directory is stable across runs; the controller state directory
is new for each run.

Authenticated runs inherit the local stack's environment-only role inputs:
`QDRANT_API_KEY` is the read-only key, and `QDRANT_INGEST_API_KEY` is the distinct
writer used only by ingest. Supply them privately in the caller environment,
never as Task variables or in the controller/gateway handoff files. The native
supervisor owns case-insensitive alias cleanup and the ingest-only writer
handoff; see [the local credential contract](live-stack.md#full-local-simulation).

| Task input | Controller input / meaning |
| --- | --- |
| `COLAB_STATE`, `COLAB_GATEWAY_IMAGE` | Required fresh private state and existing gateway digest |
| `COLAB_DOCKER_GATEWAY` | Optional `--docker-gateway`; frozen numeric RFC1918 host listener instead of native bridge discovery |
| `PRODUCTION_CORPUS_DIR`, `PRODUCTION_GOLDEN_PATH` | `--corpus`, `--golden`; originals and grounded JSONL |
| `COLAB_INVENTORY`, `COLAB_DATA_STATE` | `--inventory`, `--data-state`; census and retained ingest progress |
| `COLAB_SOURCE_INVENTORY` | `--source-inventory`; independently discovered complete source membership and hashes |
| `COLAB_EMBEDDING_EVIDENCE` | `--embedding-evidence`; complete pinned tokenizer/model metadata and approved pooling observations |
| `COLAB_PRODUCTION_APPROVAL` | `--production-approval`; independently reviewed census/pooling producer identities and explicit scan ceilings |
| `COLAB_KUBECONFIG`, `COLAB_KUBE_CONTEXT` | Explicit existing cluster identity |
| `COLAB_NAMESPACE`, `COLAB_QDRANT_SERVICE` | Retained PVC-backed Qdrant service, not an ephemeral container |
| `COLAB_INFERENCE_LOCK`, `COLAB_TRANSPORT_RECEIPT` | Approved wheel lock and prior CPU transport proof |
| `COLAB_PROFILE`, `COLAB_DATASET` | Named profile and explicit `production`/`synthetic` selection |
| `COLAB_EMBEDDING_MAX_LEN` | Measured supported input window; default 4096 is commissioning only |
| `COLAB_DEADLINE_SECONDS`, `COLAB_CU_CAP`, `COLAB_CU_FLOOR` | Defaults 7200 seconds, 20 CU, 10 CU retained floor |

Unset/empty optional inputs preserve controller defaults. Optional ports are
`COLAB_REASONING_PORT`, `COLAB_EMBEDDING_PORT`, `COLAB_QDRANT_PORT`; polling uses
`COLAB_POLL_SECONDS`. Explicit executable overrides are `COLAB_BIN`,
`COLAB_SSH_BIN`, `COLAB_SSH_KEYGEN_BIN`, `COLAB_DOCKER_BIN`, `COLAB_KUBECTL_BIN`.
Task bridges values as individual quoted argv entries, including whitespace and
delimiters. Trailing shell arguments are rejected. `COLAB_WORKLOAD` on synthetic
runs may name one private executable wrapper; its arguments belong inside that
wrapper. Production requires the native `local:stack` invocation, with its
prepared interpreter, source root and frozen environment.

The corpus inventory must bind every source hash, document/page/chunk count,
failure and exclusion; the golden file identity; exact 8B tokenizer revision
and artifact hashes; extraction/document recipe and query prefix; and measured
maximum document/query token lengths. Count actual recipe outputs with the
pinned tokenizer. Do not truncate, drop difficult inputs, change recipes or use
a convenient subset to make a 4096-token window pass. Historical corpus counts
describe their own backup and are not an independent acceptance oracle.

Production preparation reconciles that complete census against independently
discovered source membership, streamed original bytes, native document/query
recipes and approved pooling observations. Source discovery and streaming have
explicit entry, byte and time ceilings supplied in the private approval file.
No parsing, tokenizer/model loading or corpus copy happens during preparation.
The selected profile supplies the exact embedding model, revision and dimension;
conflicting explicit model environment values are refused. Other resolved native
settings are frozen for the child, including unset optional values. Private
`.env` dependencies are unsupported; supply settings and credentials through the
caller environment. Hashes establish identity, while separate review establishes
which evidence producers are trusted.

The pinned embedding snapshot publishes no `sentence_*_config.json` encoder
file. vLLM 0.28 preserves `encoder_config: null` in this case: the independently
observed plain-string request must retain special tokens, disable lowercasing
and truncation, and use the explicit model window without an encoder length
override. Admission checks the model's position bound. Do not fabricate the
missing file or derive encoder settings/query prefixes from
`config_sentence_transformers.json`; its prompt metadata is separate. A present
supported `sentence_bert_config.json` retains strict length/lowercasing checks;
other encoder filenames without it require explicit support. Actual runtime
observation and separate producer approval remain required. See the pinned
[vLLM encoder lookup](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/transformers_utils/config.py#L843).

The private approval file has exact schema version 1 and fields
`pooling_producer`, `census_producer`, and `source_scan`. Each producer identity
has `development_sha` (40 hexadecimal characters) and `source_sha256` (64).
`source_scan` supplies positive integer `max_entries`, `max_bytes`, and
`deadline_seconds`; the last cannot exceed the selected run deadline. These are
independently reviewed identities and explicit operator ceilings, never values
copied from the census to approve itself. The complete hash-locked inference
requirements supply runtime pins, including `tokenizers`; missing pins refuse
preparation. The approval file contains no credentials.

The stable private data directory must already contain the approved
`retained-index-binding.json` ledger. It binds compatible document/index identity
to the actual namespace, PVC UID, PV UID, storage path and logical collection.
An empty directory cannot establish retained-vector compatibility. Missing or
incompatible bindings require an explicit fresh-index or rebuild decision;
preparation never creates a compatibility assertion from unknown stored data.
Changes confined to golden queries or controller code do not by themselves
invalidate document compatibility. Changed document recipes, corpus roots,
sources, tokenizer semantics or embedding geometry do.

For a first build, explicitly select a new logical collection with both native
`QDRANT_COLLECTION` and `COLAB_INITIALIZE_COLLECTION`. Create an empty private
`COLAB_DATA_STATE` directory outside Git, supply the complete production inputs
above, and use a fresh `COLAB_STATE` for this local operation:

```sh
sh scripts/tools/run-task.sh local:colab:initialize-fresh \
  COLAB_INITIALIZE_COLLECTION="$QDRANT_COLLECTION" COLAB_INITIALIZE_SECONDS=180
```

Run inside the existing host guard and shared lock. This command uses only the
selected kubectl and read-only Qdrant requests through its owned loopback forward;
it never allocates Colab or installs/downloads models. It proves the responding
pod/container/PVC/PV identity, admits the complete census, and refuses any existing
logical target, dangling alias, staging/build/completion state or retained local
bookkeeping. Complete inventories have 4096-entry, 64-KiB response and 4-MiB total
read ceilings; exceeding them refuses initialization. The private ledger is
installed atomically without replacement. It records intended build identity,
not ingestion completion or serving acceptance. Ordinary preparation still
requires its own fresh state, CPU receipt and all GPU prerequisites.

Serialize every target writer/admin operation from initialization through the
first native publication. The native target lock is held during initialization;
after release, it provides no continuing remote reservation. Existing multi-host
concurrent publication remains unsupported. A failed operation releases its
owned forward and can be retried with fresh operation state after inspecting the
data directory. A committed ledger proceeds through ordinary preparation; it is
never overwritten by initialization. After a hard process/host kill, inspect any
occupied local port or `.retained-index-binding-*.tmp` residue explicitly; no
unidentified process or residue is adopted or deleted automatically. Preserve
legacy collections and snapshots; rebuild/adoption remains a separate decision.

```sh
sh scripts/tools/run-task.sh local:colab:prepare \
  COLAB_PROFILE=COLAB_G4_BF16 COLAB_DATASET=production \
  COLAB_DEADLINE_SECONDS=7200 COLAB_CU_CAP=20 COLAB_CU_FLOOR=10
```

This command assumes the required private inputs in the table are supplied in
the caller's environment or as named Task variables. Run only after a fresh
explicit approval matching that prepared cap, inside the same existing guard
and host lock with an admitted full-stack budget and finite cleanup allowance.
The two-hour absolute deadline includes downloads and warmup. CU accounting is
best effort polling of an external account, not a billing guarantee; reserve
time and CU for cleanup. Reject insufficient remaining balance, unknown usage,
an unsuitable assignment or a missing prerequisite. Do not autoallocate a
fallback shape, precision, account or second session.

Reasoning must become ready and finish a strict low-effort, 512-token warmup
with nonempty final content and `finish_reason=stop` before embedding starts.
Embedding must produce finite, normalized native 4096-dimensional vectors.
Each model shares its existing 1,200-second startup window across health/alias
readiness and first inference, bounded by the absolute run deadline. Warmup
receives the remaining window to allow cold kernel compilation; a late response
cannot certify readiness. HTTP socket timeouts still rely on the independent
controller and outer guard for the absolute run deadline.
Production startup also checks the actually installed tokenizer/runtime versions,
downloaded metadata hashes and the exact embedding server argv passed to spawn
against the approved pooling evidence before starting its workload.
Record observed software, driver/device and sampled peak resources; a successful
warmup alone does not prove the corpus workload fits.

## Persistence, acceptance and recovery

The controller verifies retained Qdrant PVC/service ownership and owns a local
loopback port-forward. Its private `handoff.env` carries the resulting
`QDRANT_URL` and progress path to local consumers. Never hand-edit an unrelated
Qdrant URL into the run or delete retained PVCs/progress during model shutdown.
Production handoff clears case variants of its frozen settings and routing in
the sourcing terminal before exporting canonical values. The native gateway
then generates fresh model-leg URLs and credentials before its consumers start.
Ingest must reconcile every source hash and chunk outcome, resume only compatible
same-build progress, and publish no partial collection. After cleanup and a new
session, prove the next ordinary operation uses that retained state.

Full acceptance follows [live-stack verification](live-stack.md) and the
[real-corpus runbook](local-real-corpus.md): actual stored content, all golden
cases in the denominator, completed simple/complex HTTP and SSE answers,
console follow-up, fresh traces, exact model/tokenizer identity, actual gateway
transport, full corpus reconciliation, and concurrency 1 then 2. Keep full
evidence private; publish only sanitized opaque identifiers and aggregates.
The separate fresh published signed-bundle/Kind lane uses the existing
[release procedure](local-release-fallback.md#3-disposable-kind-with-both-real-models)
with explicit native-4096 model overrides and per-leg Secret/TLS wiring proven
in the resulting pods. Historical small-model defaults are not Colab overrides.

Remote diagnostic production is separately bounded: 64 KiB per owned log,
64 KiB diagnostic JSON, and at most 1 MiB for the explicit diagnostic archive.
The archive excludes weights and the private corpus. These remote output caps
close a producer/write gap; they do not replace the existing host memory,
disk-admission or serialization guard. The controller drains stdout and stderr
separately, retaining at most 64 KiB per stream. Persistent raw-write allowances
are 4 MiB for work, 256 KiB for export and 512 KiB reserved for release commands.
Truncated structured output cannot establish session or resource identity.
Exhausted diagnostic capture still permits bounded stop and absence checks.
At the post-exit grace boundary, a bounded nonblocking probe checks pending EOF
before classifying incomplete pipes; it adds no wait or output allowance.

These stream limits do not constrain arbitrary workload files, CLI downloads
or Docker daemon writes. Colab freezes an explicit native gateway envelope at
preparation: 2048MiB LiteLLM and 512MiB Postgres, each without swap; an 8MiB
gateway host-log cap and Docker rotation at 8MiB×2 files per container. Later
ambient settings cannot remove those selected limits. Saturated host capture
drains excess output, records truncation, and preserves Docker failure/cleanup.
These limits need measured fit; they do not cap images, volumes, historical logs
or cumulative daemon writes. Retain Jaeger's existing memory/trace caps and admit
every full-stack memory/disk destination separately: the controller cgroup does
not contain Docker descendants. Retain final CU/session evidence on export failure.

After export, cleanup/recovery gives final account usage a command timeout of at
most five seconds and a quarter of the remaining cleanup window, followed by the
existing bounded process-reaping grace. Its output uses the export allowance,
preserving the separate release allowance. The manifest records available balance/rate/assignments
and account balance change, or an explicit unavailable observation. Unavailable
usage makes cleanup incomplete and prevents a CPU transport receipt; exact
session stop and absence verification still execute. Account observations do not
prove per-run billing.

Preparation creates one private source snapshot: at most 32 MiB of execution
source and one separately bounded 64 MiB pinned Task executable. It reuses the
prepared interpreter and copies no environment, corpus or weights. Fresh host
imports, remote uploads, gateway commands and the native workload use that
verified snapshot. This bounds snapshot writes to 96 MiB once per prepared run;
it is not a limit on later workload or daemon writes. External interpreter
dependencies and system tools remain their existing owners' responsibility.

On an interrupted controller, first interrupt its owning process, then use the
same private state for bounded recovery:

```sh
sh scripts/tools/run-task.sh local:colab:stop COLAB_STATE="$COLAB_STATE"
```

The native gateway labels its Postgres and gateway with the fresh private run
identity, after refusing pre-existing names or matching owner labels. Cleanup is
armed before Postgres readiness. Recovery discovers at most those two full
container IDs, rechecks their labels, and removes only those IDs before releasing
the exact created network. This works after the local process group is killed;
pre-existing containers/networks and the key-store volume remain untouched.
Gateway recovery retains its separate five-second window after VM release.

Stop only the exact recorded session. Follow the session-specific recovery
command printed by the controller if automatic cleanup cannot confirm release;
verify that exact session is absent from the explicit OAuth session listing.
Release also requires absence of the bound endpoint under every listed name,
including the CLI's `?` label for server assignments missing local state. An
allocation with no bound endpoint remains `cleanup_required`, even after an
empty listing: a timed-out allocation is not proof that no runtime was created.
Inspect unresolved assignments in the Colab UI; never invent a name or adopt an
unattributed endpoint to stop it automatically.
Preserve the final account usage observation, stop result and absence proof.
An archive export error must not prevent the stop attempt. A dead host cannot
run local traps: the maintainer must stop the owned session from another terminal
or the Colab UI. Unconfirmed release remains a failure, never a successful run.

Before claiming completion, record the exact tested source SHA, commands, exit
codes, expected/observed counts and private evidence locations. Live transport,
GPU co-residency, full corpus/golden acceptance and release restart remain separate
required checks. Production model revisions, precision, effort mapping and
reranker parity still need platform-owner inputs; this public reference does not
establish OpenShift SCC/OAuth, HA, air-gap or site-latency acceptance.
