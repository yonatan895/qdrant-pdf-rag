"""Human spot-check and adjudication workflow for generated gold candidates (#1317, PR 2).

Bridges ``eval.gold_candidates`` (PR 1, unadjudicated candidates refused by
every golden reader) and private passage-level evaluation sets without ever
violating #806 (human review before any row enters an eval set) or #1317's
privacy and baseline boundaries:

- ``export``: draw a deterministic sample stratified across query classes and
  lexical-overlap bands, bound to the candidate file's exact ``rows_sha256``,
  and write a private review sheet (mode 0600, outside git, never holdout or
  release-set names). Optionally fetches and SHA-256-verifies stored passage
  text through ``QdrantReader``.
- ``record``: record one human ``accept`` or ``reject`` verdict (named
  reviewer, ISO date ``YYYY-MM-DD``, optional reason) atomically in the
  private review sheet.
- ``report``: verify candidate/review binding, compute the accept rate
  overall, per class and per overlap band, and deliver a text-free verdict
  (``passed`` / ``incomplete`` / ``failed``) against the minimum sample size
  (default 20, capped at the batch size) and minimum accept rate (default 0.85).
- ``promote``: write a private eval JSONL + manifest (mode 0600, outside git)
  ONLY when the spot-check report passes. Default ``--mode adjudicated``
  (#806) emits only individually reviewed-and-accepted rows
  (``status: "adjudicated"``); explicit ``--mode spot-checked`` emits
  accepted rows as ``"adjudicated"``, excludes all rejected rows, and marks
  remaining unreviewed batch rows ``"spot_checked"`` with batch provenance
  and ``reviewed_by: None``. Every emitted row preserves
  ``source: "generated"``, so model-written gold is never presented as expert
  gold and is refused by every ``--update-baseline`` entry point.

Stdout, stderr, logs and reports carry IDs, counts, rates, reviewer names,
dates and SHA-256 hashes only — never query text, passage text or headings.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import random
import re
import secrets
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mainframe_rag.eval import gold_candidates as gc
from mainframe_rag.eval.datasets import (
    DEV,
    HOLDOUT_FILENAME,
    QUERY_CLASSES,
    RELEASE_SET_FILENAME,
    DatasetError,
    require_rc_for_collection,
    require_rc_for_golden,
)
from mainframe_rag.eval.retrieval import OVERLAP_BANDS, overlap_band

if TYPE_CHECKING:
    from mainframe_rag.ports import QdrantReader

DEFAULT_MIN_ACCEPT_RATE = 0.85
DEFAULT_MIN_SAMPLE = 20
VERDICTS = ("accept", "reject")
PROMOTE_MODES = ("adjudicated", "spot-checked")
REVIEW_KIND = "gold-spot-check-review"
PROMOTED_MANIFEST_KIND = "gold-adjudicated-manifest"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROTECTED_NAME_PREFIXES = (HOLDOUT_FILENAME.casefold(), RELEASE_SET_FILENAME.casefold())

REFUSALS: dict[str, str] = {
    "output": gc.OUTPUT_REFUSAL,
    "input_protected": (
        "refused: the frozen holdout and the independent release set are never "
        "read or written by the spot-check workflow"
    ),
    "invalid_candidates": (
        "refused: candidates input must be a readable JSONL of valid status=\"candidate\" rows "
        "with unique ids, gold_chunk_ids and provenance.text_sha256"
    ),
    "manifest_mismatch": (
        "refused: the adjacent candidate manifest is unreadable or its rows_sha256 "
        "does not match the candidates file"
    ),
    "invalid_review": (
        "refused: review input must be a readable private gold-spot-check-review JSONL "
        "with valid header and items"
    ),
    "review_unsafe_path": (
        "refused: the review file must be an existing regular file in a writable directory "
        "outside any git work tree, not named like the holdout or the release set"
    ),
    "candidates_mismatch": (
        "refused: the review file does not match the candidates file (sha256, count, "
        "row id, or text_sha256 mismatch)"
    ),
    "unknown_id": "refused: row id not found in the review file",
    "already_reviewed": (
        "refused: row already has a recorded verdict; pass --overwrite to replace it"
    ),
    "invalid_reviewer": "refused: reviewer must be a non-empty single-line name",
    "invalid_date": "refused: reviewed-at must be an ISO calendar date (YYYY-MM-DD)",
    "invalid_threshold": (
        "refused: --min-accept-rate must be a finite number in (0, 1] and --min-sample >= 1"
    ),
    "passage_mismatch": (
        "refused: stored passage text is missing or its sha256 does not match the candidate row"
    ),
    "venue": gc.REFUSALS["venue"],
    "no_accepted": "refused: no accepted rows to emit",
}


class Refused(Exception):
    """Fixed-message refusal; ``code`` keys ``REFUSALS``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _clean_reviewer(raw: object) -> str:
    if not isinstance(raw, str):
        raise Refused("invalid_reviewer")
    name = raw.strip()
    if not name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise Refused("invalid_reviewer")
    return name


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- path guards
def refuse_protected_input(path: Path) -> None:
    """Refuse holdout/release_set inputs regardless of VENUE."""
    try:
        require_rc_for_golden([path], DEV)
        if path.name.casefold().startswith(_PROTECTED_NAME_PREFIXES):
            raise Refused("input_protected")
        resolved = path.resolve(strict=True)
        require_rc_for_golden([resolved], DEV)
        if resolved.name.casefold().startswith(_PROTECTED_NAME_PREFIXES):
            raise Refused("input_protected")
    except Refused:
        raise
    except (DatasetError, OSError, RuntimeError, ValueError):
        if path.name.casefold().startswith(_PROTECTED_NAME_PREFIXES):
            raise Refused("input_protected") from None
        try:
            require_rc_for_golden([path], DEV)
        except DatasetError:
            raise Refused("input_protected") from None


