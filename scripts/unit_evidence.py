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


def run_shard(root: Path, python: str, shard: int, junit: Path, output: Path,
              *, shards: int = 2) -> tuple[int, dict]:
    """Independent full collection, then execution in a fresh pytest process."""
    require(type(shards) is int and shards in (2, 4)
            and type(shard) is int and 1 <= shard <= shards and not junit.exists())
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
                   '--unit-shard=' + str(shard), '--unit-shards=' + str(shards)]
        command += ['--collect-only'] if phase == 'collect' else ['--junitxml=' + str(junit), '--durations=20']
        code = subprocess.run(command, cwd=root, env=env, check=False).returncode
        records[phase] = json.loads(target.read_text())
        if code:
            break
    require(before == input_hashes(root))
    return code, {'schema_version': 1, 'shard': shard, **records}


def node_list(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError('unit collection or execution does not satisfy selection policy')
    require(all(isinstance(n, str) and bool(n) for n in value))
    require(len(set(value)) == len(value))
    return value


def validate(proof: dict, shard: int, xml: bytes, inputs: dict[str, str], *, shards: int = 2) -> dict:
    require(type(shards) is int and shards in (2, 4)
            and type(shard) is int and 1 <= shard <= shards)
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
    expected = eligible[shard - 1::shards]
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


def validate_union(records: list[dict], *, shards: int = 2) -> None:
    require(type(shards) is int and shards in (2, 4) and len(records) == shards)
    ordered = sorted(records, key=lambda record: record['shard'])
    require(all(type(record['shard']) is int for record in ordered))
    require([record['shard'] for record in ordered] == list(range(1, shards + 1)))
    first = ordered[0]
    executed = []
    for record in ordered:
        require(record['all'] == first['all'] and record['eligible'] == first['eligible'])
        require(record['executed'] == first['eligible'][record['shard'] - 1::shards])
        executed.extend(record['executed'])
    require(len(executed) == len(set(executed)) and sorted(executed) == first['eligible'])
