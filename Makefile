# Mainframe RAG — connected-host build and air-gap packaging.
# See docs/architecture.md. Include airgap.env (copy of airgap.env.example) when using cluster targets.
SHELL := /bin/bash
-include airgap.env

CHARTS_DIR := charts
VALUES_FILE := overlays/openshift/values.yaml
BUNDLE_DIR ?= bundles
WHEELHOUSE := $(BUNDLE_DIR)/wheelhouse
BM25_WEIGHTS := $(BUNDLE_DIR)/bm25-weights
BM25_MODEL ?= Qdrant/bm25

INGEST_IMAGE_NAME ?= mainframe-rag/ingest
AGENT_IMAGE_NAME ?= mainframe-rag/agent
IMAGE_TAG ?= latest

PY ?= $(shell command -v python3.14 2>/dev/null || command -v python3)
PIP ?= $(PY) -m pip

.DEFAULT_GOAL := help

# ---------------------------------------------------------------- venv / deps
# Install with the venv's own interpreter/pip ($(PIP) points at the base
# python — the wheelhouse build deliberately runs there), never into .venv's
# parent.
.venv:
	$(PY) -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.lock.txt -e ".[dev]"

.PHONY: venv
venv: .venv

.PHONY: wheelhouse
wheelhouse: $(WHEELHOUSE)

# Built from the venv so the wheel tags always match the runtime interpreter
# (cp314), regardless of whether the base python ships pip.
$(WHEELHOUSE): requirements.lock.txt | .venv
	rm -rf $@ && mkdir -p $@
	.venv/bin/python -m pip wheel -r requirements.lock.txt -w $@

# BM25 sparse weights must be baked into images; no runtime download in the air-gap.
# Verified against the in-repo sha256 manifest — upstream drift fails closed.
.PHONY: bm25-weights
bm25-weights: $(BM25_WEIGHTS)

$(BM25_WEIGHTS): | .venv
	rm -rf $@ && mkdir -p $@
	.venv/bin/python scripts/fetch_bm25_weights.py --model $(BM25_MODEL) --out $@ \
	  --verify-manifest bm25-weights.sha256

# ---------------------------------------------------------------- cluster recipe
.PHONY: chart
chart: $(CHARTS_DIR)
	@ls $(CHARTS_DIR)/qdrant-*.tgz >/dev/null 2>&1 || { echo "charts/qdrant-*.tgz missing; run: make pull-chart"; exit 1; }

.PHONY: pull-chart
pull-chart:
	mkdir -p $(CHARTS_DIR)
	helm pull qdrant/qdrant --destination $(CHARTS_DIR)

.PHONY: helm-template
helm-template: chart
	helm template qdrant $(CHARTS_DIR)/qdrant-*.tgz -f $(VALUES_FILE) \
	  --set image.repository=PLACEHOLDER_REGISTRY/qdrant/qdrant \
	  --set imagePullSecrets[0].name=PLACEHOLDER_PULL_SECRET \
	  --set persistence.storageClassName=PLACEHOLDER_STORAGE_CLASS \
	  --set snapshotPersistence.storageClassName=PLACEHOLDER_STORAGE_CLASS

.PHONY: helm-lint
helm-lint: chart
	helm lint $(CHARTS_DIR)/qdrant-*.tgz -f $(VALUES_FILE)

# ---------------------------------------------------------------- tests / quality
.PHONY: test
test: | .venv
	.venv/bin/python -m pytest tests -v

.PHONY: lint
lint: | .venv
	.venv/bin/python -m ruff check src tests

.PHONY: typecheck
typecheck: | .venv
	.venv/bin/python -m mypy src

.PHONY: check
check: lint typecheck test

