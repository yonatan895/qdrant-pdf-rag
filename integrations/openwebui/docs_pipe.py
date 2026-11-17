"""
title: Rosetta
author: mainframe-rag
version: 0.1.0
required_open_webui_version: 0.12.3
license: Apache-2.0
description: The documentation corpus through the agent's /v1/chat, with its validated citations, abstention and verification state.
"""

# Open WebUI Pipe (issue #1072): a stateless translator between Open WebUI and
# the agent's streaming POST /v1/chat. No tools, no model calls, no network
# call other than the configured agent URL, no logging. The header above is
# Open WebUI's function frontmatter; it deliberately has no `requirements:`
# line, because that would make Open WebUI pip-install packages at load time.
# Only httpx and pydantic are imported; both ship with Open WebUI.
# Contract and install procedure: docs/agent.md#openwebui-docs-pipe.

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, NamedTuple

import httpx
from pydantic import BaseModel, Field

OPEN_WEBUI_VERSION = "0.12.3"
CHAT_PATH = "/v1/chat"
ERROR_BODY_MAX_BYTES = 65536

Emitter = Callable[[dict[str, Any]], Awaitable[Any]]
# Anything Open WebUI could render as a link: a scheme (http:, javascript:,
# data:...), a bare `http` prefix, a protocol-relative `//` or `www.`.
_URL_LIKE = re.compile(r"\s*(?:http|www\.|//|[a-z][a-z0-9+.\-]*:)", re.IGNORECASE)

# Fixed user-visible texts. The agent's error `message` and any upstream body
# are never shown: only the stable `code` selects one of these.
_REJECTED = ("The documentation service rejected the request. Ask an administrator to check "
             "the Rosetta Pipe configuration.")
ERROR_MESSAGES: dict[str, str] = {
    "invalid_request": "The request was refused as invalid. Shorten or rephrase the question "
                       "and retry.",
    "prompt_budget_exceeded": "The question and conversation are too long for the model. "
                              "Start a new chat or shorten the question.",
    "upstream_error": "The documentation service could not complete this request. "
                      "Retry in a moment.",
    "model_timeout": "The reasoning model did not respond in time. Retry in a moment.",
    "deadline_exceeded": "The request took longer than the service allows. "
                         "Retry, or ask a narrower question.",
    "overloaded": "The documentation service is at capacity. Retry in a moment.",
    "not_configured": "The documentation service has no reasoning model configured. "
                      "Contact the operators.",
    "representation_unavailable": "The documentation index is not available right now. "
                                  "Retry later.",
    "internal": "The documentation service hit an internal error. Retry; if it persists, "
                "contact the operators.",
    "not_found": _REJECTED,
    "method_not_allowed": _REJECTED,
    "http_error": _REJECTED,
    "unsupported_reasoning_effort": "This deployment does not offer this answer depth. "
                                    "Choose another Rosetta model and retry.",
    # A pasted image the agent refused (#1287): one text for every reason
    # (type, size, pixels, count, history); the code alone selects it.
    "unsupported_image": "Rosetta could not use this image. Paste a screenshot of an accepted "
                         "type (PNG, JPEG or WebP by default), within the size and count "
                         "limits, in your latest message and retry.",
}
UNREACHABLE = ("The documentation service could not be reached. Retry; if it persists, "
               "contact the operators.")
TRUNCATED = ("The answer stream ended before it completed. "
             "Treat any text above as incomplete and retry.")
NO_TEXT_TURN = "Ask a question in plain text; attachments and images are not supported."
TRIMMED = "Earlier messages in this chat were not sent"
# The agent reports `message_omitted_chars` > 0 when the question was too
# long for the model window and its middle was left out of the prompt.
SHORTENED = "Your message was too long for the model; its middle part was not read"
DEFAULT_MAX_BODY_CHARS = 131072  # the agent's Settings.chat_max_body_chars default
# The last message alone exceeds the request-size guard: the one length
# refusal (a long paste under it is answered). States the limit, never text.
TOO_LONG = ("This message is longer than the documentation service accepts "
            "({limit} characters). Shorten it and retry.")
NO_EXCERPT = "Excerpt text is not available in this response."
# House best-practice guides (#1276) are AI-drafted documents whose doc ids
# start with this prefix (HOUSE-AI-DRAFT-<topic>, later HOUSE-REVIEWED-<topic>):
# a naming convention of the corpus, not vendor parsing. A validated citation
# of one is named in its source entry and in the accepted banner.
HOUSE_DOC_PREFIX = "HOUSE-"
HOUSE_NOTE = "house guidance"
TASK_REPLY = "Docs question"

