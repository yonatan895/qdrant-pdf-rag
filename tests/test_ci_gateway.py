"""CI gateway wiring: explicit model legs, authenticated routing, failure propagation."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_kind_uses_real_gateway_for_both_legs():
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    steps = workflow['jobs']['kind-live-rehearsal']['steps']
    env_step = next(s['run'] for s in steps if s.get('name', '').startswith('Operator airgap.env'))
    assert 'VLLM_BASE_URL=http://test-gateway:4000' in env_step
    assert 'EMBED_BASE_URL=http://test-gateway:4000/v1' in env_step
    assert 'LLM_BASE_URL=http://test-gateway:4000/v1' in env_step
    assert 'LLM_MODEL_REASONING=mock-reasoning' in env_step
    assert 'GATEWAY_API_KEY_SECRET=test-gateway-keys' in env_step
    assert 'DENSE_DIM=1024' in env_step
    assert 'RERANK_ENABLED=false' in env_step
    gateway_index = next(i for i, s in enumerate(steps) if 'deploy_test_gateway.sh' in s.get('run', ''))
    pipeline_index = next(i for i, s in enumerate(steps) if s.get('name', '').startswith('airgap-pipeline LIVE'))
    assert gateway_index < pipeline_index
    assert any('--require-reasoning --stream' in s.get('run', '') for s in steps)


def test_gateway_fixture_keeps_local_pins_and_real_routing():
    docs = list(yaml.safe_load_all((ROOT / 'scripts/ci/test-gateway.yaml').read_text()))
    launcher = (ROOT / 'scripts/run_local_gateway.sh').read_text()
    for doc in docs:
        if doc['kind'] == 'Deployment':
            image = doc['spec']['template']['spec']['containers'][0]['image']
            assert image in launcher
            assert '@sha256:' in image
    config = yaml.safe_load(docs[0]['data']['config.yaml'])
    assert {m['model_name'] for m in config['model_list']} == {'mock-reasoning', 'mock-embed'}
    assert all(m['litellm_params']['api_base'] == 'http://vllm-mock:8000/v1' for m in config['model_list'])
    assert config['router_settings']['num_retries'] == 0


@pytest.mark.parametrize('fail_at', ['', 'rollout', 'exec'])
def test_gateway_setup_reaches_keys_only_after_readiness(tmp_path, fail_at):
    log = tmp_path / 'calls.jsonl'
    stub = tmp_path / 'kubectl'
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALL_LOG'], 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ['FAIL_AT'] and os.environ['FAIL_AT'] in sys.argv:
    sys.exit(1)
if 'exec' in sys.argv:
    print(json.dumps({'llm-api-key': 'sk-test-llm', 'embed-api-key': 'sk-test-embed',
                      'context-llm-api-key': 'sk-test-llm', 'rerank-api-key': 'sk-unused'}))
''')
    stub.chmod(0o755)
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/deploy_test_gateway.sh'), 'test-gateway-lane'],
                            env={**os.environ, 'PATH': f'{tmp_path}:' + os.environ['PATH'],
                                 'CALL_LOG': str(log), 'FAIL_AT': fail_at},
                            capture_output=True, text=True, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert (result.returncode == 0) == (not fail_at)
    key_creation = [c for c in calls if 'test-gateway-keys' in c]
    assert bool(key_creation) == (not fail_at)
    assert 'sk-test' not in result.stdout + result.stderr


def test_gateway_setup_refuses_unknown_arguments():
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/deploy_test_gateway.sh'), 'test', '--skip'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2


def test_rehearsal_lanes_share_published_bundle_and_bound_jobs():
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    jobs = workflow['jobs']
    kind = jobs['kind-live-rehearsal']
    assert kind['strategy']['matrix']['lane'] == ['pipeline', 'gateway-faults', 'lifecycle']
    assert kind['strategy']['fail-fast'] is False
    lab = jobs['airgap-rehearsal']
    assert 'airgap-package' in lab['needs']
    assert any('download-artifact@' in s.get('uses', '') for s in lab['steps'])
    assert not any('make airgap-pack' in s.get('run', '') for s in lab['steps'])
    for job in jobs.values():
        assert job['permissions'] and job['timeout-minutes'] > 0
        assert 'github.run_id' in job['concurrency']['group']
        assert job['env']['SHARE'] == 'false'


@pytest.mark.parametrize('script', ['gateway_faults.sh', 'check_lifecycle.sh'])
def test_acceptance_scripts_refuse_missing_namespace(script):
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci' / script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2


@pytest.mark.parametrize('bad_probe', ['false-success', 'unrelated-failure', 'expected-failure'])
def test_fault_lane_requires_contract_failure_and_recovery(tmp_path, bad_probe):
    log = tmp_path / 'calls'
    kc = tmp_path / 'kubectl'
    kc.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
root=Path(os.environ['FAKE_STATE'])
with (root/'calls').open('a') as out: out.write(' '.join(sys.argv[1:])+'\\n')
if 'set' in sys.argv:
    values=[x for x in sys.argv if x.startswith('MOCK_')]
    (root/'fault').write_text(' '.join(values))
if 'exec' in sys.argv:
    fault=(root/'fault').read_text() if (root/'fault').exists() else ''
    failing=any(x in fault for x in ('upstream','malformed','truncated','dimension','5000')) or 'env' in sys.argv
    if failing and os.environ['PROBE_MODE']=='expected-failure':
        print('GATEWAY PROBE FAILED');sys.exit(1)
    if failing and os.environ['PROBE_MODE']=='unrelated-failure':
        print('pod disappeared');sys.exit(1)
    print('GATEWAY PROBE PASSED')
''')
    kc.chmod(0o755)
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/gateway_faults.sh'), 'test'],
        env={**os.environ, 'PATH': str(tmp_path)+':'+os.environ['PATH'], 'FAKE_STATE': str(tmp_path), 'PROBE_MODE': bad_probe},
        capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == (bad_probe == 'expected-failure'), result.stderr
    lines = log.read_text().splitlines()
    assert any('MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0' in line for line in lines)
    if not result.returncode:
        assert sum('exec deploy/rag-agent' in line for line in lines) >= 17


def test_installer_artifact_corruption_stops_before_installation(tmp_path):
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    installers = [step['run'] for job in workflow['jobs'].values() for step in job['steps']
                  if step.get('name', '').startswith(('Install checksum-pinned', 'Install pinned kubectl', 'Install pinned kind'))]
    assert len(installers) >= 10
    # Exercise the actual shell gates with corrupt downloaded bytes. The curl
    # function avoids the network; sudo records any unsafe attempt to install.
    harness = r"""curl() {
        while [ "$#" -gt 0 ]; do
            if [ "$1" = -o ]; then shift; printf corrupt > "$1"; return 0; fi
            shift
        done
        return 1
    }
    sudo() { touch "$RUNNER_TEMP/install-attempt"; return 0; }
    """
    for index, installer in enumerate(installers):
        directory = tmp_path / str(index)
        directory.mkdir()
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', harness + installer],
            env={**os.environ, 'RUNNER_TEMP': str(directory)}, capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert 'FAILED' in result.stdout + result.stderr
        assert not (directory / 'install-attempt').exists()