def refuse_review_update(path: Path) -> Path:
    """Refuse updating a review file that is missing, a symlink, inside any
    git work tree, named like holdout/release_set, or in an unwritable dir."""
    refuse_protected_input(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise Refused("review_unsafe_path")
        parent = path.parent.resolve(strict=True)
        resolved = parent / path.name
        require_rc_for_golden([resolved], DEV)
        ancestors = {*path.absolute().parents, *resolved.parents}
        if (
            resolved.name.casefold().startswith(_PROTECTED_NAME_PREFIXES)
            or not os.access(parent, os.W_OK | os.X_OK)
            or any((p / ".git").exists() for p in ancestors)
        ):
            raise Refused("review_unsafe_path")
        return resolved
    except Refused:
        raise
    except (DatasetError, OSError, RuntimeError, ValueError):
        raise Refused("review_unsafe_path") from None


def _replace_private(path: Path, data: bytes) -> None:
    """Atomically replace an existing private review file with mode 0600."""
    resolved = refuse_review_update(path)
    tmp = resolved.with_name(f".{resolved.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Refused("review_unsafe_path") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, resolved)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _refuse_output(path: Path) -> None:
    try:
        gc.refuse_output(path)
    except gc.Refused:
        raise Refused("output") from None


def _write_private(path: Path, data: bytes) -> None:
    try:
        gc.write_private(path, data)
    except gc.Refused:
        raise Refused("output") from None


# ------------------------------------------------------ loading & validation
def load_candidates(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Read and validate a candidate JSONL file once; return ``(rows, sha256)``."""
    refuse_protected_input(path)
    try:
        data = path.read_bytes()
    except OSError:
        raise Refused("invalid_candidates") from None
    digest = _sha256_bytes(data)

    mpath = gc.manifest_path(path)
    if mpath.exists():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Refused("manifest_mismatch") from None
        if not isinstance(manifest, dict) or manifest.get("rows_sha256") != digest:
            raise Refused("manifest_mismatch")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("invalid_candidates") from None

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise Refused("invalid_candidates") from None
        if not _valid_candidate_row(obj, seen_ids):
            raise Refused("invalid_candidates")
        seen_ids.add(obj["id"])
        rows.append(obj)
    if not rows:
        raise Refused("invalid_candidates")
    return rows, digest


def _valid_candidate_row(obj: object, seen_ids: set[str]) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("status") != "candidate":
        return False
    row_id = obj.get("id")
    if not isinstance(row_id, str) or not row_id.strip() or row_id in seen_ids:
        return False
    query = obj.get("query")
    if not isinstance(query, str) or not query.strip():
        return False
    if obj.get("query_class") not in QUERY_CLASSES:
        return False
    docs = obj.get("expected_doc_ids")
    if not isinstance(docs, list) or not docs or any(not isinstance(d, str) or not d for d in docs):
        return False
    chunks = obj.get("gold_chunk_ids")
    if (
        not isinstance(chunks, list)
        or not chunks
        or any(not isinstance(c, str) or not c.strip() for c in chunks)
        or len(set(chunks)) != len(chunks)
    ):
        return False
    prov = obj.get("provenance")
    if not isinstance(prov, dict):
        return False
    text_sha = prov.get("text_sha256")
    return isinstance(text_sha, str) and bool(_SHA256_RE.match(text_sha))


def parse_review_text(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse and validate a ``gold-spot-check-review`` JSONL buffer."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise Refused("invalid_review") from None
        if not isinstance(obj, dict):
            raise Refused("invalid_review")
        records.append(obj)
    if len(records) < 2:
        raise Refused("invalid_review")
    header = records[0]
    if (
        header.get("record") != "spot_check_header"
        or header.get("kind") != REVIEW_KIND
        or header.get("version") != 1
        or not isinstance(header.get("candidates_sha256"), str)
        or not _SHA256_RE.match(header["candidates_sha256"])
        or type(header.get("total_candidates")) is not int
        or header["total_candidates"] < 1
        or type(header.get("sample_n")) is not int
        or header["sample_n"] < 1
    ):
        raise Refused("invalid_review")

    items = records[1:]
    if len(items) != header["sample_n"]:
        raise Refused("invalid_review")
    seen_ids: set[str] = set()
    for item in items:
        if item.get("record") != "spot_check_item":
            raise Refused("invalid_review")
        row_id = item.get("id")
        if not isinstance(row_id, str) or not row_id.strip() or row_id in seen_ids:
            raise Refused("invalid_review")
        seen_ids.add(row_id)
        text_sha = item.get("text_sha256")
        if not isinstance(text_sha, str) or not _SHA256_RE.match(text_sha):
            raise Refused("invalid_review")
        verdict = item.get("verdict")
        reviewer = item.get("reviewer")
        reviewed_at = item.get("reviewed_at")
        if verdict is None:
            if reviewer is not None or reviewed_at is not None:
                raise Refused("invalid_review")
        else:
            if verdict not in VERDICTS:
                raise Refused("invalid_review")
            try:
                _clean_reviewer(reviewer)
            except Refused:
                raise Refused("invalid_review") from None
            if not _is_iso_date(reviewed_at):
                raise Refused("invalid_review")
    return header, items


def load_review(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Read a review file once; return ``(header, items, sha256)``."""
    refuse_protected_input(path)
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Refused("invalid_review") from None
    header, items = parse_review_text(text)
    return header, items, _sha256_bytes(data)


def serialize_review(header: dict[str, Any], items: Sequence[dict[str, Any]]) -> bytes:
    lines = [header, *items]
    return "".join(
        json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n" for rec in lines
    ).encode("utf-8")


# --------------------------------------------------------------- core steps
def stratified_spot_sample(
    rows: Sequence[dict[str, Any]], *, sample: int, seed: int
) -> list[dict[str, Any]]:
    """Deterministic sample stratified over query_class, then lexical overlap
    band inside each class. Input-order independent (sorted by id first)."""
    rng = random.Random(seed)
    by_class: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in sorted(rows, key=lambda r: str(r["id"])):
        band = overlap_band(row.get("lexical_overlap")) or "unknown"
        by_class[str(row["query_class"])][band].append(row)

    classes = sorted(by_class)
    rng.shuffle(classes)
    queues: dict[str, list[list[dict[str, Any]]]] = {}
    for cls in classes:
        strata = [by_class[cls][b] for b in sorted(by_class[cls])]
        for stratum in strata:
            rng.shuffle(stratum)
        rng.shuffle(strata)
        queues[cls] = strata

    target = min(sample, len(rows))
    picked: list[dict[str, Any]] = []
    while len(picked) < target and any(queues.values()):
        for cls in classes:
            if len(picked) >= target:
                break
            strata = queues[cls]
            if not strata:
                continue
            stratum = strata.pop(0)
            picked.append(stratum.pop())
            if stratum:
                strata.append(stratum)
    return picked


def batch_overlap_bands(candidates: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Rows of the whole candidate batch per lexical-overlap band (every band
    listed; rows without an overlap value are not counted)."""
    counts = Counter(overlap_band(row.get("lexical_overlap")) for row in candidates)
    return {band: counts[band] for band in OVERLAP_BANDS}


def build_review_records(
    candidates: Sequence[dict[str, Any]],
    *,
    candidates_sha256: str,
    sample: int,
    seed: int,
    passages: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the review header and item records for a stratified sample."""
    sampled = stratified_spot_sample(candidates, sample=sample, seed=seed)
    header: dict[str, Any] = {
        "record": "spot_check_header",
        "kind": REVIEW_KIND,
        "version": 1,
        "candidates_sha256": candidates_sha256,
        "total_candidates": len(candidates),
        "sample_n": len(sampled),
        "seed": seed,
    }
    items: list[dict[str, Any]] = []
    for row in sampled:
        source_chunk_id = row["gold_chunk_ids"][0]
        expected_sha = row["provenance"]["text_sha256"]
        item: dict[str, Any] = {
            "record": "spot_check_item",
            "id": row["id"],
            "query_class": row["query_class"],
            "query": row["query"],
            "expected_doc_ids": list(row["expected_doc_ids"]),
            "expected_heading": row.get("expected_heading"),
            "expected_page": row.get("expected_page"),
            "gold_chunk_ids": list(row["gold_chunk_ids"]),
            "gold_chunk_ids_truncated": bool(row.get("gold_chunk_ids_truncated", False)),
            "lexical_overlap": row.get("lexical_overlap"),
            "overlap_band": overlap_band(row.get("lexical_overlap")),
            "text_sha256": expected_sha,
            "verdict": None,
            "reviewer": None,
            "reviewed_at": None,
            "reason": None,
        }
        if passages is not None:
            text = passages.get(source_chunk_id)
            if text is None or _sha256_text(text) != expected_sha:
                raise Refused("passage_mismatch")
            item["passage_text"] = text
        items.append(item)
    return header, items


def record_verdict(
    header: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    row_id: str,
    verdict: str,
    reviewer: str,
    reviewed_at: str,
    reason: str | None = None,
    overwrite: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Record one human accept/reject verdict; return updated records and
    text-free confirmation."""
    if verdict not in VERDICTS:
        raise Refused("invalid_review")
    clean_reviewer = _clean_reviewer(reviewer)
    if not _is_iso_date(reviewed_at):
        raise Refused("invalid_date")
    clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None

    updated: list[dict[str, Any]] = []
    found = False
    for item in items:
        if item["id"] == row_id:
            found = True
            if item["verdict"] is not None and not overwrite:
                raise Refused("already_reviewed")
            updated.append({
                **item,
                "verdict": verdict,
                "reviewer": clean_reviewer,
                "reviewed_at": reviewed_at,
                "reason": clean_reason,
            })
        else:
            updated.append(dict(item))
    if not found:
        raise Refused("unknown_id")

    reviewed_n = sum(1 for it in updated if it["verdict"] is not None)
    confirmation = {
        "id": row_id,
        "verdict": verdict,
        "reviewer": clean_reviewer,
        "reviewed_at": reviewed_at,
        "reviewed": reviewed_n,
        "remaining": len(updated) - reviewed_n,
    }
    return dict(header), updated, confirmation


def evaluate_spot_check(
    candidates: Sequence[dict[str, Any]],
    *,
    candidates_sha256: str,
    header: dict[str, Any],
    items: Sequence[dict[str, Any]],
    min_accept_rate: float = DEFAULT_MIN_ACCEPT_RATE,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> dict[str, Any]:
    """Verify candidate/review binding and compute the text-free spot-check
    report and pass/incomplete/fail verdict."""
    if (
        isinstance(min_accept_rate, bool)
        or not isinstance(min_accept_rate, (int, float))
        or not math.isfinite(min_accept_rate)
        or not (0.0 < min_accept_rate <= 1.0)
        or type(min_sample) is not int
        or min_sample < 1
    ):
        raise Refused("invalid_threshold")

    if (
        header["candidates_sha256"] != candidates_sha256
        or header["total_candidates"] != len(candidates)
    ):
        raise Refused("candidates_mismatch")

    by_id = {row["id"]: row for row in candidates}
    for item in items:
        cand = by_id.get(item["id"])
        if (
            cand is None
            or cand["provenance"]["text_sha256"] != item["text_sha256"]
            or cand["query_class"] != item.get("query_class")
        ):
            raise Refused("candidates_mismatch")

    def rate_block(sub: Sequence[dict[str, Any]]) -> dict[str, Any]:
        reviewed = [it for it in sub if it["verdict"] is not None]
        acc = sum(1 for it in reviewed if it["verdict"] == "accept")
        rej = sum(1 for it in reviewed if it["verdict"] == "reject")
        return {
            "n": len(sub),
            "reviewed": len(reviewed),
            "accepted": acc,
            "rejected": rej,
            "accept_rate": round(acc / len(reviewed), 4) if reviewed else None,
        }

    reviewed_items = [it for it in items if it["verdict"] is not None]
    accepted_ids = sorted(it["id"] for it in reviewed_items if it["verdict"] == "accept")
    rejected_ids = sorted(it["id"] for it in reviewed_items if it["verdict"] == "reject")
    unreviewed_ids = sorted(it["id"] for it in items if it["verdict"] is None)
    reviewed_n = len(reviewed_items)
    unreviewed_n = len(unreviewed_ids)
    accepted = len(accepted_ids)
    rejected = len(rejected_ids)
    accept_rate = round(accepted / reviewed_n, 4) if reviewed_n else None
    required_sample = min(min_sample, len(candidates))

    if unreviewed_n > 0 or reviewed_n < required_sample or reviewed_n == 0:
        verdict = "incomplete"
    elif accept_rate is not None and accept_rate >= min_accept_rate:
        verdict = "passed"
    else:
        verdict = "failed"

    by_class = {
        cls: rate_block([it for it in items if it["query_class"] == cls])
        for cls in sorted({str(it["query_class"]) for it in items})
    }
    by_overlap_band = {}
    for band in OVERLAP_BANDS:
        sub = [it for it in items if overlap_band(it.get("lexical_overlap")) == band]
        if sub:
            by_overlap_band[band] = rate_block(sub)

    reviewers = sorted({str(it["reviewer"]) for it in reviewed_items if it.get("reviewer")})
    return {
        "kind": "gold-spot-check-report",
        "version": 1,
        "candidates_sha256": candidates_sha256,
        "total_candidates": len(candidates),
        "sample_n": len(items),
        "required_sample": required_sample,
        "reviewed_n": reviewed_n,
        "unreviewed_n": unreviewed_n,
        "accepted": accepted,
        "rejected": rejected,
        "accept_rate": accept_rate,
        "min_accept_rate": float(min_accept_rate),
        "min_sample": min_sample,
        "verdict": verdict,
        "reviewers": reviewers,
        "by_class": by_class,
        "by_overlap_band": by_overlap_band,
        "candidates_by_overlap_band": batch_overlap_bands(candidates),
        "accepted_ids": accepted_ids,
        "rejected_ids": rejected_ids,
        "unreviewed_ids": unreviewed_ids,
    }


def build_promoted_rows(
    candidates: Sequence[dict[str, Any]],
    *,
    candidates_sha256: str,
    header: dict[str, Any],
    items: Sequence[dict[str, Any]],
    review_sha256: str,
    mode: str = "adjudicated",
    min_accept_rate: float = DEFAULT_MIN_ACCEPT_RATE,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Promote reviewed candidates ONLY when the spot-check passes.

    Returns ``(rows, report)`` where ``rows`` is ``None`` if the spot-check
    verdict is not ``"passed"``.
    """
    if mode not in PROMOTE_MODES:
        raise Refused("invalid_review")
    report = evaluate_spot_check(
        candidates,
        candidates_sha256=candidates_sha256,
        header=header,
        items=items,
        min_accept_rate=min_accept_rate,
        min_sample=min_sample,
    )
    if report["verdict"] != "passed":
        return None, report

    review_by_id = {it["id"]: it for it in items}
    promoted: list[dict[str, Any]] = []
    spot_meta = {
        "review_sha256": review_sha256,
        "accept_rate": report["accept_rate"],
        "sample_n": report["sample_n"],
        "accepted": report["accepted"],
        "rejected": report["rejected"],
        "min_accept_rate": report["min_accept_rate"],
    }
    for row in candidates:
        review = review_by_id.get(row["id"])
        if review is not None:
            if review["verdict"] == "reject":
                continue
            promoted.append({
                **row,
                "source": "generated",
                "status": "adjudicated",
                "reviewers": [review["reviewer"]],
                "reviewed_by": review["reviewer"],
                "reviewed_at": review["reviewed_at"],
            })
        elif mode == "spot-checked":
            promoted.append({
                **row,
                "source": "generated",
                "status": "spot_checked",
                "reviewers": list(report["reviewers"]),
                "reviewed_by": None,
                "reviewed_at": None,
                "spot_check": dict(spot_meta),
            })
    if not promoted:
        raise Refused("no_accepted")
    return promoted, report


# ----------------------------------------------------------------------- CLI
def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Human spot-check and adjudication workflow for generated gold candidates (#1317)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="export a stratified private review sample")
    p_exp.add_argument("--candidates", type=Path, required=True)
    p_exp.add_argument("--out", type=Path, required=True)
    p_exp.add_argument("--sample", type=_positive_int, default=DEFAULT_MIN_SAMPLE)
    p_exp.add_argument("--seed", type=int, required=True)
    p_exp.add_argument("--with-passages", action="store_true",
                       help="fetch and SHA-256-verify source chunk text into the private review sheet")
    p_exp.add_argument("--collection", default=None,
                       help="collection or alias for --with-passages (default: configured collection)")

    p_rec = sub.add_parser("record", help="record a human accept/reject verdict in a review sheet")
    p_rec.add_argument("--review", type=Path, required=True)
    p_rec.add_argument("--id", dest="row_id", required=True)
    p_rec.add_argument("--verdict", choices=VERDICTS, required=True)
    p_rec.add_argument("--reviewer", required=True)
    p_rec.add_argument("--reviewed-at", required=True, help="ISO date YYYY-MM-DD")
    p_rec.add_argument("--reason", default=None)
    p_rec.add_argument("--overwrite", action="store_true")

    p_rep = sub.add_parser("report", help="compute text-free spot-check accept-rate report")
    p_rep.add_argument("--candidates", type=Path, required=True)
    p_rep.add_argument("--review", type=Path, required=True)
    p_rep.add_argument("--min-accept-rate", type=float, default=DEFAULT_MIN_ACCEPT_RATE)
    p_rep.add_argument("--min-sample", type=_positive_int, default=DEFAULT_MIN_SAMPLE)
    p_rep.add_argument("--out", type=Path, default=None)

    p_pro = sub.add_parser("promote", help="write a private eval set after a passing spot-check")
    p_pro.add_argument("--candidates", type=Path, required=True)
    p_pro.add_argument("--review", type=Path, required=True)
    p_pro.add_argument("--out", type=Path, required=True)
    p_pro.add_argument("--mode", choices=PROMOTE_MODES, default="adjudicated")
    p_pro.add_argument("--min-accept-rate", type=float, default=DEFAULT_MIN_ACCEPT_RATE)
    p_pro.add_argument("--min-sample", type=_positive_int, default=DEFAULT_MIN_SAMPLE)
    return parser


def _fetch_verified_passages(
    candidates: Sequence[dict[str, Any]],
    *,
    sample: int,
    seed: int,
    collection: str | None,
    client: QdrantReader | None,
) -> dict[str, str]:
    if client is None or collection is None:
        from mainframe_rag.config import load_settings, qdrant_pool_limits

        settings = load_settings()
        collection = collection or settings.qdrant_collection
        try:
            require_rc_for_collection(collection)
        except DatasetError:
            raise Refused("venue") from None
        if client is None:
            from qdrant_client import QdrantClient

            client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout=settings.qdrant_timeout_s,
                limits=qdrant_pool_limits(settings.qdrant_url),
            )
    else:
        try:
            require_rc_for_collection(collection)
        except DatasetError:
            raise Refused("venue") from None
    _, physical = gc.resolve_collection(client, collection)
    try:
        require_rc_for_collection(physical)
    except DatasetError:
        raise Refused("venue") from None
    sampled = stratified_spot_sample(candidates, sample=sample, seed=seed)
    chunk_ids = [row["gold_chunk_ids"][0] for row in sampled]
    return gc.fetch_texts(client, collection, chunk_ids)


def main(
    argv: list[str] | None = None, *, client: QdrantReader | None = None
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            _refuse_output(args.out)
            candidates, digest = load_candidates(args.candidates)
            passages: dict[str, str] | None = None
            if args.with_passages:
                passages = _fetch_verified_passages(
                    candidates,
                    sample=args.sample,
                    seed=args.seed,
                    collection=args.collection,
                    client=client,
                )
            header, items = build_review_records(
                candidates,
                candidates_sha256=digest,
                sample=args.sample,
                seed=args.seed,
                passages=passages,
            )
            _write_private(args.out, serialize_review(header, items))
            summary = {
                "candidates_sha256": digest,
                "total_candidates": header["total_candidates"],
                "sample_n": header["sample_n"],
                "seed": header["seed"],
                "candidates_by_overlap_band": batch_overlap_bands(candidates),
                "by_class": dict(sorted(Counter(it["query_class"] for it in items).items())),
                "by_overlap_band": dict(
                    sorted(Counter(overlap_band(it.get("lexical_overlap")) or "unknown" for it in items).items())
                ),
            }
            print(json.dumps(summary, sort_keys=True))
            return 0

        if args.command == "record":
            refuse_review_update(args.review)
            header, items, _ = load_review(args.review)
            new_header, new_items, confirmation = record_verdict(
                header,
                items,
                row_id=args.row_id,
                verdict=args.verdict,
                reviewer=args.reviewer,
                reviewed_at=args.reviewed_at,
                reason=args.reason,
                overwrite=args.overwrite,
            )
            _replace_private(args.review, serialize_review(new_header, new_items))
            print(json.dumps(confirmation, sort_keys=True))
            return 0

        if args.command == "report":
            if args.out is not None:
                _refuse_output(args.out)
            candidates, digest = load_candidates(args.candidates)
            header, items, _ = load_review(args.review)
            report = evaluate_spot_check(
                candidates,
                candidates_sha256=digest,
                header=header,
                items=items,
                min_accept_rate=args.min_accept_rate,
                min_sample=args.min_sample,
            )
            payload = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
            if args.out is not None:
                _write_private(args.out, payload)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["verdict"] == "passed" else 1

        if args.command == "promote":
            _refuse_output(args.out)
            _refuse_output(gc.manifest_path(args.out))
            candidates, digest = load_candidates(args.candidates)
            header, items, review_sha = load_review(args.review)
            promoted, report = build_promoted_rows(
                candidates,
                candidates_sha256=digest,
                header=header,
                items=items,
                review_sha256=review_sha,
                mode=args.mode,
                min_accept_rate=args.min_accept_rate,
                min_sample=args.min_sample,
            )
            if promoted is None:
                print(json.dumps(report, sort_keys=True))
                return 1
            data = "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in promoted
            ).encode("utf-8")
            manifest = {
                "kind": PROMOTED_MANIFEST_KIND,
                "version": 1,
                "mode": args.mode,
                "source": "generated",
                "candidates_sha256": digest,
                "review_sha256": review_sha,
                "rows_sha256": _sha256_bytes(data),
                "emitted_n": len(promoted),
                "adjudicated_n": sum(1 for r in promoted if r["status"] == "adjudicated"),
                "spot_checked_n": sum(1 for r in promoted if r["status"] == "spot_checked"),
                "excluded_rejected_n": report["rejected"],
                "reviewers": report["reviewers"],
                "spot_check_report": report,
            }
            _write_private(args.out, data)
            try:
                _write_private(
                    gc.manifest_path(args.out),
                    (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"),
                )
            except BaseException:
                args.out.unlink(missing_ok=True)
                raise
            print(json.dumps({
                "mode": args.mode,
                "emitted_n": manifest["emitted_n"],
                "adjudicated_n": manifest["adjudicated_n"],
                "spot_checked_n": manifest["spot_checked_n"],
                "excluded_rejected_n": manifest["excluded_rejected_n"],
                "rows_sha256": manifest["rows_sha256"],
                "verdict": report["verdict"],
                "accept_rate": report["accept_rate"],
            }, sort_keys=True))
            return 0
    except Refused as exc:
        print(REFUSALS[exc.code], file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
