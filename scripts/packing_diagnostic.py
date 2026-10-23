#!/usr/bin/env python3
"""Offline packing diagnostic (issue #811, phase 0): for golden rows, run the
real retrieval and the real answer-prompt packing with NO LLM and record what
was retrieved, what the planner packed, what it dropped and why.

It answers one question per row: is the gold evidence lost by packing
(retrieved but not in the prompt), or never retrieved? That decides whether
coverage-first packing (phase 1) is justified.

Production code paths only, nothing reimplemented:
  * retrieval is ``mainframe_rag.retrieve.query.search`` at the answer path's
    depth (``SEARCH_LIMIT`` = 8) with settings from the environment;
  * packing is ``answer_core._build_prepared_prompt`` (the one prompt-build
    owner of the JSON, stream and chat surfaces) over an estimator-only
    tokenizer, i.e. ``_build_prompt`` -> ``_plan_packed_excerpts``;
  * a spy on ``_plan_packed_excerpts`` records the exact budget and plan;
  * a hit's drop reason is decided by re-running the same planner on that
    hit alone with an unbounded budget: no result means no unit fits the
    per-chunk cap; a result means only the cross-hit budget break dropped it.

The verification step in production counts tokens with the model server's
tokenizer and may trim further than the estimator; this tool uses the
estimator, so it can only under-report trims (trimmed hits are flagged).

Read-only against Qdrant. The report holds counts, ranks, offsets and
identifiers only: no chunk text, no headings, no answers.

    VENUE=rc QDRANT_URL=http://127.0.0.1:16333 QDRANT_COLLECTION=manuals_current \\
    EMBED_MODE=vllm EMBED_BASE_URL=http://127.0.0.1:8001/v1 \\
    EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B DENSE_DIM=1024 \\
        python scripts/packing_diagnostic.py --classes CMP,TBL,SYS \\
        --profile target --profile w16384:llm_max_model_len=16384 \\
        --out report.json --markdown report.md

Each --profile re-packs the same retrieved hits under a named planner variant
(window, reserves, chunk caps); the default is one `env` profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from mainframe_rag.agent import answer as answer_mod
from mainframe_rag.agent.answer import (
    PackedExcerpt,
    PromptEvidence,
    _entry_anchors,
    _excerpt_header,
    _hit_spans,
    _packed_excerpt,
    _requested_identifiers,
)
from mainframe_rag.agent.answer_core import (
    AnswerCoreDeps,
    AnswerCoreInput,
    _build_prepared_prompt,
)
from mainframe_rag.agent.tokenizer import FallbackTokenizer, estimate_tokens
from mainframe_rag.config import Settings
from mainframe_rag.eval.datasets import GoldenEntry
from mainframe_rag.eval.retrieval import SEARCH_LIMIT
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.query import SearchHit

DEFAULT_CLASSES = ("CMP", "TBL", "SYS")

# Drop reasons (a hit absent from the final prompt has exactly one).
NO_UNIT_FITS_CAP = "no_unit_fits_cap"  # _select_range returned None for the hit
BUDGET_BREAK = "budget_break"  # cut by the break at answer.py's budget check
TRIMMED_AFTER_PLAN = "trimmed_after_plan"  # planned, then removed by verification

# Target outcomes.
NOT_RETRIEVED = "not_retrieved"
RETRIEVED_PACKED = "retrieved_packed"
RETRIEVED_DROPPED = "retrieved_dropped"

# Row categories (priority order).
CAT_PACKING_LOSS = "packing_loss"
CAT_PARTIAL_RETRIEVAL = "partial_retrieval"
CAT_NOT_RETRIEVED = "not_retrieved"
CAT_OK = "ok"


@dataclass
class HitTrace:
    rank: int
    doc_id: str
    chunk_type: str
    chunk_id: str
    gold_doc: bool
    heading_match: bool | None
    entry_ids: list[str]  # requested identifiers whose entry heading is in this hit
    status: str  # packed | packed_truncated | dropped
    mention_ids: list[str] = field(default_factory=list)  # named anywhere in the hit text
    start: int | None = None
    end: int | None = None
    source_chars: int = 0
    units_total: int = 0
    units_retained: int = 0
    entry_ids_in_prompt: list[str] = field(default_factory=list)
    mention_ids_in_prompt: list[str] = field(default_factory=list)
    drop_reason: str | None = None
    est_tokens_alone: int | None = None  # None when no unit fits the cap
    fits_remaining_budget: bool | None = None  # dropped hits only


@dataclass
class TargetTrace:
    # doc | heading | entry-heading (a unit headed by the requested identifier) |
    # entry-mention (only named inside other text: rules older than code-entry
    # extraction) | entry-none (named in no gold-doc hit)
    kind: str
    key: str  # doc id, identifier, or "heading"
    outcome: str
    hit_ranks: list[int] = field(default_factory=list)  # hits that carry the target
    # For a retrieved_dropped target: which phase-1 mechanisms would have put
    # it in the prompt. skip_and_continue: a budget-dropped carrier fits the
    # budget left after the real plan. coverage_pass: an identifier carrier
    # fits an otherwise empty budget (packed first). prioritize_doc_only: the
    # same for a doc/heading target, which the #811 design does not cover.
    # Empty = neither (the carrier is larger than the whole budget, or it is
    # packed but its unit was cut).
    recovered_by: list[str] = field(default_factory=list)


@dataclass
class RowTrace:
    id: str
    row_class: str
    category: str
    query_complexity: str
    requested_ids: list[str]
    budget_tokens: int
    planned_tokens: int
    hits_total: int
    hits_packed: int
    hits_dropped: int
    targets: list[TargetTrace]
    hits: list[HitTrace]
    query_kind: str = ""
    # Requested identifiers (from the query) whose entry heading unit is in a
    # gold-doc hit that was retrieved / that is inside the packed range.
    requested_retrieved: int = 0
    requested_in_prompt: int = 0


def _requested_id_list(query: str) -> list[str]:
    parsed = parse_query(query)
    return sorted({*parsed.message_ids, *parsed.members, *parsed.system_codes})


def _ids_matching_anchors(
    text: str, spans: Sequence[Any], requested: re.Pattern[str] | None, ids: Sequence[str]
) -> list[tuple[str, Any]]:
    """(identifier, anchor span) for every anchor heading in `text`. Anchors
    come from the production ``_entry_anchors``; this only labels which
    identifier each anchor line starts with."""
    out: list[tuple[str, Any]] = []
    for span in _entry_anchors(text, tuple(spans), requested):
        first = text[span.start : span.end].lstrip("\n").split("\n", 1)[0]
        for ident in ids:
            if re.match(r"\W*" + re.escape(ident) + r"(?!\w)", first, re.IGNORECASE):
                out.append((ident, span))
    return out


def _mentions(text: str, ids: Sequence[str]) -> list[tuple[str, int, int]]:
    """(identifier, start, end) of every whole-token occurrence in `text`."""
    out: list[tuple[str, int, int]] = []
    for ident in ids:
        for m in re.finditer(r"(?<!\w)" + re.escape(ident) + r"(?!\w)", text, re.IGNORECASE):
            out.append((ident, m.start(), m.end()))
    return out


class _PlanSpy:
    """Records the real ``_plan_packed_excerpts`` call (args and result)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], list[PackedExcerpt]]] = []
        self._real = answer_mod._plan_packed_excerpts

    def __call__(self, *args: Any, **kwargs: Any) -> list[PackedExcerpt]:
        result = self._real(*args, **kwargs)
        self.calls.append((args, result))
        return result


