"""Extraction-rules version (issue #124).

The payload content of every point — identifier regexes (#120), chrome
stripping, chunk boundaries, classify labels — is produced by five rule
modules. Nothing used to version that content: ingest skips on file sha
alone, so a retrieval-side regex widening silently desynced queries (new
rules) from payloads (old rules) and collapsed recall with no error
anywhere. This module computes the version instead of asking a human to
bump a string: sha256 over the source bytes of exactly the modules that
produce payload content. A rules change always changes the version; a
version match proves identical rules, which is what the resume skip and
the collection-level gate both key on.

Included files are the payload producers only — embed models, prompts,
and retrieval constants do not change what is stored, so they do not
belong here (model_ids/settings_hash cover those in the run manifest).
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

# Exactly the modules whose changes alter extracted payload content.
_RULE_MODULES: tuple[str, ...] = (
    "regexes.py",
    "ingest/ibm_pdf.py",
    "ingest/chrome.py",
    "ingest/chunk.py",
    "ingest/classify.py",
)


def rule_module_paths() -> tuple[Path, ...]:
    """Absolute paths of the payload-producing rule modules (test seam:
    tests pin the set; hashing is over bytes, so any packaging layout
    works as long as all five resolve)."""
    pkg_root = Path(__file__).resolve().parent.parent  # .../mainframe_rag
    paths = []
    for rel in _RULE_MODULES:
        p = pkg_root / rel
        if not p.exists():
            raise RuntimeError(f"extraction rules module missing: {rel}")
        paths.append(p)
    return tuple(paths)


def hash_rule_files(paths: tuple[Path, ...]) -> str:
    """Stable sha256 (first 16 hex) over (relative name, file bytes) pairs.
    The name is folded in so a module swap between paths cannot collide."""
    digest = hashlib.sha256()
    for p in sorted(paths, key=lambda x: x.name):
        digest.update(p.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(p.read_bytes())
    return digest.hexdigest()[:16]


@lru_cache(maxsize=1)
def extraction_rules_version() -> str:
    """Version of the payload-extraction rules currently on disk. Cached
    per process: workers spawn fresh processes and re-read, so a mid-run
    tree switch is still caught by the shell-safety rule, not by this
    cache."""
    return hash_rule_files(rule_module_paths())
