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
    {**ANSWER, 'verification_state': 'insufficient_evidence'},
    {**ANSWER, 'citations': []}, {**ANSWER, 'answer': ''},
])
def test_http_200_is_not_grounded_acceptance(value):
    assert not grounded(value)


@pytest.mark.parametrize('bad_followup', [False, True])
def test_complete_run_and_terminal_error(bad_followup):
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
                return httpx2.Response(200, json={'verification_state': 'unverified_draft', 'citations': []})
            return httpx2.Response(200, json=ANSWER)
        if path == '/ui/chat/stream':
            messages = json.loads(request.content)['messages']
            assert messages[1]['content'] == ANSWER['answer']
            body = frame('error', {'code': 'upstream_error'}) if bad_followup else frame('final', ANSWER)
            return httpx2.Response(200, text=body, headers={'content-type': 'text/event-stream'})
        if path.startswith('/api/traces/'):
            assert path.endswith(trace_id)
            return httpx2.Response(200, json={'data': [{'traceID': trace_id,
                'spans': [{'operationName': 'v1.search'}]}]})
        return httpx2.Response(200)

    with httpx2.Client(base_url='http://synthetic', transport=httpx2.MockTransport(respond)) as client:
        report = check(client, client, 'synthetic question', 'synthetic followup', trace_wait=0)
    assert report['passed'] is not bad_followup
    assert report['checks']['console_followup']['status'] == ('FAIL' if bad_followup else 'PASS')
    assert 'Synthetic answer' not in json.dumps(report)
