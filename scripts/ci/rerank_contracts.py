"""CI-only checks of the configured rerank leg from the application pod (issue #815).

Run in the agent pod via stdin: the pod carries the Deployment's real
RERANK_* settings, the per-leg Secret key and the gateway CA. Modes:

    gateway-auth     the rerank key is accepted on both gateway routes; a wrong,
                     absent or other-leg key is refused on both
    ready            /healthz is 200 with the rerank leg up
    discover-scope   print a product/version scope that exists in the corpus
    search           print a scoped /v1/search observation (needs --scope-json)
    degraded         rerank routes down: fixed-message failure, bypass, unready
    recovered        rerank routes back: ready again, then an ordinary search
    off              rerank disabled: /healthz rerank is null

Output is fixed-label lines or one JSON observation of chunk ids and scores.
Keys, response bodies and passage text are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx2

APP = 'http://localhost:8080'
# Natural-language, lowercase: no identifier or injection signal, so rerank is not bypassed.
QUERY = 'how do operators reissue a rejected command after initialization completes'
IDENTIFIER_QUERY = 'IEA500I operator message'
LIMIT = 40  # SearchRequest maximum: every scoped candidate comes back
REFUSED = (401, 403)
STATE_TIMEOUT_S = 75  # health_rerank_ttl_s (15) + health_rerank_timeout_s (5) + margin
POLL_S = 3


class Failed(Exception):
    pass


def require(condition, label):
    if not condition:
        raise Failed(label)


def search(client, query, scope=None, *, limit=LIMIT):
    return client.post('/v1/search', json={'query': query, 'limit': limit, **(scope or {})})


def observation(response, scope):
    require(response.status_code == 200, 'search status')
    body = response.json()
    return {
        'query': QUERY, 'scope': scope, 'query_kind': body['query_kind'],
        'hits': [{'chunk_id': h['chunk_id'], 'score': h['score'], 'rerank_score': h.get('rerank_score'),
                  'doc_id': h['doc_id'], 'page_label': h['page_label']} for h in body['hits']],
    }


def healthz(client):
    response = client.get('/healthz')
    return response.status_code, response.json()


def wait_for_health(client, ready):
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        status, body = healthz(client)
        if ready and status == 200 and body.get('rerank') is True:
            return body
        if not ready and status == 503 and body.get('rerank') is False:
            return body
        if time.monotonic() >= deadline:
            raise Failed('healthz rerank state did not converge')
        time.sleep(POLL_S)


def gateway_auth():
    base = os.environ['RERANK_BASE_URL'].rstrip('/')
    model = os.environ['RERANK_MODEL']
    key, llm, embed = (os.environ.get(n, '') for n in ('RERANK_API_KEY', 'LLM_API_KEY', 'EMBED_API_KEY'))
    # The Deployment must carry a distinct, real per-leg rerank key (not another leg's, not a placeholder).
    require(key.startswith('sk-') and key not in (llm, embed, 'sk-unused-rerank-disabled'), 'rerank key wiring')
    routes = {
        'score': (base + '/score', {'model': model, 'text_1': 'auth check', 'text_2': ['a', 'b']}, 'data'),
        'rerank': (base + '/rerank', {'model': model, 'query': 'auth check', 'documents': ['a', 'b']}, 'results'),
    }
    credentials = {'rerank key': key, 'wrong key': 'sk-wrong', 'absent key': None, 'other-leg key': llm}
    with httpx2.Client(timeout=30) as client:
        for route, (url, payload, field) in routes.items():
            for label, secret in credentials.items():
                headers = {} if secret is None else {'Authorization': 'Bearer ' + secret}
                response = client.post(url, json=payload, headers=headers)
                require(key not in response.text, f'{route} response echoes a key')
                if label == 'rerank key':
                    require(response.status_code == 200, f'{route}: {label} accepted')
                    require(len(response.json()[field]) == 2, f'{route}: {label} shape')
                else:
                    require(response.status_code in REFUSED, f'{route}: {label} refused')
                print(f'PASS: {route} {label} {"accepted" if label == "rerank key" else "refused"}')


def run(mode, args):
    with httpx2.Client(base_url=APP, timeout=60) as client:
        if mode == 'ready':
            status, body = healthz(client)
            require(status == 200 and body['status'] == 'ok' and body['rerank'] is True, 'healthz rerank up')
            print('PASS: healthz reports the rerank leg up')
        elif mode == 'discover-scope':
            response = search(client, QUERY)
            require(response.status_code == 200, 'search status')
            tagged = [h for h in response.json()['hits'] if h.get('product')]
            require(bool(tagged), 'corpus has a product-tagged document')
            print(json.dumps({'product': tagged[0]['product'], 'version': tagged[0].get('version')}))
        elif mode == 'search':
            scope = json.loads(args.scope_json)
            print(json.dumps(observation(search(client, QUERY, scope), scope)))
        elif mode == 'degraded':
            scope = json.loads(args.scope_json)
            failed = search(client, QUERY, scope)
            require(failed.status_code == 502, 'non-identifier search fails closed')
            fixed = failed.json()
            require(fixed.get('code') == 'upstream_error' and fixed.get('message') == 'retrieval failed',
                    'search failure is the fixed message')
            require('mock injected failure' not in failed.text, 'search failure echoes upstream text')
            print('PASS: search fails closed with the fixed retrieval-failed message')
            lookup = search(client, IDENTIFIER_QUERY, scope)
            require(lookup.status_code == 200 and lookup.json()['hits'], 'identifier lookup still serves')
            require(all(h.get('rerank_score') is None for h in lookup.json()['hits']), 'identifier bypasses rerank')
            print('PASS: identifier lookup still 200 (bypasses rerank)')
            body = wait_for_health(client, ready=False)
            require(body['status'] == 'degraded' and body['rerank'] is False, 'healthz degraded body')
            require('mock injected failure' not in json.dumps(body), 'healthz echoes upstream text')
            print('PASS: healthz is 503 degraded with the rerank leg unready')
        elif mode == 'recovered':
            scope = json.loads(args.scope_json)
            wait_for_health(client, ready=True)
            hits = observation(search(client, QUERY, scope), scope)['hits']
            require(bool(hits) and all(h['rerank_score'] is not None for h in hits), 'reranked search after recovery')
            print('PASS: healthz ready again and the next ordinary search is reranked')
        elif mode == 'off':
            status, body = healthz(client)
            require(status == 200 and body['rerank'] is None, 'healthz rerank null when disabled')
            print('PASS: healthz reports rerank null when disabled')
        else:
            raise Failed('unknown mode')


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['gateway-auth', 'ready', 'discover-scope', 'search', 'degraded',
                                         'recovered', 'off'])
    parser.add_argument('--scope-json')
    args = parser.parse_args(argv)
    try:
        if args.mode == 'gateway-auth':
            gateway_auth()
        else:
            run(args.mode, args)
    except (Failed, KeyError, TypeError, ValueError, httpx2.HTTPError) as exc:
        # Fixed labels only: never a response body, header, URL or key.
        print('RERANK CONTRACT FAILED: ' + args.mode + ': ' + (str(exc) if isinstance(exc, Failed) else type(exc).__name__))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
