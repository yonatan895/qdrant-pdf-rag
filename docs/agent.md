# Agent HTTP and reasoning reference

Owner: this file. Design overview: `docs/architecture.md` §§4.4–4.5. Day-2
operations: `docs/install_and_ops.md` §5. Retrieval contracts:
`docs/retrieval.md`.

> One fact, one owner — this file owns agent internals. Code is named by
> module and function, never by line number.

## 1. Endpoints

Async routes in `agent/app.py` (search/answer/chat plus the operator console).
Every request gets a 12-hex-char
`request_id` from middleware, shared by all logs, the unhandled-error
handler, and the response (chat surfaces it as `chatcmpl-<request_id>`).

- `POST /v1/search` — `SearchRequest{query (min 1 char), product?,
  version?, limit (default 8, 1–40)}` → `SearchResponse{request_id,
  query_kind, hits}`. No LLM involved.
- `POST /v1/answer` — `AnswerRequest{query, product?, version?,
  splunk_context?, stream (default false)}` → `AnswerResponse{request_id,
  answer, citations, citations_inferred, inferred_indices, script,
  script_lang, verification_state, script_review_required}`.
  Retrieval always runs
  with a hardcoded `limit=8` (tuning the search `limit` does not change
  answers); the JSON response deliberately omits `query_kind`, `hits`,
  `usage`, `finish_reason`, and `ttft` (those live on spans, logs, and the
  SSE `final` event). `citations_inferred` is the provenance flag (issue
  #269): true when every returned cite was mapped from bare bracket markers
  with no explicit citation line — the eval never counts those as grounded.
  `inferred_indices` (issue #299) lists the 1-based `[n]` prompt labels those
  markers pointed at (empty on every other path), so right-doc / wrong-index
  is measurable. Both the allowlist and the label mapping come exclusively
  from the final supplied-evidence manifest (issue #364): retrieved hits that
  packing/trimming omitted, and the prompt's worked example cite, are never
  accepted as grounding.
- `GET /healthz` (readiness) — `HealthzResponse{status, qdrant, embed?,
  representation}`. Qdrant is checked by GET-ting the pooled client's
  `{base}/readyz` and requiring exactly `200` plus the body `all shards are
  ready` (case/space normalized); the upstream body goes to the log, never
  the client. `embed` is tri-state: `None` when no embedder is configured,
  `false` on any exception or non-200 from a `["ping"]` embeddings probe,
  else the boolean result. `representation` is the live, uncached
  `resolve_serving_generation` outcome (issues #391 F3/F4): the configured
  alias resolved to its physical generation and that generation's OWN
  `<physical>__completions` contract compared against the wanted one.
  `status` is `ok` only when Qdrant is ok, embed is not `False`, and
  `representation` is `compatible`, `record_only_drift`, or `empty`;
  anything else is `degraded` **and HTTP 503** (Kubernetes probes judge
  only the status code, so the JSON label alone never made a pod unready).
  `empty` stays ready on purpose: the deploy -> ingest sequence waits for
  the agent before data exists (bootstrap must not deadlock); requests are
  still refused by the serving gate. Any Qdrant exception becomes
  `503 qdrant_unready`.
- `GET /livez` (liveness) — always `200 {"status": "alive"}` while the
  process serves. Data-serving readiness belongs to `/healthz`; the
  deployment livenessProbe points here so a non-servable generation never
  restarts a healthy agent.
- **Serving generation gate** — every retrieval path (`/v1/search`,
  `/v1/answer`, `/v1/chat`, `/v1/chat/completions`, `/ui/chat*`) asks the
  gate for the current generation before any embed/LLM/stream work and
  binds to the validated physical collection name; within
  `REPRESENTATION_CACHE_TTL_S` (default 5s) requests reuse one validation,
  after which a fresh resolution makes an alias swap or rollback visible.
  Not servable (drift, legacy, pending migration, unverified/unknown, or
  `empty`) is the fixed `503 representation_unavailable` JSON envelope on
  the API and the stream endpoints (the console's HTML form keeps its
  fixed error banner). The gate is read-only — it never writes metadata or
  repairs collections.
- `POST /v1/chat` (native) and `POST /v1/chat/completions` (OpenAI-compatible
  alias) — `ChatRequest{messages (client-managed history; min 1, roles
  `system`/`user`/`assistant`, `extra="forbid"`), product?, version?,
  splunk_context?, stream?, temperature?, model?, max_tokens?}`. At least one
   `user`-role message is required (missing user turn is a body-validation
   failure: `422 invalid_request` / `request body failed validation`, issue
   #314). `agent/chat_turn.prepare_chat_turn` owns active-turn normalization
  for API, console, core execution, condensation and prompt assembly (#415).
  The latest user message is stripped, must remain nonempty, and is guarded
  by `query_max_chars`. Earlier user/assistant messages remain history; caller
  system messages and entries after the latest user are excluded from model
  input. Trailing assistant/system entries remain accepted request syntax and
  never become a question or history for that user turn. The **entire supplied
  body**, including excluded entries and Splunk context, is capped by
  `chat_max_body_chars` before normalization removes anything. Missing, blank or
  overlong active questions return the fixed 422 envelope before retrieval or
  model work; the HTML form retains its existing safe error-banner mapping.
  `temperature` overrides
   `Settings.llm_temperature`; `model` is accepted for OpenAI compatibility but
   ignored — inference always uses `Settings.llm_model_reasoning`, and the
  response `model` field reports that reasoning model (issue #313); `max_tokens` is
  accepted and ignored (token limits are server-side). The response is
  `ChatCompletionsResponse{id: "chatcmpl-<request_id>", created, model,
  choices[], usage, citations, citations_inferred, inferred_indices, hits,
  verification_state, script, script_lang, script_review_required}`;
  `choices[0].message.content` carries the answer plus a trailing markdown
  `**Citations:**` bullet list when cites exist. Empty hits return
  `finish_reason: "stop"`, zeroed usage, and empty citations/hits. Provenance
  (`citations_inferred`, `inferred_indices`, issues #269/#299) rides chat JSON
  and the SSE finish chunk. Prior turns: the last `chat_max_turns` (10)
  non-system messages, each cut to `chat_max_prior_turn_chars` (1000,
  ` ... [history truncated]`); pasted `Retrieved manual excerpts:` tails are
  pruned from prior assistant turns. Follow-up turns condense only when
  `CHAT_CONDENSE_ENABLED=true` (default off): one low-effort reasoning call
  rewrites the latest turn into a standalone search query (last 4 non-system
  turns, 1000 chars each), bypasses the LLM entirely when the turn already
  carries a message/member identifier or abend code, and falls back to the raw
  latest text on any failure. The condensation A/B and its default-flip
  decision live in `docs/eval.md` (`sh scripts/tools/run-task.sh eval:chat`).
- `GET /ui` — operator console (ADR-0004), a thin adapter over the same core:
  Jinja2 shell + HTMX form/fragment + SSE stream (`/ui/chat`, `/ui/chat/stream`,
  `/ui/healthz`, `/ui/static/*`), browser-only `localStorage` state, strict CSP.
  Every `/ui` path serves the stable `404 not_found` envelope while
  `UI_ENABLED` is false; the oauth-proxy sidecar authenticates external ingress
  when `AGENT_ROUTE=true` (ADR-0004 §3). The production chart sets
  `UI_ENABLED=true`, so only the external Route is OAuth-protected — the
  ClusterIP 8080 `/ui` stays reachable to in-cluster tools.
- `GET /metrics` — Prometheus text exposition, opt-in via `metrics_enabled`
  (disabled serves the stable `404 not_found` envelope; a scrape failure is
  `503 metrics_unavailable` / `metrics are not available`). No trace span by
  design (issue #187).

Per-endpoint flow: `search` runs the length guard, then retrieval under a
root span — faults become `502 upstream_error / retrieval failed`, timings
become `Server-Timing`, then the response. `answer` resolves streaming
first (`?stream=` wins over the body field when set), runs the length
guard, asserts the reasoning model is configured (`503 not_configured`,
before any retrieval), retrieves under the root span (same `502` as
search), classifies complexity, short-circuits empty hits (§3), builds the
prompt, and either chats once (JSON) or streams (SSE). `chat` runs the same
assertion and retrieval (hardcoded `limit=8` for both endpoints), resolving
the follow-up search query through `resolve_search_query` first so the
condense gate cannot be honored on one path only.

## 2. Error contract

Every JSON error body is `ErrorEnvelope{code, message}` with a fixed
message — no exception text, no upstream bodies, no internals, on any
status (`/ui` failures render HTML banners instead, §1):

| Code | Status | Trigger |
|---|---|---|
| `upstream_error` / `retrieval failed` | 502 | Any retrieval fault, identical on both endpoints |
| `upstream_error` / `answer failed` | 502 | LLM call or response-parse fault |
| `internal` / `internal error` | 500 | Prompt-build failure and any unhandled exception |
| `not_configured` / `reasoning model…` | 503 | `/v1/answer` or `/v1/chat` without `LLM_BASE_URL` + reasoning model (pre-retrieval) |
| `qdrant_unready` / `qdrant…` | 503 | `/healthz` Qdrant exception |
| `representation_unavailable` / `the retrieval generation is not available` | 503 | Serving gate: resolved generation is `empty` or not validated compatible (drift/legacy/pending/unknown); `/ui/chat` renders its banner while `/ui/chat/stream` returns this envelope |
| `invalid_request` / `request body failed validation` | 422 | Pydantic failure, the shared query-length guard, and `/v1/chat` with no `user`-role message (one message, every 422 path) |
| `prompt_budget_exceeded` / `prompt exceeds the model token budget` | 422 | Irreducible token-budget overflow (issue #368): fixed content alone exceeds the window with nothing left to trim; raised before any model call on JSON/chat, as an `error` event (no `final`) on already-open streams; `/ui/chat` renders its fixed banner |
| `metrics_unavailable` / `metrics are not available` | 503 | `/metrics` scrape failure while enabled |
| `not_found` / `not found` | 404 | Unknown route |
| `method_not_allowed` / `method not allowed` | 405 | Wrong method |
| `http_error` / `request failed` | framework's | Framework-raised `HTTPException` (nothing in `src/` raises it; `detail` is stripped) |

Server side keeps `str(exc)[:200]` in logs. The 502/500 split is deliberate:
same fault → same code on every endpoint, and a model/parse fault is never
mislabeled as retrieval.

## 3. Streaming (SSE)

`POST /v1/answer?stream=true` yields zero or more `event: token` deltas,
then exactly one terminal `event: final` carrying the full answer, validated
citations, the `citations_inferred` provenance flag, the `inferred_indices`
list, the `verification_state` label, optional script plus its review flag,
retrieval hits, query kind, `ttft_ms`, and token usage. A mid-stream failure
emits `event: error` and ends **without** a `final` — clients must treat
stream-end-without-final as a failed request. `/ui/chat/stream` consumes the
same token→final contract with a UI-specific terminal event name.

`/v1/chat` + `/v1/chat/completions` with `stream=true` instead stream OpenAI
`chat.completion.chunk` frames (`data: {...}` content deltas), then one
terminal chunk with `finish_reason` carrying `citations`,
`citations_inferred`, `inferred_indices`, and `hits` (chat chunks carry no
`usage`), then `data: [DONE]`. A mid-stream failure emits one
`data: {"error": {"code": "upstream_error", "message": "stream failed"}, "verification_state": "generation_incomplete"}`
frame (the strict `error` object plus the additive sibling state, issue #365)
**followed by** `data: [DONE]` — never a fake `finish_reason`; clients
must treat an error frame as failure even though `[DONE]` still arrives. The
strict stream-end rule above is the upstream reasoning wire and the
`/v1/answer` / `/ui` contract, not the OpenAI-compatible chat contract.

- The `final` schema is identical on the empty-hits path: zero citations,
  `citations_inferred: false`, empty `inferred_indices`, `ttft_ms: null`, zeroed usage. The empty-hits
  short-circuit happens before prompt build and any LLM call, on both JSON
  and SSE. A pre-generation budget failure (issue #368) likewise precedes
  any model call: JSON/chat answer it with `422 prompt_budget_exceeded`,
  while an already-open stream carries `event: error` and ends without
  a `final`.
- `Server-Timing` on SSE responses carries the retrieval legs only;
  `llm`/`ttft` timings ride the `final` event (JSON responses carry all of
  them as headers).
- A stream is complete only with `[DONE]` **and** an explicit non-null
  terminal `finish_reason`: `[DONE]` alone, an upstream `error` frame
  (even when followed by `[DONE]`), and a malformed frame are all
  `TruncatedStreamError`, never a fabricated successful finish (issue
  #365). The non-streaming payload parser requires identical validation
  parity: a top-level `error`, non-string content, or a missing/null
  `finish_reason` is rejected and never synthesizes `stop` or coerces objects.
  Recovery differs between buffered and client-visible calls: see the
  HTTP/model fallback contract below. An explicitly classified `length`
  or `content_filter` finish *with* `[DONE]` is complete transport, not
  truncated — the verification state below owns the incomplete label.
- Supported protocol variants stay supported: `:` comment keepalives,
  blank/non-data lines, OpenAI `include_usage` frames (`choices: []` with
  a usage block), and finish-only or content-null delta frames. Failure
  reasons are fixed labels (`missing [DONE]`, `upstream error frame`,
  `malformed frame`, `missing finish reason`) — upstream error bodies are
  never copied into client responses, logs, or exception text.
- `LLM_STREAM` (default off) routes every server-side reasoning call over
  the streaming wire and measures TTFT on the first content token; the JSON
  paths still return one answer. `sh scripts/tools/run-task.sh local:agent` and `sh scripts/tools/run-task.sh local:stack` set
  it; client-visible SSE is requested per call (`stream=true`). The SSE
  generator ends the root span in a `finally` so disconnects stay in the
  same trace.
- The empty-hits answer echoes up to 5 parsed identifier terms (`, +N more`
  beyond that) for identifier queries, a generic line otherwise — and
  deliberately never falls back to unfiltered serving.

## 4. Prompt assembly and budgets

Complexity (`classify_query_complexity`) drives three things: max context
chars, reasoning effort, and the system prompt variant. Rules: message-id
queries stay `simple` unless a deep root appears; complex roots are
`diagnos, recover, abend, compar, tuning, optimi, tradeoff` substrings;
otherwise multi-step configuration phrasing, comparisons (`versus`, `vs`,
`difference between`), or procedural phrasing paired with operational nouns
(`rule, interval, parameter, parmlib, jcl, policy, threshold, journal`)
select `complex`. Default is `simple`.

- Simple: 8000 context chars, `low` effort. Complex: 4500 context chars,
  `high` effort, extended system prompt. Temperature 0.2 both. The 4500 cap
  reserves roughly 2.6k tokens of headroom for thinking + answer inside the
  4096-token local window — the truncation bug it fixes (8k context starving
  generation, `finish: length`, dropped `Citations:`) must not be
  reintroduced by raising it.
- Blocks are named (`context`, `question`, `excerpt`, `tail`, the standalone
  `excerpts` header when nothing is packed, and the `stable_cache`
  `instructions` block) and packed
  per-type: syntax, message, and table chunks up to 3000 chars; narrative
  prose up to the narrative cap (1100 for complex queries); cuts marked
  with a truncation suffix; packing stops at the context budget with at
  most one partial chunk.
- `prompt_order` policies reorder (never drop or duplicate — violations fail
  closed): `retrieval` keeps historical assembly order byte-identical;
  `stable_cache` frames excerpts in attributeless
  `<retrieved-excerpt>` delimiters and orders instructions → context →
  excerpts → question → tail, with a static deterministic instruction block
  (no cites, timestamps, or ids — the prefix-cache premise) that demotes
  excerpts to untrusted data.
- Tokenizer path (when a tokenizer is configured): plans with the
  in-process estimator (`≈3.5` chars/token, 350-token narrative cap), then
  verifies the packed prompt against the whole-message count per trim round
  and trims up to 4 rounds (64-char overcut, drop under 80 chars, else
  suffix). Chat packing (`build_chat_messages`) uses the same discipline but
  trims in two tiers for up to `4*2 + len(prior turns)` rounds: excerpt
  bodies first, then it pops the oldest history turn. Never per-chunk
  tokenize RPCs.
- Planning and verification both charge reserved output, complex thinking reserve
  and safety margin; simple prompts use no thinking reserve.
- `splunk_context` truncates at 4000 chars with a suffix before packing, so
  caller context can never starve excerpts.
- Evidence manifest (issue #364): `build_messages` / `build_chat_messages`
  return a `PreparedPrompt{messages, evidence}`. `evidence` is built from the
  final packed list after the last trim — never by re-scanning prompt text —
  and records per supplied excerpt its prompt label, `chunk_id`/`doc_id`,
  citation, retained-range boundary, truncation flag, and estimator token
  count, plus the omitted retrieved labels. Duplicate display citations keep
  distinct identities. The manifest is the only source of the citation
  allowlist and of `inferred_indices`; the retrieval list rides
  `AnswerCoreOutput.hits` separately as retrieved candidates. For multi-turn
  chat, prior turns are conversation context, never evidence for the new
  answer.
- The tail's worked example is `hits[0].cite` even when packing dropped that
  hit: it is prompt furniture, never a manifest entry, so echoing it is
  rejected. The system prompt's seven rules (ground-only, synthesize-from-templates,
  version-disagree attribution, admit-gap, fenced scripts as examples,
  identify-a-doc-number/message-id/short-name query instead of refusing,
  mandatory `Citations:` with a few-shot example) plus the complex-query
  extension (decompose, cross-examine, verified fences,
  diagnose-and-recover headings) live in code by design — this file
  documents their shape, not their text.

## 5. Citation validation

Only cite strings actually supplied in the final prompt reach the client,
via two passes plus a trailing sweep in `cites.py` / `parse_answer`. The
allowlist is `PromptEvidence.allowed_citations` (issue #364) — retrieved
hits omitted by budget packing, and the tail's example cite, are rejected:

1. Explicit block: from a `Citations:` header (any `#` depth, any case),
   consuming cite-shaped or bullet lines (after normalization) until the
   first blank past seen cites — later prose is preserved as body.
2. Trailing bare cites: a blank-tolerant tail scan for allowed cite lines
   without any header.
3. Bracket fallback on the fence-processed content (only when the passes
   above found no citation): `[n]` / `[n, m]` only, resolved through the
   manifest's prompt labels (not retrieval rank), deduped, flagged
   inferred. Markers that occur only inside dropped `thought`/`thinking`
   or extracted `SCRIPT_LANGS` fences are never promoted. **Parentheses are
   never inferred** — IBM-manual noise like `z/OS (3.1)` stays body text.

- One shared normalizer peels list markers, `>` quotes, paired
  punctuation, ``[x](url)`` links, `<angle>` wraps, `(parens)` groups, and
  `[1]:`-style numeric prefixes (up to 6 rounds) on both paths — two
  regexes for one concept would diverge.
- Validation is exact-match + dedupe against the supplied set; standalone
  lines only — inline mentions (`refer to SA22-… for details`), dimensions
  (`3.5 inches`), and table pipes survive.
- Fenced code blocks in `SCRIPT_LANGS` (`jcl, rexx, sh, bash, shell,
  python, py, yaml, yml, json, ops, rule, parmlib`) become `script` (joined,
  removed from body) and pass through **unvalidated** — cite-like lines
  inside code are kept; `thought`/`thinking` fences are dropped; other
  fences unwrap to body. Validating scripts would corrupt JCL/REXX;
  treating them as cited provenance would hallucinate it.
- Abstention zero-cite (issues #135/#305): `parse_answer` clears citations
  and inferred indices when `is_abstention` holds — at least one explicit
  refusal marker **and** under 200 chars of non-refusal remainder. Marker
  alone would zero a grounded answer that quotes one hedging sentence;
  shape alone would miss a refusal citing real-but-unsupporting chunks. It
  applies to every consumer (JSON, SSE final, chat, `/ui`).

## 6. Lifespan, pools, timeouts

Everything opened in lifespan is closed in lifespan. The pools: one async
HTTP pool and one sync HTTP pool (bounded connections/keepalive, embed
timeout, connect-only retries — retries never resend a request), the
embedder/tokenizer/reranker sharing the sync pool (they are sync protocols
called via `asyncio.to_thread`), the reasoning client `HttpxLLMClient` with
its own pool and the long answer timeout, and one async Qdrant client on
the query timeout.

- Startup fail-fast order: reject unknown `embed_mode`; refuse
  hash-without-`ALLOW_HASH_MODE`; in vLLM mode require dim + endpoint. The
  LLM is deliberately **not** validated at startup — missing reasoning
  config fails per-request at `/v1/answer` (503, pre-retrieval).
- Shutdown closes the `llm_client` local, not the `llm` global (tests swap
  the global after startup); embedder/tokenizer/reranker have no close
  (shared pool closed once); `close()` never nulls a pool, so post-shutdown
  calls raise instead of silently rebuilding. Qdrant close is awaited only
  if awaitable (sync doubles keep working).
- No sync fallback on the event loop: healthz and retrieval use the pooled
  clients only; a missing pool is a startup bug.
- The reasoning client never retries at the transport (sync and async,
  `retries=0`) — answers are non-idempotent; a retry would re-think. The
  permitted second asks are the per-operation streaming fallbacks documented
  in the HTTP/model contract below; buffered and emitted content differ. Dispatch picks async
  client → running loop → sync, so injected async clients and bare test
  doubles (even plain `str` returns, normalized to chat results) all work.
- Health timeouts are split from traffic timeouts (5s Qdrant, 10s embed);
  the tokenizer RPC gets 5s.

## 7. Settings catalog

Every timeout, retry, batch size, and limit comes from `Settings` with
bounded defaults (new settings require a default assertion in
`test_config.py`); no magic numbers at call sites. Key defaults and their
readers:

| Setting | Default | Read by |
|---|---|---|
| `qdrant_url` / `qdrant_api_key` / `qdrant_collection` / `qdrant_snapshots_dir` | `http://localhost:6333` / unset / `mainframe_manuals` / `/qdrant/snapshots` | lifespan, healthz readyZ, retrieve calls, manifests, harness snapshot restore |
| `qdrant_timeout_s` / `qdrant_ingest_timeout_s` | 30 / 120 | query path / ingest path (split: different call shapes) |
| `embed_mode` | `vllm` (normalized lower/strip) | lifespan fail-fast, embedder dispatch, ingest |
| `embed_base_url` / `embed_model` / `dense_dim` / `embed_api_key` | unset (endpoint trio required in vLLM; key unset = keyless) | endpoint+model validation, healthz ping, dim check, `Authorization` on embed calls |
| `embed_timeout_s` | 60.0 | both HTTP pools |
| `dense_query_prefix` | asymmetric instruct prefix | dense query vectors only |
| `prompt_max_context_chars` / `_complex` | 8000 / 4500 | `build_messages` by complexity |
| `prompt_max_chunk_chars` / `prompt_max_chunk_chars_complex` | 3000 / 1100 | per-type packing caps |
| `query_max_chars` / `splunk_context_max_chars` | 2000 (422s) / 4000 (truncate+suffix) | length guard (search/answer, chat latest turn) / prompt packing |
| `chat_condense_enabled` | `false` | follow-up condensation gate (`resolve_search_query`) |
| `chat_max_body_chars` | 32768 (422) | `/v1/chat` + `/ui` body cap (shared `chat_body_chars` helper) |
| `chat_max_turns` / `chat_max_prior_turn_chars` | 10 / 1000 | chat history packing caps |
| `prompt_order` | `retrieval` (`stable_cache` alt) | block ordering |
| `llm_base_url` / `llm_model_reasoning` / `llm_api_key` | unset (answer stays disabled; key unset = keyless) | per-request assertion, LLM client, tokenizer |
| `answer_timeout_s` | 300.0 | reasoning client; no transport retries, explicit fallback policy below |
| `llm_reasoning_effort_simple` / `_complex` / `llm_temperature` | low / high / 0.2 | answer path |
| `llm_max_model_len` / `llm_reserved_output_tokens` / `llm_thinking_reserve_tokens_complex` / `llm_token_safety_margin` / `llm_max_chunk_tokens_narrative` / `llm_tokenize_timeout_s` | 4096 / 1536 / 1000 / 128 / 350 / 5.0 | tokenizer-path budgeting (complex prompt budget prices high-effort thinking, issue #298) |
| `llm_stream` | `false` | server-side reasoning SSE |
| `http_connect_retries` / `http_max_connections` / `http_max_keepalive_connections` | 2 (connect-only) / 200 / 100 | both pools, embed/context clients |
| `health_qdrant_timeout_s` / `health_embed_timeout_s` | 5.0 / 10.0 | healthz only |
| `representation_cache_ttl_s` | 5.0 (0 = validate every request) | serving generation gate: alias resolution + contract validation cache |
| `allow_hash_mode` / `log_level` | `false` / INFO | lifespan hash gate / logging |
| `otel_exporter_otlp_endpoint` / `otel_sample_ratio` / `otel_export_queue_size` / `otel_export_timeout_ms` | unset = tracing off / 1.0 / 2048 / 5000 | tracing setup |
| `metrics_enabled` | `false` = /metrics 404s | Prometheus exposition for UWM scrapes |
| `ui_enabled` | `false` (fail-closed 404) | webui router gate (`/ui*`); the production chart sets it true |
| `rerank_enabled` / `rerank_model` / `rerank_base_url` / `rerank_api_key` / `rerank_endpoint_order` / `rerank_fusion_alpha` / `rerank_candidates` / `rerank_batch_size` / `rerank_timeout_s` | false / bge-reranker-v2-m3 / embed URL / unset (keyless) / `score_first` (`rerank_first` for gateways) / 1.0 / 50 / 32 / 5.0 | rerank dispatch → retrieve (see `retrieval.md` §6) |
| `rrf_k` / `rrf_weight_*` / `rrf_sparse_boost_syntax` / `rrf_sparse_boost_table` / `retrieve_max_chunks_per_page|doc` | 2 / 1.0,1.0 – 1.0,3.0 / 1.0 / 1.0 / 1, 3 | retrieve fusion + diversification |
| `acronym_expansion_enabled` / `comparative_split_enabled` / `diagnostic_dualpath_enabled` | `false` / `true` / `false` | rewrite + multipath (see `retrieval.md` §§3b,7) |
| ingest-only (`ingest_workers` = CPU-1, `batch_size` 128, `ingest_upsert_streams` 4, `ingest_bulk_load` false, `bm25_model`, `bm25_cache_dir` unset, `contextual_*` incl. `context_llm_timeout_s` 30.0 / `context_max_chars` 500 / `context_cache_path` unset) | — | ingest; see `docs/ingest.md` §§6–9 |
| `zowe_mcp_enabled` / `zowe_mcp_base_url` / `zowe_mcp_timeout_s` / `zowe_mcp_max_bytes` / `zowe_mcp_dry_run` | `false` (client not constructed unless enabled) / unset / 15.0 / 262144 / `false` | live-state client (default off; prompt/deployment integration remains incomplete; see `architecture.md`) |

## 8. Log and trace contract

Logs are one JSON object per line via `configure_logging`: `search` logs
query kind, hits, stage timings, elapsed; `answer` adds complexity, LLM
timings, citation counts, script presence, finish reason, the finalized
`verification_state`, token usage
(+ stream/TTFT marks on SSE); `chat` uses actions `chat`, `chat_retrieval`,
`chat_answer`, `chat_stream`, and the answer log also carries the citation
WHY telemetry (`inline_bracket_present`, `citations_header_present`,
`cites_rejected_shape_bad`, `cites_rejected_unmapped`) and the supplied
`evidence` count (issue #364 — counts only; the gap to `hits` is packing
omission). Errors log `str(exc)[:200]` server-side only. **Never query text,
PDF/manual text, or secrets** — the JSON-log rule is unchanged by tracing.

Spans mirror the log contract (one request = one trace: a
`v1.search`/`v1.answer`/`v1.chat`/`ui.chat` root → (`chat.condense` before
retrieval on an eligible chat follow-up) → retrieve
embed/prefetch/RRF/rerank/diversify → prompt build → LLM chat with
model/effort/TTFT/finish/tokens). The bounded query text is the one
allowed free-text span attribute; PDF text and secrets never enter spans.
The OTel API/SDK/exporter dependency pins stay version-locked. Spawn parse workers
stay untraced; parent stages own their records/spans. Outbound model headers carry
W3C traceparent via `bearer_auth_headers` when tracing is enabled.
Tracing ships default-off (endpoint unset → no exporter, no network),
export is fail-open and bounded (collector outages log and drop, never fail
a request), and setup must register the tracer provider — otherwise
import-time proxy tracers silently no-op. Non-`stop` finish reasons raise
an `answer_alert` log (no counters — multi-worker unsafe) that the L2
harness joins by `request_id`.

<a id="serving-contract"></a>
## Serving generation and reader lifetime

**Status: partially implemented.** **Authority:** #391 F3/F4, implemented in
PR #396; lifetime acceptance remains #391. **Decision owner:**
`agent.serving.ServingGate` and `representation.resolve_serving_generation`.
Static inspection: `9fece72df92ca5da414bc8f5b05cb2f89fcd18c8`, 15 September 2026.

**Inputs/producers/state/consumers:** operator settings plus writer-owned alias,
physical data and `<physical>__completions` → process-local TTL validation →
search, answer, both chat routes and console. `/healthz` requests fresh validation;
`/livez` proves only process liveness. The serving credential is read-only.

**Allowed states:** compatible/record-only drift may serve; empty is bootstrap
readiness only; drift, legacy, pending and unknown refuse with the stable
`representation_unavailable` envelope before retrieval/model/stream work.
The gate binds a request to a validated physical name, and aliases become visible
after revalidation. Negative results are cached too. See
[metadata outcomes](ingest.md#metadata-contract) for distinctions the reader
currently collapses and publication checks that remain incomplete.

**Required assumptions and limits:** a physical name is not an immutable snapshot.
Caching is safe with respect to alias movement only while the validated physical
data/metadata remain stable for the entire request. In-place writes, a forced
same-representation repair, an admin mutation or deletion can invalidate that
assumption during a warm cache and after a request has obtained its target.
TTL expiry, `invalidate()`, a pending marker and a fresh readiness probe do not
cancel/drain active readers. There is no reader lease or writer fence here.
Repair therefore needs operator quiescence/draining; a same-representation repair
must not be described as atomic. Preserve old physical data plus metadata for
rollback and keep settings compatible; do not GC targets still in use.

**Evidence:** `tests/test_serving_gate.py::test_resolve_binds_physical_and_reads_its_own_metadata`,
`test_resolve_refuses_physical_drift_even_when_alias_metadata_is_compatible`,
`test_gate_caches_within_ttl_and_revalidates`, plus endpoint refusal tests in
`tests/test_agent_api.py`, `tests/test_chat_api.py`, `tests/test_webui.py`.
These pin binding/refusal/cache behavior, not immutability or absence of concurrent
mutation. Active-reader repair and warm-cache mutation remain #391 acceptance
counterexamples; [publication](ingest.md#publication-contract) owns writer ordering.

<a id="answer-contract"></a>
## Supplied evidence and answer states

**Status: partially implemented.** **Authority:** #364 (supplied-evidence eligibility),
#365 (answer verification and provisional/incomplete states), #368 (prompt evidence
preservation and token-budget enforcement), #372 (browser execution, persistence,
and presentation of those states). These have separate acceptance
owners; completing one does not close the others. **Decision owners:**
`answer.build_messages` / `build_chat_messages` / `parse_answer`, `answer_core`,
`sse`, `webui.routes` and `webui/static/js/console.js`.

**Inputs and transitions:** retrieved hits → packed excerpts and `PromptEvidence`
→ reasoning output → citation normalization/eligibility → final response. Earlier
chat turns and omitted hits are not new supplied evidence. During streaming,
tokens are provisional; terminal events and error states determine completion.

| Guarantee | What establishes it / what it does not establish |
|---|---|
| Supplied evidence | Manifest records excerpts surviving packing and trim; retrieval membership alone is insufficient |
| Citation eligibility | Exact allowed cite or mapped bracket index from supplied evidence; not entailment of a claim |
| Claim support | Requires the claim to follow from retained source content; valid citation shape/allowlist membership does not prove it (#365) |
| Provisional output | Token deltas may precede validation or failure; never present them as a completed verified answer (#365; browser presentation #372) |
| Completed answer | Endpoint-specific successful terminal state, not EOF or `[DONE]` alone: an explicit non-null terminal finish and no upstream error/malformed frame are required; chat error frames followed by `[DONE]` still fail |

Verification states (issue #365, computed once in `answer_core`
from the finalized parse plus the transport outcome — one rule,
`answer.verification_state_for`, so JSON, SSE, chat, and console agree):

| State | Meaning | Never means |
|---|---|---|
| `accepted` | Eligible citations present and generation finished (`stop`) | Semantic proof of any claim |
| `insufficient_evidence` | Abstention-shaped refusal or empty-hits short-circuit | A failed request (still 200 + explicit text) |
| `unverified_draft` | Fluent non-abstention answer with zero eligible citations (absent, rejected, or inferred-only) | An error (still 200 — the draft label is the signal) |
| `generation_incomplete` | `length` finish, empty generation after fallbacks, absent/`null` terminal finish, upstream `error` frame, malformed frame, or stream error/cancel/disconnect | An accepted answer (terminal wire shape may still be complete) |

Carriage: `verification_state` rides `AnswerResponse`, the answer SSE
`final` (both paths), chat JSON top-level and the terminal chunk `extra`,
and console turns (badge, history persistence, export). Mid-stream failures
carry it too (issue #365): the answer SSE `error` event gains a hardcoded
`verification_state: "generation_incomplete"`, and chat error frames keep the
strict OpenAI `error` object with an additive sibling top-level
`verification_state` (standard clients ignore unknown keys). A client
disconnect or cancellation before any terminal frame cannot receive a frame
at all: the server records `answer_alert client_disconnect` plus a
`client_disconnect` RED outcome and marks the span `rag.stream_aborted`.
Non-streaming failures keep their existing error envelopes and carry no
answer state: no partial answer was ever emitted, so the transport error IS
the outcome. Streamed tokens cannot be retracted — a rejection after tokens
were rendered cannot un-display them; only the state badge and the
history/export labels correct the record, which is why failed partials
persist as `generation_incomplete` in the console rather than reading as
accepted.
`script_review_required`
is true whenever a script fence was extracted — scripts pass through
unvalidated on every surface, so a surfaced script is a human-review draft,
never certified-executable guidance; chat surfaces `script`/`script_lang`
with the same flag. Defaults stay closed: direct constructions read
`unverified_draft`, never `accepted`. The answer log carries the finalized
label for eval joins; the eval splits `false_refusal_rate` (answer-tier
explicit refusals) from `unsafe_answer_rate` (answered abstain-tier traps)
on dev synthetic sets, with the verification-state histogram per row.
Real-corpus acceptance with expert adjudication stays RC-owned.

Scripts extracted from fences pass through unvalidated; a script is not proven
correct because the answer contains an eligible citation. Inferred bracket-only
citations retain provenance on API/chat/console and never count as grounding in
the answer eval/L2. Abstention uses the shared marker-plus-shape predicate;
parse-time citation WHY telemetry cannot be reconstructed from the stripped body.

Prompt evidence preservation and token-budget enforcement (issue #368,
implemented): per-chunk caps, total-context remainder cuts, and tokenizer
verification trims all snap to whole atomic units — code statements, table
rows, SYSIN records — or omit the excerpt with explicit omission metadata;
a partial statement is never presented as a complete excerpt. Unit spans
persist on structured chunk payloads (`units`, additive and unindexed;
prose payloads are byte-identical to before) and legacy points redetect
with the same chunk splitters, so no second prompt-side parser exists.
Ordinary narrative keeps character truncation with the generic suffix; only
procedural/atomic content is omit-or-whole. The manifest records
`units_total`/`units_retained` per entry plus `omitted_indices`, and wholly
omitted chunks stay outside the citation allowlist. After trimming, the
final messages are confirmed against the real tokenizer/template budget
(fixed system/history/context/question text plus output/thinking reserves);
estimator-only and char-packing paths report `budget_verified: false`
(estimated, never confirmed; the offline/caller-provided `tokenizer=None`
char-packing path is estimated-only with no token budget claim, while production
serving paths always supply a tokenizer). On tokenizer-backed paths, fixed
content alone over the window raises before generation (`422 prompt_budget_exceeded`,
§2). The answer log carries `budget_verified` and `units_omitted` for eval joins;
RC-side completeness/support/truncation/latency measurement stays RC-owned (#367).

**Mutation/lifetime:** the per-answer supplied manifest must follow every trim;
it is independent of cached generation validation. Console/browser state remains
browser-only under ADR-0004, with strict CSP and vendored assets; UI gating must
cover all routes. **Evidence:** `tests/test_prompt_order.py`,
`tests/test_answer_core.py`, `tests/test_agent_api.py`, `tests/test_chat_api.py`,
`tests/test_stream_truncation.py`, `tests/test_webui.py`,
`tests/test_prompt_packing_units.py`, `tests/test_evidence_manifest.py`. Inspect their actual
assertions: eligibility and transport tests do not prove semantic support or all
browser completion behavior. #365/#372 retain those gaps; no model run is claimed
by this documentation audit.

<a id="http-model-contract"></a>
## HTTP/model fallback and lifecycle policy

**Status:** implemented call shapes with tests; semantic answer/completion limits
are above. **Authority:** ADR-0001/0004, #363 and existing transport contracts;
#397 corrects the overly broad old “never retry” summary. **Decision owners:**
`HttpxLLMClient`, `VllmTokenizer`, `HttpReranker`, `http_client` and lifespan.

| Operation | Current permitted recovery / failure boundary |
|---|---|
| Reasoning transport | Sync/async reasoning pools use `retries=0`; no connection-level automatic repeat |
| Buffered `achat` / `_chat_sync` with `LLM_STREAM` | Empty content, caught stream/protocol failures, missing `[DONE]`, upstream error/malformed frame, or missing explicit finish lead to one non-streaming POST; accumulated content is discarded, even if a prefix was buffered. The POST must itself carry an explicit finish, have no top-level error, and have valid string content (issue #365) |
| Client-visible `chat_stream` | No-content or pre-output stream failure (malformed first frame, pre-output error, missing finish/done before any emitted token) can make one non-streaming ask; malformed/rejected/empty/no-finish fallback fails. Missing `[DONE]`, an upstream error frame, a malformed frame, or `[DONE]` without an explicit finish after actually emitted content raises truncation; no replay after those tokens |
| Tokenizer | Plan locally, verify whole messages per trim round; first RPC failure warns and pins estimator for that instance. No per-chunk RPCs; gateway may lack `/tokenize` |
| Embed/context/health pools | Bounded Settings connect-only retries, no generic POST replay policy |
| Rerank | Configured score/rerank endpoint order plus alternate endpoint fallback; exhaustion fails closed; [retrieval](retrieval.md) owns dispatch |
| Condensation | Optional reasoning call; failure returns the raw latest query, not a fabricated condensed result |
| Tracing export | Separately bounded fail-open export; outages drop/log and must not fail request/shutdown |

This policy describes current operations, not permission to add retries. A new
fallback/timeout is a behavior change, with bounded Settings and evidence.
Settings for different call shapes stay separate; add default assertions to
`tests/test_config.py`. Every client opened by lifespan closes there; closing
must not cause later calls to rebuild a pool. Sync embed/rerank/tokenizer calls
run off the event loop. Runtime dispatch must not sniff monkeypatched attributes;
construct production clients explicitly and use documented awaitable seams.
Do not read request state that nothing sets or keep handlers nothing can raise.
Every handler/error shape needs a reachable test; catch narrowly around the
smallest call, preserve stable 404/405/500 contracts, and never leak internals.

**Evidence:** `tests/test_stream_truncation.py`, `tests/test_vllm_server_contract.py`,
`tests/test_agent_api.py`, `tests/test_chat_api.py`, `tests/test_mock_vllm.py`,
`tests/test_probe_gateway.py`, `tests/test_failfast.py`.
The approved local/CI gateway-only LiteLLM adapter exception is owned by
[deployment policy](deploy.md#deployment-policy); production gateway protection
remains the platform team's responsibility.
