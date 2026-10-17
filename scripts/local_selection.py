"""Deterministic test selection for local verification (#796 G4/G5).

A pure function of the repository tree and the changed paths. Every rule only
adds tests:

- import closure: test modules that transitively import a changed module;
- references: modules whose string literals name a changed file (its repo
  path, dotted module name, or the basename of a script or non-Python file),
  then the test modules importing them (subprocess-driven tests and helpers);
- contracts: the evidence suites of context-map rows whose boundaries link a
  changed file.

Shared test inputs, dependency/lock inputs and the Task runner are broad: they
escalate to T2 (all unit modules except the slow ones). A changed file under
src/ or scripts/ that maps to no test is unmapped; the runner refuses it unless
the caller covers it with an explicit, recorded selection. Dynamic dispatch
the static rules cannot see is the known gap; CI's unit lane is the backstop.
"""
from __future__ import annotations

import ast
import dataclasses
import posixpath
import re
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.review_tooling import SHARED_TEST_INPUTS, _match_any

SOURCE_TREES = ('src/mainframe_rag', 'scripts', 'tests')
INTERNAL_ROOTS = ('mainframe_rag', 'scripts', 'tests')
BROAD_PATTERNS = sorted(SHARED_TEST_INPUTS) + [
    'pyproject.toml', 'requirements*.txt', 'locks/**', 'Taskfile.yml', 'taskfiles/**', 'scripts/tools/**']
# The two modules that take ~60% of unit time in CI (#796); they run when affected.
SLOW_MODULES = ('tests/test_colab.py', 'tests/test_ingest_publish.py')
GUARDED_PATTERNS = ['src/**', 'scripts/**']
CONTEXT_MAP = 'docs/agent-workflow.md'
_LINK = re.compile(r'\]\(([^)#\s]+)')


