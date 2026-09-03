"""Contextual retrieval prefixes (issue #78).

An LLM-generated 1-2 sentence situating context per chunk, embedded WITH the
chunk. Active only when CONTEXTUAL_EMBED_ENABLED=true (default off); the
generating model is a cheap chat model configured separately from the
reasoning model (CONTEXT_LLM_BASE_URL / CONTEXT_LLM_MODEL) because per-chunk
gist generation is a short, high-volume workload — never an answer.

Cache discipline: contexts are keyed by (prompt template version, doc sha256,
chunk id) in a JSONL sidecar file next to the --progress inventory.
Parse workers load the file once per worker and return full per-doc mappings;
the parent appends them (single writer, no locking). Chunk ids are
deterministic UUID5, so re-ingesting unchanged docs makes zero LLM calls —
and the inventory sha skip means unchanged docs never even reach a worker.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage

if TYPE_CHECKING:
    from mainframe_rag.ingest.chunk import Chunk

log = logging.getLogger("ingest")

# Prompt template version. Bumped ONLY with the template text below; the
# version rides the cache key, so a template change invalidates every cached
# context automatically (stale contexts would embed under a new semantic).
#
# v2 (issue #78 reviewer sequence): v1 asked the model to name the manual and
# section, which duplicated the already-indexed header and invited
# instruction echo ("The user wants me to...") on small models. v2 forbids
# restating anything the header carries and demands only the passage gist.
# Wording is deliberately imperative with no enumerated list — an enumerated
# "facts, parameters, values, actions" draft made the model mirror it back
# as section headers ("Key Facts:", "Parameters:", ...). Validated live
# against Qwen2.5-0.5B-Instruct on message/syntax/narrative passages.
CONTEXT_PROMPT_VERSION = "v2"

CONTEXT_SYSTEM_PROMPT = (
    "In one or two sentences, say what the passage states. "
    "Never repeat the manual title, document ID, or section path. "
    "No headings, lists, or preamble."
)

# Server-side completion cap: a 1-2 sentence gist is ~150 tokens; 256 leaves
# margin without letting a rambling model burn time. The deterministic
# settings.context_max_chars truncation below is the real bound.
MAX_COMPLETION_TOKENS = 256


def build_context_messages(
    *,
    product: str | None,
    version: str | None,
    doc_id: str,
    title: str,
    heading_path: str,
    body: str,
) -> list[ChatMessage]:
    """Prompt for one chunk's situating context. Header fields mirror
    build_embed_text so the model sees exactly what the header-only
    baseline embeds (the ablation in the issue compares against this)."""
    header = " ".join(p for p in (product, version, doc_id) if p)
    user = "\n".join(
        p
        for p in (
            f"Manual: {title} ({header})" if header else f"Manual: {title}",
            f"Section: {heading_path}" if heading_path else None,
            f"Passage:\n{body}",
        )
        if p
    )
    return [
        ChatMessage(role="system", content=CONTEXT_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user),
    ]


def normalize_context(text: str, max_chars: int) -> str:
    """Collapse whitespace and enforce the deterministic char cap so the
    embed-window budget pin stays provable for any model output."""
    collapsed = " ".join(text.split())
    return collapsed[:max_chars].rstrip()


def cache_key(doc_sha256: str, chunk_id: str) -> str:
    return f"{CONTEXT_PROMPT_VERSION}:{doc_sha256}:{chunk_id}"


def load_context_cache(path: Path) -> dict[str, str]:
    """Last-wins merge of the sidecar JSONL. A corrupt line is skipped with a
    warning and regenerated on demand — the cache is disposable, failing a
    whole ingest over one bad line would be the wrong tradeoff."""
    entries: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            key = f"{obj['v']}:{obj['doc_sha256']}:{obj['chunk_id']}"
            context = obj["context"]
        except (ValueError, KeyError, TypeError):
            log.warning(
                json.dumps(
                    {
                        "action": "context_cache_skip_line",
                        "path": str(path),
                        "lineno": lineno,
                    }
                )
            )
            continue
        if isinstance(context, str) and context:
            entries[key] = context
    return entries


def append_context_entries(path: Path, doc_sha256: str, entries: dict[str, str]) -> None:
    """Append-only merge (single parent writer — no locking needed). Matches
    the inventory file's append-only discipline."""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for chunk_id, context in entries.items():
            fh.write(
                json.dumps(
                    {
                        "v": CONTEXT_PROMPT_VERSION,
                        "doc_sha256": doc_sha256,
                        "chunk_id": chunk_id,
                        "context": context,
                    }
                )
                + "\n"
            )


def resolve_cache_path(settings: Settings, progress: Path) -> Path:
    """Explicit CONTEXT_CACHE_PATH wins; otherwise a sibling of the
    --progress inventory (`inventory.jsonl` -> `inventory.contexts.jsonl`)."""
    if settings.context_cache_path:
        return Path(settings.context_cache_path)
    name = progress.name
    stem = name[: -len(progress.suffix)] if progress.suffix else name
    return progress.with_name(f"{stem}.contexts.jsonl")


class ContextLLMClient:
    """Cheap chat-model client for gist generation. Sync POST, own timeout,
    no retries — same no-retry discipline as every other outbound call.
    Accepts an injected client for hermetic tests."""

    def __init__(self, settings: Settings, client: httpx2.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def _http(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(
                timeout=self._settings.context_llm_timeout_s,
                transport=httpx2.HTTPTransport(retries=self._settings.http_connect_retries),
            )
        return self._client

    def complete(self, messages: list[ChatMessage]) -> str:
        base_url, model = self._settings.require_context_llm()
        resp = self._http().post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": [m.model_dump() for m in messages],
                "temperature": 0.0,
                "max_tokens": MAX_COMPLETION_TOKENS,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        return normalize_context(str(content), self._settings.context_max_chars)


def generate_contexts(
    chunks: list[Chunk],
    *,
    doc_sha256: str,
    product: str | None,
    version: str | None,
    title: str,
    client: ContextLLMClient,
    cache: dict[str, str],
    max_chars: int,
) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (full, new): full maps every chunk id to its context (cache
    hits + fresh generations) for the payload; new holds only fresh
    generations for the parent to append to the sidecar file. Sequential
    calls — worker-count parallelism is the throughput knob. A generation
    failure raises: the worker traps it into an error record, failing the
    doc loudly instead of embedding a silent empty prefix."""
    full: dict[str, str] = {}
    new: dict[str, str] = {}
    for chunk in chunks:
        key = cache_key(doc_sha256, chunk.chunk_id)
        hit = cache.get(key)
        if hit:
            full[chunk.chunk_id] = hit
            continue
        messages = build_context_messages(
            product=product,
            version=version,
            doc_id=chunk.doc_id,
            title=title,
            heading_path=chunk.heading_path,
            body=chunk.text,
        )
        context = normalize_context(client.complete(messages), max_chars)
        if not context:
            raise RuntimeError(f"context model returned empty gist for chunk {chunk.chunk_id}")
        full[chunk.chunk_id] = context
        new[chunk.chunk_id] = context
        cache[key] = context
    return full, new