# Answer depths (#1255): the manifold entries Open WebUI shows as models,
# `<Function id>.<id>` (v0.12.3 functions.py get_function_models). The id is
# stored in chats and model grants, so it never changes; the name is what
# users see. Each entry sends its id as the agent's symbolic `reasoning_depth`
# (#1289), never a literal effort: the agent resolves it from its own
# LLM_REASONING_EFFORTS (quick the lowest offered effort, thorough the highest,
# standard its configured simple/complex default), so a site without xhigh
# needs no repackage. The first entry is the default: DEFAULT_MODELS in the
# chart names it, and an unknown entry or a pre-#1255 chat's single-model id
# gets it (no field sent). Ids, names and the default: maintainer decision
# 2026-11-16 (#1255); some thorough answers may reach the 270 s request
# deadline (the site eval measures that).
class Depth(NamedTuple):
    id: str
    name: str


DEPTHS: tuple[Depth, ...] = (
    Depth("standard", "Rosetta"),
    Depth("quick", "Rosetta \N{EN DASH} quick"),
    Depth("thorough", "Rosetta \N{EN DASH} thorough"),
)
_DEPTH_IDS = frozenset(depth.id for depth in DEPTHS)

WORKING = "Searching the documentation…"
FAILED = "Request failed"
STATE_LABELS: dict[str, str] = {
    "insufficient_evidence": "Insufficient evidence — not operational guidance",
    "unverified_draft": "Unverified draft — no eligible citation",
    "generation_incomplete": "Incomplete generation — output was cut off",
}
UNKNOWN_STATE = "Unverified — verification state unknown"

# The model's own citations section (answer prompt rule 7) is unvalidated and
# the agent appends its validated list after it, so the Pipe hides the model's
# section (issue #1098). The header and list-marker patterns are copies of
# src/mainframe_rag/agent/cites.py CITATIONS_HEADER_RE, _CITATION_ALIAS_HEADER_RE
# and _MARKER_RE (the Pipe cannot import the agent); a test pins them equal.
_CITATIONS_HEADER = re.compile(
    r"^[^\S\r\n]*(?:#{1,6}[^\S\r\n]*)?[*_`]*Citations?[*_`]*:[*_`]*[^\S\r\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ALIAS_HEADER = re.compile(
    r"^\s*(?:#{1,6}\s*)?[*_`]*(?:Sources?|References?)[*_`]*:[*_`]*\s*$",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+|\[\d+\]:?\s*)+")
# Every citation shape cites.py accepts is `<id> <title>, <heading>, p. <page>`:
# a section line with a `, ` gap before a `, p. ` tail is a citation (a superset
# of the agent's shape test, which also needs the supplied doc ids).
_PAGE_TAIL = re.compile(r",\s+p\.\s")
_COMMA_GAP = re.compile(r",\s+.")
# Any prefix of a header line matches this with a prefix of a header word, or
# the whole word and a header tail. Only such a line start is held.
_HEADER_START = re.compile(
    r"[^\S\n]*(?:#+[^\S\n]*)?[*_`]*([A-Za-z]*)([*_`]*:?[*_`]*[^\S\n]*)"
)
_HEADER_WORDS = ("citation", "citations", "source", "sources", "reference", "references")
HEADER_MAX_CHARS = 64  # a longer line is never treated as a header
HOLD_MAX_CHARS = 2048  # a longer undecided line inside a section is prose
_FENCES = ("```", "~~~")
INFERRED = "Inferred citations — bracket provenance, not grounding"
SCRIPT_REVIEW = "Human review required — draft script, not certified executable"

# Open WebUI ratings (#1255): Open WebUI keeps no message field a rating
# Action can read except the content, so with the chart's ratings switch on
# (RAG_ANSWER_REFERENCE=true) a completed answer ends with this line carrying
# the agent's request id (its chunk id is `chatcmpl-<id>`). The rating Action
# (rating_action.py) reads it back with the same pattern; a test pins the two.
# The visible line and its wording: maintainer decision 2026-11-16 (#1255).
REFERENCE_ENV = "RAG_ANSWER_REFERENCE"
REFERENCE_LINE = "\n\nAnswer reference: `{request_id}`"
REFERENCE_RE = re.compile(r"\n\nAnswer reference: `([0-9a-f]{12})`[ \t\r\n]*\Z")
_CHUNK_ID = re.compile(r"chatcmpl-([0-9a-f]{12})\Z")
_REQUEST_ID_KEY = "__request_id"  # set on `final` by _deltas, never by the agent

# Pasted images (#1287), temporary chats only. On only when the chart sets
# RAG_CHAT_IMAGES=true (read per call, like REFERENCE_ENV); default off. Only
# the last user turn's `data:image/` URLs are forwarded; earlier turns are
# always text. The agent's transcript of each image (final-frame
# `image_reads`) is shown in a collapsed block before the reference line; on
# the next turn build_request moves it back into the user turn it came from,
# so no earlier image is ever re-sent and the Pipe stays stateless. Every
# user-visible text below is draft wording for maintainer review.
IMAGES_ENV = "RAG_CHAT_IMAGES"
DEFAULT_MAX_IMAGE_BYTES = 5242880  # the agent's Settings.chat_image_max_bytes default
DEFAULT_MAX_IMAGES = 3  # the agent's Settings.chat_image_max_count default
# The agent counts an image-only turn as its fixed question (agent images.py
# IMAGE_ONLY_QUESTION, 54 characters); a test pins the two.
IMAGE_ONLY_QUESTION_CHARS = 54
IMAGES_OFF = ("Rosetta does not read images in this deployment. Type the question, or the "
              "text from the screen, and retry.")
