"""Bounded request text for the HTTP request models (issues #1011, #1013).

Separate from `chat_turn` because the validator needs the application's one
Settings object: the app imports this module, so that import stays deferred and
the core answer modules never reach the application singleton.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, model_validator

from mainframe_rag.agent.chat_turn import validate_request_text


class BoundedRequestText(BaseModel):
    """Base for request models whose caller-supplied text is bounded before
    retrieval or any model call; fields a model does not declare read as None."""

    @model_validator(mode="after")
    def _bounded_text(self) -> Self:
        from mainframe_rag.agent import app as app_mod

        validate_request_text(
            app_mod.settings,
            query=getattr(self, "query", None),
            product=getattr(self, "product", None),
            version=getattr(self, "version", None),
            splunk_context=getattr(self, "splunk_context", None),
            messages=getattr(self, "messages", None),
        )
        return self
