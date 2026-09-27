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

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

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

    @model_validator(mode="after")
    def _behavior_matches_expectations(self) -> GoldenEntry:
        if self.expected_behavior == "abstain" and self.expected_doc_ids:
            raise ValueError(
                "abstain entries must not set expected_doc_ids; "
                "expectations are expressed via must_not_retrieve/must_not_message_ids"
            )
        if self.expected_behavior == "answer" and not self.expected_doc_ids:
            raise ValueError(
                "answer entries require expected_doc_ids; "
                "use expected_behavior='abstain' for negative/trap queries"
            )
        return self


def load_golden(path: Path) -> list[GoldenEntry]:
    return parse_golden_text(read_golden_text(path))


def parse_golden_text(text: str) -> list[GoldenEntry]:
    """Parse an already acquired dataset without reopening its source."""
    entries: list[GoldenEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = GoldenEntry.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise SystemExit(f"invalid golden entry: {line[:120]} ({exc})")
        entries.append(entry)
    return entries


def default_baseline_path(embed_mode: str) -> Path:
    """Mode-keyed baselines: hash numbers gate CI/dev; vllm numbers gate
    release-candidate runs on the live stack. The two are not comparable."""
    return Path("evals/baseline-vllm.json") if embed_mode == "vllm" else Path("evals/baseline.json")


DEV_GOLDEN_PATH = Path("evals/golden.jsonl")
HOLDOUT_PATH = Path("evals/holdout.jsonl")
HOLDOUT_FILENAME = "holdout.jsonl"

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


def read_golden_text(path: Path | str) -> str:
    """Read once; authorize and verify frozen holdout bytes before decoding.

    A resolved holdout uses its target's adjacent ``.sha256`` file. The
    single SHA256 record identifies the target basename, either alone or
    with the historical ``evals/`` prefix; it never redirects the read.
    The pin travels with the approved dataset, including installed usage.
    Ordinary dev datasets retain their existing unpinned read semantics.
    """
    path = Path(path)
    try:
        target = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DatasetPinError("dataset path cannot be resolved") from exc
    protected = path.name == HOLDOUT_FILENAME or target.name == HOLDOUT_FILENAME
    require_rc_for_golden([HOLDOUT_PATH if protected else target])
    if not protected:
        return target.read_text(encoding="utf-8")
    try:
        pin = target.with_name(target.name + ".sha256")
        record = pin.read_text(encoding="utf-8")
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)\n?", record)
        if match is None or match[2] not in (target.name, f"evals/{target.name}"):
            raise DatasetPinError("frozen holdout requires one valid sha256 record naming its dataset")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != match[1].lower():
            raise DatasetPinError("frozen holdout sha256 mismatch")
        return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise DatasetPinError("frozen holdout or its sha256 pin is unavailable or invalid") from exc
