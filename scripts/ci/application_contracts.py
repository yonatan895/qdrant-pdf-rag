"""CI-only checks against the actual application HTTP interfaces.

Run in the agent pod via stdin. Synthetic citations prove interfaces, not quality.
"""
from __future__ import annotations

import argparse
import json

import httpx2


def events(text: str) -> list[tuple[str, object]]:
    assert text.replace('\r\n', '\n').endswith('\n\n'), 'unterminated frame'
    result = []
    for frame in text.replace('\r\n', '\n').split('\n\n'):
        kind, data = 'message', []
        for line in frame.splitlines():
            if line.startswith('event:'):
                kind = line[6:].strip()
            elif line.startswith('data:'):
                data.append(line[5:].lstrip())
        if data:
            raw = '\n'.join(data)
            result.append((kind, '[DONE]' if raw == '[DONE]' else json.loads(raw)))
    assert result, 'empty stream'
    return result


def check_stream(text: str, *, openai: bool, failure: bool) -> None:
    frames = events(text)
    errors, finishes, tokens = [], [], []
    for kind, payload in frames:
        if payload == '[DONE]':
            continue
        assert isinstance(payload, dict)
        if kind == 'error' or 'error' in payload:
            error = payload.get('error', payload)
            assert error['code'] == 'upstream_error'
            assert error['message'] == 'stream failed'
            errors.append(error)
        elif openai:
            choice = payload['choices'][0]
            if choice.get('finish_reason') is not None:
                finishes.append(choice)
            if choice.get('delta', {}).get('content'):
                tokens.append(choice['delta']['content'])
        elif kind == 'final':
            finishes.append(payload)
        elif kind == 'token':
            tokens.append(payload.get('delta') or payload.get('token'))
    if openai:
        assert frames[-1][1] == '[DONE]'
        assert sum(p == '[DONE]' for _, p in frames) == 1
        terminal = frames[-2][1]
        assert isinstance(terminal, dict)
        if failure:
            assert 'error' in terminal
        else:
            assert terminal.get('choices')
            assert terminal['choices'][0].get('finish_reason') == 'stop'
    else:
        assert frames[-1][0] == ('error' if failure else 'final')
    if failure:
        assert len(errors) == 1 and not finishes
    else:
        assert not errors and len(finishes) == 1 and any(tokens)
        final = finishes[0]
        assert final['finish_reason'] == 'stop'
        assert final['citations'] and not final['citations_inferred']


def run(client, *, failure: bool, streams_only: bool) -> None:
    query = 'Explain the IEA500I operator message.'
    messages = [{'role': 'user', 'content': query}]
    if not failure:
        health = client.get('/healthz')
        assert health.status_code == 200 and health.json()['status'] == 'ok'
        assert client.get('/ui').status_code == 200
    for path in ['/v1/answer', '/v1/chat', '/v1/chat/completions', '/ui/chat/stream']:
        chat = path != '/v1/answer'
        body = {'messages': messages} if chat else {'query': query}
        if path != '/ui/chat/stream' and not streams_only:
            response = client.post(path, json=body)
            payload = response.json()
            if failure:
                assert response.status_code == 502
                assert payload['code'] == 'upstream_error'
                assert payload['message'] in ('answer failed', 'retrieval failed')
            else:
                assert response.status_code == 200
                assert payload['citations'] and not payload['citations_inferred']
                if chat:
                    choice = payload['choices'][0]
                    assert choice['finish_reason'] == 'stop' and choice['message']['content']
                    followup = client.post(path, json={'messages': messages + [
                        {'role': 'assistant', 'content': choice['message']['content']},
                        {'role': 'user', 'content': 'What should the operator do about IEA500I?'}]})
                    assert followup.status_code == 200 and followup.json()['citations']
                else:
                    assert payload['answer']
        if path != '/ui/chat/stream':
            body['stream'] = True
        response = client.post(path, json=body)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        check_stream(response.text, openai=path.startswith('/v1/chat'), failure=failure)


def main() -> int:
    from mainframe_rag.config import load_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-failure', action='store_true')
    parser.add_argument('--streams-only', action='store_true')
    args = parser.parse_args()
    try:
        with httpx2.Client(base_url='http://localhost:8080',
                           timeout=load_settings().answer_timeout_s) as client:
            run(client, failure=args.expect_failure, streams_only=args.streams_only)
    except (AssertionError, ValueError, KeyError, TypeError, httpx2.HTTPError):
        print('APPLICATION CONTRACT FAILED')
        return 1
    print('APPLICATION CONTRACT PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
