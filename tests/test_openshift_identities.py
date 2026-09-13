"""OpenShift must select identities; chart defaults must not reintroduce IDs."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('overlay', ['openshift', 'ci'])
def test_qdrant_removes_chart_identity_defaults(overlay):
    values = yaml.safe_load((ROOT / 'overlays' / overlay / 'values.yaml').read_text())
    container = values['containerSecurityContext']
    # Explicit null is required: omission inherits the vendored chart's IDs.
    assert container['runAsUser'] is None
    assert container['runAsGroup'] is None
    assert values['podSecurityContext']['fsGroup'] is None
    assert container['runAsNonRoot'] is True
    assert container['allowPrivilegeEscalation'] is False
    assert values['image']['useUnprivilegedImage'] is True


def test_jaeger_accepts_assigned_identity_with_restricted_security():
    pod = yaml.safe_load((ROOT / 'deploy/kustomize/jaeger/deployment.yaml').read_text())['spec']['template']['spec']
    assert 'fsGroup' not in pod['securityContext']
    assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    for container in pod['containers']:
        security = container['securityContext']
        assert 'runAsUser' not in security and 'runAsGroup' not in security
        assert security['allowPrivilegeEscalation'] is False
        assert security['capabilities']['drop'] == ['ALL']
