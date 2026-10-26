#!/usr/bin/env python3
"""Hermetic local verification runner with an honest receipt (#796).

Local verification is a fast check of the changed behavior against exactly
this worktree's code. CI's unit lane remains the only authority for the full
suite; a receipt never substitutes for it. Each guarantee fails closed:

- G1 right code: the prepared interpreter imports `mainframe_rag` from this
  worktree's `src`.
- G2 owned environment: the child environment is built from an allowlist
  (never inherited), temp files live on disk outside the repository, and
  memory is capped.
- G3 no interference: no other holder of the Colab assignment lock when the
  selection exercises scripts/colab.py, and no other pytest running from this tree.
- G4/G5 selection: without explicit tests, scripts/local_selection.py derives
  T1 (or T2 for broad changes) from the diff against origin/main plus dirty
  paths, printed before running. An unmapped src/scripts change refuses unless
  `--select` covers it; the override is recorded. T0 (ruff, mypy on changed
  files) runs first and fails fast.
- G6 receipt: tier, SHAs, selection, counts, exit code and the fixed list of
  what was not verified. A dirty tree runs but is marked "not evidence".

Positional test paths keep explicit mode: exactly those tests, no diff analysis.
Exit codes: 0 passed, 1 failed or incomplete, 2 refused before running.
"""
from __future__ import annotations

import argparse
import fcntl
import itertools
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import local_selection
from scripts.ci_evidence import junit_bytes

ENV_ALLOWLIST = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LANGUAGE', 'TERM', 'TZ',
                 # systemd-run --user reaches the user manager through these.
                 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')
ENV_NAME = re.compile(r'[A-Z_][A-Z0-9_]*')
RAM_FILESYSTEMS = {'tmpfs', 'ramfs'}
DEFAULT_MEMORY_MAX_MIB = 3072
HARNESS_SELECTION_REFUSAL = ('tests/test_local_verify.py exercises this runner; '
                             'run it with pytest or CI, not qa:local')
NOT_VERIFIED = (
    'full unit suite (CI unit_tests lane is the authority)',
    'tests outside the selection',
    'integration, simulation, load, HA and hazard lanes',
    'live stack and real data',
    'completeness of the selection',
)


class Refusal(Exception):
    """A guarantee cannot be established; nothing was run."""


def child_env(inherited: dict[str, str], root: Path, tmpdir: Path, passed: list[str]) -> dict[str, str]:
    env = {k: v for k, v in inherited.items() if k in ENV_ALLOWLIST or k.startswith('LC_')}
    for name in passed:
        if name not in inherited:
            raise Refusal(f'--pass-env {name} is not set')
        env[name] = inherited[name]
    env['PATH'] = os.pathsep.join([str(root / '.tools/bin'), env.get('PATH', os.defpath)])
    env['TMPDIR'] = str(tmpdir)
    # Match the CI unit lane: Task contract tests fail rather than skip without the runner.
    env['TASK_CONTRACTS_REQUIRE_RUNNER'] = '1'
    # Match the CI unit lane's plugin set: no ambient plugin autoloading.
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    return env


def check_interpreter(root: Path, python: Path, env: dict[str, str]) -> None:
    """G1: subprocesses spawned by tests import exactly this worktree's source."""
    if not os.access(python, os.X_OK):
        raise Refusal(f'{python} is missing; run: sh scripts/tools/run-task.sh dev:setup')
    probe = subprocess.run([str(python), '-c', 'import mainframe_rag; print(mainframe_rag.__file__)'],
                           cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False)
    expected = (root / 'src/mainframe_rag/__init__.py').resolve()
    found = probe.stdout.strip()
    if probe.returncode != 0 or not found or Path(found).resolve() != expected:
        raise Refusal('the prepared interpreter does not import this worktree\'s src '
                      f'(found {found or "nothing"}); run dev:setup in this worktree')
    if not (root / '.tools/bin/task').is_file():
        raise Refusal('pinned Task runner missing at .tools/bin/task; link or install .tools')


def filesystem_type(path: Path, mounts: str) -> str | None:
    """Longest mount-point prefix of `path` in /proc/self/mounts text."""
    best, kind = '', None
    target = str(path.resolve())
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = fields[1].replace('\\040', ' ')
        if (target == point or target.startswith(point.rstrip('/') + '/')) and len(point) > len(best):
            best, kind = point, fields[2]
    return kind


def default_out_dir(environ: dict[str, str]) -> Path:
    state = environ.get('XDG_STATE_HOME') or str(Path(environ.get('HOME', '~')).expanduser() / '.local/state')
    return Path(state) / 'mainframe-rag' / 'local-verify'