def pack_prompt(
    query: str, hits: list[SearchHit], settings: Settings, complexity: str | None = None
) -> tuple[Any, _PlanSpy]:
    """Build the prompt exactly as the answer path does, with the estimator
    tokenizer, spying on the planner. Returns (PreparedPrompt, spy)."""
    from mainframe_rag.agent.answer import classify_query_complexity

    complexity = complexity or classify_query_complexity(query)
    deps = AnswerCoreDeps(
        settings=settings,
        llm=None,  # type: ignore[arg-type]  # not used by prompt building
        retrieve=None,  # type: ignore[arg-type]
        tokenizer=FallbackTokenizer(),
    )
    spy = _PlanSpy()
    with mock.patch.object(answer_mod, "_plan_packed_excerpts", spy):
        prepared = asyncio.run(
            _build_prepared_prompt(
                AnswerCoreInput(query=query), deps, hits, complexity, "low", None
            )
        )
    return prepared, spy


def trace_row(
    row_id: str,
    entry: GoldenEntry,
    hits: list[SearchHit],
    settings: Settings,
    query_kind: str = "",
) -> RowTrace:
    query = entry.query
    prepared, spy = pack_prompt(query, hits, settings)
    complexity = answer_mod.classify_query_complexity(query)
    evidence: PromptEvidence = prepared.evidence
    final = {e.prompt_index: e for e in evidence.entries}

    requested = _requested_identifiers(query)
    ids = _requested_id_list(query)
    gold_docs = set(entry.expected_doc_ids)
    heading = (entry.expected_heading or "").lower()

    budget_tokens = 0
    planned: dict[int, PackedExcerpt] = {}
    plan_args: tuple[Any, ...] = ()
    if spy.calls:
        plan_args, plan_result = spy.calls[-1]
        budget_tokens = plan_args[1]
        planned = {p.index: p for p in plan_result}
    planned_tokens = sum(
        estimate_tokens(f"{_excerpt_header(p.index, p.hit)}\n{p.body}") for p in planned.values()
    )
    remaining = budget_tokens - planned_tokens

    traces: list[HitTrace] = []
    for rank, hit in enumerate(hits, 1):
        text = hit.text.strip()
        spans = _hit_spans(hit, text)
        labelled = _ids_matching_anchors(text, spans, requested, ids)
        entry_ids = sorted({ident for ident, _ in labelled})
        mentions = _mentions(text, ids)
        trace = HitTrace(
            rank=rank,
            doc_id=hit.doc_id,
            chunk_type=hit.chunk_type,
            chunk_id=hit.chunk_id,
            gold_doc=hit.doc_id in gold_docs,
            heading_match=(heading in hit.heading.lower()) if heading else None,
            entry_ids=entry_ids,
            mention_ids=sorted({ident for ident, _, _ in mentions}),
            status="dropped",
            source_chars=len(text),
            units_total=len(spans),
        )
        ev = final.get(rank)
        if ev is not None:
            trace.status = "packed_truncated" if ev.truncated else "packed"
            trace.start, trace.end = ev.start_char, ev.included_chars
            trace.units_retained = ev.units_retained
            trace.entry_ids_in_prompt = sorted(
                {
                    ident
                    for ident, span in labelled
                    if ev.start_char <= span.start and span.end <= ev.included_chars
                }
            )
            trace.mention_ids_in_prompt = sorted(
                {
                    ident
                    for ident, lo, hi in mentions
                    if ev.start_char <= lo and hi <= ev.included_chars
                }
            )
        else:
            trace.drop_reason = _drop_reason(rank, hit, plan_args, planned, trace)
            if trace.drop_reason == BUDGET_BREAK:
                trace.fits_remaining_budget = (
                    trace.est_tokens_alone is not None and trace.est_tokens_alone <= remaining
                )
        traces.append(trace)

    targets = _targets(entry, query, ids, traces, budget_tokens)
    category = _category(targets)
    entry_targets = [t for t in targets if t.kind.startswith("entry-")]

    return RowTrace(
        id=row_id,
        row_class=row_id.split("-")[0],
        category=category,
        query_complexity=complexity,
        requested_ids=ids,
        budget_tokens=budget_tokens,
        planned_tokens=planned_tokens,
        hits_total=len(traces),
        hits_packed=sum(1 for t in traces if t.status != "dropped"),
        hits_dropped=sum(1 for t in traces if t.status == "dropped"),
        targets=targets,
        hits=traces,
        query_kind=query_kind,
        requested_retrieved=sum(1 for t in entry_targets if t.outcome != NOT_RETRIEVED),
        requested_in_prompt=sum(1 for t in entry_targets if t.outcome == RETRIEVED_PACKED),
    )


