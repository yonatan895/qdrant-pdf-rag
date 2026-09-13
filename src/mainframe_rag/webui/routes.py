"""Operator console (ADR-0004): server-rendered HTMX UI mounted at /ui.

One service with the agent: every conversational turn runs through the shared
answer_core engine with the same retrieval, budgeting, and citation validation
as /v1/answer and /v1/chat. Dialogue state lives entirely in the browser
(localStorage); the server keeps none. Fail-closed: ``ui_enabled=False``
answers the standard 404 envelope on every /ui path without disclosing route
existence.

Routes:
    GET  /ui                      page shell (Jinja2, browser-owned history)
    POST /ui/chat                 plain form -> full page, HTMX -> fragment
    POST /ui/chat/stream          JSON -> SSE token/final (console.js)
    GET  /ui/healthz              status badge fragment (HTMX poll)
    GET  /ui/static/{path}        vendored CSS/JS, path-traversal guarded
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mainframe_rag.agent.answer_core import (
    AnswerCoreInput,
    chat_body_chars,
    execute_answer_core,
    execute_answer_core_stream,
)
from mainframe_rag.agent.sse import error_payload, final_payload, format_sse_event
from mainframe_rag.ports import ChatMessage

log = logging.getLogger("agent.webui")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cache-Control": "no-store",
}
_ERROR_TEXT = "The reasoning agent could not complete this request. Check the agent logs and retry."


def _require_ui() -> None:
    """Fail closed on every /ui path while the console is disabled. The
    import is lazy: app.py imports this router, so a module-level import of
    the app would be circular."""
    from mainframe_rag.agent.app import AppError, settings

    if not settings.ui_enabled:
        raise AppError(404, "not_found", "not found")


router = APIRouter(prefix="/ui", tags=["ui"], dependencies=[Depends(_require_ui)])


class UiChatRequest(BaseModel):
    """Browser payload: client-owned history plus the active turn."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    splunk_context: str | None = None
    product: str | None = None
    version: str | None = None


def _secure(response: Response) -> Response:
    for key, value in _SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


