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
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mainframe_rag.agent.answer import is_model_timeout
from mainframe_rag.agent.answer_core import (
    AnswerCoreInput,
    execute_answer_core,
    execute_answer_core_stream,
)
from mainframe_rag.agent.sse import error_payload, format_sse_event, output_final_payload
from mainframe_rag.config import ReasoningEffort
from mainframe_rag.feedback.store import (
    COMMENT_MAX_CHARS,
    RATINGS,
    REASONS,
    REQUEST_ID_PATTERN,
    RatingValue,
    Reasons,
)
from mainframe_rag.ingest.bounds import EmbedInputTooLarge
from mainframe_rag.ingest.chunk import detect_code_region
from mainframe_rag.logs import error_type
from mainframe_rag.ports import ChatMessage
from mainframe_rag.tracing import start_span

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
_BUDGET_ERROR_TEXT = (
    "The request exceeds the model's token budget. Shorten the question, "
    "context, or history and retry."
)
_EFFORT_ERROR_TEXT = (
    "This deployment does not offer the selected reasoning effort. "
    "Reload the console and choose another level."
)
_EFFORT_LABELS: dict[str, str] = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high"}


def _offered_efforts() -> tuple[ReasoningEffort, ...]:
    from mainframe_rag.agent import app as app_mod

    return app_mod.settings.reasoning_efforts()


def _require_offered_effort(effort: ReasoningEffort | None) -> None:
    """Refuse an effort the served template does not accept (#748) before
    any model call, so it is never reported as a model outage."""
    from mainframe_rag.agent import app as app_mod

    if effort is not None and effort not in _offered_efforts():
        raise app_mod.AppError(
            422, "unsupported_reasoning_effort", "the reasoning effort is not offered by this deployment"
        )


def _require_ui() -> None:
    """Fail closed on every /ui path while the console is disabled. The
    import is lazy: app.py imports this router, so a module-level import of
    the app would be circular."""
    from mainframe_rag.agent.app import AppError, settings

    if not settings.ui_enabled:
        raise AppError(404, "not_found", "not found")


router = APIRouter(prefix="/ui", tags=["ui"], dependencies=[Depends(_require_ui)])


class UiFeedbackRequest(BaseModel):
    """Console rating of one recorded answer (issue #780). The answer itself
    is never sent: the store attaches the rating to its own record."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=f"^{REQUEST_ID_PATTERN}$")
    rating: RatingValue
    reasons: Reasons = Field(default_factory=list)
    comment: str = Field(default="", max_length=COMMENT_MAX_CHARS)


_FEEDBACK_DISABLED_TEXT = "feedback is not enabled on this deployment"
# One fixed console response per client outcome (status, code, message; ok
# carries no code and its message is built from the rating); a test pins the
# keys to RateOutcome. The unknown text covers both a record
# still queued and one dropped or purged, so it promises neither.
_FEEDBACK_OUTCOMES: dict[str, tuple[int, str | None, str]] = {
    "ok": (201, None, "Rated: {rating}"),
    "unknown": (404, "unknown_answer", "this answer is not recorded, or not yet; it cannot be rated now"),
    "invalid": (422, "invalid_request", "the rating is invalid"),
    "unavailable": (503, "feedback_unavailable", "feedback is temporarily unavailable"),
}


class UiChatRequest(BaseModel):
    """Browser payload: client-owned history plus the active turn."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    splunk_context: str | None = None
    product: str | None = None
    version: str | None = None
    reasoning_effort: ReasoningEffort | None = None


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
# unordered (-/*) and ordered (1./1)) lists. A plain line directly under a
# list item continues that item (CommonMark lazy continuation) instead of
# becoming a paragraph out of order. GFM pipe tables (issue #749): a header
# row, then a delimiter row with the same cell count, then body rows; cells
# carry the inline subset and ragged rows are padded/truncated to the header.
# No links, images or raw HTML — anything outside the subset renders as
# inert text.
# console.js implements the same subset for the streaming path; the fixture
# battery in tests/test_webui.py pins both the rendering and the refusal
# cases (server side — there is no JS runtime in CI).
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*$")
_MD_FENCE_RE = re.compile(r"^ {0,3}```([\w+-]*)\s*$")
_MD_UL_RE = re.compile(r"^ {0,3}[-*]\s+(.*)$")
_MD_OL_RE = re.compile(r"^ {0,3}\d+[.)]\s+(.*)$")
_MD_CODE_RE = re.compile(r"`([^`\n]+?)`")
_MD_TABLE_DELIM_CELL_RE = re.compile(r"^:?-{3,}:?$")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")


