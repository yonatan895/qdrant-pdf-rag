"""Optional CA wiring is shared, targeted, and fails before deployment on bad inputs."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / 'scripts/airgap/common.sh'


@pytest.mark.parametrize('kind,name,container', [('Deployment', 'rag-agent', 'agent'), ('Job', 'ingest', 'ingest')])
def test_ca_patch_targets_only_application(tmp_path, kind, name, container):
    rendered = tmp_path / 'resources.yaml'
    rendered.write_text('original resources\n')
    captured = tmp_path / 'patch.yaml'
    code = '''. "$COMMON"
kustomize_render() { cp "$1/kustomization.yaml" "$CAPTURED"; cat "$1/resources.yaml"; }
wire_gateway_ca "$RENDERED" "$KIND" "$NAME" "$CONTAINER"
'''
    result = subprocess.run(['sh', '-c', code], capture_output=True, text=True, check=False,
        env={**os.environ, 'AIRGAP_ENV': '/dev/null', 'COMMON': str(COMMON),
             'GATEWAY_CA_CONFIGMAP': 'test-gateway-ca', 'CAPTURED': str(captured),
             'RENDERED': str(rendered), 'KIND': kind, 'NAME': name, 'CONTAINER': container})
    assert result.returncode == 0, result.stderr
    patch_entry = yaml.safe_load(captured.read_text())['patches'][0]
    assert patch_entry['target'] == {'kind': kind, 'name': name}
    patch = yaml.safe_load(patch_entry['patch'])
    assert patch['apiVersion'] == ('apps/v1' if kind == 'Deployment' else 'batch/v1')
    pod = patch['spec']['template']['spec']
    assert [c['name'] for c in pod['containers']] == [container]
    assert pod['containers'][0]['env'] == [{'name': 'SSL_CERT_FILE', 'value': '/etc/gateway-ca/ca-bundle.crt'}]
    assert pod['containers'][0]['volumeMounts'][0]['readOnly'] is True
    assert pod['volumes'][0]['configMap'] == {'name': 'test-gateway-ca', 'items': [{'key': 'ca-bundle.crt', 'path': 'ca-bundle.crt'}]}
    assert rendered.read_text() == 'original resources\n'


@pytest.mark.parametrize('name', ['', 'bad/name', 'BadName', 'x|y'])
def test_ca_unset_or_bad_name_never_renders(tmp_path, name):
    rendered = tmp_path / 'resources.yaml'
    rendered.write_text('original\n')
    result = subprocess.run(['sh', '-c', ('. "$COMMON"; kustomize_render() { exit 99; }; '
                             'wire_gateway_ca "$RENDERED" Deployment rag-agent agent')],
        capture_output=True, text=True, check=False,
        env={**os.environ, 'AIRGAP_ENV': '/dev/null', 'COMMON': str(COMMON),
             'GATEWAY_CA_CONFIGMAP': name, 'RENDERED': str(rendered)})
    assert (result.returncode == 0) == (name == '')
    assert result.returncode != 99
    assert rendered.read_text() == 'original\n'


@pytest.mark.parametrize('state', ['present', 'missing', 'empty'])
def test_ca_preflight_requires_configmap_and_key(tmp_path, state):
    kc = tmp_path / 'kc'
    kc.write_text('#!/bin/sh\n' + ('exit 1\n' if state == 'missing' else 'echo bundle\n' if state == 'present' else 'exit 0\n'))
    kc.chmod(0o755)
    result = subprocess.run(['sh', '-c', '. "$COMMON"; check_gateway_ca'],
        capture_output=True, text=True, check=False,
        env={**os.environ, 'AIRGAP_ENV': '/dev/null', 'COMMON': str(COMMON), 'KC': str(kc),
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