# ---------------------------------------------------------------- images (connected host)
# The air-gap never builds (issue #15): connected main is the only image
# factory, and e2e.yml tags these images with the full git SHA.
.PHONY: build-images
build-images: | $(WHEELHOUSE) $(BM25_WEIGHTS)
	docker build --build-context wheelhouse=$(WHEELHOUSE) --build-context bm25=$(BM25_WEIGHTS) \
	  -f images/Containerfile.ingest -t $(INGEST_IMAGE_NAME):$(IMAGE_TAG) .
	docker build --build-context wheelhouse=$(WHEELHOUSE) --build-context bm25=$(BM25_WEIGHTS) \
	  -f images/Containerfile.agent -t $(AGENT_IMAGE_NAME):$(IMAGE_TAG) .

# ---------------------------------------------------------------- air-gap happy path (issue #15)
.PHONY: airgap-pack airgap-load airgap-deploy airgap-ingest airgap-smoke
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

# ---------------------------------------------------------------- simulation (docker Qdrant, tests/test_integration_sim.py)
SIM_CONTAINER ?= qdrant-sim
SIM_PORT ?= 6333

# End-to-end simulation: real PDFs -> real ingest -> agent endpoints.
# Needs docker (or an already-running server via QDRANT_SIM_URL=...).
# -rs surfaces skip reasons (e.g. the vLLM-shaped variant without BM25 cache).
.PHONY: sim
sim: | .venv
	.venv/bin/python -m pytest -m integration -v -rs

# Long-lived sim server for manual iteration: make sim-qdrant, then
# QDRANT_SIM_URL=http://127.0.0.1:$(SIM_PORT) make sim
.PHONY: sim-qdrant
sim-qdrant:
	@if docker inspect $(SIM_CONTAINER) >/dev/null 2>&1; then \
	  echo "sim qdrant already running: QDRANT_SIM_URL=http://127.0.0.1:$(SIM_PORT)"; \
	else \
	  docker run -d --name $(SIM_CONTAINER) --rm -p 127.0.0.1:$(SIM_PORT):6333 \
	    $$($(PY) scripts/qdrant_pin.py); \
	  echo "Qdrant sim up: QDRANT_SIM_URL=http://127.0.0.1:$(SIM_PORT) make sim"; \
	fi

.PHONY: sim-clean
sim-clean:
	-docker stop $(SIM_CONTAINER) 2>/dev/null || true

# ---------------------------------------------------------------- benchmarks (simulation tier + load)
AGENT_URL ?= http://127.0.0.1:8080

# Full harness against the pinned Qdrant image; fails on regressions vs the
# committed baseline (RSS/disk x1.5, latency p95 x3 — see scripts/benchmark.py).
.PHONY: bench
bench: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/benchmark.py --collection bench --check benchmarks/baseline.json \
	  --out $(BUNDLE_DIR)/bench-results.json --summary $(BUNDLE_DIR)/bench-summary.md

# Re-record the committed baseline (dedicated PR — AGENTS.md). BENCH_REPEATS
# defaults to 3: the noise floor (min latency/footprint, max errors), so one
# contended pass cannot ratchet the baseline. Record in the gate's own
# environment (CI runners), never a dev machine — _meta.env proves it.
.PHONY: bench-baseline
bench-baseline: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/benchmark.py --collection bench --update-baseline benchmarks/baseline.json \
	  --repeats $(or $(BENCH_REPEATS),3) \
	  --out $(BUNDLE_DIR)/bench-results.json --summary $(BUNDLE_DIR)/bench-summary.md

# Standalone load run against an already-running agent.
.PHONY: loadtest
loadtest: | .venv
	$(PY) scripts/loadtest.py --url $(AGENT_URL) --endpoint search --concurrency 8 --duration 30