# Minimal JCL and REXX tokenizers (issue #337): spans for comments, strings,
# keywords, and numbers. Other languages keep the language class with no spans.
_JCL_KEYWORDS = (
    # Statements / operations
    "COMMAND",
    "CNTL",
    "DD",
    "ELSE",
    "ENDCNTL",
    "ENDIF",
    "EXEC",
    "IF",
    "INCLUDE",
    "JCLLIB",
    "JOB",
    "OUTPUT",
    "PEND",
    "PROC",
    "SET",
    "THEN",
    "XMIT",
    # Parameters and dispositions
    "AVGREC",
    "BLKSIZE",
    "CATLG",
    "CLASS",
    "COND",
    "CONTIG",
    "COPIES",
    "CYL",
    "DATACLAS",
    "DCB",
    "DELETE",
    "DEST",
    "DISP",
    "DSN",
    "DSNAME",
    "DUMMY",
    "EXPDT",
    "FREE",
    "HOLD",
    "KEEP",
    "LABEL",
    "LIKE",
    "LRECL",
    "MGMTCLAS",
    "MOD",
    "MSGCLASS",
    "MSGLEVEL",
    "NEW",
    "NOTIFY",
    "OLD",
    "PARM",
    "PASS",
    "PASSWORD",
    "PGM",
    "RECFM",
    "REFDD",
    "REGION",
    "RESTART",
    "RETPD",
    "RLSE",
    "SHR",
    "SPACE",
    "STORCLAS",
    "SUBSYS",
    "SYSOUT",
    "TERM",
    "TIME",
    "TRK",
    "TYPRUN",
    "UNCATLG",
    "UNIT",
    "USER",
    "VOL",
    "VOLUME",
)

_REXX_KEYWORDS = (
    # Instructions and control flow
    "ADDRESS",
    "ARG",
    "BY",
    "CALL",
    "DIGITS",
    "DO",
    "DROP",
    "ELSE",
    "END",
    "EXIT",
    "EXPOSE",
    "FOR",
    "FOREVER",
    "FORM",
    "FUZZ",
    "IF",
    "INTERPRET",
    "ITERATE",
    "LEAVE",
    "NOP",
    "NUMERIC",
    "OPTIONS",
    "OTHERWISE",
    "PARSE",
    "PROCEDURE",
    "PULL",
    "PUSH",
    "QUEUE",
    "RETURN",
    "SAY",
    "SELECT",
    "SIGNAL",
    "THEN",
    "TO",
    "TRACE",
    "UNTIL",
    "UPPER",
    "VALUE",
    "VAR",
    "WHEN",
    "WHILE",
    "WITH",
    # Built-in functions
    "ABBREV",
    "CENTER",
    "CENTRE",
    "COPIES",
    "C2D",
    "C2X",
    "DATATYPE",
    "DATE",
    "DELSTR",
    "DELWORD",
    "D2C",
    "D2X",
    "ERRORTEXT",
    "FORMAT",
    "INSERT",
    "LASTPOS",
    "LEFT",
    "LENGTH",
    "LINEIN",
    "LINEOUT",
    "LINES",
    "OVERLAY",
    "POS",
    "QUEUED",
    "RANDOM",
    "REVERSE",
    "RIGHT",
    "SOURCELINE",
    "SPACE",
    "STRIP",
    "SUBSTR",
    "SUBWORD",
    "SYMBOL",
    "TIME",
    "TRANSLATE",
    "TRUNC",
    "VERIFY",
    "WORD",
    "WORDINDEX",
    "WORDLENGTH",
    "WORDPOS",
    "WORDS",
    "X2C",
    "X2D",
)

