"""The real HTTP acceptance checker rejects false success on every wire shape."""
import importlib.util
import json
from pathlib import Path

import httpx2
import pytest

SPEC = importlib.util.spec_from_file_location('ci_application_contracts',
    Path(__file__).resolve().parents[1] / 'scripts/ci/application_contracts.py')
assert SPEC and SPEC.loader
contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contracts)


def stream(*, openai, failure=False):
    final = {'finish_reason': 'stop', 'citations': ['original synthetic, p. 1'],
             'citations_inferred': False}
    error = {'code': 'upstream_error', 'message': 'stream failed'}
    if openai:
        chunks = [{'choices': [{'delta': {'content': 'synthetic answer'}, 'finish_reason': None}]},
                  {'error': error} if failure else {'choices': [{'delta': {}, **final}]}]
        return ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'
    return ('event: token\ndata: {"delta":"synthetic answer"}\n\n' +
            ('event: error\ndata: ' + json.dumps(error) if failure else
             'event: final\ndata: ' + json.dumps(final)) + '\n\n')


@pytest.mark.parametrize('openai', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_stream_success_and_reported_failure(openai, failure):
    text = stream(openai=openai, failure=failure)
    contracts.check_stream(text, openai=openai, failure=failure)
    with pytest.raises(AssertionError):
        contracts.check_stream(text, openai=openai, failure=not failure)


@pytest.mark.parametrize('openai', [False, True])
@pytest.mark.parametrize('fault', ['empty', 'incomplete-frame', 'missing-terminal',
                                 'invalid-json', 'length-finish', 'no-cites',
                                 'inferred-cites', 'duplicate-terminal', 'late-token'])
def test_bad_stream_never_counts_as_success(openai, fault):
    text = stream(openai=openai)
    if fault == 'empty':
        text = ''
    elif fault == 'incomplete-frame':
        text = text.rstrip()
    elif fault == 'missing-terminal':
        text = text.split('\n\n')[0] + '\n\n'
    elif fault == 'invalid-json':
        text = text.replace('"stop"', 'bad-json')
    elif fault == 'length-finish':
        text = text.replace('"stop"', '"length"')
    elif fault == 'no-cites':
        text = text.replace('["original synthetic, p. 1"]', '[]')
    elif fault == 'inferred-cites':
        text = text.replace('false', 'true')
    elif fault == 'duplicate-terminal':
        text += text
    elif fault == 'late-token':
        token = text.split('\n\n')[0] + '\n\n'
        text = text.replace('data: [DONE]', token + 'data: [DONE]') if openai else text + token
    with pytest.raises((AssertionError, ValueError, KeyError, TypeError)):
        contracts.check_stream(text, openai=openai, failure=False)


class AppClient:
    def __init__(self, *, failure=False, false_success=False):
        self.failure, self.false_success = failure, false_success
        self.calls = []

    def get(self, path):
        return httpx2.Response(200, json={'status': 'ok'})

    def post(self, path, *, json):
        self.calls.append((path, json))
        failure = self.failure and not self.false_success
        if json.get('stream') or path.startswith('/ui'):
            return httpx2.Response(200, text=stream(openai=path.startswith('/v1/chat'), failure=failure),
                                   headers={'content-type': 'text/event-stream'})
        if failure:
            return httpx2.Response(502, json={'code': 'upstream_error', 'message': 'answer failed'})
        return httpx2.Response(200, json={'answer': 'synthetic answer', 'citations': ['synthetic, p. 1'],
            'citations_inferred': False, 'choices': [{'finish_reason': 'stop',
                'message': {'content': 'synthetic answer'}}]})


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('streams_only', [False, True])
def test_checker_reaches_all_http_paths(failure, streams_only):
    client = AppClient(failure=failure)
    contracts.run(client, failure=failure, streams_only=streams_only)
    assert {path for path, _ in client.calls} == {
        '/v1/answer', '/v1/chat', '/v1/chat/completions', '/ui/chat/stream'}
    if not failure and not streams_only:
        assert sum(len(body.get('messages', [])) == 3 for _, body in client.calls) == 2


def test_expected_failure_rejects_false_success():
    with pytest.raises(AssertionError):
        contracts.run(AppClient(failure=True, false_success=True), failure=True, streams_only=False)
