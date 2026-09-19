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


def test_production_qdrant_requires_hard_worker_separation():
    """Issue #360: required pod anti-affinity across workers. Best-effort
    ScheduleAnyway spreading and a PDB are not proof of independent copies;
    a capacity squeeze must leave a peer Pending, never colocated."""
    values = yaml.safe_load(
        (ROOT / 'overlays' / 'openshift' / 'values.yaml').read_text()
    )
    assert values['replicaCount'] == 3
    assert 'topologySpreadConstraints' not in values
    rules = values['affinity']['podAntiAffinity'][
        'requiredDuringSchedulingIgnoredDuringExecution'
    ]
    assert len(rules) == 1
    assert rules[0]['topologyKey'] == 'kubernetes.io/hostname'
    assert rules[0]['labelSelector']['matchLabels'] == {'app.kubernetes.io/name': 'qdrant'}


def test_jaeger_accepts_assigned_identity_with_restricted_security():
    pod = yaml.safe_load((ROOT / 'deploy/kustomize/jaeger/deployment.yaml').read_text())['spec']['template']['spec']
    assert 'fsGroup' not in pod['securityContext']
    assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    for container in pod['containers']:
        security = container['securityContext']
        assert 'runAsUser' not in security and 'runAsGroup' not in security
        assert security['allowPrivilegeEscalation'] is False
        assert security['capabilities']['drop'] == ['ALL']


def test_kind_supplies_its_own_volume_group_without_an_scc():
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    steps = workflow['jobs']['kind-live-rehearsal']['steps']
    scale = next(s['run'] for s in steps if s.get('name', '').startswith('Write Kind-scale Qdrant'))
    assert 'podSecurityContext: {fsGroup: 3000}' in scale
