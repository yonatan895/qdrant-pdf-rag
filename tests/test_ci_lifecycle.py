"""Snapshot checks must restore data and compare exact results before cleanup."""
import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fault', ['', 'count', 'query'])
def test_snapshot_check_restores_and_compares_data(monkeypatch, tmp_path, fault):
    calls = []

    class Response:
        status_code = 200
        content = b'synthetic snapshot bytes'

        def __init__(self, result):
            self.result = result

        def raise_for_status(self):
            pass

        def json(self):
            return {'result': self.result}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['headers'] == {'api-key': 'test-only'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            calls.append(('get', url))
            if '/snapshots/' in url:
                return Response({})
            count = 0 if fault == 'count' and '/ci-restore-' in url else 7
            return Response({'points_count': count})

        def post(self, url, **kwargs):
            calls.append(('post', url))
            if '/snapshots/upload' in url:
                assert kwargs['files']['snapshot'][1] == Response.content
                assert url.endswith('?priority=snapshot')
                return Response({})
            if url.endswith('/points/scroll'):
                assert kwargs['json']['with_vector'] is True
                return Response({'points': [{'vector': {'dense': [1.0, 0.0]}}]})
            if url.endswith('/points/query'):
                assert kwargs['json']['params']['exact'] is True
                assert kwargs['json']['query'] == [1.0, 0.0]
                ids = [9] if fault == 'query' and '/ci-restore-' in url else [1, 2]
                return Response({'points': [{'id': i} for i in ids]})
            assert url.endswith('/snapshots')
            return Response({'name': 'synthetic.snapshot'})

        def delete(self, url):
            calls.append(('delete', url))
            assert '/ci-restore-' in url
            return Response({})

    settings = SimpleNamespace(qdrant_url='http://qdrant', qdrant_collection='synthetic', qdrant_api_key='test-only')
    monkeypatch.setitem(sys.modules, 'httpx2', SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, 'mainframe_rag.config', SimpleNamespace(load_settings=lambda: settings))
    script = runpy.run_path(str(ROOT / 'scripts/ci/check_snapshot.py'))['SNAPSHOT_CHECK']
    executable = tmp_path / "snapshot_check.py"
    executable.write_text(script)
    if fault:
        with pytest.raises(AssertionError):
            runpy.run_path(str(executable))
    else:
        runpy.run_path(str(executable))
    assert any(method == 'post' and '/snapshots/upload' in url for method, url in calls)
    assert calls[-1][0] == 'delete'


def ingest_job():
    return {'spec': {'template': {'spec': {
        'imagePullSecrets': [{'name': 'registry-pull'}],
        'serviceAccountName': 'ingest-runner',
        'securityContext': {'runAsNonRoot': True},
        'volumes': [{'name': 'corpus', 'persistentVolumeClaim': {'claimName': 'corpus'}}],
        'containers': [{
            'name': 'ingest', 'image': 'registry.test/ingest:published-sha',
            'args': ['--src', '/corpus'],
            'volumeMounts': [{'name': 'corpus', 'mountPath': '/corpus'}],
            'envFrom': [{'secretRef': {'name': 'unrelated-ingest-credentials'}}],
            'resources': {'limits': {'memory': '512Mi'}},
            'securityContext': {'allowPrivilegeEscalation': False},
            'env': [
                {'name': 'QDRANT_URL', 'value': 'http://qdrant:6333'},
                {'name': 'QDRANT_COLLECTION', 'value': 'synthetic'},
                {'name': 'QDRANT_API_KEY', 'valueFrom': {
                    'secretKeyRef': {'name': 'custom-qdrant-apikey', 'key': 'api-key'}}},
                {'name': 'EMBED_API_KEY', 'valueFrom': {
                    'secretKeyRef': {'name': 'gateway', 'key': 'embed-api-key'}}},
            ],
        }],
    }}}}


@pytest.mark.parametrize('failure', ['', 'create', 'wait', 'logs', 'delete', 'get'])
def test_lifecycle_runs_snapshot_as_writer_job_and_cleans_up(tmp_path, failure):
    """Exercise the shell entrypoint: read-only agent refuses snapshot mutations."""
    log = tmp_path / 'calls.jsonl'
    source = tmp_path / 'ingest.json'
    source.write_text(json.dumps(ingest_job()))
    stub = tmp_path / 'kubectl'
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
payload=sys.stdin.read() if '-i' in args or args[-1:] == ['-'] else ''
with open(os.environ['CALL_LOG'],'a') as f:
    f.write(json.dumps({'args':args,'input':payload})+'\\n')
command=args[2]
if command == 'get' and 'job' in args:
    print(Path(os.environ['INGEST_JOB']).read_text())
elif command == 'exec':
    # Before the fix, check_lifecycle executed /snapshots with the agent's
    # read-only key. A 403 must remain a failure, never be ignored.
    if '/snapshots' in payload:
        print('403 Forbidden: serving credential cannot create snapshots',file=sys.stderr)
        sys.exit(1)
    if "['traceID']" in payload:
        print('trace-preserved')
elif command == 'get' and 'pvc' in args:
    print('same-pvc-uid')
if command == os.environ['FAIL_AT']:
    sys.exit(1)
''')
    stub.chmod(0o755)
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/check_lifecycle.sh'), 'test-lifecycle'],
                            env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
                                 'CALL_LOG': str(log), 'INGEST_JOB': str(source), 'FAIL_AT': failure},
                            text=True, capture_output=True, check=False, timeout=10)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert (result.returncode == 0) == (not failure)
    creates = [c for c in calls if c['args'][2] == 'create']
    deletes = [c for c in calls if c['args'][2] == 'delete']
    if failure == 'get':
        assert not creates and not deletes
        return
    job = json.loads(creates[0]['input'])
    spec = job['spec']['template']['spec']
    container = spec['containers'][0]
    assert job['metadata']['namespace'] == 'test-lifecycle'
    assert job['spec']['backoffLimit'] == 0
    assert job['spec']['activeDeadlineSeconds'] == 180
    assert spec['automountServiceAccountToken'] is False
    assert spec['serviceAccountName'] == 'ingest-runner'
    assert spec['imagePullSecrets'] == [{'name': 'registry-pull'}]
    assert spec['securityContext'] == {'runAsNonRoot': True}
    assert container['image'] == 'registry.test/ingest:published-sha'
    assert container['resources'] == {'limits': {'memory': '512Mi'}}
    assert container['securityContext'] == {'allowPrivilegeEscalation': False}
    assert 'volumes' not in spec and 'volumeMounts' not in container and 'args' not in container
    assert 'envFrom' not in container
    env = {e['name']: e for e in container['env']}
    assert set(env) == {'QDRANT_URL', 'QDRANT_COLLECTION', 'QDRANT_API_KEY'}
    assert env['QDRANT_API_KEY']['valueFrom']['secretKeyRef'] == {
        'name': 'custom-qdrant-apikey', 'key': 'api-key'}
    assert container['command'][:2] == ['python3', '-c']
    assert container['command'][2] == runpy.run_path(str(ROOT / 'scripts/ci/check_snapshot.py'))['SNAPSHOT_CHECK']
    assert bool(deletes) == (failure != 'create')
    if deletes:
        assert deletes[0]['args'][3] == 'job/' + job['metadata']['name']
    rollouts = [c for c in calls if c['args'][2] == 'rollout']
    assert bool(rollouts) == (not failure)
    if not failure:
        assert any('smoke_search.py' in ' '.join(c['args']) for c in calls)
        assert calls[-1]['args'][2] == 'exec' and 'Pre-restart trace persisted' in calls[-1]['input']
    assert not any(c['args'][2] == 'get' and 'secret' in c['args'] for c in calls)


@pytest.mark.parametrize('key_kind', ['read-only', 'literal', 'missing-name'])
def test_snapshot_job_refuses_wrong_credential_reference(key_kind):
    helper = runpy.run_path(str(ROOT / 'scripts/ci/check_snapshot.py'))
    source = ingest_job()
    key = source['spec']['template']['spec']['containers'][0]['env'][2]
    if key_kind == 'read-only':
        key['valueFrom']['secretKeyRef']['key'] = 'read-only-api-key'
    elif key_kind == 'literal':
        key.clear()
        key.update(name='QDRANT_API_KEY', value='not-a-reference')
    else:
        del key['valueFrom']['secretKeyRef']['name']
    with pytest.raises(ValueError, match='full-access Qdrant Secret key'):
        helper['maintenance_job'](source, 'test-lifecycle')


def test_snapshot_job_does_not_require_optional_pod_fields_or_mutate_ingest():
    helper = runpy.run_path(str(ROOT / 'scripts/ci/check_snapshot.py'))
    source = ingest_job()
    spec = source['spec']['template']['spec']
    for field in ('imagePullSecrets', 'serviceAccountName', 'securityContext'):
        del spec[field]
    before = copy.deepcopy(source)
    first = helper['maintenance_job'](source, 'test-lifecycle')
    second = helper['maintenance_job'](source, 'test-lifecycle')
    assert first['metadata']['name'] != second['metadata']['name']
    assert source == before


@pytest.mark.parametrize('args', [[], ['test', '--skip'], ['BAD'], ['--bad'], ['-bad'], ['x' * 64]])
def test_snapshot_helper_refuses_bad_arguments_before_cluster_calls(args):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/check_snapshot.py'), *args],
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 2
