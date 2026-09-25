#!/usr/bin/env python3
"""Execute approved historical hazard mutations in isolated candidate snapshots."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = 'tests/hazards/critical.json'


class HazardError(ValueError):
    pass


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def apply_mutation(root: Path, hazard: dict) -> None:
    relative = Path(hazard['target'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in ('src', 'scripts', 'tests'):
        raise HazardError('mutation target is outside the approved source boundaries')
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise HazardError('mutation target is missing or a symlink')
    original = path.read_text()
    before, after = hazard['before'], hazard['after']
    if not before or before == after or original.count(before) != hazard['occurrences']:
        raise HazardError('mutation preimage or occurrence count changed')
    path.write_text(original.replace(before, after))
    if path.suffix == '.py':
        compile(path.read_text(), str(path), 'exec')


def assess(xml: Path, returncode: int, hazard: dict, *, baseline: bool) -> dict:
    """A behavioral kill requires the selected assertion, never an arbitrary red run."""
    try:
        cases = list(ET.parse(xml).getroot().iter('testcase'))
    except (OSError, ET.ParseError):
        return {'status': 'invalid', 'cause': 'missing or invalid test result'}
    expected = hazard['test'].split('::')[-1]
    module = hazard['test'].split('::')[0].removesuffix('.py').replace('/', '.')
    if (len(cases) != 1 or cases[0].get('name') != expected
            or cases[0].get('classname') != module
            or list(cases[0].iter('error')) or list(cases[0].iter('skipped'))):
        return {'status': 'invalid', 'cause': 'wrong, missing, skipped or setup-error test'}
    failures = list(cases[0].iter('failure'))
    if baseline:
        return {'status': 'baseline_pass' if returncode == 0 and not failures else 'baseline_failed'}
    if returncode == 0 and not failures:
        return {'status': 'survived', 'cause': 'wrong implementation passed the selected test'}
    text = '\n'.join(f.text or '' for f in failures)
    frames = list(re.finditer(r'^([^\n]+\.py):[0-9]+: in ([^\n]+)\n', text, re.MULTILINE))
    final = text[frames[-1].end():] if frames else ''
    location = frames[-1].group(1) if frames else ''
    function = frames[-1].group(2) if frames else ''
    assertion = next((line.strip() for line in final.splitlines() if line.strip()), '')
    errors = [line for line in final.splitlines() if line.startswith('E ')]
    assertion_error = bool(errors and re.match(r'E\s+(?:assert |AssertionError(?:$|:))', errors[0]))
    if (returncode != 1 or len(failures) != 1
            or location != hazard['test'].split('::')[0]
            or function != expected.split('[')[0]
            or not (assertion == hazard['assertion'] or assertion.startswith(hazard['assertion'] + ','))
            or not assertion_error):
        return {'status': 'invalid', 'cause': 'failure was not the intended behavioral assertion'}
    return {'status': 'killed_by_behavior', 'cause': hazard['assertion'],
            'failure_message': failures[0].get('message', '')[:1500]}


def source_state(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): file_hash(path)
            for path in root.rglob('*') if path.is_file() and not path.is_symlink()}


def changed_sources(root: Path, before: dict[str, str]) -> list[str]:
    changed = []
    for name, digest in before.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
            changed.append(name)
    return changed


def subreaper(value: int | None = None) -> int:
    # Linux's subreaper makes orphaned grandchildren waitable by this runner.
    # Only children in our newly owned process group are ever signalled/reaped.
    if sys.platform != 'linux':
        raise HazardError('hazard process ownership requires Linux')
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise HazardError('cannot inspect child process ownership')
    if value is not None and libc.prctl(36, value, 0, 0, 0) != 0:
        raise HazardError('cannot establish child process ownership')
    return previous.value


def clean_group(process: subprocess.Popen) -> tuple[bool, bool]:
    """Terminate and reap this session's group; return (found, fully drained)."""
    def exists():
        process.poll()  # reap the direct child before adopted grandchildren
        while True:
            try:
                child, _ = os.waitpid(-process.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if child == 0:
                break
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    found = exists()
    for sig, seconds in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
        if not exists():
            return found, True
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not exists():
                return found, True
            time.sleep(0.01)
    return found, not exists()


def run_test(copy: Path, python: Path, hazard: dict, output: Path, *, baseline: bool,
             tools_root: Path, timeout: float = 90) -> dict:
    before = source_state(copy)
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONPATH'] = os.pathsep.join((str(copy), str(copy / 'src')))
    env['PATH'] = str(tools_root / '.tools/bin') + os.pathsep + env.get('PATH', '')
    env['AIRGAP_TASK_ARCHIVE'] = str(tools_root / '.tools/cache/task_linux_amd64.tar.gz')
    env['ALLOW_HASH_MODE'] = 'true'  # explicit dev-only synthetic fixture authorization
    command = [str(python), '-m', 'pytest', '-o', 'addopts=', '-m', 'not integration',
               hazard['test'], '-q', '--capture=tee-sys', '--tb=short', f'--junitxml={output}.xml']
    start = time.monotonic()
    timed_out = False
    previous = subreaper(1)
    private = tempfile.mkdtemp(prefix='hazard-inputs-')
    drained = True
    try:
        for key, directory in (('TMPDIR', 'tmp'), ('XDG_CACHE_HOME', 'cache'),
                               ('PYTHONPYCACHEPREFIX', 'bytecode')):
            location = Path(private) / directory
            location.mkdir()
            env[key] = str(location)
        env['PYTEST_DEBUG_TEMPROOT'] = env['TMPDIR']
        # Files avoid a descendant holding capture pipes open after pytest
        # exits. They also preserve output written before a timeout.
        with output.with_suffix('.log').open('w') as log:
            process = subprocess.Popen(command, cwd=copy, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            drained = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                leaked, drained = clean_group(process)
        drift = changed_sources(copy, before)
        detail = {'exit_code': process.returncode, 'seconds': round(time.monotonic() - start, 3),
                  'source_drift': drift, 'process_group_drained': drained}
        if not drained:
            # Never advance to the next test or delete its tree while an
            # owned process may still be writing.
            raise HazardError('owned test process group could not be drained')
        if timed_out or leaked or drift:
            cause = ('test deadline exceeded' if timed_out else
                     'test left descendant processes' if leaked else 'test changed snapshot source')
            return {'status': 'invalid', 'cause': cause, **detail}
        return {**assess(Path(str(output) + '.xml'), process.returncode, hazard, baseline=baseline),
                **detail}
    finally:
        if drained:
            shutil.rmtree(private)
        subreaper(previous)


def run_pair(archive: Path, scratch: Path, python: Path, hazard: dict,
             output: Path, tools_root: Path) -> tuple[dict, dict]:
    copies = []
    for phase in ('baseline', 'mutation'):
        copy = scratch / (hazard['id'] + '-' + phase)
        copy.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(copy, filter='data')
        copies.append(copy)
    baseline = run_test(copies[0], python, hazard, output / (hazard['id'] + '-baseline'),
                        baseline=True, tools_root=tools_root)
    if baseline['status'] != 'baseline_pass':
        return baseline, {'status': 'not_run', 'cause': 'baseline did not pass'}
    try:
        apply_mutation(copies[1], hazard)
    except (HazardError, SyntaxError, OSError):
        return baseline, {'status': 'invalid', 'cause': 'mutation could not be applied safely'}
    mutation = run_test(copies[1], python, hazard, output / (hazard['id'] + '-mutation'),
                        baseline=False, tools_root=tools_root)
    return baseline, mutation


def run(root: Path, output: Path, python: Path, selected: list[str] | None = None) -> dict:
    root, output, python = root.absolute(), output.absolute(), python.absolute()
    catalogue_path = root / CATALOGUE
    catalogue = json.loads(catalogue_path.read_text())
    if catalogue.get('schema_version') != 1 or not catalogue.get('hazards'):
        raise HazardError('unsupported or empty hazard catalogue')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if not re.fullmatch('[a-f0-9]{40}', head):
        raise HazardError('candidate identity is unavailable')
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True):
        raise HazardError('tracked candidate changes must be committed before sensitivity proof')
    for relative, local in ((CATALOGUE, catalogue_path),
                            ('scripts/check_hazard_sensitivity.py', Path(__file__))):
        committed = subprocess.check_output(['git', 'show', f'{head}:{relative}'], cwd=root)
        if local.read_bytes() != committed:
            raise HazardError('runner and catalogue must match the committed candidate')
    hazards = catalogue['hazards']
    ids = [h['id'] for h in hazards]
    if (any(not re.fullmatch('[a-z0-9-]+', key) for key in ids)
            or len(ids) != len(set(ids)) or (selected and set(selected) - set(ids))):
        raise HazardError('duplicate or unknown hazard selection')
    if selected:
        hazards = [h for h in hazards if h['id'] in selected]
    tool_env = dict(os.environ)
    tool_env['PATH'] = str(root / '.tools/bin') + os.pathsep + tool_env.get('PATH', '')
    preflight = subprocess.run([str(python), '-E', str(root / 'scripts/agent_doctor.py'),
                               '--python', str(python)], cwd=root, env=tool_env,
                              capture_output=True, text=True, timeout=15, check=False)
    if preflight.returncode != 0:
        raise HazardError('prepared dependency/tool prerequisites are unavailable')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'candidate_sha': head,
              'catalogue_sha256': file_hash(catalogue_path), 'runner_sha256': file_hash(Path(__file__)),
              'python': str(python.absolute()), 'complete_catalogue': not selected,
              'deferred': catalogue.get('deferred', []), 'results': []}
    scratch = Path(tempfile.mkdtemp(prefix='m0-hazard-'))
    archive = scratch / 'candidate.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), head], cwd=root, check=True)
    for hazard in hazards:
        result = {'id': hazard['id'], 'contract': hazard['contract'], 'target': hazard['target'],
                  'expected_test': hazard['test'], 'expected_assertion': hazard['assertion'],
                  'replacement': {'before': hazard['before'], 'after': hazard['after'],
                                  'occurrences': hazard['occurrences'], 'target_role': hazard['target_role']}}
        result['baseline'], result['mutation'] = run_pair(
            archive, scratch, python, hazard, output, root)
        report['results'].append(result)
        print(hazard['id'] + ': ' + result['mutation']['status'], flush=True)
        (output / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    # On any exception above, retain snapshots rather than deleting potentially
    # live owned inputs; only the successfully drained path removes them.
    shutil.rmtree(scratch)
    report['passed'] = bool(report['results']) and all(
        r['mutation']['status'] == 'killed_by_behavior' for r in report['results'])
    (output / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--hazard', action='append')
    args = parser.parse_args()
    try:
        result = run(args.root, args.out, args.python, args.hazard)
        return 0 if result['passed'] else 1
    except HazardError as exc:
        print(f'hazard sensitivity unavailable: {exc}', file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'hazard sensitivity unavailable: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