IMAGE_UNUSABLE = ("Rosetta reads pasted images only in a temporary chat. Paste the image into "
                  "a temporary chat, or type the text from the screen, and retry.")
TOO_MANY_IMAGES = "Attach at most {limit} images to one message and retry."
IMAGE_TOO_LARGE = "Each image must be at most {limit} MB. Paste a smaller screenshot and retry."
IMAGE_READ_FAILED = "An image could not be read, so the search did not use it"
EARLIER_IMAGE = "An earlier image was not re-sent"
IMAGE_READS_SUMMARY = "What Rosetta read from your image"
IMAGE_CARRIED = "[Image {index}, as read by the model]: {text}"
# The block opens as Open WebUI v0.12.3's details tokenizer requires
# (src/lib/utils/marked/extension.ts: `<details>` then a newline, then
# `<summary>...</summary>` and a newline). Each transcript sits in a backtick
# fence longer than any backtick run in it, with `&`, `<` and `>` escaped: that
# tokenizer counts raw `<details`/`</details>` anywhere (fences included) and
# html-decodes the block before rendering it, and processDetails rewrites
# typed details in history, so no transcript can close, open or retype the
# block, and the user still sees the exact text as code (never links or HTML).
_READS_OPEN = f"\n\n<details>\n<summary>{IMAGE_READS_SUMMARY}</summary>\n"
_READS_CLOSE = "</details>"
_READ_HEAD = re.compile(r"\n\*\*Image ([1-9][0-9]?)\*\*\n\n(`{3,})\n")
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
# Open WebUI's add_file_context (utils/middleware.py) block: a whole text
# part, or the prefix of string content. Only that exact shape is removed.
_ATTACHED_FILES = re.compile(r"<attached_files>\n.*?\n</attached_files>\n\n", re.DOTALL)


class _Failure(Exception):
    """A fixed user-visible failure; carries the fixed text, never upstream text."""

    def __init__(self, text: str, *, incomplete: bool) -> None:
        super().__init__(text)
        self.text = text
        self.incomplete = incomplete


def _error_text(code: object) -> str:
    return ERROR_MESSAGES.get(code, UNREACHABLE) if isinstance(code, str) else UNREACHABLE


def _envelope_text(raw: bytes) -> str:
    """Fixed text for an agent error body: only a string `code` is read."""
    try:
        body = json.loads(raw)
    except ValueError:
        return UNREACHABLE
    return _error_text(body.get("code")) if isinstance(body, dict) else UNREACHABLE


def _valve(valves: object, name: str) -> str:
    value = valves.get(name) if isinstance(valves, dict) else getattr(valves, name, None)
    return value.strip() if isinstance(value, str) else ""


def _is_task(task: object, metadata: object, body: dict[str, Any]) -> bool:
    """A background task call (title, tags, follow-ups, summaries...). Open
    WebUI passes the task name as `__task__` (from the request metadata);
    the metadata and body keys are checked too, so a task is never mistaken
    for a docs question if the reserved argument is not populated."""
    body_metadata = body.get("metadata")
    return any((
        task,
        isinstance(metadata, dict) and metadata.get("task"),
        body.get("task"),
        isinstance(body_metadata, dict) and body_metadata.get("task"),
    ))


def _text(content: object) -> str:
    """Plain text of a message: a string, or the text parts of an Open WebUI
    content list joined by newlines (image and file parts dropped). Open
    WebUI's `<attached_files>` block (a whole text part, or a string's
    prefix) is file metadata, not question text, and is removed."""
    if isinstance(content, str):
        if match := _ATTACHED_FILES.match(content):
            content = content[match.end():]
        return content if content.strip() else ""
    if not isinstance(content, list):
        return ""
    parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    joined = "\n".join(t for t in parts if isinstance(t, str) and t.strip()
                       and not _ATTACHED_FILES.fullmatch(t))
    return joined if joined.strip() else ""


def _image_urls(content: object) -> list[object]:
    """The URL of each `image_url` part of a content list (None when absent)."""
    if not isinstance(content, list):
        return []
    urls: list[object] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            image = part.get("image_url")
            urls.append(image.get("url") if isinstance(image, dict) else image)
    return urls


def _decoded_bytes(url: str) -> int:
    """A base64 data URL's decoded size by length arithmetic (nothing is
    decoded); 0 for any other shape, which the agent judges itself."""
    header, comma, payload = url.partition(",")
    if not comma or not header.endswith(";base64"):
        return 0
    padding = min(len(payload) - len(payload.rstrip("=")), 2)
    return len(payload) * 3 // 4 - padding


