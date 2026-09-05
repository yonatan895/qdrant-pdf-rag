# Retrieval and ranking reference

Owner: this file. Design overview: `docs/architecture.md` §4.3. Eval gates:
`docs/eval.md`. Payload schema: `docs/ingest.md` §8.

> One fact, one owner — this file owns retrieval internals. Code is named by
> module and function, never by line number.

## 1. Query data-flow

`search()` and `async_search()` in `retrieve/query.py` are near-verbatim
twins pinned by a drift-guard test: identical fakes in, identical hits out.
The async twin never runs sync I/O on the event loop — the dense-query,
sparse, and cross-encoder legs go through `asyncio.to_thread`, and the
Qdrant calls ride `isawaitable` shims so test doubles work on both twins.

Stage order for both twins:

1. `parse_query` extracts identifiers; `build_filter` turns them (plus
   product/version) into a Qdrant filter, or `None` when empty.
2. `_resolve_active_reranker` decides whether the rerank leg runs (§6).
3. `_effective_query` applies acronym expansion, if enabled (§7).
   Identifiers, the filter, and the returned `query_kind` always stay on the
   operator's original query.
4. `embedder.dense_query` + `embedder.sparse` embed the (possibly expanded)
   query.
5. `_build_prefetch_requests` issues one dense + one BM25 prefetch in a
   single batched `query_batch_points` call (falling back to two sequential
   `query_points` when the server lacks batching).
6. `rrf_fuse` merges the legs (§4); `rerank_candidates` optionally rescores
   (§6); `diversify_hits` enforces coverage caps (§8).

Output is `(hits, query_kind, timings)` where `query_kind` is `identifier`
or `nl`, and timings carry `embed_ms`/`qdrant_ms` plus `rerank_ms` only when
the rerank leg ran. Nothing in retrieval catches: embed, Qdrant, and rerank
faults propagate, and the agent maps any retrieval fault to the same
`502 upstream_error` on both endpoints.

Each `SearchHit` carries `chunk_id` (the Qdrant point id), `score` (the RRF
value, or the rerank order key after rescoring), `cite` (built by
`format_citation` as `"{doc_id} {title}, {heading}, p. {page}"`, skipping
empties), `heading`, `text`, `doc_id`, `title`, `page_label`, `chunk_type`
(defaulting to `"narrative"` when the payload lacks it), `message_ids`,
optional `product`/`version`, and optional `rerank_score`.

## 2. Prefetch and filters

Filters go in prefetch, never after ANN. Both legs carry the same filter
object and the same 9-field payload projection (`doc_id, title,
heading_path, page_label, chunk_type, product, version, message_ids, text`),
so lexical and semantic candidates are scoped identically before any fusion.

- Prefetch depth is 40 per leg, rising to the rerank-candidate count
  (default 50) when the rerank leg is active.
- `build_filter` ANDs its clauses: exact `product`, exact `version`, and
  `MatchAny` within each identifier field (`doc_id`, `message_ids`,
  `members`). Empty input yields no filter rather than a match-nothing.
- The legs are named `dense` and `bm25`, dense first.

## 3. Identifiers and query kind

`parse_query` unions matches from the raw query and its uppercased copy for
doc numbers and message ids — operators type lowercase, payloads are
canonical-uppercase, and uppercasing only adds word-char matches, so
pure-uppercase queries behave exactly as before. Member extraction stays
case-sensitive: the lowercase `xx` convention (`IEASYSxx`) matches payload
case, and uppercasing would break it.

`query_kind` is `identifier` when any of the three lists is non-empty (a
lone member code flips it too), else `nl`. The kind drives RRF weights and
the rerank bypass — but never the filter shape.

## 4. RRF fusion

Fusion is local, not server-side: Qdrant's RRF exposes no per-leg weights,
and this pipeline needs them. Each leg contributes `weight / (k + rank + 1)`
with 0-based ranks — rank 1 with `k=2` is worth `1/3`, rank 2 `1/4`, so the
top is extremely heavy compared to the industry-default `k=60`.

- Identifier queries fuse with weights `(dense 1.0, sparse 3.0)`; NL
  queries use `(1.0, 1.0)`. The 3× sparse boost is what decides exact-code
  recall, and the small `k` is what makes rank 1 dominate. Both are
  `Settings`-overridable (`rrf_weight_*`, `rrf_k`); without settings the
  module constants apply.
- The non-rerank fuse keeps only `max(limit*3, 24)` candidates — with the
  default limit of 8, 24 of up to 80 prefetched points survive to
  diversification. This truncation is the real recall knob: it has no
  setting, so changing `limit` is what moves it. The rerank path instead
  fuses the top `rerank_candidates` (default 50).

## 5. Injection screen

