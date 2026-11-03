"""The files the gap's signing, packing and verification entry points execute (#588 N1).

ADR-0009 section 6 puts the release signers and verifiers on the named-approver list. The code
they run must be there too, or a group-only change to a sourced helper changes what is packed,
signed or accepted. ``derive()`` follows the entry points through the repository and returns
every tracked file they reach; ``scripts/gap_codeowners.py`` keeps the result as data
(``SIGNING_CLOSURE``) and ``tests/test_gap_codeowners.py`` pins the two equal, so a new
dependency fails the test until the generator lists it.

Edges followed, from each reached file:

- shell: ``.``/``source`` of a file; ``sh``/``bash``/``dash`` of a script, ``-c`` strings
  (with their positional arguments) and stdin heredocs; ``python``/``python3`` of a script,
  ``-m`` module, ``-c`` code and ``-`` heredoc; a command word that is a repository path;
  ``eval`` strings, read as shell; calls of a WRAPPERS function, read as the command they
  run; and every word that is exactly a tracked path (pins, manifests, files copied into
  the bundle), except a ``git`` pathspec after ``--`` and ``tar`` member names, which name
  commit objects and archive members, not checkout files. A path is the repository root after a variable
  assigned ``$(cd -- "$(dirname -- "$0")/REL" && pwd)`` (with ``$0`` the executed script, also
  inside sourced files), ``$(dirname -- "$0")``, a variable assigned such a path, a variable in
  ROOT_VARIABLES, or a repository-relative word.
- Python: ``import``/``from`` statements, resolved against the repository root and the
  directory of every reached Python file (the script directory Python puts first on
  ``sys.path``), before the standard library, so a planted ``json.py`` next to a script is a
  dependency; and every string literal, or ``/`` chain of literals, that names a tracked file
  from the root or the file's own directory.
- A reached ``.sh`` or ``.py`` file is followed as code, however it was reached.

Fail closed: a sourced or executed reference that names no tracked file, an import that is
neither in the repository nor in the standard library, a dynamic import or ``exec``/``eval``
in Python, a dynamic shell command word (``$cmd``, ``eval "$cmd"``) and a file that cannot be
read or parsed are each reported, and ``derive()`` raises ``Unresolved`` listing all of them,
unless REVIEWED records that exact reference with its reason. An unused REVIEWED entry is
reported too.

Blind spots: data paths assembled at run time from variables, loops or string formatting
(a ``for f in a b`` loop over names, ``ROOT / name``); a file read through a tracked
directory rather than by name (``licenses/``: its notice files are pinned by hash in
``licenses/inventory.json``, which is reached); the text ``eval`` sees at run time; shell
control flow (every command of a reached file counts, reachable or not); and code a reached
program loads from outside the repository (installed packages, the bundle members it
verifies).
"""
from __future__ import annotations

import ast
import posixpath
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Variables that hold the repository root without a `cd "$(dirname "$0")..."` assignment
# the scan can read, by the file that uses them.
ROOT_VARIABLES: Mapping[tuple[str, str], str] = {
    ('scripts/airgap/bootstrap.sh', 'DEST_DIR'):
        'the workspace bootstrap clones from the verified repo.bundle: this repository',
}

