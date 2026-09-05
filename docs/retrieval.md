# Retrieval and ranking reference

Owner: this file. Design overview: `docs/architecture.md` §4.3. Eval gates: `docs/eval.md`.

> Skeleton for the docs epic (PR-A). Section headings below mark the full
> content that lands in PR-C. Each section documents the query data-flow,
> formulas, thresholds, and ordering rules with `file:line` evidence.
> One fact, one owner — do not duplicate `architecture.md`.

## 1. Query data-flow

Scope: `search()` / `async_search()` twin contract (drift-guard), stage order
(parse → screen → rewrite → embed → prefetch → RRF → rerank → diversify),
`SearchHit` fields, timing keys.

## 2. Prefetch and filters

Scope: batched `query_batch_points` shape (dense + BM25 legs, `using` names),
`query_filter` vs `filter`, sequential fallback, payload projection,
conjunctive filter semantics (`MatchAny` per field), `None`-when-empty.

## 3. Identifiers and query kind

Scope: `parse_query` (doc/message/member extraction, case rules),
`query_kind` identifier-vs-NL switch and everything it drives (RRF weights,
rerank bypass — but not filter shape).

## 4. RRF fusion

Scope: local RRF (not server fusion) rationale, formula, `k=2` rationale,
`[1,3]` vs `[1,1]` weights, fuse oversample `max(limit*3,24)` truncation.

## 5. Injection screen

Scope: normalization, the 10 trap shapes with gap bounds, trap-before-identifier
ordering and why, deliberately-narrow scope (screen ≠ abstain).

## 6. Rerank dispatch

Scope: default-off, candidate counts, bypass precedence (trap > identifier >
explicit), prefetch shrink on bypass, `HttpReranker` `/v1/score` vs `/rerank`
fallback, batching/timeouts, tie-breakers, memoized-reranker lifecycle.

## 7. Acronym rewrite

Scope: default-off, glossary versioning, allowlist/denylist, whole-word rules,
identifier bypass, what feeds embed legs vs rerank input. Known ordering gap
with trap queries is tracked as a code issue; this section documents actual
behavior.

## 8. Diversification

Scope: `max_per_page=1` / `max_per_doc=3` caps, 3-phase backfill semantics
(incl. Phase-3 force-fill), sort keys.

## Appendix A — Identifier patterns

Scope: the shared `regexes.py` catalog (`DOCNO_RE`, `MSG_RE` incl. CICS/IMS
shapes, `MEMBER_RE`, filename vs text variants) and the single-pattern rule
(ingest payload and query parse share patterns — no asymmetric false
positives).
