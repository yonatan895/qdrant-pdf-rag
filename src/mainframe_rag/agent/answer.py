"""Reasoning-model chat for /v1/answer.

Contract (architecture.md 4.6): /v1/answer must use the reasoning/thinking
model — never a cheap chat model. The only model this module can call is
settings.llm_model_reasoning; there is deliberately no other model knob.
"""

from __future__ import annotations

import re

import httpx

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import SearchHit

FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)

SYSTEM_PROMPT = (
    "You are a mainframe operations expert (z/OS, CICS, Db2, IMS, JES2/3, RACF, "
    "z/VM, VTAM). Answer operational questions. Rules:\n"
    "1. Only assert what the supplied citations support.\n"
    "2. If manuals disagree between versions, say which version each statement "
    "comes from.\n"
    "3. If the citations do not answer the question, say so explicitly.\n"
    "4. When you propose JCL, REXX, or operator steps, put them in a fenced code "
    "block and tie them to a citation. Scripts are examples, not "
    "production-ready without review.\n"
    "5. End your reply with a 'Citations:' list. Each citation line must be "
    "exactly: <doc number> <title>, <heading path>, p. <page label>\n"
)


def build_messages(
    query: str,
    hits: list[SearchHit],
    product: str | None = None,
    version: str | None = None,
    splunk_context: str | None = None,
) -> list[dict[str, str]]:
    chunks: list[str] = []
    for i, hit in enumerate(hits, 1):
        chunks.append(f"[{i}] {hit.cite}\n{hit.text}")

    parts = []
    context_bits = []
    if product:
        context_bits.append(f"product: {product}")
    if version:
        context_bits.append(f"version: {version}")
    if context_bits:
        parts.append("Sysplex context: " + ", ".join(context_bits))
    if splunk_context:
        parts.append(
            "Splunk context (live system observation; join key is the message ID):\n"
            + splunk_context.strip()
        )
    parts.append("Question: " + query)
    parts.append("Retrieved manual excerpts:\n" + "\n\n".join(chunks))

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def assert_reasoning_model(settings: Settings) -> str:
    """Fail closed: no reasoning model configured, no LLM call."""
    if not settings.llm_base_url:
        raise RuntimeError("LLM_BASE_URL is unset; /v1/answer cannot run.")
    return settings.require_reasoning_model()


def call_reasoning_model(
    messages: list[dict[str, str]], settings: Settings, client: httpx.Client | None = None
) -> str:
    model = assert_reasoning_model(settings)
    assert settings.llm_base_url  # guaranteed by assert_reasoning_model
    own_client = client is None
    client = client or httpx.Client(timeout=300.0)
    try:
        resp = client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json={"model": model, "messages": messages},
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"])
    finally:
        if own_client:
            client.close()


def parse_answer(content: str, allowed_citations: set[str]) -> dict:
    """Split model output into answer, validated citations, optional script."""
    from mainframe_rag.agent.cites import valid_citations

    fence = FENCE_RE.search(content)
    script = fence.group(1).strip() if fence else None

    citations = valid_citations(content, allowed_citations)

    # Answer body = everything before the Citations: header, minus the fence.
    body = content.split("Citations:")[0] if "Citations:" in content else content
    if fence and fence.group(0) in body:
        body = body.replace(fence.group(0), "")
    return {
        "answer": body.strip(),
        "citations": citations,
        "script": script,
    }