# Execution references that name no tracked file, by (file, word as scanned): the repository
# path they run, or None for a file outside the repository, with the reason.
REVIEWED: Mapping[tuple[str, str], tuple[str | None, str]] = {
    ('scripts/airgap/common.sh', '$AIRGAP_ENV'):
        (None, 'the operator settings file, untracked by policy (AGENTS.md)'),
    ('scripts/airgap/common.sh', './airgap.env'):
        (None, 'the operator settings file, untracked by policy (AGENTS.md)'),
    ('scripts/devkit/install.sh', '$KIT/image_manifest.py'):
        ('scripts/airgap/image_manifest.py', 'the kit copy scripts/devkit/pack.sh makes of it'),
    ('scripts/gap/import_release.sh', 'bootstrap.sh'):
        ('scripts/airgap/bootstrap.sh', "the verified bundle's copy that pack.sh makes of it"),
    ('scripts/airgap/common.sh', '$@'):
        (None, 'run() runs its arguments; each call of a WRAPPERS function is scanned as a command'),
    ('scripts/airgap/common.sh', '$_decisions'):
        (None, 'eval of the shell assignments scripts/airgap/map_values.py prints (reached)'),
    ('scripts/airgap/common.sh', '$KC'): (None, 'oc or kubectl, as kc() selects'),
    ('scripts/airgap/image_identity.sh', '$KC'): (None, 'oc or kubectl, as kc() selects'),
    ('scripts/devkit/install.sh', '$KC'): (None, 'oc or kubectl, as kc() selects'),
    ('scripts/tools/install-helm.sh', '$TMP/linux-amd64/helm'):
        (None, 'the helm binary it just unpacked and checked against scripts/tools/helm-pin.txt'),
    ('scripts/tools/install-task.sh', '$TMP/task'):
        (None, 'the task binary it just unpacked and checked against scripts/tools/task-pin.txt'),
    ('charts/mainframe-rag/files/openwebui/docs_pipe.py', 'import httpx'):
        (None, 'a chart file common.sh names; it runs inside the Open WebUI image, not here'),
    ('charts/mainframe-rag/files/openwebui/docs_pipe.py', 'import pydantic'):
        (None, 'a chart file common.sh names; it runs inside the Open WebUI image, not here'),
}
# Shell functions that run their arguments as a command: `run cmd args` is scanned as `cmd args`.
WRAPPERS = frozenset({'run'})

DIR0 = '\0dir0\0'
UNKNOWN = '\0?\0'
NAME = r'[A-Za-z_][A-Za-z0-9_]*'
ASSIGNMENT = re.compile(rf'({NAME})=(.*)', re.DOTALL)
PATH_WORD = re.compile(r'[A-Za-z0-9_.][A-Za-z0-9_./-]*')
VARIABLE = re.compile(rf'\$({NAME}|[0-9])(?=/|$)')
DIRNAME0 = re.compile(r'\s*dirname\s+(?:--\s+)?"?\$0"?\s*')
CD_DIR0 = re.compile(r'\s*(?:CDPATH=\S*\s+)?cd\s+(?:-P\s+)?(?:--\s+)?"?\$\(\s*dirname\s+(?:--\s+)?"?\$0"?\s*\)'
                     r'(/[A-Za-z0-9_./-]*)?"?\s*&&\s*pwd(?:\s+-P)?\s*')
PREFIX = frozenset({'!', '{', '}', '(', 'if', 'then', 'elif', 'else', 'fi', 'do', 'done', 'while', 'until',
                    'esac', 'time', 'exec', 'nohup'})
SHELLS = frozenset({'sh', 'bash', 'dash', 'ash', 'ksh'})
PYTHON = re.compile(r'python(?:3(?:\.[0-9]+)?)?')
DYNAMIC_CALLS = frozenset({'__import__', 'exec', 'eval', 'compile', 'import_module', 'run_path', 'run_module',
                           'spec_from_file_location', 'exec_module', 'load_module'})