# ---------------------------------------------------------------- retrieval accuracy
# Mode-keyed baselines (not comparable): hash gates CI/dev runs; vllm gates
# release-candidate runs against the live embedder. The Settings default is
# vllm (prod-first), so eval defaults to hash explicitly — but the export is
# TARGET-SCOPED, never global: make exports reach every recipe, and the
# airgap scripts refuse EMBED_MODE=hash fail-closed (a global export leaked
# into airgap-deploy and failed the CI dry-run). The eval family below is
# the only consumer of the default.
EMBED_MODE ?= hash
EVAL_BASELINE = $(if $(filter vllm,$(EMBED_MODE)),evals/baseline-vllm.json,evals/baseline.json)
HARNESS_BASELINE = $(if $(filter vllm,$(EMBED_MODE)),benchmarks/harness-vllm.json,benchmarks/harness.json)
eval eval-baseline eval-draft eval-holdout eval-answers eval-report eval-html eval-compare \
	harness-gate harness-baseline harness-l2: \
	export EMBED_MODE := $(EMBED_MODE)

# Eval against a running Qdrant (sim-qdrant or QDRANT_SIM_URL); scores
# the dev golden set (evals/golden.jsonl) through the real pipeline
# (recall@k / MRR) and checks against the mode-keyed baseline.
# The frozen holdout (evals/holdout.jsonl) is NEVER iterated against:
# `make eval-holdout` runs on release candidates only (AGENTS.md).
.PHONY: eval eval-baseline eval-draft eval-holdout verify-golden
eval: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/eval_retrieval.py --golden evals/golden.jsonl --check $(EVAL_BASELINE) \
	  --out $(BUNDLE_DIR)/eval-report.json --summary $(BUNDLE_DIR)/eval-summary.md

# Mechanical verification of golden expectations against the live collection
# (doc/heading/page/message-id facts, must_not trap validity, duplicates).
# Gates the corpus: nonzero exit on any FAIL. Verifies BOTH the dev set and
# the frozen holdout.
verify-golden: | .venv
	.venv/bin/python scripts/verify_golden.py

# Release candidates only: verify the holdout sha256 pin, then score the
# frozen holdout against its own baseline. Never iterate against this.
eval-holdout: | .venv
	@mkdir -p $(BUNDLE_DIR)
	sha256sum -c evals/holdout.jsonl.sha256
	.venv/bin/python scripts/eval_retrieval.py --golden evals/holdout.jsonl \
	  --check evals/holdout-baseline.json \
	  --out $(BUNDLE_DIR)/eval-holdout-report.json --summary $(BUNDLE_DIR)/eval-holdout-summary.md

# Re-record the committed accuracy baseline (dedicated PR — AGENTS.md).
.PHONY: eval-baseline
eval-baseline: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/eval_retrieval.py --golden evals/golden.jsonl --update-baseline $(EVAL_BASELINE) \
	  --out $(BUNDLE_DIR)/eval-report.json --summary $(BUNDLE_DIR)/eval-summary.md

# Answer-tier golden eval: /v1/answer grounding honesty (answer entries must
# ground; abstain/trap entries must not be answered). Needs the LIVE GPU
# stack (Qdrant + embed vLLM + reasoning vLLM) and the same env as `eval`;
# run via the in-process TestClient, one reasoning call per query.
# N bounds the deterministic stratified sample (N=all runs everything).
.PHONY: eval-answers
eval-answers: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/eval_answers.py --max-queries $(or $(N),24) \
	  --out $(BUNDLE_DIR)/eval-answers-report.json --summary $(BUNDLE_DIR)/eval-answers-summary.md

# Layered harness (PR A: L1 retrieval + promotion gate). Snapshot-pinned
# index, per-class recall@5/@8 + MRR + nDCG@8, paired-bootstrap CIs against
# the mode-keyed baseline. RC-only gate (AGENTS.md): never a PR gate, never
# GitLab. RESTORE=always forces a full snapshot recover (default drift-only).
.PHONY: harness-gate harness-baseline
harness-gate: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/harness.py --gate --baseline "$(HARNESS_BASELINE)" --restore $(or $(RESTORE),drift) \
	  --out $(BUNDLE_DIR)/harness-report.json
