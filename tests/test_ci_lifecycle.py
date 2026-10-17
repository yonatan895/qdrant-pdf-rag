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


class Response:
    def __init__(self, result, status=200, content=b''):
        # A wire response is a copy: later store changes must not rewrite what was read.
        self.result, self.status_code, self.content = copy.deepcopy(result), status, content

    def raise_for_status(self):
        assert self.status_code < 400

    def json(self):
        return {'result': self.result}


class FakeQdrant:
    """In-memory stand-in for the Qdrant REST surface the snapshot check uses.

    A snapshot is the JSON of a collection, so an upload really recreates it,
    and each `fault` models a restore (or cleanup) that brings back less, or
    different, state than the source holds.
    """

    def __init__(self, fault):
        self.fault = fault
        vectors = {'dense': [1.0, 0.0], 'bm25': {'indices': [0], 'values': [1.0]}}
        zero = {'dense': [0.0, 0.0], 'bm25': {'indices': [0], 'values': [1.0]}}
        manifest = {'record_type': 'representation-manifest', 'state': 'committed', 'manifest': {'dim': 2}}
        if fault == 'uncommitted-source':
            manifest['state'] = 'pending'
        self.collections = {
            'physical': {f'd{i}': ({'doc_id': 'a', 'text': f'synthetic {i}'}, vectors) for i in range(5)},
            'physical__completions': {
                'm': (manifest, zero),
                'p': ({'record_type': 'publication-metadata', 'target_collection': 'physical__completions'}, zero),
                'c': ({'doc_id': 'a', 'chunk_ids_digest': 'x' * 8, 'expected_chunks': 5}, zero),
            },
        }
        self.aliases = {'synthetic': 'physical', 'physical__build_1': 'physical'}
        self.calls = []

    @staticmethod
    def name(url):
        return url.split('/collections/')[1].split('/')[0].split('?')[0]

    def get(self, url):
        self.calls.append(('get', url))
        if url.endswith('/aliases'):
            return Response({'aliases': [{'alias_name': a, 'collection_name': c} for a, c in self.aliases.items()]})
        name = self.name(url)
        if '/snapshots/' in url:
            return Response({}, content=json.dumps(self.collections[name]).encode())
        if name not in self.collections:
            return Response({}, status=404)
        count = len(self.collections[name])
        return Response({'points_count': 0 if self.fault == 'count' and name.startswith('ci-restore-') else count})

    def post(self, url, **kwargs):
        self.calls.append(('post', url))
        name = self.name(url)
        points = self.collections.get(name, {})
        if '/snapshots/upload' in url:
            assert url.endswith('?priority=snapshot') and name.startswith('ci-restore-')
            restored = {k: (copy.deepcopy(v[0]), v[1])
                        for k, v in json.loads(kwargs['files']['snapshot'][1]).items()}
            if name.endswith('__completions'):
                if self.fault == 'no-control-restored':
                    return Response({})
                if self.fault == 'no-completion-records':
                    del restored['c']
                if self.fault == 'no-manifest':
                    del restored['m']
                if self.fault == 'completion-drift':
                    restored['c'][0]['chunk_ids_digest'] = 'tampered'
                if self.fault == 'manifest-drift':
                    restored['m'][0]['state'] = 'pending'
            elif self.fault == 'data-drift':
                restored['d1'][0]['text'] = 'tampered'
            self.collections[name] = restored
            return Response({})
        if url.endswith('/snapshots'):
            return Response({'name': name + '.snapshot'})
        if url.endswith('/points/scroll'):
            assert kwargs['json']['with_vector'] is True and kwargs['json']['with_payload'] is True
            ids = list(points)
            start = ids.index(kwargs['json']['offset']) if 'offset' in kwargs['json'] else 0
            more = ids[start + 2] if start + 2 < len(ids) else None  # small pages exercise pagination
            return Response({'points': [{'id': i, 'payload': points[i][0], 'vector': points[i][1]}
                                        for i in ids[start:start + 2]], 'next_page_offset': more})
        if url.endswith('/points/query'):
            assert kwargs['json']['params']['exact'] is True
            assert kwargs['json']['query'] == [1.0, 0.0]
            ids = list(points)[:5]
            if self.fault == 'query' and name.startswith('ci-restore-'):
                ids.reverse()
            return Response({'points': [{'id': i} for i in ids]})
        assert url.endswith('/points')
        assert kwargs['json']['with_vector'] is True and kwargs['json']['with_payload'] is True
        return Response([{'id': i, 'payload': points[i][0], 'vector': points[i][1]}
                         for i in kwargs['json']['ids'] if i in points])

    def delete(self, url):
        self.calls.append(('delete', url))
        assert '/ci-restore-' in url, 'only scratch collections may be deleted'
        self.collections.pop(self.name(url), None)
        if self.fault == 'cleanup-hits-source':
            self.collections['physical__completions']['c'][0]['chunk_ids_digest'] = 'tampered'
        return Response({})


