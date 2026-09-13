"""Local/CI-only OpenAI adapter that rejects inferred stream finishes.

Loaded by the pinned LiteLLM image, never by product images. The platform-owned
production gateway must supply equivalent protection. Checking at the proxy's
post-call hook is too late: Router replaces the wrapper holding provider state.
"""
from litellm import CustomStreamWrapper
from litellm.llms.custom_llm import CustomLLM
from litellm.llms.openai.openai import OpenAIChatCompletion


class StrictOpenAI(CustomLLM):
    def __init__(self):
        super().__init__()
        self.adapter = OpenAIChatCompletion()

    async def _request(self, *, encoding, optional_params, acompletion, stream, **kwargs):
        # Reuse the pinned adapter's request conversion, client cache, timeouts,
        # authentication and response parsing. No nested LiteLLM/router request.
        return await self.adapter.completion(
            **kwargs, acompletion=True, custom_llm_provider='openai',
            optional_params={**optional_params, 'stream': stream},
        )

    async def acompletion(self, **kwargs):
        return await self._request(**kwargs, stream=False)

    async def astreaming(self, **kwargs):
        response = await self._request(**kwargs, stream=True)
        if not isinstance(response, CustomStreamWrapper):
            raise ValueError('upstream stream state is unavailable')
        try:
            async for chunk in response:
                if any(choice.finish_reason is not None for choice in chunk.choices):
                    if response.received_finish_reason is None:
                        raise ValueError('upstream stream ended without a finish reason')
                yield chunk
            if response.received_finish_reason is None:
                raise ValueError('upstream stream ended without a finish reason')
        finally:
            await response.aclose()


strict_openai = StrictOpenAI()
