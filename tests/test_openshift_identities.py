"""OpenShift must select identities; chart defaults must not reintroduce IDs."""
from pathlib import Path

import pytest
import yaml

from tests.helpers_helm import run_new_template

ROOT = Path(__file__).resolve().parents[1]


def test_qdrant_removes_chart_identity_defaults():
    values = yaml.safe_load((ROOT / 'charts' / 'qdrant-openshift.values.yaml').read_text())
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
        (ROOT / 'charts' / 'qdrant-openshift.values.yaml').read_text()
    )
    assert values['replicaCount'] == 3
    assert 'topologySpreadConstraints' not in values
    rules = values['affinity']['podAntiAffinity'][
        'requiredDuringSchedulingIgnoredDuringExecution'
    ]
    assert len(rules) == 1
    assert rules[0]['topologyKey'] == 'kubernetes.io/hostname'
    assert rules[0]['labelSelector']['matchLabels'] == {'app.kubernetes.io/name': 'qdrant'}


@pytest.mark.parametrize('name', ['openobserve', 'otelcol'])
def test_observability_pods_accept_assigned_identity_with_restricted_security(name):
    docs = run_new_template({'metrics': {'enabled': True}})
    pod = docs[('Deployment', name)]['spec']['template']['spec']
    assert 'fsGroup' not in pod['securityContext']
    assert 'runAsUser' not in pod['securityContext'] and 'runAsGroup' not in pod['securityContext']
    assert pod['securityContext']['runAsNonRoot'] is True
    assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    for container in pod['containers']:
        security = container['securityContext']
        assert 'runAsUser' not in security and 'runAsGroup' not in security
        assert security['allowPrivilegeEscalation'] is False
        assert security['readOnlyRootFilesystem'] is True
        assert security['capabilities']['drop'] == ['ALL']



def test_production_qdrant_meets_psa_restricted_without_fixed_ids():
    """#588: vanilla PSA "restricted" needs drop-ALL and RuntimeDefault seccomp
    from the chart itself; restricted-v2 still assigns the UID/GID/fsGroup."""
    import shutil
    import subprocess

    values = yaml.safe_load((ROOT / 'charts' / 'qdrant-openshift.values.yaml').read_text())
    assert values['containerSecurityContext']['capabilities']['drop'] == ['ALL']
    assert values['containerSecurityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    assert values['podSecurityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    if shutil.which('helm') is None:
        pytest.skip('helm not installed')
    chart = next((ROOT / 'charts').glob('qdrant-*.tgz'))
    out = subprocess.run(
        ['helm', 'template', 'q', str(chart), '-f', str(ROOT / 'charts' / 'qdrant-openshift.values.yaml')],
        capture_output=True, text=True, check=True,
    ).stdout
    sts = next(d for d in yaml.safe_load_all(out) if d and d['kind'] == 'StatefulSet')
    pod = sts['spec']['template']['spec']
    sec = pod['containers'][0]['securityContext']
    assert sec['capabilities']['drop'] == ['ALL']
    assert sec['seccompProfile']['type'] == 'RuntimeDefault'
    assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
    for ctx in (sec, pod['securityContext']):
        assert not {'runAsUser', 'runAsGroup', 'fsGroup'} & set(ctx)