def check_outside_tree(path: Path, root: Path) -> None:
    """Tests assume temp files live outside any checkout (pack, gateway-env and checkout guards)."""
    target, top = path.resolve(), root.resolve()
    if target == top or top in target.parents:
        raise Refusal(f'{path} is inside the repository; choose an --out-dir outside it')


def check_disk_backed(path: Path, mounts: str) -> None:
    kind = filesystem_type(path, mounts)
    if kind in RAM_FILESYSTEMS:
        raise Refusal(f'{path} is on {kind} (RAM); choose an --out-dir on disk')


def colab_assignment_lock() -> Path:
    # The path scripts/colab.py's assignment_lock() serializes on.
    return Path('/tmp') / f'mainframe-rag-colab-{os.geteuid()}' / 'assignment.lock'


def assignment_lock_holder(lock: Path) -> str | None:
    """G3: probe the Colab assignment lock without taking it for longer than the probe."""
    try:
        descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            recorded = handle.read(32).strip()
            return f'PID {recorded.decode()}' if recorded.isdigit() else 'an unrecorded PID'
        fcntl.flock(handle, fcntl.LOCK_UN)
    return None


def colab_lock_sensitive(tests: list[str]) -> list[str]:
    """Selected test modules that exercise scripts/colab.py, and so its host lock (G3)."""
    # Importers take the lock in-process; test_colab* modules also drive it via subprocesses.
    importers = local_selection.importing_tests(local_selection.build_graph(ROOT), 'scripts.colab')
    selected = {t.split('::', 1)[0] for t in tests}
    return sorted(t for t in selected if t in importers or Path(t).name.startswith('test_colab'))


def _ancestors() -> set[int]:
    pids, pid = set(), os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
        except OSError:
            break
        pid = int(stat.rsplit(')', 1)[1].split()[1])
    return pids


def stray_pytest(root: Path, proc: Path = Path('/proc')) -> list[int]:
    """G3: other pytest processes whose working directory is inside this tree."""
    mine, found, top = _ancestors(), [], root.resolve()
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) in mine:
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            cwd = Path(os.readlink(entry / 'cwd'))
        except OSError:
            continue
        runs_pytest = any(Path(a.decode(errors='replace')).name in ('pytest', 'py.test') for a in argv) \
            or any(a == b'-m' and b == b'pytest' for a, b in itertools.pairwise(argv))
        if runs_pytest and (cwd == top or top in cwd.parents):
            found.append(int(entry.name))
    return sorted(found)


def validate_selectors(root: Path, selectors: list[str]) -> None:
    if not selectors:
        raise Refusal('no selection: name test files or node ids after --')
    tests = (root / 'tests').resolve()
    for selector in selectors:
        if selector.startswith('-'):
            raise Refusal(f'{selector!r} is an option, not a selection; pytest options are fixed')
        target = (root / selector.split('::', 1)[0]).resolve()
        if not target.exists() or not (target == tests or tests in target.parents):
            raise Refusal(f'{selector!r} is not a test path under tests/')
        if f'tests/{target.relative_to(tests).as_posix()}' in local_selection.HARNESS_TESTS:
            raise Refusal(HARNESS_SELECTION_REFUSAL)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ''


