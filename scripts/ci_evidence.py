#!/usr/bin/env python3
"""Record native CI execution evidence; this producer never grants acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))  # direct native script entry imports its approved helpers


def git(*arguments: str) -> str:
    return subprocess.check_output(['git', *arguments], cwd=ROOT, text=True).strip()


def identity() -> dict:
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    execution = git('rev-parse', 'HEAD')
    parents = git('show', '-s', '--format=%P', 'HEAD').split()
    pr = event.get('pull_request')
    head = pr['head']['sha'] if pr else execution
    base = pr['base']['sha'] if pr else (parents[0] if parents else execution)
    if not all(re.fullmatch('[a-f0-9]{40}', value) for value in (head, base, execution)):
        raise ValueError('invalid candidate identity')
    if pr and execution == head:
        git('merge-base', '--is-ancestor', base, head)
    if pr and execution != head and parents != [base, head]:
        raise ValueError('checkout does not bind the event head and base')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('tracked candidate is dirty')
    return {
        'repository': os.environ['GITHUB_REPOSITORY'],
        'repository_id': int(os.environ['GITHUB_REPOSITORY_ID']),
        'head_sha': head, 'base_sha': base, 'execution_sha': execution,
        'execution_parents': parents, 'pull_request': pr['number'] if pr else None,
        'event': os.environ['GITHUB_EVENT_NAME'],
        'workflow_ref': os.environ['GITHUB_WORKFLOW_REF'],
        'workflow_sha': os.environ['GITHUB_WORKFLOW_SHA'],
        'run_id': int(os.environ['GITHUB_RUN_ID']),
        'run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
        'job_key': os.environ['GITHUB_JOB'],
        'actor_id': int(os.environ['GITHUB_ACTOR_ID']),
        'triggering_actor': os.environ['GITHUB_TRIGGERING_ACTOR'],
        'policy_sha256': hashlib.sha256((ROOT / 'scripts/review_tooling.py').read_bytes()).hexdigest(),
        'producer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def junit_counts(path: Path) -> dict:
    """Count executed testcase records, not a claimed testsuite total or stdout."""
    return junit_bytes(path.read_bytes())


def junit_bytes(raw: bytes) -> dict:
    if len(raw) > 16 * 1024 * 1024 or b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise ValueError('unsupported test report')
    cases = list(ET.fromstring(raw).iter('testcase'))
    if not cases:
        raise ValueError('no testcases executed')
    counts = {'executed': len(cases), 'failed': 0, 'errors': 0, 'skipped': 0}
    for case in cases:
        counts['failed'] += bool(case.findall('failure'))
        counts['errors'] += bool(case.findall('error'))
        counts['skipped'] += bool(case.findall('skipped'))
    return {**counts, 'sha256': hashlib.sha256(raw).hexdigest()}


def run_unittest(modules: list[str]) -> tuple[int, dict]:
    # Match the existing dependency-free context job's unittest execution.
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    counts = {'executed': result.testsRun, 'failed': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped)}
    valid = result.wasSuccessful() and counts['executed'] > 0 and counts['skipped'] == 0
    return (0 if valid else 1), counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--job-name', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--junit', type=Path)
    parser.add_argument('--unit-shard', type=int, choices=(1, 2))
    parser.add_argument('--identity-only', action='store_true')
    parser.add_argument('--result-json', type=Path)
    parser.add_argument('--unittest', nargs='+', dest='modules')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        before = identity()
        if args.identity_only and (args.junit or args.result_json):
            raise ValueError('identity receipts cannot claim execution results')
        if args.result_json and args.result_json.exists():
            raise ValueError('structured result must be fresh')
        if args.junit and (args.junit.exists() or args.modules):
            raise ValueError('test evidence must be fresh and have one producer')
        args.output.mkdir(parents=True, exist_ok=False)
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if (args.lane == 'unit_tests') != bool(args.unit_shard):
            raise ValueError('unit evidence requires the controlled shard producer')
        if args.unit_shard and (not args.junit or args.result_json):
            raise ValueError('unit coverage requires its raw JUnit')
        if sum((bool(command), bool(args.modules), args.identity_only, bool(args.unit_shard))) != 1:
            raise ValueError('select one evidence producer')
        counts = None
        unit_coverage = None
        if args.identity_only:
            code = 0
        elif args.modules:
            code, counts = run_unittest(args.modules)
        else:
            if args.unit_shard:
                from scripts.unit_evidence import run_shard
                with tempfile.TemporaryDirectory(prefix='native-unit-') as directory:
                    code, unit_coverage = run_shard(ROOT, sys.executable, args.unit_shard,
                                                    args.junit.absolute(), Path(directory))
            else:
                code = subprocess.run(command, cwd=ROOT, check=False).returncode
            if args.junit:
                try:
                    counts = junit_counts(args.junit)
                    (args.output / 'tests.xml').write_bytes(args.junit.read_bytes())
                except (OSError, ValueError, ET.ParseError):
                    counts = {'executed': 0, 'invalid': True}
        valid = code == 0 and before == identity()
        if counts is not None:
            valid = valid and counts['executed'] > 0 and not any(
                counts.get(key) for key in ('failed', 'errors', 'skipped', 'invalid'))
        if valid and unit_coverage is not None:
            from scripts.unit_evidence import input_hashes, validate
            validate(unit_coverage, args.unit_shard, args.junit.read_bytes(), input_hashes(ROOT))
        report = {'schema_version': 1, **before, 'lane': args.lane, 'job_name': args.job_name,
                  'exit_code': code, 'passed': bool(valid), 'tests': counts,
                  'evidence_kind': 'identity' if args.identity_only else 'execution'}
        if unit_coverage is not None:
            report['unit_coverage'] = unit_coverage
        if args.result_json:
            raw = args.result_json.read_bytes()
            if len(raw) > 16 * 1024 * 1024 or not isinstance(json.loads(raw), dict):
                raise ValueError('invalid structured result')
            (args.output / 'results.json').write_bytes(raw)
            report['result_sha256'] = hashlib.sha256(raw).hexdigest()
        (args.output / 'evidence.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        return 0 if valid else 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('CI evidence unavailable: invalid identity, invocation or output', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