def _drop_reason(
    rank: int,
    hit: SearchHit,
    plan_args: tuple[Any, ...],
    planned: dict[int, PackedExcerpt],
    trace: HitTrace,
) -> str:
    """Why `hit` is absent from the final prompt. A hit the plan kept but the
    verification trim removed is flagged; otherwise the real planner is
    re-run on this hit alone with an unbounded budget to tell a unit that
    cannot fit the per-chunk cap from a hit the cross-hit budget cut."""
    if rank in planned:
        return TRIMMED_AFTER_PLAN
    solo = answer_mod._plan_packed_excerpts(
        [hit], 10**9, *plan_args[2:]
    )  # same caps/complexity/requested as the real plan
    if not solo:
        return NO_UNIT_FITS_CAP
    sole = _packed_excerpt(
        rank, hit, hit.text.strip(), solo[0].start, solo[0].end, solo[0].required_end
    )
    trace.est_tokens_alone = estimate_tokens(f"{_excerpt_header(rank, hit)}\n{sole.body}")
    return BUDGET_BREAK


def _outcome(retrieved: list[HitTrace], packed: list[HitTrace]) -> str:
    if not retrieved:
        return NOT_RETRIEVED
    return RETRIEVED_PACKED if packed else RETRIEVED_DROPPED


