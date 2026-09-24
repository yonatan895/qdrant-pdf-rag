#!/usr/bin/env python3
"""Execute approved historical hazard mutations in isolated candidate snapshots."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    if (returncode != 1 or len(failures) != 1 or hazard['assertion'] not in text
            or 'AssertionError' not in text):
        return {'status': 'invalid', 'cause': 'failure was not the intended behavioral assertion'}
    return {'status': 'killed_by_behavior', 'cause': hazard['assertion'],
            'failure_message': failures[0].get('message', '')[:1500]}


def run_test(copy: Path, python: Path, hazard: dict, output: Path, *, baseline: bool,
             tools_root: Path) -> dict:
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(copy), str(copy / 'src')))
    env['PATH'] = str(tools_root / '.tools/bin') + os.pathsep + env.get('PATH', '')
    env['AIRGAP_TASK_ARCHIVE'] = str(tools_root / '.tools/cache/task_linux_amd64.tar.gz')
    env['ALLOW_HASH_MODE'] = 'true'  # explicit dev-only synthetic fixture authorization
    # No installs, downloads or service start is part of this runner. Catalogue
    # tests are hermetic existing suites; integration selection stays excluded.
    command = [str(python), '-m', 'pytest', '-o', 'addopts=', '-m', 'not integration',
               hazard['test'], '-q', '--tb=short', f'--junitxml={output}.xml']
    start = time.monotonic()
    try:
        result = subprocess.run(command, cwd=copy, env=env, capture_output=True, text=True,
                                timeout=90, check=False)
    except subprocess.TimeoutExpired:
        return {'status': 'invalid', 'cause': 'test deadline exceeded'}
    output.with_suffix('.log').write_text(result.stdout + result.stderr)
    verdict = assess(Path(str(output) + '.xml'), result.returncode, hazard, baseline=baseline)
    return {**verdict, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - start, 3)}


def run(root: Path, output: Path, python: Path, selected: list[str] | None = None) -> dict:
    catalogue_path = root / CATALOGUE
    catalogue = json.loads(catalogue_path.read_text())
    if catalogue.get('schema_version') != 1 or not catalogue.get('hazards'):
        raise HazardError('unsupported or empty hazard catalogue')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if not re.fullmatch('[a-f0-9]{40}', head):
        raise HazardError('candidate identity is unavailable')
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True):
        raise HazardError('tracked candidate changes must be committed before sensitivity proof')
    hazards = catalogue['hazards']
    ids = [h['id'] for h in hazards]
    if len(ids) != len(set(ids)) or (selected and set(selected) - set(ids)):
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
    with tempfile.TemporaryDirectory(prefix='m0-hazard-') as temporary:
        scratch = Path(temporary)
        archive = scratch / 'candidate.tar'
        subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), head], cwd=root, check=True)
        for hazard in hazards:
            result = {'id': hazard['id'], 'contract': hazard['contract'], 'target': hazard['target'],
                      'expected_test': hazard['test'], 'expected_assertion': hazard['assertion']}
            copy = scratch / hazard['id']
            copy.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(copy, filter='data')
            baseline = run_test(copy, python, hazard, output / (hazard['id'] + '-baseline'),
                                baseline=True, tools_root=root)
            result['baseline'] = baseline
            if baseline['status'] == 'baseline_pass':
                try:
                    apply_mutation(copy, hazard)
                    result['mutation'] = run_test(copy, python, hazard, output / (hazard['id'] + '-mutation'),
                                                  baseline=False, tools_root=root)
                except (HazardError, SyntaxError, OSError):
                    result['mutation'] = {'status': 'invalid', 'cause': 'mutation could not be applied safely'}
            else:
                result['mutation'] = {'status': 'not_run', 'cause': 'baseline did not pass'}
            report['results'].append(result)
            print(hazard['id'] + ': ' + result['mutation']['status'], flush=True)
            (output / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
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