_JCL_KW_PATTERN = "|".join(sorted(_JCL_KEYWORDS, key=len, reverse=True))
_REXX_KW_PATTERN = "|".join(sorted(_REXX_KEYWORDS, key=len, reverse=True))

_JCL_TOKEN_RE = re.compile(
    r"(?P<comment>^[ \t]*//\*.*$)"
    r"|(?P<string>'(?:''|[^'\n])*')"
    r"|(?P<keyword>\b(?:" + _JCL_KW_PATTERN + r")\b)"
    r"|(?P<number>\b\d+\b)",
    re.IGNORECASE | re.MULTILINE,
)

_REXX_TOKEN_RE = re.compile(
    r"(?P<comment>/\*[\s\S]*?(?:\*/|$))"
    r"|(?P<string>'(?:''|[^'\n])*'|\"(?:\"\"|[^\"\n])*\")"
    r"|(?P<keyword>\b(?:" + _REXX_KW_PATTERN + r")\b)"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)",
    re.IGNORECASE,
)


def _tokenize_code(code: str, lang: str) -> str:
    """Tokenize JCL or REXX code into safe HTML spans.

    Every slice of source code is HTML-escaped; tokens are wrapped in
    .tok-{comment|string|keyword|number} spans. Unrecognized languages or
    plain code are returned escaped with no spans.
    """
    regex = _JCL_TOKEN_RE if lang == "jcl" else (_REXX_TOKEN_RE if lang == "rexx" else None)
    if regex is None:
        return html.escape(code, quote=False)

    out: list[str] = []
    last = 0
    for m in regex.finditer(code):
        start, end = m.span()
        if start > last:
            out.append(html.escape(code[last:start], quote=False))
        kind = m.lastgroup
        text = html.escape(m.group(), quote=False)
        out.append(f'<span class="tok-{kind}">{text}</span>')
        last = end
    if last < len(code):
        out.append(html.escape(code[last:], quote=False))
    return "".join(out)


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


def _md_table_cells(line: str) -> list[str] | None:
    """Split one pipe-table row; None when the line is not a pipe row."""
    row = line.strip()
    if "|" not in row:
        return None
    row = row.removeprefix("|")
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def _md_table_alignments(line: str, width: int) -> list[str] | None:
    """Delimiter-row alignments, or None unless it matches the header width."""
    cells = _md_table_cells(line)
    if cells is None or len(cells) != width or not all(_MD_TABLE_DELIM_CELL_RE.match(c) for c in cells):
        return None
    return ["center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":")
            else "left" if c.startswith(":") else "" for c in cells]


