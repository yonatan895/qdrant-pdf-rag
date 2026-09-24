"""Optional CA wiring is shared, targeted, and fails before deployment on bad inputs."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / 'scripts/airgap/common.sh'


def test_ca_mount_targets_only_application_containers():
    from tests.helpers_helm import FAKE_CA, run_new_template

    docs = run_new_template({
        'gateway': {'caConfigMapName': 'test-gateway-ca'},
        'route': {'enabled': True, 'destinationCA': FAKE_CA},
        'ingest': {'enabled': True, 'corpusPVC': 'corpus'},
    })
    for kind, name, container in [('Deployment', 'rag-agent', 'agent'), ('Job', 'ingest', 'ingest')]:
        pod = docs[(kind, name)]['spec']['template']['spec']
        app = next(c for c in pod['containers'] if c['name'] == container)
        assert {'name': 'SSL_CERT_FILE', 'value': '/etc/gateway-ca/ca-bundle.crt'} in app['env']
        assert {'name': 'gateway-ca', 'mountPath': '/etc/gateway-ca', 'readOnly': True} in app['volumeMounts']
        volume = next(v for v in pod['volumes'] if v['name'] == 'gateway-ca')
        assert volume['configMap'] == {'name': 'test-gateway-ca', 'items': [{'key': 'ca-bundle.crt', 'path': 'ca-bundle.crt'}]}
        for other in pod['containers']:
            if other['name'] != container:
                assert not any(e['name'] == 'SSL_CERT_FILE' for e in other.get('env', []))
                assert not any(v['name'] == 'gateway-ca' for v in other.get('volumeMounts', []))
    jaeger = docs[('Deployment', 'jaeger')]['spec']['template']['spec']
    assert not any(v['name'] == 'gateway-ca' for v in jaeger['volumes'])


@pytest.mark.parametrize('name', ['', 'bad/name', 'BadName', 'x|y'])
def test_ca_absent_or_invalid_render(name):
    from tests.helpers_helm import _helm_template_with_values, base_values

    values = base_values()
    values['gateway']['caConfigMapName'] = name
    result = _helm_template_with_values(values)
    assert (result.returncode == 0) == (name == '')
    if not name:
        assert 'SSL_CERT_FILE' not in result.stdout
        assert 'mountPath: /etc/gateway-ca' not in result.stdout


@pytest.mark.parametrize('state', ['present', 'missing', 'empty'])
def test_ca_preflight_requires_configmap_and_key(tmp_path, state):
    kc = tmp_path / 'kc'
    kc.write_text('#!/bin/sh\n' + ('exit 1\n' if state == 'missing' else 'echo bundle\n' if state == 'present' else 'exit 0\n'))
    kc.chmod(0o755)
    # Issue #478: an empty regular file (not /dev/null) keeps the
    # environment-only shape while ignoring repo ./airgap.env.
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    result = subprocess.run(['sh', '-c', '. "$COMMON"; check_gateway_ca'],
        capture_output=True, text=True, check=False,
        env={**os.environ, 'AIRGAP_ENV': str(empty_env), 'COMMON': str(COMMON), 'KC': str(kc),
             'NAMESPACE': 'test', 'AIRGAP_DRYRUN': '0', 'GATEWAY_CA_CONFIGMAP': 'gateway-ca'})
    assert (result.returncode == 0) == (state == 'present')


def test_operator_ca_environment_beats_file(tmp_path):
    config = tmp_path / 'airgap.env'
    config.write_text('GATEWAY_CA_CONFIGMAP=stale\n')
    result = subprocess.run(['sh', '-c', '. "$COMMON"; echo "$GATEWAY_CA_CONFIGMAP"'],
        capture_output=True, text=True, check=False,
        env={**os.environ, 'AIRGAP_ENV': str(config), 'COMMON': str(COMMON), 'GATEWAY_CA_CONFIGMAP': 'explicit'})
    assert result.returncode == 0
    assert result.stdout.strip() == 'explicit'
