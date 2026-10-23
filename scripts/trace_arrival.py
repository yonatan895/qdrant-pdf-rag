#!/usr/bin/env python3
"""Trace-arrival check against OpenObserve (issue #956).

Answers "did a trace for request X (or service S since time T) arrive?" through
OpenObserve's search API. Messages are fixed; credentials come from the
environment or stdin and are never printed. Stdlib plus httpx2 only: the module
also runs from /app/scripts inside the agent image.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx2

OPERATION = 'v1.search'
SERVICE = 'mainframe-rag-agent'
_TRACE_ID = re.compile(r'[0-9a-f]{32}')
_NAME = re.compile(r'[A-Za-z0-9._-]{1,128}')


class TraceAuthError(ValueError):
    """The trace backend refused the credentials."""


class TraceBackendError(RuntimeError):
    """The trace backend answered with something other than a search result."""


def credentials_from_env(env=None) -> tuple[str, str] | None:
    env = os.environ if env is None else env
    user, password = env.get('TRACE_QUERY_USER'), env.get('TRACE_QUERY_PASSWORD')
    return (user, password) if user and password else None


def _valid(value: str | None, pattern: re.Pattern[str], what: str) -> str | None:
    if value is not None and not pattern.fullmatch(value):
        raise ValueError(f'invalid {what}')
    return value


def openobserve_sql(service, operation, trace_id) -> str:
    clauses = [f"operation_name = '{operation}'"]
    if trace_id:
        clauses.append(f"trace_id = '{trace_id}'")
    else:
        clauses.append(f"service_name = '{service}'")
    return 'SELECT trace_id FROM "default" WHERE ' + ' AND '.join(clauses)


def search(client, service, operation, trace_id, since_s, auth):
    now_us = int(time.time() * 1_000_000)
    body = {'query': {'sql': openobserve_sql(service, operation, trace_id),
                      'start_time': now_us - int(since_s * 1_000_000), 'end_time': now_us + 60_000_000,
                      'from': 0, 'size': 1}}
    response = client.post('/api/default/_search', params={'type': 'traces'}, json=body, auth=auth)
    if response.status_code in (401, 403):
        raise TraceAuthError('trace backend refused the credentials')
    if response.status_code != 200:
        raise TraceBackendError('unexpected status')
    hits = response.json().get('hits')
    return f'service={service}' if hits else None


def probe(client, *, service=SERVICE, operation=OPERATION, trace_id=None,
          since_s=600.0, auth=None) -> str | None:
    """One query. A detail string when a matching trace is visible, else None; TraceAuthError on refusal."""
    _valid(trace_id, _TRACE_ID, 'trace id')
    _valid(service, _NAME, 'service')
    _valid(operation, _NAME, 'operation')
    return search(client, service, operation, trace_id, since_s, auth)


def wait(client, *, timeout=60.0, interval=5.0, sleep=time.sleep, clock=time.monotonic, **kw) -> str | None:
    """Poll until a trace arrives or the timeout passes; auth refusal raises at once."""
    _valid(kw.get('trace_id'), _TRACE_ID, 'trace id')  # refuse before any query, not retried
    _valid(kw.get('service', SERVICE), _NAME, 'service')
    _valid(kw.get('operation', OPERATION), _NAME, 'operation')
    deadline = clock() + timeout
    warned = False
    while True:
        try:
            detail = probe(client, **kw)
            if detail:
                return detail
        except TraceAuthError:
            raise
        except (httpx2.HTTPError, TraceBackendError, ValueError, KeyError, TypeError, AttributeError):
            if not warned:  # transient: retry until the deadline, saying so once
                print('trace backend poll error', file=sys.stderr)
                warned = True
        if clock() >= deadline:
            return None
        sleep(min(interval, max(0.0, deadline - clock())))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='trace query API base URL')
    parser.add_argument('--service', default=SERVICE)
    parser.add_argument('--operation', default=OPERATION)
    parser.add_argument('--trace-id')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--since', type=float, default=600, help='lookback in seconds')
    parser.add_argument('--auth-stdin', action='store_true', help='read "user:password" from stdin')
    args = parser.parse_args(argv)
    auth = credentials_from_env()
    if args.auth_stdin:
        user, _, password = sys.stdin.readline().rstrip('\r\n').partition(':')
        auth = (user, password) if user and password else None
    if auth is None:
        print('trace backend credentials missing', file=sys.stderr)
        return 1
    with httpx2.Client(base_url=args.url.rstrip('/'), timeout=10) as client:
        try:
            detail = wait(client, timeout=args.timeout, service=args.service,
                         operation=args.operation, trace_id=args.trace_id, since_s=args.since, auth=auth)
        except TraceAuthError:
            print('trace backend refused the credentials', file=sys.stderr)
            return 1
        except ValueError:
            print('invalid trace check arguments', file=sys.stderr)
            return 1
    if detail:
        print(f'traced {detail}')
        return 0
    print(f'no {args.operation} trace landed in OpenObserve', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
