"""The external commands the gap's shell scripts call, and where the kit image gets each (#1183).

GitLab CI and the bastion run the gap scripts in the dev kit image (ADR-0009 §8), whose
command set is fixed by its recipe. ``scan()`` derives the commands from the scripts
themselves; ``KIT_COMMANDS`` is the maintained answer to "who provides it in the image".
The unit test (tests/test_devkit_kit.py) checks the two against each other and against
the recorded image contents, with no image; ``tests/devkit_image_contracts.py`` checks
the same map against a built image.

The scan is a small POSIX-sh tokenizer, not a full parser: it reads every word in
command position (line start, after ``; & && || | ( ! { then do else elif if while
until``, after ``$(`` and backquotes, and after ``exec``, ``command`` and ``xargs``),
skips heredoc bodies, case patterns, assignments, redirections and the scripts' own
functions, and also takes each word a ``for`` loop hands to ``command -v``. Anything it
reads that is not a plain command name (``$var``, paths) is ignored, so a script that
runs a tool through a variable must also probe it by name (``command -v tool``).
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The scripts that run inside the kit image: the bastion's operator scripts, the gap's
# import and kit scripts, and the image's own helpers.
SCRIPT_GLOBS = ("scripts/airgap/*.sh", "scripts/devkit/*.sh", "scripts/gap/*.sh", "images/devkit/bin/*")

BUILTINS = frozenset([
    ".", ":", "[", "alias", "bg", "break", "cd", "command", "continue", "echo", "eval", "exec", "exit",
    "export", "false", "fg", "getopts", "hash", "jobs", "kill", "local", "printf", "pwd", "read",
    "readonly", "return", "set", "shift", "test", "times", "trap", "true", "type", "ulimit", "umask",
    "unalias", "unset", "wait",
])
KEYWORDS = frozenset(["if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "in", "case",
                      "esac", "function", "select", "time"])
# After these the next word is a command again.
RESTART = frozenset({";", "&", "&&", "||", "|", "(", ")", "!", "{", "}", "\n", ";;", "then", "do", "else",
                     "elif", "if", "while", "until"})
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*\Z")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
FUNCTION = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)", re.MULTILINE)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Provider kinds:
#   rpm:<name>      an RPM in images/devkit-packages.txt or the pinned base's rpmdb
#                   (licenses/ubi9-python-314-minimal.rpms.txt); the contract checks
#                   that this RPM owns the command's path in the image
#   tool:<pin>      a binary the recipe installs from a pin (scripts/tools/<pin>-pin.txt
#                   or images/devkit/agents/<pin>-pin.txt) into /usr/local/bin
#   kit-bin         a helper the image copies from images/devkit/bin/
#   app-root        the base image's Python environment, /opt/app-root/bin
KIT_COMMANDS: dict[str, str] = {
    "awk": "rpm:gawk",
    "base64": "rpm:coreutils-single",
    "basename": "rpm:coreutils-single",
    "cat": "rpm:coreutils-single",
    "chmod": "rpm:coreutils-single",
    "cmp": "rpm:diffutils",
    "cp": "rpm:coreutils-single",
    "curl": "rpm:curl-minimal",
    "cut": "rpm:coreutils-single",
    "date": "rpm:coreutils-single",
    "df": "rpm:coreutils-single",
    "diff": "rpm:diffutils",
    "dirname": "rpm:coreutils-single",
    "du": "rpm:coreutils-single",
    "env": "rpm:coreutils-single",
    "find": "rpm:findutils",
    "git": "rpm:git-core",
    "grep": "rpm:grep",
    "head": "rpm:coreutils-single",
    "helm": "tool:helm",
    "kubectl": "tool:oc",
    "ln": "rpm:coreutils-single",
    "ls": "rpm:coreutils-single",
    "mkdir": "rpm:coreutils-single",
    "mktemp": "rpm:coreutils-single",
    "mv": "rpm:coreutils-single",
    "oc": "tool:oc",
    "openssl": "rpm:openssl",
    "python3": "app-root",
    "readlink": "rpm:coreutils-single",
    "rm": "rpm:coreutils-single",
    "sed": "rpm:sed",
    "sh": "rpm:bash",
    "sha256sum": "rpm:coreutils-single",
    "skopeo": "rpm:skopeo",
    "sleep": "rpm:coreutils-single",
    "sort": "rpm:coreutils-single",
    "tail": "rpm:coreutils-single",
    "tar": "rpm:tar",
    "tr": "rpm:coreutils-single",
    "uname": "rpm:coreutils-single",
    "uniq": "rpm:coreutils-single",
    "wc": "rpm:coreutils-single",
}


SOURCE = re.compile(r"""^\s*(?:\.|source)\s+"?(?:\$\([^)]*\)|\$\{?\w+\}?)?(/?[^"\s;$]+\.sh)"?\s*$""", re.MULTILINE)


