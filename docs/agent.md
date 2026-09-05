# Agent HTTP and reasoning reference

Owner: this file. Design overview: `docs/architecture.md` §§4.4–4.5. Day-2
operations: `docs/install_and_ops.md` §5. Retrieval contracts:
`docs/retrieval.md`.

> One fact, one owner — this file owns agent internals. Code is named by
> module and function, never by line number.

## 1. Endpoints

Three async routes in `agent/app.py`. Every request gets a 12-hex-char
`request_id` from middleware, shared by all logs, the unhandled-error
handler, and the response.

- `POST /v1/search` — `SearchRequest{query (min 1 char), product?,
  version?, limit (default 8, 1–40)}` → `SearchResponse{request_id,
  query_kind, hits}`. No LLM involved.
- `POST /v1/answer` — `AnswerRequest{query, product?, version?,
  splunk_context?, stream (default false)}` → `AnswerResponse{request_id,
  answer, citations, script}`. Retrieval always runs with a hardcoded
  `limit=8` (tuning the search `limit` does not change answers); the JSON
  response deliberately omits `query_kind`, `hits`, `usage`,
  `finish_reason`, and `ttft` (those live on spans, logs, and the SSE
  `final` event).
- `GET /healthz` — `HealthzResponse{status, qdrant, embed?}`. Qdrant is
  checked by GET-ting the pooled client's `{base}/readyz` and requiring
  exactly `200` plus the body `all shards are ready` (case/space
  normalized); the upstream body goes to the log, never the client.
  `embed` is tri-state: `None` when no embedder is configured, `false` on
  any exception or non-200 from a `["ping"]` embeddings probe, else the
  boolean result. `status` is `ok` only when Qdrant is ok and embed is not
  `False`, else `degraded` (still HTTP 200). Any Qdrant exception becomes
  `503 qdrant_unready`.

Per-endpoint flow: `search` runs the length guard, then retrieval under a
root span — faults become `502 upstream_error / retrieval failed`, timings
become `Server-Timing`, then the response. `answer` resolves streaming
first (`?stream=` wins over the body field when set), runs the length
guard, asserts the reasoning model is configured (`503 not_configured`,
before any retrieval), retrieves under the root span (same `502` as
search), short-circuits empty hits (§3), classifies complexity, builds the
prompt, and either chats once (JSON) or streams (SSE).

## 2. Error contract

Every client body is `ErrorEnvelope{code, message}` with a fixed message —
no exception text, no upstream bodies, no internals, on any status:

| Code | Status | Trigger |
|---|---|---|
| `upstream_error` / `retrieval failed` | 502 | Any retrieval fault, identical on both endpoints |
| `upstream_error` / `answer failed` | 502 | LLM call or response-parse fault |
| `internal` / `internal error` | 500 | Prompt-build failure and any unhandled exception |
| `not_configured` / `reasoning model…` | 503 | `/v1/answer` without `LLM_BASE_URL` + reasoning model (pre-retrieval) |
| `qdrant_unready` / `qdrant…` | 503 | `/healthz` Qdrant exception |
| `invalid_request` / `request body failed validation` | 422 | Pydantic failure and the shared query-length guard (one helper, same code, both endpoints) |
| `not_found` / `not found` | 404 | Unknown route |
| `method_not_allowed` / `method not allowed` | 405 | Wrong method |
| `http_error` / `request failed` | framework's | Framework-raised `HTTPException` (nothing in `src/` raises it; `detail` is stripped) |

Server side keeps `str(exc)[:200]` in logs. The 502/500 split is deliberate:
same fault → same code on every endpoint, and a model/parse fault is never
mislabeled as retrieval.

## 3. Streaming (SSE)

`GET/POST /v1/answer?stream=true` yields zero or more `event: token` deltas,
then exactly one terminal `event: final` carrying the full answer, validated
citations, optional script, retrieval hits, query kind, `ttft_ms`, and token
usage. A mid-stream failure emits `event: error` and ends **without** a
`final` — clients must treat stream-end-without-final as a failed request.