def _escape(text: str) -> str:
    for raw, escaped in _ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _unescape(text: str) -> str:
    for raw, escaped in reversed(_ESCAPES):
        text = text.replace(escaped, raw)
    return text


def _image_reads_block(final: dict[str, Any]) -> str:
    """The collapsed block of the agent's image transcripts (`image_reads`),
    or "" when the final frame has none."""
    reads = final.get("image_reads")
    items = [(r["index"], r["text"]) for r in reads if isinstance(r, dict)
             and type(r.get("index")) is int and 1 <= r["index"] <= 99
             and isinstance(r.get("text"), str) and r["text"].strip()
             ] if isinstance(reads, list) else []
    if not items:
        return ""
    parts = [_READS_OPEN]
    for index, text in items:
        runs = [len(run) for run in re.findall("`+", text)]
        fence = "`" * max(3, max(runs, default=0) + 1)
        parts.append(f"\n**Image {index}**\n\n{fence}\n{_escape(text)}\n{fence}\n")
    parts.append(_READS_CLOSE)
    return "".join(parts)


def _split_reads(text: str) -> tuple[str, list[tuple[int, str]]]:
    """An answer's text without its trailing image-reads block, and the
    transcripts the block carries ([] when there is no block or it does not
    parse, for instance after an edit). Open WebUI trims history content, so
    trailing whitespace is tolerated."""
    stripped = text.rstrip()
    start = stripped.rfind(_READS_OPEN)
    if start < 0 or not stripped.endswith(_READS_CLOSE):
        return text, []
    body = stripped[start + len(_READS_OPEN):-len(_READS_CLOSE)]
    reads: list[tuple[int, str]] = []
    pos = 0
    while pos < len(body):
        head = _READ_HEAD.match(body, pos)
        close = body.find(f"\n{head.group(2)}\n", head.end()) if head else -1
        if head is None or close < 0:
            reads = []
            break
        if transcript := _unescape(body[head.end():close]):
            reads.append((int(head.group(1)), transcript))
        pos = close + len(head.group(2)) + 2
    return stripped[:start], reads


def _build(messages: object, user_valves: object, *, images_on: bool, max_images: int,
           max_image_bytes: int) -> tuple[dict[str, Any] | str, bool]:
    """(the agent request body, or the fixed text to show instead of a
    call; whether an earlier image was dropped without its transcript)."""
    items = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    if not items or items[-1].get("role") != "user":
        return NO_TEXT_TURN, False
    last = items[-1].get("content")
    question, urls = _text(last), _image_urls(last)
    content: str | list[dict[str, Any]] = question
    if urls:
        if not images_on:
            return IMAGES_OFF, False
        if not all(isinstance(u, str) and u.startswith("data:image/") for u in urls):
            return IMAGE_UNUSABLE, False
        data_urls = [u for u in urls if isinstance(u, str)]
        if len(data_urls) > max_images:
            return TOO_MANY_IMAGES.format(limit=max_images), False
        if any(_decoded_bytes(u) > max_image_bytes for u in data_urls):
            return IMAGE_TOO_LARGE.format(limit=f"{max_image_bytes / 1048576:.3g}"), False
        content = ([{"type": "text", "text": question}] if question else []) + [
            {"type": "image_url", "image_url": {"url": u}} for u in data_urls]
    elif not question:
        return NO_TEXT_TURN, False
    turns: list[dict[str, Any]] = []
    dropped = False
    for i, message in enumerate(items[:-1]):
        role, text = message.get("role"), _text(message.get("content"))
        if role == "assistant":
            # The reference line is display state, not part of the answer.
            text = REFERENCE_RE.sub("", text)
            if i and items[i - 1].get("role") == "user" and _image_urls(
                    items[i - 1].get("content")):
                text = _split_reads(text)[0]
        elif role == "user" and (count := len(_image_urls(message.get("content")))):
            # An earlier image is never re-sent: its transcript, carried in
            # the answer that follows it, takes its place.
            following = items[i + 1]
            reads = (_split_reads(REFERENCE_RE.sub("", _text(following.get("content"))))[1]
                     if following.get("role") == "assistant" else [])
            dropped = dropped or not set(range(1, count + 1)) <= {n for n, _ in reads}
            carried = [IMAGE_CARRIED.format(index=n, text=t) for n, t in reads]
            text = "\n\n".join([text, *carried] if text else carried)
        if role in ("user", "assistant") and text:
            turns.append({"role": role, "content": text})
    turns.append({"role": "user", "content": content})
    body: dict[str, Any] = {"messages": turns, "stream": True}
    for name in ("product", "version"):
        if value := _valve(user_valves, name):
            body[name] = value
    return body, dropped