def run_snapshot_check(monkeypatch, tmp_path, fault):
    qdrant = FakeQdrant(fault)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['headers'] == {'api-key': 'test-only'}

        def __enter__(self):
            return qdrant

        def __exit__(self, *args):
            pass

    settings = SimpleNamespace(qdrant_url='http://qdrant', qdrant_collection='synthetic', qdrant_api_key='test-only')
    monkeypatch.setitem(sys.modules, 'httpx2', SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, 'mainframe_rag.config', SimpleNamespace(load_settings=lambda: settings))
    script = runpy.run_path(str(ROOT / 'scripts/ci/check_snapshot.py'))['SNAPSHOT_CHECK']
    executable = tmp_path / 'snapshot_check.py'
    executable.write_text(script)
    return qdrant, executable


def test_snapshot_check_proves_data_completions_and_representation_restore(monkeypatch, tmp_path, capsys):
    qdrant, executable = run_snapshot_check(monkeypatch, tmp_path, '')
    before = copy.deepcopy(qdrant.collections)
    runpy.run_path(str(executable))
    out = capsys.readouterr().out
    assert 'data-only restore refused' in out and 'next query and snapshot succeeded' in out
    assert 'synthetic 0' not in out  # stored text is never printed
    # Source pair and aliases untouched; every scratch collection removed again.
    assert qdrant.collections == before and qdrant.aliases['synthetic'] == 'physical'
    scratch = sorted(qdrant.name(url) for method, url in qdrant.calls if method == 'post' and '/snapshots/upload' in url)
    assert len(scratch) == 2 and scratch[1] == scratch[0] + '__completions' and scratch[0].startswith('ci-restore-')
    assert sorted(qdrant.name(url) for method, url in qdrant.calls if method == 'delete') == scratch
    # The source pair is snapshotted for the restore, and once more after cleanup.
    snapshots = [qdrant.name(url) for method, url in qdrant.calls if method == 'post' and url.endswith('/snapshots')]
    assert snapshots == ['physical', 'physical__completions', 'physical']
    assert not any('/aliases' in url for method, url in qdrant.calls if method != 'get')


@pytest.mark.parametrize('fault, reason', [
    ('count', 'restored point count differs'),
    ('query', 'restored exact query differs'),
    ('data-drift', 'restored sampled payload differs'),
    # Counterexample: the points come back but not the matching completions state.
    ('no-control-restored', 'restored completions collection missing'),
    ('no-completion-records', 'restored completions or representation points differ'),
    ('no-manifest', 'restored completions or representation points differ'),
    ('completion-drift', 'restored completion or representation payload differs'),
    ('manifest-drift', 'restored completion or representation payload differs'),
    ('cleanup-hits-source', 'source completions or representation changed'),
    ('uncommitted-source', 'source representation manifest is not committed'),
])
def test_snapshot_check_refuses_incomplete_or_damaging_restore(monkeypatch, tmp_path, fault, reason):
    qdrant, executable = run_snapshot_check(monkeypatch, tmp_path, fault)
    with pytest.raises(AssertionError) as failure:
        runpy.run_path(str(executable))
    assert str(failure.value) == reason
    # Even a refused restore removes its scratch collections and only ever writes to them.
    assert not any(name.startswith('ci-restore-') for name in qdrant.collections)
    assert all('/ci-restore-' in url for method, url in qdrant.calls if method in ('delete',) or
               (method == 'post' and '/snapshots/upload' in url))
    if fault == 'uncommitted-source':
        assert not any('/snapshots' in url for method, url in qdrant.calls if method == 'post')


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


def test_snapshot_job_inherits_read_only_security_with_its_own_temporary_storage():
    """Recovery must not lose /tmp or reattach ingest's corpus/work PVCs."""
    from tests.helpers_helm import run_new_template

    source = run_new_template({"ingest": {"enabled": True, "corpusPVC": "manuals"}})["Job", "ingest"]
    before = copy.deepcopy(source)
    job = runpy.run_path(str(ROOT / 'scripts/ci/check_snapshot.py'))['maintenance_job'](source, 'recovery')
    pod = job['spec']['template']['spec']
    container = pod['containers'][0]
    assert pod['securityContext'] == {
        'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'},
    }
    assert container['securityContext'] == {
        'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
        'capabilities': {'drop': ['ALL']},
    }
    assert pod['volumes'] == [{'name': 'tmp', 'emptyDir': {}}]
    assert container['volumeMounts'] == [{'name': 'tmp', 'mountPath': '/tmp'}]
    assert source == before


@pytest.mark.parametrize('failure', ['', 'create', 'logs', 'get'])
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
    assert spec['volumes'] == [{'name': 'tmp', 'emptyDir': {}}]
    assert container['volumeMounts'] == [{'name': 'tmp', 'mountPath': '/tmp'}]
    assert 'args' not in container
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


@pytest.mark.parametrize('key_kind', ['read-only', 'literal'])
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


@pytest.mark.parametrize('args', [['test', '--skip'], ['BAD']])
def test_snapshot_helper_refuses_bad_arguments_before_cluster_calls(args):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/check_snapshot.py'), *args],
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 2
