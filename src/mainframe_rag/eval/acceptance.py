"""Independent release set + pre-registered absolute acceptance (issue #367).

Owns three things and nothing else:

* the SME-authored release-set format (typed JSONL records with provenance and
  adjudication fields) and its fail-closed validator;
* the pre-registered criteria file (``evals/acceptance-criteria.json``) and its
  statistical rule (point estimate or Wilson bound, repeats, flip tolerance);
* the scorer that applies the criteria to adjudicated per-case outcomes and
  reports pass/fail/insufficient per criterion.

Authoring and adjudication are human work outside this repository's tuning
loop; this module only refuses to score what is not attributable. The set is
read through ``datasets.read_release_set_text`` (RC venue + sha256 pin) and is
refused by every golden reader, so it can never become a tuning dataset.
Running the candidate and recording outcomes stay with the existing runners
and the reviewer; this module never reaches a model or storage service.

Exit codes of ``main``: 0 certified accepted; 1 certifying run rejected or
incomplete; 2 refusal (venue, pin, malformed input, overlap with tuning sets)
or a non-certifying result (proposed criteria or stand-in profile). A
non-certifying result is never a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mainframe_rag.eval.datasets import (
    QUERY_CLASSES,
    DatasetError,
    VenueError,
    read_golden_text,
    read_release_set_text,
    resolve_venue,
)

SCHEMA = 1
DEFAULT_CRITERIA_PATH = Path("evals/acceptance-criteria.json")
CRITICAL_KINDS = ("scope", "release", "protocol", "access", "false_completion", "provenance")
FAILURE_STAGES = (
    "none",
    "extraction",
    "retrieval",
    "evidence_omitted",
    "context_exhaustion",
    "protocol",
    "unsupported_answer",
    "infrastructure",
)
_HEX64 = re.compile(r"[0-9a-f]{64}")


class AcceptanceError(RuntimeError):
    """Input cannot be used under the acceptance contract (CLI exit 2)."""


def _nonblank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _iso_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be an ISO date (YYYY-MM-DD)") from exc
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Release set
# --------------------------------------------------------------------------


class Independence(_Strict):
    authored_outside_tuning_loop: Literal[True]
    saw_system_outputs: Literal[False]
    statement: str = Field(min_length=1)


class Manifest(_Strict):
    record: Literal["manifest"]
    set_id: str
    version: str
    stage: Literal["pilot", "release"]
    criteria_sha256: str
    created_at: str
    corpus_revision: str
    authors: list[str] = Field(min_length=1)
    adjudicators: list[str] = Field(min_length=1)
    independence: Independence

    v_text = field_validator("set_id", "version", "corpus_revision")(_nonblank)
    v_date = field_validator("created_at")(_iso_date)

    @field_validator("criteria_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not _HEX64.fullmatch(v):
            raise ValueError("must be a lowercase sha256 hex digest")
        return v

    @field_validator("authors", "adjudicators")
    @classmethod
    def _people(cls, v: list[str]) -> list[str]:
        return [_nonblank(x) for x in v]


class Evidence(_Strict):
    doc_id: str
    release: str
    physical_page: int = Field(ge=1)
    locator: str
    excerpt_sha256: str | None = None  # hash of the adjudicated passage; never the text

    v_text = field_validator("doc_id", "release", "locator")(_nonblank)

    @field_validator("excerpt_sha256")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is not None and not _HEX64.fullmatch(v):
            raise ValueError("must be a lowercase sha256 hex digest")
        return v


class Provenance(_Strict):
    author: str
    authored_at: str
    source: str
    release: str | None = None

    v_text = field_validator("author", "source")(_nonblank)
    v_date = field_validator("authored_at")(_iso_date)


class Adjudication(_Strict):
    adjudicator: str
    adjudicated_at: str
    verdict: Literal["accepted"]
    notes: str | None = None

    v_text = field_validator("adjudicator")(_nonblank)
    v_date = field_validator("adjudicated_at")(_iso_date)


class ReleaseCase(_Strict):
    record: Literal["case"]
    id: str
    query: str
    query_class: Literal[QUERY_CLASSES]  # type: ignore[valid-type]
    expected_behavior: Literal["answer", "abstain"]
    abstain_reason: Literal["insufficient_evidence", "wrong_premise"] | None = None
    expected_doc_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    required_conditions: list[str] = Field(default_factory=list)
    must_not_assert: list[str] = Field(default_factory=list)
    critical_probe: Literal[CRITICAL_KINDS] | None = None  # type: ignore[valid-type]
    provenance: Provenance
    adjudication: Adjudication

    v_text = field_validator("id", "query")(_nonblank)

    @model_validator(mode="after")
    def _shape(self) -> ReleaseCase:
        if self.provenance.author.casefold() == self.adjudication.adjudicator.casefold():
            raise ValueError("author and adjudicator must be different people")
        if self.expected_behavior == "answer":
            if self.abstain_reason is not None or self.must_not_assert:
                raise ValueError("answer cases take no abstain_reason/must_not_assert")
            if not self.evidence or not self.expected_doc_ids or not self.required_facts:
                raise ValueError("answer cases require evidence, expected_doc_ids and required_facts")
            if {e.doc_id for e in self.evidence} != set(self.expected_doc_ids):
                raise ValueError("expected_doc_ids must equal the evidence doc ids")
        else:
            if self.abstain_reason is None or not self.must_not_assert:
                raise ValueError("abstain cases require abstain_reason and must_not_assert")
            if self.expected_doc_ids or self.evidence or self.required_facts:
                raise ValueError("abstain cases take no expected docs, evidence or required facts")
        return self


class ReleaseSet(BaseModel):
    manifest: Manifest
    cases: list[ReleaseCase]

    def by_id(self) -> dict[str, ReleaseCase]:
        return {c.id: c for c in self.cases}


def normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


def parse_release_set(text: str) -> ReleaseSet:
    """Validate one release set; any defect raises ``AcceptanceError``."""
    manifest: Manifest | None = None
    cases: list[ReleaseCase] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
            kind = obj.get("record") if isinstance(obj, dict) else None
            if kind == "manifest":
                if manifest is not None or cases:
                    raise ValueError("exactly one manifest record, before all cases")
                manifest = Manifest.model_validate(obj)
            elif kind == "case":
                if manifest is None:
                    raise ValueError("manifest record must come first")
                cases.append(ReleaseCase.model_validate(obj))
            else:
                raise ValueError("unknown or missing record type")
        except (ValueError, ValidationError) as exc:
            raise AcceptanceError(f"release set line {number}: {_short(exc)}") from exc
    if manifest is None:
        raise AcceptanceError("release set has no manifest record")
    if not cases:
        raise AcceptanceError("release set has no cases")
    ids = Counter(c.id for c in cases)
    dup = sorted(i for i, n in ids.items() if n > 1)
    if dup:
        raise AcceptanceError(f"duplicate case ids: {dup}")
    queries = Counter(normalize_query(c.query) for c in cases)
    if any(n > 1 for n in queries.values()):
        raise AcceptanceError("duplicate queries (case/whitespace-folded)")
    authors = {a.casefold() for a in manifest.authors}
    adjudicators = {a.casefold() for a in manifest.adjudicators}
    for case in cases:
        if case.provenance.author.casefold() not in authors:
            raise AcceptanceError(f"case {case.id}: author is not declared in the manifest")
        if case.adjudication.adjudicator.casefold() not in adjudicators:
            raise AcceptanceError(f"case {case.id}: adjudicator is not declared in the manifest")
    return ReleaseSet(manifest=manifest, cases=cases)


def find_overlaps(release: ReleaseSet, tuning_queries: Sequence[str]) -> list[str]:
    """Case ids whose normalized query also appears in a tuning dataset."""
    seen = {normalize_query(q) for q in tuning_queries}
    return sorted(c.id for c in release.cases if normalize_query(c.query) in seen)


def load_release_set(path: Path | str) -> tuple[ReleaseSet, str]:
    """RC-venue, sha-pinned read plus validation; returns the set and its hash."""
    try:
        text, digest = read_release_set_text(path)
    except VenueError:
        raise
    except DatasetError as exc:
        raise AcceptanceError(str(exc)) from exc
    return parse_release_set(text), digest


# --------------------------------------------------------------------------
# Pre-registered criteria
# --------------------------------------------------------------------------

RATE_METRICS = frozenset(
    {"answer_pass", "abstain_pass", "completion_rate", "false_refusal_rate", "evidence_supplied_rate", "flip_rate"}
)
COUNT_METRICS = frozenset({"critical_failures", "fabrication_count", "case_count"})
_METRIC_BEHAVIOR = {
    "answer_pass": "answer",
    "false_refusal_rate": "answer",
    "evidence_supplied_rate": "answer",
    "abstain_pass": "abstain",
    "fabrication_count": "abstain",
}


class Criterion(_Strict):
    id: str
    metric: str
    scope: str = "all"  # all | answer | abstain | answer:<class> | abstain:<class>
    op: Literal[">=", "<="]
    threshold: float = Field(ge=0)
    statistic: Literal["point", "wilson_lower", "wilson_upper"] = "point"
    min_n: int = Field(default=1, ge=1)
    rationale: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> Criterion:
        if self.metric not in RATE_METRICS | COUNT_METRICS:
            raise ValueError(f"unknown metric {self.metric!r}")
        behavior, _, qclass = self.scope.partition(":")
        if behavior not in ("all", "answer", "abstain") or (qclass and qclass not in QUERY_CLASSES):
            raise ValueError(f"invalid scope {self.scope!r}")
        if behavior == "all" and qclass:
            raise ValueError("scope 'all' takes no class")
        needed = _METRIC_BEHAVIOR.get(self.metric)
        if needed and behavior != needed:
            raise ValueError(f"{self.metric} requires scope {needed!r}")
        if self.metric in RATE_METRICS and self.threshold > 1:
            raise ValueError("rate thresholds are within [0, 1]")
        if self.metric in COUNT_METRICS and self.statistic != "point":
            raise ValueError("count metrics use the point statistic")
        pairs = {"point": (">=", "<="), "wilson_lower": (">=",), "wilson_upper": ("<=",)}
        if self.op not in pairs[self.statistic]:
            raise ValueError("wilson_lower pairs with >=, wilson_upper with <=")
        return self


class Stage(_Strict):
    repeats: int = Field(ge=1)
    statistical_claim: bool
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    criteria: list[Criterion] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Stage:
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion ids must be unique within a stage")
        if any(c.metric == "flip_rate" for c in self.criteria) and self.repeats < 2:
            raise ValueError("flip_rate needs at least two repeats")
        return self


class Criteria(_Strict):
    schema_version: Literal[1] = Field(alias="schema")
    criteria_id: str
    status: Literal["proposed", "registered"]
    registered_by: str | None = None
    registered_at: str | None = None
    certifying_profiles: list[Literal["production", "standin"]]
    stages: dict[Literal["pilot", "release"], Stage]
    note: str = ""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _registered(self) -> Criteria:
        if self.status == "registered":
            if not self.registered_by or not self.registered_at:
                raise ValueError("registered criteria name who registered them and when")
            _iso_date(self.registered_at)
        if "standin" in self.certifying_profiles:
            raise ValueError("a stand-in profile can never certify release acceptance")
        return self


def load_criteria(path: Path | str) -> tuple[Criteria, str]:
    try:
        data = Path(path).read_bytes()
        return Criteria.model_validate_json(data), hashlib.sha256(data).hexdigest()
    except (OSError, ValueError) as exc:  # ValidationError is a ValueError
        raise AcceptanceError(f"criteria unavailable or invalid: {_short(exc)}") from exc


# --------------------------------------------------------------------------
# Outcomes (one adjudicated record per case and repeat)
# --------------------------------------------------------------------------


class Profile(_Strict):
    kind: Literal["production", "standin"]
    label: str
    reasoning_model: str = ""
    embed_model: str = ""
    rerank_model: str = ""  # "none" is an explicit statement; blank is missing
    corpus_collection: str = ""
    corpus_revision: str = ""
    image: str = ""
    git_sha: str = ""
    temperature: float | None = None

    @model_validator(mode="after")
    def _identity(self) -> Profile:
        if self.kind == "production":
            missing = [
                k
                for k in ("reasoning_model", "embed_model", "rerank_model", "corpus_collection",
                          "corpus_revision", "image", "git_sha")
                if not getattr(self, k).strip()
            ]
            if missing or self.temperature is None:
                raise ValueError(f"production profile must record: {missing + ['temperature'] * (self.temperature is None)}")
        return self


class RunRecord(_Strict):
    record: Literal["run"]
    set_sha256: str
    criteria_sha256: str
    repeats: int = Field(ge=1)
    executed_at: str
    profile: Profile

    v_date = field_validator("executed_at")(_iso_date)


class Outcome(_Strict):
    record: Literal["outcome"]
    case_id: str
    repeat: int = Field(ge=1)
    status: Literal["scored", "skipped", "error"]
    adjudicator: str
    completed: bool = False
    refused: bool = False
    useful: bool | None = None
    supported: bool | None = None
    traceable: bool | None = None
    evidence_supplied: bool | None = None
    appropriate_abstention: bool | None = None
    fabricated_instruction: bool | None = None
    critical_failures: list[Literal[CRITICAL_KINDS]] = Field(default_factory=list)  # type: ignore[valid-type]
    failure_stage: Literal[FAILURE_STAGES] = "none"  # type: ignore[valid-type]

    v_text = field_validator("case_id", "adjudicator")(_nonblank)


def parse_outcomes(text: str) -> tuple[RunRecord, list[Outcome]]:
    run: RunRecord | None = None
    outcomes: list[Outcome] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line, parse_constant=_reject_constant)
            kind = obj.get("record") if isinstance(obj, dict) else None
            if kind == "run":
                if run is not None or outcomes:
                    raise ValueError("exactly one run record, before all outcomes")
                run = RunRecord.model_validate(obj)
            elif kind == "outcome":
                outcomes.append(Outcome.model_validate(obj))
            else:
                raise ValueError("unknown or missing record type")
        except (ValueError, ValidationError) as exc:
            raise AcceptanceError(f"outcomes line {number}: {_short(exc)}") from exc
    if run is None:
        raise AcceptanceError("outcomes have no run record")
    return run, outcomes


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def wilson_bound(successes: float, n: int, confidence: float, side: Literal["lower", "upper"]) -> float:
    """One-sided Wilson score bound for a proportion (successes may be fractional)."""
    if n <= 0:
        raise ValueError("n must be positive")
    z = NormalDist().inv_cdf(confidence)
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denom = 1 + z * z / n
    return (centre - margin) / denom if side == "lower" else (centre + margin) / denom


def _passed(case: ReleaseCase, o: Outcome | None) -> bool:
    if o is None or o.status != "scored" or not o.completed or o.critical_failures:
        return False
    if case.expected_behavior == "answer":
        return not o.refused and bool(o.useful and o.supported and o.traceable)
    return bool(o.appropriate_abstention) and o.fabricated_instruction is False


def _cell(metric: str, case: ReleaseCase, o: Outcome | None) -> float:
    if metric in ("answer_pass", "abstain_pass"):
        return float(_passed(case, o))
    if metric == "critical_failures":
        return float(len(o.critical_failures)) if o else 0.0
    if metric not in ("completion_rate", "false_refusal_rate", "evidence_supplied_rate", "fabrication_count"):
        raise AssertionError(metric)  # pragma: no cover
    if o is None or o.status != "scored":
        return 0.0  # missing/skipped/errored rows count as failures, never exclusions
    if metric == "completion_rate":
        return float(bool(o.completed))
    if metric == "false_refusal_rate":
        return float(bool(o.completed and o.refused))
    if metric == "evidence_supplied_rate":
        return float(o.evidence_supplied is True)
    return float(o.fabricated_instruction is True)


def _in_scope(case: ReleaseCase, scope: str) -> bool:
    behavior, _, qclass = scope.partition(":")
    if behavior != "all" and case.expected_behavior != behavior:
        return False
    return not qclass or case.query_class == qclass


def _check_outcomes(release: ReleaseSet, outcomes: list[Outcome], repeats: int) -> dict[tuple[str, int], Outcome]:
    cases = release.by_id()
    table: dict[tuple[str, int], Outcome] = {}
    for o in outcomes:
        case = cases.get(o.case_id)
        if case is None:
            raise AcceptanceError(f"outcome for unknown case {o.case_id!r}")
        if o.repeat > repeats:
            raise AcceptanceError(f"outcome repeat {o.repeat} exceeds the pre-registered {repeats}")
        key = (o.case_id, o.repeat)
        if key in table:
            raise AcceptanceError(f"duplicate outcome for {o.case_id!r} repeat {o.repeat}")
        if o.status == "scored" and o.completed:
            need: tuple[bool | None, ...]
            if case.expected_behavior == "answer" and not o.refused:
                need = (o.useful, o.supported, o.traceable)
            elif case.expected_behavior == "abstain":
                need = (o.appropriate_abstention, o.fabricated_instruction)
            else:
                need = ()
            if any(v is None for v in need):
                raise AcceptanceError(f"outcome {o.case_id!r} repeat {o.repeat}: adjudication fields missing")
        table[key] = o
    return table


def _evaluate(c: Criterion, stage: Stage, release: ReleaseSet, table: dict[tuple[str, int], Outcome]) -> dict[str, Any]:
    scoped = [case for case in release.cases if _in_scope(case, c.scope)]
    base: dict[str, Any] = {
        "id": c.id, "metric": c.metric, "scope": c.scope, "op": c.op,
        "threshold": c.threshold, "statistic": c.statistic, "n": len(scoped), "min_n": c.min_n,
    }
    if c.metric == "case_count":
        value: float | None = float(len(scoped))
    elif len(scoped) < c.min_n:
        return {**base, "value": None, "bound": None, "status": "insufficient", "reason": "too few cases in scope"}
    elif c.metric == "flip_rate":
        flips = 0
        for case in scoped:
            passes = [_passed(case, table.get((case.id, r))) for r in range(1, stage.repeats + 1)]
            flips += len(set(passes)) > 1
        value = flips / len(scoped)
    elif c.metric in COUNT_METRICS:
        value = sum(
            _cell(c.metric, case, table.get((case.id, r)))
            for case in scoped
            for r in range(1, stage.repeats + 1)
        )
    else:
        cells = [
            _cell(c.metric, case, table.get((case.id, r)))
            for case in scoped
            for r in range(1, stage.repeats + 1)
        ]
        value = sum(cells) / len(cells)
    bound = value
    if c.statistic != "point" and value is not None:
        bound = wilson_bound(value * len(scoped), len(scoped), stage.confidence,
                             "lower" if c.statistic == "wilson_lower" else "upper")
    if value is None or bound is None or not math.isfinite(bound):
        return {**base, "value": value, "bound": bound, "status": "insufficient", "reason": "non-finite"}
    ok = bound >= c.threshold - 1e-9 if c.op == ">=" else bound <= c.threshold + 1e-9
    return {**base, "value": round(value, 4), "bound": round(bound, 4), "status": "pass" if ok else "fail"}


def score(
    release: ReleaseSet,
    set_sha256: str,
    criteria: Criteria,
    criteria_sha256: str,
    run: RunRecord,
    outcomes: list[Outcome],
) -> dict[str, Any]:
    """Apply the pre-registered criteria; structural defects raise AcceptanceError."""
    stage = criteria.stages.get(release.manifest.stage)
    if stage is None:
        raise AcceptanceError(f"criteria define no {release.manifest.stage!r} stage")
    if run.set_sha256 != set_sha256:
        raise AcceptanceError("outcomes were recorded against a different release-set hash")
    if run.criteria_sha256 != criteria_sha256:
        raise AcceptanceError("outcomes were recorded against different criteria")
    if run.repeats != stage.repeats:
        raise AcceptanceError(f"run declares {run.repeats} repeats; criteria pre-register {stage.repeats}")
    binding_ok = release.manifest.criteria_sha256 == criteria_sha256
    if not binding_ok and criteria.status == "registered":
        raise AcceptanceError("release set was adjudicated against different criteria than the registered ones")

    table = _check_outcomes(release, outcomes, stage.repeats)
    expected = len(release.cases) * stage.repeats
    results = [_evaluate(c, stage, release, table) for c in stage.criteria]
    results.append({
        "id": "outcomes_complete", "metric": "outcomes_complete", "scope": "all", "op": ">=",
        "threshold": expected, "statistic": "point", "n": len(release.cases), "min_n": 1,
        "value": len(table), "bound": len(table),
        "status": "pass" if len(table) == expected else "insufficient",
        **({} if len(table) == expected else {"reason": "missing outcomes count as failures, not as omissions"}),
    })
    statuses = {r["status"] for r in results}
    verdict = "rejected" if "fail" in statuses else "incomplete" if "insufficient" in statuses else "accepted"

    reasons = []
    if criteria.status != "registered":
        reasons.append("criteria are PROPOSED, not registered by the maintainer/SME")
    if run.profile.kind not in criteria.certifying_profiles:
        reasons.append(f"profile {run.profile.kind!r} cannot certify (certifying: {criteria.certifying_profiles})")
    if not binding_ok:
        reasons.append("release set manifest names different criteria than the scored file")
    certifying = not reasons

    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "certifying": certifying,
        "non_certifying_reasons": reasons,
        "stage": release.manifest.stage,
        "statistical_claim": stage.statistical_claim,
        "repeats": stage.repeats,
        "criteria": {"id": criteria.criteria_id, "status": criteria.status, "sha256": criteria_sha256},
        "set": {
            "set_id": release.manifest.set_id, "sha256": set_sha256,
            "answer_cases": sum(c.expected_behavior == "answer" for c in release.cases),
            "abstain_cases": sum(c.expected_behavior == "abstain" for c in release.cases),
        },
        "profile": run.profile.model_dump(),
        "criteria_results": results,
        "diagnostics": _diagnostics(release, stage, table),
    }


def _diagnostics(release: ReleaseSet, stage: Stage, table: dict[tuple[str, int], Outcome]) -> dict[str, Any]:
    """Separate denominators: all-request success vs quality conditional on completion."""
    answer = [c for c in release.cases if c.expected_behavior == "answer"]
    slots = [(c, table.get((c.id, r))) for c in answer for r in range(1, stage.repeats + 1)]
    completed = [(c, o) for c, o in slots if o and o.status == "scored" and o.completed]
    passes = sum(_passed(c, o) for c, o in slots)
    failures: Counter[str] = Counter()
    for c, o in slots:
        if _passed(c, o):
            continue
        failures["missing" if o is None else "skipped_or_error" if o.status != "scored" else o.failure_stage] += 1
    by_class: dict[str, dict[str, Any]] = {}
    for qclass in sorted({c.query_class for c in release.cases}):
        members = [c for c in release.cases if c.query_class == qclass]
        cells = [_passed(c, table.get((c.id, r))) for c in members for r in range(1, stage.repeats + 1)]
        by_class[qclass] = {"cases": len(members), "pass_rate": round(sum(cells) / len(cells), 4)}
    return {
        "answer_all_request_pass_rate": round(passes / len(slots), 4) if slots else None,
        "answer_completion_rate": round(len(completed) / len(slots), 4) if slots else None,
        "answer_pass_given_completed": round(sum(_passed(c, o) for c, o in completed) / len(completed), 4)
        if completed else None,
        "answer_failure_attribution": dict(sorted(failures.items())),
        "by_class": by_class,
    }


def exit_code(report: dict[str, Any]) -> int:
    if not report["certifying"]:
        return 2
    return 0 if report["verdict"] == "accepted" else 1


def summary_text(report: dict[str, Any]) -> str:
    mode = "certifying" if report["certifying"] else "NON-CERTIFYING"
    lines = [f"acceptance verdict: {report['verdict']} ({mode}) stage={report['stage']}"]
    lines += [f"  non-certifying: {r}" for r in report["non_certifying_reasons"]]
    for r in report["criteria_results"]:
        shown = r["bound"] if r.get("bound") is not None else "n/a"
        lines.append(f"  [{r['status']:<12}] {r['id']}: {r['metric']}({r['scope']}) {shown} {r['op']} {r['threshold']} (n={r['n']})")
    return "\n".join(lines)


def _short(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    return text[:200]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _tuning_queries() -> list[str]:
    """Dev golden always; the frozen holdout only under the RC venue (already required)."""
    queries: list[str] = []
    for name in ("evals/golden.jsonl", "evals/holdout.jsonl", "evals/paraphrase.jsonl"):
        path = Path(name)
        if not path.exists():
            continue
        try:
            text = read_golden_text(path)
        except DatasetError as exc:
            raise AcceptanceError(f"cannot read tuning dataset {name} for the overlap check: {exc}") from exc
        queries += [json.loads(x).get("query", "") for x in text.splitlines() if x.strip() and not x.startswith("#")]
    return queries


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Score adjudicated outcomes against pre-registered release criteria (#367)")
    p.add_argument("--set", type=Path, required=True, help="release_set.jsonl (needs adjacent .sha256 and VENUE=rc)")
    p.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA_PATH)
    p.add_argument("--outcomes", type=Path, help="adjudicated outcomes JSONL; omit with --validate-only")
    p.add_argument("--validate-only", action="store_true", help="validate the set and its overlap with tuning sets only")
    p.add_argument("--out", type=Path, help="write the JSON report here")
    args = p.parse_args(argv)
    try:
        if resolve_venue() != "rc":
            raise VenueError("release acceptance requires VENUE=rc; the release set is never a tuning dataset")
        release, set_sha = load_release_set(args.set)
        overlaps = find_overlaps(release, _tuning_queries())
        if overlaps:
            raise AcceptanceError(f"release cases overlap tuning datasets: {overlaps}")
        if args.validate_only:
            print(f"release set valid: {len(release.cases)} cases, sha256={set_sha}")
            return 0
        if args.outcomes is None:
            raise AcceptanceError("--outcomes is required unless --validate-only")
        criteria, criteria_sha = load_criteria(args.criteria)
        run, outcomes = parse_outcomes(args.outcomes.read_text(encoding="utf-8"))
        report = score(release, set_sha, criteria, criteria_sha, run, outcomes)
    except (AcceptanceError, DatasetError, OSError, UnicodeError) as exc:
        print(f"acceptance refused: {exc}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(summary_text(report))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
