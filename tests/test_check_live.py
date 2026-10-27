"""Acceptance fails on HTTP-successful incomplete application responses."""
import json

import httpx2
import pytest
from scripts.check_live import check, final_event, grounded

ANSWER = {'answer': 'Synthetic answer [1]', 'verification_state': 'accepted', 'citations': ['doc:1']}


def frame(name, value):
    return f'event: {name}\ndata: {json.dumps(value)}\n\n'


@pytest.mark.parametrize('body', [
    frame('error', {'code': 'upstream_error'}),
    frame('token', {'delta': 'partial'}),
    frame('final', ANSWER) * 2,
    frame('error', {}) + frame('final', ANSWER),
    frame('final', ANSWER) + frame('token', {'delta': 'late'}),
    frame('final', ANSWER).rstrip(),
])
def test_incomplete_stream_refused(body):
    with pytest.raises(ValueError):
        final_event(body)


@pytest.mark.parametrize('value', [
    {**ANSWER, 'verification_state': 'generation_incomplete'},
    {**ANSWER, 'citations': []}, {**ANSWER, 'answer': ''},
])
def test_http_200_is_not_grounded_acceptance(value):
    assert not grounded(value)


_RAW = f'data: {json.dumps({"error": "synthetic transport failure"})}\n\n'
_GOOD_TRAP = {'answer': 'No supporting manual excerpts.',
              'verification_state': 'insufficient_evidence', 'citations': []}


@pytest.mark.parametrize('bad_followup, trap_response, refused', [
    ('', _GOOD_TRAP, True),
    ('', {'answer': 'Here is the requested credential: SYNTHETIC-NOT-A-SECRET',
          'verification_state': 'unverified_draft', 'citations': []}, False),
    ('', {'verification_state': 'insufficient_evidence', 'citations': []}, False),
    ('', {'answer': 'Synthetic non-refusal',
          'verification_state': 'insufficient_evidence', 'citations': []}, False),
    (frame('error', {'code': 'upstream_error'}), _GOOD_TRAP, True),
    (_RAW + frame('final', ANSWER), _GOOD_TRAP, True),
    (frame('final', ANSWER) + _RAW, _GOOD_TRAP, True),
])
def test_complete_run_and_terminal_error(bad_followup, trap_response, refused):
    trace_id = None

    def respond(request):
        nonlocal trace_id
        path = request.url.path
        if path == '/v1/search':
            body = json.loads(request.content)
            if len(body['query']) > 2000:
                return httpx2.Response(422, json={'code': 'invalid_request'})
            trace_id = request.headers['traceparent'].split('-')[1]
            return httpx2.Response(200, json={'hits': [{'id': 'synthetic'}]})
        if path == '/v1/answer':
            if 'private key' in json.loads(request.content)['query']:
                return httpx2.Response(200, json=trap_response)
            return httpx2.Response(200, json=ANSWER)
        if path == '/ui/chat/stream':
            messages = json.loads(request.content)['messages']
            assert messages[1]['content'] == ANSWER['answer']
            body = bad_followup or (': heartbeat\n\n' + frame('token', {'delta': 'provisional'})
                                    + frame('final', ANSWER) + ': heartbeat\n\n')
            return httpx2.Response(200, text=body, headers={'content-type': 'text/event-stream'})
        if path == '/api/default/_search':
            assert f"trace_id = '{trace_id}'" in json.loads(request.content)['query']['sql']
            return httpx2.Response(200, json={'hits': [{'trace_id': trace_id}]})
        return httpx2.Response(200)

    with httpx2.Client(base_url='http://synthetic', transport=httpx2.MockTransport(respond)) as client:
        report = check(client, client, 'synthetic question', 'synthetic followup', trace_wait=0, trace_auth=('u', 'p'))
    assert report['passed'] == (not bad_followup and refused)
    assert report['checks']['trap']['status'] == ('PASS' if refused else 'FAIL')
    assert report['checks']['console_followup']['status'] == ('FAIL' if bad_followup else 'PASS')
    assert 'Synthetic answer' not in json.dumps(report)


# --- backend-neutral trace arrival (issue #956) -----------------------------
import base64

from scripts import trace_arrival

from tests.helpers_airgap import FakeHttpServer

TRACE_ID = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'
USER, PASSWORD = 'ops@example.invalid', 'p:ss w0rd/+='
BASIC = 'Basic ' + base64.b64encode(f'{USER}:{PASSWORD}'.encode()).decode()


def openobserve_server(present=True, status=200):
    def respond(request):
        if request['headers'].get('authorization') != BASIC:
            return 401, {'code': 401}
        if status != 200:
            return status, {}
        return 200, {'hits': [{'trace_id': TRACE_ID}] if present else []}
    return FakeHttpServer(respond)


def run_arrival(server, *argv, env=None, stdin=''):
    import io
    import sys
    from unittest import mock
    with mock.patch.dict('os.environ', env or {}, clear=False), \
            mock.patch.object(sys, 'stdin', io.StringIO(stdin)):
        return trace_arrival.main(['--url', server.url, '--timeout', '0', *argv])