class Unresolved(ValueError):
    """References the derivation cannot resolve; it never skips one silently."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = sorted(set(problems))
        super().__init__('unresolved references:\n' + '\n'.join(self.problems))


class _ParseError(ValueError):
    pass


@dataclass
class _Command:
    line: int
    words: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    heredocs: list[str] = field(default_factory=list)


class _Lexer:
    """A small POSIX-sh lexer: quotes, expansions, substitutions, heredocs; no execution."""

    def __init__(self, text: str) -> None:
        self.s, self.i, self.line = text, 0, 1
        self.commands: list[_Command] = []
        self.pending: list[tuple[str, bool, _Command]] = []
        # Open `case` statements; True while their next words are patterns, up to `)`.
        self.cases: list[bool] = []

    def parse(self, until: str | None = None) -> None:
        cmd, depth, target = _Command(self.line), 0, None
        s = self.s

        def finish() -> _Command:
            if cmd.words or cmd.targets or cmd.heredocs or any(c is cmd for _, _, c in self.pending):
                self.commands.append(cmd)
            return _Command(self.line)

        while self.i < len(s):
            c = s[self.i]
            if c in ' \t':
                self.i += 1
            elif c == '\\' and s[self.i + 1:self.i + 2] == '\n':
                self.i += 2
                self.line += 1
            elif c == '\n':
                self.i += 1
                self.line += 1
                cmd = finish()
                self._heredocs()
            elif c == '#':
                while self.i < len(s) and s[self.i] != '\n':
                    self.i += 1
            elif c in ';&|':
                if s.startswith(';;', self.i) and self.cases:
                    self.cases[-1] = True
                    self.i += 1
                self.i += 1
                cmd = finish()
            elif c == '(' and self.cases and self.cases[-1]:
                self.i += 1
            elif c == '(':
                self.i += 1
                depth += until == ')'
                cmd = finish()
            elif c == ')' and self.cases and self.cases[-1]:
                self.i += 1
                self.cases[-1] = False
            elif c == ')':
                self.i += 1
                if until == ')' and depth == 0:
                    finish()
                    return
                depth -= until == ')'
                cmd = finish()
            elif c in '<>':
                op = re.match(r'<<-|<<<|<<|<&|>&|>>|<>|>\||<|>', s[self.i:])
                assert op is not None
                if cmd.words and cmd.words[-1].isdigit() and self.i and s[self.i - 1].isdigit():
                    cmd.words.pop()  # an fd number: 2>file
                self.i += op.end()
                target = op.group()
            else:
                value = self._word()
                if target is None and self.cases and (self.cases[-1] or not cmd.words) and value == 'esac':
                    self.cases.pop()
                    continue
                if target is None and self.cases and self.cases[-1]:
                    continue  # a case pattern
                if target is None and value == 'in' and len(cmd.words) == 2 and cmd.words[0] == 'case':
                    self.cases.append(True)
                if target in ('<<', '<<-'):
                    self.pending.append((value, target == '<<-', cmd))
                elif target in ('<&', '>&'):
                    pass
                elif target is not None:
                    cmd.targets.append(value)
                else:
                    cmd.words.append(value)
                target = None
        if until is not None:
            raise _ParseError(f'line {self.line}: unterminated $(')
        if self.cases:
            raise _ParseError(f'line {self.line}: unterminated case')
        finish()
        if self.pending:
            raise _ParseError(f'line {self.line}: unterminated heredoc')

    def _heredocs(self) -> None:
        for delimiter, strip, cmd in self.pending:
            body = []
            while True:
                if self.i >= len(self.s):
                    raise _ParseError(f'line {self.line}: heredoc {delimiter!r} has no end')
                end = self.s.find('\n', self.i)
                end = len(self.s) if end < 0 else end
                text = self.s[self.i:end]
                self.i, self.line = end + 1, self.line + 1
                if (text.lstrip('\t') if strip else text) == delimiter:
                    break
                body.append(text)
            cmd.heredocs.append('\n'.join(body) + '\n')
        self.pending = []

    def _word(self) -> str:
        out, s = [], self.s
        while self.i < len(s) and s[self.i] not in ' \t\n;&|()<>':
            c = s[self.i]
            if c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise _ParseError(f'line {self.line}: unterminated quote')
                out.append(s[self.i + 1:end])
                self.line += s.count('\n', self.i, end)
                self.i = end + 1
            elif c == '"':
                self.i += 1
                while True:
                    if self.i >= len(s):
                        raise _ParseError(f'line {self.line}: unterminated quote')
                    c = s[self.i]
                    if c == '"':
                        self.i += 1
                        break
                    if c == '\\' and s[self.i + 1:self.i + 2] in ('$', '`', '"', '\\', '\n'):
                        out.append(s[self.i + 1] if s[self.i + 1] != '\n' else '')
                        self.i += 2
                    elif c == '$':
                        out.append(self._dollar())
                    elif c == '`':
                        out.append(self._backquote())
                    else:
                        self.line += c == '\n'
                        out.append(c)
                        self.i += 1
            elif c == '\\':
                out.append(s[self.i + 1:self.i + 2])
                self.i += 2
            elif c == '$':
                out.append(self._dollar())
            elif c == '`':
                out.append(self._backquote())
            else:
                out.append(c)
                self.i += 1
        return ''.join(out)

    def _dollar(self) -> str:
        s = self.s
        self.i += 1
        if s.startswith('((', self.i):
            depth, self.i = 0, self.i + 2
            while self.i < len(s) and not (depth == 0 and s.startswith('))', self.i)):
                depth += {'(': 1, ')': -1}.get(s[self.i], 0)
                self.i += 1
            self.i += 2
            return UNKNOWN
        if s.startswith('(', self.i):
            self.i += 1
            start = self.i
            self.parse(until=')')
            inner = s[start:self.i - 1]
            if DIRNAME0.fullmatch(inner):
                return DIR0
            match = CD_DIR0.fullmatch(inner)
            if match:
                return DIR0 + (match[1] or '')
            return UNKNOWN
        if s.startswith('{', self.i):
            depth, start = 0, self.i + 1
            while self.i < len(s):
                c = s[self.i]
                depth += {'{': 1, '}': -1}.get(c, 0)
                self.i += 1
                if depth == 0:
                    break
            inner = s[start:self.i - 1]
            return '$' + inner if re.fullmatch(NAME, inner) else UNKNOWN
        match = re.compile(NAME).match(s, self.i)
        if match:
            self.i = match.end()
            return '$' + match.group()
        if self.i < len(s) and (s[self.i].isdigit() or s[self.i] in '@*#?-$!'):
            self.i += 1
            return '$' + s[self.i - 1]
        return '$'

    def _backquote(self) -> str:
        end = self.s.find('`', self.i + 1)
        if end < 0:
            raise _ParseError(f'line {self.line}: unterminated backquote')
        inner = _Lexer(self.s[self.i + 1:end].replace('\\`', '`'))
        inner.parse()
        self.commands.extend(inner.commands)
        self.i = end + 1
        return UNKNOWN


def shell_commands(text: str) -> list[_Command]:
    lexer = _Lexer(text)
    lexer.parse()
    return lexer.commands


@dataclass
class _Context:
    """One shell process: its $0 directory and its variables (shared with sourced files)."""

    file: str
    dir0: str
    vars: dict[str, str]


class _Derivation:
    def __init__(self, root: Path, reviewed: Mapping[tuple[str, str], tuple[str | None, str]],
                 root_variables: Mapping[tuple[str, str], str]) -> None:
        listed = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True,
                                timeout=60).stdout.decode('utf-8', 'replace')
        self.root = root
        self.tracked = frozenset(filter(None, listed.split('\0')))
        self.dirs = frozenset(posixpath.dirname(p) for p in self.tracked) | {''}
        self.reviewed, self.root_variables = reviewed, root_variables
        self.used: set[tuple[str, str]] = set()
        self.closure: set[str] = set()
        self.problems: set[str] = set()
        self.executed: set[tuple[str, str]] = set()
        self.python_done: set[str] = set()
        self.stack: list[str] = []
        self.labels: list[str] = []
        self.by: dict[str, str] = {}

    def add(self, path: str) -> None:
        self.closure.add(path)
        self.by.setdefault(path, self.labels[-1] if self.labels else 'entry point')

    # -- references -------------------------------------------------------
    def resolve(self, value: str, ctx: _Context) -> str | None:
        """The repository-relative path a word names, or None when it is not a repository path."""
        if value.startswith(DIR0):
            value = ctx.dir0 + value[len(DIR0):] if ctx.dir0 else value[len(DIR0) + 1:] or '.'
        else:
            match = VARIABLE.match(value)
            if match:
                name, rest = match[1], value[match.end():]
                if (ctx.file, name) in self.root_variables:
                    self.used.add((ctx.file, name))
                    value = rest.lstrip('/') or '.'
                elif name in ctx.vars:
                    value = posixpath.join(ctx.vars[name], rest.lstrip('/')) if ctx.vars[name] else rest.lstrip('/')
                else:
                    return None
        if not value or '\0' in value or '$' in value or value.startswith('/') or not PATH_WORD.fullmatch(value):
            return None
        norm = posixpath.normpath(value)
        if norm == '..' or norm.startswith('../'):
            return None
        return '' if norm == '.' else norm

    def data(self, value: str, ctx: _Context) -> None:
        for candidate in (value, value.partition('=')[2]):
            path = self.resolve(candidate, ctx)
            if path in self.tracked:
                self.reach(path)

    def reach(self, path: str) -> None:
        """A tracked file the closure reads; code is followed as code."""
        self.add(path)
        if path.endswith('.sh') and path not in self.stack:
            self.run_shell(path, posixpath.dirname(path))
        elif path.endswith('.py'):
            self.python_file(path)

    def execution(self, word: str, ctx: _Context, kind: str) -> str | None:
        """A file that is sourced or run: tracked, or reviewed, or a problem."""
        path = self.resolve(word, ctx)
        if path is not None and path in self.tracked:
            self.add(path)
            return path
        key = (ctx.file, word.replace(DIR0, '$(dirname "$0")').replace(UNKNOWN, '$(...)'))
        if key in self.reviewed:
            self.used.add(key)
            mapped = self.reviewed[key][0]
            if mapped is not None and mapped not in self.tracked:
                self.problems.add(f'{ctx.file}: reviewed {word!r} maps to untracked {mapped}')
                return None
            if mapped is not None:
                self.add(mapped)
            return mapped
        self.problems.add(f'{ctx.file}: cannot resolve {kind} {key[1]!r}')
        return None

    def read(self, path: str) -> str | None:
        try:
            return (self.root / path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            self.problems.add(f'{path}: cannot read')
            return None

    # -- shell ------------------------------------------------------------
    def run_shell(self, path: str, dir0: str) -> None:
        if (path, dir0) in self.executed:
            return
        self.executed.add((path, dir0))
        self.add(path)
        text = self.read(path)
        if text is not None:
            self.shell(text, _Context(path, dir0, {}), path)

    def shell(self, text: str, ctx: _Context, label: str) -> None:
        self.stack.append(ctx.file)
        self.labels.append(label)
        try:
            commands = shell_commands(text)
        except _ParseError as error:
            self.problems.add(f'{label}: cannot parse shell ({error})')
            commands = []
        for cmd in commands:
            self.command(cmd, ctx)
        self.stack.pop()
        self.labels.pop()

    def command(self, cmd: _Command, ctx: _Context) -> None:
        words = list(cmd.words)
        for target in cmd.targets:
            self.data(target, ctx)
        while words and (words[0] in PREFIX or ASSIGNMENT.fullmatch(words[0])):
            word = words.pop(0)
            match = ASSIGNMENT.fullmatch(word)
            if match:
                self.assign(match[1], match[2], ctx, bare=not words)
        if not words:
            return
        name, args = words[0], words[1:]
        if name in ('export', 'local', 'readonly'):
            for arg in args:
                match = ASSIGNMENT.fullmatch(arg)
                if match:
                    self.assign(match[1], match[2], ctx, bare=True)
            return
        while name in WRAPPERS and args or name in ('command', 'env') and args and not args[0].startswith('-'):
            name, args = args[0], args[1:]
        if name == 'git' and '--' in args:
            args = args[:args.index('--')]  # a pathspec names objects of a commit, not checkout files
        elif name == 'tar':
            # Member names are inside the archive; only the archive (-f) and the -C/-T files count.
            args = [w for p, w in zip(['', *args[:-1]], args, strict=True)
                    if p in ('-C', '-T') or (p.endswith('f') and not p.startswith('--'))]
        for arg in args:
            self.data(arg, ctx)
        base = posixpath.basename(name)
        if name in ('.', 'source'):
            if args:
                self.source(args[0], ctx)
        elif base in SHELLS:
            self.shell_invocation(args, cmd, ctx)
        elif PYTHON.fullmatch(base):
            self.python_invocation(args, cmd, ctx)
        elif name == 'eval':
            self.shell(' '.join(args), ctx, f'{ctx.file}:{cmd.line} eval')
        elif re.match(r'[A-Za-z0-9_$\0?{}]+=', name):
            return  # an indirect assignment built by eval: ${name}=value
        elif '/' in name or '$' in name or '\0' in name:
            path = self.execution(name, ctx, 'command')
            if path is not None:
                self.reach(path)

    def assign(self, name: str, value: str, ctx: _Context, bare: bool) -> None:
        path = self.resolve(value, ctx)
        if path is not None and (path in self.tracked or path in self.dirs):
            if bare:
                ctx.vars[name] = path
            if path in self.tracked:
                self.reach(path)
        elif bare:
            ctx.vars.pop(name, None)

    def source(self, word: str, ctx: _Context) -> None:
        path = self.execution(word, ctx, 'sourced file')
        if path is None or path in self.stack:
            return
        text = self.read(path)
        if text is not None:
            # Same process: $0 and the variables are the sourcing script's.
            self.shell(text, _Context(path, ctx.dir0, ctx.vars), path)

    def shell_invocation(self, args: list[str], cmd: _Command, ctx: _Context) -> None:
        rest = list(args)
        while rest and rest[0][:1] in '-+' and len(rest[0]) > 1:
            option = rest.pop(0)
            if option == '-o' and rest:
                rest.pop(0)
            elif 'c' in option[1:] and option[:1] == '-':
                if not rest:
                    self.problems.add(f'{ctx.file}:{cmd.line}: sh -c without a command string')
                    return
                script, positional = rest[0], rest[1:]
                inner = _Context(ctx.file, ctx.dir0, dict(ctx.vars))
                for index, value in enumerate(positional):
                    path = self.resolve(value, ctx)
                    if path is not None:
                        inner.vars[str(index)] = path
                    else:
                        inner.vars.pop(str(index), None)
                self.shell(script, inner, f'{ctx.file}:{cmd.line} sh -c')
                return
        if rest and rest[0] != '-':
            path = self.execution(rest[0], ctx, 'shell script')
            if path is not None and path.endswith('.sh'):
                self.run_shell(path, posixpath.dirname(path))
            elif path is not None:
                self.reach(path)
        elif cmd.heredocs:
            for body in cmd.heredocs:
                self.shell(body, _Context(ctx.file, ctx.dir0, {}), f'{ctx.file}:{cmd.line} heredoc')
        else:
            self.problems.add(f'{ctx.file}:{cmd.line}: shell reads a script from stdin')

    def python_invocation(self, args: list[str], cmd: _Command, ctx: _Context) -> None:
        rest = list(args)
        while rest:
            option = rest.pop(0)
            if option == '-':
                break
            if option in ('-c', '-m'):
                if not rest:
                    break
                value = rest.pop(0)
                if option == '-c':
                    self.python_source(value, f'{ctx.file}:{cmd.line} python -c', posixpath.dirname(ctx.file))
                else:
                    self.module(value, (), ctx.file, '')
                return
            if option in ('-W', '-X') and rest:
                rest.pop(0)
                continue
            if option.startswith('-') and len(option) > 1:
                continue
            path = self.execution(option, ctx, 'Python script')
            if path is not None:
                self.reach(path)
            return
        if cmd.heredocs:
            for body in cmd.heredocs:
                self.python_source(body, f'{ctx.file}:{cmd.line} python heredoc', posixpath.dirname(ctx.file))
        elif args:
            self.problems.add(f'{ctx.file}:{cmd.line}: python reads a program from stdin')

    # -- Python -----------------------------------------------------------
    def python_file(self, path: str) -> None:
        if path in self.python_done:
            return
        self.python_done.add(path)
        self.add(path)
        text = self.read(path)
        if text is not None:
            self.python_source(text, path, posixpath.dirname(path))

    def python_source(self, text: str, label: str, here: str) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            self.problems.add(f'{label}: cannot parse Python')
            return
        self.labels.append(label)
        self.python_tree(tree, label, here)
        self.labels.pop()

    def python_tree(self, tree: ast.AST, label: str, here: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.module(alias.name, (), label, here)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = here
                    for _ in range(node.level - 1):
                        base = posixpath.dirname(base)
                    self.package_from(base, node.module, [a.name for a in node.names], label)
                elif node.module:
                    self.module(node.module, [a.name for a in node.names], label, here)
            elif isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''
                if called in DYNAMIC_CALLS and not (called == 'compile' and isinstance(func, ast.Attribute)):
                    self.problems.add(f'{label}:{node.lineno}: dynamic code ({called})')
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.literal(node.value, here)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                parts: list[str] = []
                left: ast.expr = node
                while isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div) and \
                        isinstance(left.right, ast.Constant) and isinstance(left.right.value, str):
                    parts.insert(0, left.right.value)
                    left = left.left
                if isinstance(left, ast.Constant) and isinstance(left.value, str):
                    parts.insert(0, left.value)
                if len(parts) > 1:
                    self.literal('/'.join(parts), here)

    def literal(self, value: str, here: str) -> None:
        if not PATH_WORD.fullmatch(value):
            return
        for candidate in {posixpath.normpath(value), posixpath.normpath(posixpath.join(here, value))}:
            if candidate in self.tracked:
                self.reach(candidate)

    def python_roots(self, here: str) -> list[str]:
        """sys.path entries a reached program can have: the root and every reached script's directory."""
        return sorted({'', here, *(posixpath.dirname(p) for p in self.closure if p.endswith('.py'))})

    def module(self, name: str, names: Iterable[str], label: str, here: str) -> None:
        found = False
        for base in self.python_roots(here):
            found |= self.package_from(base, name, names, None)
        top = name.split('.')[0]
        if found or top in sys.stdlib_module_names or top in sys.builtin_module_names:
            return
        key = (label, f'import {top}')
        if key in self.reviewed and self.reviewed[key][0] is None:
            self.used.add(key)
        else:
            self.problems.add(f'{label}: cannot resolve import {name!r}')

    def package_from(self, base: str, name: str | None, names: Iterable[str], label: str | None) -> bool:
        """Reach the files `from base import name.names` loads under base; True when any exists."""
        found: list[str] = []
        package: str | None = base
        for part in (name.split('.') if name else []):
            here = posixpath.join(package, part) if package else part
            if f'{here}/__init__.py' in self.tracked:
                found.append(f'{here}/__init__.py')
                package = here
            elif here in self.dirs and f'{here}.py' not in self.tracked:
                package = here  # a namespace package
            else:
                found += [f'{here}.py'] if f'{here}.py' in self.tracked else []
                package = None
                break
        if package is not None:
            for item in names:
                prefix = posixpath.join(package, item) if package else item
                found += [p for p in (f'{prefix}.py', f'{prefix}/__init__.py') if p in self.tracked]
        if not found and label is not None:
            self.problems.add(f'{label}: cannot resolve relative import {name!r}')
        for path in found:
            self.reach(path)
        return bool(found)