def build_request(messages: object, user_valves: object) -> dict[str, Any] | None:
    """The agent ChatRequest body: user/assistant turns as plain text
    (system prompts, file parts, tool turns and every other Open WebUI field
    are dropped), `stream: true`, and product/version only when the user set
    them. The last message is the question: None, so no agent call, unless
    it is a user turn with text, or, with images on (RAG_CHAT_IMAGES=true),
    usable images; its images are its only content parts. An earlier turn is
    never asked in its place, and its images are replaced by their carried
    transcripts. Limits are the agent's defaults; the Pipe applies its valves."""
    built, _dropped = _build(messages, user_valves,
                             images_on=os.environ.get(IMAGES_ENV) == "true",
                             max_images=DEFAULT_MAX_IMAGES, max_image_bytes=DEFAULT_MAX_IMAGE_BYTES)
    return built if isinstance(built, dict) else None


def selected_depth(model: object) -> str | None:
    """The agent `reasoning_depth` for the selected model id
    `<Function id>.<depth>`; None (no field: the agent default) for an unknown
    depth or an id without one, such as the single-model id chats stored
    before #1255."""
    if not isinstance(model, str) or "." not in model:
        return None
    depth = model.split(".", 1)[1]
    return depth if depth in _DEPTH_IDS else None


def _chars(content: str | list[dict[str, Any]]) -> int:
    """Characters of one turn as the agent counts them: a content list is its
    text parts joined by newlines, and an image-only turn is the agent's
    fixed question."""
    if isinstance(content, str):
        return len(content)
    texts = [p["text"] for p in content if p.get("type") == "text"]
    if not texts:
        return IMAGE_ONLY_QUESTION_CHARS
    return sum(len(t) for t in texts) + len(texts) - 1


def fit_request(request: dict[str, Any], max_chars: int) -> tuple[dict[str, Any] | None, bool]:
    """Fit a built request to the agent's body cap, counted as the agent counts
    it (chat_body_chars): message characters plus product plus version. Drops
    the oldest turns first and then any leading assistant turn, so the history
    never starts with an answer to a dropped question. The last turn is always
    kept. Returns (request, trimmed); (None, False) when the last turn alone
    does not fit, so there is no call to make. A content list (a last turn
    with images) counts its text only, as the agent does; images have their
    own limits."""
    turns: list[dict[str, Any]] = request["messages"]
    budget = max_chars - sum(len(request.get(name, "")) for name in ("product", "version"))
    if _chars(turns[-1]["content"]) > budget:
        return None, False
    total = sum(_chars(t["content"]) for t in turns)
    start = 0
    while start < len(turns) - 1 and (total > budget or turns[start]["role"] != "user"):
        total -= _chars(turns[start]["content"])
        start += 1
    if start == 0:
        return request, False
    return {**request, "messages": turns[start:]}, True


def _plain(value: object) -> object:
    """A display name Open WebUI cannot render as a link."""
    if isinstance(value, str) and _URL_LIKE.match(value):
        return "Citation: " + value
    return value


def _source_events(final: dict[str, Any]) -> list[dict[str, Any]]:
    """One Open WebUI `source` event per validated citation; the document is
    the excerpt text the agent supplied under that citation, never a search."""
    citations = final.get("citations")
    if not isinstance(citations, list):
        return []
    supplied = [e for e in final.get("supplied_evidence") or [] if isinstance(e, dict)]
    base = "inferred, not grounding" if final.get("citations_inferred") is True else "source-matched"
    # The house mark rides only the accepted banner's condition, like the banner.
    mark_house = _source_matched(final)
    events: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, str):
            continue
        matches = [e for e in supplied if e.get("citation") == citation]
        house = mark_house and any(_is_house(e) for e in matches)
        label = f"{base}, {HOUSE_NOTE}" if house else base
        documents = [str(e.get("text", "")) for e in matches] or [NO_EXCERPT]
        metadata: list[dict[str, Any]] = [
            {"source": _plain(citation), "name": f"[{e.get('index')}] {_plain(citation)}",
             "doc_id": _plain(e.get("doc_id")), "page_label": e.get("page_label"),
             "product": e.get("product"), "version": e.get("version"), "label": label}
            for e in matches
        ] or [{"source": _plain(citation), "name": _plain(citation), "label": label}]
        doc_id = matches[0].get("doc_id") if matches else None
        name = _plain(citation)
        events.append({"type": "source", "data": {
            "source": {"name": f"{name} ({HOUSE_NOTE})" if house else name,
                       "id": _plain(doc_id if isinstance(doc_id, str) else citation)},
            "document": documents,
            "metadata": metadata,
        }})
    return events


def _is_house(excerpt: dict[str, Any]) -> bool:
    doc_id = excerpt.get("doc_id")
    return isinstance(doc_id, str) and doc_id.startswith(HOUSE_DOC_PREFIX)


def _cites_house(final: dict[str, Any], citations: list[Any]) -> bool:
    """A validated citation names a supplied excerpt of a house guide."""
    supplied = [e for e in final.get("supplied_evidence") or [] if isinstance(e, dict)]
    return any(_is_house(e) and e.get("citation") in citations for e in supplied)


def _source_matched(final: dict[str, Any]) -> bool:
    """`accepted` with explicit (non-inferred) validated citations."""
    citations = final.get("citations")
    return (final.get("verification_state") == "accepted" and isinstance(citations, list)
            and bool(citations) and final.get("citations_inferred") is not True)