def _target(
    kind: str, key: str, rel: list[HitTrace], covered: list[HitTrace], budget_tokens: int
) -> TargetTrace:
    outcome = _outcome(rel, covered)
    recovered_by: list[str] = []
    if outcome == RETRIEVED_DROPPED:
        cut = [t for t in rel if t.drop_reason == BUDGET_BREAK]
        if any(t.fits_remaining_budget for t in cut):
            recovered_by.append("skip_and_continue")
        if any(t.est_tokens_alone is not None and t.est_tokens_alone <= budget_tokens for t in cut):
            # Fits an empty budget, so packing it FIRST would work. The #811
            # coverage pass does that for requested identifiers only; for a
            # doc or heading target it would be a wider design (reported
            # separately so the two are not conflated).
            recovered_by.append("coverage_pass" if kind.startswith("entry-") else "prioritize_doc_only")
    return TargetTrace(kind, key, outcome, [t.rank for t in rel], recovered_by)


def _targets(
    entry: GoldenEntry, query: str, ids: list[str], traces: list[HitTrace], budget_tokens: int
) -> list[TargetTrace]:
    """The gold evidence a good answer needs, at the level the row can name.

    * heading row   -> one target: a gold-doc hit whose heading matches. A row
                       that also requests a message id needs a unit headed by
                       it (issue #922): a heading shared by a whole chapter
                       matches every hit, so it stands in for the entry only
                       when the heading names no requested id, and a mention
                       alone does not satisfy the entry target;
    * comparison    -> one target per expected doc plus one per requested
                       identifier (the cross-hit coverage premise of #811);
    * other row with a requested identifier -> one target per identifier
                       (a gold-doc hit with a unit headed by it, else one
                       naming it; the packed range must cover that text);
    * otherwise     -> one target per expected doc.
    """
    gold = set(entry.expected_doc_ids)
    is_cmp = (entry.id or "").startswith("CMP") or entry.query_class == "comparative"
    heading = (entry.expected_heading or "").lower()
    out: list[TargetTrace] = []
    need_head = False  # a mention does not satisfy the target (issue #922)

    def in_prompt(t: HitTrace) -> bool:
        return t.status != "dropped"

    if heading:
        # Message rows (issue #922): a chapter-title heading that names the id
        # matches every hit of the chapter, so only a unit headed by the id
        # counts as the entry. The heading target stays when it names none.
        msg_ids = parse_query(query).message_ids
        if not any(i.lower() in heading for i in msg_ids):
            rel = [t for t in traces if t.gold_doc and t.heading_match]
            out.append(_target("heading", "heading", rel, [t for t in rel if in_prompt(t)], budget_tokens))
        if not msg_ids:
            return out
        ids = list(msg_ids)
        need_head = True
    elif is_cmp or not ids:
        for doc in sorted(gold):
            rel = [t for t in traces if t.doc_id == doc]
            out.append(_target("doc", doc, rel, [t for t in rel if in_prompt(t)], budget_tokens))
    if ids:
        for ident in ids:
            pool = [t for t in traces if not gold or t.gold_doc]
            heads = [t for t in pool if ident in t.entry_ids]
            if heads:
                covered = [t for t in heads if ident in t.entry_ids_in_prompt]
                out.append(_target("entry-heading", ident, heads, covered, budget_tokens))
                continue
            seen = [t for t in pool if ident in t.mention_ids]
            covered = [t for t in seen if ident in t.mention_ids_in_prompt]
            if need_head and seen:
                # Named only inside other text, e.g. a running header: the
                # entry itself was never retrieved.
                out.append(TargetTrace("entry-mention", ident, NOT_RETRIEVED, [t.rank for t in seen]))
                continue
            out.append(
                _target("entry-mention" if seen else "entry-none", ident, seen, covered, budget_tokens)
            )
    return out


