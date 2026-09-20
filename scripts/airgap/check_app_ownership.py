#!/usr/bin/env python3
"""Refuse to take application resources from another Helm release.

Input is kubectl get -f <rendered app> --ignore-not-found -o json, never
Secrets. The deployer must serialize app operations in its owned namespace.
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


def check(document, namespace):
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
        check(json.load(sys.stdin), sys.argv[1])
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        sys.exit('FAIL: application ownership preflight refused; inspect resource ownership before retrying')