def git_status(root: Path) -> str:
    """Raw porcelain v1 status: never strip the XY columns or quote paths."""
    result = subprocess.run(['git', 'status', '--porcelain', '-z', '--untracked-files=normal'],
                            cwd=root, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ''


def dirty_paths(status: str) -> list[str]:
    """Both sides of a rename plus every modified, deleted or untracked path."""
    found: set[str] = set()
    fields = status.split('\0')
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        found.add(entry[3:])
        if entry[0] in 'RC' or entry[1] in 'RC':
            if index < len(fields) and fields[index]:
                found.add(fields[index])
            index += 1
    return sorted(found)


def tree_state(root: Path) -> dict:
    dirty = dirty_paths(git_status(root))
    return {'head': git(root, 'rev-parse', 'HEAD') or None,
            'base': {'ref': 'origin/main', 'sha': git(root, 'merge-base', 'HEAD', 'origin/main') or None},
            'dirty': dirty}


def changed_paths(root: Path, state: dict) -> list[str]:
    """Committed changes since the merge-base plus every dirty path (G4 input)."""
    if not state['base']['sha']:
        raise Refusal('no merge-base with origin/main; fetch origin or name tests explicitly')
    committed = git(root, 'diff', '--name-only', state['base']['sha'], 'HEAD').splitlines()
    return sorted({p for p in committed if p} | set(state['dirty']))


# The CI lint_and_types scope (.github/workflows/ci.yml): T0 never flags what CI accepts.
CI_LINT_SCRIPTS = ('scripts/eval_retrieval.py', 'scripts/review_tooling.py', 'scripts/ci_evidence.py',
                   'scripts/acceptance_evidence.py')


def t0_targets(root: Path, changed: list[str]) -> dict[str, list[str]]:
    """Changed files within CI's lint scope; mypy covers all of src, as CI does."""
    files = [p for p in changed if p.endswith('.py') and (root / p).is_file()
             and (p.startswith(('src/', 'tests/')) or p in CI_LINT_SCRIPTS)]
    return {'ruff': files, 'mypy_src': ['src'] if any(p.startswith('src/') for p in files) else [],
            'mypy_scripts': [p for p in files if p in CI_LINT_SCRIPTS]}


def run_t0(root: Path, python: Path, env: dict[str, str], changed: list[str]) -> dict:
    """Seconds-tier static checks; a tool with no targets is None (not applicable)."""
    targets = t0_targets(root, changed)
    commands = {'ruff': ['ruff', 'check'], 'mypy_src': ['mypy'], 'mypy_scripts': ['mypy', '--follow-imports=silent']}
    record: dict = {'files': targets['ruff']}
    for key, args in commands.items():
        record[key] = (subprocess.run([str(python), '-m', *args, *targets[key]], cwd=root, env=env,
                                      check=False).returncode if targets[key] else None)
    return record


def t0_failures(t0: dict | None) -> list[str]:
    return [k for k in ('ruff', 'mypy_src', 'mypy_scripts') if t0 and t0.get(k)]


def result_of(code: int, counts: dict | None, t0: dict | None = None, ran: bool = True) -> str:
    failed_t0 = t0_failures(t0)
    if failed_t0:
        return 'failed: T0 ' + ', '.join(failed_t0)
    if not ran:
        return 'nothing selected: no test maps to the change'
    if counts is None:
        return 'failed: no readable test report'
    if code != 0 or counts['failed'] or counts['errors']:
        return 'failed'
    if counts['skipped']:
        return f'incomplete: {counts["skipped"]} skipped (a skip is not a pass)'
    return 'passed'


def build_receipt(*, state: dict, tier: str, selection: dict, command: list[str], env: dict[str, str],
                  dropped: list[str], passed: list[str], memory_max: int | None, code: int,
                  counts: dict | None, started: float, finished: float, t0: dict | None = None,
                  ran: bool = True, colab_lock_checked: bool = True) -> dict:
    result = result_of(code, counts, t0, ran)
    evidence = result == 'passed' and not state['dirty'] and state['head'] is not None
    return {
        'schema_version': 2,
        'tier': tier,
        'selection': selection,
        't0': t0,
        'tree': state,
        'evidence': evidence,
        'label': ('local verification of the selection only' if evidence else
                  'uncommitted, not evidence' if state['dirty'] else 'not evidence'),
        'environment': {'allowlisted': sorted(k for k in env if k in ENV_ALLOWLIST or k.startswith('LC_')),
                        'passed_explicitly': passed, 'dropped': dropped,
                        'set': {k: env[k] for k in ('TMPDIR', 'TASK_CONTRACTS_REQUIRE_RUNNER',
                                                     'PYTEST_DISABLE_PLUGIN_AUTOLOAD')}},
        'memory_max_mib': memory_max,
        'colab_lock_checked': colab_lock_checked,
        'command': command,
        'exit_code': code,
        'counts': counts,
        'result': result,
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started)),
        'duration_s': round(finished - started, 1),
        'not_verified': list(NOT_VERIFIED),
    }