def derive(root: Path, entry_points: Iterable[str],
           reviewed: Mapping[tuple[str, str], tuple[str | None, str]] = REVIEWED,
           root_variables: Mapping[tuple[str, str], str] = ROOT_VARIABLES) -> frozenset[str]:
    """Every tracked file the entry points execute or read by name; raises Unresolved otherwise."""
    return frozenset(referrers(root, entry_points, reviewed, root_variables))


def referrers(root: Path, entry_points: Iterable[str],
              reviewed: Mapping[tuple[str, str], tuple[str | None, str]] = REVIEWED,
              root_variables: Mapping[tuple[str, str], str] = ROOT_VARIABLES) -> dict[str, str]:
    """derive(), with the first file (or heredoc) that reached each path."""
    run = _Derivation(root, reviewed, root_variables)
    for entry in entry_points:
        if entry not in run.tracked:
            run.problems.add(f'{entry}: entry point is not a tracked file')
        elif entry.endswith('.py'):
            run.python_file(entry)
        else:
            run.run_shell(entry, posixpath.dirname(entry))
    # The Python search path grows as scripts are reached; repeat until it is stable.
    while True:
        before = len(run.closure)
        for path in sorted(run.closure):
            if path.endswith('.py'):
                run.python_done.discard(path)
                run.python_file(path)
        if len(run.closure) == before:
            break
    stale = [f'reviewed reference {key} is no longer used' for key in reviewed if key not in run.used]
    stale += [f'root variable {key} is no longer used' for key in root_variables if key not in run.used]
    if run.problems or stale:
        raise Unresolved([*run.problems, *stale])
    return {path: run.by[path] for path in sorted(run.closure)}


if __name__ == '__main__':
    from scripts.gap_codeowners import SIGNING_ENTRY_POINTS

    for path, referrer in referrers(REPO, SIGNING_ENTRY_POINTS).items():
        print(f'{path}  <- {referrer}')
