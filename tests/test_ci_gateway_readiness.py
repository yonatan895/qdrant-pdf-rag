"""Exercise the setup request path, including the Ready-before-Service race."""
import importlib.util
import io
import json
import ssl
import urllib.error
from pathlib import Path

import pytest


@pytest.fixture
def setup_gateway():
    path = Path(__file__).resolve().parents[1] / 'scripts/ci/mint_gateway_keys.py'
    spec = importlib.util.spec_from_file_location('gateway_setup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clock(monkeypatch, setup_gateway):
    now = [0.0]
    monkeypatch.setattr(setup_gateway.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(setup_gateway.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


@pytest.mark.parametrize('failure', [
    urllib.error.URLError(ConnectionRefusedError()),
    urllib.error.URLError(TimeoutError()),
    TimeoutError(),
])
def test_service_wait_retries_only_read_before_creating_keys(setup_gateway, monkeypatch, clock, capsys, failure):
    requests = []
    created = []

    def open_request(request, *, timeout):
        requests.append(request)
        assert request.full_url.startswith('https://test-gateway:4000/')
        assert request.get_header('Authorization').startswith('Bearer ')
        if len(requests) == 1:
            assert request.get_method() == 'GET'
            raise failure
        if request.get_method() == 'POST':
            assert timeout == 30
            created.append(json.loads(request.data)['models'][0])
            return io.BytesIO(json.dumps({'key': 'sk-' + created[-1]}).encode())
        if request.get_header('Authorization') in ('Bearer ', 'Bearer sk-wrong'):
            raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', None, None)
        return io.BytesIO(b'{"data": []}')

    monkeypatch.setenv('GATEWAY_MASTER_KEY', 'sk-master')
    monkeypatch.setattr(setup_gateway.urllib.request, 'urlopen', open_request)
    setup_gateway.main()
    keys = json.loads(capsys.readouterr().out)
    assert keys['llm-api-key'] == 'sk-mock-reasoning'
    assert keys['embed-api-key'] == 'sk-mock-embed'
    assert created == ['mock-reasoning', 'mock-embed']
    assert [r.get_method() for r in requests[:3]] == ['GET', 'GET', 'POST']
    assert clock == [1.0]


@pytest.mark.parametrize('error', [
    urllib.error.HTTPError('https://test-gateway:4000/v1/models', 401, 'Unauthorized', None, None),
    urllib.error.HTTPError('https://test-gateway:4000/v1/models', 503, 'Unavailable', None, None),
    urllib.error.URLError(ssl.SSLCertVerificationError('untrusted CA')),
    urllib.error.URLError(ssl.SSLCertVerificationError('hostname mismatch')),
    json.JSONDecodeError('malformed', '', 0),
])
def test_service_wait_does_not_retry_http_tls_or_malformed_responses(setup_gateway, monkeypatch, clock, error):
    calls = []

    def fail(request, *, timeout):
        calls.append(request)
        raise error

    monkeypatch.setattr(setup_gateway.urllib.request, 'urlopen', fail)
    with pytest.raises(type(error)):
        setup_gateway.wait_for_service('sk-master')
    assert len(calls) == 1
    assert calls[0].get_method() == 'GET'
    assert clock == [0.0]


def test_service_wait_has_a_total_deadline(setup_gateway, monkeypatch, clock):
    timeouts = []

    def unavailable(request, *, timeout):
        assert request.get_method() == 'GET'
        timeouts.append(timeout)
        clock[0] += timeout
        raise urllib.error.URLError(TimeoutError())

    monkeypatch.setattr(setup_gateway.urllib.request, 'urlopen', unavailable)
    with pytest.raises(TimeoutError, match='readiness deadline'):
        setup_gateway.wait_for_service('sk-master')
    assert clock == [setup_gateway.SERVICE_READY_TIMEOUT_S]
    assert max(timeouts) <= setup_gateway.SERVICE_REQUEST_TIMEOUT_S


def test_key_creation_is_never_retried(setup_gateway, monkeypatch, clock, capsys):
    methods = []

    def wire(request, *, timeout):
        methods.append(request.get_method())
        if request.get_method() == 'POST':
            raise urllib.error.URLError(ConnectionRefusedError())
        return io.BytesIO(b'{"data": []}')

    monkeypatch.setenv('GATEWAY_MASTER_KEY', 'sk-master')
    monkeypatch.setattr(setup_gateway.urllib.request, 'urlopen', wire)
    with pytest.raises(urllib.error.URLError):
        setup_gateway.main()
    assert methods == ['GET', 'POST']
    assert not capsys.readouterr().out
    assert clock == [0.0]
