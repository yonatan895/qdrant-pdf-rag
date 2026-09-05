# Agent HTTP and reasoning reference

Owner: this file. Design overview: `docs/architecture.md` §§4.4–4.5. Day-2
operations: `docs/install_and_ops.md` §5.

> Skeleton for the docs epic (PR-A). Section headings below mark the full
> content that lands in PR-D. Each section documents contracts (request,
> response, error, event), budgets, and ordering rules with `file:line`
> evidence. One fact, one owner — do not duplicate `architecture.md`.

## 1. Endpoints

Scope: `/healthz`, `/v1/search`, `/v1/answer` — request models and limits
(answer hardcodes `limit=8`), response models and deliberate field omissions,
per-endpoint step → status-code map.

## 2. Error contract

Scope: full error table (`code`, `message`, status, trigger): `upstream_error`
(502 retrieval vs LLM), `internal` (500 prompt-build), `not_configured` (503),
`qdrant_unready` (503 healthz), `invalid_request` (422 incl. length guard),
`not_found`/`method_not_allowed`/`http_error`; fixed-message rule (no
internals in bodies, `str(exc)[:200]` server-side only).

## 3. Streaming (SSE)

Scope: `?stream=true` vs body precedence; `token` → exactly-one `final`
schema (incl. empty-hits parity); mid-stream `error` ends without `final`;
`[DONE]` discipline and `TruncatedStreamError` (recover-before-first-token
vs fail-after); `LLM_STREAM` default-off; `Server-Timing` vs `final`-event
placement of `llm`/`ttft`.

## 4. Prompt assembly and budgets

Scope: complexity classifier (roots, message-id short-circuit, default simple);
`max_context` 8000/4500 and the 4096-token ceiling rationale; per-type chunk
caps (3000 syntax/message/table, 1100 narrative); block model and
`prompt_order` policies (`retrieval` byte-identical history vs `stable_cache`
framing); prefix-cache static instructions; tokenizer verify-once path
(constants, trim rounds); `splunk_context` truncation.

## 5. Citation validation

Scope: `Citations:` block parse, normalization rounds, exact-match allowlist,
standalone-line rule, trailing bare cites, bracket-inference bounds,
parentheses-never rule, script-fence passthrough (unvalidated),
`empty_hits_answer` 5-term cap and no-fallback rule.

## 6. Lifespan, pools, timeouts

Scope: what opens/closes in lifespan (`http`, `http_sync`, `llm_client` local
vs `llm` global, shared pools with embedder/tokenizer/reranker); startup
fail-fast order (embed validated, LLM request-time); sync-off-event-loop rule
(`asyncio.to_thread` legs); reasoning-model-only client with zero retries;
healthz tri-state embed and exact `readyz` match.

## 7. Settings catalog

Scope: every `Settings` field — default, readers, and rationale. Sourced from
`config.py` + pinned by `tests/test_config.py`.

## 8. Log and trace contract

Scope: JSON log keys per action, never-log rule (no query/PDF text, no
secrets), span tree and attributes, bounded-query-text exception, OTel
opt-in/export-bounded behavior.
