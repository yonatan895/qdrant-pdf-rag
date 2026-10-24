#!/usr/bin/env python3
"""Fail-closed Route exposure checks for the optional console (issue #373)
and the optional Open WebUI front end (#1074, ADR-0007 R3).

  inventory NAMESPACE enabled|disabled SERVICE...   (stdin: Route list JSON)
      Pre-mutation. Refuses when any Route other than the chart-owned
      `rag-agent` Route backs a protected Service (the unauthenticated agent
      HTTP port or Qdrant), or when the owned Route carries backends the
      release will not own. Prints `owned-route-present` when `rag-agent`
      exists so the caller can retire it before mutation when disabled.
  classify NAMESPACE CA_FILE SERVICE...              (stdin: Route list JSON)
      Deployment preflight classifies the existing owned Route against the
      OAuth contract. Prints `owned-route-incompatible` for an owned Route
      to retire before release work, or `owned-route-present` to retain it.
  verify NAMESPACE CA_FILE                           (stdin: one Route JSON)
      After the release. The live Route must be the OAuth contract: Service
      rag-agent, targetPort oauth, reencrypt, HTTP redirected, destination CA
      equal to the generated bundle, no alternate backends.
  verify-openwebui NAMESPACE CA_FILE HOST            (stdin: one Route JSON)
      After the release. The live `rag-openwebui` Route must be its contract:
      Service rag-openwebui, targetPort https, reencrypt, HTTP redirected,
      the configured host, destination CA equal to the generated bundle, no
      alternate backends.

`--openwebui=enabled|disabled[:CA_FILE:HOST]`, given before the subcommand's
arguments of inventory/classify, adds the front end: `rag-openwebui` is then
always a protected Service (only its own owned Route may back it, whatever
either selection), and the owned `rag-openwebui` Route prints
`openwebui-route-present`, or `openwebui-route-incompatible` when enabled
with a CA and host and the live Route differs from its contract.

Input is `kubectl get ... -o json`; it can carry TLS material for custom host
certificates, so only fixed fields are read and no value is ever echoed except
a validated Kubernetes name. The caller must reject a failed kubectl command
first (empty or malformed input is a refusal, not an empty inventory).
"""
import json
import re
import sys
from pathlib import Path

from check_app_ownership import check as check_ownership

OWNED = 'rag-agent'
OWNED_OPENWEBUI = 'rag-openwebui'
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


def inventory(document, namespace, mode, protected, ca_text=None, openwebui=None):
    """`openwebui` is None (front end not considered) or (mode, ca_text, host)."""
    owned_state = ''
    owui_state = ''
    if openwebui is not None:
        protected = {*protected, OWNED_OPENWEBUI}
    for item in _route_items(document, namespace):
        name = item['metadata'].get('name')
        spec = item['spec']
        if openwebui is not None and name == OWNED_OPENWEBUI:
            check_ownership(item, namespace)
            owui_mode, owui_ca, owui_host = openwebui
            owui_state = 'openwebui-route-present'
            if owui_mode == 'enabled' and spec.get('alternateBackends'):
                _fail('Route rag-openwebui has alternateBackends the release does not own; '
                      'delete the Route (oc delete route rag-openwebui) and re-run so it is recreated')
            stray = sorted(set(_backends(spec)) & (protected - {OWNED_OPENWEBUI}))
            if stray:
                _fail('Route rag-openwebui exposes a protected Service other than rag-openwebui; '
                      'delete it (oc delete route rag-openwebui) and re-run')
            if owui_mode == 'enabled' and owui_ca is not None:
                try:
                    verify_openwebui(item, namespace, owui_ca, owui_host)
                except ValueError:
                    owui_state = 'openwebui-route-incompatible'
            continue
        if name == OWNED:
            check_ownership(item, namespace)
            owned_state = 'owned-route-present'
            if mode == 'enabled' and spec.get('alternateBackends'):
                _fail('Route rag-agent has alternateBackends the release does not own; '
                      'delete the Route (oc delete route rag-agent) and re-run so it is recreated')
            if ca_text is not None:
                try:
                    verify(item, namespace, ca_text)
                except ValueError:
                    owned_state = 'owned-route-incompatible'
            continue
        exposed = sorted(set(_backends(spec)) & protected)
        if exposed:
            _fail(f'Route {_label(name)} exposes a protected Service outside the OAuth-protected '
                  f'{OWNED} Route; remove or retarget it (never expose the agent HTTP port or Qdrant directly)')
    return '\n'.join(state for state in (owned_state, owui_state) if state)


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


