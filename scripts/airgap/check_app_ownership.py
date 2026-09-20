#!/usr/bin/env python3
"""Refuse to take application resources from another Helm release.

Input is kubectl get -f <rendered app> --ignore-not-found -o json, never
Secrets. Successful --ignore-not-found reads can produce empty stdout when
no objects exist; the caller must reject a failed kubectl command first.
The deployer must serialize app operations in its owned namespace.
Only the fixed first-party inventory can be adopted from the old renderer.
"""
import json
import sys

ALLOWED = {
    ('apps/v1', 'Deployment', 'rag-agent'),
    ('v1', 'Service', 'rag-agent'),
    ('v1', 'ServiceAccount', 'rag-agent'),
    ('route.openshift.io/v1', 'Route', 'rag-agent'),
    ('monitoring.coreos.com/v1', 'ServiceMonitor', 'rag-agent'),
    ('apps/v1', 'Deployment', 'jaeger'),
    ('v1', 'Service', 'jaeger'),
    ('v1', 'ConfigMap', 'jaeger-config'),
    ('v1', 'PersistentVolumeClaim', 'jaeger-badger'),
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
            raise ValueError('unexpected application resource inventory')
        if disabled and (item.get('kind'), meta.get('name')) not in {
            ('Deployment', 'jaeger'), ('Service', 'jaeger'),
            ('ConfigMap', 'jaeger-config'), ('ServiceAccount', 'rag-agent'),
            ('Route', 'rag-agent'), ('ServiceMonitor', 'rag-agent'),
        }:
            raise ValueError('unexpected disabled application resource')
        annotations = meta.get('annotations', {})
        labels = meta.get('labels', {})
        if annotations.get('meta.helm.sh/release-name', 'mainframe-rag') != 'mainframe-rag':
            raise ValueError('application resource belongs to another Helm release')
        if annotations.get('meta.helm.sh/release-namespace', namespace) != namespace:
            raise ValueError('application resource belongs to another Helm namespace')
        if labels.get('app.kubernetes.io/managed-by', 'Helm') != 'Helm':
            raise ValueError('application resource has another deployment manager')
        if meta.get('ownerReferences'):
            raise ValueError('application resource has a controller owner')


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
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        sys.exit('FAIL: application ownership preflight refused; inspect resource ownership before retrying')
