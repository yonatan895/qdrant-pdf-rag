"""Fault checks must observe the desired upstream state, not an old Ready pod."""
import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ci/wait_mock_state.py'


@pytest.fixture
def waiter(monkeypatch):
    spec = importlib.util.spec_from_file_location('mock_state', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    return module, now


@pytest.mark.parametrize('pending', ['stale', 'refused', 'timeout', 'wrapped-timeout',
                                     'incomplete', 'non-object', 'unhealthy'])
def test_waits_for_the_service_to_serve_the_requested_state(waiter, monkeypatch, pending):
    module, now = waiter
    expected = {'chat_fault': 'upstream', 'embed_fault': 'healthy', 'ttft_ms': 0.0}
    calls = []

    def read(url, *, timeout):
        calls.append(url)
        assert url == 'http://vllm-mock:8000/healthz'
        assert timeout <= module.REQUEST_TIMEOUT_S
        if len(calls) == 1:
            if pending == 'refused':
                raise urllib.error.URLError(ConnectionRefusedError())
            if pending == 'timeout':
                raise TimeoutError()
            if pending == 'wrapped-timeout':
                raise urllib.error.URLError(TimeoutError())
            state = {'status': 'ok'} if pending == 'incomplete' else {
                'status': 'ok', **expected, 'chat_fault': 'healthy'}
            if pending == 'non-object':
                state = []
            if pending == 'unhealthy':
                state = {**expected, 'status': 'starting'}
        else:
            state = {'status': 'ok', **expected}
        return io.BytesIO(json.dumps(state).encode())

    monkeypatch.setattr(module.urllib.request, 'urlopen', read)
    module.wait_for_state(expected)
    assert len(calls) == 2 and now == [1.0]


def test_stale_state_exhausts_the_deadline(waiter, monkeypatch):
    module, now = waiter
    monkeypatch.setattr(module.urllib.request, 'urlopen',
                        lambda *a, **k: io.BytesIO(b'{"status":"ok","chat_fault":"healthy"}'))
    with pytest.raises(TimeoutError, match='requested state'):
        module.wait_for_state({'chat_fault': 'upstream'})
    assert now == [module.STATE_TIMEOUT_S]


@pytest.mark.parametrize('failure', ['http', 'json', 'other-transport'])
def test_invalid_health_response_fails_immediately(waiter, monkeypatch, failure):
    module, now = waiter

    def read(url, **kwargs):
        if failure == 'http':
            raise urllib.error.HTTPError(url, 403, 'Forbidden', None, None)
        if failure == 'other-transport':
            raise urllib.error.URLError(ValueError('invalid transport'))
        return io.BytesIO(b'not json')

    monkeypatch.setattr(module.urllib.request, 'urlopen', read)
    with pytest.raises((urllib.error.URLError, json.JSONDecodeError)):
        module.wait_for_state({'chat_fault': 'upstream'})
    assert now == [0.0]


def test_cli_confirms_the_exact_requested_state(waiter, monkeypatch, capsys):
    module, _ = waiter
    expected = {'chat_fault': 'truncated', 'embed_fault': 'healthy', 'ttft_ms': 5000.0}
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), 'truncated', 'healthy', '5000'])
    monkeypatch.setattr(module.urllib.request, 'urlopen',
                        lambda *a, **k: io.BytesIO(json.dumps({'status': 'ok', **expected}).encode()))
    module.main()
    assert json.loads(capsys.readouterr().out.removeprefix('MOCK STATE READY ')) == expected


@pytest.mark.parametrize('arguments', [[], ['typo', 'healthy', '0'],
                                     ['healthy', 'healthy', '-1'],
                                     ['healthy', 'healthy', 'nan'],
                                     ['healthy', 'healthy', 'inf']])
def test_invalid_state_arguments_never_wait(arguments):
    result = subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True,
                            text=True, check=False, timeout=5)
    assert result.returncode == 2