def verify_openwebui(document, namespace, ca_text, host):
    items = _route_items(document, namespace)
    if len(items) != 1 or items[0]['metadata'].get('name') != OWNED_OPENWEBUI:
        _fail('Route rag-openwebui is missing or not unique')
    spec = items[0]['spec']
    to = spec.get('to') or {}
    problems = []
    if to.get('kind', 'Service') != 'Service' or to.get('name') != OWNED_OPENWEBUI:
        problems.append('to')
    if spec.get('alternateBackends'):
        problems.append('alternateBackends')
    if (spec.get('port') or {}).get('targetPort') != 'https':
        problems.append('port.targetPort')
    if not host or spec.get('host') != host:
        problems.append('host')
    tls = spec.get('tls') or {}
    if tls.get('termination') != 'reencrypt':
        problems.append('tls.termination')
    if tls.get('insecureEdgeTerminationPolicy') != 'Redirect':
        problems.append('tls.insecureEdgeTerminationPolicy')
    live_ca = tls.get('destinationCACertificate')
    if not ca_text.strip() or not isinstance(live_ca, str) or live_ca.strip() != ca_text.strip():
        problems.append('tls.destinationCACertificate')
    if problems:
        _fail('live Route rag-openwebui differs from the reencrypt contract in: ' + ', '.join(problems))


def _openwebui_option(argv):
    """Strip a leading --openwebui=... option; return (argv, option or None)."""
    if len(argv) > 1 and argv[1].startswith('--openwebui='):
        mode, _, rest = argv[1][len('--openwebui='):].partition(':')
        if mode not in ('enabled', 'disabled'):
            _fail('usage')
        ca_text, host = None, ''
        if rest:
            ca_file, sep, host = rest.rpartition(':')
            if not sep or not ca_file or mode != 'enabled':
                _fail('usage')
            ca_text = Path(ca_file).read_text(encoding='utf-8')
        return [argv[0], *argv[2:]], (mode, ca_text, host)
    return argv, None


def main(argv):
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _fail('empty Route inventory')
        document = json.loads(raw)
        argv, openwebui = _openwebui_option(argv)
        command = argv[1] if len(argv) > 1 else ''
        if command == 'inventory' and len(argv) >= 4 and argv[3] in ('enabled', 'disabled'):
            protected = {OWNED, *argv[4:]}
            state = inventory(document, argv[2], argv[3], protected, openwebui=openwebui)
            if state:
                print(state)
        elif command == 'classify' and len(argv) >= 4:
            state = inventory(document, argv[2], 'enabled', {OWNED, *argv[4:]},
                              Path(argv[3]).read_text(encoding='utf-8'), openwebui=openwebui)
            if state:
                print(state)
        elif command == 'verify' and len(argv) == 4 and openwebui is None:
            verify(document, argv[2], Path(argv[3]).read_text(encoding='utf-8'))
        elif command == 'verify-openwebui' and len(argv) == 5 and openwebui is None:
            verify_openwebui(document, argv[2], Path(argv[3]).read_text(encoding='utf-8'), argv[4])
        else:
            _fail('usage')
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        text = str(exc) if isinstance(exc, ValueError) else 'invalid Route data'
        sys.exit(f'FAIL: Route exposure preflight refused: {text}')


if __name__ == '__main__':
    main(sys.argv)