def _md_table_html(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    def cell(tag: str, text: str, align: str) -> str:
        cls = f' class="align-{align}"' if align else ""
        return f"<{tag}{cls}>{_md_inline(html.escape(text, quote=False))}</{tag}>"

    width = len(header)
    head = "".join(cell("th", text, align) for text, align in zip(header, aligns, strict=True))
    body = "".join(
        "<tr>" + "".join(cell("td", text, align)
                         for text, align in zip((row + [""] * width)[:width], aligns, strict=True)) + "</tr>"
        for row in rows
    )
    return f'<div class="md-table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render_markdown_subset(text: str) -> str:
    """Render the assistant-turn markdown subset to safe HTML."""
    out: list[str] = []
    para: list[str] = []
    in_list: str | None = None  # "ul" | "ol" | None
    item: list[str] = []  # escaped lines of the open list item

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    def flush_item() -> None:
        if item:
            out.append(f"<li>{_md_inline(' '.join(item))}</li>")
            item.clear()

    def close_list() -> None:
        nonlocal in_list
        flush_item()
        if in_list is not None:
            out.append(f"</{in_list}>")
            in_list = None

    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        fence = _MD_FENCE_RE.match(line)
        if fence:
            flush_para()
            close_list()
            lang = fence.group(1).strip().lower() if fence.group(1) else None
            body: list[str] = []
            idx += 1
            while idx < len(lines) and not _MD_FENCE_RE.match(lines[idx]):
                body.append(lines[idx])
                idx += 1
            idx += 1  # consume the closing fence, or run off the end (unclosed)
            raw_code = "\n".join(body)
            if not lang:
                lang = detect_code_region(raw_code)
            cls = f' class="language-{lang}"' if lang else ""
            code_html = (
                _tokenize_code(raw_code, lang)
                if lang in ("jcl", "rexx")
                else html.escape(raw_code, quote=False)
            )
            # Static label: the only words this renderer emits are our own.
            # console.js copies from the sibling <code> node, so no payload
            # travels in attributes.
            out.append(
                '<pre><button class="copy-btn" type="button">Copy</button>'
                f"<code{cls}>{code_html}</code></pre>"
            )
            continue
        header = _md_table_cells(line)
        aligns = (_md_table_alignments(lines[idx + 1], len(header))
                  if header is not None and idx + 1 < len(lines) else None)
        if header is not None and aligns is not None:
            flush_para()
            close_list()
            rows: list[list[str]] = []
            idx += 2
            while idx < len(lines) and lines[idx].strip():
                row = _md_table_cells(lines[idx])
                if row is None:
                    break
                rows.append(row)
                idx += 1
            out.append(_md_table_html(header, aligns, rows))
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading:
            flush_para()
            close_list()
            level = len(heading.group(1))
            escaped_heading = html.escape(heading.group(2), quote=False)
            out.append(f"<h{level}>{_md_inline(escaped_heading)}</h{level}>")
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
            flush_item()
            item.append(html.escape(match.group(1), quote=False))
            idx += 1
            continue
        if not line.strip():
            flush_para()
            close_list()
            idx += 1
            continue
        (item if item else para).append(html.escape(line.strip(), quote=False))
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
    verification_state: str | None = None,
    citations_inferred: bool = False,
    script_review_required: bool = False,
    supplied_evidence: list[Any] | None = None,
    evidence_omitted: int = 0,
) -> dict[str, Any]:
    turn = {
        "role": role,
        "content": content,
        "citations": citations or [],
        "splunk_context": splunk_context,
        "history_content": history_content if history_content is not None else content,
        # UTC instant; the template prints HH:MM UTC and console.js upgrades
        # it to the operator's local time on load (P2).
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        # Verification state (issue #365): rendered as a badge and a
        # state-driven citations title; persisted in browser history so a
        # reload cannot promote a draft to accepted. None for user turns.
        "verification_state": verification_state,
        "citations_inferred": citations_inferred,
        "script_review_required": script_review_required,
        # Supplied-excerpt view (issue #635): this response only, rendered
        # once by the fragment; never part of history_json or browser state.
        "supplied_evidence": supplied_evidence,
        "evidence_omitted": evidence_omitted,
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
    offered = _offered_efforts()
    requested = (form or {}).get("reasoning_effort")
    selected = requested if requested in offered else ("low" if "low" in offered else offered[0])
    body = templates.get_template("index.html").render(
        request=request,
        turns=turns,
        error=error,
        history_json=_history_json(turns),
        splunk_context=(form or {}).get("splunk_context", ""),
        product=(form or {}).get("product", ""),
        version=(form or {}).get("version", ""),
        reasoning_effort=selected,
        reasoning_efforts=[(effort, _EFFORT_LABELS[effort]) for effort in offered],
        **_feedback_context(),
    )
    return _secure(HTMLResponse(body, status_code=status_code))


_FEEDBACK_LABELS = {
    "good": "Good", "neutral": "Neutral", "bad": "Bad",
    "wrong": "Wrong", "incomplete": "Incomplete",
    "missing_or_wrong_citation": "Missing or wrong citation",
    "should_have_declined": "Should have declined", "wrongly_declined": "Wrongly declined",
    "too_slow": "Too slow", "other": "Other",
}


def _feedback_context() -> dict[str, Any]:
    """Template inputs for the rating controls (#780), from the store's vocabulary."""
    return {
        "feedback_enabled": _feedback_client() is not None,
        "feedback_ratings": [(value, _FEEDBACK_LABELS[value]) for value in RATINGS],
        "feedback_reasons": [(value, _FEEDBACK_LABELS[value]) for value in REASONS],
    }


def _feedback_client():
    from mainframe_rag.agent import app as app_mod

    return app_mod.feedback


def _render_pair(
    request: Request,
    turns: list[dict[str, Any]],
    *,
    history_json: str,
    error: str | None = None,
) -> Response:
    feedback = _feedback_context()
    body = "".join(
        templates.get_template("_message_pair.html").render(request=request, turn=turn, **feedback)
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


async def _run_turn(request: Request, req: UiChatRequest, root_span, started: float):
    """Run one console turn through the shared answer core; the caller owns
    client-facing error mapping (fixed text, detail to logs only)."""
    from mainframe_rag.agent import app as app_mod

    _require_offered_effort(req.reasoning_effort)
    request_id = getattr(request.state, "request_id", "ui")
    turn = app_mod.prepare_chat_request(request_id, req.messages, req.splunk_context)
    request.state.feedback_query = turn.query
    # Serving gate before any retrieval (issues #391 F3/F4): the console
    # refuses with the same stable 503 as the API when the generation is
    # unverified.
    deps = await app_mod.serving_deps()
    core_input = AnswerCoreInput(
        query=turn.query,
        messages=turn.messages,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        is_chat=True,
        reasoning_effort=req.reasoning_effort,
    )
    try:
        output = await execute_answer_core(core_input, deps, parent_span=root_span)
    except Exception as exc:
        app_mod._span_error(root_span, exc)
        raise
    else:
        log.info(
            app_mod.json_log(
                request_id,
                "console",
                query_kind=output.query_kind,
                hits=len(output.hits),
                verification_state=output.verification_state,
            )
        )
        app_mod._record_feedback(request, "console", output, app_mod._output_log_fields(
            output, output.query_kind, output.timings, started, stream=False))
        return output


def history_to_turns(history: list[ChatMessage]) -> list[dict[str, Any]]:
    return [_turn(message.role, message.content) for message in history]


@router.get("", response_class=HTMLResponse)
async def ui_index(request: Request) -> Response:
    return _render_page(request, [])


@router.get("/healthz", response_class=HTMLResponse)
async def ui_healthz() -> Response:
    from mainframe_rag.agent import app as app_mod

    try:
        health, _code = await app_mod.evaluate_healthz()
        online = health.status == "ok"
        css = "badge-ok" if online else "badge-warn"
        label = "Online" if online else "Degraded"
    except Exception as exc:  # noqa: BLE001 — badge degrades, never a 500
        log.warning("ui_healthz failed: %s", error_type(exc))
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
    reasoning_effort: ReasoningEffort | None = Form(default=None),  # noqa: B008
) -> Response:
    from mainframe_rag.agent import app as app_mod

    started = time.monotonic()
    request_id = getattr(request.state, "request_id", "ui")
    # One SERVER span per turn (issue #529 OBS-1B §4.3): created before turn
    # validation and the serving gate so rejections share the trace.
    root_span = start_span(
        app_mod.tracer,
        "ui.chat",
        context=app_mod.parent_context(request.headers),
        kind=SpanKind.SERVER,
        attributes={"http.request_id": request_id, "rag.stream": False},
    )
    with app_mod._request_span(request, root_span, "console", started) as owner:
        history = _parse_history(messages)
        context = splunk_context.strip() if splunk_context and splunk_context.strip() else None
        user_turn = _turn("user", message.strip(), splunk_context=context)
        turns = history_to_turns(history) + [user_turn]
        form = {
            "splunk_context": splunk_context or "",
            "product": product or "",
            "version": version or "",
            "reasoning_effort": reasoning_effort or "low",
        }
        is_htmx = request.headers.get("HX-Request") == "true"

        try:
            req = UiChatRequest(
                messages=[*history, ChatMessage(role="user", content=message.strip())],
                splunk_context=context,
                product=(product or None),
                version=(version or None),
                reasoning_effort=reasoning_effort,
            )
            # Same admission slot and total deadline as the API routes
            # (issue #374); a refusal renders the fixed banner like any
            # other console failure, with the stable code in RED.
            await app_mod._admit(owner)
            output = await app_mod._within_deadline(owner, _run_turn(request, req, root_span, started))
        except Exception as exc:  # noqa: BLE001 — fixed banner to the operator, detail to logs
            from mainframe_rag.agent.answer import PromptBudgetExceeded

            if isinstance(exc, app_mod.AppError) and exc.status >= 500:
                app_mod._span_error(root_span, exc)
            log.error(app_mod.json_log(request_id, "console", error=error_type(exc)))
            # An irreducible budget overflow is the operator's request to
            # shrink, not a server fault: distinct fixed banner (issue #368).
            effort_refused = isinstance(exc, app_mod.AppError) and exc.code == "unsupported_reasoning_effort"
            error_text = (
                _BUDGET_ERROR_TEXT
                if isinstance(exc, PromptBudgetExceeded)
                else _EFFORT_ERROR_TEXT
                if effort_refused
                else _ERROR_TEXT
            )
            status_code = 422 if isinstance(exc, PromptBudgetExceeded) or effort_refused else 502
            outcome = (
                "prompt_budget_exceeded"
                if isinstance(exc, PromptBudgetExceeded)
                else exc.code
                if isinstance(exc, app_mod.AppError)
                else "invalid_request"
                if isinstance(exc, ValidationError | EmbedInputTooLarge)
                else "upstream_error"
            )
            app_mod._record_endpoint(request, "console", outcome, started)
            if is_htmx:
                return _render_pair(
                    request,
                    [user_turn],
                    history_json=_history_json(turns[:-1]),
                    error=error_text,
                )
            return _render_page(request, turns, error=error_text, form=form, status_code=status_code)

        app_mod._record_endpoint(
            request,
            "console",
            "ok",
            started,
            query_class=output.query_kind,
            hits=len(output.hits),
            ttft_ms=output.ttft_ms,
            llm_model=app_mod.settings.llm_model_reasoning,
            verification_state=output.verification_state,
        )
        assistant_content = output.answer
        if output.script:
            # Tagged script fences (JCL/REXX/…) leave the answer body during
            # citation parsing; the console renders them with their threaded
            # language tag (issue #337), falling back to unlabeled when None.
            tag = output.script_lang or ""
            assistant_content += f"\n\n```{tag}\n{output.script}\n```"
        assistant_turn = _turn(
            "assistant",
            assistant_content,
            citations=output.citations,
            history_content=_assistant_content(assistant_content, output.citations),
            verification_state=output.verification_state,
            citations_inferred=output.citations_inferred,
            script_review_required=output.script_review_required,
            supplied_evidence=list(output.supplied),
            evidence_omitted=len(output.evidence.omitted_indices),
        )
        if _feedback_client() is not None:
            # The fallback fragment's rating form attaches to this record (#780).
            assistant_turn["request_id"] = request_id
        turns.append(assistant_turn)
        if is_htmx:
            return _render_pair(request, [user_turn, assistant_turn], history_json=_history_json(turns))
        return _render_page(request, turns, form=form)


@router.post("/chat/stream")
async def ui_chat_stream(request: Request, req: UiChatRequest) -> Response:
    from mainframe_rag.agent import app as app_mod

    started = time.monotonic()
    request_id = getattr(request.state, "request_id", "ui")
    # One SERVER span per turn (issue #529 OBS-1B §4.3): created before turn
    # validation and the serving gate so rejections share the trace.
    root_span = start_span(
        app_mod.tracer,
        "ui.chat",
        context=app_mod.parent_context(request.headers),
        kind=SpanKind.SERVER,
        attributes={"http.request_id": request_id, "rag.stream": True},
    )
    with app_mod._request_span(request, root_span, "console", started) as owner:
        await app_mod._admit(owner)
        return await app_mod._within_deadline(owner, _console_stream_response(req, owner))


async def _console_stream_response(req, owner):
    from mainframe_rag.agent import app as app_mod

    request = owner.request
    root_span = owner.span
    request_id = request.state.request_id
    started = owner.started
    _require_offered_effort(req.reasoning_effort)
    turn = app_mod.prepare_chat_request(request_id, req.messages, req.splunk_context)
    request.state.feedback_query = turn.query
    # Serving gate before the stream opens (issues #391 F3/F4): a non-servable
    # generation is the same stable 503 JSON the API returns, never an SSE
    # error frame after a 200 was already committed.
    deps = await app_mod.serving_deps()
    core_input = AnswerCoreInput(
        query=turn.query,
        messages=turn.messages,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        is_chat=True,
        reasoning_effort=req.reasoning_effort,
    )

    async def events():
        core_events = execute_answer_core_stream(core_input, deps, parent_span=root_span)
        bounded = app_mod._deadline_iter(owner, core_events)
        try:
            async for item in bounded:
                if item["type"] == "token":
                    delta = item["delta"]
                    if delta:
                        yield format_sse_event(
                            "token", {"type": "token", "delta": delta, "token": delta}
                        )
                elif item["type"] == "final":
                    output = item["output"]
                    app_mod._record_endpoint(
                        request,
                        "console",
                        "ok",
                        started,
                        query_class=output.query_kind,
                        hits=len(output.hits),
                        ttft_ms=output.ttft_ms,
                        llm_model=app_mod.settings.llm_model_reasoning,
                        verification_state=output.verification_state,
                    )
                    log.info(
                        app_mod.json_log(
                            request_id,
                            "console",
                            query_kind=output.query_kind,
                            hits=len(output.hits),
                            verification_state=output.verification_state,
                            stream=True,
                        )
                    )
                    app_mod._record_feedback(request, "console", output, app_mod._output_log_fields(
                        output, output.query_kind, output.timings, started, stream=True))
                    yield format_sse_event(
                        "final", output_final_payload(request_id, output, output.query_kind)
                    )
        except Exception as exc:  # noqa: BLE001 — mid-stream: error event, no final
            from mainframe_rag.agent.answer import PromptBudgetExceeded

            if isinstance(exc, app_mod.RequestDeadlineExceeded):
                owner.deadline_failed()
            app_mod._span_error(root_span, exc)
            if isinstance(exc, PromptBudgetExceeded):
                log.warning(
                    app_mod.json_log(request_id, "console", error=error_type(exc), stream=True)
                )
            else:
                log.error(
                    app_mod.json_log(request_id, "console", error=error_type(exc), stream=True)
                )
            app_mod._record_endpoint(
                request,
                "console",
                "prompt_budget_exceeded"
                if isinstance(exc, PromptBudgetExceeded)
                else "deadline_exceeded"
                if isinstance(exc, app_mod.RequestDeadlineExceeded)
                else "model_timeout"
                if is_model_timeout(exc)
                else "upstream_error",
                started,
                verification_state="generation_incomplete",
            )
            yield format_sse_event("error", error_payload(*app_mod.stream_error_fields(exc)))

        finally:
            await bounded.aclose()
            await core_events.aclose()

    headers = {**_SECURITY_HEADERS}
    headers.pop("Cache-Control", None)
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"

    return owner.stream(events(), media_type="text/event-stream", headers=headers)


@router.post("/feedback")
async def ui_feedback(request: Request, req: UiFeedbackRequest) -> Response:
    """Forward one console rating to the feedback store (issue #780). Fixed
    client messages and stable codes. No rater identity is recorded: the agent
    port is reachable without the OAuth proxy, so a forwarded-user header
    proves nothing (attribution is deferred to the deployment, PR B)."""
    client = _feedback_client()
    if client is None:
        return _secure(JSONResponse(status_code=404, content={
            "code": "feedback_disabled", "message": _FEEDBACK_DISABLED_TEXT}))
    status, code, message = await _rate(client, req)
    if code is None:
        return _secure(JSONResponse(status_code=status, content={"status": "recorded"}))
    return _secure(JSONResponse(status_code=status, content={"code": code, "message": message}))


@router.post("/feedback/form", response_class=HTMLResponse)
async def ui_feedback_form(request: Request) -> Response:
    """Fallback console rating (#780): the same validation, store call and
    fixed texts as /ui/feedback. An htmx request gets the result text for the
    form's status slot, always 200 like the /ui/chat banner (htmx 1.9 never
    swaps a 4xx/5xx); the form stays, so a failed rating can be retried and a
    re-rating replaces the last. A native post (no JS, opened in a new tab)
    gets a full page with the real status."""
    client = _feedback_client()
    if client is None:
        return _feedback_result(request, _FEEDBACK_DISABLED_TEXT, 404)
    form = await request.form()
    try:
        req = UiFeedbackRequest.model_validate({
            "request_id": str(form.get("request_id", "")), "rating": str(form.get("rating", "")),
            "reasons": [str(value) for value in form.getlist("reasons")],
            "comment": str(form.get("comment", "")),
        })
    except ValidationError:
        status, _code, message = _FEEDBACK_OUTCOMES["invalid"]
        return _feedback_result(request, message, status)
    status, _code, message = await _rate(client, req)
    return _feedback_result(request, message, status)


async def _rate(client: Any, req: UiFeedbackRequest) -> tuple[int, str | None, str]:
    """Submit the rating and resolve the client's outcome to its one fixed response."""
    from mainframe_rag.feedback.store import Rating

    outcome = await client.rate(Rating(request_id=req.request_id, rating=req.rating,
                                       reasons=list(req.reasons), comment=req.comment))
    status, code, message = _FEEDBACK_OUTCOMES[outcome]
    return status, code, message.format(rating=req.rating) if outcome == "ok" else message


def _feedback_result(request: Request, text: str, status: int) -> Response:
    if request.headers.get("HX-Request") == "true":
        return _secure(HTMLResponse(html.escape(text), status_code=200))
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Answer rating</title>'
            '<link rel="stylesheet" href="/ui/static/css/console.css"></head>'
            f'<body class="theme-dark"><main class="feedback-result"><p role="status">{html.escape(text)}</p>'
            '<p>You can close this tab and return to the console.</p></main></body></html>')
    return _secure(HTMLResponse(page, status_code=status))


@router.get("/static/{path:path}")
async def ui_static(path: str) -> Response:
    target = (_STATIC_DIR / path).resolve()
    if not target.is_relative_to(_STATIC_DIR.resolve()) or not target.is_file():
        from mainframe_rag.agent.app import AppError

        raise AppError(404, "not_found", "not found")
    response = _secure(FileResponse(target))
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response
