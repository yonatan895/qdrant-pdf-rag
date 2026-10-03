"""Contextual retrieval prefixes (issue #78).

An LLM-generated 1-2 sentence situating context per chunk, embedded WITH the
chunk. Active only when CONTEXTUAL_EMBED_ENABLED=true (default off); the
generating model is a cheap chat model configured separately from the
reasoning model (CONTEXT_LLM_BASE_URL / CONTEXT_LLM_MODEL) because per-chunk
gist generation is a short, high-volume workload — never an answer.

Cache discipline (issue #416): a cached context is reusable only when every
input that determined it is unchanged. `ContextBinding` is the ONE owner of
that identity: prompt template version, doc sha256, chunk id, the contextual
model name, the `max_chars` normalization cap, and a digest of the exact
messages (header + section + body) sent to the model. Records live in a JSONL
sidecar next to the --progress inventory, written by the parent (single
writer, no locking) and loaded once per parse worker. Same exact input still
makes zero LLM calls; any changed input is a miss that regenerates. Records
from before schema 2 (no model/input binding) and malformed records are
explicit misses, never errors, and the file is never rewritten or deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2

from mainframe_rag.config import Settings, bearer_auth_headers
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


# Sidecar record schema. Bumped only when the identity fields below change.
# Records without it (the pre-#416 `v`/`doc_sha256`/`chunk_id`/`context` shape)
# carry no model/input/cap binding and are treated as misses.
CONTEXT_CACHE_SCHEMA = 2


def input_digest(messages: list[ChatMessage]) -> str:
    """sha256 over the exact messages sent to the model (system prompt plus
    the header/section/body user turn), length-framed by JSON encoding."""
    payload = json.dumps(
        [[m.role, m.content] for m in messages], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_key(
    *,
    prompt_version: str,
    doc_sha256: str,
    chunk_id: str,
    model: str,
    max_chars: int,
    digest: str,
) -> str:
    """The single key constructor: every generation-relevant input rides it."""
    bound = json.dumps([prompt_version, doc_sha256, chunk_id, model, max_chars, digest])
    return f"ctx{CONTEXT_CACHE_SCHEMA}:{hashlib.sha256(bound.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ContextBinding:
    """Everything one document's contexts depend on besides the chunk itself.
    Builds the prompt, the cache key and the sidecar record from one place so
    the worker's load/hit path and the parent's write path cannot drift."""

    doc_sha256: str
    product: str | None
    version: str | None
    title: str
    model: str
    max_chars: int

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        doc_sha256: str,
        product: str | None,
        version: str | None,
        title: str,
    ) -> ContextBinding:
        return cls(
            doc_sha256=doc_sha256,
            product=product,
            version=version,
            title=title,
            model=settings.require_context_llm()[1],
            max_chars=settings.context_max_chars,
        )

    def messages(self, chunk: Chunk) -> list[ChatMessage]:
        return build_context_messages(
            product=self.product,
            version=self.version,
            doc_id=chunk.doc_id,
            title=self.title,
            heading_path=chunk.heading_path,
            body=chunk.text,
        )

    def key(self, chunk: Chunk) -> str:
        return cache_key(
            prompt_version=CONTEXT_PROMPT_VERSION,
            doc_sha256=self.doc_sha256,
            chunk_id=chunk.chunk_id,
            model=self.model,
            max_chars=self.max_chars,
            digest=input_digest(self.messages(chunk)),
        )

    def record(self, chunk: Chunk, context: str) -> dict[str, Any]:
        return {
            "schema": CONTEXT_CACHE_SCHEMA,
            "v": CONTEXT_PROMPT_VERSION,
            "doc_sha256": self.doc_sha256,
            "chunk_id": chunk.chunk_id,
            "model": self.model,
            "max_chars": self.max_chars,
            "input_sha256": input_digest(self.messages(chunk)),
            "context": context,
        }


def _record_key(obj: Any) -> tuple[str, str] | None:
    """(key, context) for a valid schema-2 record, else None. Strict types:
    a hand-edited or truncated record is a miss, never a wrong hit."""
    if not isinstance(obj, dict) or obj.get("schema") != CONTEXT_CACHE_SCHEMA:
        return None
    strs = [obj.get(f) for f in ("v", "doc_sha256", "chunk_id", "model", "input_sha256")]
    max_chars = obj.get("max_chars")
    context = obj.get("context")
    if not all(isinstance(x, str) and x for x in strs):
        return None
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        return None
    if not isinstance(context, str) or not context or len(context) > max_chars:
        return None
    version, doc_sha256, chunk_id, model, digest = strs
    key = cache_key(
        prompt_version=str(version),
        doc_sha256=str(doc_sha256),
        chunk_id=str(chunk_id),
        model=str(model),
        max_chars=max_chars,
        digest=str(digest),
    )
    return key, context


def load_context_cache(path: Path) -> dict[str, str]:
    """Last-wins merge of the sidecar JSONL. A corrupt line is skipped with a
    warning and regenerated on demand — the cache is disposable, failing a
    whole ingest over one bad line would be the wrong tradeoff. Legacy
    (pre-schema-2) records are counted and skipped: they cannot prove which
    model, input or cap produced them, so they are misses, left on disk."""
    entries: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries
    legacy = 0
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if (
            isinstance(obj, dict)
            and "schema" not in obj
            and {"v", "doc_sha256", "chunk_id"} <= obj.keys()
        ):
            legacy += 1
            continue
        parsed = _record_key(obj)
        if parsed is None:
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
        entries[parsed[0]] = parsed[1]
    if legacy:
        log.warning(
            json.dumps(
                {
                    "action": "context_cache_legacy_records_ignored",
                    "path": str(path),
                    "count": legacy,
                }
            )
        )
    return entries


def _lacks_final_newline(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            if fh.tell() == 0:
                return False
            fh.seek(-1, 2)
            return fh.read(1) != b"\n"
    except FileNotFoundError:
        return False


def append_context_entries(
    path: Path,
    binding: ContextBinding,
    chunks: list[Chunk],
    entries: dict[str, str],
) -> None:
    """Append-only merge (single parent writer — no locking needed). Matches
    the inventory file's append-only discipline. `entries` maps chunk id to
    context; each record is bound through `binding` so a later load can only
    hit for the identical model, input and cap."""
    if not entries:
        return
    by_id = {c.chunk_id: c for c in chunks}
    missing = [cid for cid in entries if cid not in by_id]
    if missing:
        raise ValueError(f"context entries for unknown chunk ids: {missing[:3]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if _lacks_final_newline(path):
            # A crash mid-append can leave a torn last line; start on a fresh
            # line so it cannot swallow the first record written now.
            fh.write("\n")
        for chunk_id, context in entries.items():
            fh.write(json.dumps(binding.record(by_id[chunk_id], context)) + "\n")


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

    @property
    def model(self) -> str:
        """Contextual model name bound into every cache key."""
        return self._settings.require_context_llm()[1]

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
            headers=bearer_auth_headers(self._settings.context_llm_api_key),
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
    binding = ContextBinding(
        doc_sha256=doc_sha256,
        product=product,
        version=version,
        title=title,
        model=client.model,
        max_chars=max_chars,
    )
    full: dict[str, str] = {}
    new: dict[str, str] = {}
    for chunk in chunks:
        key = binding.key(chunk)
        hit = cache.get(key)
        # A hit must already satisfy the current normalization policy.
        if hit and normalize_context(hit, max_chars) == hit:
            full[chunk.chunk_id] = hit
            continue
        context = normalize_context(client.complete(binding.messages(chunk)), max_chars)
        if not context:
            raise RuntimeError(f"context model returned empty gist for chunk {chunk.chunk_id}")
        full[chunk.chunk_id] = context
        new[chunk.chunk_id] = context
        cache[key] = context
    return full, new
