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
          'scripts/prepare_python.py', 'scripts/dependency_lock.py', 'scripts/agent_doctor.py',
          'scripts/devkit_key.py')
POLICY = {'testpaths': ['tests'], 'python_files': ['test_*.py', '*_test.py'],
          'python_classes': ['Test'], 'python_functions': ['test'],
          'addopts': ['-q', '-m', 'not integration']}


# Shard-assignment rules. Without a "rule" key a proof means the original
# round-robin rule: sorted(eligible)[shard - 1::shards]. RULE names the balanced
# rule: longest-processing-time-first over recorded per-test milliseconds.
RULE = 'lpt-durations-v1'
RULE_KEYS = {'name', 'durations_sha256', 'durations_text'}
MAX_DURATIONS_BYTES = 8 * 1024 * 1024


def require(value: bool) -> None:
    if not value:
        raise ValueError('unit collection or execution does not satisfy selection policy')


def parse_durations(text: str) -> tuple[int, dict[str, int]]:
    """Return (default_ms, {node id: ms}) from a durations file; fail closed on any other shape."""
    require(isinstance(text, str) and len(text) <= MAX_DURATIONS_BYTES)

    def unique(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs))
        return result

    data = json.loads(text, object_pairs_hook=unique)
    require(isinstance(data, dict) and set(data) == {'schema_version', 'default_ms', 'ms'})
    require(type(data['schema_version']) is int and data['schema_version'] == 1)
    default_ms, ms = data['default_ms'], data['ms']
    require(type(default_ms) is int and default_ms >= 1 and isinstance(ms, dict))
    require(all(isinstance(k, str) and k and type(v) is int and v >= 0 for k, v in ms.items()))
    return default_ms, ms


def assign_balanced(nodes: list[str], shards: int, default_ms: int, ms: dict[str, int]) -> list[list[str]]:
    """Deterministic LPT: heaviest first (ties by node id) onto the lightest shard (ties by lowest index).

    Unknown nodes weigh default_ms; every weight is at least 1 so zero-cost
    tests still spread. Each returned shard is sorted by node id.
    """
    require(len(set(nodes)) == len(nodes) and type(shards) is int and shards >= 1)
    weight = {node: max(1, ms.get(node, default_ms)) for node in nodes}
    loads = [0] * shards
    bins: list[list[str]] = [[] for _ in range(shards)]
    for node in sorted(nodes, key=lambda n: (-weight[n], n)):
        target = min(range(shards), key=lambda k: (loads[k], k))
        bins[target].append(node)
        loads[target] += weight[node]
    return [sorted(b) for b in bins]


def select_nodes(nodes: list[str], shard: int, shards: int, durations_text: str | None = None) -> list[str]:
    """The one shard-assignment function: round-robin without durations, balanced with them."""
    ordered = sorted(nodes)
    if durations_text is None:
        return ordered[shard - 1::shards]
    default_ms, ms = parse_durations(durations_text)
    return assign_balanced(ordered, shards, default_ms, ms)[shard - 1]


def rule_record(durations_text: str) -> dict[str, str]:
    parse_durations(durations_text)
    return {'name': RULE, 'durations_sha256': hashlib.sha256(durations_text.encode()).hexdigest(),
            'durations_text': durations_text}


def record_durations(record: dict) -> str | None:
    """Durations text a proof record claims, after checking its hash; None means round-robin."""
    if 'rule' not in record:
        return None
    rule = record['rule']
    require(isinstance(rule, dict) and set(rule) == RULE_KEYS and rule['name'] == RULE
            and isinstance(rule['durations_text'], str) and isinstance(rule['durations_sha256'], str))
    require(hashlib.sha256(rule['durations_text'].encode()).hexdigest() == rule['durations_sha256'])
    parse_durations(rule['durations_text'])
    return rule['durations_text']


def input_hashes(root: Path) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in INPUTS}


def run_shard(root: Path, python: str, shard: int, junit: Path, output: Path,
              *, shards: int = 2, durations: Path | None = None) -> tuple[int, dict]:
    """Independent full collection, then execution in a fresh pytest process."""
    require(type(shards) is int and shards in (2, 4, 8)
            and type(shard) is int and 1 <= shard <= shards and not junit.exists())
    require(not os.environ.get('PYTEST_ADDOPTS') and not os.environ.get('PYTEST_PLUGINS'))
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    before = input_hashes(root)
    durations_before = hashlib.sha256((root / durations).read_bytes()).hexdigest() if durations else None
    records = {}
    code = 0
    for phase in ('collect', 'execute'):
        target = output / (phase + '.json')
        require(not target.exists())
        command = [python, '-m', 'pytest', '-p', 'anyio.pytest_plugin', '-p', 'tests.ci_shard',
                   '-o', 'addopts=', '-m', '', '-k', '', 'tests', '-q',
                   '--unit-phase=' + phase, '--unit-evidence=' + str(target),
                   '--unit-shard=' + str(shard), '--unit-shards=' + str(shards)]
        if durations:
            command.append('--unit-durations=' + str(durations))
        command += ['--collect-only'] if phase == 'collect' else ['--junitxml=' + str(junit), '--durations=20']
        code = subprocess.run(command, cwd=root, env=env, check=False).returncode
        records[phase] = json.loads(target.read_text())
        if code:
            break
    require(before == input_hashes(root))
    if durations:
        require(durations_before == hashlib.sha256((root / durations).read_bytes()).hexdigest())
    return code, {'schema_version': 1, 'shard': shard, **records}


def node_list(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError('unit collection or execution does not satisfy selection policy')
    require(all(isinstance(n, str) and bool(n) for n in value))
    require(len(set(value)) == len(value))
    return value


def validate(proof: dict, shard: int, xml: bytes, inputs: dict[str, str], *, shards: int = 2) -> dict:
    require(type(shards) is int and shards in (2, 4, 8)
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
    require(('rule' in collection) == ('rule' in execution) and collection.get('rule') == execution.get('rule'))
    durations_text = record_durations(collection)
    eligible = collection['eligible']
    expected = select_nodes(eligible, shard, shards, durations_text)
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
    return {'shard': shard, 'all': collection['all'], 'eligible': eligible, 'executed': expected,
            'rule_sha256': collection['rule']['durations_sha256'] if durations_text is not None else None}


def validate_union(records: list[dict], *, shards: int = 2) -> None:
    require(type(shards) is int and shards in (2, 4, 8) and len(records) == shards)
    ordered = sorted(records, key=lambda record: record['shard'])
    require(all(type(record['shard']) is int for record in ordered))
    require([record['shard'] for record in ordered] == list(range(1, shards + 1)))
    first = ordered[0]
    executed = []
    # Every shard must have been validated against one rule; for the balanced
    # rule validate() already recomputed the assignment from its durations
    # hash, so only the exact-partition proof below remains.
    require(all(record.get('rule_sha256') == first.get('rule_sha256') for record in ordered))
    for record in ordered:
        require(record['all'] == first['all'] and record['eligible'] == first['eligible'])
        if first.get('rule_sha256') is None:
            require(record['executed'] == first['eligible'][record['shard'] - 1::shards])
        executed.extend(record['executed'])
    require(len(executed) == len(set(executed)) and sorted(executed) == first['eligible'])