def script_paths(root: Path = REPO) -> list[Path]:
    """The kit scripts, plus every repository script they source (`. "$ROOT/scripts/…sh"`;
    sourced env files are data)."""
    root = root.resolve()
    todo = sorted({p for pattern in SCRIPT_GLOBS for p in root.glob(pattern) if p.is_file() and _is_shell(p)})
    seen: set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for match in SOURCE.finditer(_strip_comments(path.read_text(encoding="utf-8"))):
            tail = match.group(1).lstrip("/")
            for candidate in (root / tail, path.parent / tail, path.parent / Path(tail).name):
                if candidate.resolve().is_relative_to(root) and candidate.is_file():
                    todo.append(candidate.resolve())
                    break
            else:
                raise AssertionError(f"{path.relative_to(root)} sources {match.group(1)}, "
                                     "which is not in the repository")
    return sorted(seen)


def _is_shell(path: Path) -> bool:
    if path.suffix == ".sh":
        return True
    first = path.read_bytes()[:64].split(b"\n", 1)[0]
    return first.startswith(b"#!") and re.search(rb"\b(sh|bash|dash)\b", first) is not None


def _strip_heredocs(text: str) -> str:
    """Drop heredoc bodies (their lines are data, not commands)."""
    out: list[str] = []
    ends: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        if ends:
            word, dash = ends[0]
            if (line.lstrip("\t") if dash else line) == word:
                ends.pop(0)
            continue
        out.append(line)
        if "<<" in line and not line.lstrip().startswith("#"):
            for match in HEREDOC.finditer(line):
                ends.append((match.group(2), match.group(0).startswith("<<-")))
    return "\n".join(out)