def _category(targets: list[TargetTrace]) -> str:
    outcomes = [t.outcome for t in targets]
    if not outcomes:
        return CAT_NOT_RETRIEVED
    if RETRIEVED_DROPPED in outcomes:
        return CAT_PACKING_LOSS
    if NOT_RETRIEVED in outcomes:
        return CAT_NOT_RETRIEVED if RETRIEVED_PACKED not in outcomes else CAT_PARTIAL_RETRIEVAL
    return CAT_OK


def summarize(rows: list[RowTrace]) -> dict[str, Any]:
    """Per row class: row categories, target outcomes by kind, drop reasons,
    and how many budget-dropped hits would still have fit the leftover
    budget (the skip-and-continue signal)."""
    per_class: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "rows": 0,
            "categories": Counter(),
            "targets": defaultdict(Counter),
            "drop_reasons": Counter(),
            "budget_dropped_fits_remaining": 0,
            "dropped_targets_recovered_by": Counter(),
            "requested_ids": 0,
            "requested_retrieved": 0,
            "requested_in_prompt": 0,
        }
    )
    for row in rows:
        cls = per_class[row.row_class]
        cls["rows"] += 1
        cls["categories"][row.category] += 1
        for t in row.targets:
            cls["targets"][t.kind][t.outcome] += 1
            if t.outcome == RETRIEVED_DROPPED:
                for mech in t.recovered_by or ["neither"]:
                    cls["dropped_targets_recovered_by"][mech] += 1
        for h in row.hits:
            if h.drop_reason:
                cls["drop_reasons"][h.drop_reason] += 1
                if h.fits_remaining_budget:
                    cls["budget_dropped_fits_remaining"] += 1
        cls["requested_ids"] += len(row.requested_ids)
        cls["requested_retrieved"] += row.requested_retrieved
        cls["requested_in_prompt"] += row.requested_in_prompt
    return {
        name: {
            **{k: (dict(v) if isinstance(v, Counter) else v) for k, v in cls.items() if k != "targets"},
            "targets": {kind: dict(c) for kind, c in cls["targets"].items()},
        }
        for name, cls in sorted(per_class.items())
    }


