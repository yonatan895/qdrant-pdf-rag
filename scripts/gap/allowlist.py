#!/usr/bin/env python3
"""Gap release path allow-list and classifier (GH#1207 Rev 2 §3a, D2).

Every gap release is judged on its cumulative delta against its base upstream
release (`git diff --no-renames --name-only <gap_base> <tagged_sha>`). Every
changed path must match the allow-list; anything else is refused. A path is on
the list only when it feeds no image build step and no member copied from the
base bundle.

Patterns are root-anchored (`/path`) like GitLab CODEOWNERS so static signing
closure scanners know they are match patterns, never files opened from the
checkout.

Verdicts per path:
  - ``allowed-no-layer``: safe for gap release and does not enter the app
    images' ``src/`` or ``scripts/`` layers (Increment 1 packs these directly).
  - ``requires-layer-replacement``: allowed by D2 (``src/**`` or non-ignored
    ``scripts/**``), requires deterministic layer replacement (Increment 2;
    refused by Increment 1 pack).
  - ``refused``: feeds an image build step, a member copied from the base, or
    is not shown to be safe for gap release; goes upstream instead.

Standard library only (runs under ``python3 -I``).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Fixed refusal messages (GH#1207 Rev 2 §3a): never print paths inside the
# refusal line; operators inspect individual paths via `release.sh classify`.
REFUSAL_MESSAGE = (
    "STOP: gap release refused: the tagged commit differs from its base "
    "upstream release in paths that are not on the gap release allow-list "
    "(they feed an image build step or a member copied from the base, or are "
    "not shown to be safe). Nothing was packed. List them with "
    '"release.sh classify --tag <tag>"; revert them on main through a merge '
    "request, or report the change upstream and release it from there."
)

LAYER_REPLACEMENT_REFUSAL = (
    "STOP: gap release refused: the tagged commit changes src/ or scripts/ "
    "paths that require image layer replacement (Increment 2), which is not "
    "enabled in Increment 1. Nothing was packed. List them with "
    '"release.sh classify --tag <tag>".'
)

# Exact top-level files allowed (root-anchored patterns, no image layer impact).
ALLOWED_TOP_LEVEL_FILES: frozenset[str] = frozenset({
    "/.gitlab-ci.yml",
    "/AGENTS.md",
    "/README.md",
    "/ROADMAP.md",
    "/Taskfile.yml",
    "/airgap.env.example",
})

# Whole top-level directories allowed without image layer impact.
ALLOWED_NO_LAYER_DIRS: frozenset[str] = frozenset({
    "/.claude",
    "/.gitlab",
    "/benchmarks",
    "/deploy",
    "/docs",
    "/evals",
    "/gap",
    "/taskfiles",
    "/tests",
})

# Subtrees allowed inside otherwise refused top-level directories.
ALLOWED_NO_LAYER_SUBTREES: tuple[str, ...] = (
    "/charts/mainframe-rag/",
)

# Refused inside the allowed chart subtree (fail-closed): a nested/vendored
# subchart or lock file brings third-party chart code, with its own image
# references, into the deployed chart with no licence or provenance record
# (Rev 2 D2 refuses vendored charts). The allow-list sees paths only, so a
# `dependencies:` edit in Chart.yaml is not visible here; without a
# charts/ member or Chart.lock it cannot render offline.
CHART_SUBTREE_EXCLUSIONS: tuple[tuple[str, str], ...] = (
    ("/charts/mainframe-rag/charts", "nested or vendored subchart enters the deployed chart without licence or provenance record"),
    ("/charts/mainframe-rag/Chart.lock", "pins vendored chart dependencies"),
    ("/charts/mainframe-rag/*/Chart.lock", "pins vendored chart dependencies"),
)

# .agents/ is allowed except what feeds a member copied from the base (the
# reference-docs member and the licence records): the vendored skill trees,
# the Qdrant skills, and the provenance and pin files beside them.
AGENTS_DIR = "/.agents"
AGENTS_EXCLUSIONS: tuple[tuple[str, str], ...] = (
    ("/.agents/skills/qdrant-*", "feeds the copied reference-docs.tar.gz member and the licence records"),
    ("/.agents/vendor/*", "vendored third-party skills feed the licence records"),
    ("/.agents/*-provenance.md", "feeds the copied reference-docs.tar.gz member or the licence records"),
    ("/.agents/*.sha256", "pins a vendored third-party skill set"),
)

# Top-level directories whose files enter the app images' COPY layers
# (COPY src/ /app/src/ and COPY scripts/ /app/scripts/).
LAYER_DIRS: frozenset[str] = frozenset({"/src", "/scripts"})

# Explicit exclusions inside /scripts/** that MUST be refused (Rev 2 §3a):
# each feeds an earlier Containerfile step or a copied bundle member.
SCRIPT_EXCLUSIONS: tuple[tuple[str, str], ...] = (
    ("/scripts/dependency_lock.py", "copied before and run by the image install step"),
    ("/scripts/tools/*-pin.txt", "pins the bundled Task member or the dev kit"),
    ("/scripts/airgap/reference-docs.json", "defines the copied reference-docs.tar.gz member"),
    ("/scripts/airgap/reference-docs*.paths", "defines the copied reference-docs.tar.gz member"),
)

# Paths under /scripts/ excluded from app image build context by .dockerignore:
# changing these does not alter the COPY scripts/ layer.
DOCKERIGNORED_SCRIPT_PATHS: frozenset[str] = frozenset({
    "/scripts/mock_vllm.py",
})
DOCKERIGNORED_SCRIPT_PREFIXES: tuple[str, ...] = (
    "/scripts/ci/",
    "/scripts/gateway/",
)

# Every tracked top-level entry that is refused (or partially refused, like
# /charts), with its documented reason. Upstream tests assert every tracked
# top-level entry in the repository is explicitly decided here or in the
# allowed sets.
REFUSED_TOP_LEVEL: dict[str, str] = {
    "/.dockerignore": "Containerfile build context filter affects image layers",
    "/.gitattributes": "repository git metadata not shown safe for gap releases",
    "/.github": "connected-side GitHub workflows and templates",
    "/.gitignore": "repository git metadata not shown safe for gap releases",
    "/LICENSE": "feeds copied THIRD-PARTY-NOTICES.txt and reference-docs.tar.gz",
    "/LICENSE.mattpocock-skills": "feeds third-party license records",
    "/LICENSE.pstack-skills": "feeds third-party license records",
    "/LICENSE.qdrant-skills": "feeds copied reference-docs.tar.gz and license records",
    "/NOTICE.mattpocock-skills": "feeds third-party license records",
    "/NOTICE.pstack-skills": "feeds third-party license records",
    "/NOTICE.qdrant-skills": "feeds copied reference-docs.tar.gz and license records",
    "/bm25-weights.sha256": "input of the BM25 image build step",
    "/charts": "vendored charts outside charts/mainframe-rag/ (e.g. charts/qdrant-*.tgz) are bound in MANIFEST and SBOM",
    "/images": "Containerfiles and derived image build inputs",
    "/images.txt": "pins base, third-party and derived images copied from the base bundle",
    "/integrations": "Open WebUI integration inputs not shown safe for gap releases",
    "/licenses": "feeds copied THIRD-PARTY-NOTICES.txt and license inventory",
    "/locks": "input of the image wheelhouse install step",
    "/observability": "not shown safe for gap releases; no gap use known",
    "/pyproject.toml": "packaging and build metadata",
    "/release-archive.txt": "connected-side release archive metadata",
    "/requirements.build.lock.txt": "input of the image build wheelhouse step",
    "/requirements.dev.lock.txt": "input of the dev kit image",
    "/requirements.lock.txt": "input of the image wheelhouse install step",
    "/site": "public static website, never deployed in the gap",
    "/site-src": "public static website sources, never deployed in the gap",
}

# Declared exceptions where a Containerfile COPY source or pack.sh input is
# intentionally allowed (with its mechanism/guard) rather than refused.
DECLARED_EXCEPTIONS: dict[str, str] = {
    "/src": "replaced deterministically by image_layers.py (Increment 2; refused in Increment 1)",
    "/scripts": "replaced deterministically by image_layers.py (Increment 2; refused in Increment 1), with SCRIPT_EXCLUSIONS refused",
    "/README.md": "copied reference-docs.tar.gz keeps base project docs unchanged per D7",
    "/AGENTS.md": "copied reference-docs.tar.gz keeps base project docs unchanged per D7",
    "/docs": "copied reference-docs.tar.gz keeps base project docs unchanged per D7",
}

TOP_LEVEL_DECIDED: frozenset[str] = frozenset(
    entry.lstrip("/")
    for entry in (
        ALLOWED_TOP_LEVEL_FILES | ALLOWED_NO_LAYER_DIRS | LAYER_DIRS | frozenset(REFUSED_TOP_LEVEL) | {AGENTS_DIR}
    )
)


@dataclass(frozen=True)
class PathClassification:
    path: str
    verdict: str  # "allowed-no-layer" | "requires-layer-replacement" | "refused"
    reason: str


@dataclass(frozen=True)
class ClassificationReport:
    no_layer: tuple[PathClassification, ...]
    layer_replacement: tuple[PathClassification, ...]
    refused: tuple[PathClassification, ...]

    @property
    def all_allowed(self) -> bool:
        return not self.refused

    @property
    def needs_layer_replacement(self) -> bool:
        return bool(self.layer_replacement)

    def to_dict(self) -> dict[str, object]:
        return {
            "all_allowed": self.all_allowed,
            "needs_layer_replacement": self.needs_layer_replacement,
            "no_layer": [{"path": c.path, "reason": c.reason} for c in self.no_layer],
            "layer_replacement": [{"path": c.path, "reason": c.reason} for c in self.layer_replacement],
            "refused": [{"path": c.path, "reason": c.reason} for c in self.refused],
        }


def _normalize(raw: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        return ""
    norm = posixpath.normpath(raw)
    if norm in (".", "..") or norm.startswith("../"):
        return ""
    return norm


def classify_path(raw_path: str) -> PathClassification:
    """Classify one repository-relative path against the gap release allow-list."""
    path = _normalize(raw_path)
    if not path:
        return PathClassification(raw_path, "refused", "invalid or unsafe path")
    anchored = "/" + path

    if "/" not in path:
        if anchored in ALLOWED_TOP_LEVEL_FILES:
            return PathClassification(path, "allowed-no-layer", "allowed top-level file (not in any image)")
        reason = REFUSED_TOP_LEVEL.get(anchored, "top-level path is not on the gap release allow-list")
        return PathClassification(path, "refused", reason)

    top, _, _rest = path.partition("/")
    anchored_top = "/" + top

    if anchored_top in ALLOWED_NO_LAYER_DIRS:
        return PathClassification(path, "allowed-no-layer", f"allowed directory {top}/ (not in any image)")

    if anchored_top == AGENTS_DIR:
        for pattern, reason in AGENTS_EXCLUSIONS:
            # fnmatch's * also matches "/", so a directory pattern covers its subtree.
            if fnmatch.fnmatchcase(anchored, pattern) or fnmatch.fnmatchcase(anchored, pattern + "/*"):
                return PathClassification(path, "refused", f"excluded .agents/ path: {reason}")
        return PathClassification(path, "allowed-no-layer", "allowed .agents/ path (not in any image or copied member)")

    for prefix in ALLOWED_NO_LAYER_SUBTREES:
        if anchored.startswith(prefix):
            for pattern, reason in CHART_SUBTREE_EXCLUSIONS:
                if fnmatch.fnmatchcase(anchored, pattern) or fnmatch.fnmatchcase(anchored, pattern + "/*"):
                    return PathClassification(path, "refused", f"excluded chart path: {reason}")
            return PathClassification(path, "allowed-no-layer", f"allowed subtree {prefix.lstrip('/')} (rendered from checkout)")

    if top == "src":
        return PathClassification(path, "requires-layer-replacement", "enters app image COPY src/ layer (Increment 2)")

    if top == "scripts":
        for pattern, reason in SCRIPT_EXCLUSIONS:
            if fnmatch.fnmatchcase(anchored, pattern):
                return PathClassification(path, "refused", f"excluded scripts/ path: {reason}")
        if anchored in DOCKERIGNORED_SCRIPT_PATHS or any(anchored.startswith(p) for p in DOCKERIGNORED_SCRIPT_PREFIXES):
            return PathClassification(path, "allowed-no-layer", "excluded from image build context by .dockerignore")
        return PathClassification(
            path,
            "requires-layer-replacement",
            "enters app image COPY scripts/ layer (Increment 2)",
        )

    reason = REFUSED_TOP_LEVEL.get(anchored_top, f"top-level directory {top}/ is not on the gap release allow-list")
    return PathClassification(path, "refused", reason)


def classify_paths(paths: Iterable[str]) -> ClassificationReport:
    """Classify a set of repository-relative changed paths."""
    no_layer: list[PathClassification] = []
    layer_replacement: list[PathClassification] = []
    refused: list[PathClassification] = []
    for path in sorted(set(paths)):
        result = classify_path(path)
        if result.verdict == "allowed-no-layer":
            no_layer.append(result)
        elif result.verdict == "requires-layer-replacement":
            layer_replacement.append(result)
        else:
            refused.append(result)
    return ClassificationReport(
        no_layer=tuple(no_layer),
        layer_replacement=tuple(layer_replacement),
        refused=tuple(refused),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify changed paths against the gap release allow-list (D2).")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON report")
    parser.add_argument("paths", nargs="*", help="repository-relative paths to classify")
    args = parser.parse_args(argv)
    report = classify_paths(args.paths)
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for item in (*report.no_layer, *report.layer_replacement, *report.refused):
            print(f"{item.verdict}\t{item.path}\t{item.reason}")
    return 0 if report.all_allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
