# ADR-0013: Research mode, a bounded model-directed search loop on the answer path

- **Status:** proposed (issue [#1315](https://github.com/yonatan895/qdrant-pdf-rag/issues/1315),
  tracker #1191). Nothing here is decided until the maintainer merges it, and
  accepting it enables nothing: no code exists, the mode would ship default-off,
  and a default change is a later, separate concern (D9).
  **Dependency, not assumed:** `ROADMAP.md` still closes corrective retrieval
  (#86) with "do not re-propose". Open PR #1313 proposes opening that verdict
  for re-measurement and is **not merged**. This record can be accepted only
  after #1313 merges, or with an explicit maintainer ruling recorded here that
  #86's gate does not cover this mode (open point 1).
  If accepted it **amends** the candidate-selection contract in
  [docs/agent.md](../agent.md#candidate-selection) and V2 of
  [ADR-0007](0007-open-webui-docs-front-end.md), and it **confirms without
  change** the AGENTS.md search boundary, [ADR-0003](0003-zowe-mcp-read.md) and
  [ADR-0010](0010-team-tools-on-open-webui.md), as "Clauses" lists.
- **Context:**
  - Today one retrieval feeds one answer: zero model-directed searches per
    answer. A diagnostic question often needs a second look once the first
    evidence names a message, parameter or component (#1315; #1310's misses
    were diagnostic rows).
  - #86 tried a second retrieval triggered by a fused-score margin with acronym
    expansion as the retry, and measured it negative. A follow-up query written
    by the reasoning model after it read the first evidence was never built or
    measured.
  - **What the repository says about determinism (checked at 0d14b11d).** There
    is no general rule that the docs path is deterministic. The rules that
    exist are narrower: search never calls an LLM (C1, C4, C6); the live-read
    router is deterministic and the model never chooses a read (C3, C5); and
    named components are deterministic by their own contracts (the injection
    screen C14, `derive_retrieval_query`, the multi-path split, the coding
    classifier, follow-up anchoring). The answer path already makes optional
    model calls before retrieval or packing: follow-up condensation (C10), the
    pasted-image read (C9, C13) and candidate selection (C11). So #1315's
    "docs path is deterministic" rule does not exist as written; this record
    confirms the narrower rules and amends only the clauses marked below.
  - Three-part test: the loop's place, bounds and trust rules are costly to
    change once clients depend on the mode; a reader of ADR-0010's rejected
    "repository-owned tool loop" would not expect a loop here; and each
    decision below had a real alternative.

## Decision

A **research mode**: on an opted-in request, after the ordinary first retrieval,
the reasoning model may ask for a bounded number of further searches before the
one answer call. Each decision names the alternative considered.

1. **D1, the loop lives on the answer path; search stays LLM-free.** It is a
   fixed stage in the shared answer core, after the existing search plan
   (literal, condensed or anchored, unchanged) and before packing. Each
   follow-up search is the same retrieval call the first round uses.
   `/v1/search`, the MCP knowledge search, the ops CLI search and ingest never
   run it and never call a model. It is not a tool loop and not a framework: the
   stage has one fixed shape (plan, screen, search, repeat), one operation
   (manuals search), no registry and no new dependency, like the condensation
   and selection stages. *Alternatives:* a model-written query inside
   retrieval (rejected: breaks C1); Open WebUI's native tool loop with search as
   a tool (rejected: the Docs model stays tool-free, C7, and the loop would
   leave the agent's screens, citation validation and trace); an orchestration
   library (forbidden, C2).
2. **D2, the model emits a search query string and nothing else.** A planning
   call returns either "done" or one query string, in a strict format the
   build issue fixes; any other reply ends the loop (D6). Every follow-up
   search carries the request's own `product`/`version` scope unchanged
   and the server's own limits. The model cannot set or widen a filter, pick a
   collection, endpoint, tool, hit count, effort or number of rounds. The
   query never reaches the live-read planner (C3 holds with or without the
   proposed dataset-read pilot of open PR #1295), the coding classifier, or
   the retrieval-derived prompt decisions (complexity, resolved-code note,
   identifier anchoring, no-hits wording); those keep reading the user's
   searched text only. The question the answer model reads stays the user's
   own words. *Alternatives:* model-chosen filters or structured search
   arguments (rejected: manual text could steer scope, C5); several queries
   per round (rejected for the first release: the bound stays easy to state).
3. **D3, every follow-up query is untrusted and passes the user-query
   screens.** It is model output derived from retrieved manual text, so it is
   checked exactly as a caller's query before any embed or search: the
   unsearchable predicate (`chat_turn.is_unsearchable_query`), the injection
   screen (`screen_query`; a trap is never searched) and the retrieval query
   length limit on its effective text, with the embed-input bound still
   enforced by the embedder. A query that fails a screen, or repeats a text
   already searched in this request, is dropped and the loop ends; it is
   never repaired, truncated or retried. If the user's own searched text
   screens as a trap, the mode makes no planning call at all (the selection
   bypass rule, C12). A query that names an identifier does **not** bypass:
   that is the case the mode exists for. The planning prompt presents
   excerpts as data, not instructions. *Alternative:* sanitise and clip, as
   the image read does (C13); rejected because that text comes from the
   user's own image, while this text comes from the corpus.
4. **D4, hard bounds, each with a fixed outcome.** All are bounded Settings
   with ceilings in code; the values are open point 3.
   - *Rounds:* a maximum number of follow-up rounds, one planning call and at
     most one search each. At the bound: stop, answer.
   - *Searches:* a maximum total per request, the first retrieval included (an
     anchored first round counts as one). At the bound: stop, answer.
   - *Stage budget:* one time budget across all follow-up rounds, not renewed
     per round, sized so the answer call keeps its time. At expiry: stop,
     answer with the evidence held.
   - *Request deadline:* the existing `request_deadline_s` covers the whole
     request (C17). It is opt-in and off by default (C16), so Settings refuses
     the mode at startup unless it is set. Expiry is the existing
     `504 deadline_exceeded` or terminal SSE error; no partial answer is
     invented.
   - *Evidence:* first-round hits are never dropped or reordered by a later
     round. New hits are appended in round order, deduplicated by chunk id,
     up to a fixed packed-excerpt cap; the existing prompt budget still trims
     from the tail. A round that adds no new chunk ends the loop.
   *Alternatives:* a deadline only (rejected: C16); a loop that runs until the
   model says done (the issue's forbidden outcome).
5. **D5, the user selects the mode per request; never the default in the
   first release.** Two gates: an operator switch (default off, no production
   manifest sets it before D9 is met) and a per-request field on the chat
   routes, separate from `reasoning_depth`/`reasoning_effort` so that
   `thorough` keeps its meaning. With the operator switch off, a request that
   asks for the mode gets a fixed 4xx with a stable code before retrieval or
   any model call, never a silent ordinary answer. In Open WebUI the docs Pipe
   lists one extra entry only when the switch is on, and that entry sends the
   field; the Pipe still makes no retrieval, planning or model call of its own
   (C8). Neither the model nor retrieved text can turn the mode on.
   *Alternatives:* attach it to `thorough` (rejected: a silent behaviour
   change for existing users); a score-based automatic trigger (rejected:
   that is #86's measured-negative trigger, C20).
6. **D6, failure and fallback follow the HTTP/model contract.** The planning
   call is an optional reasoning call like condensation and selection (C15):
   one attempt, no retry, under the stage budget and the request deadline. A
   transport error, timeout, empty or malformed reply ends the loop, and the
   answer uses the evidence held; no new client error and no fabricated query.
   A failed follow-up search also ends the loop with the evidence held (open
   point 5). First-round failures are unchanged. Cancellation and the request
   deadline propagate. This is a new fallback under C18, so the build adds its
   row to the policy table with bounded Settings and evidence.
7. **D7, evidence and citations.** A follow-up hit is an ordinary retrieval
   hit: every packed excerpt keeps its stored citation, appears in
   `supplied_evidence` and the citation allowlist, and passes the existing
   chunk screening. The model's query text is never evidence and never
   grounds a citation; citation validation is unchanged.
8. **D8, a text-free trace per round.** The root span records the mode, the
   rounds run, the searches made and one stop reason from a closed
   vocabulary; each round is a child span with counts only (hits returned,
   new hits). Follow-up query text is manual-derived, so it never enters
   spans, logs or the feedback record (C19). Names are fixed by the build
   issue with `docs/metric-contract.md`.
9. **D9, measured before any default or site enablement.** The dev golden
   cannot rank this (#1310: near-saturated at document level). The decision
   instrument is the generated passage-level gold of #1317, which does not
   exist yet, from a batch that passed its spot-check rate. Required: a
   paired off/on run with per-query attribution reporting the share of
   answers that pack the gold passage (by query class), model calls,
   searches and latency per answer, the stop-reason distribution, and
   unchanged trap and abstain behaviour (zero planning calls on trap rows).
   Stand-in lane results are labelled as such and are relative to that
   lane's model; they justify continuing, not a default. No baseline, gate
   or frozen set changes. Making the mode a default is its own approved
   concern.
10. **D10, what this does not reopen.**
    - *HyDE and step-back (#82, C21):* those replaced the user's query before
      the first retrieval; step-back failed by dropping the product tokens
      that select the manual. Here the first retrieval is always the user's
      query, its hits are never displaced (D4), and the scope filter is fixed
      (D2). #82 stays closed under its own gate.
    - *Margin-triggered retry (#86, C20):* no score decides a re-search and
      acronym expansion is not a retry. The trigger is the user's opt-in; the
      variant is a model-written query, which #86 never measured. This record
      claims no evidence that the variant helps: D9 is how that is found
      out, and whether #86's gate nevertheless covers the mode is open point 1.
    - *Tools beyond search* (Team models own them, ADR-0010) and the
      cross-reference graph (#94).

## Clauses relied on or amended

Quoted from 0d14b11d. "Confirmed" means the clause keeps its text and this mode
is designed to satisfy it.

| # | Where | Clause | Effect |
|---|---|---|---|
| C1 | `AGENTS.md`:69-70 | "Search never calls an LLM; answers/chat/console use the designated reasoning model." | Confirmed (D1) |
| C2 | `AGENTS.md`:77 | "No LangChain, LlamaIndex, second vector DB, new orchestration framework," | Confirmed; reading in D1, open point 2 |
| C3 | `docs/adr/0003-zowe-mcp-read.md`:22-24 | "never chosen by the model or by manual text: a deterministic router plans, and only a closed allowlist of read operations can run." | Confirmed (D2) |
| C4 | `docs/adr/0003-zowe-mcp-read.md`:95-96 | "Search never calls an LLM; routing is deterministic and trap queries always route `manual`." | Confirmed (D1, D3) |
| C5 | `docs/adr/0003-zowe-mcp-read.md`:133-134 | "*Generic tool/connector framework or LLM-chosen tool calls:* the model would pick sources and untrusted text could steer reads." | Confirmed; the reason D2 and D3 exist |
| C6 | `docs/agent.md`:2122-2123 | "Search never calls an LLM; routing is deterministic and trap queries stay on the manuals path." | Confirmed |
| C7 | `docs/adr/0010-team-tools-on-open-webui.md`:37-39, 185-186 | "The **Docs model stays tool-free**: it is still only the hash-checked docs Pipe over the agent, with no tool, tool server or Function attached." and "**A repository-owned tool loop.** Rejected: it would be a new orchestration layer in this repository." | Confirmed; reading in D1, open point 2 |
| C8 | `docs/adr/0007-open-webui-docs-front-end.md`:83-85, 132-133 | "The only models are the docs Pipe's entries: one Function, whose answer-depth entries Open WebUI lists as models `mainframe_rag_docs.<depth>`, each answering only through the agent" and "The Pipe is stateless. It does no retrieval, prompt assembly, tool planning or LLM calls of its own." | **V2 amended** (one more entry that is not an answer depth, D5); V3 confirmed |
| C9 | `docs/adr/0007-open-webui-docs-front-end.md`:119-121 | "With pasted images on, the agent makes one extra reasoning-model call per image (its reading feeds retrieval);" | Precedent |
| C10 | `docs/agent.md`:192-193 | "one low-effort reasoning call rewrites the latest turn into a standalone search query" | Precedent (`CHAT_CONDENSE_ENABLED`, default off) |
| C11 | `docs/agent.md`:1681-1682, 1691-1692 | "The stage never rewrites the query and never retrieves again." and "Stage order is fixed for later work: select, then (if ever built) expand." | Precedent (#1309). **Amended:** the first sentence stays true of selection; a research request does not run selection in the first release (open point 4) |
| C12 | `docs/agent.md`:1671 | "or a searched text (the query or an anchored antecedent) screens as a trap" | Relied on (D3) |
| C13 | `docs/agent.md`:1988 | "The read text is model output, so its control characters become spaces and it is clipped" | Precedent; D3 is stricter |
| C14 | `docs/retrieval.md`:371-373 | "`screen_query` classifies each query as `answerable` or `trap` with deterministic regexes over a normalized query — no LLM, no network, no model to vendor." | Relied on (D3) |
| C15 | `docs/agent.md`:1843 | "Optional reasoning call; failure returns the raw latest query, not a fabricated condensed result" | Relied on (D6) |
| C16 | `docs/agent.md`:1845 | "Opt-in (`request_*`, default off): refusal is `503 overloaded` before any work; expiry cancels async legs and is `504 deadline_exceeded`" | Relied on (D4) |
| C17 | `docs/agent.md`:1090-1092 | "one budget from handler entry (queue wait included; body decoding/framework validation precedes it) across condense/embed/search/rerank/tokenize (or completion-usage count)/model legs" | Relied on (D4) |
| C18 | `docs/agent.md`:1848-1849 | "This policy describes current operations, not permission to add retries. A new fallback/timeout is a behavior change, with bounded Settings and evidence." | Relied on; the build adds a policy row (D6) |
| C19 | `docs/agent.md`:1252-1253 | "Span attributes carry finite labels, counts, and operator config only" | Relied on (D8) |
| C20 | `ROADMAP.md`:163-166; `docs/retrieval.md`:364 | "**Reopening gate (all required):** a confidence signal with measured hit/miss separation plus a retry variant with measured positive headroom, both with per-query attribution on post-freeze real pools. Until then, do not re-propose." and "margin never decides re-search" | Not changed here; dependency on #1313 (open point 1) |
| C21 | `ROADMAP.md`:118-121 | "**Reopening gate (all required):** per-query attribution shows failures from vocabulary mismatch (query terms absent from relevant docs) on the post-#214/#216 stack; paired A/B on a post-re-ingest real corpus; beats the +1-call latency cost. Until then, do not re-propose." | Not reopened (D10) |

## Consequences

- An opted-in answer costs up to the round bound in extra planning calls and
  searches, and more latency; an ordinary request is byte-identical to today.
- A research answer can hold more excerpts than an ordinary one, so prompt
  budget trimming is hit more often.
- Residual risk: with no product scope set, a follow-up query can add hits
  from another product's manual. They can only follow the first-round hits,
  and D9's attribution measures it.
- Residual risk: a poisoned manual passage can try to steer the follow-up
  query. It can produce only a screened search string inside the user's
  scope, never a read, a tool call or a filter (D2, D3).
- The build updates the owners in the same PR: `docs/agent.md` (request
  field, error code, stage contract, policy row, settings),
  `docs/metric-contract.md`, the Pipe contract and ADR-0007 V2.

## Open points for the maintainer

1. **#86's gate and #1313.** Does "do not re-propose" cover an opt-in,
   model-written follow-up? *Recommendation:* treat it as covered, merge or
   decline #1313 first, and accept this record only after that; do not waive
   the gate inside this ADR.
2. **Is a fixed search-only stage a "repository-owned loop" in ADR-0010's
   sense?** *Recommendation:* no, on the D1 reading (one operation, no tool
   registry, no dependency), recorded here so it is not stretched to tools.
3. **Bound values.** *Recommendation:* at most 2 follow-up rounds, at most 3
   searches per request, ceilings fixed in code; the stage budget and packed
   cap are set from the stand-in measurement, not guessed here.
4. **Research with candidate selection (#1309).** *Recommendation:* exclusive
   per request in the first release (selection bypasses with a fixed reason),
   so each is measured alone; combine only after both have results.
5. **A failed follow-up search.** *Recommendation:* end the loop and answer
   with the evidence held, with a fixed stop reason. The stricter alternative
   is the existing `502 upstream_error` for any retrieval fault.
6. **Routes.** *Recommendation:* the chat routes and the docs Pipe only;
   `/v1/answer` and the console (frozen fallback) get no field in the first
   release.
7. **Does the user see the follow-up queries?** *Recommendation:* no in the
   first release: a fixed status with the search count only, because the
   text is manual-derived model output. Showing it is a later decision.
8. **Planning-call effort.** *Recommendation:* the lowest offered effort, as
   the other auxiliary calls use, with the effort recorded in D9's runs.
9. **ADR number.** 0012 is taken by open PR #1295; this is 0013 and is
   renumbered only on request.
