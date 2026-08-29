"""Reasoning-model chat for /v1/answer.

Contract (architecture.md 4.6): /v1/answer must use the reasoning/thinking
model — never a cheap chat model. The only model this module can call is
settings.llm_model_reasoning; there is deliberately no other model knob.
"""

from __future__ import annotations

import re

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import SearchHit

FENCE_RE = re.compile(r"```([a-zA-Z]*)\n(.*?)```", re.DOTALL)
CITATIONS_BLOCK_RE = re.compile(r"(?:\n|^)\s*Citations:\s*\n(?:[ \t]*[-*•]?[ \t]*[^\n]+\n?)*", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a mainframe operations expert (z/OS, CICS, Db2, IMS, JES2/3, RACF, "
    "z/VM, VTAM). Answer operational questions. Rules:\n"
    "1. Only assert what the supplied excerpts support.\n"
    "2. If manuals disagree between versions, say which version each statement "
    "comes from.\n"
    "3. If the excerpts do not answer the question, say so explicitly.\n"
    "4. When you propose JCL, REXX, or operator steps, put them in a fenced code "
    "block and tie them to a citation. Scripts are examples, not "
    "production-ready without review.\n"
    "5. You MUST end your reply with a 'Citations:' section listing the exact citation strings of the excerpts used, for example:\n"
    "Citations:\n"
    "SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17\n"
)


def build_messages(
    query: str,
    hits: list[SearchHit],
    product: str | None = None,
    version: str | None = None,
    splunk_context: str | None = None,
    max_context_chars: int = 8000,
    max_chunk_chars: int = 3000,
) -> list[dict[str, str]]:
    chunks: list[str] = []
    total_chars = 0
    for i, hit in enumerate(hits, 1):
        text = hit.text.strip()
        if len(text) > max_chunk_chars:
            text = text[:max_chunk_chars].rstrip() + "\n... [truncated]"
        chunk_repr = f"[{i}] {hit.cite}\n{text}"
        if total_chars + len(chunk_repr) > max_context_chars and chunks:
            remaining = max_context_chars - total_chars
            if remaining > 200:
                text = text[:remaining].rstrip() + "\n... [truncated]"
                chunks.append(f"[{i}] {hit.cite}\n{text}")
            break
        chunks.append(chunk_repr)
        total_chars += len(chunk_repr)

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

    example_cite = (
        hits[0].cite
        if hits
        else "SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17"
    )
    parts.append(
        "Please answer based strictly on the retrieved manual excerpts above and conclude with the 'Citations:' section copying the exact citation line for each excerpt used, for example:\n"
        f"Citations:\n{example_cite}"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def assert_reasoning_model(settings: Settings) -> tuple[str, str]:
    """Fail closed: no reasoning model configured, no LLM call. Returns
    (base_url, model) so callers never re-read the Optional settings fields."""
    if not settings.llm_base_url:
        raise RuntimeError("LLM_BASE_URL is unset; /v1/answer cannot run.")
    return settings.llm_base_url, settings.require_reasoning_model()


class HttpxLLMClient:
    """LLMClient implementation: the reasoning model only — deliberately no
    other model knob (architecture.md 4.6). LLM env fails closed at request
    time (assert_reasoning_model, called by /v1/answer before retrieval);
    startup fail-fast covers the embed path (PR D).
    Owns its own connection pool with the long answer timeout (do NOT share
    the embed client's short timeout). No retries: /v1/answer is a single
    shot — a retry would re-ask a reasoning model that may already be
    thinking, and answers are not idempotent (issue #20 PR C)."""

    def __init__(self, settings: Settings, client: httpx2.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def _http(self) -> httpx2.Client:
        if self._client is None:
            # retries=0: connection blips surface as errors, never re-issued.
            self._client = httpx2.Client(
                timeout=self._settings.answer_timeout_s,
                transport=httpx2.HTTPTransport(retries=0),
            )
        return self._client

    def close(self) -> None:
        # Deliberately does not null the client: a post-shutdown chat() must
        # fail loudly on the closed pool, never silently rebuild one.
        if self._client is not None:
            self._client.close()

    def chat(self, messages: list[dict[str, str]]) -> str:
        base_url, model = assert_reasoning_model(self._settings)
        resp = self._http().post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model, "messages": messages},
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"])


def parse_answer(
    content: str,
    allowed_citations: set[str],
    ordered_cites: list[str] | None = None,
) -> dict:
    """Split model output into answer, validated citations, optional script.

    The `citations` list and the answer body are filtered to the hit set.
    `script` (fenced block) is code and deliberately passes through
    unvalidated — stripping citation-looking lines would corrupt examples.
    Documented behavior, pinned by test (issue #20 PR C)."""
    from mainframe_rag.agent.cites import (
        strip_unauthorized_citations,
        valid_citations,
    )

    fence = FENCE_RE.search(content)
    script: str | None = None
    if fence:
        lang = fence.group(1).strip().lower()
        fence_content = fence.group(2).strip()
        body_without_fence = content.replace(fence.group(0), "").strip()
        # If explicitly a code script (jcl, rexx, sh, etc.) or there is substantial non-fenced body text
        if lang in ("jcl", "rexx", "sh", "bash", "python", "yaml", "json") or len(body_without_fence) > 50:
            script = fence_content
            content_for_body = body_without_fence
        else:
            # Whole answer was wrapped in markdown code fence
            content_for_body = fence_content
    else:
        content_for_body = content

    citations = valid_citations(content, allowed_citations)
    citations_inferred = False
    inferred_indices: list[int] = []

    if not citations and ordered_cites:
        # Strictly match bracketed numbers like [1], [2], [1, 2] corresponding to [{i}] prompt excerpts.
        # Parentheses (e.g. "z/OS (3.1)", "(2)", "APARs (1, 2)") are ignored to avoid false inference.
        for match in re.finditer(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]", content):
            for num_str in re.findall(r"\b\d+\b", match.group(1)):
                idx = int(num_str) - 1
                if 0 <= idx < len(ordered_cites):
                    cite = ordered_cites[idx]
                    if cite in allowed_citations and cite not in citations:
                        citations.append(cite)
                        inferred_indices.append(idx + 1)
                        citations_inferred = True

    # Strip Citations: block from anywhere in the text (top, middle, or bottom)
    body = CITATIONS_BLOCK_RE.sub("\n", content_for_body).strip()
    if not body and script:
        body = script
        script = None

    # A fabricated full-format cite quoted mid-answer must not reach the
    # client either (issue #20 PR C: no cite outside the hit set).
    body = strip_unauthorized_citations(body, allowed_citations)
    return {
        "answer": body.strip(),
        "citations": citations,
        "script": script,
        "citations_inferred": citations_inferred,
        "inferred_indices": inferred_indices,
    }