- The `final` schema is identical on the empty-hits path: zero citations,
  `ttft_ms: null`, zeroed usage. The empty-hits short-circuit happens before
  prompt build and any LLM call, on both JSON and SSE.
- `Server-Timing` on SSE responses carries the retrieval legs only;
  `llm`/`ttft` timings ride the `final` event (JSON responses carry all of
  them as headers).
- Streams must end with `[DONE]`: ending without it is
  `TruncatedStreamError`, never `finish_reason: stop`. Before the first
  content token the client falls back to a single non-streaming POST
  (discarding the prefix); after tokens arrived the error surfaces and no
  `final` follows. A `length` finish *with* `[DONE]` is complete, not
  truncated.
- Server-side reasoning SSE is gated by `LLM_STREAM` (default off;
  `make run-agent` enables it for TTFT measurement). TTFT is measured on
  the first content token. The SSE generator ends the root span in a
  `finally` so disconnects stay in the same trace.
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
- Blocks are named (`context` / `question` / `excerpt` / `tail`) and packed
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
  verifies the packed prompt **once** against the whole-message count and
  trims up to 4 rounds (64-char overcut, drop under 80 chars, else suffix).
  Never per-chunk tokenize RPCs.
- `splunk_context` truncates at 4000 chars with a suffix before packing, so
  caller context can never starve excerpts.
- The system prompt's six rules (ground-only, synthesize-from-templates,
  version-disagree attribution, admit-gap, fenced scripts as examples,
  mandatory `Citations:` with a few-shot example) plus the complex-query
  extension (decompose, cross-examine, verified fences,
  diagnose-and-recover headings) live in code by design — this file
  documents their shape, not their text.

## 5. Citation validation

Only retrieved cite strings reach the client, via two passes plus a
trailing sweep in `cites.py` / `parse_answer`:

1. Explicit block: from a `Citations:` header (any `#` depth, any case),
   consuming cite-shaped or bullet lines (after normalization) until the
   first blank past seen cites — later prose is preserved as body.
2. Trailing bare cites: a blank-tolerant tail scan for allowed cite lines
   without any header.
3. Bracket fallback on raw content: `[n]` / `[n, m]` only, bounds-checked
   against retrieved hits, deduped, flagged inferred. **Parentheses are
   never inferred** — IBM-manual noise like `z/OS (3.1)` stays body text.

- One shared normalizer peels list markers, `>` quotes, paired
  punctuation, `[x](url)` links, `<angle>` wraps, `(parens)` groups, and
  `[1]:`-style numeric prefixes (up to 6 rounds) on both paths — two
  regexes for one concept would diverge.
- Validation is exact-match + dedupe against the retrieved pool; standalone
  lines only — inline mentions (`refer to SA22-… for details`), dimensions
  (`3.5 inches`), and table pipes survive.
