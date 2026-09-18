# Mainframe RAG — command entry (Make -> Task compatibility shim, issue #402).
# See docs/task-runner.md#shim.
#
# Every target below forwards once to the pinned Task implementation and does
# nothing else: Task owns dispatch, defaults, ordering and freshness; scripts
# keep validation, lifecycle and pipeline logic. Never Task -> Make.
#
# Forwarding rule (proved by tests/test_taskfile_contracts.py make/task
# consistency checks): recipes forward PY explicitly because Make resolves it
# (`python3.14 || python3`) differently from Task (`python3`). Every other
# caller override rides the recipe environment into Task's env form
# automatically — both `VAR=x make target` and `make target VAR=x` keep
# working with no per-target repetition. Task-side defaults match Make's.
#
# Historical note (kept deliberately): airgap.env was never `-include`d here.
# scripts/airgap/*.sh source it themselves with explicit environment winning
# over the file (see common.sh); a make-level include would silently override
# `VAR=x make ...` with stale file values. Task keeps the same rule: no root
# dotenv, per-task bridges only.
#
# Shim requirements and limits:
# - Connected host targets require the pinned `task` on PATH (sh scripts/tools/install-task.sh;
#   doctor reports it). Without it, forwarding targets fail with command-not-found.
# - Air-gap entry points (`airgap-*`) call stage scripts directly so offline
#   bastions without Task remain functional until increment C offline handoff (TR437-F1 Option 2).
# - Multiple goals run sequentially through separate task processes.
# - Do not use -j: concurrent task processes must never share one .venv or
#   bundle dir for installs/builds (Task `deps` are concurrent by design and
#   are not used by the migrated tasks either).
# - -C dir resolves tasks against the repository root; caller-relative paths
#   resolve from the root, not from dir.
# - Unknown targets fail with make's own "No rule to make target" error; no
#   catch-all is provided on purpose.
# This shim is removed at the #402 D-gate once every executable consumer has
# moved and the signed offline bootstrap works without Make.
SHELL := /bin/bash

PY ?= $(shell command -v python3.14 2>/dev/null || command -v python3)

.DEFAULT_GOAL := help

# ---------------------------------------------------------------- venv / deps
.PHONY: venv
venv:
	task dev:setup PY="$(PY)"

# ---------------------------------------------------------------- artifacts
.PHONY: wheelhouse
wheelhouse:
	task artifacts:wheelhouse

.PHONY: bm25-weights
bm25-weights:
	task artifacts:bm25

# ---------------------------------------------------------------- cluster recipe
.PHONY: chart
chart:
	task artifacts:chart-check

.PHONY: pull-chart
pull-chart:
	task artifacts:chart-fetch

.PHONY: helm-template
helm-template:
	task artifacts:helm-render

.PHONY: helm-lint
helm-lint:
	task artifacts:helm-lint

# ---------------------------------------------------------------- tests / quality
.PHONY: test
test:
	task qa:unit

.PHONY: lint
lint:
	task qa:lint

.PHONY: typecheck
typecheck:
	task qa:typecheck

.PHONY: check
check: lint typecheck test

.PHONY: check-context agent-doctor
check-context:
	task qa:context PY="$(PY)"

agent-doctor:
	task dev:doctor PY="$(PY)"

# ---------------------------------------------------------------- images (connected host)
.PHONY: build-images
build-images:
	task artifacts:images

# ---------------------------------------------------------------- air-gap happy path (issue #15)
# Direct script wrappers retained until offline Task handoff lands (TR437-F1 Option 2).
.PHONY: airgap-pack airgap-load airgap-deploy airgap-ingest airgap-smoke airgap-validate airgap-pipeline airgap-dryrun
airgap-pack:
	sh scripts/airgap/pack.sh
airgap-load:
	sh scripts/airgap/load.sh
airgap-deploy:
	sh scripts/airgap/deploy.sh
airgap-ingest:
	sh scripts/airgap/ingest.sh
airgap-smoke:
	sh scripts/airgap/smoke.sh
airgap-validate:
	sh scripts/airgap/validate.sh
airgap-pipeline:
	sh scripts/airgap/pipeline.sh