harness-baseline: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/harness.py --update-baseline --baseline "$(HARNESS_BASELINE)" --restore $(or $(RESTORE),drift) \
	  --out $(BUNDLE_DIR)/harness-report.json

# Harness L2 (answer tier): citation precision/recall, temp-0 NLI faithfulness
# judge, truncation rate from the app's non-stop alerts, syntax-shape gold.
# Live GPU stack only (Qdrant + embedding vLLM + reasoning vLLM); structural
# fails gate the exit code, rates are trend data. Never a PR gate, never
# GitLab. N bounds reasoning-model calls (deterministic stratified sample).
.PHONY: harness-l2
harness-l2: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/harness_l2.py --max-queries $(or $(N),24) \
	  --out $(BUNDLE_DIR)/harness-l2-report.json --summary $(BUNDLE_DIR)/harness-l2-summary.md

# Draft golden-set candidates from a collection's payload (edit the queries).
eval-draft: | .venv
	.venv/bin/python scripts/eval_retrieval.py --label-draft --docs 40

# ---------------------------------------------------------------- reports & artifacts
# Render evaluation and benchmark reports into text, markdown, or self-contained HTML dashboards.
.PHONY: eval-report eval-html eval-compare bench-report bench-html bench-compare query-demo

eval-report: | .venv
	.venv/bin/python scripts/render_report.py eval \
	  --report $(or $(REPORT),$(BUNDLE_DIR)/eval-report.json) \
	  --baseline $(or $(BASELINE),evals/baseline.json)

eval-html: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/render_report.py eval \
	  --report $(or $(REPORT),$(BUNDLE_DIR)/eval-report.json) \
	  --baseline $(or $(BASELINE),evals/baseline.json) \
	  --format html --out $(or $(OUT),$(BUNDLE_DIR)/eval-report.html)
	@echo "Rendered $(or $(OUT),$(BUNDLE_DIR)/eval-report.html)"

eval-compare: | .venv
	.venv/bin/python scripts/render_report.py compare-eval \
	  --base $(or $(BASE),evals/baseline.json) \
	  --current $(or $(CURRENT),$(BUNDLE_DIR)/eval-report.json)

bench-report: | .venv
	.venv/bin/python scripts/render_report.py bench \
	  --report $(or $(REPORT),$(BUNDLE_DIR)/bench-report.json) \
	  --baseline $(or $(BASELINE),benchmarks/baseline.json)

bench-html: | .venv
	@mkdir -p $(BUNDLE_DIR)
	.venv/bin/python scripts/render_report.py bench \
	  --report $(or $(REPORT),$(BUNDLE_DIR)/bench-report.json) \
	  --baseline $(or $(BASELINE),benchmarks/baseline.json) \
	  --format html --out $(or $(OUT),$(BUNDLE_DIR)/bench-report.html)
	@echo "Rendered $(or $(OUT),$(BUNDLE_DIR)/bench-report.html)"

bench-compare: | .venv
	.venv/bin/python scripts/render_report.py compare-bench \
	  --base $(or $(BASE),benchmarks/baseline.json) \
	  --current $(or $(CURRENT),$(BUNDLE_DIR)/bench-report.json)

# Interactive query inspection and debugging CLI
query-demo: | .venv
	PYTHONPATH=. .venv/bin/python scripts/query_demo.py $(if $(QUERY),--query "$(QUERY)",) $(if $(COLLECTION),--collection "$(COLLECTION)",) $(if $(LIMIT),--limit "$(LIMIT)",) $(if $(PRODUCT),--product "$(PRODUCT)",) $(if $(VERSION),--version "$(VERSION)",) $(if $(EMBED_MODEL),--embed-model "$(EMBED_MODEL)",) $(if $(EMBED_URL),--embed-url "$(EMBED_URL)",) $(if $(EMBED_MODE),--embed-mode "$(EMBED_MODE)",) $(if $(DENSE_DIM),--dense-dim "$(DENSE_DIM)",) $(if $(MODEL),--model "$(MODEL)",) $(if $(VLLM_URL),--vllm-url "$(VLLM_URL)",)

