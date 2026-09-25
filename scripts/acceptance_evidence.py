"""Read native CI receipts as bounded data for the approved-base acceptance policy.

Callers supply API records fetched by numeric ID, the current candidate, and
approved producer definitions. No artifact is imported, extracted or executed.
This normalizer does not publish checks or establish human review approval.
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from typing import Any

from scripts.ci_evidence import junit_bytes
from scripts.unit_evidence import validate as validate_unit_coverage

LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class NativeProducer:
    workflow: str
    job: str
    job_key: str
    lane: str
    artifact: str
    kind: str
    tests: bool = False
    structured: bool = False


# These are native job names, including each independently executed unit shard.
# They do not replace review_tooling's impact/required-lane policy.
PRODUCERS = (
    NativeProducer('agent-context.yml', 'check-context', 'check-context', 'context_check', 'context', 'execution', True),
    NativeProducer('ci.yml', 'lint', 'lint', 'lint_and_types', 'lint', 'execution'),
    NativeProducer('ci.yml', 'unit (1/2)', 'unit', 'unit_tests', 'unit-1', 'execution', True),
    NativeProducer('ci.yml', 'unit (2/2)', 'unit', 'unit_tests', 'unit-2', 'execution', True),
    NativeProducer('ci.yml', 'sim', 'sim', 'simulation', 'sim', 'execution', True),
    NativeProducer('ci.yml', 'gate-l1', 'gate-l1', 'gate_l1', 'gate-l1', 'execution', structured=True),
    NativeProducer('ci.yml', 'hazards', 'hazards', 'hazards', 'hazards', 'execution', structured=True),
    NativeProducer('load.yml', 'load', 'load', 'load', 'load', 'execution', True),
    NativeProducer('ha.yml', 'ha', 'ha', 'ha', 'ha', 'execution', True),
    NativeProducer('e2e.yml', 'build', 'build', 'packaging', 'build', 'identity'),
    NativeProducer('e2e.yml', 'airgap-dryrun', 'airgap-dryrun', 'packaging', 'airgap-dryrun', 'identity'),
)


def require(condition: bool) -> None:
    if not condition:
        raise ValueError('native evidence identity or execution does not satisfy policy')


def object_json(raw: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    require(len(raw) <= LIMIT)
    value = json.loads(raw, object_pairs_hook=unique)
    require(isinstance(value, dict))
    return value


def artifact_members(raw: bytes, digest: str) -> dict[str, bytes]:
    require(len(raw) <= LIMIT and digest == 'sha256:' + hashlib.sha256(raw).hexdigest())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(1 <= len(entries) <= 3)
        require(sum(item.file_size for item in entries) <= LIMIT)
        result = {}
        for item in entries:
            require(item.filename in {'evidence.json', 'tests.xml', 'results.json'})
            require(item.filename not in result and not item.is_dir())
            require(not stat.S_ISLNK(item.external_attr >> 16))
            require(not item.flag_bits & 1)
            result[item.filename] = archive.read(item)
        require('evidence.json' in result)
        return result


def validate_hazard_report(report: dict[str, Any], execution: str, policy: dict[str, Any] | None) -> None:
    """Require every approved challenge and its intended behavioral kill."""
    require(isinstance(policy, dict))
    assert policy is not None
    catalogue_bytes = policy['catalogue']
    catalogue = object_json(catalogue_bytes)
    require(catalogue['schema_version'] == 1)
    hazards = catalogue['hazards']
    require(isinstance(hazards, list) and bool(hazards))
    expected = {hazard['id']: hazard for hazard in hazards}
    require(len(expected) == len(hazards))
    require(type(report.get('schema_version')) is int and report['schema_version'] == 1)
    require(report.get('candidate_sha') == execution)
    require(report.get('catalogue_sha256') == hashlib.sha256(catalogue_bytes).hexdigest())
    require(report.get('runner_sha256') == policy['runner_sha256'])
    require(report.get('complete_catalogue') is True and report.get('passed') is True)
    results = report['results']
    require(isinstance(results, list) and len(results) == len(expected))
    seen = set()
    for result in results:
        require(isinstance(result, dict) and isinstance(result.get('id'), str))
        identity = result['id']
        require(identity in expected and identity not in seen)
        seen.add(identity)
        hazard = expected[identity]
        require(all(result.get(key) == hazard[source] for key, source in (
            ('contract', 'contract'), ('target', 'target'), ('expected_test', 'test'),
            ('expected_assertion', 'assertion'))))
        replacement = result.get('replacement')
        expected_replacement = {key: hazard[key] for key in ('before', 'after', 'occurrences', 'target_role')}
        require(isinstance(replacement, dict) and replacement == expected_replacement)
        require(all(type(replacement[key]) is type(value) for key, value in expected_replacement.items()))
        baseline, mutation = result.get('baseline'), result.get('mutation')
        require(isinstance(baseline, dict) and isinstance(mutation, dict))
        require(baseline.get('status') == 'baseline_pass' and type(baseline.get('exit_code')) is int
                and baseline['exit_code'] == 0)
        require(mutation.get('status') == 'killed_by_behavior' and type(mutation.get('exit_code')) is int
                and mutation['exit_code'] == 1 and mutation.get('cause') == hazard['assertion'])


def normalize_native(
    *, candidate: dict[str, Any], producer: NativeProducer,
    run: dict[str, Any], job: dict[str, Any], artifact: dict[str, Any],
    execution_commit: dict[str, Any], archive: bytes,
    policy_digest: str, producer_digest: str, workflow_digest: str, workflow_source: bytes,
    hazard_policy: dict[str, Any] | None = None,
    unit_policy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Reject stale/misattributed receipts before returning a native lane result.

    run must be the latest selected run; job comes from that run's *attempt*
    endpoint. Hashes identify the approved producer code, not artifact claims.
    Selection, API pagination, actor authorization and rechecking currentness
    immediately before publication remain the caller's responsibilities.
    """
    require(hashlib.sha256(workflow_source).hexdigest() == workflow_digest)
    require(run['event'] == 'pull_request')
    require(run['path'] == '.github/workflows/' + producer.workflow)
    require(run['repository']['id'] == candidate['repository_id'])
    require(run['repository']['full_name'] == candidate['repository'])
    require(run['head_sha'] == candidate['head_sha'])
    require(run['head_repository']['id'] == candidate['head_repository_id'])
    require(run['status'] == 'completed')
    require(job['run_id'] == run['id'] and job['run_attempt'] == run['run_attempt'])
    require(job['head_sha'] == candidate['head_sha'] and job['name'] == producer.job)
    require(job['status'] == 'completed' and job['conclusion'] == 'success')
    require(type(job['id']) is int and job['id'] > 0)
    require(artifact['expired'] is False)
    require(artifact['name'] == f"evidence-{producer.artifact}-attempt-{run['run_attempt']}")
    binding = artifact['workflow_run']
    require(binding['id'] == run['id'] and binding['repository_id'] == candidate['repository_id'])
    require(binding['head_sha'] == candidate['head_sha'])
    require(binding['head_repository_id'] == run['head_repository']['id'])
    files = artifact_members(archive, artifact['digest'])
    report = object_json(files['evidence.json'])
    expected = {
        'schema_version': 1, 'repository': candidate['repository'],
        'repository_id': candidate['repository_id'], 'pull_request': candidate['number'],
        'head_sha': candidate['head_sha'], 'base_sha': candidate['base_sha'],
        'event': 'pull_request', 'run_id': run['id'], 'run_attempt': run['run_attempt'],
        'job_key': producer.job_key, 'job_name': producer.job, 'lane': producer.lane,
        'actor_id': run['actor']['id'], 'triggering_actor': run['triggering_actor']['login'],
        'workflow_ref': f"{candidate['repository']}/.github/workflows/{producer.workflow}@refs/pull/{candidate['number']}/merge",
        'policy_sha256': policy_digest, 'producer_sha256': producer_digest,
        'evidence_kind': producer.kind,
    }
    require(all(type(report.get(key)) is type(value) and report.get(key) == value
                for key, value in expected.items()))
    require(report['passed'] is True and type(report['exit_code']) is int and report['exit_code'] == 0)
    # Native API head_sha is the PR head, whereas checkout/workflow SHA is the
    # merge revision. The commit API, not the receipt, establishes its parents.
    require(report['execution_sha'] == execution_commit['sha'] == candidate['execution_sha'])
    parents = [parent['sha'] for parent in execution_commit['parents']]
    require(parents == [candidate['base_sha'], candidate['head_sha']])
    require(report['execution_parents'] == parents)
    require(report['workflow_sha'] == execution_commit['sha'])
    counts = report['tests']
    if producer.tests:
        require(isinstance(counts, dict))
        require(type(counts.get('executed')) is int and counts['executed'] > 0)
        require(all(type(counts.get(key)) is int and counts[key] == 0
                    for key in ('failed', 'errors', 'skipped')))
        if producer.lane == 'context_check':
            # The dependency-free unittest producer counts its actual result;
            # its approved code is the authority, not a JUnit file it never emits.
            require('tests.xml' not in files)
        else:
            require(junit_bytes(files['tests.xml']) == counts)
    else:
        require(counts is None and 'tests.xml' not in files)
    unit_coverage = None
    if producer.lane == 'unit_tests':
        if unit_policy is None:
            raise ValueError("native unit selection policy is required")
        shard = {'unit (1/2)': 1, 'unit (2/2)': 2}[producer.job]
        unit_coverage = validate_unit_coverage(report['unit_coverage'], shard, files['tests.xml'], unit_policy)
    if producer.structured:
        require(report['result_sha256'] == hashlib.sha256(files['results.json']).hexdigest())
        structured = object_json(files['results.json'])
        if producer.lane == 'hazards':
            validate_hazard_report(structured, candidate['execution_sha'], hazard_policy)
    else:
        require('results.json' not in files)
        structured = None
    return {'lane': producer.lane, 'status': 'success', 'run_id': run['id'],
            'run_attempt': run['run_attempt'], 'job_id': job['id'],
            'artifact_id': artifact['id'], 'artifact_digest': artifact['digest'],
            'execution_sha': execution_commit['sha'], 'tests': counts, 'results': structured,
            'unit_coverage': unit_coverage}


def paginate(get, endpoint: str, key: str | None = None, *, identity_key: str = "id") -> list[dict[str, Any]]:
    """Read every native page; truncation, duplicate IDs and changing totals fail."""
    rows: list[dict[str, Any]] = []
    require(identity_key in {"id", "filename"})
    ids: set[int | str] = set()
    total = None
    for page in range(1, 101):
        separator = '&' if '?' in endpoint else '?'
        response = get(f'{endpoint}{separator}per_page=100&page={page}')
        batch = response[key] if key else response
        require(isinstance(batch, list) and len(batch) <= 100)
        if key and 'total_count' in response:
            count = response['total_count']
            require(type(count) is int and count >= 0)
            if total is None:
                total = count
            require(count == total)
        for row in batch:
            require(isinstance(row, dict))
            identity = row.get(identity_key)
            require(type(identity) is (int if identity_key == 'id' else str))
            require(identity not in ids)
            ids.add(identity)
            rows.append(row)
        if len(batch) < 100:
            require(total is None or len(rows) == total)
            return rows
    raise ValueError('native API pagination limit reached; evidence is incomplete')