def _substitutions(text: str) -> list[str]:
    """The bodies of every ``$(...)`` and backquoted command substitution, nested ones
    included; ``$((...))`` arithmetic is skipped."""
    bodies: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$((", i):
            i += 3
            continue
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            body = text[i + 2:j - 1]
            bodies.append(body)
            bodies.extend(_substitutions(body))
            i = j
            continue
        if text[i] == "`" and (i == 0 or text[i - 1] != "\\"):
            j = text.find("`", i + 1)
            if j > i:
                bodies.append(text[i + 1:j])
                i = j + 1
                continue
        i += 1
    return bodies


def _strip_comments(text: str) -> str:
    """Remove ``#`` comments: a ``#`` that starts a word outside quotes, to the line end."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                out.append(text[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        out.append(c)
        i += 1
    return "".join(out)


OPERATOR = re.compile(r";;|&&|\|\||>>|<<-?|[<>]&|[;&|()<>\n]")


def _blank_expansions(text: str) -> str:
    """Replace ``$((...))`` and ``${...}`` with a plain word: their insides are never
    commands here (a substitution inside ``${x:-$(cmd)}`` is read by ``_substitutions``)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        for opener, pair in (("$((", "()"), ("${", "{}")):
            if text.startswith(opener, i):
                depth, j = opener.count(pair[0]), i + len(opener)
                while j < len(text) and depth:
                    depth += {pair[0]: 1, pair[1]: -1}.get(text[j], 0)
                    j += 1
                out.append("0")
                i = j
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(_blank_expansions(text).replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw = list(lexer)
    except ValueError:  # an unbalanced quote inside a substitution body: read what parses
        return []
    # shlex joins adjacent punctuation (";;\n"); split it back into shell operators.
    tokens: list[str] = []
    for tok in raw:
        tokens.extend(OPERATOR.findall(tok) if tok and all(c in ";&|()<>\n" for c in tok) else [tok])
    return tokens


def _commands(text: str) -> set[str]:
    found: set[str] = set()
    tokens = _tokens(text)
    expect = True
    case_patterns = 0  # >0 while reading case patterns up to their ')'
    case_depth = 0
    probe_loop: list[str] | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if case_patterns:
            if tok == ")":
                case_patterns = 0
                expect = True
            elif tok == "esac":  # the last branch's ';;' leads straight to esac
                case_patterns = 0
                case_depth -= 1
                expect = False
            i += 1
            continue
        if tok == "case":
            case_depth += 1
            while i < len(tokens) and tokens[i] != "in":
                i += 1
            case_patterns = 1
            i += 1
            continue
        if tok == "esac":
            case_depth -= 1
            expect = False
            i += 1
            continue
        if tok == ";;" and case_depth:
            case_patterns = 1
            i += 1
            continue
        if tok == "for":
            words: list[str] = []
            j = i + 2
            if j < len(tokens) and tokens[j] == "in":
                j += 1
                while j < len(tokens) and tokens[j] not in (";", "\n"):
                    words.append(tokens[j])
                    j += 1
            probe_loop = words
            i = j
            expect = True
            continue
        if tok in RESTART:
            expect = True
            i += 1
            continue
        if any(c in tok for c in "<>"):
            i += 2  # the redirection's target is not a command
            continue
        if not expect:
            i += 1
            continue
        if ASSIGNMENT.match(tok):
            i += 1
            continue
        expect = False
        if tok in ("exec", "xargs", "nohup", "env"):
            j = i + 1
            while j < len(tokens) and (tokens[j].startswith("-") or ASSIGNMENT.match(tokens[j])):
                j += 2 if tok == "env" and tokens[j] == "-u" else 1
            if tok != "exec":
                found.add(tok)
            i = j
            expect = True
            continue
        if tok == "command":
            if nxt in ("-v", "-V"):
                target = tokens[i + 2] if i + 2 < len(tokens) else ""
                if NAME.match(target):
                    found.add(target)
                elif target.startswith("$") and probe_loop:
                    found.update(w for w in probe_loop if NAME.match(w))
                i += 3
                continue
            i += 1
            expect = True
            continue
        if NAME.match(tok) and tok not in KEYWORDS and nxt != ")":
            found.add(tok)
        i += 1
    return found


def scan(path: Path) -> set[str]:
    """Every external-command candidate in one script (builtins and keywords removed)."""
    text = _strip_comments(_strip_heredocs(path.read_text(encoding="utf-8")))
    found = _commands(text)
    for body in _substitutions(text):
        found |= _commands(body)
    return {c for c in found if c not in BUILTINS and c not in KEYWORDS}


def defined_functions(paths: list[Path]) -> set[str]:
    return {m.group(1) for p in paths for m in FUNCTION.finditer(p.read_text(encoding="utf-8"))}


def interpreter(path: Path) -> set[str]:
    """The commands a ``#!`` line runs: ``#!/usr/bin/env python3`` is env and python3."""
    first = path.read_bytes()[:128].split(b"\n", 1)[0].decode("utf-8", "replace")
    if not first.startswith("#!"):
        return set()
    words = first[2:].split()
    names = {Path(words[0]).name} if words else set()
    if names == {"env"} and len(words) > 1:
        names.add(words[1])
    return names


def gap_commands(root: Path = REPO) -> dict[str, set[str]]:
    """{command: {scripts calling it}} over every kit script, minus their own functions,
    plus the interpreter of every helper the image ships (images/devkit/bin/)."""
    root = root.resolve()
    paths = script_paths(root)
    functions = defined_functions(paths)
    calls: dict[str, set[str]] = {}
    for path in paths:
        for command in scan(path) - functions:
            calls.setdefault(command, set()).add(path.relative_to(root).as_posix())
    for pattern in SCRIPT_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            for command in interpreter(path):
                calls.setdefault(command, set()).add(path.relative_to(root).as_posix())
    return calls
