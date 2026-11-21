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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote

import httpx2

from mainframe_rag.gateways.common import (
    GatewayConfig,
    GatewayConfigError,
    GatewayError,
    Tool,
    ToolContext,
    ToolOutput,
    UpstreamClient,
    create_gateway_app,
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
    max_job_log_bytes: int = 8 * 1024 * 1024
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
            "max_job_log_bytes",
            "per_page",
            "max_page",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise GatewayConfigError(f"backend.{name} must be a positive integer")
        if self.per_page > 100:
            raise GatewayConfigError("backend.per_page must be at most 100")
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not 0 < self.timeout_s <= 120
        ):
            raise GatewayConfigError("backend.timeout_s must be in (0, 120]")
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
    "created_at",
    "updated_at",
    "closed_at",
    "web_url",
)
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

    def _paging(self, args: Mapping[str, Any]) -> dict[str, str | int]:
        return {"per_page": self.backend.per_page, "page": int(args.get("page", 1))}

    async def list_projects(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        params: dict[str, str | int] = {
            **self._paging(args),
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
            **self._paging(args),
            "recursive": str(bool(args.get("recursive", False))).lower(),
        }
        for key in ("path", "ref"):
            if args.get(key):
                params[key] = args[key]
        items = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/repository/tree", params
        )
        return ToolOutput(rows=_rows(items, ("path", "type", "name")))

    async def read_file(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {"ref": args["ref"]} if args.get("ref") else {}
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
        params: dict[str, str | int] = {
            **self._paging(args),
            "scope": "blobs",
            "search": args["query"],
        }
        if args.get("ref"):
            params["ref"] = args["ref"]
        items = await self.client.get_json(ctx.principal, f"/projects/{project_id}/search", params)
        return ToolOutput(rows=_rows(items, ("path", "ref", "startline", "data")))

    async def list_commits(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = self._paging(args)
        if args.get("ref"):
            params["ref_name"] = args["ref"]
        if args.get("path"):
            params["path"] = args["path"]
        items = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/repository/commits", params
        )
        return ToolOutput(
            rows=_rows(items, ("short_id", "title", "author_name", "created_at", "web_url"))
        )

    async def list_merge_requests(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {**self._paging(args), "state": args.get("state", "opened")}
        if args.get("search"):
            params["search"] = args["search"]
        items = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/merge_requests", params
        )
        return ToolOutput(rows=_rows(items, _MR_KEYS))

    async def get_merge_request(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        doc = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/merge_requests/{args['iid']}"
        )
        return ToolOutput(rows=[_pick(doc, (*_MR_KEYS, "description", "labels"))])

    async def get_merge_request_diffs(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        items = await self.client.get_json(
            ctx.principal,
            f"/projects/{project_id}/merge_requests/{args['iid']}/diffs",
            self._paging(args),
        )
        keys = ("old_path", "new_path", "new_file", "deleted_file", "renamed_file", "diff")
        return ToolOutput(rows=_rows(items, keys))

    async def list_issues(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = {**self._paging(args), "state": args.get("state", "opened")}
        for key in ("search", "labels"):
            if args.get(key):
                params[key] = args[key]
        items = await self.client.get_json(ctx.principal, f"/projects/{project_id}/issues", params)
        return ToolOutput(rows=_rows(items, _ISSUE_KEYS))

    async def get_issue(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        doc = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/issues/{args['iid']}"
        )
        return ToolOutput(rows=[_pick(doc, (*_ISSUE_KEYS, "description"))])

    async def list_pipelines(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        params: dict[str, str | int] = self._paging(args)
        for key in ("ref", "status"):
            if args.get(key):
                params[key] = args[key]
        items = await self.client.get_json(
            ctx.principal, f"/projects/{project_id}/pipelines", params
        )
        return ToolOutput(rows=_rows(items, _PIPELINE_KEYS))

    async def list_pipeline_jobs(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        project_id, _ = await self._project(ctx, args["project"])
        items = await self.client.get_json(
            ctx.principal,
            f"/projects/{project_id}/pipelines/{args['pipeline_id']}/jobs",
            self._paging(args),
        )
        return ToolOutput(rows=_rows(items, _JOB_KEYS))

    async def read_job_log(self, ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
        """The tail of a job log, ANSI colour codes removed. A log longer than
        `max_job_log_bytes` is refused (`upstream_too_large`), never read on."""
        project_id, _ = await self._project(ctx, args["project"])
        body = await self.client.get_bytes(
            ctx.principal,
            f"/projects/{project_id}/jobs/{args['job_id']}/trace",
            max_bytes=self.backend.max_job_log_bytes,
        )
        tail_bytes = int(args.get("tail_bytes", ctx.limits.max_result_bytes // 2))
        text = _ANSI_RE.sub("", body[-tail_bytes:].decode("utf-8", errors="replace"))
        if len(body) > tail_bytes:
            text = "[earlier log lines omitted]\n" + text
        return ToolOutput(text=text)

    def tools(self) -> list[Tool]:
        b = self.backend
        project = _string(
            "Project full path, e.g. group/subgroup/project", _PROJECT_RE.pattern, 1024
        )
        page = _int("Result page, from 1", 1, b.max_page)
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
                    "page": page,
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
                {"project": project, "query": search, "ref": ref, "page": page},
                ["project", "query"],
            ),
            tool(
                "list_commits",
                "List commits on a ref, optionally for one path.",
                self.list_commits,
                {"project": project, "ref": ref, "path": path, "page": page},
                ["project"],
            ),
            tool(
                "list_merge_requests",
                "List merge requests.",
                self.list_merge_requests,
                {"project": project, "state": state, "search": search, "page": page},
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
                {"project": project, "iid": iid, "page": page},
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
                    "page": page,
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
                "list_pipelines",
                "List CI pipelines.",
                self.list_pipelines,
                {"project": project, "ref": ref, "status": pipeline_status, "page": page},
                ["project"],
            ),
            tool(
                "list_pipeline_jobs",
                "List the jobs of one pipeline.",
                self.list_pipeline_jobs,
                {"project": project, "pipeline_id": _int("Pipeline id", 1, 2**53), "page": page},
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
    config = load_config(GATEWAY, env, require_auth=mode == "oidc")
    backend = backend_from_config(config)
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
    return Gateway(app=app, client=client, token=token, mode=mode)


async def _checked_scopes(gateway: Gateway) -> list[str]:
    """The start-up check on its own client, closed in the same event loop
    (uvicorn serves on another loop with a fresh client)."""
    try:
        return await check_token_scopes(gateway.client, gateway.token)
    finally:
        await gateway.client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Start-up: configuration, then the token scope check, then serve. Any
    failure exits non-zero with a fixed reason; the token is never logged."""
    from mainframe_rag.gateways.common import serve
    from mainframe_rag.logs import configure_logging

    del argv
    configure_logging("info")
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