airgap-dryrun:
	AIRGAP_DRYRUN=1 IMAGE_SHA=$$(git rev-parse HEAD 2>/dev/null || echo "0000000000000000000000000000000000000000") \
	  INTERNAL_REGISTRY=registry.example.internal/mainframe-rag \
	  NAMESPACE=mainframe-rag \
	  STORAGE_CLASS=gp3-csi \
	  EMBED_MODEL=ibm-granite/granite-embedding-278m-multilingual \
	  DENSE_DIM=768 \
	  EMBED_MODEL_REVISION=dryrun-rev-1 \
	  VLLM_BASE_URL=http://vllm.inference.svc.cluster.local:8000/v1 \
	  CORPUS_PVC=corpus-pvc \
	  sh scripts/airgap/pipeline.sh --dry-run

# ---------------------------------------------------------------- simulation (docker Qdrant, tests/test_integration_sim.py)
.PHONY: sim
sim:
	task qa:sim

.PHONY: sim-qdrant
sim-qdrant:
	task local:qdrant:up

.PHONY: sim-clean
sim-clean:
	task local:qdrant:down

# ---------------------------------------------------------------- benchmarks (simulation tier + load)
.PHONY: bench
bench:
	task eval:bench

.PHONY: bench-baseline
bench-baseline:
	task eval:bench-baseline

.PHONY: loadtest
loadtest:
	task eval:load PY="$(PY)"

.PHONY: loadtest-mock
loadtest-mock:
	task qa:load

# ---------------------------------------------------------------- retrieval accuracy
.PHONY: eval eval-baseline eval-draft eval-holdout verify-golden gate-l1
eval:
	task eval:retrieval

.PHONY: eval-paraphrase
eval-paraphrase:
	task eval:paraphrase

.PHONY: gate-l1
gate-l1:
	task eval:gate-l1

.PHONY: verify-golden
verify-golden:
	task eval:verify-golden

.PHONY: eval-holdout
eval-holdout:
	task eval:holdout

.PHONY: eval-baseline
eval-baseline:
	task eval:baseline

.PHONY: capture-pool
capture-pool:
	task eval:capture-pool

.PHONY: eval-answers
eval-answers:
	task eval:answers

.PHONY: eval-chat
eval-chat:
	task eval:chat

.PHONY: harness-gate harness-baseline
harness-gate:
	task eval:harness:gate
harness-baseline:
	task eval:harness:baseline

.PHONY: harness-l2
harness-l2:
	task eval:harness:l2

.PHONY: harness-l3 harness-l3-baseline
harness-l3:
	task eval:harness:l3

harness-l3-baseline:
	task eval:harness:l3-baseline

.PHONY: harness-l4 harness-l4-record
harness-l4:
	task eval:harness:l4
harness-l4-record:
	task eval:harness:l4-record

.PHONY: eval-draft
eval-draft:
	task eval:draft

# ---------------------------------------------------------------- reports & artifacts
.PHONY: eval-report eval-html eval-compare bench-report bench-html bench-compare query-demo

eval-report:
	task eval:report

eval-html:
	task eval:html

eval-compare:
	task eval:compare

bench-report:
	task eval:bench-report

bench-html:
	task eval:bench-html

bench-compare:
	task eval:bench-compare

# ---------------------------------------------------------------- local simulation
.PHONY: query-demo
query-demo:
	task local:query

.PHONY: ask
ask:
	task local:ask

.PHONY: local-vllm local-vllm-embed local-vllm-rerank test-vllm-e2e run-agent
local-vllm:
	task local:llm
local-vllm-embed:
	task local:embed
local-vllm-rerank:
	task local:rerank

.PHONY: local-gateway
local-gateway:
	task local:gateway:up

.PHONY: local-gateway-stop
local-gateway-stop:
	task local:gateway:down

.PHONY: local-jaeger local-jaeger-stop
local-jaeger:
	task local:jaeger:up
local-jaeger-stop:
	task local:jaeger:down

.PHONY: local-stack
local-stack:
	task local:stack

.PHONY: test-vllm-e2e
test-vllm-e2e:
	task qa:vllm-e2e

.PHONY: run-agent
run-agent:
	task local:agent

# ---------------------------------------------------------------- e2e demo
.PHONY: e2e-demo-pdfs
e2e-demo-pdfs:
	task dev:demo-pdfs

# ---------------------------------------------------------------- clean
.PHONY: clean
clean:
	task dev:clean

.PHONY: help
help:
	@echo "Command interface is moving to Task (issue #402): 'task --list' and 'task <name> --summary' (docs/task-runner.md). Targets below forward to Task once and need it on PATH (sh scripts/tools/install-task.sh)."
	task --list
