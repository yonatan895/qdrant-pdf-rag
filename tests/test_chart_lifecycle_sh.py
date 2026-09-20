"""Hermetic lifecycle rehearsal: A/A/B/failed-B/rollback/B/redeploy-A.

scripts/ci/chart_lifecycle.sh must prove the migration-required release
sequence with structural guards, not merely helm exit codes. A stub-only
pass while PVC identities change, a StatefulSet appears, smoke fails, or
the fault injection unexpectedly succeeds would each hide a data/serving
regression, so every trip is pinned here with stubbed helm/kubectl.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/ci/chart_lifecycle.sh'

HELM_STUB = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state_file = os.environ['STATE_FILE']
log_file = os.environ['CALL_LOG']
with open(log_file, 'a') as f:
    f.write(json.dumps({'tool': 'helm', 'args': args}) + '\\n')
state = {'rev': 0, 'status': 'deployed', 'values': 'a', 'pvc_calls': 0}
try:
    state.update(json.loads(open(state_file).read()))
except Exception:
    pass
op = args[0] if args else ''
if op == 'lint':
    sys.exit(0)
if op == 'install':
    state.update(rev=1, status='deployed', values='a')
elif op == 'upgrade':
    if any(a.startswith('images.agent.tag=') for a in args):
        if os.environ.get('FAULT_OK') == '1':
            state['rev'] += 1
            state['status'] = 'deployed'
            sys.exit(0)
        state['fault_image'] = not bool(os.environ.get('ADMISSION_FAIL'))
        state['status'] = 'failed'
        with open(state_file, 'w') as f:
            json.dump(state, f)
        print('Error: timed out waiting for the condition', file=sys.stderr)
        sys.exit(1)
    state['rev'] += 1
    state['status'] = 'deployed'
    state['values'] = 'b' if os.environ['VALUES_B'] in args else 'a'
elif op == 'rollback':
    state.update(rev=int(args[2]), status='deployed', values='a')
elif op == 'status':
    print(json.dumps({'version': state['rev'], 'info': {'status': state['status']}}))
elif op == 'get':
    if os.environ.get('READ_FAIL') == 'manifest':
        sys.exit(1)
    print(open(os.environ['MANIFEST_FILE']).read())
else:
    sys.exit(2)
with open(state_file, 'w') as f:
    json.dump(state, f)
'''

KUBECTL_STUB = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log_file = os.environ['CALL_LOG']
payload = sys.stdin.read() if args[-1:] == ['-'] else ''
with open(log_file, 'a') as f:
    f.write(json.dumps({'tool': 'kubectl', 'args': args, 'input': payload}) + '\\n')
state_file = os.environ['STATE_FILE']
state = {'rev': 0, 'status': 'deployed', 'values': 'a', 'pvc_calls': 0}
try:
    state.update(json.loads(open(state_file).read()))
except Exception:
    pass
fail_at = os.environ.get('FAIL_AT', '')
argv = list(args)
ns = ''
while len(argv) >= 2 and argv[0] == '-n':
    ns = argv[1]
    argv = argv[2:]
command = argv[0] if argv else ''
rest = argv[1:]
if command == 'create' and rest[0] == 'namespace':
    if os.environ.get('NS_EXISTS') == '1':
        print(f'namespaces "{rest[1]}" already exists', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if command == 'create' and 'externalname' in rest:
    print('apiVersion: v1\\nkind: Service\\nmetadata:\\n  name: x')
    sys.exit(0)
if command == 'apply':
    sys.exit(0)
if command == 'get' and rest[0] in ('secret', 'configmap'):
    name = rest[1]
    print(f'apiVersion: v1\\nkind: {"Secret" if rest[0]=="secret" else "ConfigMap"}\\n'
          f'metadata:\\n  name: {name}\\n  namespace: {os.environ["DATA_NS"]}\\n'
          f'  uid: some-uid\\n  resourceVersion: "99"\\ndata:\\n  k: dmFs\\n')
    sys.exit(0)
if command == 'get' and rest[0] == 'pvc':
    if os.environ.get('READ_FAIL') == 'pvc':
        sys.exit(1)
    if ns == os.environ['NS']:
        state['chart_pvc_calls'] = state.get('chart_pvc_calls', 0) + 1
        with open(state_file, 'w') as f:
            json.dump(state, f)
        trip_after = int(os.environ.get('PVC_TRIP_AFTER', '999'))
        print('tripped-pvc-uid' if state['chart_pvc_calls'] > trip_after else 'chart-pvc-uid')
    else:
        state['data_pvc_calls'] = state.get('data_pvc_calls', 0) + 1
        with open(state_file, 'w') as f:
            json.dump(state, f)
        trip_after = int(os.environ.get('DATA_TRIP_AFTER', '999'))
        print('tripped-data-uid' if state['data_pvc_calls'] > trip_after else 'data-pvc-uid')
    sys.exit(0)
if command == 'get' and rest[0] == 'sts':
    if os.environ.get('READ_FAIL') == 'sts':
        sys.exit(1)
    if os.environ.get('STS_TRIP') == '1':
        print('qdrant   1/1   1m')
    sys.exit(0)
if command == 'get' and rest[0].startswith('deploy/'):
    if args[-1].endswith('.image}'):
        print('test/agent:' + ('0000000000000000000000000000000000000000'
              if state.get('fault_image') else 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'))
        sys.exit(0)
    if 'OTEL_DEPLOYMENT_ENVIRONMENT' in ' '.join(args):
        if os.environ.get('MARK_MODE') == 'stuck-a':
            print('')
        elif state['values'] == 'b':
            print(os.environ['B_MARKER'])
        else:
            print('')
    sys.exit(0)
if command == 'rollout':
    sys.exit(1 if fail_at == 'rollout' else 0)
if command == 'wait':
    if '--for=condition=Available' in args and 'deploy/rag-agent' in args:
        sys.exit(1 if fail_at == 'rollout' else 0)
    sys.exit(2)
if command == 'exec':
    sys.exit(1 if fail_at == 'smoke' else 0)
if command == 'delete':
    sys.exit(0)
sys.exit(2)
'''

MANIFEST_CLEAN = '''apiVersion: apps/v1
kind: Deployment
metadata:
  name: rag-agent
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: jaeger-badger
'''

MANIFEST_STS = MANIFEST_CLEAN + '''---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: sneakysts
'''


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'helm').write_text(HELM_STUB)
    (bindir / 'kubectl').write_text(KUBECTL_STUB)
    (bindir / 'helm').chmod(0o755)
    (bindir / 'kubectl').chmod(0o755)
    chart = tmp_path / 'chart'
    chart.mkdir()
    (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: mainframe-rag\nversion: 0.1.0\n')
    values_a = tmp_path / 'values-a.yaml'
    values_a.write_text('replicaCount: 1\n')
    values_b = tmp_path / 'values-b.yaml'
    values_b.write_text('replicaCount: 1\ntracing:\n  deploymentEnvironment: lifecycle-B\n')
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(MANIFEST_CLEAN)
    state = tmp_path / 'state.json'
    state.write_text('{}')
    log = tmp_path / 'calls.jsonl'
    log.write_text('')
    env = {
        **os.environ,
        'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
        'CALL_LOG': str(log),
        'STATE_FILE': str(state),
        'MANIFEST_FILE': str(manifest),
        'VALUES_B': str(values_b),
        'DATA_NS': 'data-plane',
        'NS': 'chart-ns',
        'B_MARKER': 'lifecycle-B',
    }
    for var in ('FAULT_OK', 'FAIL_AT', 'NS_EXISTS', 'PVC_TRIP_AFTER',
                'DATA_TRIP', 'STS_TRIP', 'MARK_MODE'):
        env.pop(var, None)
    return tmp_path, env, log


def run_harness(harness, *extra_env):
    tmp_path, env, log = harness
    env = {**env, **dict(extra_env)}
    result = subprocess.run(
        ['sh', str(SCRIPT), 'chart-ns', str(tmp_path / 'chart'),
         str(tmp_path / 'values-a.yaml'), str(tmp_path / 'values-b.yaml')],
        env=env, text=True, capture_output=True, check=False, timeout=30)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.read_text() else []
    return result, calls


def helm_calls(calls):
    return [c['args'] for c in calls if c['tool'] == 'helm']


def verb_of(call):
    args = list(call['args'])
    while len(args) >= 2 and args[0] == '-n':
        args = args[2:]
    return args[0] if args else ''


def kubectl_calls(calls, *verbs):
    return [c for c in calls if c['tool'] == 'kubectl' and (not verbs or verb_of(c) in verbs)]


def test_full_sequence_green(harness):
    result, calls = run_harness(harness)
    assert result.returncode == 0, result.stderr
    ops = [a[0] for a in helm_calls(calls)]
    mutating = [o for o in ops if o in ('install', 'upgrade', 'rollback')]
    assert mutating == ['install', 'upgrade', 'upgrade', 'upgrade',
                        'rollback', 'upgrade', 'upgrade']
    assert 'status' in ops  # revision probes between mutations
    # Helm --timeout needs a duration unit (live rehearsal caught bare "300").
    import re as _re
    for a in helm_calls(calls):
        if '--timeout' in a:
            assert _re.fullmatch(r'[0-9]+(s|m|h)', a[a.index('--timeout') + 1]), a
    # Fault injection rides values-a with only the bad-tag --set.
    fault = [a for a in helm_calls(calls) if '--set' in a]
    assert len(fault) == 1
    fault = fault[0]
    assert '--set' in fault and 'images.agent.tag=0000000000000000000000000000000000000000' in fault
    assert str(harness[0] / 'values-a.yaml') in fault
    # Rollback targets the pristine A revision captured at install.
    rollbacks = [a for a in helm_calls(calls) if a[0] == 'rollback']
    assert len(rollbacks) == 1
    rollback = rollbacks[0]
    assert rollback[1] == 'chart-lifecycle' and rollback[2] == '1'
    # Smoke (exec) runs at every stable step and after recovery: 7 total.
    # Six use full rollout gating; the post-fault one gates on availability
    # instead (rollout status would wedge on the failed ReplicaSet).
    smokes = [c for c in kubectl_calls(calls, 'exec') if 'smoke_search.py' in ' '.join(c['args'])]
    assert len(smokes) == 7
    rollouts = [c for c in kubectl_calls(calls, 'rollout')]
    assert len(rollouts) == 6
    waits = [c for c in kubectl_calls(calls, 'wait')]
    assert len(waits) == 1
    assert '--for=condition=Available' in waits[0]['args']
    # Namespace we created is removed on success.
    deletes = kubectl_calls(calls, 'delete')
    assert len(deletes) == 1 and deletes[0]['args'][1] == 'namespace'
    # DATA_NS sees reads only, never mutations.
    for c in calls:
        if c['tool'] == 'kubectl' and '-n' in c['args']:
            args = c['args']
            if args[args.index('-n') + 1] == 'data-plane':
                assert verb_of(c) == 'get', args
    # Secret/ConfigMap copies land in the chart namespace without stale identity.
    applies = [c['input'] for c in kubectl_calls(calls, 'apply')]
    copies = [a for a in applies if 'namespace: chart-ns' in a]
    assert len(copies) >= 3
    assert all('uid:' not in a and 'resourceVersion:' not in a for a in copies)
    assert 'chart lifecycle complete' in result.stdout


def test_fault_unexpected_success_fails(harness):
    result, _ = run_harness(harness, ('FAULT_OK', '1'))
    assert result.returncode != 0
    assert 'fault injection unexpectedly succeeded' in result.stderr


def test_chart_pvc_change_trips_guard(harness):
    result, _ = run_harness(harness, ('PVC_TRIP_AFTER', '1'))
    assert result.returncode != 0
    assert 'chart-namespace PVC identities changed' in result.stderr


def test_data_pvc_change_trips_guard(harness):
    result, _ = run_harness(harness, ('DATA_TRIP_AFTER', '1'))
    assert result.returncode != 0
    assert 'data-plane PVC identities changed' in result.stderr


def test_statefulset_trips_guard(harness):
    result, _ = run_harness(harness, ('STS_TRIP', '1'))
    assert result.returncode != 0
    assert 'StatefulSet' in result.stderr


def test_statefulset_in_manifest_trips_guard(harness):
    tmp_path, _env, _ = harness
    (tmp_path / 'manifest.yaml').write_text(MANIFEST_STS)
    result, _ = run_harness(harness)
    assert result.returncode != 0
    assert 'release manifest owns a StatefulSet' in result.stderr


def test_smoke_failure_blocks(harness):
    result, calls = run_harness(harness, ('FAIL_AT', 'smoke'))
    assert result.returncode != 0
    # Fails at the first smoke, before any upgrade: only install ran.
    mutating = [a[0] for a in helm_calls(calls) if a[0] in ('install', 'upgrade', 'rollback')]
    assert mutating == ['install']


def test_rollout_failure_blocks(harness):
    result, _ = run_harness(harness, ('FAIL_AT', 'rollout'))
    assert result.returncode != 0


def test_b_mark_mismatch_blocks(harness):
    result, _ = run_harness(harness, ('MARK_MODE', 'stuck-a'))
    assert result.returncode != 0
    assert 'serving B expected' in result.stderr


def test_preexisting_namespace_is_kept(harness):
    result, calls = run_harness(harness, ('NS_EXISTS', '1'))
    assert result.returncode == 0, result.stderr
    assert not kubectl_calls(calls, 'delete')


@pytest.mark.parametrize('argv', [[], ['only-ns']])
def test_arg_validation_before_cluster_calls(harness, argv):
    _tmp_path, env, log = harness
    result = subprocess.run(['sh', str(SCRIPT), *argv],
                            env=env, text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 2
    assert log.read_text() == ''


def test_missing_data_ns_fails_closed(harness):
    tmp_path, env, log = harness
    env = {k: v for k, v in env.items() if k != 'DATA_NS'}
    result = subprocess.run(
        ['sh', str(SCRIPT), 'chart-ns', str(tmp_path / 'chart'),
         str(tmp_path / 'values-a.yaml'), str(tmp_path / 'values-b.yaml')],
        env=env, text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode != 0
    assert 'DATA_NS is required' in result.stderr
    assert log.read_text() == ''


@pytest.mark.parametrize('resource', ['pvc', 'sts', 'manifest'])
def test_failed_cluster_reads_block_acceptance(harness, resource):
    result, _ = run_harness(harness, ('READ_FAIL', resource))
    assert result.returncode != 0
    assert 'chart lifecycle complete' not in result.stdout


def test_values_paths_reach_helm_without_word_splitting(harness):
    scale = harness[0] / 'scale values [kind].yaml'
    scale.write_text('replicaCount: 1\n')
    result, calls = run_harness(harness, ('SCALE_VALUES', str(scale)))
    assert result.returncode == 0, result.stderr
    rendered = [a for a in helm_calls(calls) if a[0] in ('lint', 'install', 'upgrade')]
    assert len(rendered) == 7
    for args in rendered:
        assert args[-2:] == ['-f', str(scale)]


def test_published_bundle_entry_uses_resolved_operator_values(tmp_path):
    import shutil

    import yaml

    root = tmp_path / 'verified source'
    for relative in ('scripts/ci', 'scripts/airgap'):
        (root / relative).mkdir(parents=True)
    for relative in ('scripts/ci/rehearse_chart.sh', 'scripts/airgap/common.sh',
                     'scripts/airgap/map_values.py'):
        shutil.copyfile(ROOT / relative, root / relative)
    (root / 'airgap.env').write_text(
        'INTERNAL_REGISTRY=registry.test/team\nNAMESPACE=file-namespace\n'
        'IMAGE_SHA=' + 'a' * 40 + '\n'
        'STORAGE_CLASS=local-path\nEMBED_MODEL=mock-embed\n'
        'EMBED_MODEL_REVISION="revision with spaces | and pipes"\n'
        'DENSE_DIM=1024\nVLLM_BASE_URL=https://test-gateway:4000\n'
        'CORPUS_PVC=corpus\nPULL_SECRET=rehearsal-pull\n'
    )
    (root / 'scripts/ci/chart_lifecycle.sh').write_text(
        '#!/bin/sh\nset -eu\n'
        'test "$DATA_NS" = cli-namespace\n'
        'test "$PULL_SECRET_SRC" = rehearsal-pull\n'
        'test "$B_MARKER" = chart-lifecycle-B\n'
        'test "$1" = cli-namespace-chart\n'
        'test "$2" = charts/mainframe-rag\n'
        'test "$3" = dist/chart-lifecycle-a.yaml\n'
        'test "$4" = dist/chart-lifecycle-b.yaml\n'
        'touch lifecycle-invoked\n'
    )
    result = subprocess.run(
        ['sh', str(root / 'scripts/ci/rehearse_chart.sh')],
        env={'PATH': os.environ['PATH'], 'NAMESPACE': 'cli-namespace'},
        text=True, capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (root / 'lifecycle-invoked').exists()
    a, b = [yaml.safe_load((root / f'dist/chart-lifecycle-{suffix}.yaml').read_text())
            for suffix in ('a', 'b')]
    assert a['ingest']['enabled'] is False
    assert a['models']['embedding']['revision'] == 'revision with spaces | and pipes'
    assert a['pullSecret']['name'] == 'rehearsal-pull'
    assert a['tracing']['deploymentEnvironment'] == 'chart-lifecycle-A'
    assert b['tracing']['deploymentEnvironment'] == 'chart-lifecycle-B'
    a['tracing']['deploymentEnvironment'] = b['tracing']['deploymentEnvironment']
    assert a == b
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    steps = workflow['jobs']['kind-live-rehearsal']['steps']
    step = next(step for step in steps if 'sh scripts/ci/rehearse_chart.sh' in step.get('run', ''))
    assert step['if'] == "matrix.lane == 'lifecycle'"
    assert 'cd gapbox/qdrant-pdf-rag' in step['run']


def test_admission_rejection_is_not_failed_rollout_evidence(harness):
    result, _ = run_harness(harness, ('ADMISSION_FAIL', '1'))
    assert result.returncode != 0
    assert 'fault image was not admitted' in result.stderr
