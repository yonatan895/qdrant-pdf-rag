"""Read-only GitLab site probe for the team-token gateway (#1343, ADR-0014).

`python -m mainframe_rag.gateways.gitlab --site-probe [--job PROJECT:JOB_ID]
[--issue PROJECT:IID] [--receipt FILE]` runs in the gateway pod with the
gateway's own configuration, client and checks (`build_gateway`, the GET-only
`GitLabClient`, `check_token_scopes`, the project allowlist resolution) and
records the ADR-0014 site checks in a text-free receipt
(`mainframe_rag.probe_receipt`):

- `gitlab.version`: `GET /version`, as major/minor/patch integers (17.x passes;
  another major is `inconclusive`, a human decides);
- `gitlab.token_scopes`: the start-up scope check, and that the token holds
  `read_api` alone (ADR-0014 D1: `read_repository` only once a tool needs it);
- `gitlab.job_trace_stream`: the named job's log streamed to its end within
  the gateway's read ceilings (bytes and chunks counted, nothing kept);
- `gitlab.issue_notes_filter`: the named issue's first notes page with and
  without `activity_filter=only_comments`, counting system notes in each.

Job and issue checks run only for targets the operator names, inside the
allowlists; otherwise they are `inconclusive`. Nothing from a response but
counts, booleans, version integers and fixed codes reaches the receipt.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mainframe_rag.gateways.common import GatewayError, ToolContext
from mainframe_rag.gateways.gitlab import (
    _PROJECT_RE,
    GATEWAY,
    Gateway,
    GitLabTools,
    TokenScopeError,
    check_token_scopes,
)
from mainframe_rag.mcp.tool_auth import Principal
from mainframe_rag.probe_receipt import Receipt

PROBE = "gitlab"
CHECKS: tuple[str, ...] = (
    "gitlab.version",
    "gitlab.token_scopes",
    "gitlab.job_trace_stream",
    "gitlab.issue_notes_filter",
)
EXPECTED_MAJOR = 17
_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
# The probe keeps only this many trailing bytes of a job log, and drops them.
JOB_TAIL_KEEP_BYTES = 1024
NOTES_PAGE_ROWS = 100
_PRINCIPAL = Principal(sub="site-probe", groups=frozenset())


@dataclass(frozen=True, slots=True)
class Target:
    """An operator-named project path and a positive id (job id or issue iid)."""

    project: str
    number: int


def parse_target(raw: str, backend_allows: Callable[[str], bool], label: str) -> Target:
    """`PROJECT:NUMBER` inside the allowlists; ValueError with a fixed message."""
    project, sep, number = raw.rpartition(":")
    if (
        not sep
        or not _PROJECT_RE.fullmatch(project)
        or not number.isascii()
        or not number.isdigit()
        or int(number) < 1
    ):
        raise ValueError(f"--{label} must be PROJECT_PATH:NUMBER")
    if not backend_allows(project):
        raise ValueError(f"--{label} names a project outside the gateway allowlists")
    return Target(project=project, number=int(number))


class _Probe:
    def __init__(self, gateway: Gateway, clock: Callable[[], float]) -> None:
        self.gateway = gateway
        self.client = gateway.client
        self.tools = GitLabTools(gateway.client)
        self.clock = clock
        self.ctx = ToolContext(
            gateway=GATEWAY,
            principal=_PRINCIPAL,
            request_id="site-probe",
            sub_hash="site-probe",
            limits=gateway.limits,
        )
        self.receipt = Receipt(PROBE)

    async def version(self) -> None:
        check = "gitlab.version"
        try:
            doc = await self.client.get_json(_PRINCIPAL, "/version")
        except GatewayError as exc:
            self.receipt.add(check, "fail", reason=exc.code)
            return
        raw = doc.get("version") if isinstance(doc, dict) else None
        match = _VERSION_RE.match(raw) if isinstance(raw, str) else None
        if match is None:
            self.receipt.add(check, "inconclusive", reason="unparsed")
            return
        major, minor, patch = (int(part) for part in match.groups())
        facts = {"major": major, "minor": minor, "patch": patch}
        if major == EXPECTED_MAJOR:
            self.receipt.add(check, "pass", **facts)
        else:
            self.receipt.add(check, "inconclusive", reason="other_major", **facts)

    async def token_scopes(self) -> None:
        check = "gitlab.token_scopes"
        try:
            scopes = await check_token_scopes(self.client, self.gateway.token)
        except TokenScopeError as exc:
            self.receipt.add(check, "fail", reason=exc.reason)
            return
        held = frozenset(scopes)
        facts = {
            "read_api": "read_api" in held,
            "read_repository": "read_repository" in held,
            "scope_count": len(held),
        }
        if held == {"read_api"}:
            self.receipt.add(check, "pass", **facts)
        else:
            self.receipt.add(check, "fail", reason="wider_than_read_api", **facts)

    async def _project_id(self, check: str, target: Target) -> int | None:
        try:
            project_id, _ = await self.tools._project(self.ctx, target.project)
        except GatewayError as exc:
            self.receipt.add(check, "inconclusive", reason=f"project_{exc.code}")
            return None
        return project_id

    async def job_trace(self, target: Target | None) -> None:
        check = "gitlab.job_trace_stream"
        if target is None:
            self.receipt.add(check, "inconclusive", reason="no_job_named")
            return
        project_id = await self._project_id(check, target)
        if project_id is None:
            return
        backend = self.client.backend
        chunks = 0

        def count(chunk: bytes) -> None:
            nonlocal chunks
            if chunk:
                chunks += 1

        started = self.clock()
        try:
            read = await self.client.get_tail(
                _PRINCIPAL,
                f"/projects/{project_id}/jobs/{target.number}/trace",
                keep_bytes=JOB_TAIL_KEEP_BYTES,
                max_read_bytes=backend.max_job_log_read_bytes,
                deadline_s=backend.max_job_log_read_s,
                observe=count,
            )
        except GatewayError as exc:
            if exc.code == "not_found":
                self.receipt.add(check, "inconclusive", reason="job_not_found")
            else:
                self.receipt.add(check, "fail", reason=exc.code)
            return
        facts: dict[str, Any] = {
            "read_bytes": read.total,
            "chunks": chunks,
            "complete": read.complete,
            "elapsed_ms": int((self.clock() - started) * 1000),
            "max_read_bytes": backend.max_job_log_read_bytes,
        }
        if read.complete:
            self.receipt.add(check, "pass", **facts)
        else:
            self.receipt.add(check, "fail", reason="stopped_at_ceiling", **facts)

    async def _notes(self, project_id: int, iid: int, *, filtered: bool) -> list[Any]:
        params: dict[str, str | int] = {
            "sort": "asc",
            "order_by": "created_at",
            "per_page": NOTES_PAGE_ROWS,
            "page": 1,
        }
        if filtered:
            params["activity_filter"] = "only_comments"
        rows, _, _ = await self.client.get_json_page(
            _PRINCIPAL, f"/projects/{project_id}/issues/{iid}/notes", params
        )
        if not isinstance(rows, list):
            raise GatewayError("upstream_error")
        return rows

    async def notes_filter(self, target: Target | None) -> None:
        check = "gitlab.issue_notes_filter"
        if target is None:
            self.receipt.add(check, "inconclusive", reason="no_issue_named")
            return
        project_id = await self._project_id(check, target)
        if project_id is None:
            return
        try:
            plain = await self._notes(project_id, target.number, filtered=False)
        except GatewayError as exc:
            reason = "issue_not_found" if exc.code == "not_found" else exc.code
            self.receipt.add(check, "inconclusive", reason=reason)
            return
        try:
            filtered = await self._notes(project_id, target.number, filtered=True)
        except GatewayError as exc:
            # The control request worked, so the parameter itself was refused.
            self.receipt.add(check, "fail", reason="filter_refused", upstream=exc.code)
            return

        def system(rows: list[Any]) -> int:
            return sum(1 for row in rows if isinstance(row, dict) and row.get("system") is True)

        facts = {
            "rows_unfiltered": len(plain),
            "rows_filtered": len(filtered),
            "system_notes_unfiltered": system(plain),
            "system_notes_filtered": system(filtered),
        }
        if facts["system_notes_filtered"]:
            self.receipt.add(check, "inconclusive", reason="filter_ignored", honoured=False, **facts)
        elif not facts["system_notes_unfiltered"]:
            self.receipt.add(check, "inconclusive", reason="no_system_notes", **facts)
        else:
            self.receipt.add(check, "pass", honoured=True, **facts)


async def run_gitlab_probe(
    gateway: Gateway,
    *,
    job: Target | None = None,
    issue: Target | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Receipt:
    """The four checks in `CHECKS` order on the gateway's own client (closed
    after)."""
    probe = _Probe(gateway, clock)
    try:
        await probe.version()
        await probe.token_scopes()
        await probe.job_trace(job)
        await probe.notes_filter(issue)
    finally:
        await gateway.client.aclose()
    return probe.receipt
