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