def _status_text(final: dict[str, Any]) -> str:
    """The fixed verification banner for a completed answer."""
    state = final.get("verification_state")
    citations = final.get("citations")
    count = len(citations) if isinstance(citations, list) else 0
    if _source_matched(final) and isinstance(citations, list):
        parts = [f"Source-matched · {count} citation" + ("" if count == 1 else "s")
                 + (f" (incl. {HOUSE_NOTE})" if _cites_house(final, citations) else "")]
    else:
        parts = [STATE_LABELS.get(state, UNKNOWN_STATE) if isinstance(state, str)
                 else UNKNOWN_STATE]
    if final.get("citations_inferred") is True:
        parts.append(INFERRED)
    if final.get("script_review_required") is True:
        parts.append(SCRIPT_REVIEW)
    return " · ".join(parts)


def _validated_list(final: dict[str, Any]) -> str | None:
    """The agent's validated-list delta for this terminal choice, spelled as
    src/mainframe_rag/agent/app.py _chat_final_frames sends it; None without
    citations (the agent then sends no list)."""
    citations = final.get("citations")
    if not isinstance(citations, list) or not citations:
        return None
    if not all(isinstance(c, str) for c in citations):
        return None
    return "\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in citations)


async def _deltas(lines: AsyncIterator[str], final: dict[str, Any],
                  agent_list: list[str]) -> AsyncIterator[str]:
    """Content deltas of an OpenAI-style chat stream. On success `final`
    holds the terminal choice (finish_reason plus the agent metadata). An
    error frame, a malformed frame, no finish or no [DONE] raise _Failure.

    Each delta is held until the next frame: when that frame is the terminal
    one and the held delta is exactly the agent's validated list for it, the
    delta goes to `agent_list` instead (it is not model text). Otherwise the
    held delta stays model text and, when the terminal frame carries a
    validated list, its rendering is appended to `agent_list` instead (#1212)."""
    finished = False
    held: str | None = None
    async for line in lines:
        if not line.startswith("data:"):
            continue  # blank lines, `:` keepalives, other SSE fields
        data = line[5:].strip()
        if data == "[DONE]":
            if not finished:
                break
            return
        try:
            frame = json.loads(data)
        except ValueError:
            frame = None
        if not isinstance(frame, dict):
            if held is not None:
                yield held
            raise _Failure(TRUNCATED, incomplete=True)
        if "error" in frame:
            if held is not None:
                yield held
            error = frame["error"]
            raise _Failure(_error_text(error.get("code") if isinstance(error, dict) else None),
                           incomplete=True)
        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices:
            continue  # include_usage-style frame
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta")
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content:
            if held is not None:
                yield held
            held = content
        if choice.get("finish_reason") is not None:
            final.clear()
            final.update(choice)
            final.pop(_REQUEST_ID_KEY, None)  # only the chunk id may set it
            chunk_id = frame.get("id")
            if isinstance(chunk_id, str) and (match := _CHUNK_ID.match(chunk_id)):
                final[_REQUEST_ID_KEY] = match.group(1)
            finished = True
            rendering = _validated_list(final)
            if held is not None and held == rendering:
                agent_list.append(held)
            else:
                if held is not None:
                    yield held
                if rendering is not None and not agent_list:
                    agent_list.append(rendering)
            held = None
    if held is not None:
        yield held
    raise _Failure(TRUNCATED, incomplete=True)


def _citation_like(line: str) -> bool:
    """Linear: the last `, p. ` tail, and a `, ` gap before it."""
    tails = [m.start() for m in _PAGE_TAIL.finditer(line)]
    return bool(tails) and _COMMA_GAP.search(line, 0, tails[-1]) is not None


