"""In-pod OpenObserve checks for the kind-observability lane (issue #956, ADR-0006).

Runs inside the agent pod (`kubectl exec -i deploy/rag-agent -- python3 - CANARY...`) so
it uses the credential the chart already gave the agent (OTEL_EXPORTER_OTLP_HEADERS);
CI never reads the Secret. Prints counts and states only: never a canary value, a
credential, or stored content. Exit 0 only when every check passed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import uuid

import httpx2

BASE = 'http://openobserve:5080'
AGENT = 'http://localhost:8080'
HEADER = urllib.parse.unquote(os.environ['OTEL_EXPORTER_OTLP_HEADERS']).split('Authorization=', 1)[1]
CANARIES = sys.argv[1:]
assert CANARIES, 'canary strings are required'
client = httpx2.Client(base_url=BASE, headers={'Authorization': HEADER}, timeout=60)
failures: list[str] = []


def search(org: str, kind: str, sql: str, size: int = 10000) -> httpx2.Response:
    now = int(time.time() * 1_000_000)
    return client.post(f'/api/{org}/_search', params={'type': kind}, json={'query': {
        'sql': sql, 'start_time': now - 3600 * 1_000_000, 'end_time': now + 60_000_000,
        'from': 0, 'size': size}})


def wait_for(label: str, probe, timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if probe():
                print(f'{label}: PASS')
                return True
        except (httpx2.HTTPError, ValueError, KeyError):
            pass
        if time.monotonic() >= deadline:
            print(f'{label}: FAIL')
            failures.append(label)
            return False
        time.sleep(3)


# 1. Canary traffic: the strings ride the query, a header and an unknown route.
for canary in CANARIES:
    httpx2.post(AGENT + '/v1/search', json={'query': canary, 'limit': 1},
                headers={'X-Canary-Probe': canary}, timeout=60)
    httpx2.get(AGENT + '/' + canary, timeout=30)

# 2. A normal request with a known trace id must arrive as a v1.search trace.
trace_id = uuid.uuid4().hex
httpx2.post(AGENT + '/v1/search', json={'query': 'IEA500I operator message', 'limit': 3},
            headers={'traceparent': f'00-{trace_id}-{uuid.uuid4().hex[:16]}-01'}, timeout=60)
wait_for('trace_arrival', lambda: search(
    'default', 'traces',
    f"SELECT trace_id FROM \"default\" WHERE trace_id = '{trace_id}' AND operation_name = 'v1.search'",
    1).json().get('hits'))

# 3. Metrics arrive through the collector (Prometheus-compatible query API).
wait_for('metric_arrival', lambda: client.get(
    '/api/default/prometheus/api/v1/query', params={'query': 'rag_requests_total'}
).json().get('data', {}).get('result'))

# 4. Canary scan of every stream in the default and _meta orgs.
time.sleep(10)  # let the batch exporters flush the canary traffic
scanned = hits = unreadable = 0
for org in ('default', '_meta'):
    for kind in ('logs', 'metrics', 'traces'):
        listing = client.get(f'/api/{org}/streams', params={'type': kind})
        if listing.status_code != 200:
            unreadable += 1
            continue
        for stream in listing.json().get('list', []):
            name = stream['name']
            response = search(org, kind, f'SELECT * FROM "{name}"')
            if response.status_code != 200:
                unreadable += 1
                continue
            scanned += 1
            blob = json.dumps(response.json())
            hits += sum(blob.count(canary) for canary in CANARIES)
print(f'canary_scan: streams_scanned={scanned} unreadable={unreadable} hits={hits}')
if not scanned:
    failures.append('canary_scan_empty')
if hits:
    failures.append('canary_hits')
sys.exit(1 if failures else 0)
