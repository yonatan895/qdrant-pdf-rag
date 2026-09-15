#!/usr/bin/env python3
"""Offline structural check; supported Markdown subset is documented in agent-workflow.md."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT_BUDGET = 8192
CHAIN_BUDGET = 24576
REQUIRED = (
    "AGENTS.md", "docs/agent-workflow.md", ".github/ISSUE_TEMPLATE/agent-task.md",
    ".github/pull_request_template.md", "scripts/check_agent_context.py",
    "scripts/agent_doctor.py", "Makefile",
)
LINK = re.compile(r"\[([^\]\n]+)\]\(([^()\s]+)\)")
ANCHOR = re.compile(r'<a id="([a-z0-9-]+)"></a>')
CANONICAL_PREFIX = "https://github.com/yonatan895/qdrant-pdf-rag/blob/main/"
INSTRUCTION_NAMES = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md", "CONTEXT.md"}
# Generated/third-party trees are not first-party workflow policy.
EXCLUDED = {".git", ".venv", ".agents", "vendor", "dist", "bundles", "node_modules",
            "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "output"}


def local_target(root: Path, source: Path, target: str) -> tuple[Path, str]:
    path, _, anchor = target.partition("#")
    result = (source.parent / path).resolve() if path else source.resolve()
    if not result.is_relative_to(root) or Path(path).is_absolute():
        raise ValueError("local link must stay inside the repository")
    return result, anchor


def canonical_target(root: Path, target: str) -> tuple[Path, str]:
    """Map a canonical blob/main URL to a checkout path without network access."""
    actual, expected = urlsplit(target), urlsplit(CANONICAL_PREFIX)
    if ((actual.scheme, actual.netloc) != (expected.scheme, expected.netloc)
            or not actual.path.startswith(expected.path) or actual.query):
        raise ValueError(f"expected canonical repository URL under {CANONICAL_PREFIX}")
    relative = unquote(actual.path[len(expected.path):])
    if not relative:
        raise ValueError("canonical link must name a repository-relative file")
    return local_target(root, root/"AGENTS.md", relative+"#"+unquote(actual.fragment))


def is_template_source(root: Path, source: Path) -> bool:
    try:
        return source.relative_to(root).as_posix().startswith(".github/")
    except ValueError:
        return False


def table(text: str, name: str) -> list[tuple[int, list[str]]]:
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"{name}: expected one start/end marker pair")
    if text.index(end) < text.index(start):
        raise ValueError(f"{name}: end marker precedes start")
    section = text.split(start, 1)[1].split(end, 1)[0]
    rows = []
    for offset, line in enumerate(section.splitlines(), text[:text.index(start)].count("\n") + 1):
        if line.startswith("|") and line.endswith("|"):
            rows.append((offset, [cell.strip() for cell in line[1:-1].split("|")]))
    if len(rows) < 3:
        raise ValueError(f"{name}: table needs a header, separator and at least one row")
    return rows[2:]


def check(root: Path, client_limit: int | None = None) -> tuple[list[str], list[str]]:
    root = root.resolve()
    errors: list[str] = []
    notes: list[str] = []
    texts: dict[Path, str] = {}

    def read(path: Path) -> str:
        if not path.resolve().is_relative_to(root):
            errors.append(f"{path.relative_to(root)}: instruction/reference escapes repository")
            return ""
        if path not in texts:
            try:
                texts[path] = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError):
                errors.append(f"{path.relative_to(root)}: missing/unreadable UTF-8 file")
                texts[path] = ""
        return texts[path]

    def links(source: Path, text: str) -> None:
        template = is_template_source(root, source)
        for _, target in LINK.findall(text):
            try:
                url = urlsplit(target)
                canonical = target.startswith(CANONICAL_PREFIX) or (template and bool(url.scheme) and "/blob/" in url.path)
                if canonical:
                    path, anchor = canonical_target(root, target)
                elif url.scheme:
                    continue  # Unrelated external sources are never crawled.
                elif template:
                    errors.append(f"{source.relative_to(root)}: template links must use canonical repository URL: {target}")
                    continue
                else:
                    path, anchor = local_target(root, source, target)
            except ValueError as exc:
                errors.append(f"{source.relative_to(root)}: {target}: {exc}")
                continue
            if not path.exists():
                kind = "canonical" if canonical else "local"
                errors.append(f"{source.relative_to(root)}: broken {kind} reference {target}")
            elif anchor and (not path.is_file() or anchor not in ANCHOR.findall(read(path))):
                errors.append(f"{source.relative_to(root)}: missing explicit anchor {target}")

    for name in REQUIRED:
        if not (root / name).is_file():
            errors.append(f"{name}: required entry point missing")
    entry = root / "AGENTS.md"
    size = len(read(entry).encode("utf-8"))
    notes.append(f"AGENTS.md: {size}/{ROOT_BUDGET} UTF-8 bytes")
    if not size or size > ROOT_BUDGET:
        errors.append(f"AGENTS.md: root budget requires 1..{ROOT_BUDGET} bytes; got {size}")
    workflow = root / "docs/agent-workflow.md"
    content = read(workflow)
    for name in REQUIRED[:4]:
        links(root / name, read(root / name))

    try:
        owners = table(content, "context-map")
        contracts: set[str] = set()
        for line, cells in owners:
            label = f"docs/agent-workflow.md:{line} context-map"
            if len(cells) != 4 or not cells[0]:
                errors.append(f"{label}: expected contract, canonical owner, boundaries, evidence")
                continue
            if cells[0] in contracts:
                errors.append(f"{label}: duplicate contract {cells[0]}")
            contracts.add(cells[0])
            owner = LINK.fullmatch(cells[1])
            if not owner or ":" in owner[2] or not owner[2].split("#")[0].endswith(".md"):
                errors.append(f"{label}: {cells[0]} needs one canonical local Markdown owner link")
            if not LINK.search(cells[2]) or not LINK.search(cells[3]):
                errors.append(f"{label}: {cells[0]} needs boundary and evidence links")
    except ValueError as exc:
        errors.append(f"docs/agent-workflow.md: {exc}")

    covered: set[Path] = set()
    try:
        chains = table(content, "instruction-chains")
        for line, cells in chains:
            label = f"docs/agent-workflow.md:{line} instruction-chains"
            if len(cells) != 2:
                errors.append(f"{label}: expected invocation directory and chain links")
                continue
            directory = cells[0].strip("`")
            invocation, _ = local_target(root, root / "placeholder", directory)
            if not invocation.is_dir():
                errors.append(f"{label}: invocation directory {directory} does not exist")
            chain = [local_target(root, workflow, target)[0] for _, target in LINK.findall(cells[1])]
            if not chain or chain[0] != entry or len(set(chain)) != len(chain):
                errors.append(f"{label}: chain must start at root AGENTS.md with no duplicates")
            total = sum(len(read(path).encode("utf-8")) for path in chain) + max(0, len(chain)-1)*2
            covered.update(chain)
            ancestors = [invocation, *invocation.parents]
            expected = {directory/name for directory in ancestors if directory.is_relative_to(root)
                        for name in INSTRUCTION_NAMES if (directory/name).is_file()}
            if set(chain) != expected:
                errors.append(f"{label}: declared chain differs from instructions on {directory}'s ancestor path")
            previous = root
            for path in chain:
                if path.name not in INSTRUCTION_NAMES or not invocation.is_relative_to(path.parent):
                    errors.append(f"{label}: {path.relative_to(root)} is not an instruction on this directory chain")
                if not path.parent.is_relative_to(previous):
                    errors.append(f"{label}: chain must be ordered from root to working directory")
                previous = path.parent
            notes.append(f"chain {directory}: {total}/{CHAIN_BUDGET} UTF-8 bytes including separators")
            if total > CHAIN_BUDGET:
                errors.append(f"{label}: chain budget exceeded ({total} > {CHAIN_BUDGET})")
            if client_limit is not None and total >= client_limit:
                errors.append(f"{label}: {total} bytes must be below effective client limit {client_limit}")
    except ValueError as exc:
        errors.append(f"docs/agent-workflow.md: {exc}")

    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED and not Path(directory, name).is_symlink()]
        for name in INSTRUCTION_NAMES.intersection(names):
            path = Path(directory, name)
            if path not in covered:
                errors.append(f"{path.relative_to(root)}: instruction file missing from audited chains")
    return errors, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--client-limit", type=int, help="known effective client byte limit; no global config read")
    args = parser.parse_args(argv)
    if args.client_limit is not None and args.client_limit <= 0:
        parser.error("--client-limit must be positive")
    errors, notes = check(args.root, args.client_limit)
    print("\n".join(notes))
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        return 1
    print("Context structure OK; semantic review and actual loader audit remain separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
