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
  answer, citations, citations_inferred, inferred_indices, script}`.
  Retrieval always runs
  with a hardcoded `limit=8` (tuning the search `limit` does not change
  answers); the JSON response deliberately omits `query_kind`, `hits`,
  `usage`, `finish_reason`, and `ttft` (those live on spans, logs, and the
  SSE `final` event). `citations_inferred` is the provenance flag (issue
  #269): true when every returned cite was mapped from bare bracket markers
  with no explicit citation line — the eval never counts those as grounded.
  `inferred_indices` (issue #299) lists the 1-based prompt excerpt indices
  those markers pointed at (empty on every other path), so right-doc /
  wrong-index is measurable.
- `GET /healthz` — `HealthzResponse{status, qdrant, embed?}`. Qdrant is
  checked by GET-ting the pooled client's `{base}/readyz` and requiring
  exactly `200` plus the body `all shards are ready` (case/space
  normalized); the upstream body goes to the log, never the client.
  `embed` is tri-state: `None` when no embedder is configured, `false` on
  any exception or non-200 from a `["ping"]` embeddings probe, else the
  boolean result. `status` is `ok` only when Qdrant is ok and embed is not
  `False`, else `degraded` (still HTTP 200). Any Qdrant exception becomes
  `503 qdrant_unready`.
- `POST /v1/chat` (native) and `POST /v1/chat/completions` (OpenAI-compatible
  alias) — `ChatRequest{messages (client-managed history; min 1, roles
  `system`/`user`/`assistant`, `extra="forbid"`), product?, version?,
  splunk_context?, stream?, temperature?, model?, max_tokens?}`. At least one
  `user`-role message is required (`422 invalid_request` / `at least one user
  message is required`); the latest user turn is stripped and length-guarded
  by the shared `query_max_chars` rule, and the whole body is capped by
  `chat_max_body_chars` (the same helper as `/ui`). `temperature` overrides
  `Settings.llm_temperature`; `model` is accepted for OpenAI compatibility but
  never routed — inference always uses `Settings.llm_model_reasoning`, and a
  supplied value is echoed in the response `model` field; `max_tokens` is
  accepted and ignored (token limits are server-side). The response is
  `ChatCompletionsResponse{id: "chatcmpl-<request_id>", created, model,
  choices[], usage, citations, citations_inferred, inferred_indices, hits}`;
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
  decision live in `docs/eval.md` (`make eval-chat`).
- `GET /ui` — operator console (ADR-0004), a thin adapter over the same core:
  Jinja2 shell + HTMX form/fragment + SSE stream (`/ui/chat`, `/ui/chat/stream`,
  `/ui/healthz`, `/ui/static/*`), browser-only `localStorage` state, strict CSP.
  Every `/ui` path serves the stable `404 not_found` envelope while
  `UI_ENABLED` is false; the oauth-proxy sidecar authenticates external ingress
  when `AGENT_ROUTE=true` (ADR-0004 §3). The production overlay sets
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

Every JSON client body is `ErrorEnvelope{code, message}` with a fixed
message — no exception text, no upstream bodies, no internals, on any
status (`/ui` failures render HTML banners instead, §1):

| Code | Status | Trigger |
|---|---|---|
| `upstream_error` / `retrieval failed` | 502 | Any retrieval fault, identical on both endpoints |
| `upstream_error` / `answer failed` | 502 | LLM call or response-parse fault |
| `internal` / `internal error` | 500 | Prompt-build failure and any unhandled exception |
| `not_configured` / `reasoning model…` | 503 | `/v1/answer` or `/v1/chat` without `LLM_BASE_URL` + reasoning model (pre-retrieval) |
| `qdrant_unready` / `qdrant…` | 503 | `/healthz` Qdrant exception |
| `invalid_request` / `request body failed validation` | 422 | Pydantic failure and the shared query-length guard (one helper, same code, both endpoints) |
| `invalid_request` / `at least one user message is required` | 422 | `/v1/chat` + `/v1/chat/completions` with no `user`-role message |
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
list, optional script,
retrieval hits, query kind, `ttft_ms`, and token usage. A mid-stream failure
emits `event: error` and ends **without** a `final` — clients must treat
stream-end-without-final as a failed request. `/ui/chat/stream` consumes the
same token→final contract with a UI-specific terminal event name.

`/v1/chat` + `/v1/chat/completions` with `stream=true` instead stream OpenAI
`chat.completion.chunk` frames (`data: {...}` content deltas), then one
terminal chunk with `finish_reason` carrying `citations`,
`citations_inferred`, `inferred_indices`, and `hits` (chat chunks carry no
`usage`), then `data: [DONE]`. A mid-stream failure emits one
`data: {"error": {"code": "upstream_error", "message": "stream failed"}}`
frame **followed by** `data: [DONE]` — never a fake `finish_reason`; clients
must treat an error frame as failure even though `[DONE]` still arrives. The
strict stream-end rule above is the upstream reasoning wire and the
`/v1/answer` / `/ui` contract, not the OpenAI-compatible chat contract.

- The `final` schema is identical on the empty-hits path: zero citations,
  `citations_inferred: false`, empty `inferred_indices`, `ttft_ms: null`, zeroed usage. The empty-hits
  short-circuit happens before prompt build and any LLM call, on both JSON
  and SSE.
- `Server-Timing` on SSE responses carries the retrieval legs only;
  `llm`/`ttft` timings ride the `final` event (JSON responses carry all of
  them as headers).
- Streams must end with `[DONE]`: ending without it is
  `TruncatedStreamError`, never `finish_reason: stop`. Before the first
  content token the client falls back to a single non-streaming POST
  (discarding the prefix); after tokens arrived the error surfaces and no
  `final` follows. A `length` finish *with* `[DONE]` is complete, not
  truncated.
- `LLM_STREAM` (default off) routes every server-side reasoning call over
  the streaming wire and measures TTFT on the first content token; the JSON
  paths still return one answer. `make run-agent` and `make local-stack` set
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
- `splunk_context` truncates at 4000 chars with a suffix before packing, so
  caller context can never starve excerpts.
- The system prompt's seven rules (ground-only, synthesize-from-templates,
  version-disagree attribution, admit-gap, fenced scripts as examples,
  identify-a-doc-number/message-id/short-name query instead of refusing,
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
3. Bracket fallback on raw content (only when the passes above found no citation): `[n]` / `[n, m]` only, bounds-checked
   against retrieved hits, deduped, flagged inferred. **Parentheses are
   never inferred** — IBM-manual noise like `z/OS (3.1)` stays body text.

- One shared normalizer peels list markers, `>` quotes, paired
  punctuation, ``[x](url)`` links, `<angle>` wraps, `(parens)` groups, and
  `[1]:`-style numeric prefixes (up to 6 rounds) on both paths — two
  regexes for one concept would diverge.
- Validation is exact-match + dedupe against the retrieved pool; standalone
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
  only second ask is the documented `LLM_STREAM` fallback: a stream that
  truncates before any content is re-issued once as a non-streaming POST
  (§3). Dispatch picks async
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
| `answer_timeout_s` | 300.0 | reasoning client, never retried |
| `llm_reasoning_effort_simple` / `_complex` / `llm_temperature` | low / high / 0.2 | answer path |
| `llm_max_model_len` / `llm_reserved_output_tokens` / `llm_thinking_reserve_tokens_complex` / `llm_token_safety_margin` / `llm_max_chunk_tokens_narrative` / `llm_tokenize_timeout_s` | 4096 / 1536 / 1000 / 128 / 350 / 5.0 | tokenizer-path budgeting (complex prompt budget prices high-effort thinking, issue #298) |
| `llm_stream` | `false` | server-side reasoning SSE |
| `http_connect_retries` / `http_max_connections` / `http_max_keepalive_connections` | 2 (connect-only) / 200 / 100 | both pools, embed/context clients |
| `health_qdrant_timeout_s` / `health_embed_timeout_s` | 5.0 / 10.0 | healthz only |
| `allow_hash_mode` / `log_level` | `false` / INFO | lifespan hash gate / logging |
| `otel_exporter_otlp_endpoint` / `otel_sample_ratio` / `otel_export_queue_size` / `otel_export_timeout_ms` | unset = tracing off / 1.0 / 2048 / 5000 | tracing setup |
| `metrics_enabled` | `false` = /metrics 404s | Prometheus exposition for UWM scrapes |
| `ui_enabled` | `false` (fail-closed 404) | webui router gate (`/ui*`); the prod overlay sets it true |
| `rerank_enabled` / `rerank_model` / `rerank_base_url` / `rerank_api_key` / `rerank_endpoint_order` / `rerank_fusion_alpha` / `rerank_candidates` / `rerank_batch_size` / `rerank_timeout_s` | false / bge-reranker-v2-m3 / embed URL / unset (keyless) / `score_first` (`rerank_first` for gateways) / 1.0 / 50 / 32 / 5.0 | rerank dispatch → retrieve (see `retrieval.md` §6) |
| `rrf_k` / `rrf_weight_*` / `rrf_sparse_boost_syntax` / `rrf_sparse_boost_table` / `retrieve_max_chunks_per_page|doc` | 2 / 1.0,1.0 – 1.0,3.0 / 1.0 / 1.0 / 1, 3 | retrieve fusion + diversification |
| `acronym_expansion_enabled` / `comparative_split_enabled` / `diagnostic_dualpath_enabled` | `false` / `true` / `false` | rewrite + multipath (see `retrieval.md` §§3b,7) |
| ingest-only (`ingest_workers` = CPU-1, `batch_size` 128, `ingest_upsert_streams` 4, `ingest_bulk_load` false, `bm25_model`, `bm25_cache_dir` unset, `contextual_*` incl. `context_llm_timeout_s` 30.0 / `context_max_chars` 500 / `context_cache_path` unset) | — | ingest; see `docs/ingest.md` §§6–9 |
| `zowe_mcp_enabled` / `zowe_mcp_base_url` / `zowe_mcp_timeout_s` / `zowe_mcp_max_bytes` / `zowe_mcp_dry_run` | `false` (client unbuilt) / unset / 15.0 / 262144 / `false` | live-state client (default off; see `architecture.md`) |

## 8. Log and trace contract

Logs are one JSON object per line via `configure_logging`: `search` logs
query kind, hits, stage timings, elapsed; `answer` adds complexity, LLM
timings, citation counts, script presence, finish reason, token usage
(+ stream/TTFT marks on SSE); `chat` uses actions `chat`, `chat_retrieval`,
`chat_answer`, `chat_stream`, and the answer log also carries the citation
WHY telemetry (`inline_bracket_present`, `citations_header_present`,
`cites_rejected_shape_bad`, `cites_rejected_unmapped`). Errors log
`str(exc)[:200]` server-side only. **Never query text, PDF/manual text, or
secrets** — the JSON-log rule is unchanged by tracing.

Spans mirror the log contract (one request = one trace: a
`v1.search`/`v1.answer`/`v1.chat`/`ui.chat` root → (`chat.condense` before
retrieval on an eligible chat follow-up) → retrieve
embed/prefetch/RRF/rerank/diversify → prompt build → LLM chat with
model/effort/TTFT/finish/tokens). The bounded query text is the one
allowed free-text span attribute; PDF text and secrets never enter spans.
Tracing ships default-off (endpoint unset → no exporter, no network),
export is fail-open and bounded (collector outages log and drop, never fail
a request), and setup must register the tracer provider — otherwise
import-time proxy tracers silently no-op. Non-`stop` finish reasons raise
an `answer_alert` log (no counters — multi-worker unsafe) that the L2
harness joins by `request_id`.
