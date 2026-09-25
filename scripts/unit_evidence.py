"""Controlled unit selection and data-only coverage validation (no pytest import)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

INPUTS = ('pyproject.toml', 'tests/conftest.py', 'tests/ci_shard.py', 'scripts/unit_evidence.py',
          'requirements.dev.lock.txt', 'locks/cp314-linux-x86_64.json',
          'scripts/prepare_python.py', 'scripts/dependency_lock.py', 'scripts/agent_doctor.py')
POLICY = {'testpaths': ['tests'], 'python_files': ['test_*.py', '*_test.py'],
          'python_classes': ['Test'], 'python_functions': ['test'],
          'addopts': ['-q', '-m', 'not integration']}


def require(value: bool) -> None:
    if not value:
        raise ValueError('unit collection or execution does not satisfy selection policy')


def input_hashes(root: Path) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in INPUTS}


def run_shard(root: Path, python: str, shard: int, junit: Path, output: Path) -> tuple[int, dict]:
    """Independent full collection, then execution in a fresh pytest process."""
    require(shard in (1, 2) and not junit.exists())
    require(not os.environ.get('PYTEST_ADDOPTS') and not os.environ.get('PYTEST_PLUGINS'))
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    before = input_hashes(root)
    records = {}
    code = 0
    for phase in ('collect', 'execute'):
        target = output / (phase + '.json')
        require(not target.exists())
        command = [python, '-m', 'pytest', '-p', 'anyio.pytest_plugin', '-p', 'tests.ci_shard',
                   '-o', 'addopts=', '-m', '', '-k', '', 'tests', '-q',
                   '--unit-phase=' + phase, '--unit-evidence=' + str(target),
                   '--unit-shard=' + str(shard)]
        command += ['--collect-only'] if phase == 'collect' else ['--junitxml=' + str(junit), '--durations=20']
        code = subprocess.run(command, cwd=root, env=env, check=False).returncode
        records[phase] = json.loads(target.read_text())
        if code:
            break
    require(before == input_hashes(root))
    return code, {'schema_version': 1, 'shard': shard, **records}


def node_list(value: object) -> list[str]:
    require(isinstance(value, list) and bool(value))
    require(all(isinstance(n, str) and bool(n) for n in value))
    require(len(set(value)) == len(value))
    return value


def validate(proof: dict, shard: int, xml: bytes, inputs: dict[str, str]) -> dict:
    require(type(proof.get('schema_version')) is int and proof['schema_version'] == 1
            and type(proof.get('shard')) is int and proof['shard'] == shard)
    collection, execution = proof['collect'], proof['execute']
    for phase, record in (('collect', collection), ('execute', execution)):
        require(record['passed'] is True and record['phase'] == phase)
        require(record['inputs'] == inputs and set(inputs) == set(INPUTS))
        require(record['policy'] == POLICY)
        require(set(record['versions']) == {'pytest', 'pluggy', 'anyio'}
                and all(isinstance(v, str) and v for v in record['versions'].values()))
        require(record['plugins'] == ['_pytest', 'anyio.pytest_plugin', 'tests/ci_shard.py', 'tests/conftest.py'])
        all_ids = node_list(record['all'])
        eligible = node_list(record['eligible'])
        excluded = record['integration']
        require(isinstance(excluded, list) and all(isinstance(n, str) for n in excluded))
        require(len(set(excluded)) == len(excluded))
        require(not set(eligible) & set(excluded) and sorted(eligible + excluded) == sorted(all_ids))
        require(all_ids == sorted(all_ids) and eligible == sorted(eligible) and excluded == sorted(excluded))
    for key in ('all', 'eligible', 'integration', 'inputs', 'policy', 'plugins', 'versions'):
        require(collection[key] == execution[key])
    eligible = collection['eligible']
    expected = eligible[shard - 1::2]
    require(collection['selected'] == eligible and collection['executed'] == [])
    require(execution['selected'] == expected and sorted(node_list(execution['executed'])) == expected)
    # Decode the producer's exact node ID property. Base64 preserves XML's
    # whitespace-sensitive attributes without parsing ambiguous ::/[] IDs.
    observed = []
    for case in ET.fromstring(xml).iter('testcase'):
        properties = [p for p in case.findall('./properties/property') if p.get('name') == 'native_nodeid']
        require(len(properties) == 1)
        encoded = properties[0].get('value', '')
        decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
        require(base64.b64encode(decoded.encode()).decode() == encoded)
        observed.append(decoded)
    require(sorted(node_list(observed)) == expected)
    return {'shard': shard, 'all': collection['all'], 'eligible': eligible, 'executed': expected}


def validate_union(records: list[dict]) -> None:
    require(len(records) == 2)
    first, second = sorted(records, key=lambda r: r['shard'])
    require([first['shard'], second['shard']] == [1, 2])
    require(first['all'] == second['all'] and first['eligible'] == second['eligible'])
    require(not set(first['executed']) & set(second['executed']))
    require(sorted(first['executed'] + second['executed']) == first['eligible'])
