"""Dataset identity, loading, venue and pinned-set access (issue #508 C2).

Canonical owner for ``eval_retrieval.GoldenEntry``, golden loaders,
``venue.py`` rules and mode-keyed baseline selection. Protected holdout
reads enforce the RC venue and adjacent approved pin before parsing.
Scoring, mode defaults, baseline and label rules remain separate owners.

Data-path rule: pure functions accept explicit paths. Default dataset paths
are workspace-relative (``evals/...``) so source checkouts and installed
entry points resolve them identically from the workspace root supplied by
CLI composition; the package never guesses a parent count via
``Path(__file__).parents[N]``.

Holdout identity is filename-based (resolved name included) so the frozen
holdout stays protected across source/installed entry points and
symlink/path aliases. This is fail-closed hardening versus the old
absolute ``REPO/evals/holdout.jsonl`` comparison: a copy or symlink alias
named ``holdout.jsonl`` (or resolving to one) is still the frozen holdout
and still requires ``VENUE=rc``. Ordinary ``golden.jsonl`` paths are
unaffected.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

QUERY_CLASSES = (
    "message_id",
    "doc_number",
    "syntax",
    "diagnostic",
    "comparative",
    "version",
    "negative",
    "table",
)

# Status vocabulary for generated passage-level gold rows (#1317).
# "candidate" and "rejected" are refused by every golden reader;
# "adjudicated" (individually reviewed and accepted) and "spot_checked"
# (batch passed a human spot-check threshold) may be loaded for private evals,
# never for baseline recording.
ADJUDICATED_STATUSES = frozenset({"adjudicated", "spot_checked"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_iso_date(value: str | None) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


class GoldenEntry(BaseModel):
    query: str = Field(min_length=1)
    expected_doc_ids: list[str] = Field(default_factory=list)
    expected_heading: str | None = None
    expected_page: str | None = None
    must_not_retrieve: list[str] = Field(default_factory=list)
    must_not_message_ids: list[str] = Field(default_factory=list)
    expected_behavior: Literal["answer", "abstain"] = "answer"
    query_class: Literal[QUERY_CLASSES] | None = None  # type: ignore[valid-type]
    id: str | None = None
    source: str | None = None
    note: str | None = None
    # Passage-level gold fields (#1317 PR 2): optional, backward compatible.
    # gold_chunk_ids[0] is the source chunk; gold_chunk_ids[1:] are section siblings.
    gold_chunk_ids: list[str] = Field(default_factory=list)
    gold_chunk_ids_truncated: bool = False
    section_chunk_count: int | None = None
    lexical_overlap: float | None = None
    status: str | None = None
    reviewers: list[str] = Field(default_factory=list)
    reviewed_by: str | None = None
    reviewed_at: str | None = None

    @model_validator(mode="after")
    def _behavior_matches_expectations(self) -> GoldenEntry:
        if self.expected_behavior == "abstain" and self.expected_doc_ids:
            raise ValueError(
                "abstain entries must not set expected_doc_ids; "
                "expectations are expressed via must_not_retrieve/must_not_message_ids"
            )
        if self.expected_behavior == "abstain" and self.gold_chunk_ids:
            raise ValueError("abstain entries must not set gold_chunk_ids")
        if self.expected_behavior == "answer" and not self.expected_doc_ids:
            raise ValueError(
                "answer entries require expected_doc_ids; "
                "use expected_behavior='abstain' for negative/trap queries"
            )
        if self.gold_chunk_ids:
            if any(not isinstance(cid, str) or not cid.strip() for cid in self.gold_chunk_ids):
                raise ValueError("gold_chunk_ids entries must be non-empty strings")
            if len(set(self.gold_chunk_ids)) != len(self.gold_chunk_ids):
                raise ValueError("gold_chunk_ids must not contain duplicates")
        if self.section_chunk_count is not None:
            if type(self.section_chunk_count) is not int or self.section_chunk_count < 1:
                raise ValueError("section_chunk_count must be a positive integer")
            if self.gold_chunk_ids and self.section_chunk_count < len(self.gold_chunk_ids):
                raise ValueError("section_chunk_count cannot be less than len(gold_chunk_ids)")
        if self.lexical_overlap is not None and (
            isinstance(self.lexical_overlap, bool)
            or not math.isfinite(self.lexical_overlap)
            or not (0.0 <= self.lexical_overlap <= 1.0)
        ):
            raise ValueError("lexical_overlap must be a finite number in [0.0, 1.0]")
        if self.reviewed_at is not None and not _is_iso_date(self.reviewed_at):
            raise ValueError("reviewed_at must be an ISO calendar date (YYYY-MM-DD)")
        return self


def has_generated_entries(entries: Sequence[GoldenEntry]) -> bool:
    """True when any entry carries ``source == 'generated'`` (#1317)."""
    return any(entry.source == "generated" for entry in entries)


def load_golden(path: Path) -> list[GoldenEntry]:
    return parse_golden_text(read_golden_text(path))


def _golden_records(text: str) -> list[dict[str, Any]]:
    _refuse_candidate_rows(text)
    return [
        json.loads(line)
        for line in map(str.strip, text.splitlines())
        if line and not line.startswith("#")
    ]


def load_golden_records(paths: Sequence[Path | str]) -> list[dict[str, Any]]:
    """Raw JSON records from acquired golden files, in argument order (the
    answer tiers score the raw record; release records are valid here)."""
    return [
        record
        for path in paths
        for record in _golden_records(read_golden_text(path))
    ]


def load_golden_records_with_identity(
    paths: Sequence[Path | str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Raw records plus a text-free dataset identity for the run parameters.

    Identity is the basename and sha256 of the exact bytes read, one entry per
    resolved dataset in argument order, so an arm can prove which split it
    used without recording a private path or any question text."""
    records: list[dict[str, Any]] = []
    identity: list[dict[str, str]] = []
    for path in paths:
        data = read_golden_bytes(path)
        records.extend(_golden_records(data.decode("utf-8")))
        identity.append({
            "name": Path(path).name,
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return records, identity


def parse_golden_text(text: str) -> list[GoldenEntry]:
    """Parse an already acquired dataset without reopening its source."""
    # Before any row is validated: an invalid-entry message echoes row text.
    refusal = refusal_for_golden_text(text)
    if refusal is not None:
        raise SystemExit(refusal)
    entries: list[GoldenEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if _is_release_set_record(line):
            raise SystemExit(
                "release-set records are not golden entries; the independent "
                "release set is scored only by the acceptance entry point"
            )
        try:
            entry = GoldenEntry.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise SystemExit(f"invalid golden entry: {line[:120]} ({exc})") from exc
        entries.append(entry)
    return entries


def default_retrieval_paths(
    embed_mode: str, suite: Literal["dev", "paraphrase"] = "dev",
) -> tuple[Path, Path]:
    """Workspace-relative dataset/baseline pair for the normalized Settings mode.

    Explicit CLI paths override these defaults. Selecting a pair neither reads
    data nor authorizes recording; the calling operation owns those decisions.
    """
    if suite not in ("dev", "paraphrase"):
        raise ValueError("unsupported retrieval suite")
    golden = "golden.jsonl" if suite == "dev" else "paraphrase.jsonl"
    family = "" if suite == "dev" else "-paraphrase"
    mode = "-vllm" if embed_mode == "vllm" else ""
    return Path("evals") / golden, Path("evals") / f"baseline{family}{mode}.json"


DEV_GOLDEN_PATH = Path("evals/golden.jsonl")
HOLDOUT_PATH = Path("evals/holdout.jsonl")
HOLDOUT_FILENAME = "holdout.jsonl"
RELEASE_SET_FILENAME = "release_set.jsonl"

RC_ONLY_COLLECTIONS = frozenset({"real_manuals"})

VENUE_ENV = "VENUE"
DEV = "dev"
RC = "rc"


class DatasetError(RuntimeError):
    """A dataset cannot be used under its declared access contract."""


class DatasetPinError(DatasetError):
    """A protected dataset is unavailable or does not match its pin."""


class VenueError(DatasetError):
    """An RC-only instrument was requested without a declared RC venue."""


class CandidateDatasetError(DatasetError):
    """A file holding generated, unadjudicated rows was passed as a dataset."""


# One fixed message per refusal class; never carries row text.
CANDIDATE_REFUSAL = (
    "refused: the file holds generated gold candidates (status \"candidate\"); "
    "an unadjudicated row is not an eval row and is never read as gold"
)
REJECTED_REFUSAL = (
    "refused: the file holds rejected gold candidates (status \"rejected\"); "
    "rejected rows are never read as gold"
)
SPOT_CHECK_REFUSAL = (
    "refused: the file holds spot-check review records; "
    "review sheets are not eval datasets and are never read as gold"
)
UNREVIEWED_GENERATED_REFUSAL = (
    "refused: generated gold rows (source \"generated\") require human review provenance "
    "(status \"adjudicated\" or \"spot_checked\" with named reviewers)"
)
GENERATED_BASELINE_REFUSAL = (
    "refused: generated gold rows (source \"generated\") cannot enter a baseline; "
    "model-written gold is never a baseline input"
)
_SPOT_CHECK_RECORD_TYPES = frozenset({"spot_check_header", "spot_check_item"})


def _parse_json_object(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line.lstrip("\ufeff"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def is_candidate_record(line: str) -> bool:
    """Content identity of a generated gold candidate (issue #1317): a JSON
    object whose ``status`` is ``"candidate"``. The status alone decides,
    whatever the file is called and whichever other fields the row keeps."""
    if '"candidate"' not in line and "\\u" not in line and "\\U" not in line:
        return False
    obj = _parse_json_object(line)
    return obj is not None and obj.get("status") == "candidate"


def has_candidate_record(text: str) -> bool:
    return (
        ('"candidate"' in text or "\\u" in text or "\\U" in text)
        and any(is_candidate_record(line) for line in text.splitlines())
    )


def is_rejected_record(line: str) -> bool:
    """Content identity of a rejected gold candidate: a JSON object whose
    ``status`` is ``"rejected"``."""
    if '"rejected"' not in line and "\\u" not in line and "\\U" not in line:
        return False
    obj = _parse_json_object(line)
    return obj is not None and obj.get("status") == "rejected"


def has_rejected_record(text: str) -> bool:
    return (
        ('"rejected"' in text or "\\u" in text or "\\U" in text)
        and any(is_rejected_record(line) for line in text.splitlines())
    )


def is_spot_check_record(line: str) -> bool:
    """Content identity of a spot-check review record (header or item)."""
    if '"record"' not in line and "\\u" not in line and "\\U" not in line:
        return False
    obj = _parse_json_object(line)
    return obj is not None and obj.get("record") in _SPOT_CHECK_RECORD_TYPES


def has_spot_check_record(text: str) -> bool:
    return (
        ('"record"' in text or "\\u" in text or "\\U" in text)
        and any(is_spot_check_record(line) for line in text.splitlines())
    )


def is_unreviewed_generated_record(line: str) -> bool:
    """True when a JSON object declares ``source == 'generated'`` without
    valid human adjudication or batch spot-check provenance."""
    if '"generated"' not in line and "\\u" not in line and "\\U" not in line:
        return False
    obj = _parse_json_object(line)
    if obj is None or obj.get("source") != "generated":
        return False
    status = obj.get("status")
    if status not in ADJUDICATED_STATUSES:
        return True
    reviewers = obj.get("reviewers")
    if (
        not isinstance(reviewers, list)
        or not reviewers
        or any(not isinstance(r, str) or not r.strip() for r in reviewers)
    ):
        return True
    if status == "adjudicated":
        reviewed_by = obj.get("reviewed_by")
        reviewed_at = obj.get("reviewed_at")
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            return True
        if not _is_iso_date(reviewed_at):
            return True
    return False


def has_unreviewed_generated_record(text: str) -> bool:
    return (
        ('"generated"' in text or "\\u" in text or "\\U" in text)
        and any(is_unreviewed_generated_record(line) for line in text.splitlines())
    )


def refusal_for_golden_text(text: str) -> str | None:
    """Fixed refusal message when ``text`` contains unadjudicated candidates,
    rejected rows, spot-check review records, or generated rows missing human
    review provenance; None otherwise."""
    if has_candidate_record(text):
        return CANDIDATE_REFUSAL
    if has_rejected_record(text):
        return REJECTED_REFUSAL
    if has_spot_check_record(text):
        return SPOT_CHECK_REFUSAL
    if has_unreviewed_generated_record(text):
        return UNREVIEWED_GENERATED_REFUSAL
    return None


def _refuse_candidate_rows(text: str) -> None:
    """Refuse a dataset holding any candidate, rejected, or review record
    (#806/#1317: a row needs human adjudication before it enters an eval set).
    Every golden reader calls this on the content it acquired, before parsing
    or scoring a row."""
    refusal = refusal_for_golden_text(text)
    if refusal is not None:
        raise CandidateDatasetError(refusal)


def resolve_venue(environ: Mapping[str, str] | None = None) -> str:
    """VENUE env: unset/blank -> dev; only dev/rc accepted (a typo fails
    closed rather than silently selecting the wrong instrument)."""
    env = os.environ if environ is None else environ
    raw = str(env.get(VENUE_ENV, "")).strip().lower()
    if not raw:
        return DEV
    if raw not in (DEV, RC):
        raise VenueError(f"{VENUE_ENV}={raw!r} is not a venue; use {DEV!r} or {RC!r}")
    return raw


def _is_holdout(path: Path | str) -> bool:
    """Filename identity for the frozen holdout (source + installed).

    Both the literal name and the resolved name are checked so symlink and
    path aliases resolve consistently: a link named ``golden.jsonl``
    pointing at holdout content still resolves to ``holdout.jsonl`` and is
    refused in dev; a copy named ``holdout.jsonl`` is likewise refused.
    """
    try:
        candidate = Path(path)
    except (TypeError, ValueError):
        return False
    if candidate.name == HOLDOUT_FILENAME:
        return True
    try:
        return candidate.resolve().name == HOLDOUT_FILENAME
    except OSError:
        return False


def _is_release_set(path: Path | str) -> bool:
    """Filename identity (literal or resolved) of the independent release set."""
    try:
        candidate = Path(path)
    except (TypeError, ValueError):
        return False
    if candidate.name == RELEASE_SET_FILENAME:
        return True
    try:
        return candidate.resolve().name == RELEASE_SET_FILENAME
    except OSError:
        return False


def _is_release_set_record(line: str) -> bool:
    """Content guard: a renamed release-set copy still carries typed records."""
    if '"record"' not in line:
        return False
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get("record") in ("manifest", "case")


def require_rc_for_golden(paths: Sequence[Path | str], venue: str | None = None) -> None:
    """Refuse the frozen holdout unless the venue is declared RC."""
    if (venue or resolve_venue()) == RC:
        return
    for path in paths:
        if _is_holdout(path):
            raise VenueError(
                f"frozen holdout {HOLDOUT_FILENAME} requires {VENUE_ENV}={RC}; "
                "dev runs tune against evals/golden.jsonl only"
            )
        if _is_release_set(path):
            raise VenueError(
                f"independent release set {RELEASE_SET_FILENAME} requires "
                f"{VENUE_ENV}={RC}; it is never a tuning dataset"
            )


def require_rc_for_collection(collection: str, venue: str | None = None) -> None:
    """Refuse the real-corpus collection unless the venue is declared RC."""
    if (venue or resolve_venue()) == RC:
        return
    if collection in RC_ONLY_COLLECTIONS:
        raise VenueError(
            f"collection {collection!r} is the real-corpus RC venue; "
            f"set {VENUE_ENV}={RC} to evaluate it"
        )


def resolve_golden_paths(
    explicit: Sequence[Path | str] | None = None,
    venue: str | None = None,
) -> list[Path]:
    """Golden paths for one run: dev defaults to the golden set only; the
    frozen holdout joins the run only under ``VENUE=rc``. Explicit paths are
    honored but still guarded (an explicit holdout path in dev fails)."""
    venue = venue or resolve_venue()
    if explicit:
        paths = [Path(p) for p in explicit]
    else:
        paths = [DEV_GOLDEN_PATH]
        if venue == RC:
            paths.append(HOLDOUT_PATH)
    require_rc_for_golden(paths, venue)
    return paths


def require_tuning_golden(paths: Sequence[Path | str], operation: str) -> None:
    """Refuse a tuning sweep whose resolved set includes the frozen holdout.

    ``--subset``/``--repeats`` write the rows an arm is tuned on; the holdout
    is never a tuning dataset (docs/eval.md section 7). The guard lives here,
    before any read, so an unsafe invocation cannot open the holdout or send a
    model call. An explicit non-holdout ``--golden`` is the only way through.
    """
    for path in paths:
        if _is_holdout(path):
            raise VenueError(
                f"{operation} needs an explicit non-holdout --golden: "
                f"the resolved set contains the frozen holdout {HOLDOUT_FILENAME}"
            )


def resolve_harness_golden_paths(
    embed_mode: str,
    explicit: Sequence[Path | str] | None = None,
    venue: str | None = None,
) -> list[Path]:
    """L1 harness defaults for the caller's normalized Settings mode.

    The synthetic/hash venue uses dev golden only, even under RC. The
    vLLM venue uses the existing venue-controlled set. Explicit selections
    retain the ordinary holdout authorization rule. Other evaluators keep
    their own defaults through ``resolve_golden_paths``.
    """
    if not explicit and embed_mode != "vllm":
        explicit = [DEV_GOLDEN_PATH]
    return resolve_golden_paths(explicit, venue)


def read_golden_bytes(path: Path | str) -> bytes:
    """Read once; authorize and verify frozen holdout bytes before decoding.

    A resolved holdout uses its target's adjacent ``.sha256`` file. The
    single SHA256 record identifies the target basename, either alone or
    with the historical ``evals/`` prefix; it never redirects the read.
    The pin travels with the approved dataset, including installed usage.
    Ordinary dev datasets retain their existing unpinned read semantics.
    Returns the exact file bytes, so a dataset identity hashes the same
    bytes its pin (or an independent `sha256sum`) names.
    """
    path = Path(path)
    try:
        target = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DatasetPinError("dataset path cannot be resolved") from exc
    if _is_release_set(path):
        raise DatasetError(
            "the independent release set is not a golden/tuning dataset; "
            "it is read only by the acceptance entry point"
        )
    protected = path.name == HOLDOUT_FILENAME or target.name == HOLDOUT_FILENAME
    require_rc_for_golden([HOLDOUT_PATH if protected else target])
    if not protected:
        data = target.read_bytes()
        _refuse_candidate_rows(data.decode("utf-8", "replace"))
        return data
    try:
        pin = target.with_name(target.name + ".sha256")
        record = pin.read_text(encoding="utf-8")
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)\n?", record)
        if match is None or match[2] not in (target.name, f"evals/{target.name}"):
            raise DatasetPinError("frozen holdout requires one valid sha256 record naming its dataset")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != match[1].lower():
            raise DatasetPinError("frozen holdout sha256 mismatch")
        # Validate the text encoding here, as the old text reader did, so an
        # invalid protected dataset still fails as DatasetPinError rather than
        # raising a decode error later. The bytes remain the identity source.
        text = data.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise DatasetPinError("frozen holdout or its sha256 pin is unavailable or invalid") from exc
    # After the venue and pin checks, so their order and messages are unchanged.
    _refuse_candidate_rows(text)
    return data


def read_golden_text(path: Path | str) -> str:
    """Decoded `read_golden_bytes` (same authorization and pin rules)."""
    return read_golden_bytes(path).decode("utf-8")


def read_dev_only_bytes(path: Path | str) -> bytes:
    """A dev-only dataset with no holdout route (the fixed-history sessions,
    issue #1191): the frozen holdout and the release set are refused by name
    before any read, at every venue, and by content after it."""
    if _is_holdout(path) or _is_release_set(path):
        raise DatasetError(
            f"dev-only dataset: {HOLDOUT_FILENAME} and {RELEASE_SET_FILENAME} are refused"
        )
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise DatasetError("dev-only dataset is unreadable") from exc
    text = raw.decode("utf-8", "replace")
    if any(_is_release_set_record(line) for line in text.splitlines()):
        raise DatasetError("release-set records are not a dev dataset")
    _refuse_candidate_rows(text)
    return raw


def read_release_set_text(path: Path | str) -> tuple[str, str]:
    """Read the independent release set: RC venue + adjacent sha256 pin.

    Same protection as the frozen holdout (VENUE rule, one pin record naming
    the dataset basename, hash and decode of one byte buffer), but this is the
    only reader that may open a release set: the golden readers refuse it.
    Returns ``(text, sha256_hex)``; the hash identifies the scored set.
    """
    path = Path(path)
    if resolve_venue() != RC:
        raise VenueError(
            f"independent release set requires {VENUE_ENV}={RC}; it is never a tuning dataset"
        )
    try:
        target = path.resolve()
        pin = target.with_name(target.name + ".sha256")
        record = pin.read_text(encoding="utf-8")
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)\n?", record)
        if match is None or match[2] != target.name:
            raise DatasetPinError("release set requires one valid sha256 record naming its dataset")
        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != match[1].lower():
            raise DatasetPinError("release set sha256 mismatch")
        return data.decode("utf-8"), digest
    except DatasetPinError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise DatasetPinError("release set or its sha256 pin is unavailable or invalid") from exc