class _CitationsFilter:
    """Model text minus its own citations sections, line by line, as
    src/mainframe_rag/agent/cites.py extract_body_and_citations delimits them:
    a header alone on its line (Citations:, or the Sources:/References:
    aliases) starts a section; a Citations: section takes citation and bullet
    lines, an alias section only citation lines; a blank line after a
    citation, or any other line, ends it and that line is prose again. An
    alias header followed by prose is kept with it. Fenced code is never
    touched. Prose order is unchanged.

    Bounded: only the undecided current line is held, at most
    HEADER_MAX_CHARS outside a section (a line that cannot be a header
    streams at once) and HOLD_MAX_CHARS inside one, past which it is prose.
    Line ends are emitted with the next text, so trailing newlines before the
    validated list are not doubled."""

    def __init__(self) -> None:
        self._line = ""  # the held, undecided start of the current line
        self._open = False  # the current line is prose and streams through
        self._head = ""  # the current line's first characters (fence check)
        self._fence = False
        self._section: str | None = None  # None, "canonical" or "alias"
        self._has_cites = False
        self._alias_header: str | None = None  # kept if prose follows it
        self._newlines = 0  # line ends not yet emitted
        self.emitted = False

    def feed(self, text: str) -> str:
        out: list[str] = []
        for i, part in enumerate(text.split("\n")):
            if i:
                self._end_line(out)
            if part:
                self._add(part, out)
        return "".join(out)

    def close(self) -> str:
        """Decide the unterminated last line; pending line ends stay pending."""
        out: list[str] = []
        if not self._open and self._line:
            self._decide(self._line, out, terminated=False)
        self._line, self._open = "", False
        return "".join(out)

    def newlines(self) -> str:
        """The pending line ends (for a stream that ends without a list)."""
        text, self._newlines = "\n" * self._newlines, 0
        return text

    def _emit(self, text: str, out: list[str]) -> None:
        if text:
            out.append("\n" * self._newlines + text)
            self._newlines = 0
            self.emitted = True

    def _add(self, part: str, out: list[str]) -> None:
        if len(self._head) < 8:
            self._head += part[: 8 - len(self._head)]
        if self._open:
            self._emit(part, out)
            return
        self._line += part
        if self._fence or not self._holdable(self._line):
            self._end_section(out)
            self._emit(self._line, out)
            self._line, self._open = "", True

    def _holdable(self, line: str) -> bool:
        if self._section is not None:
            return len(self._alias_header or "") + len(line) <= HOLD_MAX_CHARS
        match = _HEADER_START.fullmatch(line) if len(line) <= HEADER_MAX_CHARS else None
        if match is None:
            return False
        word = match.group(1).lower()
        return any(w == word if match.group(2) else w.startswith(word) for w in _HEADER_WORDS)

    def _end_line(self, out: list[str]) -> None:
        if self._open:
            self._newlines += 1
            self._open = False
        else:
            self._decide(self._line, out, terminated=True)
        if self._head.lstrip().startswith(_FENCES):
            self._fence = not self._fence
        self._line, self._head = "", ""

    def _end_section(self, out: list[str]) -> None:
        if self._section is not None and self._alias_header is not None:
            self._emit(self._alias_header, out)
            self._newlines += 1
        self._section, self._alias_header = None, None

    def _decide(self, raw: str, out: list[str], *, terminated: bool) -> None:
        line = raw.removesuffix("\r")
        if not self._fence and len(line) <= HEADER_MAX_CHARS:
            canonical = bool(_CITATIONS_HEADER.match(line))
            if canonical or _ALIAS_HEADER.match(line):
                self._section = "canonical" if canonical else "alias"
                self._alias_header = None if canonical else raw
                self._has_cites = False
                return
        if self._section is not None:
            stripped = line.strip()
            if not stripped:
                if self._has_cites:  # a blank after a citation ends the section
                    self._section, self._alias_header = None, None
                    self._newlines += 1
                return
            bullet = self._section == "canonical" and bool(_LIST_MARKER.match(stripped))
            if bullet or _citation_like(stripped):
                self._has_cites, self._alias_header = True, None
                return
            self._end_section(out)
        self._emit(raw, out)
        if terminated:
            self._newlines += 1