def main(argv: list[str] | None = None, *, lock: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('--out-dir', type=Path, default=default_out_dir(dict(os.environ)),
                        help='run directory root, outside the repository '
                             '(default $XDG_STATE_HOME/mainframe-rag/local-verify)')
    parser.add_argument('--python', type=Path, default=ROOT / '.venv/bin/python',
                        help='prepared interpreter (default .venv/bin/python; recorded)')
    parser.add_argument('--pass-env', action='append', default=[], metavar='NAME',
                        help='pass one more inherited variable to the tests (recorded)')
    cap = parser.add_mutually_exclusive_group()
    cap.add_argument('--memory-max', type=int, default=DEFAULT_MEMORY_MAX_MIB, metavar='MIB')
    cap.add_argument('--no-memory-cap', action='store_true', help='run uncapped (recorded)')
    parser.add_argument('--select', action='append', default=[], metavar='TEST',
                        help='auto mode: add tests; required to cover unmapped changes (recorded)')
    parser.add_argument('--t2', action='store_true', help='auto mode: force T2')
    parser.add_argument('selectors', nargs='*', help='explicit mode: run exactly these tests')
    args = parser.parse_args(argv)
    lock = lock or colab_assignment_lock()
    try:
        if not sys.platform.startswith('linux'):
            raise Refusal('the process and lock checks need Linux /proc')
        if any(not ENV_NAME.fullmatch(name) for name in args.pass_env):
            raise Refusal('--pass-env takes variable names only')
        if args.selectors and (args.select or args.t2):
            raise Refusal('--select and --t2 apply to auto mode; explicit mode runs exactly the named tests')
        for given in (args.selectors, args.select):
            if given:
                validate_selectors(ROOT, given)
        state = tree_state(ROOT)
        selection: dict[str, Any]
        if args.selectors:
            tier, tests, changed = 'explicit', list(args.selectors), []
            selection = {'mode': 'explicit', 'selectors': tests}
        else:
            changed = changed_paths(ROOT, state)
            if not changed:
                raise Refusal('no change against origin/main; name tests explicitly')
            chosen = local_selection.select(changed, force_t2=args.t2)
            if chosen.harness and not chosen.tests and not args.select:
                raise Refusal('harness self-test change(s) are owned by CI or isolated pytest: '
                              + ', '.join(chosen.harness) + '; ' + HARNESS_SELECTION_REFUSAL)
            if chosen.unmapped and not args.select:
                raise Refusal('unmapped change(s) with no test: ' + ', '.join(chosen.unmapped)
                              + '; cover them with --select tests/<file> (recorded in the receipt)')
            tier, tests = chosen.tier, sorted(set(chosen.tests) | set(args.select))
            selection = {'mode': 'auto', 'changed': changed, **chosen.as_record(), 'tests': tests,
                         'override': ({'select': args.select, 'unmapped': list(chosen.unmapped)}
                                      if args.select else None)}
        run = args.out_dir / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
                              + f'-{(state["head"] or "nohead")[:12]}' + ('-dirty' if state['dirty'] else ''))
        check_outside_tree(run, ROOT)
        (run / 'tmp').mkdir(parents=True)
        check_disk_backed(run, Path('/proc/self/mounts').read_text())
        env = child_env(dict(os.environ), ROOT, run / 'tmp', args.pass_env)
        dropped = sorted(set(os.environ) - set(env))
        python = args.python
        check_interpreter(ROOT, python, env)
        lock_sensitive = colab_lock_sensitive(tests)
        holder = assignment_lock_holder(lock) if lock_sensitive else None
        if holder:
            raise Refusal(f'Colab assignment lock is held by {holder} and the selection runs '
                          f'{", ".join(lock_sensitive)}; stop that run first')
        strays = stray_pytest(ROOT)
        if strays:
            raise Refusal(f'pytest already running from this tree: PID {", ".join(map(str, strays))}')
        memory_max = None if args.no_memory_cap else args.memory_max
        if memory_max is not None and subprocess.run(
                ['systemd-run', '--user', '--scope', '-q', 'true'], env=env, capture_output=True,
                check=False).returncode != 0:
            raise Refusal('systemd-run --user is unavailable for the memory cap; pass --no-memory-cap')
    except (Refusal, OSError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 2
    print(f'selection: {tier}, {len(tests)} test module(s)')
    for test in tests:
        why: list[str] = selection.get('reasons', {}).get(test, [])
        print(f'  {test}' + (f'  <- {", ".join(why[:3])}' + (' ...' if len(why) > 3 else '') if why else ''))
    junit = run / 'junit.xml'
    command = [str(python), '-m', 'pytest', '-p', 'anyio.pytest_plugin', '-p', 'no:cacheprovider',
               '--basetemp', str(run / 'bt'), '--junitxml', str(junit), *tests]
    if memory_max is not None:
        command = ['systemd-run', '--user', '--scope', '-q', '-p', f'MemoryMax={memory_max}M',
                   '-p', 'MemorySwapMax=0', *command]
    started = time.time()
    t0 = run_t0(ROOT, python, env, changed) if selection['mode'] == 'auto' else None
    ran = bool(tests) and not t0_failures(t0)
    code, counts = 0, None
    if ran:
        code = subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
        try:
            counts = junit_bytes(junit.read_bytes())
        except (OSError, ValueError, ET.ParseError):
            counts = None
    finished = time.time()
    receipt = build_receipt(state=state, tier=tier, selection=selection, command=command, env=env,
                            dropped=dropped, passed=args.pass_env, memory_max=memory_max, code=code,
                            counts=counts, started=started, finished=finished, t0=t0, ran=ran,
                            colab_lock_checked=bool(lock_sensitive))
    (run / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'local verification: {receipt["result"]} ({receipt["label"]})')
    print(f'receipt: {run / "receipt.json"}')
    print('not verified: ' + '; '.join(NOT_VERIFIED))
    return 0 if receipt['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