def _parse_history(raw: str | None) -> list[ChatMessage]:
    """The hidden form field carries the browser's history as JSON; malformed
    input degrades to an empty history (the browser owns the state)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    history: list[ChatMessage] = []
    for item in parsed:
        try:
            history.append(ChatMessage.model_validate(item))
        except ValidationError:
            continue
    return history


def _assistant_content(answer: str, citations: list[str]) -> str:
    if citations:
        return answer + "\n\nCitations:\n" + "\n".join(f"- {c}" for c in citations)
    return answer


# Safe markdown subset for assistant turns (issue #326 P1). The reasoning
# model speaks markdown but the console must never execute it: the whole
# input is HTML-escaped FIRST and only our own generated tags exist in the
# output (spool dumps / retrieved chunks may carry hostile markup; CSP is
# the backstop, escaping is the guarantee). Subset, deliberately small:
# ATX headings (#–####, space required), **bold**, *italic*, `code`,
# fenced code blocks (``` + optional language, unclosed closes at EOF),
# unordered (-/*) and ordered (1./1)) lists. No links, images, tables, or
# raw HTML — anything outside the subset renders as inert text.
# console.js implements the same subset for the streaming path; the fixture
# battery in tests/test_webui.py pins both the rendering and the refusal
# cases (server side — there is no JS runtime in CI).
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*$")
_MD_FENCE_RE = re.compile(r"^ {0,3}```([\w+-]*)\s*$")
_MD_UL_RE = re.compile(r"^ {0,3}[-*]\s+(.*)$")
_MD_OL_RE = re.compile(r"^ {0,3}\d+[.)]\s+(.*)$")
_MD_CODE_RE = re.compile(r"`([^`\n]+?)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")


def _md_inline(escaped: str) -> str:
    """Inline subset over already-escaped text. Code spans are extracted to
    placeholders first so `**`/`*` inside code stay literal."""
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(f"<code>{match.group(1)}</code>")
        return f"\ue000{len(spans) - 1}\ue001"

    text = _MD_CODE_RE.sub(stash, escaped)
    text = _MD_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC_RE.sub(r"<em>\1</em>", text)
    for idx, rendered in enumerate(spans):
        text = text.replace(f"\ue000{idx}\ue001", rendered)
    return text


def render_markdown_subset(text: str) -> str:
    """Render the assistant-turn markdown subset to safe HTML."""
    out: list[str] = []
    para: list[str] = []
    in_list: str | None = None  # "ul" | "ol" | None

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list is not None:
            out.append(f"</{in_list}>")
            in_list = None

    lines = html.escape(text, quote=False).split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        fence = _MD_FENCE_RE.match(line)
        if fence:
            flush_para()
            close_list()
            lang = fence.group(1)
            body: list[str] = []
            idx += 1
            while idx < len(lines) and not _MD_FENCE_RE.match(lines[idx]):
                body.append(lines[idx])
                idx += 1
            idx += 1  # consume the closing fence, or run off the end (unclosed)
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{chr(10).join(body)}</code></pre>")
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading:
            flush_para()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_md_inline(heading.group(2))}</h{level}>")
            idx += 1
            continue
        ul = _MD_UL_RE.match(line)
        ol = _MD_OL_RE.match(line) if ul is None else None
        match = ul if ul is not None else ol
        if match is not None:
            flush_para()
            kind = "ul" if ul is not None else "ol"
            if in_list != kind:
                close_list()
                out.append(f"<{kind}>")
                in_list = kind
            out.append(f"<li>{_md_inline(match.group(1))}</li>")
            idx += 1
            continue
        if not line.strip():
            flush_para()
            close_list()
            idx += 1
            continue
        para.append(line.strip())
        idx += 1
    flush_para()
    close_list()
    return "\n".join(out)


def _turn(
    role: str,
    content: str,
    *,
    citations: list[str] | None = None,
    splunk_context: str | None = None,
    history_content: str | None = None,
) -> dict[str, Any]:
    turn = {
        "role": role,
        "content": content,
        "citations": citations or [],
        "splunk_context": splunk_context,
        "history_content": history_content if history_content is not None else content,
    }
    # Assistant turns render the safe markdown subset; operator turns stay
    # plain <pre> (the operator's own keystrokes, never model output).
    # history_content keeps the raw text for the LLM either way.
    if role == "assistant":
        turn["content_html"] = render_markdown_subset(content)
    return turn


def _history_json(turns: list[dict[str, Any]]) -> str:
    return json.dumps([{"role": t["role"], "content": t["history_content"]} for t in turns])


def _render_page(
    request: Request,
    turns: list[dict[str, Any]],
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    body = templates.get_template("index.html").render(
        request=request,
        turns=turns,
        error=error,
        history_json=_history_json(turns),
        splunk_context=(form or {}).get("splunk_context", ""),
        product=(form or {}).get("product", ""),
        version=(form or {}).get("version", ""),
    )
    return _secure(HTMLResponse(body, status_code=status_code))


def _render_pair(
    request: Request,
    turns: list[dict[str, Any]],
    *,
    history_json: str,
    error: str | None = None,
) -> Response:
    body = "".join(
        templates.get_template("_message_pair.html").render(request=request, turn=turn)
        for turn in turns
    )
    if error:
        body += f'<div class="error-banner" role="alert">{error}</div>'
    # The form's hidden history must advance with the fragment, swapped out
    # of band so a later plain POST (no-JS) still carries the conversation.
    body += (
        '<input type="hidden" id="history-json" name="messages" '
        f'value="{html.escape(history_json, quote=True)}" hx-swap-oob="true">'
    )
    return _secure(HTMLResponse(body))


async def _run_turn(request: Request, req: UiChatRequest):
    """Run one console turn through the shared answer core; the caller owns
    client-facing error mapping (fixed text, detail to logs only)."""
    from mainframe_rag.agent import app as app_mod

    settings = app_mod.settings
    latest = [m for m in req.messages if m.role == "user"][-1].content.strip()
    if len(latest) > settings.query_max_chars:
        raise app_mod.AppError(422, "invalid_request", "request body failed validation")
    if chat_body_chars(req.messages, req.splunk_context) > settings.chat_max_body_chars:
        raise app_mod.AppError(422, "invalid_request", "request body failed validation")

    request_id = getattr(request.state, "request_id", "ui")
    root_span = app_mod.tracer.start_span(
        "ui.chat",
        context=app_mod.parent_context(request.headers),
        attributes={"http.request_id": request_id, "rag.stream": False},
    )
    core_input = AnswerCoreInput(
        query=latest,
        messages=req.messages,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        request_id=request_id,
        is_chat=True,
    )
    try:
        return await execute_answer_core(core_input, app_mod.core_deps(), parent_span=root_span)
    finally:
        root_span.end()


def history_to_turns(history: list[ChatMessage]) -> list[dict[str, Any]]:
    return [_turn(message.role, message.content) for message in history]


@router.get("", response_class=HTMLResponse)
async def ui_index(request: Request) -> Response:
    return _render_page(request, [])


@router.get("/healthz", response_class=HTMLResponse)
async def ui_healthz() -> Response:
    from mainframe_rag.agent import app as app_mod

    try:
        health = await app_mod.healthz()
        online = health.status == "ok"
        css = "badge-ok" if online else "badge-warn"
        label = "Online" if online else "Degraded"
    except Exception as exc:  # noqa: BLE001 — badge degrades, never a 500
        log.warning("ui_healthz failed: %s", str(exc)[:200])
        css, label = "badge-down", "Offline"
    return _secure(HTMLResponse(f'<span class="badge {css}">&#9679; {label}</span>'))


@router.post("/chat", response_class=HTMLResponse)
async def ui_chat(
    request: Request,
    message: str = Form(min_length=1),
    messages: str | None = Form(default=None),
    splunk_context: str | None = Form(default=None),
    product: str | None = Form(default=None),
    version: str | None = Form(default=None),
) -> Response:
    history = _parse_history(messages)
    context = splunk_context.strip() if splunk_context and splunk_context.strip() else None
    user_turn = _turn("user", message.strip(), splunk_context=context)
    turns = history_to_turns(history) + [user_turn]
    form = {
        "splunk_context": splunk_context or "",
        "product": product or "",
        "version": version or "",
    }
    is_htmx = request.headers.get("HX-Request") == "true"

    try:
        req = UiChatRequest(
            messages=[*history, ChatMessage(role="user", content=message.strip())],
            splunk_context=context,
            product=(product or None),
            version=(version or None),
        )
        output = await _run_turn(request, req)
    except Exception as exc:  # noqa: BLE001 — fixed banner to the operator, detail to logs
        log.error("ui_chat failed: %s", str(exc)[:200])
        if is_htmx:
            return _render_pair(
                request,
                [user_turn],
                history_json=_history_json(turns[:-1]),
                error=_ERROR_TEXT,
            )
        return _render_page(request, turns, error=_ERROR_TEXT, form=form, status_code=502)

    assistant_turn = _turn(
        "assistant",
        output.answer,
        citations=output.citations,
        history_content=_assistant_content(output.answer, output.citations),
    )
    turns.append(assistant_turn)
    if is_htmx:
        return _render_pair(request, [user_turn, assistant_turn], history_json=_history_json(turns))
    return _render_page(request, turns, form=form)


@router.post("/chat/stream")
async def ui_chat_stream(request: Request, req: UiChatRequest) -> Response:
    from mainframe_rag.agent import app as app_mod

    latest = [m for m in req.messages if m.role == "user"][-1].content.strip()
    if len(latest) > app_mod.settings.query_max_chars:
        raise app_mod.AppError(422, "invalid_request", "request body failed validation")
    if chat_body_chars(req.messages, req.splunk_context) > app_mod.settings.chat_max_body_chars:
        raise app_mod.AppError(422, "invalid_request", "request body failed validation")

    request_id = getattr(request.state, "request_id", "ui")
    root_span = app_mod.tracer.start_span(
        "ui.chat",
        context=app_mod.parent_context(request.headers),
        attributes={"http.request_id": request_id, "rag.stream": True},
    )
    core_input = AnswerCoreInput(
        query=latest,
        messages=req.messages,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        request_id=request_id,
        is_chat=True,
        stream=True,
    )

    async def events():
        try:
            async for item in execute_answer_core_stream(
                core_input, app_mod.core_deps(), parent_span=root_span
            ):
                itype = item.get("type")
                if itype == "token":
                    delta = item.get("delta") or ""
                    if delta:
                        yield format_sse_event(
                            "token", {"type": "token", "delta": delta, "token": delta}
                        )
                elif itype == "final":
                    output = item["output"]
                    yield format_sse_event(
                        "final",
                        final_payload(
                            request_id,
                            output.answer,
                            output.citations,
                            output.citations_inferred,
                            output.script,
                            output.query_kind,
                            output.hits,
                            output.finish_reason,
                            output.ttft_ms,
                            output.usage,
                            inferred_indices=output.inferred_indices,
                        ),
                    )
        except Exception as exc:  # noqa: BLE001 — mid-stream: error event, no final
            log.error("ui_chat_stream failed: %s", str(exc)[:200])
            yield format_sse_event("error", error_payload())
        finally:
            root_span.end()

    headers = {**_SECURITY_HEADERS}
    headers.pop("Cache-Control", None)
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


@router.get("/static/{path:path}")
async def ui_static(path: str) -> Response:
    target = (_STATIC_DIR / path).resolve()
    if not target.is_relative_to(_STATIC_DIR.resolve()) or not target.is_file():
        from mainframe_rag.agent.app import AppError

        raise AppError(404, "not_found", "not found")
    response = _secure(FileResponse(target))
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response
