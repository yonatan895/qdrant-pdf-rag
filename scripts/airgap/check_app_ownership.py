#!/usr/bin/env python3
"""Refuse to take application resources from another Helm release.

Input is kubectl get -f <rendered app> --ignore-not-found -o json, never
Secrets. Successful --ignore-not-found reads can produce empty stdout when
no objects exist; the caller must reject a failed kubectl command first.
The deployer must serialize app operations in its owned namespace.
Only the fixed first-party inventory can be adopted from the old renderer.
"""
import json
import re
import sys

# Only identities that look like Kubernetes ones are named in a refusal;
# anything else (and every value, annotation or label) stays out of output.
_IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9./-]{0,252}')


class Refused(ValueError):
    """A refusal naming the object it judged: apiVersion, kind, name only."""

    def __init__(self, reason, item):
        meta = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
        parts = [item.get('apiVersion'), item.get('kind'), meta.get('name')]
        shown = [p if isinstance(p, str) and _IDENTITY.fullmatch(p) else '<invalid>' for p in parts]
        super().__init__(f'{reason}: {shown[0]} {shown[1]}/{shown[2]}')

ALLOWED = {
    ('apps/v1', 'Deployment', 'rag-agent'),
    ('v1', 'Service', 'rag-agent'),
    ('v1', 'ServiceAccount', 'rag-agent'),
    ('route.openshift.io/v1', 'Route', 'rag-agent'),
    ('monitoring.coreos.com/v1', 'ServiceMonitor', 'rag-agent'),
    ('networking.k8s.io/v1', 'NetworkPolicy', 'rag-agent-ingress'),
    ('apps/v1', 'Deployment', 'jaeger'),
    ('v1', 'Service', 'jaeger'),
    ('v1', 'ConfigMap', 'jaeger-config'),
    ('v1', 'PersistentVolumeClaim', 'jaeger-badger'),
    # OpenObserve backend and otelcol collector (ADR-0006, #956): the deploy
    # after the one that created them must find them in the live inventory.
    ('apps/v1', 'Deployment', 'openobserve'),
    ('v1', 'Service', 'openobserve'),
    ('v1', 'PersistentVolumeClaim', 'openobserve-data'),
    ('networking.k8s.io/v1', 'NetworkPolicy', 'openobserve-egress-deny'),
    ('apps/v1', 'Deployment', 'otelcol'),
    ('v1', 'ConfigMap', 'otelcol-config'),
    ('networking.k8s.io/v1', 'NetworkPolicy', 'otelcol-egress'),
    # Answer-feedback service (#804): the next ordinary deploy must find its own
    # objects in the live inventory. None is a disabled-cleanup entry: the
    # release prunes the workload, Service, policy and monitor, and the claim
    # (helm.sh/resource-policy: keep) is never deleted by any deploy path.
    ('apps/v1', 'StatefulSet', 'rag-feedback'),
    ('v1', 'Service', 'rag-feedback'),
    ('v1', 'PersistentVolumeClaim', 'rag-feedback-data'),
    ('networking.k8s.io/v1', 'NetworkPolicy', 'rag-feedback-ingress'),
    ('monitoring.coreos.com/v1', 'ServiceMonitor', 'rag-feedback'),
    # Open WebUI docs front end (#1074, ADR-0007): its own objects only; the
    # claim (helm.sh/resource-policy: keep) is never in a cleanup inventory.
    ('apps/v1', 'Deployment', 'rag-openwebui'),
    ('v1', 'Service', 'rag-openwebui'),
    ('v1', 'ConfigMap', 'rag-openwebui-pipe'),
    # Always rendered with Open WebUI, and with OPENWEBUI_IDP_CA_FILE (#1134).
    ('v1', 'ConfigMap', 'rag-openwebui-proxy'),
    ('v1', 'ConfigMap', 'rag-openwebui-idp-ca'),
    ('v1', 'PersistentVolumeClaim', 'rag-openwebui-data'),
    ('networking.k8s.io/v1', 'NetworkPolicy', 'rag-openwebui'),
    ('route.openshift.io/v1', 'Route', 'rag-openwebui'),
}


def check(document, namespace, disabled=False):
    if not isinstance(document, dict):
        raise ValueError('invalid resource inventory')
    items = document.get('items') if document.get('kind') == 'List' else [document]
    if not isinstance(items, list):
        raise ValueError('invalid resource inventory')
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('invalid resource inventory')
        meta = item.get('metadata', {})
        if ((item.get('apiVersion'), item.get('kind'), meta.get('name')) not in ALLOWED
                or meta.get('namespace') != namespace):
            raise Refused('unexpected application resource inventory', item)
        if disabled and (item.get('kind'), meta.get('name')) not in {
            ('Deployment', 'jaeger'), ('Service', 'jaeger'),
            ('ConfigMap', 'jaeger-config'), ('ServiceAccount', 'rag-agent'),
            ('Route', 'rag-agent'), ('ServiceMonitor', 'rag-agent'),
        }:
            raise Refused('unexpected disabled application resource', item)
        annotations = meta.get('annotations', {})
        labels = meta.get('labels', {})
        if annotations.get('meta.helm.sh/release-name', 'mainframe-rag') != 'mainframe-rag':
            raise Refused('application resource belongs to another Helm release', item)
        if annotations.get('meta.helm.sh/release-namespace', namespace) != namespace:
            raise Refused('application resource belongs to another Helm namespace', item)
        if labels.get('app.kubernetes.io/managed-by', 'Helm') != 'Helm':
            raise Refused('application resource has another deployment manager', item)
        if meta.get('ownerReferences'):
            raise Refused('application resource has a controller owner', item)


if __name__ == '__main__':
    try:
        raw = sys.stdin.read()
        document = json.loads(raw) if raw.strip() else {'kind': 'List', 'items': []}
        disabled = sys.argv[2:] == ['--disabled']
        check(document, sys.argv[1], disabled=disabled)
        if disabled:
            # Emit only deletion identities, never PodSpec/configuration data.
            items = document['items'] if document.get('kind') == 'List' else [document]
            if not items:
                sys.exit(0)
            json.dump({'apiVersion': 'v1', 'kind': 'List', 'items': [
                {'apiVersion': item['apiVersion'], 'kind': item['kind'],
                 'metadata': {'name': item['metadata']['name'],
                              'namespace': item['metadata']['namespace']}}
                for item in items
            ]}, sys.stdout)
    except Refused as exc:
        sys.exit(f'FAIL: application ownership preflight refused ({exc}); '
                 'inspect resource ownership before retrying')
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        sys.exit('FAIL: application ownership preflight refused; inspect resource ownership before retrying')
