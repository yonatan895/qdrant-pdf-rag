# ADR-0013: Research mode, a bounded model-directed search loop on the answer path

- **Status:** accepted (issue
  [#1315](https://github.com/yonatan895/qdrant-pdf-rag/issues/1315), tracker
  #1191). On 2026-11-20 the maintainer decided all 16 open points as
  recommended; each is folded into its decision below and listed under "Decided
  open points". Accepted by the maintainer on 2026-11-20 in PR #1322
  (`19d669c1`) after PR #1313 (`0f7eb07c`) merged. Accepting it enables nothing
  by itself: the mode ships default-off (#1325), and a default change is a
  later, separate concern (D9).
  **Dependency (settled):** `ROADMAP.md` previously closed corrective retrieval
  (#86) with "do not re-propose". PR #1313 opened that verdict for
  re-measurement and **merged** (`0f7eb07c`). Decided 2026-11-20 (maintainer,
  points 1 and 14): the #86 and #82 gates are treated as covering this mode and
  are not waived inside this ADR, so #1313 merged first and this record was
  accepted after that (D10). What #1313's merge does and does not
  license is in D9 (point 15).
  **Number.** Decided 2026-11-20 (maintainer, point 11): 0012 is taken by open
  PR #1295; this is 0013 and is renumbered only on request. *Alternative
  considered:* renumbering now.
  If accepted it **amends** V2 of [ADR-0007](0007-open-webui-docs-front-end.md)
  and the candidate-selection contract in
  [docs/agent.md](../agent.md#candidate-selection) (D1, point 4). It **confirms
  without change** the AGENTS.md search boundary and
  [ADR-0003](0003-zowe-mcp-read.md). It confirms
  [ADR-0010](0010-team-tools-on-open-webui.md) and ADR-0007's reading of "new
  orchestration framework" on the reading decided in D1 (point 2; C2, C7). That
  reading covers a fixed search-only stage and is not stretched to tools.
  "Clauses" lists each.
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
   and selection stages.
   **Decided 2026-11-20 (maintainer, point 2):** this fixed search-only stage is
   not an "orchestration layer" in ADR-0010's sense (C7: TD3 and the rejected
   repository-owned tool loop), on the reading above (one operation, no tool
   registry, no dependency). It is recorded here so that the reading is not
   stretched to tools. *Alternative considered:* that it is one; C2 and C7 would
   then forbid the mode as designed, and it would need an explicit amendment of
   ADR-0010 and of ADR-0007's reading, which this record does not make.
   **Decided 2026-11-20 (maintainer, point 4):** research and candidate
   selection (#1309) are exclusive per request in the first release (selection
   bypasses with a fixed reason), so each is measured alone; they are combined
   only after both have results. *Alternative considered:* running both on one
   request in the first release.
   *Alternatives:* a model-written query inside
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
4. **D4, hard bounds, each with one fixed outcome.** All are bounded Settings
   with ceilings in code.
   **Decided 2026-11-20 (maintainer, point 3):** at most 2 follow-up rounds and
   at most 3 searches per request, ceilings fixed in code; the stage budget, the
   packed cap and the planning prompt's excerpt count, excerpt length and reply
   cap are set from the stand-in measurement, not guessed here. *Alternative
   considered:* fixing those values in this record before the measurement.
   - *No first-round evidence:* if the first retrieval returns no hits, the
     mode makes no planning call and no follow-up search, and the existing
     empty-hits short-circuit runs unchanged, before any model call (C23). If
     it returns hits but none yields an excerpt the planning prompt can show,
     there is likewise no planning call, and packing takes the existing
     zero-usable-excerpts path (C23). Neither clause is amended.
   - *Rounds:* a maximum number of follow-up rounds, one planning call and at
     most one search each. At the bound: stop, answer.
   - *Searches:* a maximum total per request, the first retrieval included.
     One search is one retrieval call as the first round makes it: an anchored
     first round counts as one, and a query that the deterministic multi-path
     split turns into its at most two paths counts as one (C24). At the bound:
     stop, answer.
   - *Planning prompt:* the call reads the request's searched texts and a
     fixed-length head of each excerpt held, in evidence order up to a fixed
     excerpt count, with a fixed reply cap. It is planned against the model
     window like any other prompt. If it does not fit, no planning call is
     made, the loop ends and the answer uses the evidence held; the planning
     stage never returns `422 prompt_budget_exceeded`.
     **Decided 2026-11-20 (maintainer, point 16):** the planning call reads
     neither chat history nor image-read text directly. It reads only the
     request's searched texts as the search plan produced them (the literal,
     condensed or anchored text) and the excerpts held: no earlier turn, no
     image and no separate transcription. On an image turn the searched text
     already holds the clipped image reading that feeds retrieval (C9, C13), and
     that is all of it the planner sees. History and image text therefore reach
     the planner only as text the first round already searched and D3 already
     screened. *Alternative considered:* giving the planning call the chat
     history or the image reading directly.
   - *Stage budget:* one time budget across all follow-up rounds, not renewed
     per round. It is checked at two points only, before a planning call
     starts and before a follow-up search starts. One outcome per case:
     - *Expired before a planning call:* stop, answer with the evidence held.
     - *Expires during a planning call:* the call's timeout is the budget
       left, and the model leg is async, so it is cancelled. This is D6's
       planning-call timeout: stop, answer with the evidence held.
     - *Expired when the planning call returns:* the query is discarded
       unsearched; stop, answer with the evidence held.
     - *Expires while a follow-up search is in flight:* the search is not
       abandoned. Its synchronous embed and BM25 legs cannot be interrupted
       (C25), so it runs to its end under its own per-leg timeouts and the
       request deadline. If it succeeds, its new hits are appended and the
       loop ends; if it fails, that is D6's failed follow-up search. The
       budget alone never produces a 502 and never discards a finished
       search.
       **Decided 2026-11-20 (maintainer, point 12):** this rule. *Alternative
       considered:* abandoning the search at expiry and answering with the
       evidence held; that still spends the time and the threads (C25) and
       throws a finished result away.

     The budget does not guarantee the answer call's time. The stage can
     overrun it by at most one search's per-leg timeouts, and only the request
     deadline bounds the whole request. Settings refuses a stage budget that,
     with that overrun added, is not below the request deadline; an answer
     call that still runs out of time ends as the existing 504.
   - *Request deadline:* the existing `request_deadline_s` covers the whole
     request (C17). It is opt-in and off by default (C16), so Settings refuses
     the mode at startup unless it is set. Expiry is the existing
     `504 deadline_exceeded` or terminal SSE error; no partial answer is
     invented. The supported deployment already ships a 270 s deadline, sized
     from a medium-effort record whose maximum was 232 s (C26). So the startup
     refusal bites only local, CI and stand-in runs that leave the deadline
     unset, and at that effort the loop's headroom there is about 38 s (270
     minus 232), less at a higher effort. The stage budget has to fit inside
     that; resizing the deadline is its own approved concern, not part of
     this record.
   - *Evidence:* first-round hits are never dropped or reordered by a later
     round. New hits are appended in round order, deduplicated by chunk id,
     up to a fixed packed-excerpt cap; the existing prompt budget still trims
     from the tail. A round that adds no new chunk ends the loop.
   *Alternatives:* a deadline only (rejected: C16); a loop that runs until the
   model says done (the issue's forbidden outcome).
5. **D5, the user selects the mode per request; never the default in the
   first release.** Two gates: an operator switch (default off; when a site
   may turn it on is decided below, point 9) and a per-request field on the chat
   routes, separate from `reasoning_depth`/`reasoning_effort` so that
   `thorough` keeps its meaning. With the operator switch off, a request that
   carries the field gets a fixed 4xx with a stable code before retrieval or
   any model call, never a silent ordinary answer. In Open WebUI the docs Pipe
   lists one extra entry only when the switch is on, and that entry sends the
   field; the Pipe still makes no retrieval, planning or model call of its own
   (C8). Neither the model nor retrieved text can turn the
   mode on.
   **Decided 2026-11-20 (maintainer, point 6):** the field exists on the chat
   routes and the docs Pipe only; `/v1/answer` and the console (frozen fallback)
   get no field in the first release. *Alternative considered:* a field on
   `/v1/answer` and the console as well.
   **Decided 2026-11-20 (maintainer, point 10):** the research entry answers at
   the `standard` effort. A Pipe entry sends one value, so research and answer
   depth do not combine in the picker. *Alternative considered:* a research
   entry per depth; that is a later decision.
   **Decided 2026-11-20 (maintainer, point 9):** a site may turn the operator
   switch on for opt-in use before #1317 results exist, as a labelled pilot,
   once a stand-in run shows the bounds hold and trap and abstain behaviour is
   unchanged; quality claims wait for D9, which gates only a default change.
   Merging #1313 does not by itself license the pilot (D9, point 15); the pilot
   rests on this decision and its conditions. *Alternative considered:* no site
   use of the switch until D9's results exist.
   **How the Pipe learns the switch. Decided 2026-11-20 (maintainer, point
   13):** one
   chart value sets both the agent's switch and an environment variable on
   the Open WebUI pod, which the Pipe reads when it lists its entries, as
   pasted images already work (C27). The Pipe stays stateless and never asks
   the agent. One outcome per disagreement:
   - *The Pipe lists the entry and the agent's switch is off* (rollout skew,
     a hand-set environment): the entry sends the field, the agent returns
     the fixed 4xx above and the Pipe shows its fixed text for that code. No
     ordinary answer.
   - *The agent's switch is on and the Pipe does not list the entry:* the
     mode cannot be reached from Open WebUI. Every listed entry answers as
     today and no request carries the field.
   - *A stored chat names the research entry after the switch goes off:* by
     source reading, not yet observed live (C28), Open WebUI drops a model id
     it does not list and selects the default entry. The next message then
     carries no field and gets an ordinary answer under the default entry's
     name. This record accepts that as a visible fallback: the rule above
     binds a request that carries the field, and this one does not. The
     build's browser check has to observe it. *Alternative
     considered:* the Pipe always lists the entry and, with the switch off,
     returns a fixed "research is off" text with no agent call: no fallback
     at all, but every site shows an entry that may never work.
   *Alternatives:* attach it to `thorough` (rejected: a silent behaviour
   change for existing users); a score-based automatic trigger (rejected:
   that is #86's measured-negative trigger, C20).
6. **D6, failure and fallback follow the HTTP/model contract.** The planning
   call is an optional reasoning call like condensation and selection (C15):
   one attempt, no retry, under the stage budget and the request deadline,
   and made only when first-round evidence exists (D4, C23). A
   transport error, timeout, empty or malformed reply ends the loop, and the
   answer uses the evidence held; no new client error and no fabricated query.
   A failed follow-up search is a retrieval fault like any other: the existing
   fixed `502 upstream_error`, identical on every path (C22); point 5 below keeps
   that. A stage-budget expiry
   is never itself a failed search (D4). Cancellation and the
   request deadline propagate. The planning-call fallback is new under C18, so
   the build adds its row to the policy table with bounded Settings and
   evidence.
   **Decided 2026-11-20 (maintainer, point 5):** a failed follow-up search stays
   the existing `502 upstream_error` (C22). *Alternative considered:* ending the
   loop and answering with the first-round evidence; that amends C22 and needs
   its own evidence (C18).
   **Decided 2026-11-20 (maintainer, point 8):** the planning call runs at the
   lowest offered effort, as the other auxiliary calls use, with the effort
   recorded in D9's runs. *Alternative considered:* a higher effort for the
   planning call.
7. **D7, evidence and citations.** A follow-up hit is an ordinary retrieval
   hit: every packed excerpt keeps its stored citation, appears in
   `supplied_evidence` and the citation allowlist, and is packed under the
   same prompt rules as a first-round hit. The model's query text is never evidence and never
   grounds a citation; citation validation is unchanged.
8. **D8, a text-free trace per round.** The root span records the mode, the
   rounds run, the searches made and one stop reason from a closed
   vocabulary; each round is a child span with counts only (hits returned,
   new hits). Follow-up query text is manual-derived, so it never enters
   spans, logs or the feedback record (C19). Names are fixed by the build
   issue with `docs/metric-contract.md`.
   **Decided 2026-11-20 (maintainer, point 7):** the user does not see the
   follow-up queries in the first release: a fixed status with the search count
   only, because the text is manual-derived model output. *Alternative
   considered:* showing the query text; that is a later decision.
9. **D9, measured before any default change.** The dev golden
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
   **Decided 2026-11-20 (maintainer, point 15):** #1313 opens a re-measurement
   only and says adoption still needs a paired A/B. Its merge is enough to
   accept this record (acceptance enables nothing), to build the mode
   default-off and to measure it on the stand-in lane and under this decision.
   It is not enough for the site pilot: users at a site using the mode is use,
   not measurement, so the pilot rests on the explicit decision of point 9 (D5)
   and its conditions, and a default needs this decision's paired run.
   *Alternative considered:* treating #1313's merge as enough for the site pilot
   as well.
10. **D10, what this does not reopen.**
    - *HyDE and step-back (#82, C21):* those replaced the user's query before
      the first retrieval; step-back's recorded failure (PR #175) was dropping
      the product tokens that select the manual. Here the first retrieval is always the user's
      query, its hits are never displaced (D4), and the scope filter is fixed
      (D2). Those are differences, not a ruling.
      **Decided 2026-11-20 (maintainer, point 14):** #82's "do not re-propose"
      gate (C21) is treated as covering a model-written follow-up query, as
      point 1 does for #86, and is not waived inside this ADR. #1313 names #82
      among the ideas it opens, so its merge would settle both. The differences
      above explain why the recorded failure need not recur; they are not a
      waiver. *Alternative considered:* ruling here that the gate does not cover
      a model-written follow-up query.
    - *Margin-triggered retry (#86, C20):* no score decides a re-search and
      acronym expansion is not a retry. The trigger is the user's opt-in; the
      variant is a model-written query, which #86 never measured. This record
      claims no evidence that the variant helps: D9 is how that is found
      out.
      **Decided 2026-11-20 (maintainer, point 1):** #86's "do not re-propose"
      gate (C20) is treated as covering an opt-in, model-written follow-up.
      #1313 is merged or declined first and this record is accepted only after
      that; the gate is not waived inside this ADR. *Alternative considered:* an
      explicit ruling recorded here that the gate does not cover this mode.
    - *Tools beyond search* (Team models own them, ADR-0010) and the
      cross-reference graph (#94).

## Clauses relied on or amended

Quoted from 0d14b11d, with that commit's line numbers. "Confirmed" means the
clause keeps its text and this mode is designed to satisfy it. On this branch
the two "proposed, not in effect" pointers move every later line by two:
`docs/agent.md` after line 1663 (C6, C11 to C13, C15, C16, C18, C27, C28) and
ADR-0007 after line 11 (C7, C8, C9).

| # | Where | Clause | Effect |
|---|---|---|---|
| C1 | `AGENTS.md`:69-70 | "Search never calls an LLM; answers/chat/console use the designated reasoning model." | Confirmed (D1) |
| C2 | `AGENTS.md`:77 | "No LangChain, LlamaIndex, second vector DB, new orchestration framework," | Confirmed on D1's reading (point 2, decided 2026-11-20; see C7) |
| C3 | `docs/adr/0003-zowe-mcp-read.md`:22-24 | "never chosen by the model or by manual text: a deterministic router plans, and only a closed allowlist of read operations can run." | Confirmed (D2) |
| C4 | `docs/adr/0003-zowe-mcp-read.md`:95-96 | "Search never calls an LLM; routing is deterministic and trap queries always route `manual`." | Confirmed (D1, D3) |
| C5 | `docs/adr/0003-zowe-mcp-read.md`:133-134 | "*Generic tool/connector framework or LLM-chosen tool calls:* the model would pick sources and untrusted text could steer reads." | Confirmed; the reason D2 and D3 exist |
| C6 | `docs/agent.md`:2122-2123 | "Search never calls an LLM; routing is deterministic and trap queries stay on the manuals path." | Confirmed |
| C7 | `docs/adr/0010-team-tools-on-open-webui.md`:37-39, 47-48, 185-186, 147; `docs/adr/0007-open-webui-docs-front-end.md`:371-372 | "The **Docs model stays tool-free**: it is still only the hash-checked docs Pipe over the agent, with no tool, tool server or Function attached." and "The loop runs outside this repository's runtime: the repository adds no orchestration layer." and "**A repository-owned tool loop.** Rejected: it would be a new orchestration layer in this repository." and "no repository-owned tool loop exists" and "these rules bind this repository's code, dependencies and answer path." | Confirmed for Team models and the Docs model. For D1's stage, confirmed on the reading decided 2026-11-20 (point 2): a fixed search-only stage is neither a tool loop nor an orchestration layer. The reading is not stretched to tools. On the other reading these clauses would forbid the mode as designed, and it would need an explicit amendment of ADR-0010 and of ADR-0007's reading, which this record does not make |
| C8 | `docs/adr/0007-open-webui-docs-front-end.md`:83-85, 132-133 | "The only models are the docs Pipe's entries: one Function, whose answer-depth entries Open WebUI lists as models `mainframe_rag_docs.<depth>`, each answering only through the agent" and "The Pipe is stateless. It does no retrieval, prompt assembly, tool planning or LLM calls of its own." | **V2 amended** (one more entry that is not an answer depth, D5); V3 confirmed |
| C9 | `docs/adr/0007-open-webui-docs-front-end.md`:119-121 | "With pasted images on, the agent makes one extra reasoning-model call per image (its reading feeds retrieval);" | Precedent |
| C10 | `docs/agent.md`:192-193 | "one low-effort reasoning call rewrites the latest turn into a standalone search query" | Precedent (`CHAT_CONDENSE_ENABLED`, default off) |
| C11 | `docs/agent.md`:1681-1682, 1691-1692 | "The stage never rewrites the query and never retrieves again." and "Stage order is fixed for later work: select, then (if ever built) expand." | Precedent (#1309, `agent/answer_select.py`). **Amended if this record is accepted (D1; point 4, decided 2026-11-20):** the first sentence stays true of selection, and a research request does not run selection in the first release. The stage-order sentence keeps its text: the research stage is not the "expand" it names and takes no place in that order while the two are exclusive. A later record that combines them has to fix where research sits relative to select and expand; this one does not |
| C12 | `docs/agent.md`:1671 | "or a searched text (the query or an anchored antecedent) screens as a trap" | Relied on (D3) |
| C13 | `docs/agent.md`:1988 | "The read text is model output, so its control characters become spaces and it is clipped" | Precedent; D3 is stricter |
| C14 | `docs/retrieval.md`:371-373 | "`screen_query` classifies each query as `answerable` or `trap` with deterministic regexes over a normalized query — no LLM, no network, no model to vendor." | Relied on (D3) |
| C15 | `docs/agent.md`:1843 | "Optional reasoning call; failure returns the raw latest query, not a fabricated condensed result" | Relied on (D6) |
| C16 | `docs/agent.md`:1845 | "Opt-in (`request_*`, default off): refusal is `503 overloaded` before any work; expiry cancels async legs and is `504 deadline_exceeded`" | Relied on (D4) |
| C17 | `docs/agent.md`:1090-1092 | "one budget from handler entry (queue wait included; body decoding/framework validation precedes it) across condense/embed/search/rerank/tokenize (or completion-usage count)/model legs" | Relied on (D4) |
| C18 | `docs/agent.md`:1848-1849 | "This policy describes current operations, not permission to add retries. A new fallback/timeout is a behavior change, with bounded Settings and evidence." | Relied on; the build adds a policy row (D6) |
| C19 | `docs/agent.md`:1252-1253 | "Span attributes carry finite labels, counts, and operator config only" | Relied on (D8) |
| C20 | `ROADMAP.md`:163-166; `docs/retrieval.md`:364 | "**Reopening gate (all required):** a confidence signal with measured hit/miss separation plus a retry variant with measured positive headroom, both with per-query attribution on post-freeze real pools. Until then, do not re-propose." and "margin never decides re-search" | Not changed here and not waived; dependency on #1313 (D10; point 1, decided 2026-11-20) |
| C21 | `ROADMAP.md`:114-115, 118-121 | "the LLM-rewriting remainder (PR #175) is closed unmerged" and "**Reopening gate (all required):** per-query attribution shows failures from vocabulary mismatch (query terms absent from relevant docs) on the post-#214/#216 stack; paired A/B on a post-re-ingest real corpus; beats the +1-call latency cost. Until then, do not re-propose." | Not changed here and not waived; treated as covering a model-written follow-up query (D10; point 14, decided 2026-11-20) |
| C22 | `docs/agent.md`:539 | "Any retrieval or condensation fault, identical on search, answer, chat and console (#985)" | Confirmed (D6; point 5, decided 2026-11-20) |
| C23 | `docs/agent.md`:608-610, 611-612 | "The empty-hits short-circuit happens before prompt build and any LLM call, on both JSON and SSE." and "Retrieved hits with zero usable supplied excerpts (issue #634) take the same path, never a model call" | Confirmed (D4, D6): no planning call without first-round evidence |
| C24 | `docs/retrieval.md`:210-211, 234 | "decomposes comparative and diagnostic queries into at most two retrieval paths" and "first marker only, cap 2 paths." | Relied on (D4: a split query is one search) |
| C25 | `docs/agent.md`:1098-1100 | "**Known gap:** sync legs already running via `asyncio.to_thread` (embed POST, BM25, rerank, prompt build/tokenize RPC) cannot be interrupted" | Relied on (D4 stage budget, point 12) |
| C26 | `docs/agent.md`:1057-1059, 1060-1062 | "The supported deployment ships `REQUEST_DEADLINE_S=270` through the chart value `requestDeadlineSeconds`" and "It is chosen from the medium-effort record (#917: p95 162 s, max 232 s on 126 dev rows), so the supported route sees no 504 at that effort" | Relied on (D4 request deadline) |
| C27 | `docs/agent.md`:2027 | "On only when the Open WebUI pod has `RAG_CHAT_IMAGES=true` (the chart sets it with `models.reasoning.chatImagesEnabled`)." | Precedent (D5, point 13) |
| C28 | `docs/agent.md`:2021; `docs/deploy.md`:2071-2072 | "v0.12.3 refuses an unlisted model id before the Pipe" and "v0.12.3 drops on opening it a model id it does not list and selects `DEFAULT_MODELS` (`Chat.svelte` `loadChat`; source reading, not yet observed live)" | Relied on (D5); not observed live, so the build has to observe it |

## Consequences

- An opted-in answer costs up to the round bound in extra planning calls and
  searches, and more latency; an ordinary request is byte-identical to today.
- A research answer can hold more excerpts than an ordinary one, so prompt
  budget trimming is hit more often; it trims from the tail, so follow-up
  hits go first.
- Residual risk: with no product scope set, a follow-up query can add hits
  from another product's manual. They can only follow the first-round hits,
  and D9's attribution measures it.
- Residual risk: a poisoned manual passage can try to steer the follow-up
  query. It can produce only a screened search string inside the user's
  scope, never a read, a tool call or a filter (D2, D3).
- The build updates the owners in the same PR: `docs/agent.md` (request
  field, error code, stage contract, policy row, settings),
  `docs/metric-contract.md`, the Pipe contract, ADR-0007 V2 and the chart
  value that sets the switch on both sides (D5).

## Decided open points

This record had 16 open points, each with a recommendation. On 2026-11-20 the
maintainer decided all of them: "go ahead with your recommendations". Each
recommendation is the decision, with no change of substance, and sits in the
decision it belongs to together with the alternative considered. No open point
remains. Following PR #1313's merge (`0f7eb07c`, point 1), the maintainer
accepted this record in PR #1322 (`19d669c1`).

| Point | Decision (2026-11-20, maintainer) | Where it lives |
|---|---|---|
| 1 | #86's gate is treated as covering the mode; #1313 is merged or declined first and this record is accepted only after that; the gate is not waived here | Status; D10 (#86); C20 |
| 2 | A fixed search-only stage is not an "orchestration layer" in ADR-0010's sense; the reading is not stretched to tools | D1; C2, C7 |
| 3 | At most 2 follow-up rounds and 3 searches per request, ceilings in code; the other values come from the stand-in measurement | D4 |
| 4 | Research and candidate selection are exclusive per request in the first release; combined only after both have results | D1; C11 |
| 5 | A failed follow-up search stays the existing `502 upstream_error` | D6; C22 |
| 6 | Chat routes and the docs Pipe only; no field on `/v1/answer` or the console in the first release | D5 |
| 7 | The user does not see the follow-up queries in the first release: a fixed status with the search count only | D8 |
| 8 | The planning call runs at the lowest offered effort, recorded in D9's runs | D6 |
| 9 | A site may turn the switch on for opt-in use before #1317 results, as a labelled pilot, once a stand-in run shows the bounds hold and trap and abstain behaviour is unchanged; quality claims wait for D9 | D5 |
| 10 | The research entry answers at the `standard` effort; an entry per depth is a later decision | D5 |
| 11 | The number stays 0013; renumbered only on request | Status (belongs to no D) |
| 12 | A follow-up search in flight at stage-budget expiry finishes, its hits are kept and the loop ends; only a failed search is a 502 | D4 (stage budget); C25 |
| 13 | One chart value sets the switch on both sides; the fixed 4xx when they disagree; the default-entry fallback for a stored chat is accepted as visible | D5; C27, C28 |
| 14 | #82's gate is treated as covering a model-written follow-up query and is not waived here; #1313's merge would settle it with #86 | Status; D10 (#82); C21 |
| 15 | Merging #1313 is enough to accept this record, to build the mode default-off and to measure it on the stand-in lane and under D9; it is not enough for the site pilot, which rests on point 9, and a default needs D9's paired run | D9; Status |
| 16 | The planning call reads only the request's searched texts and the excerpts held: no chat history, image or separate transcription directly | D4 (planning prompt) |