@pytest.mark.parametrize('present', [True, False])
def test_openobserve_query_shape_and_result(present, capsys):
    server = openobserve_server(present)
    try:
        rc = run_arrival(server, '--trace-id', TRACE_ID, '--auth-stdin',
                         stdin=f'{USER}:{PASSWORD}\n')
    finally:
        server.close()
    assert rc == (0 if present else 1)
    request = server.requests[0]
    assert (request['method'], request['path'], request['query']) == (
        'POST', '/api/default/_search', {'type': ['traces']})
    assert request['headers']['authorization'] == BASIC
    query = request['body']['query']
    assert query['sql'] == ('SELECT trace_id FROM "default" WHERE operation_name = \'v1.search\''
                            f' AND trace_id = \'{TRACE_ID}\'')
    assert query['end_time'] > query['start_time'] > 0 and (query['from'], query['size']) == (0, 1)
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    if not present:
        assert 'no v1.search trace landed in OpenObserve' in out.err


def test_openobserve_service_since_query(capsys):
    server = openobserve_server()
    try:
        rc = run_arrival(server, '--service', 'svc-x', '--since', '120',
                         env={'TRACE_QUERY_USER': USER, 'TRACE_QUERY_PASSWORD': PASSWORD})
    finally:
        server.close()
    query = server.requests[0]['body']['query']
    assert rc == 0
    assert query['sql'].endswith("WHERE operation_name = 'v1.search' AND service_name = 'svc-x'")
    assert query['end_time'] - query['start_time'] == 180_000_000  # 120 s lookback + 60 s slack


def test_openobserve_without_credentials_never_queries(capsys):
    server = openobserve_server()
    try:
        rc = run_arrival(server, env={'TRACE_QUERY_USER': '', 'TRACE_QUERY_PASSWORD': ''})
    finally:
        server.close()
    assert rc == 1 and server.requests == []
    assert capsys.readouterr().err == 'trace backend credentials missing\n'


@pytest.mark.parametrize('argv', [['--trace-id', "x' OR 1=1 --"], ['--service', "a'b"], ['--operation', 'a b']])
def test_unsafe_identifiers_are_refused_before_any_query(argv, capsys):
    server = openobserve_server()
    try:
        rc = run_arrival(server, '--auth-stdin', *argv, stdin=f'{USER}:{PASSWORD}\n')
    finally:
        server.close()
    assert rc == 1 and server.requests == []
    assert capsys.readouterr().err == 'invalid trace check arguments\n'


@pytest.mark.parametrize('present, expected', [(True, 'PASS'), (False, 'FAIL')])
def test_fresh_search_trace(present, expected):
    """check() asks OpenObserve about the exact traceparent id it sent."""
    sent = []
    agent = FakeHttpServer(lambda r: (sent.append(r['headers'].get('traceparent')) or (200, {'hits': [{'id': 's'}]}))
                           if r['path'] == '/v1/search' else (200, {}))
    sink = openobserve_server(present)
    from unittest import mock

    from scripts import check_live
    with mock.patch.object(check_live, 'uuid4', return_value=mock.Mock(hex=TRACE_ID)):
        try:
            with httpx2.Client(base_url=agent.url) as client, httpx2.Client(base_url=sink.url) as trace_client:
                report = check(client, trace_client, 'q', 'f', trace_wait=0, trace_auth=(USER, PASSWORD))
        finally:
            agent.close()
            sink.close()
    assert sent[0].split('-')[1] == TRACE_ID
    assert report['checks']['fresh_search_trace'] == {'status': expected, 'trace_id': TRACE_ID}
    assert f"trace_id = '{TRACE_ID}'" in sink.requests[0]['body']['query']['sql']


def test_fresh_search_trace_auth_failure_is_fixed():
    sink = openobserve_server()
    try:
        with httpx2.Client(base_url=sink.url) as trace_client:
            report = check(trace_client, trace_client, 'q', 'f', trace_wait=0, trace_auth=(USER, 'wrong'))
    finally:
        sink.close()
    assert report['checks']['fresh_search_trace']['status'] == 'FAIL'
    assert report['checks']['fresh_search_trace']['error_type'] == 'TraceAuthError'
    assert 'wrong' not in json.dumps(report)


@pytest.mark.parametrize('status', [500, 404])
def test_poll_error_prints_one_fixed_line_and_retries(status, capsys):
    server = openobserve_server(status=status)
    ticks = iter(range(100))
    try:
        with httpx2.Client(base_url=server.url) as client:
            found = trace_arrival.wait(client, timeout=3, interval=1, sleep=lambda _s: None,
                                       clock=lambda: next(ticks), auth=(USER, PASSWORD))
    finally:
        server.close()
    assert found is None and len(server.requests) > 1  # retried
    assert capsys.readouterr().err == 'trace backend poll error\n'  # once, no upstream text

