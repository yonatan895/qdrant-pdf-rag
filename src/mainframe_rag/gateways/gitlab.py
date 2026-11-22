"""Read-only GitLab gateway (#1078, ADR-0010, ADR-0014).

An MCP server on the agent image, run as `python -m mainframe_rag.gateways.gitlab`.
It reads one GitLab instance through REST v4 with one team token (a group
access token holding `read_api` and at most `read_repository`, ADR-0014 E4),
looked up through a `GitLabTokenProvider`. It never writes:

- `GitLabClient` issues GET only (`UpstreamClient` with GET alone allowed, no
  redirects, no proxy environment, a byte cap, the configured origin and
  `/api/v4` prefix only);
- at start-up `check_token_scopes` reads `personal_access_tokens/self` and
  refuses to serve unless the token is active and holds only allowed scopes;
- every project is checked against the group and project allowlists before
  any call, and again by the `path_with_namespace` GitLab returns; every
  later call uses the numeric id that check resolved.

Callers authenticate in one of two modes (`GITLAB_AUTH_MODE`): `oidc` (the
default, ADR-0010's caller verification with a single-audience token, fixed
here) or `bearer` (ADR-0014's named exception: one shared secret held by
Open WebUI). Contract: docs/team-tools.md#gitlab-gateway.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote

import httpx2

from mainframe_rag.gateways.common import (
    REDACTED,
    GatewayConfig,
    GatewayConfigError,
    GatewayError,
    Limits,
    PrivateKeySpans,
    Tool,
    ToolContext,
    ToolOutput,
    UpstreamClient,
    UpstreamTail,
    clean_text,
    configure_gateway_logging,
    create_gateway_app,
    fit_text_tail,
    load_config,
)
from mainframe_rag.mcp.tool_auth import ASGIApp, JwksCache, Principal

log = logging.getLogger("gateway.gitlab")

GATEWAY: Final = "gitlab"
AUTH_MODE_ENV: Final = "GITLAB_AUTH_MODE"
BEARER_SECRET_ENV: Final = "GITLAB_GATEWAY_BEARER_SECRET"
TOKEN_ENV: Final = "GITLAB_TOKEN"
AUTH_MODES: Final = ("oidc", "bearer")
API_PREFIX: Final = "/api/v4"

# The only scopes the team token may hold (ADR-0014 E4). Anything else,
# known or not, refuses start-up: an allowlist, so a scope GitLab adds later
# is refused until reviewed.
REQUIRED_SCOPES: Final = frozenset({"read_api"})
ALLOWED_SCOPES: Final = frozenset({"read_api", "read_repository"})

_SEGMENT: Final = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}"
_PROJECT_RE: Final = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT}){{1,19}}")
_GROUP_RE: Final = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT}){{0,19}}")
_REF_PATTERN: Final = r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./+-]{0,254}"
_FILE_PATTERN: Final = r"(?!/)(?!.*(?:^|/)\.{1,2}(?:/|$))[^\x00-\x1f\x7f\\]{1,1024}"
_SEARCH_PATTERN: Final = r"[^\x00-\x1f\x7f]{1,200}"
_ANSI_RE: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]{0,256}(?:\x07|\x1b\\)")
# A log line that starts like one of the gateway's marker lines (below) is
# replaced, so a log cannot tell the model its end was not read (review N1;
# the z/OS bridge neutralises its observation delimiters the same way).
_MARKER_LOOKALIKE_RE: Final = re.compile(r"(?im)^([ \t]*)[\[\uff3b][ \t]*gateway[ \t]*:.*$")
NEUTRALIZED_MARKER: Final = "[neutralized-gateway-marker]"

# Bearer mode (ADR-0014 D4): every Open WebUI user is the one caller
# `service:openwebui`, so the per-caller limits are instance-wide. These
# replace the runtime defaults for keys the configuration leaves out
# (maintainer, 2026-11-21); oidc mode keeps the runtime defaults.
BEARER_LIMIT_DEFAULTS: Final = {"max_concurrent_per_user": 4, "rate_per_minute": 120}

# Issue and merge-request descriptions and note bodies are long documents
# (maintainer, 2026-11-21, item f): the runtime clips every string at 4096
# characters, which cut the team's runbooks, so GitLab raises it to 32768.
# GitLab titles and names are short, so the one cap does not lengthen them.
# The result cap is 128 KiB for GitLab (maintainer, 2026-11-21, round 3):
# room for a full 32768-character field of up to three-byte characters; a
# field of four-byte or escape-heavy characters is clipped to fit
# (render_untrusted). The shared `Limits` defaults, and so Splunk's, stay
# at 4096 characters and 64 KiB. Explicit configuration wins over both.
LONG_TEXT_LIMIT_DEFAULTS: Final = {"max_field_chars": 32768, "max_result_bytes": 128 * 1024}

# The fixed line after a list result that leaves rows out (maintainer,
# 2026-11-21, round 3): it names the start of the first row not shown.
CONTINUE_LINE: Final = (
    "[gateway: {count}more {noun} not shown; call {tool} again with start={start}"
    " and the other arguments unchanged]"
)
CONTINUE_PAST_LIMIT: Final = (
    "[gateway: more {noun} exist past start={limit}, the last {tool} can read;"
    " narrow the request]"
)

# The bridge-authored lines around a cut job log (docs/team-tools.md).
JOB_LOG_OMITTED: Final = "[gateway: about {} of earlier log omitted]\n"
JOB_LOG_UNREAD: Final = (
    "[gateway: the end of the log was not read; reading stopped after about {}]\n"
)
JOB_LOG_STOPPED: Final = "[gateway: reading stopped here; the end of the log was not read]\n"


class TokenScopeError(Exception):
    """The team token failed the start-up check. `reason` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- configuration -------------------------------------------------------------------


