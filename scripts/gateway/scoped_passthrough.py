"""Keep local tokenizer/score pass-through requests scoped to the caller's model.

The pinned gateway authenticates custom routes but deliberately skips its normal
model allowlist on them. Reapply the local virtual-key contract before forwarding.
No platform gateway code or product dependency is added by this local/CI hook.
"""
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger


class ScopedPassThrough(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type == "pass_through_endpoint":
            model = data.get("model")
            allowed = user_api_key_dict.models or []
            if not isinstance(model, str) or not model or model not in allowed:
                raise HTTPException(status_code=403, detail="model access denied")
        return data


guard = ScopedPassThrough()
