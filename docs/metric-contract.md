# Metric and trace contract map (backend-independent slice of #529 OBS-3)

Status: investigation queries, not dashboards. Every name, label, operation,
and tag below was captured from the live dev stack (agent on `origin/main`
plus the uncommitted dev-only `METRICS_ENABLED` passthrough; no app-code
change) and every query was verified against that live data: PromQL by
label-existence plus bucket-shape math over parsed exposition, Jaeger lookups
by executing them. No Prometheus runs in dev, so PromQL was not executed by
an engine; no 5xx occurred, so the ERROR-status lookup is contract-derived,
not live-tested.

## Prometheus exposition (`GET /metrics`, opt-in via `metrics_enabled`)

Source names are OTel instrument names; on the wire dots become underscores,
counters gain `_total`, and histograms gain their unit suffix (`_seconds`,
`_milliseconds`; unitless instruments gain nothing). `otel_scope_*` labels
are exporter artifacts (constant per process), not query dimensions.

| OTel instrument | Stored series | Type | Unit | Labels |
|---|---|---|---|---|
| `rag.requests.total` | `rag_requests_total` | counter | `1` | `endpoint`, `query_class`, `outcome`, `verification_state` |
| `rag.request.duration` | `rag_request_duration_seconds_{bucket,count,sum}` | histogram | `s` | same four as the counter |
| `rag.retrieval.hits` | `rag_retrieval_hits_{bucket,count,sum}` | histogram | `1` | `endpoint`, `query_class` only (no outcome) |
| `rag.llm.ttft` | `rag_llm_ttft_milliseconds_{bucket,count,sum}` | histogram | `ms` | `model` only |

Label vocabularies (bounded by construction; anything else never becomes a label):

- `endpoint`: `search`, `answer`, `chat`, `console` (anything else is not counted).
- `query_class`: classifier output (`nl` observed); `unknown` when unclassified.
- `outcome`: `ok` plus the fixed client/server outcome vocabulary (`invalid_request` observed).
- `verification_state`: member of `VERIFICATION_STATES` when known, else the
  key is dropped to the unlabeled series (never emitted empty) — match
  on `= "accepted"`, never on label presence.
- `model`: the deployed reasoning-model id (e.g. `Qwen/Qwen2.5-0.5B-Instruct`).
- `hits`/`ttft` are recorded only when the leg measured them (missing means
  unmeasured, never zero); framework-level 4xx records a counter with no
  duration/hits companions beyond the shared series shape.

Buckets (pinned in `metrics.py`, verified cumulative on the wire):

- duration (s): `0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300`
- ttft (ms): `10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000`
- hits: `0, 1, 2, 3, 5, 8, 13, 21, 40`

Privacy (verified by grep over full exposition): no query text, headings,
doc ids, keys, or tokens appear in any series.

## Trace contract (Jaeger service `mainframe-rag-agent`)

Operations and Kinds (13, matching the OBS-1B tree plus `chat.condense`):

- SERVER roots: `v1.search`, `v1.answer`, `v1.chat`, `ui.chat`
- CLIENT legs: `retrieve.embed`, `retrieve.prefetch`, `retrieve.rerank`, `llm.chat`, `chat.condense`
- INTERNAL stages: `retrieve.search`, `retrieve.rrf`, `retrieve.diversify`, `prompt.build`

Root-span tags by endpoint (all carry `http.request_id` except framework
rejections, which leave no trace — see below):

- `v1.search`: `rag.query_kind`, `rag.hits`, `rag.limit`
- `v1.answer`: plus `rag.stream`, `rag.evidence`, `rag.citations`, `rag.has_script`
- `v1.chat`, `ui.chat`: `rag.stream` (empty-hit runs also set `rag.query_kind`/`rag.hits: 0` on the root)

Stage tags used by the queries: `retrieve.search` (`rag.query_kind`,
`rag.split_mode`, `rag.hits`, `rag.rerank_active`), `llm.chat`
(`llm.model`, `llm.ttft_ms`, `llm.{prompt,completion,total}_tokens`,
`llm.finish_reason`).

## Error-attribution split (verified)

| Failure | Trace | Metric |
|---|---|---|
| Framework validation 422 (e.g. empty query: `AnswerRequest.min_length=1` rejects before the handler runs) | none — no span exists | `rag_requests_total{outcome="invalid_request"}` |
| In-handler 4xx | root ends UNSET (no ERROR tag) | same counter, handler-specific outcome |
| 5xx | root status ERROR (`otel.status_code="ERROR"`) | same counter, server outcome |

Consequence: failed-request investigation starts at the metric
(`outcome!="ok"`), then pivots to traces only when a trace exists.

## Join keys

- Trace lookup: `service=mainframe-rag-agent` + `tags={"http.request_id":"…"}` —
  resolves exactly one trace (executed live).
- Logs carry the same `request_id`; the success response body carries it.
- Metrics have no `request_id` (cardinality law): metric→trace pivots go via
  time window + `endpoint`, never via a label.
- No exemplars are emitted: there is no metric→trace span link to follow.

## Boundary: gateway spans are not ours

The distributed trace also contains gateway spans (`litellm-local` locally).
The local gateway exports them only through an attribute allowlist (#636:
operation, model, token usage, status — no content, key-derived metadata,
events or error text). The production gateway is platform-owned and its span
content is not ours to qualify, so investigation queries MUST still filter
`service=mainframe-rag-agent`. Nothing in this map depends on gateway span
shape.

## Shipped queries

`observability/queries/request-investigation.promql` (M1–M6) and the J1–J3
Jaeger lookups embedded in `tests/test_metric_contract.py::test_jaeger_lookups_documented`
as URL templates. `tests/test_metric_contract.py` pins this whole map in CI:
instrument names/units/label keys, bucket boundaries, and that every
`rag_*` name and label key in the PromQL file exists on the wire shape.