@dataclass(frozen=True)
class GitLabBackend:
    """The `backend` section of the gateway configuration file."""

    base_url: str
    allowed_groups: frozenset[str]
    allowed_projects: frozenset[str]
    ca_file: str | None = None
    timeout_s: float = 15.0
    max_response_bytes: int = 1024 * 1024
    max_file_bytes: int = 1024 * 1024
    max_job_log_read_bytes: int = 64 * 1024 * 1024
    max_job_log_read_s: float = 20.0
    per_page: int = 20
    max_page: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.startswith("https://"):
            raise GatewayConfigError("backend.base_url must be an https URL")
        if "?" in self.base_url or "#" in self.base_url or "@" in self.base_url:
            raise GatewayConfigError("backend.base_url must be a plain origin and path")
        for name, pattern in (("allowed_groups", _GROUP_RE), ("allowed_projects", _PROJECT_RE)):
            items = getattr(self, name)
            if not all(isinstance(i, str) and pattern.fullmatch(i) for i in items):
                raise GatewayConfigError(f"backend.{name} must hold GitLab full paths")
            object.__setattr__(self, name, frozenset(i.lower() for i in items))
        if not self.allowed_groups and not self.allowed_projects:
            raise GatewayConfigError("backend needs at least one allowed group or project")
        for name in (
            "max_response_bytes",
            "max_file_bytes",
            "max_job_log_read_bytes",
            "per_page",
            "max_page",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise GatewayConfigError(f"backend.{name} must be a positive integer")
        if self.per_page > 100:
            raise GatewayConfigError("backend.per_page must be at most 100")
        for name in ("timeout_s", "max_job_log_read_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 120
            ):
                raise GatewayConfigError(f"backend.{name} must be in (0, 120]")
        if self.ca_file is not None and not isinstance(self.ca_file, str):
            raise GatewayConfigError("backend.ca_file must be a path")

    def allows(self, path: str) -> bool:
        """Whether a project full path is in scope: listed itself, or under an
        allowed group at a segment boundary (`team` never admits `team-x`).
        GitLab paths are case-insensitive, so the comparison is too."""
        folded = path.lower()
        return folded in self.allowed_projects or any(
            folded.startswith(group + "/") for group in self.allowed_groups
        )


_BACKEND_KEYS: Final = frozenset(f.name for f in dataclasses.fields(GitLabBackend))


def backend_from_config(config: GatewayConfig) -> GitLabBackend:
    doc = dict(config.backend)
    unknown = sorted(set(doc) - _BACKEND_KEYS)
    if unknown:
        raise GatewayConfigError(f"backend has unknown keys: {', '.join(unknown)}")
    for key in ("allowed_groups", "allowed_projects"):
        value = doc.get(key, [])
        if not isinstance(value, list):
            raise GatewayConfigError(f"backend.{key} must be a list")
        doc[key] = frozenset(value)
    if "base_url" not in doc:
        raise GatewayConfigError("backend.base_url is required")
    return GitLabBackend(**doc)


# --- the team token ------------------------------------------------------------------


class GitLabTokenProvider(Protocol):
    """Which GitLab token serves a verified caller. One method, so a per-user
    provider (ADR-0010 TD4) can replace the static one without touching the
    tools, the client or the caller check."""

    async def token_for(self, principal: Principal) -> str: ...


class StaticGroupTokenProvider:
    """The one team token for every caller (ADR-0014 E4)."""

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token or token != token.strip() or " " in token:
            raise GatewayConfigError(f"{TOKEN_ENV} must be a non-empty token without spaces")
        self._token = token

    def __repr__(self) -> str:  # never the token
        return "StaticGroupTokenProvider()"

    async def token_for(self, principal: Principal) -> str:
        del principal  # every caller gets the same read-only token, by decision
        return self._token


# --- the REST client -----------------------------------------------------------------


class GitLabClient:
    """GET-only GitLab REST v4. Each call carries the provider's token in
    `PRIVATE-TOKEN`; nothing else in a request is caller-controlled except
    validated path segments (percent-encoded whole) and query parameters."""

    def __init__(
        self,
        backend: GitLabBackend,
        provider: GitLabTokenProvider,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.backend = backend
        self.provider = provider
        self.upstream = UpstreamClient(
            backend.base_url.rstrip("/") + API_PREFIX,
            timeout_s=backend.timeout_s,
            max_bytes=backend.max_response_bytes,
            ca_file=backend.ca_file,
            allowed_methods=frozenset({"GET"}),
            transport=transport,
        )

    async def _get(
        self,
        principal: Principal,
        path: str,
        params: Mapping[str, str | int] | None = None,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        token = await self.provider.token_for(principal)
        response = await self.upstream.request(
            "GET", path, params=params, headers={"PRIVATE-TOKEN": token}, max_bytes=max_bytes
        )
        return response.body

    async def get_json(
        self, principal: Principal, path: str, params: Mapping[str, str | int] | None = None
    ) -> Any:
        body = await self._get(principal, path, params)
        try:
            return json.loads(body)
        except ValueError, RecursionError:
            raise GatewayError("upstream_error") from None

    async def get_json_page(
        self, principal: Principal, path: str, params: Mapping[str, str | int]
    ) -> tuple[Any, bool | None, int | None]:
        """A JSON page, whether GitLab names a following page (`x-next-page`
        a page number: True; present but empty: False; absent or not a
        number: None) and its `x-total` (None when absent)."""
        token = await self.provider.token_for(principal)
        response = await self.upstream.request(
            "GET", path, params=params, headers={"PRIVATE-TOKEN": token}
        )

        def number(name: str) -> int | None:
            raw = response.headers.get(name, "").strip()
            return int(raw) if raw.isascii() and raw.isdigit() else None

        raw_next = response.headers.get("x-next-page")
        following = number("x-next-page")
        more: bool | None = None
        if raw_next is not None and not raw_next.strip():
            more = False  # GitLab's last page
        elif following is not None and following > 0:
            more = True
        return response.json(), more, number("x-total")

    async def get_tail(
        self,
        principal: Principal,
        path: str,
        *,
        keep_bytes: int,
        max_read_bytes: int,
        deadline_s: float,
        observe: Callable[[bytes], None] | None = None,
    ) -> UpstreamTail:
        token = await self.provider.token_for(principal)
        return await self.upstream.stream_tail(
            "GET",
            path,
            headers={"PRIVATE-TOKEN": token},
            keep_bytes=keep_bytes,
            max_read_bytes=max_read_bytes,
            deadline_s=deadline_s,
            observe=observe,
        )

    async def get_bytes(
        self,
        principal: Principal,
        path: str,
        params: Mapping[str, str | int] | None = None,
        *,
        max_bytes: int,
    ) -> bytes:
        return await self._get(principal, path, params, max_bytes=max_bytes)

    async def aclose(self) -> None:
        await self.upstream.aclose()


async def check_token_scopes(client: GitLabClient, token: str) -> list[str]:
    """Fail closed unless `token` is active, unrevoked, holds `read_api` and
    holds no scope outside ALLOWED_SCOPES. A GitLab that cannot answer
    (`personal_access_tokens/self` absent or refused) is a refusal too
    (ADR-0010 TD4b). Returns the scopes for the start-up log."""
    try:
        response = await client.upstream.request(
            "GET", "/personal_access_tokens/self", headers={"PRIVATE-TOKEN": token}
        )
        doc = json.loads(response.body)
    except GatewayError as exc:
        raise TokenScopeError(
            "scope_unverifiable" if exc.code == "not_found" else exc.code
        ) from None
    except ValueError, RecursionError:
        raise TokenScopeError("scope_unverifiable") from None
    if not isinstance(doc, dict):
        raise TokenScopeError("scope_unverifiable")
    scopes = doc.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise TokenScopeError("scope_unverifiable")
    if doc.get("active") is not True or doc.get("revoked") is not False:
        raise TokenScopeError("token_inactive")
    held = frozenset(scopes)
    if held - ALLOWED_SCOPES:
        raise TokenScopeError("token_scope_too_wide")
    if not held >= REQUIRED_SCOPES:
        raise TokenScopeError("token_scope_missing")
    return sorted(held)


# --- job logs ------------------------------------------------------------------------


def _about(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _redact_keys(data: bytes, start: int, keys: Sequence[tuple[int, int | None]]) -> bytes:
    """`data` (stream offset `start`) with every byte of a key span replaced
    by the redaction marker. A key open at the end runs to the end, short of
    the last newline."""
    out = bytearray()
    pos = 0
    for key_start, key_end in keys:
        low = max(key_start - start, pos)
        high = len(data) if key_end is None else min(key_end - start, len(data))
        if high == len(data) and data.endswith(b"\n"):
            high -= 1
        if high <= low:
            continue
        out += data[pos:low] + REDACTED.encode()
        pos = high
    return bytes(out + data[pos:])


def job_log_text(
    read: UpstreamTail,
    tail_bytes: int,
    limits: Limits,
    keys: Sequence[tuple[int, int | None]] = (),
) -> str:
    """The end of a streamed job log as whole, cleaned lines, with marker lines.

    Of the last `tail_bytes` read, a partial first line is dropped (and, when
    the read stopped before the end, a partial last line), so a cut never
    leaves a fragment of a secret that redaction would no longer recognise.
    `keys` are the private-key spans of the whole stream (`PrivateKeySpans`):
    every kept byte inside one is redacted, so a key whose BEGIN line lies
    before the kept lines, or whose END line was never read, leaves no line
    of its body. Log lines that read like a marker line are neutralised. The
    kept lines are cleaned whole, then lines are dropped from the start until
    the framed text fits `max_result_bytes`. A marker line says roughly how
    much came before; when the end was not read, a line before and after the
    text says so."""
    data = read.tail
    if read.total > tail_bytes:
        data = data[-(tail_bytes + 1) :]
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    start = read.total - len(data)  # the stream offset of the first kept byte
    skipped = start
    if not read.complete:
        data = data[: data.rfind(b"\n") + 1]
    data = _redact_keys(data, start, keys)
    text = clean_text(_ANSI_RE.sub("", data.decode("utf-8", errors="replace")))
    text = _MARKER_LOOKALIKE_RE.sub(lambda m: m.group(1) + NEUTRALIZED_MARKER, text)
    unread = "" if read.complete else JOB_LOG_UNREAD.format(_about(read.total))
    stopped = "" if read.complete else JOB_LOG_STOPPED
    kept = fit_text_tail(text, limits, prefix=unread, suffix=stopped)
    if skipped or len(kept) < len(text):
        # Fit again with the longest marker this could need, then write it.
        widest = unread + JOB_LOG_OMITTED.format(_about(2**60))
        kept = fit_text_tail(text, limits, prefix=widest, suffix=stopped)
        omitted = skipped + len(text[: len(text) - len(kept)].encode("utf-8"))
        return unread + JOB_LOG_OMITTED.format(_about(omitted)) + kept + stopped
    return unread + kept + stopped


# --- tools ---------------------------------------------------------------------------


def _enc(value: str) -> str:
    return quote(value, safe="")


def _pick(item: object, keys: Sequence[str]) -> dict[str, Any]:
    """The named fields of a GitLab object; `author`/`assignee` become the
    username alone (no e-mail, avatar or URL)."""
    if not isinstance(item, Mapping):
        raise GatewayError("upstream_error")
    out: dict[str, Any] = {}
    for key in keys:
        value = item.get(key)
        if key in ("author", "assignee", "user") and isinstance(value, Mapping):
            value = value.get("username")
        out[key] = value
    return out


def _not_system(item: object) -> bool:
    return not (isinstance(item, Mapping) and item.get("system") is True)


def _rows(items: object, keys: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise GatewayError("upstream_error")
    return [_pick(item, keys) for item in items]


_PROJECT_KEYS: Final = (
    "path_with_namespace",
    "name",
    "description",
    "default_branch",
    "visibility",
    "web_url",
    "last_activity_at",
    "archived",
)
_MR_KEYS: Final = (
    "iid",
    "title",
    "state",
    "author",
    "source_branch",
    "target_branch",
    "draft",
    "merge_status",
    "created_at",
    "updated_at",
    "merged_at",
    "web_url",
)
_ISSUE_KEYS: Final = (
    "iid",
    "title",
    "state",
    "author",
    "assignee",
    "labels",
    "user_notes_count",
    "created_at",
    "updated_at",
    "closed_at",
    "web_url",
)
_NOTE_KEYS: Final = ("id", "author", "body", "created_at", "updated_at", "system", "internal")
_PIPELINE_KEYS: Final = (
    "id",
    "iid",
    "status",
    "ref",
    "sha",
    "source",
    "created_at",
    "updated_at",
    "web_url",
)
_JOB_KEYS: Final = (
    "id",
    "name",
    "stage",
    "status",
    "ref",
    "created_at",
    "started_at",
    "finished_at",
    "duration",
    "failure_reason",
    "allow_failure",
    "web_url",
)


def _string(description: str, pattern: str, max_length: int) -> dict[str, Any]:
    return {
        "type": "string",
        "description": description,
        "pattern": pattern,
        "maxLength": max_length,
    }


def _int(description: str, low: int, high: int) -> dict[str, Any]:
    return {"type": "integer", "description": description, "minimum": low, "maximum": high}


def _schema(props: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolOutput]]


class GitLabTools:
    """The fixed read-only tool list. Every per-project tool first resolves
    the project (allowlist, then GitLab's own answer), then works by id."""

    def __init__(self, client: GitLabClient) -> None:
        self.client = client
        self.backend = client.backend

    async def _project(self, ctx: ToolContext, path: str) -> tuple[int, dict[str, Any]]:
        if not _PROJECT_RE.fullmatch(path) or not self.backend.allows(path):
            raise GatewayError("project_not_allowed")
        doc = await self.client.get_json(ctx.principal, f"/projects/{_enc(path)}")
        if not isinstance(doc, dict):
            raise GatewayError("upstream_error")
        project_id, resolved = doc.get("id"), doc.get("path_with_namespace")
        # A renamed or moved project answers under its old path: the path it
        # now has must be in scope too.
        if (
            isinstance(project_id, bool)
            or not isinstance(project_id, int)
            or not isinstance(resolved, str)
            or not self.backend.allows(resolved)
        ):
            raise GatewayError("project_not_allowed")
        return project_id, doc

    @property
    def max_start(self) -> int:
        """The last row a paged tool may start at: as deep as `max_page` pages
        of `per_page` rows."""
        return self.backend.max_page * self.backend.per_page - 1

    async def _list(
        self,
        ctx: ToolContext,
        args: Mapping[str, Any],
        *,
        tool: str,
        noun: str,
        path: str,
        params: Mapping[str, str | int],
        keys: Sequence[str],
        per_page: int | None = None,
        keep: Callable[[object], bool] | None = None,
    ) -> ToolOutput:
        """One GitLab list call from row `start` (args), as rows with a
        continuation. GitLab pages by number, so the page holding `start` is
        read and the rows before it skipped; every row keeps its index in
        GitLab's order, so the continuation names the first row the result
        did not show (dropped to fit the cap, or on the next page). `keep`
        filters rows here; those rows still count in the index, and the
        remaining count is then given as "up to". A page larger than
        `max_response_bytes` is asked for again with fewer rows."""
        size = per_page or self.backend.per_page
        start = int(args.get("start", 0))
        while True:
            page = start // size + 1
            first = (page - 1) * size
            try:
                items, more, total = await self.client.get_json_page(
                    ctx.principal, path, {**params, "per_page": size, "page": page}
                )
                break
            except GatewayError as exc:
                # A page of long rows can pass max_response_bytes: `start` does
                # not depend on the page size, so ask for fewer rows (20, 5, 1).
                if exc.code != "upstream_too_large" or size == 1:
                    raise
                size = max(1, size // 4)
        if not isinstance(items, list):
            raise GatewayError("upstream_error")
        if more is None:  # no usable header: a full page may have a next one
            more = len(items) >= size
        indexed = [
            (first + index, item)
            for index, item in enumerate(items)
            if first + index >= start and (keep is None or keep(item))
        ]
        rows = [_pick(item, keys) for _, item in indexed]
        limit = self.max_start

        def continuation(shown: int) -> str | None:
            if shown < len(indexed):
                following = indexed[shown][0]
            elif more:
                following = first + size
            else:
                return None
            if following > limit:
                return CONTINUE_PAST_LIMIT.format(noun=noun, limit=limit, tool=tool)
            count = ""
            if total is not None and total > following:
                count = f"{'up to ' if keep is not None else ''}{total - following} "
            return CONTINUE_LINE.format(count=count, noun=noun, tool=tool, start=following)

        return ToolOutput(rows=rows, continuation=continuation)

    async def list_projects(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        params: dict[str, str | int] = {
            "per_page": self.backend.per_page,
            "page": int(args.get("page", 1)),
            "include_subgroups": "true",
            "archived": "false",
            "order_by": "last_activity_at",
        }
        search = args.get("search")
        if search:
            params["search"] = search
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in sorted(self.backend.allowed_groups):
            items = await self.client.get_json(
                ctx.principal, f"/groups/{_enc(group)}/projects", params
            )
            for row in _rows(items, _PROJECT_KEYS):
                path = row.get("path_with_namespace")
                if isinstance(path, str) and self.backend.allows(path) and path.lower() not in seen:
                    seen.add(path.lower())
                    rows.append(row)
        if int(args.get("page", 1)) == 1:
            for path in sorted(self.backend.allowed_projects - seen):
                if search and search.lower() not in path:
                    continue
                if any(path.startswith(g + "/") for g in self.backend.allowed_groups):
                    continue
                try:
                    _, doc = await self._project(ctx, path)
                except GatewayError as exc:
                    if exc.code in ("not_found", "project_not_allowed"):
                        continue
                    raise
                rows.append(_pick(doc, _PROJECT_KEYS))
        return ToolOutput(rows=rows)

    async def get_project(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        _, doc = await self._project(ctx, args["project"])
        return ToolOutput(rows=[_pick(doc, (*_PROJECT_KEYS, "topics", "open_issues_count"))])

    async def list_tree(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {
            "recursive": str(bool(args.get("recursive", False))).lower(),
        }
        for key in ("path", "ref"):
            if args.get(key):
                params[key] = args[key]
        return await self._list(
            ctx,
            args,
            tool="list_repository_tree",
            noun="entries",
            path=f"/projects/{project_id}/repository/tree",
            params=params,
            keys=("path", "type", "name"),
        )

    async def read_file(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, project = await self._project(ctx, args["project"])
        ref = args.get("ref") or project.get("default_branch")
        params: dict[str, str | int] = {"ref": ref} if isinstance(ref, str) and ref else {}
        body = await self.client.get_bytes(
            ctx.principal,
            f"/projects/{project_id}/repository/files/{_enc(args['file_path'])}/raw",
            params,
            max_bytes=self.backend.max_file_bytes,
        )
        if b"\x00" in body:
            raise GatewayError("binary_content")
        return ToolOutput(text=body.decode("utf-8", errors="replace"))

    async def search_code(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {"scope": "blobs", "search": args["query"]}
        if args.get("ref"):
            params["ref"] = args["ref"]
        return await self._list(
            ctx,
            args,
            tool="search_code",
            noun="matches",
            path=f"/projects/{project_id}/search",
            params=params,
            keys=("path", "ref", "startline", "data"),
        )

    async def list_commits(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {}
        if args.get("ref"):
            params["ref_name"] = args["ref"]
        if args.get("path"):
            params["path"] = args["path"]
        return await self._list(
            ctx,
            args,
            tool="list_commits",
            noun="commits",
            path=f"/projects/{project_id}/repository/commits",
            params=params,
            keys=("short_id", "title", "author_name", "created_at", "web_url"),
        )

    async def list_merge_requests(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {"state": args.get("state", "opened")}
        if args.get("search"):
            params["search"] = args["search"]
        return await self._list(
            ctx,
            args,
            tool="list_merge_requests",
            noun="merge requests",
            path=f"/projects/{project_id}/merge_requests",
            params=params,
            keys=_MR_KEYS,
        )

    async def get_merge_request(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        doc = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/merge_requests/{args['iid']}"
        )
        return ToolOutput(rows=[_pick(doc, (*_MR_KEYS, "description", "labels"))])

    async def get_merge_request_diffs(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        return await self._list(
            ctx,
            args,
            tool="get_merge_request_diffs",
            noun="file diffs",
            path=f"/projects/{project_id}/merge_requests/{args['iid']}/diffs",
            params={},
            keys=("old_path", "new_path", "new_file", "deleted_file", "renamed_file", "diff"),
        )

    async def list_issues(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {"state": args.get("state", "opened")}
        for key in ("search", "labels"):
            if args.get(key):
                params[key] = args[key]
        return await self._list(
            ctx,
            args,
            tool="list_issues",
            noun="issues",
            path=f"/projects/{project_id}/issues",
            params=params,
            keys=_ISSUE_KEYS,
        )

    async def get_issue(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        doc = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/issues/{args['iid']}"
        )
        return ToolOutput(rows=[_pick(doc, (*_ISSUE_KEYS, "description"))])

    async def list_pipelines(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {}
        for key in ("ref", "status"):
            if args.get(key):
                params[key] = args[key]
        return await self._list(
            ctx,
            args,
            tool="list_pipelines",
            noun="pipelines",
            path=f"/projects/{project_id}/pipelines",
            params=params,
            keys=_PIPELINE_KEYS,
        )

    async def list_pipeline_jobs(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        return await self._list(
            ctx,
            args,
            tool="list_pipeline_jobs",
            noun="jobs",
            path=f"/projects/{project_id}/pipelines/{args['pipeline_id']}/jobs",
            params={},
            keys=_JOB_KEYS,
        )

    async def list_issue_notes(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        """One issue's notes, oldest first by default. System notes (label,
        state and description changes) are left out unless `include_system`:
        asked of GitLab with `activity_filter` and filtered here as well, for
        a GitLab without that parameter (assumed to ignore it: a site check)."""
        project_id, _ = await self._project(ctx, args["project"])
        include_system = bool(args.get("include_system", False))
        params: dict[str, str | int] = {"sort": args.get("sort", "asc"), "order_by": "created_at"}
        if not include_system:
            params["activity_filter"] = "only_comments"
        return await self._list(
            ctx,
            args,
            tool="list_issue_notes",
            noun="notes",
            path=f"/projects/{project_id}/issues/{args['iid']}/notes",
            params=params,
            keys=_NOTE_KEYS,
            per_page=int(args.get("per_page", self.backend.per_page)),
            keep=None if include_system else _not_system,
        )

    async def read_job_log(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        """The end of a job log, streamed: only the last `tail_bytes` are kept
        in memory, however long the log. Reading stops after
        `max_job_log_read_bytes` or `max_job_log_read_s` (counted from the
        start of the call), and what was read is returned, marked."""
        started = time.monotonic()
        project_id, _ = await self._project(ctx, args["project"])
        # The result cap clamps the tail; job_log_text keeps the end.
        tail_bytes = min(
            int(args.get("tail_bytes", ctx.limits.max_result_bytes // 2)),
            ctx.limits.max_result_bytes,
        )
        remaining = self.backend.max_job_log_read_s - (time.monotonic() - started)
        if remaining <= 0:
            raise GatewayError("upstream_timeout")
        keys = PrivateKeySpans(horizon=tail_bytes + 1)
        read = await self.client.get_tail(
            ctx.principal,
            f"/projects/{project_id}/jobs/{args['job_id']}/trace",
            keep_bytes=tail_bytes + 1,
            max_read_bytes=self.backend.max_job_log_read_bytes,
            deadline_s=remaining,
            observe=keys.feed,
        )
        return ToolOutput(text=job_log_text(read, tail_bytes, ctx.limits, keys.finish()))

    def tools(self) -> list[Tool]:
        b = self.backend
        project = _string(
            "Project full path, e.g. group/subgroup/project", _PROJECT_RE.pattern, 1024
        )
        page = _int("Result page, from 1", 1, b.max_page)
        start = _int(
            "Index of the first row to return, from 0. A result that leaves rows"
            " out ends with a line naming the start to call next.",
            0,
            b.max_page * b.per_page - 1,
        )
        ref = _string("Branch, tag or commit SHA", _REF_PATTERN, 255)
        iid = _int("Project-scoped number (iid)", 1, 2**31 - 1)
        search = _string("Text to search for", _SEARCH_PATTERN, 200)
        state = {"type": "string", "enum": ["opened", "closed", "merged", "all"]}
        issue_state = {"type": "string", "enum": ["opened", "closed", "all"]}
        pipeline_status = {
            "type": "string",
            "enum": [
                "created",
                "pending",
                "running",
                "success",
                "failed",
                "canceled",
                "skipped",
                "manual",
                "scheduled",
            ],
        }
        path = _string("Repository path", _FILE_PATTERN, 1024)

        def tool(
            name: str,
            description: str,
            handler: Handler,
            props: dict[str, Any],
            required: Sequence[str] = (),
        ) -> Tool:
            return Tool(name, description, _schema(props, required), handler)

        return [
            tool(
                "list_projects",
                "List projects in scope, optionally filtered by name.",
                self.list_projects,
                {"search": search, "page": page},
            ),
            tool(
                "get_project",
                "Read one project's metadata.",
                self.get_project,
                {"project": project},
                ["project"],
            ),
            tool(
                "list_repository_tree",
                "List files and directories at a path and ref.",
                self.list_tree,
                {
                    "project": project,
                    "path": path,
                    "ref": ref,
                    "recursive": {"type": "boolean"},
                    "start": start,
                },
                ["project"],
            ),
            tool(
                "read_file",
                "Read one text file at a ref (default branch when omitted).",
                self.read_file,
                {"project": project, "file_path": path, "ref": ref},
                ["project", "file_path"],
            ),
            tool(
                "search_code",
                "Search file contents in one project.",
                self.search_code,
                {"project": project, "query": search, "ref": ref, "start": start},
                ["project", "query"],
            ),
            tool(
                "list_commits",
                "List commits on a ref, optionally for one path.",
                self.list_commits,
                {"project": project, "ref": ref, "path": path, "start": start},
                ["project"],
            ),
            tool(
                "list_merge_requests",
                "List merge requests.",
                self.list_merge_requests,
                {"project": project, "state": state, "search": search, "start": start},
                ["project"],
            ),
            tool(
                "get_merge_request",
                "Read one merge request with its description.",
                self.get_merge_request,
                {"project": project, "iid": iid},
                ["project", "iid"],
            ),
            tool(
                "get_merge_request_diffs",
                "Read the file diffs of one merge request.",
                self.get_merge_request_diffs,
                {"project": project, "iid": iid, "start": start},
                ["project", "iid"],
            ),
            tool(
                "list_issues",
                "List issues.",
                self.list_issues,
                {
                    "project": project,
                    "state": issue_state,
                    "search": search,
                    "labels": _string("Comma-separated labels", r"[^\x00-\x1f\x7f]{1,200}", 200),
                    "start": start,
                },
                ["project"],
            ),
            tool(
                "get_issue",
                "Read one issue with its description.",
                self.get_issue,
                {"project": project, "iid": iid},
                ["project", "iid"],
            ),
            tool(
                "list_issue_notes",
                "List the comments on one issue, oldest first (sort=desc for newest"
                " first); system notes only with include_system. A result that"
                " leaves notes out ends with a line naming the start to call next.",
                self.list_issue_notes,
                {
                    "project": project,
                    "iid": iid,
                    "start": start,
                    "per_page": _int("Notes per page", 1, b.per_page),
                    "sort": {"type": "string", "enum": ["asc", "desc"]},
                    "include_system": {"type": "boolean"},
                },
                ["project", "iid"],
            ),
            tool(
                "list_pipelines",
                "List CI pipelines.",
                self.list_pipelines,
                {"project": project, "ref": ref, "status": pipeline_status, "start": start},
                ["project"],
            ),
            tool(
                "list_pipeline_jobs",
                "List the jobs of one pipeline.",
                self.list_pipeline_jobs,
                {"project": project, "pipeline_id": _int("Pipeline id", 1, 2**53), "start": start},
                ["project", "pipeline_id"],
            ),
            tool(
                "read_job_log",
                "Read the end of one CI job's log.",
                self.read_job_log,
                {
                    "project": project,
                    "job_id": _int("Job id", 1, 2**53),
                    "tail_bytes": _int("How many bytes from the end", 1024, 1024 * 1024),
                },
                ["project", "job_id"],
            ),
        ]


# --- assembly and entry point ----------------------------------------------------------


@dataclass
class Gateway:
    app: ASGIApp
    client: GitLabClient
    token: str
    mode: str
    limits: Limits


def auth_mode(environ: Mapping[str, str]) -> str:
    mode = environ.get(AUTH_MODE_ENV, "oidc").strip().lower() or "oidc"
    if mode not in AUTH_MODES:
        raise GatewayConfigError(f"{AUTH_MODE_ENV} must be oidc or bearer")
    return mode


def build_gateway(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    jwks: JwksCache | None = None,
) -> Gateway:
    """Read the configuration and secrets, then assemble the app. Tests pass
    `transport` (a fake GitLab) and `jwks`; nothing here touches the network."""
    env = os.environ if environ is None else environ
    mode = auth_mode(env)
    config = load_config(
        GATEWAY,
        env,
        require_auth=mode == "oidc",
        limit_defaults={**LONG_TEXT_LIMIT_DEFAULTS, **(BEARER_LIMIT_DEFAULTS if mode == "bearer" else {})},
    )
    backend = backend_from_config(config)
    if backend.max_job_log_read_s >= config.limits.tool_timeout_s:
        # Reading must stop, and the tail be returned, before the call is cancelled.
        raise GatewayConfigError("backend.max_job_log_read_s must be below limits.tool_timeout_s")
    token = env.get(TOKEN_ENV, "")
    provider = StaticGroupTokenProvider(token)
    client = GitLabClient(backend, provider, transport=transport)
    tools = GitLabTools(client).tools()
    if mode == "bearer":
        secret = env.get(BEARER_SECRET_ENV, "")
        if not secret:
            raise GatewayConfigError(f"{BEARER_SECRET_ENV} is required in bearer mode")
        app = create_gateway_app(config, tools, bearer_secret=secret)
    else:
        if config.auth is None:  # load_config requires it in this mode
            raise GatewayConfigError("auth is required in oidc mode")
        # ADR-0010 TD6: exactly this gateway's audience, fixed here.
        fixed = dataclasses.replace(config.auth, require_single_audience=True)
        app = create_gateway_app(dataclasses.replace(config, auth=fixed), tools, jwks=jwks)
    return Gateway(app=app, client=client, token=token, mode=mode, limits=config.limits)


async def _checked_scopes(gateway: Gateway) -> list[str]:
    """The start-up check on its own client, closed in the same event loop
    (uvicorn serves on another loop with a fresh client)."""
    try:
        return await check_token_scopes(gateway.client, gateway.token)
    finally:
        await gateway.client.aclose()


def _site_probe(argv: Sequence[str]) -> int:
    """`--site-probe` (#1343): the read-only ADR-0014 site checks with the
    gateway's configuration and client; a text-free receipt on stdout (and
    `--receipt FILE`, 0600). Exit 1 when a check failed, 2 on a configuration
    or target error (fixed messages, never a target or the token)."""
    import argparse
    from pathlib import Path

    from mainframe_rag.gateways import gitlab_probe
    from mainframe_rag.probe_receipt import write_receipt

    parser = argparse.ArgumentParser(prog="python -m mainframe_rag.gateways.gitlab")
    parser.add_argument("--site-probe", action="store_true", required=True)
    parser.add_argument("--job", default=None, help="PROJECT_PATH:JOB_ID whose log to stream")
    parser.add_argument("--issue", default=None, help="PROJECT_PATH:IID whose notes to read")
    parser.add_argument("--receipt", type=Path, default=None, help="also write the receipt (0600)")
    args = parser.parse_args(list(argv))
    try:
        gateway = build_gateway()
        allows = gateway.client.backend.allows
        job = gitlab_probe.parse_target(args.job, allows, "job") if args.job else None
        issue = gitlab_probe.parse_target(args.issue, allows, "issue") if args.issue else None
    except (GatewayConfigError, ValueError) as exc:
        print(f"gitlab site probe: invalid configuration or target ({exc})", file=sys.stderr)
        return 2
    receipt = asyncio.run(gitlab_probe.run_gitlab_probe(gateway, job=job, issue=issue))
    try:
        write_receipt(receipt, sys.stdout, args.receipt)
    except OSError:
        print("gitlab site probe: the receipt file could not be written", file=sys.stderr)
        return 2
    return receipt.exit_code()


def main(argv: Sequence[str] | None = None) -> int:
    """Start-up: configuration, then the token scope check, then serve. Any
    failure exits non-zero with a fixed reason; the token is never logged.
    `--site-probe` runs the read-only site probe instead of serving."""
    from mainframe_rag.gateways.common import serve

    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if "--site-probe" in args:
            return _site_probe(args)
        print("gitlab gateway: the only option is --site-probe", file=sys.stderr)
        return 2
    configure_gateway_logging()
    try:
        scopes = asyncio.run(_checked_scopes(build_gateway()))
        gateway = build_gateway()
    except GatewayConfigError as exc:
        log.error(
            json.dumps(
                {"action": "gitlab_gateway_start", "decision": "config_error", "detail": str(exc)}
            )
        )
        return 2
    except TokenScopeError as exc:
        log.error(json.dumps({"action": "gitlab_gateway_start", "decision": exc.reason}))
        return 3
    log.info(
        json.dumps(
            {
                "action": "gitlab_gateway_start",
                "decision": "ok",
                "mode": gateway.mode,
                "scopes": scopes,
            }
        )
    )
    serve(gateway.app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