def module_name(path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.suffix != '.py' or not any(path.startswith(tree + '/') for tree in SOURCE_TREES):
        return None
    parts = list(pure.with_suffix('').parts)
    if parts[0] == 'src':
        parts = parts[1:]
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.startswith('tests/') and path.count('/') == 1 and (
        name.startswith('test_') or name.endswith('_test.py')) and name.endswith('.py')


@dataclasses.dataclass(frozen=True)
class Graph:
    paths: dict[str, str]                  # module name -> repo path
    importers: dict[str, frozenset[str]]   # module name -> internal modules importing it
    strings: dict[str, tuple[str, ...]]    # module name -> its string literals
    evidence: dict[str, frozenset[str]]    # boundary path -> context-map evidence test paths


def _imports(name: str, is_package: bool, tree: ast.AST) -> set[str]:
    found: set[str] = set()
    package = name if is_package else name.rpartition('.')[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                anchor = package.split('.') if package else []
                anchor = anchor[:len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = '.'.join([*anchor, *([base] if base else [])])
            targets = [base] + [f'{base}.{alias.name}' for alias in node.names if base]
        else:
            continue
        for target in targets:
            parts = target.split('.')
            if parts and parts[0] in INTERNAL_ROOTS:
                found.update('.'.join(parts[:i]) for i in range(1, len(parts) + 1))
    found.discard(name)
    return found


def _context_evidence(root: Path) -> dict[str, frozenset[str]]:
    text = (root / CONTEXT_MAP).read_text(encoding='utf-8')
    table = text.split('<!-- context-map:start -->', 1)[1].split('<!-- context-map:end -->', 1)[0]
    mapping: dict[str, set[str]] = defaultdict(set)
    for row in table.splitlines():
        cells = row.split('|')
        if len(cells) < 6 or cells[1].strip() in ('Contract', '') or set(cells[1].strip()) <= {'-'}:
            continue

        def targets(cell: str) -> set[str]:
            return {posixpath.normpath(posixpath.join('docs', link)) for link in _LINK.findall(cell)}

        tests = {t for t in targets(cells[4]) if is_test_module(t)}
        for boundary in targets(cells[3]):
            mapping[boundary].update(tests)
    return {k: frozenset(v) for k, v in mapping.items()}


def build_graph(root: Path = ROOT) -> Graph:
    paths: dict[str, str] = {}
    importers: dict[str, set[str]] = defaultdict(set)
    strings: dict[str, tuple[str, ...]] = {}
    for tree_dir in SOURCE_TREES:
        for file in sorted((root / tree_dir).rglob('*.py')):
            rel = file.relative_to(root).as_posix()
            if '__pycache__' in rel:
                continue
            name = module_name(rel)
            if name is None:
                continue
            try:
                tree = ast.parse(file.read_text(encoding='utf-8'), filename=rel)
            except (SyntaxError, UnicodeDecodeError):
                tree = ast.Module(body=[], type_ignores=[])
            paths[name] = rel
            for target in _imports(name, file.name == '__init__.py', tree):
                importers[target].add(name)
            strings[name] = tuple(sorted({n.value for n in ast.walk(tree)
                                          if isinstance(n, ast.Constant) and isinstance(n.value, str)}))
    return Graph(paths, {k: frozenset(v) for k, v in importers.items()}, strings, _context_evidence(root))


def _closure(graph: Graph, starts: set[str]) -> set[str]:
    seen, stack = set(starts), list(starts)
    while stack:
        for importer in graph.importers.get(stack.pop(), ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return seen


def importing_tests(graph: Graph, module: str) -> set[str]:
    """Test module paths whose import closure reaches `module` (no string references)."""
    return {graph.paths[m] for m in _closure(graph, {module})
            if m in graph.paths and is_test_module(graph.paths[m])}


def _tokens(path: str) -> list[re.Pattern[str]]:
    words = [path]
    name = module_name(path)
    if name:
        words.append(name)
    base = PurePosixPath(path).name
    if (path.startswith('scripts/') or not path.endswith('.py')) and base != '__init__.py':
        words.append(base)
    return [re.compile(r'(?<![\w.-])' + re.escape(word) + r'(?![\w])') for word in words]


def tests_for(graph: Graph, path: str) -> set[str]:
    """Test module paths that exercise `path` under the static rules above."""
    name = module_name(path)
    affected = _closure(graph, {name}) if name else set()
    files = {path} | {graph.paths[m] for m in affected if m in graph.paths}
    patterns = [p for file in sorted(files) for p in _tokens(file)]
    referencing = {m for m, literals in graph.strings.items()
                   if any(p.search(s) for s in literals for p in patterns)}
    modules = affected | _closure(graph, referencing)
    found = {graph.paths[m] for m in modules if m in graph.paths and is_test_module(graph.paths[m])}
    return found | set(graph.evidence.get(path, ()))


@dataclasses.dataclass(frozen=True)
class Selection:
    tier: str                         # 'T1' or 'T2'
    tests: tuple[str, ...]            # sorted test module paths to run
    reasons: dict[str, tuple[str, ...]]  # test path -> changed paths that selected it
    broad: tuple[str, ...]            # changed paths that escalated to T2
    unmapped: tuple[str, ...]         # guarded changed paths no rule maps

    def as_record(self) -> dict:
        return {'tier': self.tier, 'tests': list(self.tests), 'broad': list(self.broad),
                'unmapped': list(self.unmapped), 'reasons': {k: list(v) for k, v in self.reasons.items()}}


def all_unit_modules(root: Path = ROOT) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in (root / 'tests').glob('*.py')
                  if is_test_module(p.relative_to(root).as_posix()))


def select(changed: list[str], *, root: Path = ROOT, graph: Graph | None = None,
           force_t2: bool = False) -> Selection:
    graph = graph or build_graph(root)
    reasons: dict[str, set[str]] = defaultdict(set)
    broad, unmapped = [], []
    for path in sorted(set(changed)):
        if _match_any(path, BROAD_PATTERNS):
            broad.append(path)
            continue
        hits = tests_for(graph, path)
        for test in hits:
            reasons[test].add(path)
        if not hits and (root / path).exists() and _match_any(path, GUARDED_PATTERNS):
            unmapped.append(path)
    tier = 'T2' if broad or force_t2 else 'T1'
    tests = set(reasons)
    if tier == 'T2':
        tests |= {t for t in all_unit_modules(root) if t not in SLOW_MODULES}
    return Selection(tier, tuple(sorted(tests)), {k: tuple(sorted(v)) for k, v in sorted(reasons.items())},
                     tuple(broad), tuple(unmapped))