# Interactive conversational Q&A assistant (reasoning LLM + Qdrant retrieval)
.PHONY: ask
ask: | .venv
	PYTHONPATH=. .venv/bin/python scripts/query_demo.py --answer $(if $(QUERY),--query "$(QUERY)",) $(if $(COLLECTION),--collection "$(COLLECTION)",) $(if $(LIMIT),--limit "$(LIMIT)",) $(if $(PRODUCT),--product "$(PRODUCT)",) $(if $(VERSION),--version "$(VERSION)",) $(if $(EMBED_MODEL),--embed-model "$(EMBED_MODEL)",) $(if $(EMBED_URL),--embed-url "$(EMBED_URL)",) $(if $(EMBED_MODE),--embed-mode "$(EMBED_MODE)",) $(if $(DENSE_DIM),--dense-dim "$(DENSE_DIM)",) $(if $(MODEL),--model "$(MODEL)",) $(if $(VLLM_URL),--vllm-url "$(VLLM_URL)",)

# Local GPU acceleration & vLLM testing
.PHONY: local-vllm local-vllm-embed test-vllm-e2e
local-vllm:
	sh scripts/run_local_vllm.sh

local-vllm-embed:
	MODEL=$(or $(MODEL),Qwen/Qwen3-Embedding-0.6B) PORT=$(or $(PORT),8001) GPU_MEM=$(or $(GPU_MEM),0.30) MAX_LEN=$(or $(MAX_LEN),4096) sh scripts/run_local_vllm.sh

test-vllm-e2e: | .venv
	PYTHONPATH=. .venv/bin/python scripts/test_local_e2e_vllm.py $(if $(MODEL),--model "$(MODEL)",) $(if $(VLLM_URL),--vllm-url "$(VLLM_URL)",) $(if $(EMBED_MODEL),--embed-model "$(EMBED_MODEL)",) $(if $(EMBED_URL),--embed-url "$(EMBED_URL)",) $(if $(DENSE_DIM),--dense-dim "$(DENSE_DIM)",) $(if $(EMBED_MODE),--embed-mode "$(EMBED_MODE)",)


# ---------------------------------------------------------------- e2e demo
.PHONY: e2e-demo-pdfs
e2e-demo-pdfs: | .venv
	mkdir -p output/demo-pdfs
	.venv/bin/python scripts/make_synthetic_pdf.py --out output/demo-pdfs/SA22-0000-00_outline.pdf
	.venv/bin/python scripts/make_synthetic_pdf.py --plain --out output/demo-pdfs/plain-widget-notes.pdf

# ---------------------------------------------------------------- clean
.PHONY: clean
clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache $(BUNDLE_DIR) output

.PHONY: help
help:
	@echo "Connected host : venv wheelhouse bm25-weights pull-chart helm-lint helm-template build-images"
	@echo "Air-gap happy path : airgap-pack (connected) | airgap-load airgap-deploy airgap-ingest airgap-smoke (inside the gap)"
	@echo "Simulation     : sim (pytest integration tier; docker Qdrant) | sim-qdrant sim-clean"
	@echo "Benchmarks     : bench (regression gate vs baseline) | bench-baseline (re-record) | loadtest"
	@echo "Accuracy       : eval (golden-set recall/MRR) | eval-baseline (re-record) | eval-draft (label helper)"
	@echo "Reports & Demo : eval-report eval-html eval-compare | bench-report bench-html bench-compare | query-demo ask"
	@echo "Local vLLM / GPU : local-vllm (serve reasoning model) | local-vllm-embed (serve embedding model) | test-vllm-e2e (automated end-to-end suite)"
	@echo "Quality        : test lint typecheck check"
	@echo "See README 'Air-gap workflow' section and docs/architecture.md."
