# Mainframe RAG — connected-host build and air-gap packaging.
# See docs/architecture.md. Include airgap.env (copy of airgap.env.example) when using cluster targets.
SHELL := /bin/bash
-include airgap.env

CHARTS_DIR := charts
VALUES_FILE := overlays/openshift/values.yaml
OC_MIRROR_DIR := oc-mirror
BUNDLE_DIR ?= bundles
IMAGE_ARCHIVE ?= $(BUNDLE_DIR)/qdrant-image.tar
GIT_BUNDLE := $(BUNDLE_DIR)/repo.gitbundle
WHEELHOUSE := $(BUNDLE_DIR)/wheelhouse
BM25_WEIGHTS := $(BUNDLE_DIR)/bm25-weights
BM25_MODEL ?= Qdrant/bm25
REGISTRY ?= $(REGISTRY_INTERNAL)

QDRANT_IMAGE ?= docker.io/qdrant/qdrant:v1.19.0-unprivileged
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
.PHONY: bm25-weights
bm25-weights: $(BM25_WEIGHTS)

$(BM25_WEIGHTS): | .venv
	rm -rf $@ && mkdir -p $@
	.venv/bin/python scripts/fetch_bm25_weights.py --model $(BM25_MODEL) --out $@

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
.PHONY: build-images
build-images: | $(WHEELHOUSE) $(BM25_WEIGHTS)
	docker build --build-context wheelhouse=$(WHEELHOUSE) --build-context bm25=$(BM25_WEIGHTS) \
	  -f images/Containerfile.ingest -t $(INGEST_IMAGE_NAME):$(IMAGE_TAG) .
	docker build --build-context wheelhouse=$(WHEELHOUSE) --build-context bm25=$(BM25_WEIGHTS) \
	  -f images/Containerfile.agent -t $(AGENT_IMAGE_NAME):$(IMAGE_TAG) .

.PHONY: pull-images
pull-images:
	@command -v skopeo >/dev/null || { echo "skopeo required"; exit 1; }
	skopeo copy docker://$(QDRANT_IMAGE) docker-archive:$(IMAGE_ARCHIVE)
	@echo "Record digest in images.txt:"; skopeo inspect docker://$(QDRANT_IMAGE) | grep -E '"Digest"'

.PHONY: push-images
push-images:
	@command -v skopeo >/dev/null || { echo "skopeo required"; exit 1; }
	docker tag $(INGEST_IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(INGEST_IMAGE_NAME):$(IMAGE_TAG)
	docker tag $(AGENT_IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(AGENT_IMAGE_NAME):$(IMAGE_TAG)
	docker push $(REGISTRY)/$(INGEST_IMAGE_NAME):$(IMAGE_TAG)
	docker push $(REGISTRY)/$(AGENT_IMAGE_NAME):$(IMAGE_TAG)

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

# ---------------------------------------------------------------- legacy/manual air-gap steps
.PHONY: pack
pack: chart wheelhouse bm25-weights
	@mkdir -p $(BUNDLE_DIR)
	@[ -f $(IMAGE_ARCHIVE) ] || [ -n "$(wildcard mirror_seq*.tar)" ] || { echo "No image archive: run make pull-images or oc mirror"; exit 1; }
	@if [ -d .git ]; then git bundle create $(GIT_BUNDLE) --all; fi
	@echo "=== Air-gap bundle contents ($(BUNDLE_DIR)) ==="
	@echo "chart:        $$(ls $(CHARTS_DIR)/qdrant-*.tgz)"
	@echo "values:       $(VALUES_FILE)"
	@echo "oc-mirror:    $(OC_MIRROR_DIR)/imageset-config.yaml"
	@echo "images:       $$( [ -f $(IMAGE_ARCHIVE) ] && echo $(IMAGE_ARCHIVE) || echo 'mirror_seq*.tar (oc mirror output)' )"
	@echo "git bundle:   $(GIT_BUNDLE)"
	@echo "wheelhouse:   $(WHEELHOUSE)"
	@echo "bm25 weights: $(BM25_WEIGHTS)"
	@echo "images.txt:   images.txt (digest pins)"

.PHONY: load-images
load-images:
	@command -v skopeo >/dev/null || { echo "skopeo required"; exit 1; }
	skopeo copy docker-archive:$(IMAGE_ARCHIVE) docker://$(REGISTRY)/qdrant/qdrant:$(QDRANT_TAG)

.PHONY: helm-apply
helm-apply: chart
	helm upgrade -i qdrant $(CHARTS_DIR)/qdrant-*.tgz \
	  -n $(OPENSHIFT_NAMESPACE) -f $(VALUES_FILE) \
	  --set image.repository=$(REGISTRY)/qdrant/qdrant

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
	@echo "Connected host : venv wheelhouse bm25-weights pull-chart helm-lint helm-template build-images push-images pull-images"
	@echo "Air-gap happy path : airgap-pack (connected) | airgap-load airgap-deploy airgap-ingest airgap-smoke (inside the gap)"
	@echo "Air-gap legacy/manual : pack load-images helm-apply (or oc-mirror; see docs)"
	@echo "Quality        : test lint typecheck check"
	@echo "See README 'Air-gap' section and docs/architecture.md."
