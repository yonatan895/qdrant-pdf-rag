#!/usr/bin/env python3
"""Fail-closed Route exposure checks for the optional console (issue #373).

  inventory NAMESPACE enabled|disabled SERVICE...   (stdin: Route list JSON)
      Pre-mutation. Refuses when any Route other than the chart-owned
      `rag-agent` Route backs a protected Service (the unauthenticated agent
      HTTP port or Qdrant), or when the owned Route carries backends the
      release will not own. Prints `owned-route-present` when `rag-agent`
      exists so the caller can retire it before mutation when the console
      Route is disabled.
  verify NAMESPACE CA_FILE                           (stdin: one Route JSON)
      After the release. The live Route must be the OAuth contract: Service
      rag-agent, targetPort oauth, reencrypt, HTTP redirected, destination CA
      equal to the generated bundle, no alternate backends.

Input is `kubectl get ... -o json`; it can carry TLS material for custom host
certificates, so only fixed fields are read and no value is ever echoed except
a validated Kubernetes name. The caller must reject a failed kubectl command
first (empty or malformed input is a refusal, not an empty inventory).
"""
import json
import re
import sys
from pathlib import Path

OWNED = 'rag-agent'
ROUTE_API = 'route.openshift.io/v1'
NAME = re.compile(r'^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$')


def _fail(message):
    raise ValueError(message)


def _label(name):
    return name if isinstance(name, str) and NAME.match(name) else '(invalid name)'


def _route_items(document, namespace):
    if not isinstance(document, dict):
        _fail('invalid Route inventory')
    items = document.get('items') if document.get('kind') == 'List' else [document]
    if not isinstance(items, list):
        _fail('invalid Route inventory')
    for item in items:
        if not isinstance(item, dict) or item.get('kind') != 'Route' or item.get('apiVersion') != ROUTE_API:
            _fail('unexpected Route inventory')
        meta = item.get('metadata')
        if not isinstance(meta, dict) or meta.get('namespace') != namespace or not isinstance(item.get('spec'), dict):
            _fail('unexpected Route inventory')
    return items


def _backends(spec):
    """Service names the Route sends traffic to, primary and alternates."""
    names = []
    to = spec.get('to')
    if not isinstance(to, dict):
        _fail('Route has no backend')
    if to.get('kind', 'Service') == 'Service':
        names.append(to.get('name'))
    alternates = spec.get('alternateBackends') or []
    if not isinstance(alternates, list):
        _fail('invalid Route alternateBackends')
    for alt in alternates:
        if not isinstance(alt, dict):
            _fail('invalid Route alternateBackends')
        if alt.get('kind', 'Service') == 'Service':
            names.append(alt.get('name'))
    return names


def inventory(document, namespace, mode, protected):
    owned_present = False
    for item in _route_items(document, namespace):
        name = item['metadata'].get('name')
        spec = item['spec']
        if name == OWNED:
            owned_present = True
            if mode == 'enabled' and spec.get('alternateBackends'):
                _fail('Route rag-agent has alternateBackends the release does not own; '
                      'delete the Route (oc delete route rag-agent) and re-run so it is recreated')
            continue
        exposed = sorted(set(_backends(spec)) & protected)
        if exposed:
            _fail(f'Route {_label(name)} exposes a protected Service outside the OAuth-protected '
                  f'{OWNED} Route; remove or retarget it (never expose the agent HTTP port or Qdrant directly)')
    return owned_present


def verify(document, namespace, ca_text):
    items = _route_items(document, namespace)
    if len(items) != 1 or items[0]['metadata'].get('name') != OWNED:
        _fail('Route rag-agent is missing or not unique')
    spec = items[0]['spec']
    to = spec.get('to') or {}
    problems = []
    if to.get('kind', 'Service') != 'Service' or to.get('name') != OWNED:
        problems.append('to')
    if spec.get('alternateBackends'):
        problems.append('alternateBackends')
    if (spec.get('port') or {}).get('targetPort') != 'oauth':
        problems.append('port.targetPort')
    tls = spec.get('tls') or {}
    if tls.get('termination') != 'reencrypt':
        problems.append('tls.termination')
    if tls.get('insecureEdgeTerminationPolicy') != 'Redirect':
        problems.append('tls.insecureEdgeTerminationPolicy')
    live_ca = tls.get('destinationCACertificate')
    if not ca_text.strip() or not isinstance(live_ca, str) or live_ca.strip() != ca_text.strip():
        problems.append('tls.destinationCACertificate')
    if problems:
        _fail('live Route rag-agent differs from the OAuth reencrypt contract in: ' + ', '.join(problems))


def main(argv):
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _fail('empty Route inventory')
        document = json.loads(raw)
        command = argv[1] if len(argv) > 1 else ''
        if command == 'inventory' and len(argv) >= 4 and argv[3] in ('enabled', 'disabled'):
            protected = {OWNED, *argv[4:]}
            if inventory(document, argv[2], argv[3], protected):
                print('owned-route-present')
        elif command == 'verify' and len(argv) == 4:
            verify(document, argv[2], Path(argv[3]).read_text(encoding='utf-8'))
        else:
            _fail('usage')
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        text = str(exc) if isinstance(exc, ValueError) else 'invalid Route data'
        sys.exit(f'FAIL: Route exposure preflight refused: {text}')


if __name__ == '__main__':
    main(sys.argv)
