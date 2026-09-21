"""The gateway must not forward a finish invented after provider exhaustion."""
import runpy
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/gateway/strict_finish.py'


@pytest.mark.anyio
@pytest.mark.parametrize('allowed,model,denied', [
    (['reasoning'], 'reasoning', False),
    (['embed'], 'reasoning', True),
    ([], 'reasoning', True),
    (['reasoning'], None, True),
    (['reasoning'], ['reasoning'], True),
])
async def test_passthrough_keeps_model_scope(monkeypatch, allowed, model, denied):
    from fastapi import HTTPException

    module = types.ModuleType('litellm.integrations.custom_logger')
    module.CustomLogger = object
    monkeypatch.setitem(sys.modules, 'litellm.integrations.custom_logger', module)
    guard = runpy.run_path(str(SCRIPT.with_name('scoped_passthrough.py')))['guard']
    auth = types.SimpleNamespace(models=allowed)
    data = {'model': model, 'messages': [{'role': 'user', 'content': 'synthetic'}]}
    if denied:
        with pytest.raises(HTTPException) as exc:
            await guard.async_pre_call_hook(auth, None, data, 'pass_through_endpoint')
        assert exc.value.status_code == 403
        assert exc.value.detail == 'model access denied'
    else:
        assert await guard.async_pre_call_hook(auth, None, data, 'pass_through_endpoint') is data
    assert await guard.async_pre_call_hook(auth, None, data, 'completion') is data


@pytest.fixture
def gateway_provider(monkeypatch):
    # LiteLLM remains absent from the product environment. These stand-ins
    # model the pinned gateway's state; live checks also run its actual image.
    class Wrapper:
        def __init__(self, events):
            self.events = iter(events)
            self.received_finish_reason = None
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            event = next(self.events, None)
            if event is None:
                raise StopAsyncIteration
            if isinstance(event, Exception):
                raise event
            self.received_finish_reason, emitted_finish = event
            return types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason=emitted_finish)])

        async def aclose(self):
            self.closed = True

    module = types.ModuleType('litellm')
    module.CustomStreamWrapper = Wrapper
    custom = types.ModuleType('litellm.llms.custom_llm')
    custom.CustomLLM = object

    class Adapter:
        def __init__(self):
            self.response = None
            self.calls = []

        async def completion(self, **kwargs):
            self.calls.append(kwargs)
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    adapter = types.ModuleType('litellm.llms.openai.openai')
    adapter.OpenAIChatCompletion = Adapter
    monkeypatch.setitem(sys.modules, 'litellm', module)
    monkeypatch.setitem(sys.modules, 'litellm.llms.custom_llm', custom)
    monkeypatch.setitem(sys.modules, 'litellm.llms.openai.openai', adapter)
    provider = runpy.run_path(str(SCRIPT))['strict_openai']
    return provider, Wrapper


@pytest.mark.anyio
@pytest.mark.parametrize('finish', ['stop', 'length', 'tool_calls'])
async def test_forwards_an_observed_provider_finish_and_closes(gateway_provider, finish):
    provider, wrapper = gateway_provider
    response = wrapper([(None, None), (finish, finish)])
    provider.adapter.response = response
    stream = provider.astreaming(encoding=None, optional_params={}, acompletion=True)
    chunks = [chunk async for chunk in stream]
    assert [chunk.choices[0].finish_reason for chunk in chunks] == [None, finish]
    assert response.closed


@pytest.mark.anyio
@pytest.mark.parametrize('events', [[(None, None), (None, 'stop')], [(None, None)], []])
async def test_missing_provider_finish_never_forwards_success(gateway_provider, events):
    provider, wrapper = gateway_provider
    response = wrapper(events)
    provider.adapter.response = response
    chunks = []
    with pytest.raises(ValueError, match='ended without a finish reason'):
        async for chunk in provider.astreaming(encoding=None, optional_params={}, acompletion=True):
            chunks.append(chunk)
    assert all(chunk.choices[0].finish_reason is None for chunk in chunks)
    assert response.closed