`screen_query` classifies each query as `answerable` or `trap` with
deterministic regexes over a normalized query — no LLM, no network, no model
to vendor. Normalization lowercases, replaces formatting noise
(backticks, quotes, asterisks, tildes, underscores, `>#()[]{}|\`) with a
space (never deletes, so `ignore_the_excerpts` still separates into words),
and collapses whitespace.

The trap catalog targets ten override shapes with bounded gaps (enough for
one adjective — "the supplied excerpts" — without letting the verb drift
onto unrelated nouns pages away): ignoring the excerpts, ignoring /
disregarding / overriding previous-or-system instructions, `you are now`,
`new instructions`, revealing the system prompt or instructions, answering
`from memory`, reciting keys/secrets/passwords/certificates (wider 40-char
gap), bypassing safety/refusal/guardrail/filter/abstention, and bare
`jailbreak`.

Two ordering rules matter:

- **Trap is checked before identifiers.** A trap carrying `IEASYSxx`-style
  codes still screens as trap.
- **The screen is deliberately narrow — screen ≠ abstain.** Live-state and
  out-of-scope questions (PTF lists, syslogs, nonexistent docs) and
  sibling-competitor traps (nearby message ids, edition suffixes) stay
  `answerable` for retrieval; abstention happens separately in the answer
  layer. The rerank bypass (§6) therefore never starves legitimate traffic.

## 6. Rerank dispatch

Reranking ships default-off. When enabled, the fused top candidates
(default 50) are rescored by a cross-encoder and stably resorted by
`(rerank_score, RRF score, chunk_id)`; length mismatches raise rather than
misalign.

Dispatch (`_resolve_active_reranker`): an explicitly passed reranker wins,
else the flag-built memoized one. Then the bypass applies — and it
**nullifies even an explicitly passed reranker**:

- `trap` queries bypass (injection must never be re-ranked; RRF order
  stands so the trap hard-zero holds with reranking on).
- `identifier` queries bypass (the cross-encoder scores shape-compatibility,
  so on exact-code queries it prefers confident definitions of the *wrong*
  message and buries the right context — observed live with rank 13+
  over RRF rank 1, a gap no rank-fusion constant bridges; the lexical anchor
  is the trustworthy signal here).

Bypassed queries also prefetch at the non-rerank depth — no 50-candidate
fetch for a leg that will not run. The bypass reason lands on the trace.

Reranker construction (`build_reranker`, the single dispatch point): off →
`None`; hash mode → lexical `HashReranker` (token overlap over query length
plus log passage length); an embed/rerank base URL → `HttpReranker`, which
tries `{base}/v1/score` (or `{base}/score` when the base already ends in
`/v1`) with batch size 32 and a 5s timeout, falls back to the
Cohere/TEI-style `/rerank` shape on narrowly-defined failures
(bad-status/request/shape errors), and raises strictly when the second leg
fails or returns mismatched/out-of-bounds indexes. Anything else raises
fail-closed. Rerank input passages are `product/version/doc_id` header plus
title/heading/text (not raw chunk text), and the query is the
acronym-expanded form.

The memoized reranker is keyed by `id(settings)` — a memory address, so a
mutated `Settings` object never rebuilds it and GC address reuse can alias
it. Treat settings as immutable once serving starts. (Tracked as a code
issue; this section documents actual behavior.)

## 7. Acronym rewrite

Deterministic, model-free, default-off (`acronym_expansion_enabled`). Known
mainframe acronyms gain inline appositions (`IPL` → `IPL (Initial Program
Load)`), which helps the dense leg (semantics) and the sparse leg
(expansion terms). The glossary is versioned (`acronyms_v1.json`, 181
entries) and loaded once.

Deliberately narrow, in this order: whole-word tokens only (slashed forms
like `SMP/E`, `TCP/IP`, `PR/SM` match as one token); two-letter tokens
expand only from the reviewed allowlist `{CF, LU, EE}` (bare two-letter
matches are overwhelmingly false positives); identifier-shaped tokens never
expand; identifier-heavy queries bypass rewriting entirely via
`should_rewrite`, enforced inside `expand_query` itself so no future caller
can dilute the exact-code path; ambiguous tokens (`DSN`, `PDF`, `AIX`,
`CA`, `MAP`, `MQ`, …) are excluded from the glossary rather than guessed —
exclusion beats wrong expansion.

Known gap (tracked as a code issue): the code comment claims trap queries
never reach rewriting because the screen runs first, but the trap check is
not enforced on this path — an identifier-free trap query containing an
acronym is still expanded for the embed legs. This section documents actual
behavior.

## 8. Diversification

`diversify_hits` caps coverage at 1 chunk per page and 3 per document
(`Settings`-overridable), because near-duplicate consecutive chunks would
otherwise monopolize the prompt slots. Backfill runs in three phases:

1. Take candidates respecting **both** caps.
2. Relax the per-doc cap first, still respecting per-page.
3. Sort leftovers by (pages already taken, −score) and **force-fill** —
   Phase 3 can violate the per-page cap to reach `limit`.

So "max 1 per page" holds until the pool runs dry, then completeness wins.
Callers must not assume page-diversity in short pools.

## Appendix A — Identifier patterns

One shared pattern set (`regexes.py`) feeds both ingest payloads and query
parsing: a token matches on both sides or neither — there is never an
asymmetric false positive, only term matching. Tune before widening any of
these; widening changes both corpus extraction and query parsing at once.

- **Doc numbers** `DOCNO_RE`: 2–4 letters, 2 digits, dash, 4 digits, optional
  2-digit suffix (`SA22-7592-05`). The filename-anchored variant used at
  parse time is deliberately separate (see `docs/ingest.md` §2) so pattern
  changes cannot churn point ids.
- **Message ids** `MSG_RE`: classic 3-letter form (`IEA500I`) plus the
  families it misses, added from real-corpus measurement — CICS `DFH` cards
  with 0–2 middle letters and no trailing severity (`DFHAC2006`,
  `DFHSI1579`), IMS `DFS` codes with optional severity (`DFS058` alongside
  `DFS058I`), and 4-letter-prefix codes (`DSNA670I`, `TSSC001E`).
- **Members** `MEMBER_RE`: 3–8 uppercase letters ending in `xx` (PARMLIB
  convention: `IEASYSxx`) or 2 digits. Case-sensitive by design.
- **Front/back-matter titles** `SKIP_ALWAYS_RE`: notices, trademarks,
  reader comments, bibliography, copyright, index — matched at title end
  because IBM titles carry prefixes ("Appendix A. Notices").
