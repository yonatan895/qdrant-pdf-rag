"""Read-only Splunk site probe for the Splunk gateway (#1077, ADR-0015).

`python -m mainframe_rag.gateways.splunk --site-probe [--receipt FILE]` runs in the
gateway pod with the gateway's own configuration and client (`build_gateway`,
the POST-only `SplunkMcpClient`, `tool_check`, `compose_spl`) and answers the
items the Splunk documentation leaves open. It prints two things:

- a text-free receipt (`mainframe_rag.probe_receipt`), one JSON line per check:
  - `splunk.initialize`: the negotiated protocol version, whether a session id
    was issued and whether initialize was answered as JSON or an event stream;
  - `splunk.startup_check`: the gateway's start-up verdict (the run-query tool
    and its arguments);
  - `splunk.result_shape`: whether `| makeresults count=1` returns rows where
    `result_source` and `result_path` look;
  - `splunk.comment`: the same search with the TD5 comment, against that control;
  - `splunk.search_prefix`: one count over the first allowed index with and
    without a leading `search` keyword, so the site learns whether the tool adds
    its own;
  - `splunk.composed_search`: a search composed exactly as a tool call composes
    one (index clause, time bounds, `| head 1`, comment as configured);
- one line `{"probe": "splunk", "site_private_detail": {...}}` after the receipt: the
  server's name and version, the listed tool names, the run-query tool's
  input (and output) schema, and the key and JSON-type shape of the
  makeresults result. It holds no result value, row or token, but the schema
  is the server's own text and may carry site names in enums or defaults:
  keep it at the site.

The receipt never holds a value, index name, query or token. An unexpected
exception ends the run with the receipt still whole (the unfinished checks
`inconclusive probe_error`, exit 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

from mainframe_rag.gateways.common import GatewayError, ToolContext, clean_text, sub_hash
from mainframe_rag.gateways.splunk import (
    Gateway,
    Template,
    UnsupportedProtocol,
    attribution_comment,
    compose_spl,
    find_rows,
    shape,
    tool_check,
)
from mainframe_rag.mcp.tool_auth import Principal
from mainframe_rag.probe_receipt import Receipt

PROBE = "splunk"
CHECKS: tuple[str, ...] = (
    "splunk.initialize",
    "splunk.startup_check",
    "splunk.result_shape",
    "splunk.comment",
    "splunk.search_prefix",
    "splunk.composed_search",
)
CONTROL_QUERY = "| makeresults count=1"
_PRINCIPAL = Principal(sub="site-probe", groups=frozenset())


def _clean(value: object) -> object:
    """A schema with every string cleaned (control and format characters
    dropped, secret-shaped values redacted)."""
    if isinstance(value, str):
        return clean_text(value)[:2048]
    if isinstance(value, dict):
        return {clean_text(str(k))[:128]: _clean(v) for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [_clean(v) for v in value[:200]]
    return value


def _result_shape(result: dict[str, Any]) -> dict[str, object]:
    content = result.get("content")
    items: list[object] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            items.append("unknown")
            continue
        entry: dict[str, object] = {"type": clean_text(str(item.get("type")))[:32]}
        text = item.get("text")
        if isinstance(text, str):
            try:
                entry["json"] = shape(json.loads(text))
            except ValueError, RecursionError:
                entry["json"] = None
                entry["chars"] = len(text)
        items.append(entry)
    out: dict[str, object] = {"isError": shape(result.get("isError")), "content": items}
    if "structuredContent" in result:
        out["structuredContent"] = shape(result["structuredContent"])
    return out


def _count(rows: list[Any] | None) -> int | None:
    if not rows or not isinstance(rows[0], dict):
        return None
    raw = rows[0].get("count")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    return None


class _Probe:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.client = gateway.client
        self.backend = gateway.backend
        self.receipt = Receipt(PROBE)
        self.detail: dict[str, object] = {}
        self.ctx = ToolContext(
            gateway=PROBE,
            principal=_PRINCIPAL,
            request_id=uuid.uuid4().hex,
            sub_hash=sub_hash(_PRINCIPAL.sub),
            limits=gateway.limits,
        )

    def _deadline(self) -> float:
        return asyncio.get_running_loop().time() + self.backend.timeout_s

    async def _query(self, query: str) -> tuple[dict[str, Any] | None, str | None]:
        """The run-query result, or the fixed code it failed with."""
        try:
            return await self.client.run_query(query, self._deadline()), None
        except GatewayError as exc:
            return None, exc.code

    def _skip(self, checks: tuple[str, ...], reason: str) -> None:
        for check in checks:
            if self.receipt.status_of(check) is None:
                self.receipt.add(check, "inconclusive", reason=reason)

    async def initialize(self) -> bool:
        check = "splunk.initialize"
        try:
            session = await self.client.session(self._deadline())
        except UnsupportedProtocol:
            self.receipt.add(check, "fail", reason="protocol_unsupported")
            return False
        except GatewayError as exc:
            self.receipt.add(check, "fail", reason=exc.code)
            return False
        self.detail["server_info"] = {"name": session.server_name, "version": session.server_version}
        self.receipt.add(
            check,
            "pass",
            protocol=session.protocol,
            session_issued=session.session_id is not None,
            answer_kind=session.answer_kind,
        )
        return True

    async def startup_check(self) -> bool:
        check = "splunk.startup_check"
        try:
            tools = await self.client.list_tools(self._deadline())
        except GatewayError as exc:
            self.receipt.add(check, "fail", reason=exc.code)
            return False
        self.detail["tools"] = [clean_text(str(t.get("name")))[:128] for t in tools]
        tool = next((t for t in tools if t.get("name") == self.backend.tool_name), None)
        if tool is not None:
            self.detail["tool_input_schema"] = _clean(tool.get("inputSchema"))
            if "outputSchema" in tool:
                self.detail["tool_output_schema"] = _clean(tool.get("outputSchema"))
        schema = tool.get("inputSchema") if tool is not None else None
        props = schema.get("properties") if isinstance(schema, dict) else None
        facts: dict[str, object] = {
            "tool_count": len(tools),
            "tool_present": tool is not None,
            "property_count": len(props) if isinstance(props, dict) else 0,
        }
        reason = tool_check(self.backend, tools)
        if reason is None:
            self.receipt.add(check, "pass", **facts)
            return True
        self.receipt.add(check, "fail", reason=reason, **facts)
        return False

    def _rows_facts(self, result: dict[str, Any]) -> dict[str, object]:
        rows = find_rows(self.backend, result)
        text = result.get("content")
        return {
            "is_error": result.get("isError") not in (None, False),
            "structured": "structuredContent" in result,
            "content_items": len(text) if isinstance(text, list) else 0,
            "rows_found": rows is not None,
            "row_count": len(rows) if rows is not None else 0,
        }

    def _usable(self, result: dict[str, Any]) -> bool:
        if result.get("isError") not in (None, False):
            return False
        if self.backend.result_source == "text":
            content = result.get("content")
            return isinstance(content, list) and any(
                isinstance(i, dict) and i.get("type") == "text" for i in content
            )
        return find_rows(self.backend, result) is not None

    async def result_shape(self) -> bool:
        check = "splunk.result_shape"
        result, code = await self._query(CONTROL_QUERY)
        if result is None:
            self.receipt.add(check, "fail", reason=code)
            return False
        self.detail["makeresults_shape"] = _result_shape(result)
        facts = self._rows_facts(result)
        if facts["is_error"]:
            self.receipt.add(check, "fail", reason="search_refused", **facts)
            return False
        if not self._usable(result):
            self.receipt.add(check, "fail", reason="rows_not_found", **facts)
            return True  # the control ran; the comment check can still compare
        self.receipt.add(check, "pass", **facts)
        return True

    async def comment(self, control_ok: bool) -> None:
        check = "splunk.comment"
        enabled = self.backend.attribution_comment
        if not control_ok:
            self.receipt.add(check, "inconclusive", reason="control_failed", enabled=enabled)
            return
        query = f"{CONTROL_QUERY} {attribution_comment(self.ctx.sub_hash, self.ctx.request_id)}"
        result, code = await self._query(query)
        refused = result is None or result.get("isError") not in (None, False)
        facts = {"enabled": enabled, "accepted": not refused}
        if not refused:
            self.receipt.add(check, "pass", **facts)
        elif enabled:
            self.receipt.add(check, "fail", reason=code or "comment_refused", **facts)
        else:
            self.receipt.add(check, "inconclusive", reason=code or "comment_refused", **facts)

    async def _count(self, query: str) -> tuple[bool, int | None]:
        result, _ = await self._query(query)
        if result is None or result.get("isError") not in (None, False):
            return False, None
        return True, _count(find_rows(self.backend, result))

    async def search_prefix(self) -> None:
        check = "splunk.search_prefix"
        index = self.backend.allowed_indexes[0]
        body = f'index="{index}" earliest=-24h latest=now | head 1 | stats count'
        with_ok, with_count = await self._count(f"search {body}")
        without_ok, without_count = await self._count(body)
        configured = self.backend.search_prefix
        facts: dict[str, object] = {
            "configured": configured,
            "with_prefix_ok": with_ok,
            "without_prefix_ok": without_ok,
            "count_with": with_count,
            "count_without": without_count,
        }
        works: bool | None
        if not with_ok and not without_ok:
            self.receipt.add(check, "fail", reason="both_refused", **facts)
            return
        if with_ok and not without_ok:
            works = configured
        elif without_ok and not with_ok:
            works = not configured
        elif with_count is None or without_count is None:
            self.receipt.add(check, "inconclusive", reason="count_unreadable", **facts)
            return
        elif with_count == without_count:
            if with_count == 0:
                self.receipt.add(check, "inconclusive", reason="no_events", **facts)
                return
            works = True
        else:
            # One form found the event and the other did not: a doubled
            # keyword searches for the word "search".
            works = configured == (with_count > without_count)
        if works:
            self.receipt.add(check, "pass", **facts)
        else:
            self.receipt.add(check, "fail", reason="prefix_mismatch", **facts)

    async def composed_search(self) -> None:
        check = "splunk.composed_search"
        probe = Template(
            name="site_probe",
            description="site probe",
            search="",
            indexes=(self.backend.allowed_indexes[0],),
            max_window_s=3600,
            default_earliest="-15m",
            default_rows=1,
        )
        spl, _ = compose_spl(self.backend, probe, {"rows": 1}, self.ctx)
        result, code = await self._query(spl)
        if result is None:
            self.receipt.add(check, "fail", reason=code)
            return
        facts = self._rows_facts(result)
        if facts["is_error"]:
            self.receipt.add(check, "fail", reason="search_refused", **facts)
        elif not self._usable(result):
            self.receipt.add(check, "fail", reason="rows_not_found", **facts)
        else:
            self.receipt.add(check, "pass", **facts)


async def run_splunk_probe(gateway: Gateway) -> tuple[Receipt, dict[str, object]]:
    """The checks in `CHECKS` order on the gateway's own client (closed after).
    Always returns the whole receipt: an unexpected exception is recorded as
    `probe_error` (class name only), never raised."""
    receipt = Receipt(PROBE)
    detail: dict[str, object] = {}
    try:
        probe = _Probe(gateway)
        receipt, detail = probe.receipt, probe.detail
        if not await probe.initialize() or not await probe.startup_check():
            probe._skip(CHECKS, "not_reached")
        else:
            control_ok = await probe.result_shape()
            await probe.comment(control_ok)
            await probe.search_prefix()
            await probe.composed_search()
    except Exception as exc:  # noqa: BLE001 - recorded by class name only
        receipt.abort(exc, CHECKS)
    finally:
        with contextlib.suppress(Exception):
            await gateway.client.aclose()
    return receipt, detail
