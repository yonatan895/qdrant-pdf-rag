"""Source-revision identity (issue #361, step 1: fail-closed planning gate).

Three identities, three jobs — do not conflate them:

- ``doc_id``: the printed publication/manual-family key (IBM form number or
  filename stem, ``ingest.ibm_pdf``). Human-facing: citations, doc-family
  search. It is NOT a destructive key: two files may resolve to one
  ``doc_id`` while carrying different bytes.
- ``source_rev``: the unambiguous source-revision key
  ``vendor|product|version|sha256`` (labels normalized). Destructive
  operations, completion records, and chunk identity key on this — the
  selector switch is the 361B migration; this PR detects ambiguity before
  any delete/upsert and stamps the key on payloads and inventory for that
  migration's provenance.
- generation: the committed representation binding
  (``ingest.completion.doc_generation_id``). Unchanged here.

This module is deliberately distinct from ``completion.source_labels``:
that is the raw CLI triple segment of a generation id (un-normalized, no
content hash — changing it would invalidate every stored completion), while
``source_rev_key`` is the normalized content-bound revision identity.
The 361B migration reconciles the two.

A mutable absolute mount path is never identity: planning works on
corpus-root-relative display paths, so relocating the corpus mount changes
nothing and reports never leak absolute filesystem layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def normalize_label(value: str | None) -> str:
    """One normalization for every source label (one rule per concept):
    strip, collapse interior whitespace, casefold. Missing labels are the
    empty segment — legal in the key, never an empty-key collision, because
    the content hash always disambiguates."""
    if value is None:
        return ""
    return " ".join(value.split()).casefold()


def source_rev_key(vendor: str | None, product: str | None, version: str | None, sha256: str) -> str:
    """Unambiguous source-revision key. The sha256 segment is the raw hex
    digest (never normalized); equal keys mean equal bytes under equal
    labels, so equal keys are always safe to treat as one revision."""
    return f"{normalize_label(vendor)}|{normalize_label(product)}|{normalize_label(version)}|{sha256}"


def corpus_relpath(path_str: str, src: Path) -> str:
    """Corpus-root-relative display path for logs and reports (never an
    absolute path — reports must not leak mount layout). Falls back to the
    bare filename when the path escapes the corpus root."""
    try:
        return str(Path(path_str).relative_to(src))
    except ValueError:
        return Path(path_str).name


@dataclass(frozen=True)
class Duplicate:
    """A walked file skipped as a byte-identical copy: the deterministic
    winner (lexicographically smallest corpus-relpath) is ingested."""

    loser_rel: str
    winner_rel: str


def plan_duplicates(
    walk_entries: list[tuple[str, str]], src: Path
) -> tuple[list[tuple[str, str]], list[Duplicate]]:
    """Split walked (path, sha) entries into winners and byte-identical
    copies (issue #361 req 3). Same sha under several paths ingests exactly
    once: the lexicographically smallest corpus-relpath wins, so the choice
    never depends on walk order, hash seeds, or worker scheduling. Pure and
    deterministic — reruns elect the same winner without any stored state."""
    by_sha: dict[str, list[tuple[str, str]]] = {}
    for entry in walk_entries:
        by_sha.setdefault(entry[1], []).append(entry)
    kept: list[tuple[str, str]] = []
    duplicates: list[Duplicate] = []
    for entry in walk_entries:
        group = by_sha[entry[1]]
        if len(group) == 1:
            kept.append(entry)
            continue
        winner = min(group, key=lambda e: corpus_relpath(e[0], src))
        if entry == winner:
            kept.append(entry)
        else:
            duplicates.append(
                Duplicate(
                    loser_rel=corpus_relpath(entry[0], src),
                    winner_rel=corpus_relpath(winner[0], src),
                )
            )
    return kept, duplicates


@dataclass(frozen=True)
class Collision:
    """Several walked files resolving to one ``doc_id`` with different
    bytes. Ingesting any of them would delete the others' points
    (``delete_by_doc`` keys on bare ``doc_id``), so the run must abort
    before any delete/upsert. Members are (corpus-relpath, sha16) sorted
    by relpath — deterministic regardless of input order."""

    doc_id: str
    members: tuple[tuple[str, str], ...]


class RevisionCollisionError(RuntimeError):
    """Fail-closed planning abort (issue #361 req 4): the walked corpus
    contains distinct source revisions claiming one document id. The report
    carries corpus-relative paths and content ids only — never manual text
    or absolute filesystem details."""

    def __init__(self, collisions: list[Collision]) -> None:
        self.collisions = collisions
        lines = [
            (
                "revision collision: distinct walked inputs resolve to one doc_id "
                "with different content — ingesting would delete one revision's "
                "points (refusing before any delete/upsert):"
            )
        ]
        for collision in collisions:
            members = ", ".join(f"{rel}@{sha16}" for rel, sha16 in collision.members)
            lines.append(
                f"  doc_id {collision.doc_id!r}: {members}. If these are distinct "
                "revisions, give them distinct --vendor/--product/--version labels "
                "or ingest them as separate corpora (distinct revisions coexist "
                "after the #361 identity migration); if they are duplicate copies, "
                "keep one input."
            )
        super().__init__("\n".join(lines))


def find_collisions(
    resolved: list[tuple[str, str, str | None]], src: Path
) -> list[Collision]:
    """Group (path, sha, prescanned doc_id) triples by doc_id and report
    groups with more than one distinct sha (issue #361 req 4). Unresolvable
    inputs (None — the file cannot be opened; the parse worker owns that
    error) never join a group, so an unreadable file can neither cause nor
    hide a collision. Pure and order-independent."""
    by_doc: dict[str, dict[str, str]] = {}
    for path_str, sha, doc_id in resolved:
        if doc_id is None:
            continue
        by_doc.setdefault(doc_id, {})[corpus_relpath(path_str, src)] = sha
    collisions: list[Collision] = []
    for doc_id in sorted(by_doc):
        rel_to_sha = by_doc[doc_id]
        if len(set(rel_to_sha.values())) < 2:
            continue
        members = tuple(sorted((rel, sha[:16]) for rel, sha in rel_to_sha.items()))
        collisions.append(Collision(doc_id=doc_id, members=members))
    return collisions


def prescan_doc_ids(paths: list[str]) -> dict[str, str | None]:
    """Best-effort doc_id resolution for every planned path (issue #361
    req 4): the same ``ibm_pdf.resolve_doc_id`` helper the parse workers
    resolve through, so prescan keys and worker doc_ids cannot diverge.
    Unreadable files map to None and are excluded from collision grouping —
    prescan validates identity, never file health. Serial open + first-four-
    pages text per file; the parent already reads every byte for hashing,
    workers reopen for the full parse."""
    from mainframe_rag.ingest.ibm_pdf import resolve_doc_id

    return {path_str: resolve_doc_id(Path(path_str)) for path_str in paths}
