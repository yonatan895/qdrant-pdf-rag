"""Corpus planning scan: confinement and readability around `walk_pdfs`.

`walk_pdfs` (a rules-version module) is the corpus definition, but it follows
file symlinks wherever they point and its `rglob` swallows unreadable
directories. Publication certifies the WHOLE corpus, so the planner (not the
walker) must refuse both before any byte is parsed (issues #996, #1002,
#1003). Messages carry corpus-relative paths and fixed text only: never
absolute paths, exception text or manual content."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mainframe_rag.ingest.ibm_pdf import sha256_file
from mainframe_rag.ingest.walk import _IGNORED_DIRS, walk_pdfs

log = logging.getLogger(__name__)

UNREADABLE_FILE_TYPE = "UnreadableFile"
UNREADABLE_FILE_MESSAGE = "source file unreadable (permission or I/O error); document not ingested"


class CorpusScanError(RuntimeError):
    """Fail-closed planning refusal (stable `code`); the run never starts."""

    code = "corpus_scan"


class OutOfRootLinkError(CorpusScanError):
    code = "corpus_symlink_outside_root"

    def __init__(self, rels: list[str]) -> None:
        self.rels = rels
        super().__init__(
            f"{self.code}: {len(rels)} corpus file(s) are symlinks resolving outside the "
            f"corpus root (refusing before any parse): {', '.join(rels[:10])}"
        )


class UnreadableSubtreeError(CorpusScanError):
    code = "corpus_subtree_unreadable"

    def __init__(self, rels: list[str]) -> None:
        self.rels = rels
        super().__init__(
            f"{self.code}: {len(rels)} corpus director(ies)/file(s) cannot be traversed, so "
            f"the walk would be partial (refusing before any publication): {', '.join(rels[:10])}"
        )


class WalkedFileMovedError(CorpusScanError):
    """Parse-time re-check failed: the walked path now resolves outside the
    corpus root (a link swapped in after the scan). Fixed text only."""

    code = "walked_file_outside_root"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: walked file no longer resolves inside the corpus root")


class WalkedFileChangedError(CorpusScanError):
    """Parse-time re-check failed: the file's bytes differ from the walk-time
    digest. Fixed text only."""

    code = "walked_file_changed"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: walked file changed after the corpus scan")


def require_inside_root(path: Path, corpus_root: str | None) -> None:
    """Worker-side confinement re-check right before parsing (TOCTOU, #996)."""
    if corpus_root and not path.resolve().is_relative_to(Path(corpus_root).resolve()):
        raise WalkedFileMovedError


def private_verified_copy(path: Path, corpus_root: str, walk_sha: str, tmp_dir: str) -> tuple[Path, Path]:
    """Copy the walked file ONCE into a private directory at the same path
    relative to the corpus root, hashing the same bytes while copying, and
    refuse unless they equal the walk digest. Returns (copy, private root):
    the parser then reads exactly the verified bytes, whatever happens to the
    original path afterwards, and path-derived labels are unchanged."""
    root = Path(corpus_root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path.resolve().relative_to(root.resolve())
    private_root = Path(tmp_dir) / "root"
    target = private_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with open(path, "rb") as src, open(target, "wb") as dst:
        for block in iter(lambda: src.read(1 << 20), b""):
            digest.update(block)
            dst.write(block)
    if digest.hexdigest() != walk_sha:
        raise WalkedFileChangedError
    return target, private_root


@dataclass(frozen=True)
class CorpusScan:
    entries: list[tuple[str, str]]  # (path, sha256) of readable PDFs
    unreadable: list[str]  # paths that could not be read (per-file error records)


def _rel(path: Path | str, root: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return "<outside-root>"


def _traversal_problems(root: Path) -> list[str]:
    """Directories that cannot be listed, and PDF names that cannot be
    stat'ed (a directory without search permission), found by our own
    traversal because `rglob` hides both."""
    bad: list[str] = []

    def onerror(exc: OSError) -> None:
        bad.append(_rel(exc.filename or root, root))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        here = Path(dirpath)
        kept = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in _IGNORED_DIRS:
                continue
            if (here / name).is_symlink():
                # Policy (#996): a symlinked directory is never traversed.
                log.info(
                    json.dumps(
                        {"action": "symlinked_dir_skipped", "path": _rel(here / name, root)}
                    )
                )
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(".pdf"):
                continue
            try:
                os.stat(here / name)
            except PermissionError:
                bad.append(_rel(here / name, root))
            except OSError:
                pass  # dangling link etc.: not a file, as the walker already treats it
    return sorted(set(bad))


def confined_pdfs(src: Path) -> list[Path]:
    """`walk_pdfs(src)` minus nothing: either every walked PDF resolves
    inside the corpus root and the tree is fully traversable, or a typed
    `CorpusScanError` is raised."""
    bad_tree = _traversal_problems(src)
    if bad_tree:
        raise UnreadableSubtreeError(bad_tree)
    pdfs = walk_pdfs(src)
    real_root = src.resolve()
    outside = [p for p in pdfs if not p.resolve().is_relative_to(real_root)]
    if outside:
        raise OutOfRootLinkError([_rel(p, src) for p in outside])
    return pdfs


def split_readable(pdfs: list[Path]) -> tuple[list[Path], list[Path]]:
    readable: list[Path] = []
    unreadable: list[Path] = []
    for p in pdfs:
        try:
            with open(p, "rb") as handle:
                handle.read(1)
        except OSError:
            unreadable.append(p)
        else:
            readable.append(p)
    return readable, unreadable


def scan_corpus(src: Path, *, limit: int | None = None) -> CorpusScan:
    """Confined walk + hash; an unreadable file is reported, not raised."""
    pdfs = confined_pdfs(src)
    if limit:
        pdfs = pdfs[:limit]
    entries: list[tuple[str, str]] = []
    unreadable: list[str] = []
    for p in pdfs:
        try:
            entries.append((str(p), sha256_file(p)))
        except OSError:
            unreadable.append(str(p))
    return CorpusScan(entries, unreadable)


def unreadable_paths(src: Path) -> list[str]:
    """Readability probe for a pre-hashed (publication) walk."""
    return [str(p) for p in split_readable(confined_pdfs(src))[1]]