async def _chunks(texts: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Each text as an OpenAI chunk dict. Open WebUI forwards a yielded string
    that starts with `data:` as a raw SSE frame and emits its `event`, so a
    model delta yielded as a string could forge a banner, source or replace
    event; inside a dict it is only ever JSON string content."""
    async for text in texts:
        yield {"object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


class Pipe:
    class Valves(BaseModel):
        AGENT_URL: str = Field(
            default="http://rag-agent:8080",
            description="Agent base URL (in-cluster Service). The only host this Pipe calls.",
        )
        TIMEOUT_S: float = Field(
            default=300.0, gt=0,
            description="Read timeout for one answer, seconds (connect is bounded at 10 s).",
        )
        MAX_BODY_CHARS: int = Field(
            default=DEFAULT_MAX_BODY_CHARS, ge=1000, le=1048576,
            description="Request body cap in characters. Must equal the deployed agent's "
                        "chat_max_body_chars; older turns are dropped to fit it.",
        )
        MAX_IMAGE_BYTES: int = Field(
            default=DEFAULT_MAX_IMAGE_BYTES, ge=1024, le=20971520,
            description="Per-image cap in decoded bytes (pasted images, RAG_CHAT_IMAGES). "
                        "Must equal the deployed agent's chat_image_max_bytes.",
        )
        MAX_IMAGES: int = Field(
            default=DEFAULT_MAX_IMAGES, ge=1, le=8,
            description="Images per message (pasted images, RAG_CHAT_IMAGES). Must equal "
                        "the deployed agent's chat_image_max_count.",
        )

    class UserValves(BaseModel):
        product: str = Field(default="", description="Restrict answers to this product.")
        version: str = Field(default="", description="Restrict answers to this version.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        # Tests inject a fake agent here; Open WebUI never sets it.
        self._transport: httpx.AsyncBaseTransport | None = None

    def pipes(self) -> list[dict[str, str]]:
        """The answer depths as Open WebUI manifold entries (#1255)."""
        return [{"id": depth.id, "name": depth.name} for depth in DEPTHS]

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Emitter | None = None,
        __task__: str | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> str | AsyncIterator[dict[str, Any]]:
        if _is_task(__task__, __metadata__, body):
            # Title/tag/follow-up/summary generation is not a docs question:
            # a fixed short text, never an agent call (it would retrieve,
            # record a turn and carry the user's content).
            return TASK_REPLY
        user_valves = (__user__ or {}).get("valves")
        images_on = os.environ.get(IMAGES_ENV) == "true"
        built, earlier_dropped = _build(
            body.get("messages"), user_valves, images_on=images_on,
            max_images=self.valves.MAX_IMAGES, max_image_bytes=self.valves.MAX_IMAGE_BYTES)
        request = built if isinstance(built, dict) else None
        if request is None:  # a fixed text instead of a call
            return built if isinstance(built, str) else NO_TEXT_TURN
        if depth := selected_depth(body.get("model")):
            request["reasoning_depth"] = depth
        request, trimmed = fit_request(request, self.valves.MAX_BODY_CHARS)
        if request is None:
            return TOO_LONG.format(limit=self.valves.MAX_BODY_CHARS)
        answer = self._answer(request, __event_emitter__, trimmed,
                              earlier_dropped=earlier_dropped, images_on=images_on)
        if body.get("stream") is True:
            return _chunks(answer)
        return "".join([text async for text in answer])

    async def _answer(self, request: dict[str, Any], emit: Emitter | None,
                      trimmed: bool = False, *, earlier_dropped: bool = False,
                      images_on: bool = False) -> AsyncIterator[str]:
        async def event(payload: dict[str, Any]) -> None:
            if emit is None:
                return
            try:
                await emit(payload)
            except Exception:  # noqa: BLE001 - Open WebUI's emitter, not our state
                return  # a lost banner must not fail or leak into the answer

        async def status(text: str, done: bool) -> None:
            await event({"type": "status", "data": {"description": text, "done": done}})

        shown = _CitationsFilter()
        try:
            if trimmed:
                await status(TRIMMED, False)
            if earlier_dropped:
                await status(EARLIER_IMAGE, False)
            await status(WORKING, False)
            final: dict[str, Any] = {}
            timeout = httpx.Timeout(self.valves.TIMEOUT_S, connect=10.0)
            async with (
                httpx.AsyncClient(
                    base_url=self.valves.AGENT_URL.rstrip("/"), timeout=timeout,
                    transport=self._transport, trust_env=False, follow_redirects=False,
                ) as client,
                client.stream("POST", CHAT_PATH, json=request,
                              headers={"accept": "text/event-stream"}) as response,
            ):
                if response.status_code != 200:
                    raw = b""
                    async for chunk in response.aiter_bytes():
                        raw += chunk
                        if len(raw) > ERROR_BODY_MAX_BYTES:
                            break
                    raise _Failure(_envelope_text(raw[:ERROR_BODY_MAX_BYTES]), incomplete=False)
                agent_list: list[str] = []
                async for delta in _deltas(response.aiter_lines(), final, agent_list):
                    if text := shown.feed(delta):
                        yield text
            if text := shown.close():
                yield text
            # The validated list, the image transcripts and the reference line
            # replace any pending line ends: they bring their own. The
            # reference line stays last (the rating Action reads it there).
            request_id = final.get(_REQUEST_ID_KEY)
            reference = (REFERENCE_LINE.format(request_id=request_id)
                         if os.environ.get(REFERENCE_ENV) == "true" and isinstance(request_id, str)
                         else "")
            reads = _image_reads_block(final) if images_on else ""
            pending = "" if reference or reads else shown.newlines()
            if text := (agent_list[0] if agent_list else pending) + reads + reference:
                yield text
            for source in _source_events(final):
                await event(source)
            omitted = final.get("message_omitted_chars")
            shortened = type(omitted) is int and omitted > 0
            read_failed = images_on and final.get("image_read_failed") is True
            await status(_status_text(final) + (f" · {SHORTENED}" if shortened else "")
                         + (f" · {IMAGE_READ_FAILED}" if read_failed else "")
                         + (f" · {EARLIER_IMAGE}" if earlier_dropped else "")
                         + (f" · {TRIMMED}" if trimmed else ""), True)
        except _Failure as failure:
            text = shown.close() + shown.newlines()
            yield text + ("\n\n" if shown.emitted else "") + failure.text
            await status(STATE_LABELS["generation_incomplete"] if failure.incomplete else FAILED,
                         True)
        except Exception:  # noqa: BLE001 - the fixed-text boundary
            # Never raise into Open WebUI: it would show the exception text.
            try:
                text = shown.close() + shown.newlines()
            except Exception:  # noqa: BLE001 - the boundary must not raise
                text = ""
            yield text + ("\n\n" if shown.emitted else "") + UNREACHABLE
            await status(FAILED, True)
