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
handler, and the product-route responses (`/v1/*`, `/ui`; chat surfaces it
as `chatcmpl-<request_id>`). The ops endpoints carry none: `/healthz`,
`/livez`, and `/metrics` responses have no `request_id`.

- `POST /v1/search` — `SearchRequest{query (min 1 char, not blank or control-character, #579), product?,
  version?, limit (default 8, 1–40)}` → `SearchResponse{request_id,
  query_kind, hits}`. No LLM involved. Each hit additionally carries
  `reference` (issue #405): the opaque `ep1.` exact-evidence reference, or
  `null` when no exact read can be promised for it (generation without a build
  binding, or a stored payload that cannot form a complete envelope). Minting
  uses the build binding the serving gate validated and reads only the served
  hits; it is best-effort and read-only, and a fault there yields nulls,
  never a failed search.
- `GET /v1/sources?limit&cursor&product&version` — the served build's sources
  from its publish-time catalog (issue #828): `SourcesResponse{request_id,
  build_id, sources[{source_revision, source_sha256, doc_id, title, product,
  version, chunk_count, physical_pages{first,last} (one-based),
  page_labels{first,last} (null = no honest label), indexed_pages}],
  next_cursor}`. `limit` 1–200 (default 50); `next_cursor: null` is the only
  end signal, there is no total, and denied sources are omitted uncounted.
  The first page lists the build the serving gate validated; a cursor (`ls1.`)
  pins its build through the immutable per-build aliases, so alias movement
  cannot change a listing in flight. Catalog metadata only (one projected
  scroll of the control collection per page, no chunk text, no model call);
  builds published before #827 carry no catalog and answer 503. Contract:
  [evidence contract, Sources](evidence-contract.md#sources-profile).
- `GET /v1/evidence/{reference}?max_bytes&product&version` — exact stored
  evidence for one cited chunk (issue #405): `EvidenceResponse{request_id,
  reference, digest, completeness ("complete"), build_id, chunk_id,
  generation_fingerprint, source_revision, source_sha256, doc_id, title,
  product, version, heading, chunk_type, text, text_bytes, atomic_spans
  (UTF-8 byte ranges, null = not recorded), location{physical_page_start,
  physical_page_end, printed_label}}`. No LLM, embedding, rerank or search;
  read-only storage calls only; the build is pinned through its immutable
  per-build aliases, so alias movement cannot redirect it, and it does not go
  through the serving gate (an old retained build stays readable). A whole
  chunk or an explicit refusal, never a prefix. Contract, outcome mapping and
  limits: [stored-payload profile](evidence-contract.md#stored-payload-profile).
  The downstream MCP consumer is `python -m mainframe_rag.mcp.knowledge`
  (tools `knowledge_search`, `evidence_read`, `sources_list`) over this HTTP surface.
- `POST /v1/answer` — `AnswerRequest{query, product?, version?,
  splunk_context?, stream (default false), temperature?}` → `AnswerResponse{request_id,
  answer, citations, citations_inferred, inferred_indices, script,
  script_lang, verification_state, script_review_required, supplied_evidence,
  evidence_omitted}` (the last two: [supplied-evidence projection](#supplied-excerpts)).
  `temperature` (issue #596) is an optional per-request override of
  `Settings.llm_temperature` (0.0–2.0, no inf/NaN; out-of-range 422s before
  retrieval); omitted means the configured default. The chat routes accept
  the same bounded override.
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
  representation, rerank?}`. Qdrant is checked by GET-ting the pooled client's
  `{base}/readyz` and requiring exactly `200` plus the body `all shards are
  ready` (case/space normalized); the upstream body stays out of both logs
  and client bodies, never the client. `embed` is tri-state: `None` when no embedder is configured,
  `false` on any exception or non-200 from a `["ping"]` embeddings probe,
  else the boolean result. `representation` is the live, uncached
  `resolve_serving_generation` outcome (issues #391 F3/F4): the configured
  alias resolved to its physical generation and that generation's OWN
  `<physical>__completions` contract compared against the wanted one.
  `rerank` is tri-state (issue #578): `None` when `RERANK_ENABLED` is off
  (readiness unchanged), else whether the configured endpoint(s) answer the
  1x1 score probe (`probe_reranker`: the serving endpoint order and
  alternate-route fallback, so a working alternate keeps the leg up). The
  probe runs off the event loop, is bounded by `health_rerank_timeout_s`,
  and its outcome (up or down) is cached for `health_rerank_ttl_s`, so
  kubelet ticks do not become GPU requests; a timeout or any exception is
  `false`, logging only the error type. Recovery shows on the first probe
  after the TTL. Rerank exhaustion fails non-identifier search closed, so
  an enabled-but-down leg makes the pod unready even though identifier
  lookups (which bypass rerank) would still serve: degraded means
  unready by design. The reasoning leg is not probed: search never calls
  an LLM, so it must not gate readiness.
  `status` is `ok` only when Qdrant is ok, embed is not `False`,
  `representation` is `compatible`, `record_only_drift`, or `empty`, and
  `rerank` is not `False`; anything else is `degraded` **and HTTP 503** (Kubernetes probes judge
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
  by `query_max_chars` and, like `/v1/search` and `/v1/answer` queries, refused
  when it contains NUL or C0 control characters other than `\t\n\r` (#579; DEL
  and C1 are not rejected). `/v1/search` and `/v1/answer` inspect the query with
  the same predicate (`chat_turn.is_unsearchable_query`) but search it unmodified,
  so padding around real text is kept. Earlier user/assistant messages remain history; caller
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
  verification_state, script, script_lang, script_review_required,
  supplied_evidence, evidence_omitted}`;
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
  ClusterIP 8080 `/ui` stays reachable to in-cluster tools of the release
  namespace and of `AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES`; other
  namespaces are denied by the default-on ingress NetworkPolicy
  ([deploy.md](deploy.md#agent-networkpolicy)).
- `GET /metrics` — Prometheus text exposition, opt-in via `metrics_enabled`
  (disabled serves the stable `404 not_found` envelope; a scrape failure is
  `503 metrics_unavailable` / `metrics are not available`). No trace span by
  design (issue #187).

Endpoint telemetry (#375): `rag.requests.total` and `rag.request.duration`
record one outcome per request through the HTTP terminal owner. Enabled console
form and SSE turns use `endpoint="console"`; pages, assets, badges and disabled
console routes do not add request series. Successful JSON/SSE finals attach the
core's bounded `verification_state` (the four states below). Empty retrieval and
refusals remain `outcome="ok"`; this measures request completion, not semantic
truth. Stream errors and disconnects before a terminal frame attach
`generation_incomplete`, with an error or `client_disconnect` outcome. JSON
errors and pre-stream refusals have no finalized quality state. Recording occurs
before yielding the terminal frame; closing after that frame cannot omit or
repeat the outcome. It establishes server production, not delivery acknowledgement.
Unknown verification labels are dropped. No query, document or user labels are
introduced; model labels remain deployment configuration. Metrics failures remain
fail-open. `tests/test_metrics.py` checks emitted counters/durations and generator
closure across API and console transports. Data-generation/admission metrics,
SLO thresholds and the sanitized pilot diagnostic packet remain with #375/#374/#446.

Per-endpoint flow: each product route opens one SERVER root span first, so
length-guard rejections and serving refusals share the admitted trace
(issue #529 OBS-1B §4.3); outbound legs (embed, Qdrant prefetch, rerank,
LLM) are CLIENT children of it and local stages stay INTERNAL. Final logs
are emitted while the span is attached and it ends after them, so they
join by trace id; unsampled contexts emit no ids. `search` retrieves under
the root span — faults become `502 upstream_error / retrieval failed`,
timings become `Server-Timing`, then the response. `answer` resolves streaming
first (`?stream=` wins over the body field when set), runs the length
guard, asserts the reasoning model is configured (`503 not_configured`,
before any retrieval), retrieves under the root span (same `502` as
search), classifies complexity, short-circuits empty hits (§3), builds the
prompt, and either chats once (JSON) or streams (SSE). `chat` runs the same
assertion and retrieval (hardcoded `limit=8` for both endpoints), resolving
the follow-up search query through `resolve_search_query` first so the
condense gate cannot be honored on one path only. The SSE generators hold
the request root for each iterator operation. A single lifetime owner starts
before admission and ends buffered work in `finally`; a streaming response
transfers that ownership to the response. Exhaustion followed by the closing
ASGI body send, explicit close, cancelled dependency I/O, and failed ASGI
delivery (even before the iterator starts) close owned iterators and end the
root exactly once. Terminal
logs, including admission/retrieval/condense failures, generation alerts and
console errors, attach to that recording root before it ends. Concurrent turns
retain independent roots; disabled or non-recording spans add no correlation
IDs. Cancellation propagates without a success final or another model call.
Only operation cleanup is shielded from ASGI cancellation; model I/O remains
cancellable, and cleanup never closes the shared HTTP client. Pre-header
cancellation records `client_disconnect` without a finalized quality state;
stream cancellation before a produced terminal records `generation_incomplete`.
Both use the existing one-outcome guard. `tests/test_tracing.py` exercises
real-SDK lifecycle, correlation and overlapping requests; the native
`tests/live_agent_probes.py` verifies socket disconnect, upstream closure,
finished Jaeger roots, exact JSON log joins and the next ordinary request.

### Console browser contract: retention and accessibility (#372)

The console's conversation state is browser-owned (ADR-0004); this section is
the contract owner for what that state is, how long it lives and how it is
cleared. Executed by `tests/console_browser_contracts.py` (real Chrome, shipped
`console.js`, scripted gateway-shaped LLM; `pytest -m browser`, skips without
an offline Chrome for Testing + matching chromedriver via
`CONSOLE_BROWSER_CHROME`/`CONSOLE_BROWSER_CHROMEDRIVER` or the selenium-manager
cache; nothing is downloaded; CI: `browser.yml`). Hermetic structure pins live in
`tests/test_webui.py`.

Retention policy as shipped (no default changed by #372):

- **Where:** only `localStorage` key `mainframe_rag_sessions` (plus the
  cosmetic theme/reasoning keys). Turns include operator text, attached incident
  context (JES spool/SYSLOG) and assistant answers quoting manual excerpts. Never
  in cookies, `sessionStorage`, IndexedDB, or server logs (server logs carry
  request id, query kind, counts, error type only; both pinned by tests).
  With answer feedback enabled (#780), an assistant turn also keeps its server
  `request_id` and the operator's rating label. **The server also keeps, for
  every finalized console answer, the question and answer text** (with
  citations and telemetry) as a quality record in the protected feedback
  store until purged under its configured retention window
  (`FEEDBACK_RETENTION_DAYS`, default 180 days; an explicit purge,
  scheduled in-process by the feedback service, #804), and a sent rating (rating, reason codes, comment) joins
  it. Clearing the
  browser's incidents does not remove those records; retention and purge are
  the store's (ADR-0005). See [Answer feedback](#answer-feedback).
- **How long:** until the operator clears it, capped at 30 incidents (oldest
  idle incident evicted first; the incidents open in this tab are protected).
  There is no time-based expiry, no logout/user-switch clearing and no account
  isolation: the key is global per browser origin profile. A cache-control
  header does not change that.
- **How cleared:** per incident (delete) or all incidents (`Clear all saved
  incidents`, two-step confirm; other tabs follow through the `storage` event;
  an in-flight answer for an erased incident is discarded, never resurrected).
  Browser "clear site data" also clears everything.
- **Storage unavailable/full or shared writes unsupported:** the console keeps
  the conversation in memory for the tab and shows a persistent notice that it
  is lost on reload. Shared persistence requires Web Locks in a secure browser
  context (HTTPS or loopback); no unlocked localStorage write fallback is used.
- **Export:** operator-initiated Markdown download of the open incident; it
  leaves browser control and carries a handling banner. Incomplete turns export
  with their non-accepted verification state.
- **Open decisions (site owner, with #373):** time-based expiry, clearing on
  logout/user switch, and per-user keys require an authenticated identity the
  console does not have and a dedicated approved concern; they are not
  implemented here.

State contract: one origin-wide Web Lock encloses each persisted read/modify/write,
including rename, delete, new incident and clear-all. Reads used to render do
not write shared state. A separate incident lock is held from saving the user
turn through saving its assistant result. A second tab sending to that incident
gets a visible refusal and keeps its unsent question; it can send after the
first turn finishes. Other incidents remain usable. Completion reads the latest
store under the write lock, preserves renames and discards deleted incidents.
Barrier-controlled browser tests exercise both contenders observing the same
version before mutation and prove their locked writes serialize. A
streaming turn is labelled provisional until the final frame; failed, stopped or
EOF-without-final output is stored and restored as `generation_incomplete`, is
qualified when reused as model context, and an unanswered question is shown as
such after reload.

Accessibility behavior (executed, not a compliance claim): landmarks and
accessible names for every control (checked on Chrome's accessibility tree),
Tab reachability of every control with a visible focus ring, keyboard
send/Stop/rename with focus return, a polite status region announcing
generating/complete/incomplete/stopped/copy and export failure (the message list
is deliberately not a live region), WCAG AA text contrast in both themes, and no
horizontal scroll at 1280/640/375/320 CSS px (stand-ins for 100/200/400% zoom).
Not covered: other browsers, real screen readers, forced-colors/OS high
contrast, native browser zoom.

<a id="answer-feedback"></a>
### Answer feedback (issue #780)

Off unless `FEEDBACK_ENABLED` (which then requires `FEEDBACK_URL` and
`FEEDBACK_API_KEY`). When off, no feedback client exists, nothing is recorded,
no call is made, and the console offers no rating. Data boundaries and
retention: [observability](observability.md#data-tiers).

- **Records:** every finalized answer, including zero-evidence declines, is
  handed once to the single-writer feedback service. Delivery is at most once:
  never duplicated (the first record wins), never retried; a failed delivery
  is counted, not raised. The paths are: `/v1/answer` JSON (`answer`) and SSE
  (`answer_stream`), `/v1/chat` JSON (`chat`) and SSE (`chat_stream`), and both
  console paths (`console`). A record holds:
  - the request's own query (the latest user turn for chat and console);
  - the final answer, script and validated citations;
  - the citations of the excerpts actually supplied to the model;
  - a scalar telemetry snapshot: the finalized log fields plus the effort the
    answer was generated at, the model identities, the extraction-rules version
    and the release (`IMAGE_SHA`);
  - the agent's finalize time, from which retention counts.
- **Never on the critical path:** records go into a bounded in-memory queue
  (`FEEDBACK_QUEUE_SIZE`) drained by one background task. A full queue or a
  failed delivery is counted on `rag.feedback.records` and logged by error type
  only; the answer is unchanged. Shutdown drains for at most
  `FEEDBACK_DRAIN_TIMEOUT_S` and counts whatever is left as dropped.
- **Ratings:** `POST /ui/feedback {request_id, rating, reasons, comment}` from
  the streaming console, and `POST /ui/feedback/form` (form-encoded) from the
  fallback console, with the same validation and codes. An htmx request gets the
  result text for the form's status slot, always status 200 like the `/ui/chat`
  banner (htmx 1.9 never swaps a 4xx/5xx); the form stays, so a failed rating
  can be retried. A native post without JS opens in a new tab, so the console
  page and its conversation stay, and gets a full page with the real status.
  In the streaming console the rating box is re-derived from page-memory state
  (choice, reasons, comment, in-flight send, last error) whenever the articles
  are rebuilt, and focus and caret inside it are restored; nothing is stored.
  A send uses a snapshot and locks every control until it settles, so the
  "Rated" state shown and saved is exactly what the service recorded. (Rare
  case: if the browser's locked store write itself rejects, the label lasts
  only until the next rebuild, which shows the box unrated; the service still
  holds the rating, and a resend is recorded as a re-rating. A storage quota
  failure does not lose it: the console falls back to page memory.) The lock
  marks the controls `aria-disabled` (the comment read-only) and keeps them in
  place, so focus never drops; it is released after the store write, always.
  Redrawing a box keeps focus on the same control, moves it to the box when
  the box becomes "Rated", and never moves focus that is elsewhere.
  - `request_id` must be the 12-hex server id; extra fields, such as an answer
    text, are refused with 422.
  - The store attaches the rating to its own record and refuses unknown ids:
    404 `unknown_answer`. One rating per answer: a resend converges and
    re-rating replaces it, so retries and double submits are idempotent.
    Delivery is asynchronous and records can be dropped or purged, so the
    message ("not recorded, or not yet") promises neither a final refusal nor a
    successful retry. Only the service's `unknown_answer` 404 maps here; any
    other 404 (such as a wrong `FEEDBACK_URL` path) is `feedback_unavailable`
    and logged.
  - Reasons must be distinct.
  - Other fixed codes: 422 `invalid_request`, 503 `feedback_unavailable` (an
    unexpected service status is logged by status only), 404 `feedback_disabled`.
  - No rater identity is recorded or accepted: the service refuses a `rater`
    field (422). The agent port is reachable without the OAuth proxy (from the
    release namespace and any allowed namespace), so a
    forwarded-user header proves nothing; attribution needs proven header
    provenance from the deployment (PR B).
  - `rag.feedback.ratings` counts accepted submissions, re-ratings included;
    the store (latest rating per answer) is the authority on rated answers.
- **Store and service** (`mainframe_rag.feedback`): SQLite in WAL mode, owned
  by one process. The first record per `request_id` wins, so a rated answer is
  never rewritten. Configuration comes from `Settings`: `FEEDBACK_STORE_PATH`,
  `FEEDBACK_SERVICE_TOKEN` (the agents' `FEEDBACK_API_KEY` value; both sides
  strip whitespace) and `FEEDBACK_RETENTION_DAYS` (180, ADR-0005). Fixed client
  errors with stable codes (`unauthorized`, `invalid_request`, `unknown_answer`,
  `not_found`, `method_not_allowed`, `internal_error`); JSON-line logs with
  `request_id` and counts only. Retention is enforced by the service itself
  (ADR-0005, #804): one in-process task, started with the app, runs the purge
  `FEEDBACK_PURGE_FIRST_DELAY_S` (30 s) after start and then every
  `FEEDBACK_PURGE_INTERVAL_S` (24 h); the authenticated `POST /v1/purge` stays
  as the manual operator action and runs the same attempt. The store runs with
  `secure_delete` and the purge then truncates the WAL, so purged text leaves
  the disk. Only a result with `wal_truncated: true` is success. A concurrent
  read snapshot (an export) makes the truncation busy: the purge does not wait
  (writers would stall), returns `wal_truncated: false` (outcome `deferred`) and
  an exception such as `database is locked` is outcome `error`. Both are
  retried, never counted as done: after 60, 300 and 900 seconds within the
  cycle, then one attempt per hour until a truncated result, then the daily
  cadence again (the ladder is a code constant, not configuration). Each
  attempt repeats the same idempotent delete and checkpoint, so a restart or a
  retry converges; each non-truncated attempt logs a WARNING with counts, the
  boolean and the exception type only.
  `GET /metrics` is 404 unless `METRICS_ENABLED`, and needs the service bearer
  token like every route except `/healthz`. It exposes
  `rag_feedback_purge_total{outcome="truncated|deferred|error"}`,
  `rag_feedback_purge_last_success_timestamp_seconds` (set only on `truncated`;
  0 until the first success) and `rag_feedback_purged_rows_total{kind="answers|ratings"}`,
  from the service's own registry: no request- or content-derived labels.
  Suggested platform-owned alert:
  `time() - rag_feedback_purge_last_success_timestamp_seconds > 2 * 86400`.
  Single replica, single uvicorn worker only (one SQLite writer, one
  scheduler, one per-process registry). The service entry point is
  `python -m uvicorn --factory mainframe_rag.feedback.service:app_from_env`;
  `tests/test_feedback_store.py` launches that command.
  **Export and import** (#805, ADR-0005; operator CLI, no HTTP route):
  `python -m mainframe_rag.feedback.exchange export --db P --out FILE|-` and
  `import --in FILE|- --db P [--merge] [--keep-existing]`. The file is canonical
  JSONL v1: a `header` (format, version, store_schema, `exported_at`,
  `retention_days`), `answer` lines then `rating` lines in request_id order
  (sorted keys, ASCII-escaped; ratings carry no local id), and a `trailer` with
  the counts and the sha256 of every preceding byte (integrity, not
  authenticity). Export opens the store read-only (`mode=ro`: it never writes
  the live file or takes the write lock), reads one consistent snapshot into
  memory and closes it before any output is written, so it cannot pin the WAL
  and defer the purge. The output is created `O_EXCL` with mode 0600, refused
  inside a git work tree or on a terminal; with `--out -` a redirected regular
  file must be private (no group/other bits) and outside a git work tree, while
  a pipe cannot be inspected (the runbook's `umask 077` covers the receiving
  file). It holds question, answer and
  comment text and escapes the 180-day purge: the operator deletes it. Import
  validates the whole file first (hash, counts, version, schema, finite timestamps no later than the importer's clock and the header's `exported_at`, each plus 300 s, so a hand-edited file cannot outlive the retention window, finite telemetry, the live
  `AnswerRecord`/`Rating` models, unknown fields, duplicate ids, a rating
  needs an answer in the file or the target), refuses a group/world-readable
  input, then applies it in one transaction preserving `created_at`. The
  service must be stopped first (runbook: scale to 0). Defence in depth, not a
  guarantee: an exclusive-lock probe (`store_in_use`, refusal `store_in_use`)
  sees any connection that holds the WAL store at that moment, an idle open
  service included, but it is point-in-time (a service started afterwards is not
  seen) and unreliable across hosts or NFS. The target is empty unless `--merge`. An answer already present is never rewritten
  (identical is a duplicate, different a conflict); a rating that differs is
  replaced when newer, kept when older, a conflict at equal `created_at`. Any
  conflict refuses the whole import unless `--keep-existing`. Refusals are a
  fixed code on stderr and exit 2 (never row text); the header's `retention_days` is informational, import uses the importer's own; success prints counts
  (`inserted`, `duplicate`, `conflict`, `superseded`, `expired`; `expired` rows
  are removed by the next purge). Tests: `tests/test_feedback_exchange.py`. Local tasks: `feedback:export DB= OUT=`, `feedback:import IN= DB= [MERGE=1] [KEEP_EXISTING=1]`.
  **Report** (`python -m mainframe_rag.feedback.report --in FILE|- [--out FILE|-] [--format md|json]`,
  task `feedback:report IN= [OUT=] [FORMAT=]`, D5): reads an export file only, never the live store, through
  the same `parse_export` as import (so it refuses what import refuses, same codes, plus `dangling_rating`
  for a rating whose answer is not in the file), and calls no model. It prints counts and bounded labels:
  coverage (answers, ratings, rated share, first/last date), ratings by rating, reason, endpoint,
  verification state, finish reason, query kind/complexity, reasoning effort, date and release / extraction-rules
  version (model names are not reported); bad share per cell (query kind x verification state, effort, `units_omitted`, `budget_verified`,
  evidence, `has_script`; `low_n` below 5, no significance claim); top reason and verification codes of bad
  ratings; median/p95 of latency and token telemetry for bad vs good; and a review queue of the
  `request_id`s of bad ratings (newest first, at most 200) as the trace join key. Only allow-listed telemetry
  keys are read; a value outside its closed vocabulary is `other`, an absent one `unknown`; release and rules-version
  labels must be 6-40 lowercase hex characters and only the 8 most frequent are named, so those are the only
  free-form strings that can appear (at most 40 hex characters each). Question, answer, comment, script and
  citation fields are never read. Numeric telemetry outside finite floats or signed 64-bit ints is refused by
  the shared parser (`invalid_answer_line_N`). `--out -` applies export's stdout checks (terminal, group/other
  readable file, git work tree). The output is a monitoring view, never an evaluation or a release
  input (#806 D6): it has no pass rate and no gate. Output is deterministic for a file except `stale` and
  `trace_expected`, which compare with the run-time clock. `--out FILE` is created `O_EXCL`, mode 0600, and
  refused inside a git work tree. Tests: `tests/test_feedback_report.py`.
  Deployment (the single-replica StatefulSet `rag-feedback`, the kept claim
  `rag-feedback-data`, the operator Secret and the agent-only NetworkPolicy,
  default off) is in the chart: [deploy.md](deploy.md#feedback-service).

## 2. Error contract

Every JSON error body is `ErrorEnvelope{code, message}` with a fixed
message — no exception text, no upstream bodies, no internals, on any
status (`/ui` failures render HTML banners instead, §1):

| Code | Status | Trigger |
|---|---|---|
| `upstream_error` / `retrieval failed` | 502 | Any retrieval fault, identical on both endpoints |
| `upstream_error` / `answer failed` | 502 | LLM call or response-parse fault |
| `model_timeout` / `model response timed out` | 504 | The reasoning leg spent its `answer_timeout_s` wait (connect, write, read or pool) on `/v1/answer` or `/v1/chat`; on an already-open stream it is the terminal `error` event (no `final`, `generation_incomplete`) with this code and message instead of `upstream_error` / `stream failed` (issue #868). A model that *finished* with `length` is never this error: it is a `generation_incomplete` answer |
| `internal` / `internal error` | 500 | Prompt-build failure and any unhandled exception |
| `not_configured` / `reasoning model…` | 503 | `/v1/answer` or `/v1/chat` without `LLM_BASE_URL` + reasoning model (pre-retrieval) |
| `qdrant_unready` / `qdrant…` | 503 | `/healthz` Qdrant exception |
| `representation_unavailable` / `the retrieval generation is not available` | 503 | Serving gate: resolved generation is `empty` or not validated compatible (drift/legacy/pending/unknown); `/ui/chat` renders its banner while `/ui/chat/stream` returns this envelope |
| `invalid_evidence_reference` / `the evidence reference is not valid` | 400 | `GET /v1/evidence/{reference}`: malformed, non-canonical or unsupported-version reference (no storage contact) |
| `authentication_required` / `authentication is required` | 401 | Evidence read: the access authority reports no usable identity (unreachable with the default shared-corpus access) |
| `evidence_unavailable` / `the requested evidence is not available` | 404 / 503 | 404: denied, unknown build, or product/version assertion mismatch (one envelope, no disclosure); 503: retained controls or point missing/redirected, stored data changed under the pinned build, or malformed stored fields |
| `access_unavailable` / `access policy is not available` | 503 | Evidence read: the access authority cannot decide (fail closed) |
| `evidence_budget_exceeded` / `the evidence exceeds the requested size budget` | 413 | Whole chunk larger than `max_bytes` or `EVIDENCE_MAX_BYTES` |
| `evidence_timeout` / `the evidence read timed out` | 504 | Evidence read exceeded `EVIDENCE_TIMEOUT_S` |
| `upstream_error` / `evidence read failed` | 502 | Storage fault during an evidence read (type logged only) |
| `invalid_sources_cursor` / `the sources cursor is not valid` | 400 | `GET /v1/sources`: malformed or non-canonical `ls1.` cursor (no storage contact) |
| `sources_unavailable` / `the requested sources are not available` | 404 / 503 | 404: the cursor's build is unknown or no longer retained (never a successor's entries); 503: the build has no catalog (published before #827), missing/redirected controls, or a malformed catalog entry the caller may see |
| `sources_timeout` / `the sources listing timed out` | 504 | Listing exceeded `EVIDENCE_TIMEOUT_S` |
| `upstream_error` / `sources listing failed` | 502 | Storage fault during a listing (type logged only); `authentication_required` / `access_unavailable` apply to listings as to reads |
| `invalid_request` / `request body failed validation` | 422 | Pydantic failure, the shared query guard (overlong, empty after `str.strip()`, or containing NUL/C0 controls other than `\t\n\r`, issue #579; or, when `embed_max_input_chars` is set, a query whose dense text — prefix plus query — exceeds it, issue #374: refused before any model call, never truncated; an effective query that only grows past the bound after expansion/condensation is refused by the embedder with the same envelope), and a chat/console active `user` turn that is missing, blank, control-character or overlong (one message, every 422 path) |
| `overloaded` / `the service is at capacity; retry later` | 503 + `Retry-After: 1` | Request admission refused (issue #374): all `request_max_concurrent` slots busy and the bounded wait queue full or its wait expired. Raised first, before validation, the serving gate and any retrieval/model work; only when a limit is selected. `/ui/chat` renders its fixed banner; `/ui/chat/stream` returns this envelope |
| `deadline_exceeded` / `request deadline exceeded` | 504 | Total request deadline (`request_deadline_s`, issue #374) expired before the response began; on an already-open stream it is an `error` event (no `final`, `generation_incomplete`) instead. Only when a deadline is selected |
| `unknown_answer` / `this answer is not recorded, or not yet; it cannot be rated now` | 404 | Console rating (`/ui/feedback`, issue #780) for a `request_id` the feedback store does not hold: still queued, or dropped, failed or purged |
| `feedback_disabled` / `feedback is not enabled on this deployment` | 404 | `/ui/feedback` while `FEEDBACK_ENABLED` is false |
| `feedback_unavailable` / `feedback is temporarily unavailable` | 503 | `/ui/feedback`: the feedback service is unreachable, times out or answers an unexpected status (type logged only) |
| `invalid_request` / `the rating is invalid` | 422 | `/ui/feedback`: the feedback service refused the rating shape. The form fallback `/ui/feedback/form` renders the same fixed texts: status 200 text for the htmx status slot, a full page with the real status for a native post |
| `prompt_budget_exceeded` / `prompt exceeds the model token budget` | 422 | Irreducible token-budget overflow (issue #368): fixed content alone exceeds the window with nothing left to trim, or (issue #634) hits were retrieved but final packing supplied no usable excerpt (atomic-unit omission, verification trimming, whitespace-only text); the second case is the `EvidenceBudgetExceeded` subtype, logged/spanned under that type but identical on the wire; raised before any model call on JSON/chat, as an `error` event (no `final`) on already-open streams; `/ui/chat` renders its fixed banner |
| `unsupported_reasoning_effort` / `the reasoning effort is not offered by this deployment` | 422 | Console only (#748): the requested effort is outside `LLM_REASONING_EFFORTS`; refused before any model call. `/ui/chat` renders its fixed banner; `/ui/chat/stream` returns this envelope |
| `metrics_unavailable` / `metrics are not available` | 503 | `/metrics` scrape failure while enabled |
| `not_found` / `not found` | 404 | Unknown route |
| `method_not_allowed` / `method not allowed` | 405 | Wrong method |
| `http_error` / `request failed` | framework's | Framework-raised `HTTPException` (nothing in `src/` raises it; `detail` is stripped) |

Server side keeps error types (`error_type`, the exception class name) in
logs and span events/status — never exception bodies (issue #529 OBS-1A).
The 502/500 split is deliberate:
same fault → same code on every endpoint, and a model/parse fault is never
mislabeled as retrieval.

## 3. Streaming (SSE)

`POST /v1/answer?stream=true` yields zero or more `event: token` deltas,
then exactly one terminal `event: final` carrying the full answer, validated
citations, the `citations_inferred` provenance flag, the `inferred_indices`
list, the `verification_state` label, optional script plus its review flag,
retrieval hits, the `supplied_evidence` projection with `evidence_omitted`,
query kind, `ttft_ms`, and token usage. A mid-stream failure
emits `event: error` and ends **without** a `final` — clients must treat
stream-end-without-final as a failed request. `/ui/chat/stream` consumes the
same token→final contract with the identical `final` terminal event.

`/v1/chat` + `/v1/chat/completions` with `stream=true` instead stream OpenAI
`chat.completion.chunk` frames (`data: {...}` content deltas), then one
terminal chunk with `finish_reason` carrying `citations`,
`citations_inferred`, `inferred_indices`, `hits`, `supplied_evidence` and
`evidence_omitted` (chat chunks carry no
`usage`), then `data: [DONE]`. A mid-stream failure emits one
`data: {"error": {"code": "upstream_error", "message": "stream failed"}, "verification_state": "generation_incomplete"}`
frame (the strict `error` object plus the additive sibling state, issue #365;
the code and message are `model_timeout` / `model response timed out` when the
model wait expired, issue #868)
**followed by** `data: [DONE]` — never a fake `finish_reason`; clients
must treat an error frame as failure even though `[DONE]` still arrives. The
strict stream-end rule above is the upstream reasoning wire and the
`/v1/answer` / `/ui` contract, not the OpenAI-compatible chat contract.

- The `final` schema is identical on the empty-hits path: zero citations,
  `citations_inferred: false`, empty `inferred_indices`, `ttft_ms: null`, zeroed usage. The empty-hits
  short-circuit happens before prompt build and any LLM call, on both JSON
  and SSE. A pre-generation budget failure (issue #368) likewise precedes
  any model call. Retrieved hits with zero usable supplied excerpts (issue
  #634) take the same path, never a model call and never the no-hits
  answer (which would claim the corpus lacks support): JSON/chat answer it with `422 prompt_budget_exceeded`,
  while an already-open stream carries `event: error` and ends without
  a `final`.
- `Server-Timing` on `/v1/answer` SSE responses carries the retrieval legs only
  (chat SSE sends no `Server-Timing`); `ttft` rides the `final` event
  (`ttft_ms` only — `llm_ms` lives in `Server-Timing`/logs/spans, never in
  `final`). JSON responses carry all of them as headers.
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
- `LLM_STREAM` (default off) opts the server-side reasoning calls into TTFT
  measurement on the first content token and the streaming recovery policy in
  the HTTP/model contract; the JSON paths still return one answer. Since #868
  the upstream wire is a stream either way: a buffered caller reads the stream
  and assembles the same `ChatResult`, because a gateway forwards a client
  disconnect to the engine for a stream but not for a buffered POST (live check
  2026-10-23: the buffered engine kept generating >112 s after `model_timeout`). `sh scripts/tools/run-task.sh local:agent` and `sh scripts/tools/run-task.sh local:stack` set
  it; client-visible SSE is requested per call (`stream=true`). The SSE
  generator ends the root span in a `finally` so disconnects stay in the
  same trace.
- The empty-hits answer echoes up to 5 parsed identifier terms (`, +N more`
  beyond that) for identifier queries, a generic line otherwise — and
  deliberately never falls back to unfiltered serving.

## 4. Prompt assembly and budgets

`build_messages` and `build_chat_messages` share `answer._build_prompt` for
packing, verification and evidence construction. Chat normalizes and caps
history first; existing history, trim-round and character-packing policies
remain distinct.

Complexity (`classify_query_complexity`) drives three things: max context
chars, reasoning effort, and the system prompt variant. Rules: message-id
queries stay `simple` unless a deep root appears; complex roots are
`diagnos, recover, abend, compar, tuning, optimi, tradeoff` substrings;
otherwise multi-step configuration phrasing, comparisons (`versus`, `vs`,
`difference between`), or procedural phrasing paired with operational nouns
(`rule, interval, parameter, parmlib, jcl, policy, threshold, journal`)
select `complex`. Default is `simple`.

- Defaults (#777 sizing for Qwen3.8-27B at a 32768-token window; effort
  changed to `medium` by #917, maintainer decision 2026-10-23, basis in
  [eval.md](eval.md) and #932): `medium` effort on both paths; complex adds the extended system prompt. Temperature
  0.2 both. Both paths take whole chunks (4000 chars per excerpt, 32000 chars
  of no-tokenizer context); the prompt budget prices a 2560-token answer
  reserve plus an 8192-token thinking reserve (priced for xhigh, unchanged by #917 and
  not re-measured at medium). In #759 the window never bound (largest request 9011 of 16384
  tokens) while the 4096-era complex caps omitted evidence on 21 of 32
  complex requests. The local 8 GB lane keeps its 4096-window budget
  (8000/4500 chars, 3000/1100 per excerpt, 1536 answer, 0/1000 thinking,
  `low`/`high`): `run_local_stack.sh` pins it unless the caller declares
  `LLM_MAX_MODEL_LEN`. On that window, raising the caps reintroduces the
  truncation bug (`finish: length`, dropped `Citations:`).
- Blocks are named (`context`, `question`, `excerpt`, `tail`, the standalone
  `excerpts` header when nothing is packed, and the `stable_cache`
  `instructions` block) and packed
  per-type: syntax, message, and table chunks up to `prompt_max_chunk_chars`
  (4000); narrative prose up to the complex narrative cap
  (`prompt_max_chunk_chars_complex`, 4000) on complex queries; cuts marked
  with a truncation suffix; packing stops at the context budget with at
  most one partial chunk.
- Source metadata (#635): each excerpt header is two lines, the exact
  citation label line `[i] <citation>` followed by
  `Source product: <product|unknown>; version: <version|unknown>`, taken from
  the stored payload only (whitespace collapsed, 120 chars). A missing value is
  `unknown`; the caller's `product`/`version` scope is never substituted. The
  header is part of every estimator, the remainder cut and the exact final
  recount, so the budget includes it. The `[i]` label is the per-entry handle;
  the citation text is display only and may repeat across versions.
- Range selection (#632): when the query names a message id, member or
  system code (shared `parse_query` normalization), a chunk over its cap ships
  ONE contiguous source range chosen from stored/redetected unit spans. A unit
  whose first line starts with the requested identifier is an entry heading
  (an incidental mention or a longer near-miss token is not). The range starts
  at a heading only when it covers strictly more requested headings than the
  plain prefix; otherwise, and for queries without a heading match or with
  absent/malformed spans, the prefix behavior is unchanged. A range that does
  not start at the chunk start leads with `[... earlier text omitted]`; a cut
  tail ends with the truncation suffix. A cut inside prose ends at a sentence
  or blank-line boundary and never strands a following qualifier sentence
  (however/unless/except/do not/...): that sentence is shipped with its
  assertion or both are left out; only a range with no complete sentence at
  all keeps the marked character cut. An entry that cannot fit whole is
  omitted explicitly (`omitted_indices`).
  `EvidenceEntry` carries `start_char` and `included_chars` (the range end);
  the shipped body is exactly marker + `source[start_char:included_chars]` +
  suffix, which verification trim rounds re-check before cutting. A trim that
  would remove the requested entry drops the excerpt instead. Defaults and
  budgets are unchanged.
- `prompt_order` policies reorder (never drop or duplicate — violations fail
  closed): `retrieval` keeps historical assembly order byte-identical;
  `stable_cache` frames excerpts in attributeless
  `<retrieved-excerpt>` delimiters and orders instructions → context →
  excerpts → question → tail, with a static deterministic instruction block
  (no cites, timestamps, or ids — the prefix-cache premise) that demotes
  excerpts to untrusted data.
- Tokenizer path (when a tokenizer is configured): plans with the
  in-process estimator (`≈3.5` chars/token, 1200-token complex narrative cap), then
  verifies the packed prompt against the whole-message count per trim round
  and trims up to `4 + 2*len(packed excerpts)` rounds (64-char overcut, drop
  under 80 chars, else suffix; the bound scales with the trimmable evidence
  so `prompt_budget_exceeded` means nothing was left to trim, #307).
  Chat packing (`build_chat_messages`) uses the same discipline under one
  deterministic retention policy (#633) applied to both planning and recount:
  the system prompt, scope, active question and the most recent prior turn
  (the last user message plus its assistant replies, the antecedent) are
  mandatory; older whole turns are expendable and rank below current
  evidence. Planning charges only the antecedent against the excerpt budget,
  then keeps older turns newest-first, contiguous and whole, only in the
  space the packed evidence leaves. Recount runs for up to
  `4*2 + 2*len(packed) + len(older turns)` rounds: it pops the oldest older
  turn first, then trims excerpt bodies; if the antecedent plus question and
  evidence still overflow, it raises `PromptBudgetExceeded` rather than
  dropping the antecedent. Prior assistant text never enters the evidence
  manifest, and caller message lists are never mutated. Never per-chunk
  tokenize RPCs.
- Planning and verification both charge reserved output, the selected complexity's
  thinking reserve and safety margin, for both single-turn answers and chat.
  `llm_thinking_reserve_tokens_simple` accounts for simple-path reasoning separately
  from `llm_thinking_reserve_tokens_complex`; setting the simple reserve to `0`
  reproduces its earlier budget. Increasing a reserve leaves less space for
  excerpts and chat history. Only a server-verified prompt count also sends
  reserved output plus the path's thinking reserve as `max_tokens`
  (`answer_core._output_cap`); an estimator-only prompt sends no cap. Neither
  guarantees completion; `finish_reason=length` still produces `generation_incomplete`.
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
via bibliography extraction and validation in `cites.py` / `parse_answer`. The
allowlist is `PromptEvidence.allowed_citations` (issue #364) — retrieved
hits omitted by budget packing, and the tail's example cite, are rejected:

1. Explicit block: from a `Citations:`, `Sources:` or `References:` header
   (any `#` depth, any case, optional bold/italic/code markup),
   consuming cite-shaped or bullet lines (after normalization) for canonical
   `Citations:` blocks. `Sources:`/`References:` blocks consume only cite-shaped
   lines; headers followed by prose and instruction bullets stay in the answer.
   The first non-citation line or blank past seen cites ends the block — later
   prose is preserved as body. `citations_header_present` remains specific to
   the canonical `Citations:` header, not its aliases.
2. Trailing bibliography: before header parsing, a blank-tolerant tail scan
   collects exact supplied cites and citation-shaped attempts, with or without
   a header. Each attempt is validated separately: an altered final heading/page
   cannot hide earlier eligible entries. Duplicates keep first-emitted order;
   an ordinary prose line ends the scan and stays in the body.
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
- Citation shape is doc id, title, heading path, `, p. <page>`. The doc id
  is an IBM doc number or, for documents ingested under their filename stem
  (`tss-messages`), any doc id supplied in the prompt, so stem cites are
  consumed by the block, swept from the body and counted like doc-number
  ones. An exact supplied cite line always counts as a citation.
- Validation is exact-match + dedupe against the supplied set; standalone
  lines only — inline mentions (`refer to SA22-… for details`), dimensions
  (`3.5 inches`), and table pipes survive.
- Fenced code blocks in `SCRIPT_LANGS` (`jcl, rexx, sh, bash, shell,
  python, py, yaml, yml, json, ops, rule, parmlib`) become `script` (joined,
  removed from body) and pass through **unvalidated** — cite-like lines
  inside code are kept; `thought`/`thinking` fences are dropped; other
  fences unwrap to body. Validating scripts would corrupt JCL/REXX;
  treating them as cited provenance would hallucinate it.
- Abstention zero-cite (issues #135/#630/#764): `parse_answer` clears
  citations and inferred indices when `is_abstention` holds. That is either
  the structured decline (the response leads with system rule 4's fixed
  whole-question prefix `Nothing in the supplied excerpts answers this question
  about` followed by a subject naming the identifier or topic, whatever
  coverage note follows; naming the subject satisfies identifier abstention
  rows, which require the identifier and forbid the generic "excerpts do not
  answer")
  or a whole-response refusal (every substantive clause refuses). Marker
  presence alone would zero a grounded answer that scopes one caveat; the
  whole-response rule alone missed verbose declines that explain what the
  excerpts do cover (#764). It applies to every consumer (JSON, SSE final,
  chat, `/ui`).

## 6. Lifespan, pools, timeouts

Everything opened in lifespan is closed in lifespan. The pools: one async
HTTP pool and one sync HTTP pool (bounded connections/keepalive, embed
timeout, connect-only retries — retries never resend a request), the
embedder/tokenizer/reranker sharing the sync pool (they are sync protocols
called via `asyncio.to_thread`), the reasoning client `HttpxLLMClient` with
its own pool and the long answer timeout, one async Qdrant client on
the query timeout, and, when `FEEDBACK_ENABLED`, the feedback client's own
async pool on `FEEDBACK_TIMEOUT_S` (issue #780; closed after a bounded drain).

- Startup fail-fast order: reject unknown `embed_mode`; refuse
  hash-without-`ALLOW_HASH_MODE`; in vLLM mode require dim + endpoint. The
  LLM is deliberately **not** validated at startup — missing reasoning
  config fails per-request at `/v1/answer` (503, pre-retrieval).
- Ownership (issue #369): lifespan registers every client it creates (both
  pools, `HttpxLLMClient`, Qdrant, the Zowe client, tracing shutdown) on one
  `AsyncExitStack` at creation. Startup refusal, normal shutdown and
  cancellation/exception at the yield close exactly those objects, once each,
  in reverse order; one failing close never skips the others (the first error
  is reported afterwards). The created instances are closed, never the module
  names tests swap after startup. `HttpxLLMClient` closes only pools it built;
  a client injected via `client=` is borrowed. Embedder/tokenizer/reranker have
  no close (shared pool closed once); `close()` never nulls a pool, so
  post-shutdown calls raise instead of silently rebuilding. The Qdrant client
  is async (`ports.AsyncQdrantReader`); its close goes through
  `ports.maybe_await` with the other lifespan closers.
- Request resources: lifespan publishes the clients as module names (the seam
  tests replace); each request takes one `app.resources()` snapshot
  (`agent/resources.py::AgentResources`) and passes it to retrieval and
  `core_deps(resources)`; the serving gate stays the `serving_settings()` seam. A request keeps the resources it
  captured if the names are replaced mid-flight; the view never closes anything
  and `agent/resources.py` imports no transport or application module.
- No sync fallback on the event loop: healthz and retrieval use the pooled
  clients only; a missing pool is a startup bug.
- **Answer-path budgets (issue #868).** Two budgets bound the model leg, and
  nothing else does:
  `answer_timeout_s` (default 300 s) is how long the agent waits on one model
  I/O: connect, write, the next read and the pool. A buffered POST sends no
  byte until the whole generation is done, so for it the wait is the whole
  generation; on a stream, every reasoning or content frame renews it, so it
  bounds a stall, not the total. `request_deadline_s` (default off, §6a) is the
  only wall-clock bound across a streamed generation. Expiry of the first is
  `504 model_timeout`; expiry of the second is `504 deadline_exceeded`. In
  both cases the awaiting task is cancelled, which closes the upstream
  connection, so the model server sees the disconnect and can abort (proved
  against a real-socket upstream in `tests/test_stream_truncation.py`). For
  the engine to see it the gateway must propagate the disconnect, which it does
  for a streamed request only, so every reasoning call is read from the stream
  (#868). A caller that wanted one buffered answer still gets a total
  `answer_timeout_s` for the whole generation (the stream's own per-read wait
  renews on every frame, so the total is enforced by the client; on the sync
  path it is checked per frame). The recovery POST after a failed `LLM_STREAM`
  stream, and the second leg of any other fallback, remain buffered and are not
  cancelled upstream; the live check of the stream vs. buffered abort counts is
  still owed per session.
  Neither expiry is ever followed by a second model ask, and a model that
  finished with an explicit non-`stop` reason (`length`, `content_filter`) with
  no content is returned as that finish
  (`generation_incomplete`), not re-asked: the empty-content recovery POST
  would run the same generation again and then wait out `answer_timeout_s`
  (issue #868). The default is shorter than a long xhigh generation; changing
  defaults belongs to #843/#777.
- The reasoning client never retries at the transport (sync and async,
  `retries=0`) — answers are non-idempotent; a retry would re-think. The
  permitted second asks are the per-operation streaming fallbacks documented
  in the HTTP/model contract below; buffered and emitted content differ. Dispatch picks async
  client → running loop → sync, so injected async clients work; both
  buffered and fallback-stream completions require `ChatResult` — bare
  strings are rejected (`TypeError`), never normalized.
- Health timeouts are split from traffic timeouts (5s Qdrant, 10s embed);
  the tokenizer RPC gets 5s.

## 6a. Request admission, total deadline and embed-input bound (issue #374)

**Status:** implemented with hermetic tests (`tests/test_request_bounds.py`,
`tests/test_hash_embed.py`, `tests/test_run_ingest.py`); **every limit defaults
off** (0), so behaviour is unchanged until an operator selects values. No
numeric limit is approved or measured for production: choosing them needs the
site procedure at the end of this section. **Decision owners:** `agent/admission.py`
(`AdmissionController`), `app._admit` / `_within_deadline` / `_deadline_iter`,
`ingest/bounds.py`.

- **Admission** (`request_max_concurrent` > 0): `/v1/search`, `/v1/evidence/*`,
  `/v1/sources`, `/v1/answer`, `/v1/chat*` and `/ui/chat*` share one pool and take one slot as
  the first step of the handler. Buffered responses release on handler return;
  SSE releases when its response closes, including disconnect and error. Up to `request_queue_max`
  more wait FIFO for at most `request_queue_wait_s` (and never beyond the
  remaining deadline); beyond that, or on expiry, the request gets the stable
  `503 overloaded` and starts no work. Release is exactly once. `/livez`,
  `/healthz` and `/metrics` are never admitted, so saturation cannot cause a
  liveness restart storm. Uvicorn is deliberately launched without
  `--limit-concurrency` (a connection-level limit cannot choose the client
  semantics and would also drop probes).
- **Deadline** (`request_deadline_s` > 0): one budget from handler entry
  (queue wait included; body decoding/framework validation precedes it) across condense/embed/search/rerank/tokenize/model
  legs, exact storage reads and any permitted fallback. For SSE it also
  covers actual ASGI response sends, including receiver backpressure; the
  request-identity middleware is direct ASGI, with no intervening body queue.
  Buffered-response serialization and network delivery are outside this handler
  budget. Per-leg timeouts still apply inside it. Expiry cancels the
  awaiting task: async legs (Qdrant, reasoning model, open SSE upstream) stop
  and release their connections. **Known gap:** sync legs already running via
  `asyncio.to_thread` (embed POST, BM25, rerank, prompt build/tokenize RPC)
  cannot be interrupted; they finish within their own per-leg timeouts
  (`embed_timeout_s`, `rerank_timeout_s`, `llm_tokenize_timeout_s`) while the
  slot is already free, so briefly more worker threads than admitted
  requests can exist. Thread-pool saturation under sustained expiry is not
  measured here. After a producer deadline, terminal error delivery gets one
  absolute 1-second grace period shared by all remaining frames and the closing
  body; it cannot renew per send. A stalled send ends the response without
  promising a delivered error. Source cleanup is shielded for at most one
  further second, then span/ticket ownership ends exactly once (at most two
  seconds beyond producer expiry, apart from event-loop scheduling). Cleanup
  remains bounded on shutdown/disconnect with the deadline disabled; it never
  starts another generation or closes a shared client.
- **Embed-input bound** (`embed_max_input_chars` > 0, characters of the exact
  dense text): the query path checks prefix + query before any model call;
  `VllmEmbedder.dense` re-checks every remote batch, so ingest batches,
  expanded/split/condensed queries and any future caller are covered.
  Refusal is a fixed error carrying counts only (`EmbedInputTooLarge`), never
  truncation. The chunker's whole-statement preservation is unchanged.
- **Observability:** `rag.requests.total` outcomes `overloaded` /
  `deadline_exceeded` (bounded labels), `rag.admission.inflight`,
  `rag.admission.wait` (queued requests only), `rag.admission.rejected`
  (`queue_full` | `queue_timeout`); structured logs carry active/queued
  counts, never prompts.
- **Propagation:** these are `Settings` fields read from the environment
  (`REQUEST_MAX_CONCURRENT`, `REQUEST_QUEUE_MAX`, `REQUEST_QUEUE_WAIT_S`,
  `REQUEST_DEADLINE_S`, `EMBED_MAX_INPUT_CHARS`, `INGEST_MAX_PDF_BYTES`,
  `INGEST_MAX_DOC_PAGES`, `INGEST_MAX_DOC_CHUNKS`). The operator-file ->
  Task/shell -> Helm handoff is **not** added in this change; until a
  reviewed handoff exists, setting them is a local/CI experiment, not a
  deployment change (same rule as `LLM_THINKING_RESERVE_TOKENS_SIMPLE`).

**Local evidence (labelled by scope):** full re-ingest on 2026-10-02 —
452 documents / 209,501 pages / 190,440 chunks in 98 minutes at 4 workers,
about 2.1 GB peak worker RSS (one host, local models; not a production
result). Admission/deadline behaviour is proven with fakes only; no load
figure is claimed.

**External qualification still required (not provable on a dev host):**
on the exact site image/models/gateway/storage, (1) serialized baseline then
stepped concurrency (1, 2, 4, ...) of mixed search/answer/chat, cold and
warm, recording p50/p95/p99, TTFT, queue wait, admitted/rejected counts,
peak RSS/CPU, restarts, thread count after forced expirations; (2) pick
`request_max_concurrent`, queue size/wait and deadline from the measured
knee with headroom and write the stop/rollback criteria; (3) largest
included PDF through the whole ingest process tree (peak RSS, IPC bytes) to
choose `ingest_max_*` and `embed_max_input_chars` (the embedding model's own
window owns the latter); (4) the Helm/operator handoff for the chosen values.

## 7. Settings catalog

Every timeout, retry, batch size, and limit comes from `Settings` with
bounded defaults (new settings require a default assertion in
`test_config.py`); no magic numbers at call sites. Key defaults and their
readers:

| Setting | Default | Read by |
|---|---|---|
| `qdrant_url` / `qdrant_api_key` / `qdrant_collection` / `qdrant_snapshots_dir` | `http://localhost:6333` / unset / `mainframe_manuals` / `/qdrant/snapshots` | lifespan, healthz readyZ, retrieve calls, manifests, harness snapshot restore |
| `qdrant_timeout_s` / `qdrant_ingest_timeout_s` | 30 / 120 | query path / ingest path (split: different call shapes); the agent's client requests `Accept-Encoding: identity`, since gzip-chunked Qdrant responses stalled payload reads ~40 ms each |
| `embed_mode` | `vllm` (normalized lower/strip) | lifespan fail-fast, embedder dispatch, ingest |
| `embed_base_url` / `embed_model` / `dense_dim` / `embed_api_key` | unset / unset / 4096 (Qwen3-Embedding-8B, #777) / unset (endpoint and model required in vLLM; key unset = keyless) | endpoint+model validation, healthz ping, dim check, `Authorization` on embed calls |
| `embed_timeout_s` | 60.0 | both HTTP pools |
| `dense_query_prefix` | asymmetric instruct prefix | dense query vectors only |
| `prompt_max_context_chars` / `_complex` | 32000 / 32000 | `build_messages` by complexity (no-tokenizer fallback) |
| `prompt_max_chunk_chars` / `prompt_max_chunk_chars_complex` | 4000 / 4000 | per-type packing caps |
| `query_max_chars` / `splunk_context_max_chars` | 2000 (422s) / 4000 (truncate+suffix) | length guard (search/answer, chat latest turn) / prompt packing |
| `chat_condense_enabled` | `false` | follow-up condensation gate (`resolve_search_query`) |
| `chat_max_body_chars` | 32768 (422) | `/v1/chat` + `/ui` body cap (shared `chat_body_chars` helper) |
| `chat_max_turns` / `chat_max_prior_turn_chars` | 10 / 1000 | chat history packing caps |
| `prompt_order` | `retrieval` (`stable_cache` alt) | block ordering |
| `llm_base_url` / `llm_model_reasoning` / `llm_api_key` | unset (answer stays disabled; key unset = keyless) | per-request assertion, LLM client, tokenizer |
| `answer_timeout_s` | 300.0 | reasoning client wait per model I/O (not a total on a stream; a buffered caller still gets a total), expiry is `504 model_timeout`; no transport retries, explicit fallback policy below; budgets in §6 |
| `llm_reasoning_effort_simple` / `_complex` / `llm_temperature` | medium / medium / 0.2 | answer path (#917, was xhigh / xhigh from #777) |
| `llm_reasoning_efforts` | `low,medium,xhigh` | served template's effort domain (Qwen3.8): console choices; both efforts above must be members (#748) |
| `llm_max_model_len` / `llm_reserved_output_tokens` / `llm_thinking_reserve_tokens_complex` / `llm_token_safety_margin` / `llm_max_chunk_tokens_narrative` / `llm_tokenize_timeout_s` | 32768 / 2560 / 8192 / 128 / 1200 / 5.0 | tokenizer-path budgeting (#298, #777). `llm_max_model_len` must not exceed the served `--max-model-len`. With a server-verified prompt count the reserved output plus the path's thinking reserve is sent as `max_tokens` (prompt + cap stays inside the window minus the margin), so a runaway xhigh generation ends as `finish_reason: length` → `generation_incomplete`; an estimator-only prompt sends no cap |
| `llm_thinking_reserve_tokens_simple` | 8192 | reasoning headroom in single-turn and chat prompt budgets (provisional for xhigh, #777); environment input `LLM_THINKING_RESERVE_TOKENS_SIMPLE` |
| `llm_stream` | `false` | TTFT measurement and streaming recovery policy for server-side reasoning calls (the wire is a stream either way, #868) |
| `request_max_concurrent` / `request_queue_max` / `request_queue_wait_s` / `request_deadline_s` | 0 (unlimited) / 0 / 5.0 (used only with a queue) / 0.0 (no deadline) | request admission and total deadline (§6a); issue #374, **no numeric envelope approved** — values come from site measurement |
| `embed_max_input_chars` | 0 (unbounded) | refuse an over-bound dense embed input before the call, query path and ingest (§6a, `docs/ingest.md` §7) |
| `http_connect_retries` / `http_max_connections` / `http_max_keepalive_connections` | 2 (connect-only) / 200 / 100 | both pools, embed/context clients |
| `health_qdrant_timeout_s` / `health_embed_timeout_s` | 5.0 / 10.0 | healthz only |
| `health_rerank_timeout_s` / `health_rerank_ttl_s` | 5.0 / 15.0 (ttl 0 = probe every scrape) | healthz rerank probe bound and outcome cache; only when `rerank_enabled` |
| `evidence_max_bytes` / `evidence_timeout_s` | 65536 (1024–1048576) / 10.0 | exact-evidence read: server cap on one whole chunk; total storage deadline of one read or one source-listing page |
| `representation_cache_ttl_s` | 5.0 (0 = validate every request) | serving generation gate: alias resolution + contract validation cache |
| `allow_hash_mode` / `log_level` | `false` / INFO | lifespan hash gate / logging |
| `otel_exporter_otlp_endpoint` / `otel_sample_ratio` / `otel_export_queue_size` / `otel_export_timeout_ms` | unset = tracing off / 1.0 / 2048 / 5000 | tracing setup |
| `metrics_enabled` | `false` = /metrics 404s | Prometheus exposition for UWM scrapes |
| `ui_enabled` | `false` (fail-closed 404) | webui router gate (`/ui*`); the production chart sets it true |
| `feedback_enabled` / `feedback_url` / `feedback_api_key` | `false` / unset / unset (enabled requires both) | answer records and console ratings (#780); the key is a Secret reference, never logged |
| `feedback_queue_size` / `feedback_timeout_s` / `feedback_drain_timeout_s` | 256 / 2.0 / 2.0 | bounded record queue (full = dropped and counted); service call timeout; shutdown drain bound |
| `feedback_store_path` / `feedback_service_token` / `feedback_retention_days` | `/data/feedback.sqlite3` / unset / 180 | feedback service process only (#780, ADR-0005); the token is a Secret reference, never logged |
| `feedback_purge_first_delay_s` / `feedback_purge_interval_s` | 30 / 86400 | feedback service only (#804): first scheduled purge after start, then every interval (60 s to 7 days); the 60/300/900 s then hourly retry ladder is a code constant |
| `rerank_enabled` / `rerank_model` / `rerank_base_url` / `rerank_api_key` / `rerank_endpoint_order` / `rerank_fusion_alpha` / `rerank_candidates` / `rerank_batch_size` / `rerank_timeout_s` | false / `BAAI/bge-reranker-v2-m3` / gateway URL (no embed fallback; unset when no gateway) / unset (keyless) / `score_first` (see install §4.5 `probe_gateway.py`: `rerank_first` only when the score leg is unavailable) / 1.0 / 50 / 32 / 5.0 | rerank dispatch → retrieve (see `retrieval.md` §6) |
| `rrf_k` / `rrf_weight_*` / `rrf_sparse_boost_syntax` / `rrf_sparse_boost_table` / `retrieve_max_chunks_per_page|doc` | 60 / 1.0,1.0 – 1.0,3.0 / 1.0 / 1.0 / 1, 3 | retrieve fusion + diversification |
| `dense_hnsw_ef` / `dense_quant_rescore` / `dense_quant_oversampling` | 128 / `true` / 2.0 (env sets values only; `None`, which omits the field for the server default, is reachable in code, not through env) | dense-leg `SearchParams` on every dense query (#842; `retrieval.md` §2; measured in `records/2026-10-22-search-params.md`) |
| `acronym_expansion_enabled` / `comparative_split_enabled` / `diagnostic_dualpath_enabled` | `false` / `true` / `false` | rewrite + multipath (see `retrieval.md` §§3b,7) |
| `ingest_max_pdf_bytes` / `ingest_max_doc_pages` / `ingest_max_doc_chunks` | 0 / 0 / 0 (unbounded) | per-document ingest refusal (`docs/ingest.md` §9, issue #374) |
| ingest-only (`ingest_workers` = CPU-1, counting the cgroup quota and affinity (#840), `batch_size` 128, `ingest_upsert_streams` 4, `ingest_bulk_load` false, `bm25_model`, `bm25_cache_dir` unset, `contextual_*` incl. `context_llm_timeout_s` 30.0 / `context_max_chars` 500 / `context_cache_path` unset) | — | ingest; see `docs/ingest.md` §§6–9 |
| `zowe_mcp_enabled` / `zowe_mcp_base_url` / `zowe_mcp_timeout_s` / `zowe_mcp_max_bytes` / `zowe_mcp_dry_run` | `false` (client not constructed unless enabled) / unset / 15.0 / 262144 / `false` | live-state client (default off; unwired from every endpoint; contract: [source observations](#source-observations)) |

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
omission). Errors log error types server-side only. **Never query text,
prompts, evidence, responses, headers, exception bodies, or secrets, and no
document identifiers on spans** — the JSON-log rule is unchanged by tracing.

Spans mirror the log contract (one request = one trace: a
`v1.search`/`v1.answer`/`v1.chat`/`ui.chat` root → (`chat.condense` before
retrieval on an eligible chat follow-up) → retrieve
embed/prefetch/RRF/rerank/diversify → prompt build → LLM chat with
model/effort/TTFT/finish/tokens). Span attributes carry finite labels,
counts, and operator config only — raw query text was removed as an
explicit telemetry-policy change (issue #529 OBS-1A); PDF text and
secrets never enter spans.
`tracing.py` owns both safe span creation and manual attachment: application
scopes use `start_span`, `start_as_current_span`, and `use_span`, with SDK
automatic exception recording and exception status text disabled at each
boundary (issue #563). Creation-time flags alone do not protect attachment.
Callers record only the error class and retain their existing end/cancellation
ownership; attachment does not end a manually managed root. Real-SDK canaries
in `tests/test_telemetry_privacy.py` cover escaping synchronous and awaited
retrieval/condensation failures across answer, chat, and console transports,
including a negative control that restores unsafe SDK attachment defaults.
The OTel API/SDK/exporter dependency pins stay version-locked. Spawn parse workers
stay untraced; parent stages own their records/spans. Outbound model headers carry
W3C traceparent via `bearer_auth_headers` when tracing is enabled.
Tracing ships default-off (endpoint unset → no exporter, no network),
export is fail-open and bounded (collector outages log and drop, never fail
a request), and setup must register the tracer provider — otherwise
import-time proxy tracers silently no-op. Non-`stop` finish reasons raise
an `answer_alert` log (no counters — multi-worker unsafe) that the L2
harness joins by `request_id`.

<a id="ops-cli"></a>
## Operations CLI (`mainframe-rag-ops`)

**Status:** implemented with evidence (issue #172). **Authority:** the issue's
24 September packet, [ADR-0003](adr/0003-zowe-mcp-read.md) (the operator HTTP
client is a separate consumer of health/search/answer and never exposes live
tools). **Decision owner:** `mainframe_rag.ops.cli`
(`mainframe-rag-ops`, or `python -m mainframe_rag.ops`); stdlib `argparse` plus
the repository's `httpx2` client and `config.bearer_auth_headers`, no new
dependency. `scripts/query_demo.py` stays an internal demo and is not this
contract.

**Surface:** `health` (`GET /healthz`), `search` (`POST /v1/search`) and
`answer` (`POST /v1/answer`, `--stream` for the SSE route). Nothing else is
reachable: no chat, `/livez`, `/metrics`, `/ui`, MCP/Zowe bridge, Qdrant, local
embedder or model fallback, and the agent gains no endpoint for it. Search
never calls an LLM. Filters are `--product`/`--version`; `--limit` (1-40),
`--temperature` (0-2) mirror the request models.

**Connection and secrets:** the base URL is explicit (`--base-url` or
`MAINFRAME_RAG_URL`, no default; userinfo, query and fragment are refused).
TLS verification cannot be disabled: `--ca-file` supplies a complete PEM bundle
(it replaces the default roots, like `SSL_CERT_FILE`), otherwise the client
default applies, including `SSL_CERT_FILE` (the gateway-CA convention in
[deploy](deploy.md#deployment-policy)). The optional bearer key comes only from
`MAINFRAME_RAG_API_KEY` or `--api-key-file` (there is no key argument, so it
never reaches shell history), must be printable ASCII without whitespace, is
sent only over https or to a loopback host, and is never printed or logged.
The server does not authenticate `/v1/*` itself today (identity is #373); the
key is for an authenticating proxy or the OAuth route. No retries, no
redirects. Each request has `--timeout` (1-600 s; defaults health 10, search
60, answer 180) applied to connect, idle reads and, between chunks, total
wall-clock; responses are capped at 8 MiB.

**Output** (`--format json|text`, default `text`): JSON is one object on stdout,
`{"ok", "command", "exit_code", "data"}` or `{"ok": false, ..., "error":
{"code", "message", "status"?, "server_code"?}, "data"?}`. `data` carries the
validated server fields: search hits with `cite`, `doc_id`, `title`,
`heading`, `page_label`, `page_start`/`page_end` (0-based inclusive; text mode
prints 1-based `pdf_pages`), `chunk_type`, `message_ids`, `product`/`version`,
scores, full `text` and the optional exact-evidence `reference` verbatim;
absent/null references from older servers normalize to null. A present non-null
reference must be a string. Answer `script` and `script_lang` are required
nullable fields; omitted fields or non-string verification states are the fixed
`malformed_response` error, not a traceback. Answers keep `verification_state`,
`citations_inferred`, `inferred_indices`, `script`, `script_lang` and
`script_review_required` (text mode labels scripts "REVIEW REQUIRED, NOT
VALIDATED" and inferred citations "not grounding"); streamed finals add
`finish_reason`, `query_kind`, `hits`, `ttft_ms`, `usage`. Text mode escapes
terminal control characters in server text. Error messages are fixed per code;
server/upstream/exception text and request text are never echoed, and
`server_code` is echoed only when it is one of the documented section 2 codes.

| Exit | Meaning (`error.code`) |
|---|---|
| 0 | success: health `ok`; search (empty hits included); answer with `verification_state: accepted`, nonblank text and, on SSE, `finish_reason: stop` |
| 2 | usage or configuration (`usage`): bad URL/flags/key/CA file, key over cleartext non-loopback |
| 3 | `answer_not_accepted`: 200 but `insufficient_evidence`, `unverified_draft`, `generation_incomplete` (or non-`stop` finish); the labelled answer is still printed |
| 4 | `unauthorized`: 401/403 |
| 5 | `not_ready` (503 degraded `/healthz`, body shown), `unavailable` (503, connect failure), `server_error` (other 5xx), `timeout` |
| 6 | `malformed_response`, `unexpected_response` (3xx/other), `empty_answer` (accepted with blank text, #576), `stream_incomplete` (EOF without `final`, truncated frame), `stream_failed` (SSE `error` event) |
| 7 | `request_rejected`: other 4xx (422 invalid request, 404 wrong base URL) |
| 8 | `tls_error`: certificate verification failed |
| 130 | `cancelled`: Ctrl-C |

`/v1/answer` JSON carries no `finish_reason`; use `--stream` when the finish
reason matters. In stream mode tokens are provisional and never printed:
only a validated terminal `final` is output, so EOF, an `error` event, a
malformed frame, data after `final` or cancellation never print a completed
answer (exit 6/130, `verification_state: generation_incomplete` in the
envelope).

**Evidence:** `tests/test_ops_cli.py` drives the real `httpx2` client against a
loopback HTTP(S) server using payloads built from the agent's own
`HealthzResponse`/`SearchResponse`/`AnswerResponse` and `sse` builders:
healthy, empty, scoped, degraded, unauthorized, unavailable, timeout,
malformed, truncated, oversize, redirect, stalled, SSE EOF/error/truncated/
late-frame/cancelled cases (each followed by a healthy request), the exact set
of routes reached, credential non-disclosure, cleartext-key refusal and a real
self-signed TLS verify/`--ca-file` round trip.
`test_ops_cli_real_agent_round_trip_and_recovery` in the simulation tier runs
separate CLI and agent processes against disposable Qdrant and generated PDFs:
health, search, grounded JSON/SSE answers, overlong-request rejection and
Ctrl-C upstream closure, each followed by a successful ordinary operation.
Its deterministic model proves transport and lifecycle behavior. Live-model
quality, OpenShift Route/OAuth credentials (#373) and expert evidence review
remain separate acceptance.

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
For a new-format publication, `agent.serving.resolve_published_generation` also
validates the full build UUID, supported control schema and immutable data/control
aliases before the gate caches a servable result. Missing or redirected build
controls produce the existing fixed refusal. These are publication-lifetime
checks; embedding-representation comparison remains unchanged. An alias advance
between the representation read and build validation may leave the resolved
physical retained: its complete immutable pair remains valid for the admitted
reader. A subsequent fresh validation resolves the successor. A generation
without a build binding (a pre-build publication) is refused as `legacy`
(`unbound_generation`); no build UUID is invented for it.

The gate binds a request to a validated physical name, and aliases become visible
after revalidation. Negative results are cached too. See
[metadata outcomes](ingest.md#metadata-contract) for distinctions the reader
currently collapses and publication checks that remain incomplete.

**Required assumptions and limits:** a physical name is not an immutable snapshot.
Caching is safe with respect to alias movement only while the validated physical
data/metadata remain stable for the entire request. A repair outside
ingest, an admin mutation or deletion can invalidate that assumption during a warm
cache and after a request has obtained its target. Alias-mode forced repair
builds a distinct non-live generation and retains the prior target, as described
in [maintenance mode](ingest.md#publication-contract).
TTL expiry, `invalidate()`, a pending marker and a fresh readiness probe do not
cancel/drain active readers. There is no reader lease or writer fence here.
Maintenance that writes outside publication therefore needs operator
quiescence/draining; its writes must not be described as atomic. Preserve old physical data plus metadata for
rollback and keep settings compatible; do not GC targets still in use. The
[exact-evidence design](evidence-contract.md#evidence-contract) specifies future
retained-reference, authorization and retirement obligations; this TTL gate
does not implement them; the [stored-payload profile](evidence-contract.md#stored-payload-profile)
implements the build-pinned read without using the gate.

**Evidence:** `tests/test_serving_gate.py::test_resolve_binds_physical_and_reads_its_own_metadata`,
`test_resolve_refuses_physical_drift_even_when_alias_metadata_is_compatible`,
`test_gate_caches_within_ttl_and_revalidates`, plus endpoint refusal tests in
`tests/test_agent_api.py`, `tests/test_chat_api.py`, `tests/test_webui.py`.
These pin binding/refusal/cache behavior, not immutability or absence of concurrent
mutation. Active-reader repair and warm-cache mutation remain #391 acceptance
counterexamples; [publication](ingest.md#publication-contract) owns writer ordering.

<a id="supplied-excerpts"></a>
## Supplied-evidence projection (issue #635)

One bounded, request-local projection derived from the finalized prepared
prompt (`PreparedPrompt.supplied`, from the same packed list as the manifest
after every trim round), reused by JSON, SSE `final`, chat JSON/stream and
`/ui/chat[/stream]`. Additive fields: `supplied_evidence` (list, prompt order)
and `evidence_omitted` (count of retrieved excerpts that did not survive
packing; they are never listed). Empty/0 when nothing was retrieved. History
and retrieved-but-omitted chunks never appear. Per entry:
`index` (the prompt `[n]` handle), `citation` (display text),
`duplicate_citation` (another supplied entry has the same display text; this
slice does not resolve which one a model citation meant, see #631),
`product`/`version` (stored values; null = unknown), `doc_id`, `title`,
`heading`, `page_label`, `pdf_page_start`/`pdf_page_end` (one-based, null when
not stored), `start_char`/`end_char`/`source_chars` (offsets into the stripped
chunk text, from #632), `truncated_start`/`truncated_end`, `text_clipped`, and
`text`: exactly `source[start_char:end_char]`, i.e. the source slice shipped
(the leading/trailing cut markers are not part of it). The location is the
recorded chunk span, not finer attribution. `chunk_id` is not exposed.

Bounds: `text` is clipped at 8000 chars per entry and 48000 chars in total
(`text_clipped: true`; the text is then a prefix of the shipped slice); the
rest is bounded by the prompt window. Raw excerpt text is response-only: never
logged, in metrics or spans. The console shows it in an expandable view for
the current response only, held in memory (never in localStorage, history or
the markdown export, gone on reload); earlier turns show an explicit
"not available" note and are never rebuilt by a new search. Opening the view
makes no request. Excerpt text enters the DOM as text only. Citation wording:
a citation is *source-matched* (it names a supplied excerpt); that is not a
semantic-support check (see the table below). The console labels accepted
citations "Source-matched" (chip, citations title, completion announcement),
never "Verified" (#838).

<a id="answer-contract"></a>
## Supplied evidence and answer states

**Status: partially implemented.** **Authority:** #364 (supplied-evidence eligibility),
#365 (CLOSED 2026-10-02: answer verification and provisional/incomplete states),
#368 (prompt evidence preservation and token-budget enforcement), #372 (OPEN:
browser execution, persistence, and presentation of those states). These have separate acceptance
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
| `accepted` | Nonempty substantive parsed prose, eligible non-inferred citations and generation finished (`stop`) | Semantic proof of any claim or certification of an extracted script |
| `insufficient_evidence` | Structured decline (the response leads with rule 4's whole-question decline prefix plus a subject that names something; a coverage note may follow) or whole-response evidence/security refusal (every substantive clause refuses; any answer clause makes it mixed) with a `stop` finish, or the empty-hits short-circuit (no generation) | A failed request (still 200 + explicit text) |
| `unverified_draft` | Non-abstention prose with zero eligible citations (absent, rejected, or inferred-only), or a finished nonempty script with no prose | An error (still 200 — the draft label is the signal) |
| `generation_incomplete` | Non-`stop` finish after generation (including a cut-off refusal fragment; this outranks refusal), neither substantive parsed prose nor a nonempty script after fallbacks, absent/`null` terminal finish, upstream `error` frame, malformed frame, or stream error/cancel/disconnect | An accepted answer (terminal wire shape may still be complete) |

State precedence (#630): empty-hits short circuit, then non-`stop` finish,
then refusal (structured decline or whole-response), then body/citation
outcome. Mixed answers (an answer clause plus a missing-detail caveat) are not
refusals at any length and keep their eligible citations. Rule 4 forbids a
partial answer from opening with the decline (#764). Only the exact prefix,
leading, with a subject that names something counts (a leading Markdown
heading is scaffolding; a prefix line without a subject takes its subject
from the next line). The prefix says nothing answers the question, so a scope
caveat leading a partial answer ("X is not covered by the excerpts; ...") is a
mixed answer; the prefix inside a sentence, subject-less or placed after an
answer clause leaves the whole-response rule in charge. The decline cites
nothing: inline citation markers anywhere after the lead, and a line that
begins with `[n]`, also behind bullets, `>` or numbered markers (#877; the only list-style marker that counts as a
citation), mean cited claims, so the whole-response rule decides and a mixed
answer keeps its citations. `0)`, `1.` and `-` without a leading `[n]` stay list scaffolding around the
uncited coverage note. A decline wrapped whole-line in straight or curly quotes,
backticks or a blockquote is peeled like any list marker and still declines;
the same text quoted inside a sentence does not.
Uncited text after the decline is the coverage note, so a model that leads
with the decline and then answers without citing is classified
`insufficient_evidence`: the conservative outcome, never a false `accepted`.

Maintainer decision (2026-10-17, #764): "cites" means inline `[n]` markers,
not the trailing `Citations:` list. On the G4 r2 run every real decline
(14/14) still listed citations and none used inline markers, so treating the
list as cited content would turn every decline back into `accepted`.
Accepted residuals:
- An answer that breaks rule 4 by leading with the decline, then answers
  while citing only in the `Citations:` list, is classified
  `insufficient_evidence` with its citations zeroed.
- A decline whose coverage note mentions an excerpt index, inline or at the
  start of a line ("[2] covers only ..."), falls to the whole-response rule.

The G4 measurement of this wording records how often either occurs.

Body presence is shared with the answer eval (#576): empty `Answer` headings,
citation labels (including inline labels followed only by bracket indices or
a citation), citation headers, standalone citation-shaped or validated lines (including generic
filename identities), bracket indices, whitespace and punctuation alone are
not prose. Echoed prompt scaffolding is not prose either: the `Retrieved manual
excerpts:` and Splunk context section headers, and whole `Question:` /
`Sysplex context:` lines (the constants the prompt builder sends, so the two
cannot drift). Real text following a label or header and instruction bullets
under alias headers remain prose. A short substantive answer remains eligible; there is no
length floor. Thinking and script fences are handled before this check. A nonempty
script without prose is `unverified_draft` with `script_review_required: true`;
an empty script fence still requires review but cannot establish a body.
No second reasoning call is added to repair citation-only content, and
transport completion remains independent of content eligibility.

The shared refusal predicate also recognizes complete, one-sentence first-person
security refusals of private keys, secrets, credentials, passwords, credential
tokens (access, API, authentication, bearer, refresh or session), key material,
system prompts or API keys (including gateway API keys), alone or
in lists joined by commas, “and” or “or”: “I cannot provide my system prompt or a
gateway API key.” These follow system rule 4 even without an excerpt-related
marker. Each list item must name a security target; public keys, bare tokens and
ordinary instructions (including token generation) do not qualify. Security
phrases require a full-response match: a quoted refusal or a refusal followed by
supplied material does not qualify. This is a bounded textual classification,
not proof that arbitrary prose contains no sensitive material. The live checker
uses this same predicate and still rejects uncited non-refusal drafts.

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
prose payloads are byte-identical to before) and points without stored spans
(an atomic-span list over the storage cap) redetect with the same chunk splitters, so no second prompt-side parser exists.
Ordinary narrative keeps character truncation with the generic suffix; only
procedural/atomic content is omit-or-whole. The manifest records
`units_total`/`units_retained` per entry plus `omitted_indices`, and wholly
omitted chunks stay outside the citation allowlist. After trimming, the
final messages are confirmed against the real tokenizer/template budget
(fixed system/history/context/question text plus output/thinking reserves),
always counting the selected final-order candidate — a retrieval-order
count never certifies a `stable_cache` prompt (issue #368);
every count and recount carries the request's selected reasoning effort and
matching thinking template kwargs, identical to completion. This state stays
local to the request; shared tokenizer instances and Settings are not mutated.
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
browser completion behavior. #372 retains those gaps (#365 closed 2026-10-02);
no model run is claimed by this documentation audit.

The shared core consumes typed operations from `core_ports`. Application
composition captures one `AgentResources` view per request (see the lifespan
section) and passes the validated physical
collection through the retrieval operation's settings. The model adapter owns
sync/async calling compatibility and validates stream token/terminal fields.
Both buffered and fallback-stream completions require `ChatResult`; bare strings
are rejected rather than assigned a successful finish or invented usage. Test
doubles implement the same structured contract. The core always awaits a
`ChatResult`; synchronous tooling keeps the boundary client interface.
Malformed terminals remain incomplete generation errors, never successful finals.
Closing a core stream closes its upstream operation without closing the shared
model client. [Architecture](architecture.md#boundary-map) owns the dependency
and capability restrictions.

<a id="http-model-contract"></a>
## HTTP/model fallback and lifecycle policy

**Status:** implemented call shapes with tests; semantic answer/completion limits
are above. **Authority:** ADR-0001/0004, #363 and existing transport contracts;
#397 corrects the overly broad old “never retry” summary. **Decision owners:**
`HttpxLLMClient`, `VllmTokenizer`, `HttpReranker`, `http_client` and lifespan.

`LLM_REASONING_EFFORT_SIMPLE` and `LLM_REASONING_EFFORT_COMPLEX` accept `low`,
`medium`, `high` and explicit `xhigh`; their defaults are `medium` and `medium` (#917; `xhigh` until 2026-10-23, #777).
The default lives in `config.Settings`, mirrored by the air-gap preflight
(`model_config.REASONING_EFFORT_DEFAULTS`) and set explicitly in the chart
`models.reasoning.reasoningEffortSimple/Complex` values, so a rendered pod carries
`medium`; tests pin the three in step. An operator may still select `xhigh`.
Shared-core overrides accept the same values. The selected effort reaches the
reasoning HTTP request verbatim for buffered, streamed and permitted fallback
calls; `high` is never translated to `xhigh`, and an omitted direct-client effort
remains omitted. Support depends on the served chat template: the stock
[Qwen3.8-27B template](https://huggingface.co/Qwen/Qwen3.8-27B/blob/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0/chat_template.jinja)
accepts `low`, `medium` and `xhigh` when thinking, so that profile must explicitly
select compatible settings ([#707](https://github.com/yonatan895/qdrant-pdf-rag/issues/707)).
`LLM_REASONING_EFFORTS` declares the efforts the served template accepts
(default `low,medium,xhigh`, the stock Qwen3.8 set; the local 8 GB lane pins `low,medium,high`). Settings and
air-gap preflight refuse a simple/complex effort outside it, naming the field
([#748](https://github.com/yonatan895/qdrant-pdf-rag/issues/748)). The console
renders exactly these choices (`xhigh` as "Extra high") and refuses any other
before a model call: `/ui/chat` shows a fixed banner and `/ui/chat/stream`
returns `422 unsupported_reasoning_effort`. A stale saved browser choice keeps
the server's default; no effort is remapped. There is still no public
answer/chat request field. Tokenizer requests retain their existing message and
generation flags; this change does not establish effort-dependent token counts.

| Operation | Current permitted recovery / failure boundary |
|---|---|
| Reasoning transport | Sync/async reasoning pools use `retries=0`; no connection-level automatic repeat |
| Buffered `achat` / `_chat_sync` without `LLM_STREAM` | One streamed request assembled into the buffered result (#868); no recovery POST, as before. An upstream error or malformed frame, a missing `[DONE]` or a missing explicit finish is `TruncatedStreamError`; an empty `stop` completion is an empty result; `length`/`content_filter` is returned as that finish. Total wait is `answer_timeout_s`, expiry `model_timeout` |
| Buffered `achat` / `_chat_sync` with `LLM_STREAM` | Empty content *with a `stop` finish*, caught stream/protocol failures, missing `[DONE]`, upstream error/malformed frame, or missing explicit finish lead to one non-streaming POST; accumulated content is discarded, even if a prefix was buffered. The POST must itself carry an explicit finish, have no top-level error, and have valid string content (issue #365). Never recovered (issue #868): a stream that ended with an explicit non-`stop` finish (`length`, `content_filter`) and no content is returned as that finish, and an `httpx` timeout (`answer_timeout_s` spent) propagates as `model_timeout` instead of starting a second ask |
| Client-visible `chat_stream` | No-content (`stop` finish) or pre-output stream failure (malformed first frame, pre-output error, missing finish/done before any emitted token) can make one non-streaming ask; malformed/rejected/empty/no-finish fallback fails. Missing `[DONE]`, an upstream error frame, a malformed frame, or `[DONE]` without an explicit finish after actually emitted content raises truncation; no replay after those tokens. The shared core applies the same gate to the terminal `done` item itself: a falsy/missing `done` finish, or a token-only stream with no `done`, raises truncation and never synthesizes `stop` (issue #365). A validated stream with zero content deltas and an explicit non-`stop` finish ends with that `done` and no recovery POST (issue #868); timeouts propagate |
| Tokenizer | Plan locally, verify whole messages per trim round. No per-chunk RPCs; gateway may lack `/tokenize`. An endpoint that is not served or gives no usable count (404/405/501, a 200 without a count, or any other non-transient non-200 such as 400/401/403) warns once and pins the estimator until restart. A transient failure (transport error, timeout, 408/429/5xx) warns and uses the estimator for `TOKENIZE_RETRY_AFTER_S` (60 s); the first count after that is the one probe, and success resumes remote counts (issue #835). `budget_verified` reads the counting thread's own last count, so a probe on another thread never confirms an estimated prompt |
| Embed/context/health pools | Bounded Settings connect-only retries, no generic POST replay policy |
| Rerank | Configured score/rerank endpoint order plus alternate endpoint fallback; exhaustion fails closed; a batch rejected on both legs with HTTP 4xx (not 5xx/transport/shape) is re-scored once with passages cut to `RERANK_RETRY_PASSAGE_MAX_CHARS` (2000), and a failing retry fails closed (issue #664); [retrieval](retrieval.md) owns dispatch |
| Condensation | Optional reasoning call; failure returns the raw latest query, not a fabricated condensed result |
| Admission / total deadline (issue #374) | Opt-in (`request_*`, default off): refusal is `503 overloaded` before any work; expiry cancels async legs and is `504 deadline_exceeded` / a terminal SSE `error` event; sync worker-thread legs are bounded by their own timeouts, not interrupted (§6a). No retry or fallback is added |
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
`tests/test_agent_api.py`, `tests/test_chat_api.py`,
`tests/test_probe_gateway.py`, `tests/test_failfast.py`.
The approved local/CI gateway-only LiteLLM adapter exception is owned by
[deployment policy](deploy.md#deployment-policy); production gateway protection
remains the platform team's responsibility.


### Exact local prompt counting

The local LiteLLM launcher provides a model-scoped `/tokenize` passthrough to
the reasoning backend. Chat counts include the assistant generation prefix and
exclude additional special tokens, matching the pinned vLLM chat defaults.
Verification counts the final selected block order and sends that same message
list, including the final bounded empty-evidence recount. Invalid/negative/bool
counts pin the process to estimation; restart after fixing the endpoint.
`probe_gateway.py --require-tokenizer` distinguishes this degraded path from
exact-count acceptance. Estimation remains available for gateways without this
optional endpoint; it is never reported as verified. Model/window/output-budget
defaults are unchanged.

The Helm operator input `CHAT_CONDENSE_ENABLED` maps to
`models.reasoning.condenseEnabled`; both default to false. Explicit local
follow-up acceptance is described in [the real-corpus runbook](local-real-corpus.md).


### Model-operation stream grammar

The legacy `ModelAdapter` and typed answer-core seam accept zero or more token
items followed by exactly one done item and end-of-iteration. A token's delta
must be a string (an empty string is valid); a terminal's finish must be a
nonempty string. Missing legacy usage defaults to empty `TokenUsage`; explicitly
supplied usage must be `TokenUsage`, including when falsy. Optional timing is
`None` or an integer, never a boolean. There are no ignorable metadata or error
mapping events at this seam: unknown kinds, error-bearing frames, duplicate
terminals and all postterminal items raise `TruncatedStreamError`. End of stream
without a terminal retains the explicit missing-finish outcome. A later `stop`
cannot replace an earlier `length` finish. The core emits a final only after
iteration ends cleanly, and validates typed-adapter events defensively too.

Wire metadata remains the HTTP model client's responsibility before it emits
these normalized events. Existing buffered sync/async/string fallback,
pre-emission retry and post-emission no-replay policy are unchanged. Cleanup
closes the operation, never the shared model client.

<a id="source-observations"></a>
## Source observations (live z/OS state)

Decision owner: [ADR-0003](adr/0003-zowe-mcp-read.md). Status: contract
accepted, **capability off and unwired**. No endpoint calls
`live_state.fetch_live`, `zowe_mcp_enabled=false` is the default, and the
manual-only POC makes zero source calls. Search never calls an LLM; routing is
deterministic and trap queries stay on the manuals path.

- **Surface:** the agent HTTP API is query traffic (no PUT/PATCH/DELETE; POST
  only on search/answer/chat and console chat) plus the console rating forward
  (`/ui/feedback`, `/ui/feedback/form`; ADR-0005, default off). It never
  writes the corpus or a source.
  With feedback enabled, the agent's only stored-data writes go to the
  feedback service: a bounded rating from those two routes, and one answer
  record (question, answer, citations, telemetry) per finalized answer from
  the answer, chat and console routes (never `/v1/search`). The source port is
  separate from manual evidence and is not a query/command proxy.
- **Approved operation:** `job_status` for one exact job id. The other three
  bridge tools (`dataset_read`, `uss_read`, `jes_spool_read`) are allowlisted in
  code but not approved for use; a new tool is a new ADR.
- **Observation shape (contract for #91, not yet implemented):** exact `target`,
  `acquired_at` (agent clock), `observed_at` (source time or null), `outcome`
  in `complete | truncated | partial | not_found | unavailable | no_target |
  dry_run | denied`, plus `truncated`. Limits: 2 calls, `zowe_mcp_max_bytes`,
  `zowe_mcp_timeout_s` per call, no polling, no cross-request cache.
- **Today in code:** `fetch_live` returns `degraded` codes
  (`dry_run`, `not_configured`, `no_target`, `timeout`, `tool_error`,
  `upstream_error`); `job_status` is planned only with an exact job id.
  Failures are fixed codes; logs and spans carry ids, tool names and byte
  counts, never source text.
- **Gate:** enabling needs a named investigation manuals cannot answer plus
  source-owner and site-security approval (ADR-0003 reactivation gate).