def render_markdown(rows: list[RowTrace], summary: dict[str, Any]) -> str:
    lines = [
        "| Row | Category | Q | Hits packed/dropped | Targets (kind:key=outcome) | Dropped hits (rank:reason) | Requested units retrieved/in prompt |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        targets = "; ".join(f"{t.kind}:{t.key}={t.outcome}" for t in r.targets) or "-"
        drops = (
            ", ".join(
                f"{h.rank}:{h.drop_reason}{'+fits' if h.fits_remaining_budget else ''}"
                for h in r.hits
                if h.drop_reason
            )
            or "-"
        )
        unit = (
            f"{r.requested_retrieved}/{r.requested_in_prompt} of {len(r.requested_ids)}"
            if r.requested_ids
            else "n/a"
        )
        lines.append(
            f"| {r.id} | {r.category} | {r.query_complexity[0]} | {r.hits_packed}/{r.hits_dropped} "
            f"| {targets} | {drops} | {unit} |"
        )
    lines += ["", "| Class | Rows | Categories | Target outcomes | Drop reasons | Dropped but fits leftover | Dropped targets recoverable by | Requested ids: retrieved/in prompt of total |", "|---|---|---|---|---|---|---|---|"]
    for name, cls in summary.items():
        tg = "; ".join(f"{k}: {v}" for k, v in cls["targets"].items()) or "-"
        lines.append(
            f"| {name} | {cls['rows']} | {cls['categories']} | {tg} | {cls['drop_reasons']} "
            f"| {cls['budget_dropped_fits_remaining']} | {cls['dropped_targets_recovered_by']} "
            f"| {cls['requested_retrieved']}/{cls['requested_in_prompt']} of {cls['requested_ids']} |"
        )
    return "\n".join(lines) + "\n"


def select_entries(
    entries: list[GoldenEntry], classes: Sequence[str], ids: Sequence[str]
) -> list[GoldenEntry]:
    wanted_ids = {i.upper() for i in ids}
    wanted_cls = {c.upper() for c in classes}
    out = []
    for e in entries:
        eid = (e.id or "").upper()
        if wanted_ids:
            if eid in wanted_ids:
                out.append(e)
        elif eid.split("-")[0] in wanted_cls:
            out.append(e)
    return out


def _representation(client: Any, settings: Settings) -> dict[str, Any]:
    """The serving gate's verdict, recorded rather than enforced: a refusal
    (or a gate that cannot run) is evidence about the corpus, not a reason to
    stop a packing diagnostic. `physical` is set only for a servable
    generation; retrieval otherwise uses the configured collection name."""
    from mainframe_rag.eval.retrieval import serving_generation

    try:
        generation = serving_generation(client, settings)
    except Exception as exc:  # noqa: BLE001 - recorded, not hidden
        return {
            "servable": False,
            "outcome": "gate_error",
            "details": [type(exc).__name__],
            "physical": None,
        }
    return {
        "servable": generation.servable,
        "outcome": generation.outcome,
        "details": list(generation.details),
        "physical": generation.physical if generation.servable else None,
    }


PLANNER_KEYS = (
    "llm_max_model_len",
    "llm_reserved_output_tokens",
    "llm_thinking_reserve_tokens_simple",
    "llm_thinking_reserve_tokens_complex",
    "llm_token_safety_margin",
    "llm_max_chunk_tokens_narrative",
    "prompt_max_chunk_chars",
    "prompt_max_chunk_chars_complex",
)


def parse_profile(spec: str) -> tuple[str, dict[str, int]]:
    """`name` or `name:key=int,key=int`: a named planner-settings variant.
    Keys must be planner settings (PLANNER_KEYS); values are integers."""
    name, _, rest = spec.partition(":")
    overrides: dict[str, int] = {}
    for part in filter(None, rest.split(",")):
        key, eq, raw = part.partition("=")
        if not eq or key not in PLANNER_KEYS:
            raise ValueError(f"profile {name!r}: {part!r} is not a planner key=int override")
        overrides[key] = int(raw)
    if not name:
        raise ValueError("profile needs a name")
    return name, overrides


def apply_overrides(settings: Settings, overrides: dict[str, int]) -> Settings:
    """Validated copy: the Settings bounds still apply to every override."""
    return Settings.model_validate({**settings.model_dump(), **overrides})


def render_report(profiles: dict[str, dict[str, Any]]) -> str:
    parts = []
    for name, body in profiles.items():
        parts.append(f"### Profile `{name}`\n\nPlanner: {json.dumps(body['planner'])}\n")
        parts.append(render_markdown(body["_rows"], body["summary"]))
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--golden", type=Path, default=_REPO / "evals" / "golden.jsonl")
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES), help="row id prefixes")
    parser.add_argument("--ids", default="", help="explicit row ids (overrides --classes)")
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="name[:key=int,...] planner variant; repeat. Default: one 'env' profile "
        "with the settings from the environment. Retrieval runs once per row.",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--markdown", type=Path, default=None, help="write the table here")
    args = parser.parse_args(argv)

    from qdrant_client import QdrantClient

    from mainframe_rag.config import load_settings
    from mainframe_rag.eval.datasets import (
        DatasetError,
        load_golden,
        require_rc_for_collection,
        require_rc_for_golden,
    )
    from mainframe_rag.ingest.embed import build_embedder
    from mainframe_rag.retrieve.query import search as retrieve_search

    settings = load_settings()
    try:
        profile_specs = [parse_profile(spec) for spec in (args.profile or ["env"])]
        profile_settings = {
            name: apply_overrides(settings, overrides) for name, overrides in profile_specs
        }
        require_rc_for_golden([args.golden])
        require_rc_for_collection(settings.qdrant_collection)
        entries = select_entries(
            load_golden(args.golden),
            [c for c in args.classes.split(",") if c],
            [i for i in args.ids.split(",") if i],
        )
    except (DatasetError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if not entries:
        print("FAIL: no golden rows selected", file=sys.stderr)
        return 2

    client = QdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=settings.qdrant_timeout_s
    )
    embedder = build_embedder(settings)
    # Record representation drift; never bypass it silently. Retrieval runs
    # against the physical collection when servable, else the configured name.
    representation = _representation(client, settings)
    collection = representation["physical"] or settings.qdrant_collection
    if not representation["servable"]:
        print(
            f"warn: representation not servable (outcome {representation['outcome']!r}: "
            f"{', '.join(representation['details'])}); diagnosing packing on the configured "
            "collection anyway and recording the drift",
            file=sys.stderr,
        )

    rows: dict[str, list[RowTrace]] = {name: [] for name in profile_settings}
    errors: list[dict[str, str]] = []
    for entry in entries:
        row_id = entry.id or "?"
        try:
            hits, kind, _timings = retrieve_search(
                client, embedder, collection, entry.query, limit=SEARCH_LIMIT, settings=settings
            )
            for name, prof in profile_settings.items():
                rows[name].append(trace_row(row_id, entry, hits, prof, kind))
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the run
            errors.append({"id": row_id, "error": type(exc).__name__})
            print(f"warn: row {row_id} failed: {type(exc).__name__}", file=sys.stderr)

    profiles: dict[str, dict[str, Any]] = {}
    for name, prof in profile_settings.items():
        profiles[name] = {
            "planner": {
                **{key: getattr(prof, key) for key in PLANNER_KEYS},
                "tokenizer": "estimator-only (FallbackTokenizer)",
            },
            "summary": summarize(rows[name]),
            "rows": [asdict(r) for r in rows[name]],
            "_rows": rows[name],
        }
    report = {
        "collection": settings.qdrant_collection,
        "physical": representation["physical"],
        "representation": representation,
        "embed_model": settings.embed_model,
        "dense_dim": settings.dense_dim,
        "rerank_enabled": settings.rerank_enabled,
        "search_limit": SEARCH_LIMIT,
        "profiles": {n: {k: v for k, v in b.items() if k != "_rows"} for n, b in profiles.items()},
        "errors": errors,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    table = render_report(profiles)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(table)
    print(table)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
