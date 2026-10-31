#!/usr/bin/env python3
"""Offline reference docs member of the release bundle (GH#1174).

CONNECTED SIDE ONLY. ``pack.sh`` runs ``build``: every doc set pinned in
``scripts/airgap/reference-docs.json`` is read from a cache directory of
archives named by their sha256 (``--cache``) or downloaded from its pinned URL,
verified against the pinned sha256 (fail closed on a mismatch), and only its
kept subset plus its licence and notice files is copied out. The project's own
docs come from ``git archive HEAD`` (``--project-tar``). The result is one
deterministic ``reference-docs.tar.gz`` holding ``reference-docs/INDEX.md``,
``reference-docs/FILES.sha256`` (one line per file), ``reference-docs/sets/<id>/``
and ``reference-docs/project/``. Raw sources only: nothing upstream is built,
rendered or executed, and nothing is fetched at runtime or in the gap.

Archives are untrusted input even after the checksum matches: an absolute
member name, a ``..`` component, an unusual character, a second top-level
directory or a duplicate name refuses the whole archive, and a link, device or
FIFO inside the kept subset refuses the set. The output holds regular files
only, so ``bootstrap.sh`` can refuse anything else.

  build     produce the member (pack time)
  check     validate the manifest and print the set count (no network)
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
TOP = "reference-docs"
INDEX = "INDEX.md"
FILES = "FILES.sha256"
# Every set must carry a redistribution licence (maintainer decision on GH#1174):
# a set without one cannot be expressed in the manifest.
NO_LICENCE = frozenset({"", "NOASSERTION", "NONE"})
SET_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# No control character (newline included) and no backslash: names stay plain in
# sha256sum lines and in `tar -t` listings, which bootstrap.sh checks.
SAFE_PART = re.compile(r"^[^\x00-\x1f\x7f\\]+$")
# Hard caps against an endless or hostile input (the pins bound the real sizes).
MAX_DOWNLOAD_BYTES = 2 * 1024**3
MAX_KEPT_BYTES = 1024**3
CHUNK = 1024 * 1024
TIMEOUT_S = 120
REQUIRED = ("id", "component", "version", "source", "url", "sha256", "license", "license_files", "include")
OPTIONAL = ("exclude", "include_list", "max_file_bytes", "attribution", "entry", "notes")


class RefDocsError(Exception):
    """A fixed, reportable refusal (no archive contents in the message)."""


@dataclass
class DocSet:
    id: str
    component: str
    version: str
    source: str
    url: str
    sha256: str
    license: str
    license_files: list[str]
    include: list[str]
    exclude: list[str] = field(default_factory=list)
    include_list: str | None = None
    max_file_bytes: int | None = None
    attribution: str | None = None
    entry: str | None = None
    notes: str | None = None
    listed: frozenset[str] = frozenset()


@dataclass
class Kept:
    files: dict[str, tuple[int, str]] = field(default_factory=dict)  # relative path -> (size, sha256)
    skipped_large: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(size for size, _ in self.files.values())


# --- manifest -----------------------------------------------------------------


def safe_relative(name: str) -> bool:
    if not name or name.startswith("/") or name.endswith("/"):
        return False
    return all(part not in ("", ".", "..") and SAFE_PART.match(part) for part in name.split("/"))


def load_manifest(path: Path) -> list[DocSet]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RefDocsError("reference docs manifest is missing or not valid JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("sets"), list):
        raise RefDocsError("unsupported reference docs manifest schema")
    sets: list[DocSet] = []
    seen: set[str] = set()
    for raw in data["sets"]:
        if not isinstance(raw, dict) or set(raw) - set(REQUIRED) - set(OPTIONAL) or any(k not in raw for k in REQUIRED):
            raise RefDocsError("a reference docs set has missing or unknown fields")
        ident = raw["id"]
        if not isinstance(ident, str) or not SET_ID.match(ident) or ident in seen:
            raise RefDocsError("a reference docs set id is invalid or duplicated")
        seen.add(ident)
        doc = DocSet(**raw)
        problem = set_problem(doc)
        if problem:
            raise RefDocsError(f"set {ident}: {problem}")
        if doc.include_list:
            doc.listed = frozenset(read_list(path.parent / doc.include_list, ident))
        sets.append(doc)
    if not sets:
        raise RefDocsError("the reference docs manifest lists no set")
    return sets


def set_problem(doc: DocSet) -> str | None:
    texts = (doc.component, doc.version, doc.source, doc.license)
    if not all(isinstance(t, str) and t.strip() and "\n" not in t and "|" not in t for t in texts):
        return "component, version, source and license must be one-line text without '|'"
    if not doc.url.startswith("https://"):
        return "url must be https"
    if not SHA256.match(doc.sha256):
        return "sha256 must be 64 lowercase hex digits"
    for name in ("license_files", "include", "exclude"):
        value = getattr(doc, name)
        if not isinstance(value, list) or not all(isinstance(v, str) and safe_pattern(v) for v in value):
            return f"{name} must be a list of relative paths or globs"
    if not all(safe_relative(f) for f in doc.license_files):
        return "license_files must be plain relative paths"
    if doc.license.strip().upper() in NO_LICENCE:
        return "every set needs an upstream redistribution licence; a set without one is not shipped"
    if not doc.license_files:
        return "a set must name its licence files"
    if not doc.include and not doc.include_list:
        return "include or include_list must select something"
    if doc.include_list is not None and not (isinstance(doc.include_list, str) and safe_relative(doc.include_list)):
        return "include_list must name a file beside the manifest"
    if doc.max_file_bytes is not None and (not isinstance(doc.max_file_bytes, int) or doc.max_file_bytes <= 0):
        return "max_file_bytes must be a positive integer"
    if doc.entry is not None and not safe_relative(doc.entry):
        return "entry must be a plain relative path"
    return None


def safe_pattern(pattern: str) -> bool:
    return bool(pattern) and not pattern.startswith("/") and ".." not in pattern.split("/") and "\\" not in pattern


def read_list(path: Path, ident: str) -> list[str]:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        raise RefDocsError(f"set {ident}: include_list file is missing") from exc
    listed = [line for line in lines if line and not line.startswith("#")]
    if not listed or not all(safe_relative(p) for p in listed) or len(set(listed)) != len(listed):
        raise RefDocsError(f"set {ident}: include_list must hold unique plain relative paths")
    return listed


def selected(doc: DocSet, relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(path.full_match(p) for p in doc.exclude):
        return False
    if relative in doc.listed:
        return True
    return any(path.full_match(p) for p in doc.include)


# --- archives -----------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "mainframe-rag-pack"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response, target.open("xb") as out:
            while block := response.read(CHUNK):
                total += len(block)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RefDocsError("download exceeds the size cap")
                out.write(block)
    except OSError as exc:
        raise RefDocsError("download failed") from exc


def obtain(doc: DocSet, cache: Path | None, work: Path, offline: bool) -> tuple[Path, bool]:
    """(verified archive path, whether it is a temporary download)."""
    cached = cache / doc.sha256 if cache else None
    if cached is not None and cached.is_file() and not cached.is_symlink():
        if sha256_file(cached) != doc.sha256:
            raise RefDocsError(f"set {doc.id}: cached archive does not match the pinned sha256")
        return cached, False
    if offline:
        raise RefDocsError(f"set {doc.id}: archive {doc.sha256} is not in the offline cache")
    target = work / f"download-{doc.id}"
    target.unlink(missing_ok=True)
    try:
        download(doc.url, target)
        if sha256_file(target) != doc.sha256:
            raise RefDocsError(f"set {doc.id}: downloaded archive does not match the pinned sha256")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target, True


def extract_kept(archive: Path, dest: Path, want, *, strip_root: bool, label: str,
                 max_file_bytes: int | None = None, always=frozenset()) -> Kept:
    """Copy the selected regular files of an untrusted archive into dest."""
    kept = Kept()
    root: str | None = None
    names: set[str] = set()
    try:
        with tarfile.open(archive, "r|*") as handle:
            for member in handle:
                name = member.name
                if not safe_relative(name.rstrip("/") if member.isdir() else name):
                    raise RefDocsError(f"{label}: archive member name is absolute, escapes or is unusual")
                if name in names:
                    raise RefDocsError(f"{label}: archive holds a member name twice")
                names.add(name)
                parts = name.rstrip("/").split("/", 1)
                if strip_root:
                    if root is None:
                        root = parts[0]
                    elif parts[0] != root:
                        raise RefDocsError(f"{label}: archive has more than one top-level directory")
                    if len(parts) == 1:
                        continue
                    relative = parts[1]
                else:
                    relative = name.rstrip("/")
                if not want(relative):
                    continue
                if member.isdir():
                    continue
                if not member.isreg():
                    raise RefDocsError(f"{label}: kept path {relative} is a link or special file")
                if max_file_bytes and member.size > max_file_bytes and relative not in always:
                    kept.skipped_large.append(relative)
                    continue
                if kept.size + member.size > MAX_KEPT_BYTES:
                    raise RefDocsError(f"{label}: kept subset exceeds the size cap")
                kept.files[relative] = write_member(handle, member, dest / relative)
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as exc:
        raise RefDocsError(f"{label}: archive cannot be read") from exc
    return kept


def write_member(handle: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> tuple[int, str]:
    stream = handle.extractfile(member)
    if stream is None:
        raise RefDocsError("archive member cannot be read")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with target.open("xb") as out:
        while block := stream.read(CHUNK):
            digest.update(block)
            size += len(block)
            out.write(block)
    if size != member.size:
        raise RefDocsError("archive member is truncated")
    return size, digest.hexdigest()


def extract_set(doc: DocSet, archive: Path, dest: Path) -> Kept:
    licences = frozenset(doc.license_files)
    kept = extract_kept(archive, dest, lambda rel: rel in licences or selected(doc, rel), strip_root=True,
                        label=f"set {doc.id}", max_file_bytes=doc.max_file_bytes, always=licences)
    missing = sorted(licences - set(kept.files))
    if missing:
        raise RefDocsError(f"set {doc.id}: licence file {missing[0]} is missing from the archive")
    absent = sorted(set(doc.listed) - set(kept.files))
    if absent:
        raise RefDocsError(f"set {doc.id}: listed path {absent[0]} is missing from the archive")
    for pattern in doc.include:
        if not any(PurePosixPath(rel).full_match(pattern) for rel in kept.files):
            raise RefDocsError(f"set {doc.id}: include {pattern} matches no file")
    if doc.entry and doc.entry not in kept.files:
        raise RefDocsError(f"set {doc.id}: entry {doc.entry} is not a kept file")
    return kept


# --- index, checksums and the member -------------------------------------------


def human(size: int) -> str:
    return f"{size / 1024**2:.2f} MB" if size >= 1024**2 else f"{size / 1024:.1f} KB"


def render_index(sets: list[DocSet], kept: dict[str, Kept], project: Kept, source_sha: str) -> str:
    lines = [
        "# Offline reference documentation",
        "",
        f"Packed from commit `{source_sha}` by `scripts/airgap/pack.sh` (GH#1174). Every upstream set",
        "is the raw source at a pinned commit or release archive whose sha256 was verified at pack",
        "time; only the subset below and its licence files were kept. Nothing here was built or",
        "rendered: Markdown, MDX, AsciiDoc, reStructuredText and Hugo/Docusaurus shortcodes show",
        "as source, and site-absolute links do not resolve offline. `FILES.sha256` lists every",
        "file (`cd reference-docs && sha256sum -c FILES.sha256`). Licence status of each set is in",
        "THIRD-PARTY-NOTICES.txt of the bundle.",
        "",
        "## Project documentation",
        "",
        f"`project/` is this repository's own documentation at `{source_sha}` (Apache-2.0, `project/LICENSE`):",
        "`project/README.md`, `project/AGENTS.md`, `project/docs/` (start at `project/docs/architecture.md`;",
        "ADRs in `project/docs/adr/`) and the vendored Qdrant skills in `project/.agents/skills/`.",
        f"{len(project.files)} files, {human(project.size)}.",
        "",
        "## Upstream sets",
        "",
        "The upstream user manuals of Qdrant (qdrant.tech), Open WebUI (docs.openwebui.com) and",
        "OpenObserve (openobserve.ai/docs) are not included: upstream publishes them with no licence.",
        "Use the Qdrant API specs and reference config (`sets/qdrant-server/`), the vendored Qdrant",
        "skills (`project/.agents/skills/`) and the Open WebUI and OpenObserve READMEs instead.",
        "",
        "| Set | Component | Version | Source | Licence | Path | Files | Size |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for doc in sets:
        got = kept[doc.id]
        path = f"sets/{doc.id}/" + (doc.entry or "")
        lines.append(f"| {doc.id} | {doc.component} | {doc.version} | {doc.source} | {doc.license} | "
                     f"`{path}` | {len(got.files)} | {human(got.size)} |")
    lines += ["", "## Set notes", ""]
    for doc in sets:
        got = kept[doc.id]
        lines.append(f"### {doc.id}")
        lines.append("")
        lines.append(f"- Archive: {doc.url} (sha256 `{doc.sha256}`)")
        lines.append("- Licence files: " + ", ".join(f"`sets/{doc.id}/{f}`" for f in doc.license_files))
        if doc.attribution:
            lines.append(f"- Attribution: {doc.attribution}")
        if got.skipped_large:
            lines.append(f"- Left out: {len(got.skipped_large)} files over {doc.max_file_bytes} bytes")
        if doc.notes:
            lines.append(f"- Notes: {doc.notes}")
        lines.append("")
    return "\n".join(lines)


def file_list(top: Path) -> list[str]:
    out = []
    for path in top.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise RefDocsError("staging holds a link or special file")
        if path.is_file():
            out.append(path.relative_to(top).as_posix())
    return sorted(out)


def write_member_archive(top: Path, output: Path, mtime: int) -> tuple[int, int]:
    """Deterministic gzip tar of every regular file under top (as reference-docs/...)."""
    names = file_list(top)
    raw = 0
    with output.open("xb") as handle, gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0,
                                                   compresslevel=9) as zipped, \
            tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            path = top / name
            info = tarfile.TarInfo(f"{TOP}/{name}")
            info.size = path.stat().st_size
            info.mtime = mtime
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            raw += info.size
            with path.open("rb") as data:
                archive.addfile(info, data)
    return raw, output.stat().st_size


def build(args: argparse.Namespace) -> int:
    sets = load_manifest(args.manifest)
    if args.output.exists():
        raise RefDocsError("output already exists")
    args.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work, prefix="refdocs-") as scratch:
        scratch_dir = Path(scratch)
        top = scratch_dir / TOP
        (top / "sets").mkdir(parents=True)
        project = extract_kept(args.project_tar, top / "project", lambda _rel: True, strip_root=False,
                               label="project docs")
        if "LICENSE" not in project.files:
            raise RefDocsError("project docs: LICENSE is missing")
        kept: dict[str, Kept] = {}
        for doc in sets:
            archive, temporary = obtain(doc, args.cache, scratch_dir, args.offline)
            try:
                kept[doc.id] = extract_set(doc, archive, top / "sets" / doc.id)
            finally:
                if temporary:
                    archive.unlink(missing_ok=True)
            print(f"reference-docs: {doc.id}: {len(kept[doc.id].files)} files, {human(kept[doc.id].size)}")
        (top / INDEX).write_text(render_index(sets, kept, project, args.source_sha) + "\n", encoding="utf-8")
        sums = [f"{sha256_file(top / name)}  {name}" for name in file_list(top)]
        (top / FILES).write_text("\n".join(sums) + "\n", encoding="utf-8")
        partial = args.output.with_name(args.output.name + ".partial")
        partial.unlink(missing_ok=True)
        try:
            raw, packed = write_member_archive(top, partial, args.mtime)
            partial.rename(args.output)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    total_files = len(sums) + 1
    print(f"reference-docs: OK sets={len(sets)} files={total_files} uncompressed={raw} compressed={packed}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    child = sub.add_parser("build")
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--project-tar", type=Path, required=True, help="git archive of the project docs")
    child.add_argument("--cache", type=Path, help="directory of pre-downloaded archives named by sha256")
    child.add_argument("--offline", action="store_true", help="never download; every archive must be cached")
    child.add_argument("--work", type=Path, required=True, help="scratch directory (downloads, staging)")
    child.add_argument("--source-sha", required=True)
    child.add_argument("--mtime", type=int, required=True, help="file timestamp in the member (commit time)")
    child.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "check":
            sets = load_manifest(args.manifest)
            print(f"reference-docs: manifest OK sets={len(sets)}")
            return 0
        return build(args)
    except RefDocsError as exc:
        print(f"reference-docs: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
