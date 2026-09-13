# qdrant-pdf-rag — SOTA Roadmap (Agent-Ready, v2)

> Each section below maps 1:1 to a GitHub issue (#75–#94). An agent picking up a task
> MUST read: the issue, this document, `AGENTS.md`, `docs/architecture.md`, and
> `docs/adr/0001-baseline-decisions.md` before writing code.

## Goal and decision basis

Deliver useful mainframe answers and procedures that operators can verify against
indexed manuals, and abstain when the evidence is insufficient. Success means
measured retrieval and answer quality within the site's latency and resource
budget, while preserving offline operation, corpus confidentiality, platform
ownership of model serving, and the ingestion/citation/API contracts.

The proposals below are hypotheses, not a checklist required to be “SOTA”. Start
with an observed failure and its per-query evidence; prefer removing unnecessary
work or improving the existing path before adding a model, retrieval leg, service,
or flag. Adopt added complexity only when measured benefit justifies its runtime,
air-gap packaging, and maintenance costs. A saturated synthetic gate proves no
semantic improvement; use the appropriate evidence venue in [docs/eval.md](docs/eval.md).

This file owns roadmap status, decision evidence, and reopening conditions.
Current implementation contracts live with their owners:

- [docs/architecture.md](docs/architecture.md): system boundaries and module map.
- [docs/ingest.md](docs/ingest.md): parsing, chunking, embedding, and point identity.
- [docs/retrieval.md](docs/retrieval.md): retrieval flow, ranking, and measured verdicts.
- [docs/agent.md](docs/agent.md): answers, chat, citations, and API contracts.
- [docs/deploy.md](docs/deploy.md): packaging and air-gap deployment.
- [docs/eval.md](docs/eval.md): evaluation instruments and their limits.

Completed and rejected entries record outcomes rather than obsolete implementation
steps. Open entries still require the linked issue's current scope and the
applicable ADR before implementation; their dependencies do not override a
rejection or its reopening gate.

## Global rules for every PR

1. **Run the applicable gates.** [AGENTS.md](AGENTS.md) and
   [docs/live-stack.md](docs/live-stack.md) define the required rungs by change class
   and the A/B evidence owed in the PR. [docs/eval.md](docs/eval.md) owns the
   dev/RC venue rules; the layered harness is RC-only, not a blanket PR requirement.
2. **Feature flags, not rewrites.** New capabilities land behind a `Settings` flag
   (`src/mainframe_rag/config.py`), default-off where behavior changes.
3. **Air-gap vendoring.** Any new model/binary: pinned version + sha256 + offline load
   path, following the `images.txt` / `bm25-weights.sha256` pattern. No runtime downloads.
4. **One PR = one capability.** Dependencies listed per PR. Check the linked issue for
   status before starting.

---

## P0 — Foundation & highest ROI

### PR-01 (issue #75): Wire the eval gate into CI + close metric gaps
> **Status: DONE — merged as PR #96.** `make gate-l1` is a required GitHub CI check;
> do not re-implement.

Current gate and CI contracts: [docs/eval.md](docs/eval.md) and
[docs/deploy.md](docs/deploy.md).

### PR-02 (issue #76): Cross-encoder reranking
> **Status: DONE — merged as PR #97.** `retrieve/rerank.py`, default-off
> (`rerank_enabled=False`).
> Shipped since: `rerank_fusion_alpha` blend, `Table:`/`Syntax:` passage
> templates, `RERANK_ENDPOINT_ORDER` (`score_first` legacy wire order,
> `rerank_first` for gateways), trap/identifier bypass. `probe_gateway.py`
> recommends the order per deployment.

Current rerank behavior and gateway leg selection: [docs/retrieval.md](docs/retrieval.md).
The platform owns model serving; this repo consumes its configured HTTP endpoint.

### PR-03 (issue #77): Async stack + SSE streaming
> **Status: DONE — merged as PR #98.** Async routes + `AsyncQdrantClient`, SSE on
> `/v1/answer` with TTFT.

Current async, streaming, and client-lifecycle contracts: [docs/agent.md](docs/agent.md).

### PR-04 (issue #78): Contextual retrieval (chunk context prefixes)
> **Status: DONE.** Shipped as `ingest/context.py` (versioned `CONTEXT_PROMPT_VERSION=v2`
> cache keyed by `v2:sha:chunk_id`, dense-only contexts) behind `CONTEXTUAL_EMBED_ENABLED`
> (default off); enabling changes every dense vector so the collection must be recreated.
> Tests: `tests/test_contextual.py`.

Measured verdict (issue #300, 2026-09-12): paraphrase vLLM A/B
(header-only vs contextual, 22 entries) is 22/22 identical with both
arms saturated at 1.0 — no signal, stays default-off, no RC escalation.
Gate-l1 cannot register this flag: it is hash-only and saturated, while
contextual embedding refuses hash mode. This result does not establish a
quality benefit or prove that the feature is useless on other corpora.

Current embedding and evaluation contracts: [docs/ingest.md](docs/ingest.md)
and [docs/eval.md](docs/eval.md).

### PR-05 (issue #79): Code-atomic chunking for JCL/REXX (rescoped)
> **Status: DONE.** `detect_code_region` + statement-boundary splitters in `ingest/chunk.py`;
> `heading_path` filterable in the Qdrant payload (`qdrant_io.py`); UUID5 contract kept.
> Tests: `tests/test_chunk_ibm_shape.py` (JCL/REXX fixtures, no-statement-split).

Current code-atomic chunking and point-identity contracts: [docs/ingest.md](docs/ingest.md).

### PR-06 (issue #80): vLLM prefix caching + prompt ordering + injection hardening
> **Status: DONE.** `order_prompt_blocks` (`retrieval`/`stable_cache` policies) in
> `agent/answer.py` with prefix-cache-safe static instructions; `--enable-prefix-caching`
> documented for the LOCAL reasoning server (`install_and_ops.md` §3.6, Budget `prefix_cache`);
> injection screening via `retrieve/screen.py` (trap before identifiers).
> Tests: `tests/test_prompt_order.py`, `tests/test_screen.py`.

Current prompt-ordering and local serving contracts: [docs/agent.md](docs/agent.md)
and [docs/live-stack.md](docs/live-stack.md).

---

## P1 — Quality & operability

### PR-07 (issue #81): Parent-child (small-to-big) retrieval
- **Scope:** `ingest/chunk.py`, `ingest/qdrant_io.py`, `retrieve/query.py`
- **Implementation:** Embed child chunks (~128–256 tokens); retrieve children, return
  parent (~512–1024 tokens) to the LLM; group via payload `parent_id`; dedupe multiple
  children → same parent. Keep the existing UUID5 point-id contract.
- **Tests:** dedupe test; payload round-trip in `test_qdrant_io.py`.
- **Gate:** L1 + L2 improvement vs PR-04 baseline.
- **Depends on:** #78, #79.

### PR-08 (issue #82): Query understanding — acronym expansion + HyDE (gated)
> **Status: CLOSED (acronym shipped; HyDE/step-back measured harmful, will not pursue).**
> Deterministic acronym expansion shipped (`retrieve/rewrite.py` +
> `acronyms_v1.json`, `acronym_expansion_enabled=False`, identifier bypass;
> tests `tests/test_rewrite.py`). The LLM-rewriting remainder was built and
> measured in PR #175 (branch `feat/82-hyde-stepback`, closed unmerged):
> paired A/B on the real corpus showed HyDE recall@1 −0.015 for +1 LLM
> call per query, step-back −0.177 (strips the product/feature tokens that
> select the right manual), combined −0.192 — full per-query attribution in
> that PR body; implementation + `--ab` instrument remain salvageable from
> the branch. The acronym implementation remains available.
>
> **Reopening gate (all required):** per-query attribution shows failures
> from vocabulary mismatch (query terms absent from relevant docs) on the
> post-#214/#216 stack; paired A/B on a post-re-ingest real corpus; beats
> the +1-call latency cost. Until then, do not re-propose.

Current acronym behavior and measured retry verdicts: [docs/retrieval.md](docs/retrieval.md).

### PR-09 (issue #83): OpenTelemetry tracing
> **Status: DONE.** Owner moved to `src/mainframe_rag/tracing.py`; library default off,
> air-gap deploy default ON — an unset endpoint resolves to the in-cluster Jaeger and the
> `off` sentinel disables tracing and the deployment together. Fail-open bounded export +
> Jaeger v2 all-in-one overlay (`deploy/kustomize/jaeger`).
> Tests: `tests/test_tracing.py`, `tests/test_ingest_tracing.py`.

Current tracing ownership and deployment contracts: [docs/architecture.md](docs/architecture.md)
and [docs/deploy.md](docs/deploy.md).

### PR-10 (issue #84): LLM-as-a-judge eval stage
> **Status: DONE (via #268 L4).** Engine `scripts/harness_l4.py` reuses the
> L2 runner (`harness_l2.py` adds the relevance leg), reference
> `evals/harness-l4-thresholds.json` with a tolerance band and a
> human-review queue; the deterministic gate-l1/L1 checks stay the gate.
> RC-only by design (`VENUE=rc`), never a PR gate.

Current L4 instrument and RC-only evaluation contract: [docs/eval.md](docs/eval.md).

### PR-11 (issue #85): Layout-aware parsing pilot (Marker)
- **Why:** PyMuPDF text extraction mangles IBM manual tables/diagrams (real concern) —
  but "fatal" is unproven. Measure, then decide.
- **Scope:** new `ingest/layout.py` (alternative front-end feeding `chunk.py`), vendored
  Marker models, `deploy/` GPU notes
- **Implementation:** Marker → structured Markdown (tables preserved) → existing
  section-outline chunking. Build a golden subset of table/diagram-heavy questions
  (process in `scripts/build_golden_corpus.py`). A/B: PyMuPDF vs Marker. Flag-gated;
  adopt only if the delta justifies ingest cost.
- **Acceptance:** A/B report in PR; adopt/reject ADR recorded in `docs/adr/`.
- **Depends on:** #75, #79.

### PR-12 (issue #86): Corrective retrieval + abstention (CRAG-style)
> **Status: CLOSED (will-not-pursue; measured negative 2026-09-12).**
> RRF fused-score margin cannot separate recall@1 misses from hits
> (dev-golden replay over `real_manuals`: T=0.10 catches 22/26 misses but
> retries 58/82 hits), and acronym-expansion-as-retry fixes 2 misses while
> breaking 4 (net −2 live on the queries where it fires). No trigger and
> no variant measured; the deterministic harness stays the gate. Numbers
> in `docs/retrieval.md` §4/§7.
>
> **Reopening gate (all required):** a confidence signal with measured
> hit/miss separation plus a retry variant with measured positive
> headroom, both with per-query attribution on post-freeze real pools.
> Until then, do not re-propose.

Decision evidence and reopening conditions: [docs/retrieval.md](docs/retrieval.md).

### PR-13 (issue #87): Prompt-injection & retrieved-content hygiene
> **Status: PARTIAL.** Baseline regex injection screen shipped (`retrieve/screen.py`,
> trap before identifiers), extract-time PDF sanitization shipped (`ibm_pdf.sanitize_page_text`),
> and context bounding shipped (`max_context_chars`). Tests are distributed across
> `tests/test_sanitize.py`, `tests/test_hygiene.py`, and `tests/test_screen.py`.
> Advanced LLM-based hygiene / dual-LLM guards remain open.
- **Scope:** `agent/answer.py`, `ingest/ibm_pdf.py` sanitization, `tests/test_sanitize.py`, `tests/test_screen.py`
- **Implementation:** Anything not covered by #80: strip/neutralize control sequences in
  extracted PDF text at ingest; size-cap assembled context (respect
  `max_context_chars`); injection fixture battery (instruction overrides, fake system
  messages, delimiter escapes — including via `splunk_context`).
- **Acceptance:** security fixture suite green in both CIs.
- **Depends on:** #80.

---

## P2 — Expansion

### PR-14 (issue #88): SPLADE sparse leg (measured)
- **Scope:** `ingest/embed.py`, `retrieve/query.py`, vendored SPLADE model
- **Implementation:** Add SPLADE sparse vectors as a THIRD prefetch leg alongside
  FastEmbed BM25; compare BM25 vs SPLADE vs both under the existing weighted RRF.
  Keep the winner — existing identifier-weighted BM25 is strong for exact codes.
- **Gate:** three-way L1 comparison; decision recorded in `docs/adr/`.
- **Depends on:** #75, #76.

### PR-15 (issue #89): Embedding improvement track
- **Scope:** `evals/expert_golden_seed.jsonl` → training pairs; `ingest/embed.py`,
  `manifest.py`
- **Implementation:** Hard-negative mining from failed harness runs; fine-tune or evaluate
  alternates. Embedding model version recorded in `manifest.py` with re-embed detection.
  NOTE: dense embeddings are served by the other team's vLLM — a model change is a
  cross-team deliverable; this repo's part is versioning, detection, and eval.
  If an MRL-capable model is adopted, Matryoshka truncation becomes available;
  otherwise skip Matryoshka, use #92.
- **Depends on:** #75, #84.

### PR-16 (issue #90): ADR 0003 — agent-initiated live-state retrieval (rescoped)
> **Status: PARTIAL — ADR proposed (`docs/adr/0003-zowe-mcp-read.md`) and a
> read-only Zowe bridge + agent wiring + mock backend shipped behind
> `zowe_mcp_enabled=false` (PRs #228–#231: `src/mainframe_rag/mcp/`,
> routing/fetch orchestration, sim-tier mock).** Tool-result prompt wiring
> and any Splunk-connector work remain open.
- **Why:** ADR 0001 decided "Splunk: context in, not crawl" and `/v1/answer` already
  accepts caller-supplied `splunk_context`. Letting the AGENT fetch live state
  supersedes ADR 0001 → per repo policy this REQUIRES a new ADR + `architecture.md`
  update in the same PR. No code in this PR.
- **Scope:** `docs/adr/0003-*.md`, `docs/architecture.md`
- **Implementation:** ADR covering: routing taxonomy (static-manual vs live-state vs
  hybrid questions); read-only Splunk REST/SPL connector interface; auth; audit logging;
  dry-run mode; fallback when telemetry is unreachable; explicit "no job control, no
  commands" safety statement; migration from caller-supplied `splunk_context`.
- **Acceptance:** ADR approved before #91 is scheduled.
- **Depends on:** none (document only).

### PR-17 (issue #91): Read-only ops tool-calling
> **Status: OPEN — transport exists** (read-only Zowe bridge + mock backend
> from #228–#231, default-off); allowlisted function-calling, audit logging,
> and tool-result prompt wiring are still to do.
- **Scope:** new `agent/tools/`, feature-flagged, allowlisted tools only
- **Implementation:** Function-calling against the #90 connector interface; every call
  audit-logged with request id; disabled by default; tool results wrapped as untrusted
  data per #87. Must satisfy ADR 0003 exactly.
- **Gate:** tool calls visible in OTel traces (#83) and audit log; non-allowlisted calls
  rejected; flag off = zero tool surface.
- **Depends on:** #90, #83, #87.

---

## P3 — Optional / research (only on measured gaps)

### PR-18 (issue #92): Qdrant RAM reduction
- Scalar/product quantization + on-disk payload on the dense collection; measure recall
  delta + RAM/latency improvement via harness. Prefer over Matryoshka unless #89 adopted
  an MRL model. Qdrant 1.19.0 — verify quantization API against that version.
- **Depends on:** #75.

### PR-19 (issue #93): ColBERT late-interaction pilot
- ONLY if exact-code retrieval still fails after #76 + #88. Qdrant multivector + MaxSim
  (verify 1.19.0 support); expect ~10x vector RAM — quantify against #92 first.
- **Depends on:** #76, #88, #92.

### PR-20 (issue #94): Cross-reference graph ("GraphRAG-lite")
- ONLY if multi-hop questions still fail after #82 + #86. Extract entity/see-also/syntax
  cross-references at ingest (deterministic, no LLM community summaries); 1-hop expansion
  at retrieval. Full GraphRAG out of scope unless this proves the direction.
- **Depends on:** measured failure after #82, #86.

---

## Shipped after 2026-09-05 without an issue slot

Work that landed outside the #75–#94 numbering. New scope still needs an
issue first — this section records what exists so agents stop re-proposing it.

- **LiteLLM-gateway trio (PRs #246–#248):** per-leg `*_API_KEY` virtual
  keys sent as `Authorization: Bearer` via the one helper
  `bearer_auth_headers` (keyless = wire-identical); delivery through the
  operator-created `GATEWAY_API_KEY_SECRET` Secret + `secretKeyRef`
  (plaintext in `airgap.env` dies fail-closed); `RERANK_ENDPOINT_ORDER`
  (`score_first` legacy, `rerank_first` for gateways, symmetric fallback);
  `scripts/probe_gateway.py` proves each leg in-cluster and recommends the
  order. No `litellm` dependency — the gateway is consumed over HTTP.
- **Air-gap precedence fix (#245):** `DENSE_DIM` joined `OPERATOR_ENV_KEYS`,
  so explicit env beats a stale `airgap.env`.
- **Multi-path retrieval (issue #214, PR #217, verdict #270):** deterministic
  comparative split (`X versus Y`) measured ON (`comparative_split_enabled` defaults
  true) + diagnostic dual-path measured neutral-negative and stays default-off
  (`diagnostic_dualpath_enabled`).
- **Table/syntax fidelity (issue #216, PR #220) + type-boost OFF (#239):** atomic table
  rows, widened message detector, page-label spans; real-corpus replay
  verdict recorded in `docs/eval.md`.
- **Capture-pool + record-replay (#237/#238):** prefetch-pool capture and a
  runbook for production ranking tuning without re-ingesting.
- **Explicit rerank URL in hash mode (issue #193, PR #195):** `RERANK_BASE_URL` opts a
  hash-mode stack into the live cross-encoder.
- **Serving profiles:** `LOCAL_RT_8GB` / `TRIPLE_8GB` / `RANK_EMBED_8GB`
  VRAM budgets + `serve resolve` CLI (`src/mainframe_rag/serve/`).
- **Retrieval precision (#240–#242):** member-case canonicalization,
  doc-number prefix matching, heading-fragment stripping.
- **Operator console + multi-turn chat (ADR-0004, PR #311):** the Streamlit
  prototype is replaced by an agent-served `/ui` (Jinja2 + vendored HTMX/SSE,
  browser-only `localStorage`, strict CSP, `UI_ENABLED` fail-closed); native
  `POST /v1/chat` + OpenAI-compatible `/v1/chat/completions` share the new
  `agent/answer_core.py` engine with `/v1/answer`; follow-up condensation is
  default-off with its A/B recorded (`make eval-chat`); `AGENT_ROUTE=true`
  renders the `openshift-ui` oauth-proxy overlay + reencrypt Route; new runtime
  deps `jinja2` + `python-multipart`.

---

## Explicitly rejected from the original review

- **Full GraphRAG at ingest:** worst cost/benefit for a static-manual corpus; the
  motivating example (JCL→REXX→VSAM→RACF) describes the *live environment* — that's
  #90/#91's problem, not a KG's.
- **ColBERT as default:** multi-vector RAM contradicts the same review's RAM concern;
  gated behind measured failure (#93).
- **Matryoshka standalone:** meaningless without an MRL embedding model; folded into
  #89/#92.
- **"Text extraction is a fatal flaw":** overstated; measured pilot (#85).
- **"No hierarchical chunking":** wrong — section-outline chunking with `heading_path`
  exists; the gap is code-atomic blocks (#79) and parent-child sizing (#81).
- **"No eval gate":** wrong — `scripts/harness*.py` already implements bootstrap-gated
  regression checks; #75 is CI enforcement, not greenfield.

---

Tracking: issues #75–#94 (PR-01 → #75 … PR-20 → #94).
