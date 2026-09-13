"""CI-only virtual-key setup after the authenticated TLS Service is reachable."""
import json
import os
import time
import urllib.error
import urllib.request

# A pod may be Ready before EndpointSlice / Service routing has converged.
# Bound only this read-only setup check; never retry key creation or model calls.
SERVICE_READY_TIMEOUT_S = 60
SERVICE_READY_POLL_S = 1
SERVICE_REQUEST_TIMEOUT_S = 5
BASE_URL = 'https://test-gateway:4000'


def call(path, key, payload=None, *, timeout=30):
    req = urllib.request.Request(BASE_URL + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def wait_for_service(master):
    deadline = time.monotonic() + SERVICE_READY_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('gateway Service readiness deadline exceeded')
        try:
            call('/v1/models', master, timeout=min(SERVICE_REQUEST_TIMEOUT_S, remaining))
            return
        except urllib.error.URLError as exc:
            # HTTP errors, certificate/hostname failures and malformed responses
            # remain immediate failures; only connection startup is retried.
            if not isinstance(exc.reason, (ConnectionRefusedError, TimeoutError)):
                raise
        except TimeoutError:
            pass
        time.sleep(min(SERVICE_READY_POLL_S, max(0, deadline - time.monotonic())))


def main():
    master = os.environ['GATEWAY_MASTER_KEY']
    wait_for_service(master)
    keys = {}
    for leg, model in [('llm', 'mock-reasoning'), ('embed', 'mock-embed')]:
        key = call('/key/generate', master, {'models': [model]})['key']
        call('/v1/models', key)
        keys[leg + '-api-key'] = key
    # Both absent and wrong credentials must be refused by the real gateway.
    for key in ['', 'sk-wrong']:
        try:
            call('/v1/models', key)
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403):
                raise
        else:
            raise RuntimeError('gateway accepted invalid credentials')
    # Deploy's optional key Secret references all configured fields.
    keys['context-llm-api-key'] = keys['llm-api-key']
    keys['rerank-api-key'] = 'sk-unused-rerank-disabled'
    print(json.dumps(keys))


if __name__ == "__main__":
    main()