- Fenced code blocks in `SCRIPT_LANGS` (`jcl, rexx, sh, bash, shell,
  python, yaml, json, ops, rule, parmlib`, …) become `script` (joined,
  removed from body) and pass through **unvalidated** — cite-like lines
  inside code are kept; `thought`/`thinking` fences are dropped; other
  fences unwrap to body. Validating scripts would corrupt JCL/REXX;
  treating them as cited provenance would hallucinate it.

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
- The reasoning client never retries (sync and async, `retries=0`) —
  answers are non-idempotent; a retry would re-think. Dispatch picks async
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
| `qdrant_url` / `qdrant_api_key` / `qdrant_collection` | `http://localhost:6333` / unset / `mainframe_manuals` | lifespan, healthz readyZ, retrieve calls, manifests |
| `qdrant_timeout_s` / `qdrant_ingest_timeout_s` | 30 / 120 | query path / ingest path (split: different call shapes) |
| `embed_mode` | `vllm` (normalized lower/strip) | lifespan fail-fast, embedder dispatch, ingest |
| `embed_base_url` / `embed_model` / `dense_dim` | unset (all required in vLLM) | endpoint+model validation, healthz ping, dim check |
| `embed_timeout_s` | 60.0 | both HTTP pools |
| `dense_query_prefix` | asymmetric instruct prefix | dense query vectors only |
| `prompt_max_context_chars` / `_complex` | 8000 / 4500 | `build_messages` by complexity |
| `prompt_max_chunk_chars` / `prompt_max_chunk_chars_complex` | 3000 / 1100 | per-type packing caps |
| `query_max_chars` / `splunk_context_max_chars` | 2000 (422s) / 4000 (truncate+suffix) | length guard / prompt packing |
| `prompt_order` | `retrieval` (`stable_cache` alt) | block ordering |
| `llm_base_url` / `llm_model_reasoning` | unset (answer stays disabled) | per-request assertion, LLM client, tokenizer |
| `answer_timeout_s` | 300.0 | reasoning client, never retried |
| `llm_reasoning_effort_simple` / `_complex` / `llm_temperature` | low / high / 0.2 | answer path |
| `llm_max_model_len` / `llm_reserved_output_tokens` / `llm_token_safety_margin` / `llm_max_chunk_tokens_narrative` / `llm_tokenize_timeout_s` | 4096 / 1536 / 128 / 350 / 5.0 | tokenizer-path budgeting |
| `llm_stream` | `false` | server-side reasoning SSE |
| `http_connect_retries` / `http_max_connections` / `http_max_keepalive` | 2 (connect-only) / 200 / 100 | both pools, embed/context clients |
| `health_qdrant_timeout_s` / `health_embed_timeout_s` | 5.0 / 10.0 | healthz only |
| `allow_hash_mode` / `log_level` | `false` / INFO | lifespan hash gate / logging |
| `otel_exporter_otlp_endpoint` (+ sample/queue/timeout) | unset = tracing off | tracing setup |
| `rerank_enabled` / `rerank_model` / `rerank_base_url` / `rerank_candidates` / `rerank_batch_size` / `rerank_timeout_s` | false / bge-reranker-v2-m3 / embed URL / 50 / 32 / 5.0 | rerank dispatch → retrieve |
| `rrf_k` / `rrf_weight_*` / `retrieve_max_chunks_per_page|doc` | 2 / 1.0,1.0 – 1.0,3.0 / 1, 3 | retrieve fusion + diversification |
| `acronym_expansion_enabled` | `false` | rewrite (not agent) |
| ingest-only (`ingest_workers` = CPU-1, `batch_size` 128, `ingest_upsert_streams` 4, `ingest_bulk_load` false, `bm25_model`, `contextual_*`) | — | ingest; see `docs/ingest.md` §§6–9 |

## 8. Log and trace contract

Logs are one JSON object per line via `configure_logging`: `search` logs
query kind, hits, stage timings, elapsed; `answer` adds complexity, LLM
timings, citation counts, script presence, finish reason, token usage
(+ stream/TTFT marks on SSE). Errors log `str(exc)[:200]` server-side
only. **Never query text, PDF/manual text, or secrets** — the JSON-log
rule is unchanged by tracing.

Spans mirror the log contract (one request = one trace: route span →
retrieve embed/prefetch/RRF/rerank/diversify → prompt build → LLM chat
with model/effort/TTFT/finish/tokens). The bounded query text is the one
allowed free-text span attribute; PDF text and secrets never enter spans.
Tracing ships default-off (endpoint unset → no exporter, no network),
export is fail-open and bounded (collector outages log and drop, never fail
a request), and setup must register the tracer provider — otherwise
import-time proxy tracers silently no-op. Non-`stop` finish reasons raise
an `answer_alert` log (no counters — multi-worker unsafe) that the L2
harness joins by `request_id`.