@pytest.mark.anyio
async def test_unavailable_upstream_state_fails_closed(gateway_provider):
    provider, _ = gateway_provider
    with pytest.raises(ValueError, match='state is unavailable'):
        async for _ in provider.astreaming(encoding=None, optional_params={}, acompletion=True):
            pytest.fail('unknown stream forwarded output')


@pytest.mark.anyio
async def test_upstream_error_is_propagated_and_stream_closed(gateway_provider):
    provider, wrapper = gateway_provider
    response = wrapper([(None, None), TimeoutError('test timeout')])
    provider.adapter.response = response
    with pytest.raises(TimeoutError, match='test timeout'):
        async for chunk in provider.astreaming(encoding=None, optional_params={}, acompletion=True):
            assert chunk.choices[0].finish_reason is None
    assert response.closed


@pytest.mark.anyio
async def test_consumer_stop_closes_upstream(gateway_provider):
    provider, wrapper = gateway_provider
    response = wrapper([(None, None), ('stop', 'stop')])
    provider.adapter.response = response
    stream = provider.astreaming(encoding=None, optional_params={}, acompletion=True)
    assert (await anext(stream)).choices[0].finish_reason is None
    await stream.aclose()
    assert response.closed


@pytest.mark.anyio
async def test_rejects_before_router_wrapper_can_erase_provider_state(gateway_provider):
    provider, wrapper = gateway_provider
    response = wrapper([(None, None), (None, 'stop')])
    provider.adapter.response = response

    class RouterWrapper:
        received_finish_reason = None  # Router does not forward inner state.

        def __aiter__(self):
            return provider.astreaming(encoding=None, optional_params={}, acompletion=True)

    chunks = []
    with pytest.raises(ValueError, match='ended without a finish reason'):
        async for chunk in RouterWrapper():
            chunks.append(chunk)
    assert len(chunks) == 1
    assert chunks[0].choices[0].finish_reason is None
    assert response.closed


@pytest.mark.anyio
@pytest.mark.parametrize('stream', [False, True])
async def test_delegates_request_parameters_once_to_existing_adapter(gateway_provider, stream):
    provider, wrapper = gateway_provider
    response = wrapper([('stop', 'stop')]) if stream else object()
    provider.adapter.response = response
    params = {'reasoning_effort': 'low', 'max_tokens': 128, 'temperature': 0.2,
              'stream_options': {'include_usage': True}, 'max_retries': 0}
    request = {'encoding': object(), 'acompletion': True, 'optional_params': params,
               'model': 'configured/model', 'messages': [{'role': 'user', 'content': 'hello'}],
               'api_base': 'https://backend.invalid/v1', 'api_key': 'dummy', 'timeout': 12.0,
               'client': object(), 'logging_obj': object(), 'headers': {'X-Test': 'value'},
               'litellm_params': {}, 'model_response': object(), 'custom_prompt_dict': {},
               'print_verbose': None, 'logger_fn': None}
    if stream:
        assert len([chunk async for chunk in provider.astreaming(**request)]) == 1
    else:
        assert await provider.acompletion(**request) is response
    expected = {k: v for k, v in request.items() if k != 'encoding'}
    expected.update(custom_llm_provider='openai', optional_params={**params, 'stream': stream})
    assert provider.adapter.calls == [expected]
    assert 'stream' not in params


@pytest.mark.anyio
@pytest.mark.parametrize('stream', [False, True])
async def test_adapter_request_failure_is_not_retried(gateway_provider, stream):
    provider, _ = gateway_provider
    provider.adapter.response = TimeoutError('test timeout')
    kwargs = {'encoding': None, 'optional_params': {}, 'acompletion': True}
    with pytest.raises(TimeoutError, match='test timeout'):
        if stream:
            async for _ in provider.astreaming(**kwargs):
                pytest.fail('failed request forwarded output')
        else:
            await provider.acompletion(**kwargs)
    assert len(provider.adapter.calls) == 1
