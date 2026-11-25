"""Read-only Splunk gateway over the site's Splunk MCP Server (#1077, ADR-0015).

An MCP server on the agent image, run as `python -m mainframe_rag.gateways.splunk`.
Its tools are the site's search templates: one tool per template, with typed
parameters. The model never writes SPL. The gateway composes each search
(index clause, time bounds, row cap and the TD5 attribution comment), then runs
it through the Splunk MCP Server's run-query tool (`splunk_run_query` by
default) with one encrypted MCP token (ADR-0010 TD7, E1). It never writes:

- `SplunkMcpClient` is a small MCP client (Streamable HTTP, protocol
  2025-06-18 or 2025-03-26) over `UpstreamClient` with POST alone allowed, no
  redirects, no proxy environment, a byte cap and the configured origin only;
  it calls one tool, the configured run-query tool, and nothing else;
- template parameters are rejected, never escaped: each type admits a small
  character set with no quote, backslash, pipe, bracket, backtick, equals
  sign, space or control character, and string values sit only inside the
  double quotes the template wrote;
- templates are checked when the configuration loads (placeholders, index
  references, time modifiers, subsearches, comments, macros outside
  `allowed_macros` and commands that write or read outside the composed index
  clause are refused);
- at start-up `check_backend` initializes a session, checks the negotiated
  protocol version and that the run-query tool takes the configured
  arguments, reads and checks the definitions of the macros templates call
  (ADR-0015 D9), and refuses to serve otherwise.

Callers authenticate in one of two modes (`SPLUNK_AUTH_MODE`): `oidc` (the
default, ADR-0010's caller verification with a single-audience token, fixed
here) or `bearer` (ADR-0015's named exception B2, approved by the maintainer
on 2026-11-25). Contract: docs/team-tools.md#splunk-gateway.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2

from mainframe_rag.gateways.common import (
    GatewayConfig,
    GatewayConfigError,
    GatewayError,
    Limits,
    Tool,
    ToolContext,
    ToolOutput,
    UpstreamClient,
    UpstreamResponse,
    clean_text,
    configure_gateway_logging,
    create_gateway_app,
    load_config,
)
from mainframe_rag.mcp.tool_auth import ASGIApp, JwksCache

log = logging.getLogger("gateway.splunk")

GATEWAY: Final = "splunk"
AUTH_MODE_ENV: Final = "SPLUNK_AUTH_MODE"
BEARER_SECRET_ENV: Final = "SPLUNK_GATEWAY_BEARER_SECRET"
TOKEN_ENV: Final = "SPLUNK_MCP_TOKEN"
AUTH_MODES: Final = ("oidc", "bearer")
CLIENT_INFO: Final = {"name": "mainframe-rag-splunk-gateway", "version": "0.1.0"}

# MCP versions this client speaks. It asks for the first; the server's answer
# must be one of these (2025-11-25 lets a server close an event stream before
# its response and expects a GET resume; 2026-07-28 drops initialize and
# sessions: neither is implemented, so neither is accepted).
CLIENT_PROTOCOL: Final = "2025-06-18"
SUPPORTED_PROTOCOLS: Final = ("2025-06-18", "2025-03-26")
MAX_TOOL_PAGES: Final = 10
# Splunk's run-query tool fails a search that returns more than 1000 events.
SPLUNK_MAX_EVENTS: Final = 1000

# Splunk's run-query tool fails a search that runs past one minute: the tool
# deadline sits just above the backend timeout (below 60 s), so the gateway
# answers with its own fixed code first. Every search runs as the one token
# user, so Splunk's per-user concurrent-search quota (`srchJobsQuota`, 3 by
# default) covers all callers together: the gateway-wide caps default to 3
# in flight and 60 a minute in both modes (ADR-0015 D10; per pod).
LIMIT_DEFAULTS: Final = {"tool_timeout_s": 60.0, "max_concurrent_total": 3, "rate_per_minute_total": 60}
# Bearer mode: every Open WebUI user is the one caller `service:openwebui`, so
# the per-caller limits are instance-wide. Three searches in flight (Splunk's
# default per-role concurrent-search quota, `srchJobsQuota` in authorize.conf;
# the token user's role at the site may differ) and 60 a minute, unless the
# configuration sets them (ADR-0015 D7, B2 approved 2026-11-25).
BEARER_LIMIT_DEFAULTS: Final = {"max_concurrent_per_user": 3, "rate_per_minute": 60}

_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_INDEX_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,79}")
_PARAM_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_TEMPLATE_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PLACEHOLDER_RE: Final = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_TIME_PATTERN: Final = r"now|-([1-9][0-9]{0,4})([smhdw])(?:@([smhdw]))?|@([smhdw])"
_TIME_RE: Final = re.compile(_TIME_PATTERN)
_UNIT_S: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_TOOL_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ARGUMENT_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_MCP_PATH_RE: Final = re.compile(r"(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}){1,8}")
_SESSION_ID_RE: Final = re.compile(r"[\x21-\x7e]{1,1024}")
_TOKEN_RE: Final = re.compile(r"[\x21-\x7e]{1,16384}")
_HEX16_RE: Final = re.compile(r"[0-9a-f]{16}")
_HEX32_RE: Final = re.compile(r"[0-9a-f]{32}")
_TEMPLATE_CHARS_RE: Final = re.compile(r"[\x20-\x7e]*")
BUILTIN_PARAMS: Final = ("earliest", "latest", "rows")
PARAM_TYPES: Final = ("identifier", "enum", "integer", "index")
RESULT_SOURCES: Final = ("auto", "structured", "text_json", "text")

# Template lint (ADR-0015 D3). The gateway alone writes the index clause and
# the time bounds, so a template may not name them (any case or spacing, with
# =, !=, <, >, :: or IN; quotes are ignored for this check, so a quoted name
# is caught too). Brackets (subsearches, which search outside the composed
# index clause), backticks other than allowlisted macro calls (D9) and these
# commands are refused. This is a lint over operator-authored text; the token user's role
# is the boundary.
_TIME_OR_INDEX_RE: Final = re.compile(
    r"(?i)(?<![A-Za-z0-9_.])(?:index|earliest|latest|_index_earliest|_index_latest"
    r"|starttime|endtime|starttimeu|endtimeu|timeformat|searchtimespan[a-z]*"
    r"|(?:start|end)?(?:days|hours|minutes|months)ago|savedsearch|eventtype)"
    r"\s*(?:!=|=|<|>|::|in\b)"
)
_COMMAND_RE: Final = re.compile(r"\|\s*([A-Za-z_][A-Za-z0-9_]*)")
REFUSED_COMMANDS: Final = frozenset(
    {
        # writes, side effects, execution
        "collect", "mcollect", "meventcollect", "tscollect", "outputlookup", "outputcsv",
        "outputtext", "delete", "sendemail", "sendalert", "script", "run", "runshellscript",
        "dump", "map", "localop",
        # reads outside the composed index clause
        "rest", "inputlookup", "inputcsv", "inputintelligence", "loadjob", "savedsearch",
        "tstats", "mstats", "mcatalog", "metadata", "metasearch", "dbinspect", "datamodel",
        "from", "pivot", "eventcount", "mpreview", "msearch", "multisearch", "union", "append",
        "appendcols", "join", "set", "makeresults", "gentimes", "typeahead", "walklex",
        # aliases and add-on commands with the same effect
        "summaryindex", "dbxquery", "dbxoutput", "ldapsearch", "fit", "outputtelemetry",
    }
)  # fmt: skip


class BackendCheckError(Exception):
    """The start-up check refused the backend. `reason` is a fixed code;
    `macro` names the allowlisted macro a macro check refused (never its
    definition)."""

    def __init__(self, reason: str, macro: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.macro = macro


class UnsupportedProtocol(GatewayError):
    """The server answered initialize with a version outside SUPPORTED_PROTOCOLS."""

    def __init__(self) -> None:
        super().__init__("upstream_protocol")


# --- time ranges ------------------------------------------------------------------------


def time_bounds(spec: str) -> tuple[int, int]:
    """How many seconds before now a relative time can lie, as (least, most):
    `now` is (0, 0); `-15m` (900, 900); a snap adds up to one snap unit,
    so `-1d@d` is (86400, 172800) and `@d` (0, 86400). ValueError when
    `spec` is outside the grammar."""
    match = _TIME_RE.fullmatch(spec)
    if match is None:
        raise ValueError("not a relative time")
    count, unit, snap, bare_snap = match.groups()
    if bare_snap is not None:
        return 0, _UNIT_S[bare_snap]
    if count is None:
        return 0, 0  # now
    base = int(count) * _UNIT_S[unit]
    return base, base + (_UNIT_S[snap] if snap else 0)


def window_fits(earliest: str, latest: str, max_window_s: int) -> bool:
    """Whether every snap of `earliest` lies before `latest` and the widest
    window they can span is at most `max_window_s`."""
    try:
        e_least, e_most = time_bounds(earliest)
        l_least, l_most = time_bounds(latest)
    except ValueError:
        return False
    return e_least >= l_most and e_most > l_least and e_most - l_least <= max_window_s


# --- templates ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """One typed template parameter. `values` lists an enum's or an index
    parameter's choices; `default` makes it optional."""

    name: str
    kind: str
    description: str = ""
    values: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    default: str | int | None = None

    def spl_value(self, value: object) -> str:
        """The value as it enters SPL, or `invalid_arguments`: a value outside
        the type is refused, never escaped."""
        if self.kind == "integer":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or self.minimum is None
                or self.maximum is None
                or not self.minimum <= value <= self.maximum
            ):
                raise GatewayError("invalid_arguments")
            return str(value)
        if not isinstance(value, str):
            raise GatewayError("invalid_arguments")
        if self.kind == "identifier":
            ok = _IDENTIFIER_RE.fullmatch(value) is not None
        else:  # enum, index
            ok = value in self.values
        if not ok:
            raise GatewayError("invalid_arguments")
        return value

    def schema(self) -> dict[str, Any]:
        prop: dict[str, Any]
        if self.kind == "integer":
            prop = {"type": "integer", "minimum": self.minimum, "maximum": self.maximum}
        elif self.kind == "identifier":
            # Anchored: JSON Schema's pattern is a search; the gateway full-matches.
            prop = {"type": "string", "pattern": f"^{_IDENTIFIER_RE.pattern}$", "maxLength": 64}
        else:
            prop = {"type": "string", "enum": list(self.values)}
        description = self.description
        if self.default is not None:
            description = f"{description} Default: {self.default}.".strip()
        if description:
            prop["description"] = description
        return prop


@dataclass(frozen=True)
class Template:
    """One site search template (operator-authored, trusted). `search` is the
    SPL after the index clause and time bounds the gateway writes."""

    name: str
    description: str
    search: str
    params: tuple[Param, ...] = ()
    indexes: tuple[str, ...] = ()
    index_param: str | None = None
    max_window_s: int = 86400
    default_earliest: str = "-1h"
    default_rows: int | None = None
    macros: tuple[MacroCall, ...] = ()
    index_from_macro: bool = False

    def param(self, name: str) -> Param:
        for param in self.params:
            if param.name == name:
                return param
        raise KeyError(name)


def _quoted_mask(text: str, where: str) -> list[bool]:
    """For each character, whether it lies inside a double-quoted SPL string
    (the quotes included). Backslash escapes the next character in a string."""
    mask: list[bool] = []
    inside = escaped = False
    for char in text:
        if inside:
            mask.append(True)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                inside = False
        else:
            inside = char == '"'
            mask.append(inside)
    if inside:
        raise GatewayConfigError(f"{where} has an unbalanced double quote")
    return mask


def split_terms(text: str) -> tuple[str, str]:
    """A template's search terms (before its first pipe outside a string)
    and the rest, which starts with that pipe or is empty."""
    mask = _quoted_mask(text, "a template search")
    for at, (char, quoted) in enumerate(zip(text, mask, strict=True)):
        if char == "|" and not quoted:
            return text[:at], text[at:]
    return text, ""


_MACRO_CALL_RE: Final = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.:-]{0,79})(?:\(([^()`]*)\))?")
_MACRO_NAME_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,79}")
# A macro argument in a template: a parameter placeholder, a literal token,
# or a double-quoted literal with no quote, backslash, comma, pipe, backtick
# or brace (a placeholder is passed bare, never inside a quoted argument).
_MACRO_ARG_RE: Final = re.compile(r'\{[a-z][a-z0-9_]{0,31}\}|[A-Za-z0-9_.:*-]{1,64}|"[^"\\,|`{}]{0,128}"')


@dataclass(frozen=True)
class MacroCall:
    """One allowlisted macro call in a template: its name, its raw argument
    texts (placeholders kept) and whether it sits in the search terms (before
    the first pipe outside a string)."""

    name: str
    args: tuple[str, ...] = ()
    in_terms: bool = True

    @property
    def stanza(self) -> str:
        """The macro's definition name: Splunk names a macro with arguments
        `name(<count>)`."""
        return self.name if not self.args else f"{self.name}({len(self.args)})"


def backtick_spans(text: str, mask: Sequence[bool], where: str) -> list[tuple[int, int]]:
    """The (start, end) of each backtick span outside strings. A backtick
    inside a string or one left open is refused."""
    spans: list[tuple[int, int]] = []
    at = 0
    while (start := text.find("`", at)) >= 0:
        if mask[start]:
            raise GatewayConfigError(f"{where} may not use a backtick inside a string")
        end = text.find("`", start + 1)
        if end < 0:
            raise GatewayConfigError(f"{where} has an unbalanced backtick")
        spans.append((start, end + 1))
        at = end + 1
    return spans


def parse_macro_call(content: str, allowed_macros: Sequence[str], where: str) -> tuple[str, tuple[str, ...]]:
    """The name and raw arguments of one backtick span's content; refused
    unless it is a call of an allowlisted macro (a comment shape is not)."""
    match = _MACRO_CALL_RE.fullmatch(content)
    if match is None:
        raise GatewayConfigError(f"{where} may use backticks only for allowlisted macro calls")
    name, raw = match.group(1), match.group(2)
    if name not in allowed_macros:
        raise GatewayConfigError(f"{where} uses macro {name} outside allowed_macros")
    if raw is None:
        return name, ()
    args = tuple(raw.split(","))
    if not all(_MACRO_ARG_RE.fullmatch(arg) for arg in args):
        raise GatewayConfigError(f"{where} passes macro {name} an argument outside the allowed forms")
    return name, args


def _first_unquoted_pipe(text: str, mask: Sequence[bool]) -> int:
    for at, (char, quoted) in enumerate(zip(text, mask, strict=True)):
        if char == "|" and not quoted:
            return at
    return len(text)


def lint_search(
    text: str, params: Mapping[str, Param], where: str, allowed_macros: Sequence[str] = ()
) -> tuple[set[str], tuple[MacroCall, ...]]:
    """Check one template's SPL (ADR-0015 D3, D9). Returns the parameter
    names it uses and its macro calls.
    Messages name the template's position, parameter and macro names, never
    the SPL."""
    if not isinstance(text, str) or len(text) > 4096 or not _TEMPLATE_CHARS_RE.fullmatch(text):
        raise GatewayConfigError(f"{where} must be printable ASCII of at most 4096 characters")
    mask = _quoted_mask(text, where)
    pipe = _first_unquoted_pipe(text, mask)
    used: set[str] = set()
    calls: list[MacroCall] = []
    scrubbed = list(text)
    for start, end in backtick_spans(text, mask, where):
        name, args = parse_macro_call(text[start + 1 : end - 1], allowed_macros, where)
        for arg in args:
            if arg.startswith("{"):
                param = params.get(arg[1:-1])
                if param is None:
                    raise GatewayConfigError(f"{where} names an undeclared parameter")
                if param.kind == "index":
                    raise GatewayConfigError(f"{where} uses index parameter {param.name} outside index_param")
                used.add(param.name)
        calls.append(MacroCall(name, args, in_terms=start < pipe))
        scrubbed[start:end] = " " * (end - start)
    rest = "".join(scrubbed)
    unquoted = "".join(" " if quoted else char for char, quoted in zip(rest, mask, strict=True))
    if "[" in unquoted or "]" in unquoted:
        raise GatewayConfigError(f"{where} may not use a subsearch")
    depth = 0
    for char in unquoted:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            break
    if depth != 0:
        raise GatewayConfigError(f"{where} has unbalanced parentheses")
    if _TIME_OR_INDEX_RE.search(rest.replace('"', " ")):
        raise GatewayConfigError(f"{where} may not name an index or a time bound")
    for command in _COMMAND_RE.findall(unquoted):
        if command.lower() in REFUSED_COMMANDS:
            raise GatewayConfigError(f"{where} uses a refused command")
    for match in _PLACEHOLDER_RE.finditer(rest):
        name = match.group(1)
        param = params.get(name)
        if param is None:
            raise GatewayConfigError(f"{where} names an undeclared parameter")
        if param.kind == "index":
            raise GatewayConfigError(f"{where} uses index parameter {name} outside index_param")
        if param.kind != "integer" and not mask[match.start()]:
            raise GatewayConfigError(f"{where} must put parameter {name} inside double quotes")
        used.add(name)
    return used, tuple(calls)


def _param_from_doc(name: str, doc: object, allowed_indexes: Sequence[str], where: str) -> Param:
    if not _PARAM_NAME_RE.fullmatch(name) or name in BUILTIN_PARAMS:
        raise GatewayConfigError(f"{where} has an invalid parameter name")
    at = f"{where}.{name}"
    if not isinstance(doc, dict) or doc.get("type") not in PARAM_TYPES:
        raise GatewayConfigError(f"{at}.type must be one of {', '.join(PARAM_TYPES)}")
    kind = doc["type"]
    allowed = {"type", "description", "default"} | {
        "identifier": set(),
        "enum": {"values"},
        "integer": {"minimum", "maximum"},
        "index": {"values"},
    }[kind]
    unknown = sorted(set(doc) - allowed)
    if unknown:
        raise GatewayConfigError(f"{at} has unknown keys: {', '.join(unknown)}")
    description = doc.get("description", "")
    if not isinstance(description, str) or len(description) > 512:
        raise GatewayConfigError(f"{at}.description must be a string of at most 512 characters")
    values: tuple[str, ...] = ()
    minimum = maximum = None
    if kind == "enum":
        raw = doc.get("values")
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(v, str) and _IDENTIFIER_RE.fullmatch(v) for v in raw)
            or len(set(raw)) != len(raw)
        ):
            raise GatewayConfigError(f"{at}.values must be distinct identifiers")
        values = tuple(raw)
    elif kind == "index":
        raw = doc.get("values", list(allowed_indexes))
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(v, str) and v in allowed_indexes for v in raw)
            or len(set(raw)) != len(raw)
        ):
            raise GatewayConfigError(f"{at}.values must be distinct entries of allowed_indexes")
        values = tuple(raw)
    elif kind == "integer":
        minimum, maximum = doc.get("minimum"), doc.get("maximum")
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not -(10**9) <= minimum <= maximum <= 10**9
        ):
            raise GatewayConfigError(f"{at} needs integer minimum <= maximum within 1e9")
    param = Param(name, kind, description, values, minimum, maximum)
    if "default" in doc:
        try:
            param.spl_value(doc["default"])
        except GatewayError:
            raise GatewayConfigError(f"{at}.default is not a valid value") from None
        param = dataclasses.replace(param, default=doc["default"])
    return param


_TEMPLATE_KEYS: Final = frozenset(
    {
        "name",
        "description",
        "search",
        "params",
        "indexes",
        "index_param",
        "max_window_s",
        "default_earliest",
        "default_rows",
        "index_from_macro",
    }
)


def template_from_doc(
    doc: object,
    position: int,
    *,
    allowed_indexes: Sequence[str],
    max_window_s: int,
    max_rows: int,
    allowed_macros: Sequence[str] = (),
) -> Template:
    where = f"backend.templates[{position}]"
    if not isinstance(doc, dict):
        raise GatewayConfigError(f"{where} must be an object")
    unknown = sorted(set(doc) - _TEMPLATE_KEYS)
    if unknown:
        raise GatewayConfigError(f"{where} has unknown keys: {', '.join(unknown)}")
    name, description = doc.get("name"), doc.get("description")
    if not isinstance(name, str) or not _TEMPLATE_NAME_RE.fullmatch(name):
        raise GatewayConfigError(f"{where}.name must be a lower-case identifier")
    if not isinstance(description, str) or not 0 < len(description) <= 1024:
        raise GatewayConfigError(f"{where}.description must be 1 to 1024 characters")
    raw_params = doc.get("params", {})
    if not isinstance(raw_params, dict):
        raise GatewayConfigError(f"{where}.params must be an object")
    params = {
        n: _param_from_doc(n, spec, allowed_indexes, f"{where}.params")
        for n, spec in raw_params.items()
    }
    used, calls = lint_search(
        doc.get("search"), params, f"{where}.search", allowed_macros  # type: ignore[arg-type]
    )
    indexes, index_param = doc.get("indexes"), doc.get("index_param")
    from_macro = doc.get("index_from_macro", False)
    if not isinstance(from_macro, bool):
        raise GatewayConfigError(f"{where}.index_from_macro must be true or false")
    if (indexes is not None) + (index_param is not None) + from_macro != 1:
        raise GatewayConfigError(f"{where} needs exactly one of indexes, index_param or index_from_macro")
    if from_macro:
        if not any(call.in_terms for call in calls):
            raise GatewayConfigError(f"{where}.index_from_macro needs a macro call in the search terms")
    elif indexes is not None:
        if (
            not isinstance(indexes, list)
            or not indexes
            or not all(isinstance(i, str) for i in indexes)
            or len(set(indexes)) != len(indexes)
        ):
            raise GatewayConfigError(f"{where}.indexes must be a non-empty list of distinct names")
        if not set(indexes) <= set(allowed_indexes):
            raise GatewayConfigError(f"{where}.indexes names an index outside allowed_indexes")
    else:
        param = params.get(index_param) if isinstance(index_param, str) else None
        if param is None or param.kind != "index":
            raise GatewayConfigError(f"{where}.index_param must name a parameter of type index")
        used.add(index_param)  # type: ignore[arg-type]
    unused = sorted(set(params) - used)
    if unused:
        raise GatewayConfigError(f"{where} declares unused parameters: {', '.join(unused)}")
    window = doc.get("max_window_s", max_window_s)
    if isinstance(window, bool) or not isinstance(window, int) or not 0 < window <= max_window_s:
        raise GatewayConfigError(f"{where}.max_window_s must be in (0, backend.max_window_s]")
    earliest = doc.get("default_earliest", "-1h")
    if not isinstance(earliest, str) or not window_fits(earliest, "now", window):
        raise GatewayConfigError(f"{where}.default_earliest must be a relative time within the window")
    rows = doc.get("default_rows")
    if rows is not None and (
        isinstance(rows, bool) or not isinstance(rows, int) or not 0 < rows <= max_rows
    ):
        raise GatewayConfigError(f"{where}.default_rows must be in [1, backend.max_rows]")
    return Template(
        name=name,
        description=description,
        search=doc["search"],
        params=tuple(params.values()),
        indexes=tuple(indexes) if indexes is not None else (),
        index_param=index_param,  # type: ignore[arg-type]
        max_window_s=window,
        default_earliest=earliest,
        default_rows=rows,
        macros=calls,
        index_from_macro=from_macro,
    )


# --- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroSource:
    """Where the start-up macro check reads macro definitions (ADR-0015 D9):
    a knowledge-object tool called once with `type_argument` = `type_value`
    and the constant `extra_arguments`; its rows are found like run-query
    rows, and each row's name, definition, argument list and eval flag at
    the given key paths. Argument names and the row shape are unconfirmed,
    so every part is configurable (the site probe shows the tool's schema)."""

    tool_name: str = "splunk_get_knowledge_objects"
    type_argument: str = "type"
    type_value: str = "macros"
    extra_arguments: Mapping[str, str | int | bool] = field(default_factory=dict)
    result_source: str = "auto"
    result_path: tuple[str, ...] = ("results",)
    name_path: tuple[str, ...] = ("name",)
    definition_path: tuple[str, ...] = ("definition",)
    args_path: tuple[str, ...] = ("args",)
    iseval_path: tuple[str, ...] = ("iseval",)


_MACRO_SOURCE_KEYS: Final = frozenset(f.name for f in dataclasses.fields(MacroSource))


def _key_path(value: object, where: str, *, empty: bool) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 4
        or (not value and not empty)
        or not all(isinstance(k, str) and 0 < len(k) <= 64 for k in value)
    ):
        raise GatewayConfigError(f"{where} must be a list of at most 4 keys")
    return tuple(value)


def _constants(value: object, where: str, reserved: str) -> dict[str, str | int | bool]:
    if not isinstance(value, dict) or len(value) > 16:
        raise GatewayConfigError(f"{where} must be an object of at most 16 keys")
    for key, item in value.items():
        if not _ARGUMENT_RE.fullmatch(key) or key == reserved:
            raise GatewayConfigError(f"{where} has an invalid argument name")
        if not (
            isinstance(item, (bool, int))
            or (isinstance(item, str) and len(item) <= 256 and _TEMPLATE_CHARS_RE.fullmatch(item))
        ):
            raise GatewayConfigError(f"{where}.{key} must be a constant")
    return dict(value)


def macro_source_from_doc(doc: object) -> MacroSource:
    where = "backend.macro_source"
    if not isinstance(doc, dict):
        raise GatewayConfigError(f"{where} must be an object")
    unknown = sorted(set(doc) - _MACRO_SOURCE_KEYS)
    if unknown:
        raise GatewayConfigError(f"{where} has unknown keys: {', '.join(unknown)}")
    out = dict(doc)
    tool_name = out.get("tool_name", "splunk_get_knowledge_objects")
    if not isinstance(tool_name, str) or not _TOOL_NAME_RE.fullmatch(tool_name):
        raise GatewayConfigError(f"{where}.tool_name must be a tool name")
    type_argument = out.get("type_argument", "type")
    if not isinstance(type_argument, str) or not _ARGUMENT_RE.fullmatch(type_argument):
        raise GatewayConfigError(f"{where}.type_argument must be an argument name")
    type_value = out.get("type_value", "macros")
    if not isinstance(type_value, str) or not _IDENTIFIER_RE.fullmatch(type_value):
        raise GatewayConfigError(f"{where}.type_value must be an identifier")
    if out.get("result_source", "auto") not in RESULT_SOURCES[:3]:
        raise GatewayConfigError(f"{where}.result_source must be one of auto, structured, text_json")
    out["extra_arguments"] = _constants(out.get("extra_arguments", {}), f"{where}.extra_arguments", type_argument)
    for key in ("result_path", "name_path", "definition_path", "args_path", "iseval_path"):
        if key in out:
            out[key] = _key_path(out[key], f"{where}.{key}", empty=key == "result_path")
    return MacroSource(**out)


@dataclass(frozen=True)
class SplunkBackend:
    """The `backend` section of the gateway configuration file."""

    base_url: str
    allowed_indexes: tuple[str, ...]
    templates: tuple[Template, ...]
    ca_file: str | None = None
    timeout_s: float = 50.0
    max_response_bytes: int = 4 * 1024 * 1024
    mcp_path: str = "/services/mcp"
    tool_name: str = "splunk_run_query"
    query_argument: str = "query"
    extra_arguments: Mapping[str, str | int | bool] = field(default_factory=dict)
    result_source: str = "auto"
    result_path: tuple[str, ...] = ("results",)
    search_prefix: bool = True
    attribution_comment: bool = True
    max_rows: int = 100
    max_window_s: int = 7 * 86400
    allowed_macros: tuple[str, ...] = ()
    allow_unverified_macros: bool = False
    macro_source: MacroSource = field(default_factory=lambda: MacroSource())


_BACKEND_KEYS: Final = frozenset(f.name for f in dataclasses.fields(SplunkBackend))


def _check_scalars(doc: Mapping[str, Any]) -> None:
    base_url = doc["base_url"]
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise GatewayConfigError("backend.base_url must be an https URL")
    if any(c in base_url for c in "?#@") or any(c.isspace() for c in base_url):
        raise GatewayConfigError("backend.base_url must be a plain origin and path")
    ca_file = doc.get("ca_file")
    if ca_file is not None and not isinstance(ca_file, str):
        raise GatewayConfigError("backend.ca_file must be a path")
    timeout = doc.get("timeout_s", 50.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < 60:
        raise GatewayConfigError("backend.timeout_s must be in (0, 60)")
    for key, low, high in (
        ("max_response_bytes", 1024, 64 * 1024 * 1024),
        ("max_rows", 1, SPLUNK_MAX_EVENTS),
        ("max_window_s", 1, 366 * 86400),
    ):
        value = doc.get(key, 1 if key != "max_response_bytes" else 1024)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise GatewayConfigError(f"backend.{key} must be an integer in [{low}, {high}]")
    mcp_path = doc.get("mcp_path", "/services/mcp")
    if (
        not isinstance(mcp_path, str)
        or not _MCP_PATH_RE.fullmatch(mcp_path)
        or "/." in mcp_path
    ):
        raise GatewayConfigError("backend.mcp_path must be an absolute path of plain segments")
    tool_name = doc.get("tool_name", "splunk_run_query")
    if not isinstance(tool_name, str) or not _TOOL_NAME_RE.fullmatch(tool_name):
        raise GatewayConfigError("backend.tool_name must be a tool name")
    query_argument = doc.get("query_argument", "query")
    if not isinstance(query_argument, str) or not _ARGUMENT_RE.fullmatch(query_argument):
        raise GatewayConfigError("backend.query_argument must be an argument name")
    for key in ("search_prefix", "attribution_comment"):
        if not isinstance(doc.get(key, True), bool):
            raise GatewayConfigError(f"backend.{key} must be true or false")
    if doc.get("result_source", "auto") not in RESULT_SOURCES:
        raise GatewayConfigError(f"backend.result_source must be one of {', '.join(RESULT_SOURCES)}")


def backend_from_config(config: GatewayConfig) -> SplunkBackend:
    doc = dict(config.backend)
    unknown = sorted(set(doc) - _BACKEND_KEYS)
    if unknown:
        raise GatewayConfigError(f"backend has unknown keys: {', '.join(unknown)}")
    for key in ("base_url", "allowed_indexes", "templates"):
        if key not in doc:
            raise GatewayConfigError(f"backend.{key} is required")
    _check_scalars(doc)
    indexes = doc["allowed_indexes"]
    if (
        not isinstance(indexes, list)
        or not indexes
        or not all(isinstance(i, str) and _INDEX_RE.fullmatch(i) for i in indexes)
        or len(set(indexes)) != len(indexes)
    ):
        raise GatewayConfigError("backend.allowed_indexes must be a non-empty list of index names")
    doc["allowed_indexes"] = tuple(indexes)
    extra = doc.get("extra_arguments", {})
    if not isinstance(extra, dict) or len(extra) > 16:
        raise GatewayConfigError("backend.extra_arguments must be an object of at most 16 keys")
    for key, value in extra.items():
        if not _ARGUMENT_RE.fullmatch(key) or key == doc.get("query_argument", "query"):
            raise GatewayConfigError("backend.extra_arguments has an invalid argument name")
        if not (
            isinstance(value, (bool, int))
            or (isinstance(value, str) and len(value) <= 256 and _TEMPLATE_CHARS_RE.fullmatch(value))
        ):
            raise GatewayConfigError(f"backend.extra_arguments.{key} must be a constant")
    doc["extra_arguments"] = dict(extra)
    macros = doc.get("allowed_macros", [])
    if (
        not isinstance(macros, list)
        or len(macros) > 128
        or not all(isinstance(m, str) and _MACRO_NAME_RE.fullmatch(m) for m in macros)
        or len(set(macros)) != len(macros)
    ):
        raise GatewayConfigError("backend.allowed_macros must be a list of distinct macro names")
    doc["allowed_macros"] = tuple(macros)
    if not isinstance(doc.get("allow_unverified_macros", False), bool):
        raise GatewayConfigError("backend.allow_unverified_macros must be true or false")
    if "macro_source" in doc:
        doc["macro_source"] = macro_source_from_doc(doc["macro_source"])
    path = doc.get("result_path", ["results"])
    if (
        not isinstance(path, list)
        or len(path) > 4
        or not all(isinstance(k, str) and 0 < len(k) <= 64 for k in path)
    ):
        raise GatewayConfigError("backend.result_path must be a list of at most 4 keys")
    doc["result_path"] = tuple(path)
    templates = doc["templates"]
    if not isinstance(templates, list) or not 0 < len(templates) <= 64:
        raise GatewayConfigError("backend.templates must be a list of 1 to 64 templates")
    max_window = doc.get("max_window_s", 7 * 86400)
    max_rows = doc.get("max_rows", 100)
    built = tuple(
        template_from_doc(
            t,
            i,
            allowed_indexes=doc["allowed_indexes"],
            max_window_s=max_window,
            max_rows=max_rows,
            allowed_macros=doc["allowed_macros"],
        )
        for i, t in enumerate(templates)
    )
    if len({t.name for t in built}) != len(built):
        raise GatewayConfigError("backend.templates names must be unique")
    doc["templates"] = built
    return SplunkBackend(**doc)


def check_token(token: str) -> None:
    """The encrypted MCP token is visible ASCII with no spaces (a header value
    that cannot split or fold); the message names the variable only."""
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise GatewayConfigError(f"{TOKEN_ENV} must be a non-empty token of visible ASCII")


# --- the SPL a call runs --------------------------------------------------------------------


def compose_spl(
    backend: SplunkBackend, template: Template, args: Mapping[str, Any], ctx: ToolContext
) -> tuple[str, int]:
    """The search one call runs, and its row cap: the index clause and time
    bounds, the template's search terms in parentheses and the rest of its
    pipeline with each value in place, `| head <rows>`, and the TD5 comment. Every value is checked again here (the runtime checked the
    schema first): a value outside its type is refused, never escaped."""
    values: dict[str, str] = {}
    for param in template.params:
        raw = args.get(param.name, param.default)
        if raw is None:
            raise GatewayError("invalid_arguments")
        values[param.name] = param.spl_value(raw)
    if template.index_param is not None:
        indexes: tuple[str, ...] = (values[template.index_param],)
    else:
        indexes = template.indexes
    if not set(indexes) <= set(backend.allowed_indexes) or not (indexes or template.index_from_macro):
        raise GatewayError("invalid_arguments")
    earliest = args.get("earliest", template.default_earliest)
    latest = args.get("latest", "now")
    if (
        not isinstance(earliest, str)
        or not isinstance(latest, str)
        or not window_fits(earliest, latest, template.max_window_s)
    ):
        raise GatewayError("invalid_arguments")
    rows = args.get("rows", template.default_rows or backend.max_rows)
    if isinstance(rows, bool) or not isinstance(rows, int) or not 0 < rows <= backend.max_rows:
        raise GatewayError("invalid_arguments")
    terms, pipeline = (
        _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], part).strip()
        for part in split_terms(template.search)
    )
    # An index-defining macro (index_from_macro) supplies the index clause:
    # the start-up macro check proved its indexes are allowed (ADR-0015 D9).
    clause = " OR ".join(f'index="{index}"' for index in indexes)
    if len(indexes) > 1:
        clause = f"({clause})"
    head = f"{clause} " if clause else ""
    spl = f"{'search ' if backend.search_prefix else ''}{head}earliest={earliest} latest={latest}"
    if terms:
        # Parenthesised, so a template's OR cannot bind to the gateway's terms
        # (Splunk evaluates OR before the implicit AND).
        spl += f" ({terms})"
    if pipeline:
        spl += f" {pipeline}"
    spl += f" | head {rows}"
    if backend.attribution_comment:
        spl += " " + attribution_comment(ctx.sub_hash, ctx.request_id)
    return spl, rows


def attribution_comment(sub_hash: str, request_id: str) -> str:
    """TD5: the caller's hash and the request id as an SPL inline comment."""
    if not _HEX16_RE.fullmatch(sub_hash) or not _HEX32_RE.fullmatch(request_id):
        raise ValueError("attribution values are fixed-format hex")  # programming error
    return f"```mfr sub_hash={sub_hash} req={request_id}```"


# --- the Splunk MCP client ------------------------------------------------------------------


def content_kind(content_type: str) -> str | None:
    media = content_type.split(";", 1)[0].strip().lower()
    return {"application/json": "json", "text/event-stream": "sse"}.get(media)


def _is_response_to(item: object, msg_id: int) -> bool:
    """A JSON-RPC response to our request `msg_id`: no `method` (a server
    request has its own id space), an integer id equal to ours (not a
    boolean), and exactly one of result and error."""
    if not isinstance(item, dict) or "method" in item:
        return False
    found = item.get("id")
    return type(found) is int and found == msg_id and (("result" in item) != ("error" in item))


class ResponseReader:
    """Finds the response to one request in a JSON body or an SSE stream,
    fed chunk by chunk (`UpstreamClient.request_until`): it tells the client
    to stop reading as soon as the response arrived, so a stream the server
    keeps open does not hold the call. Server requests and notifications on
    the stream are skipped."""

    def __init__(self, msg_id: int) -> None:
        self.msg_id = msg_id
        self.message: dict[str, Any] | None = None
        self.events = 0
        self._buffer = bytearray()
        self._data: list[bytes] = []
        self._event = b""

    def feed(self, content_type: str, chunk: bytes) -> bool:
        if content_kind(content_type) != "sse":
            return False
        if self.message is None:
            self._buffer += chunk
            self._drain()
        return self.message is not None

    def consider_body(self, body: bytes) -> None:
        self._consider(body)

    def _drain(self) -> None:
        buffer = self._buffer
        while self.message is None:
            ends = [i for i in (buffer.find(b"\r"), buffer.find(b"\n")) if i >= 0]
            if not ends:
                return
            end = min(ends)
            if buffer[end] == 0x0D:
                if end + 1 == len(buffer):
                    return  # a CR at the end of a chunk may start a CRLF
                step = 2 if buffer[end + 1] == 0x0A else 1
            else:
                step = 1
            line = bytes(buffer[:end])
            del buffer[: end + step]
            self._line(line)

    def _line(self, line: bytes) -> None:
        if not line:
            data, event = self._data, self._event
            self._data, self._event = [], b""
            if data and event in (b"", b"message"):
                self.events += 1
                self._consider(b"\n".join(data))
            return
        if line.startswith(b":"):
            return  # an SSE comment
        name, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if name == b"data":
            self._data.append(value)
        elif name == b"event":
            self._event = value

    def _consider(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            doc = json.loads(raw)
        except ValueError, RecursionError:
            raise GatewayError("upstream_protocol") from None
        for item in doc if isinstance(doc, list) else [doc]:
            if _is_response_to(item, self.msg_id):
                self.message = item
                return


def _never(content_type: str, chunk: bytes) -> bool:
    del content_type, chunk
    return False


@dataclass(frozen=True)
class McpSession:
    """What one initialize negotiated. `session_id` is None when the server
    issued none; `answer_kind` is how initialize was answered (json or sse)."""

    protocol: str
    session_id: str | None
    server_name: str | None
    server_version: str | None
    answer_kind: str


# JSON-RPC errors a server answers initialize with when it does not speak the
# requested version: invalid params (2025-06-18's example, which also lists
# the supported versions in data) and UnsupportedProtocolVersion (-32022 in
# 2026-07-28).
_VERSION_ERROR_CODES: Final = frozenset({-32602, -32022})


def _refuses_version(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    data = error.get("data")
    return error.get("code") in _VERSION_ERROR_CODES or (isinstance(data, dict) and "supported" in data)


def _info(value: object) -> str | None:
    return clean_text(value)[:128] if isinstance(value, str) else None


class SplunkMcpClient:
    """The gateway's MCP client for the Splunk MCP Server: POST only, one
    shared session per process, re-initialized once when the server expires
    it (404 on a request carrying its id). Each call takes a deadline (event
    loop time) that bounds every request it makes."""

    def __init__(
        self,
        backend: SplunkBackend,
        token: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        check_token(token)
        self.backend = backend
        self.upstream = UpstreamClient(
            backend.base_url,
            timeout_s=backend.timeout_s,
            max_bytes=backend.max_response_bytes,
            ca_file=backend.ca_file,
            allowed_methods=frozenset({"POST"}),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )
        self._session: McpSession | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self.answer_kinds: set[str] = set()
        self.last_answer_kind: str | None = None

    def __repr__(self) -> str:  # never the token
        return f"SplunkMcpClient(base={self.backend.base_url!r})"

    def _headers(self, session: McpSession | None) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream"}
        if session is not None:
            headers["MCP-Protocol-Version"] = session.protocol
            if session.session_id is not None:
                headers["Mcp-Session-Id"] = session.session_id
        return headers

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise GatewayError("upstream_timeout")
        return remaining

    async def _exchange(
        self, method: str, params: Mapping[str, Any], session: McpSession | None, deadline: float
    ) -> tuple[dict[str, Any], UpstreamResponse, str]:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)}
        reader = ResponseReader(self._next_id)
        response = await self.upstream.request_until(
            "POST",
            self.backend.mcp_path,
            json_body=message,
            headers=self._headers(session),
            deadline_s=self._remaining(deadline),
            until=reader.feed,
        )
        kind = content_kind(response.content_type)
        if kind == "json":
            reader.consider_body(response.body)
        elif kind is None:
            raise GatewayError("upstream_protocol")
        if reader.message is None:
            raise GatewayError("upstream_protocol")
        self.answer_kinds.add(kind)
        self.last_answer_kind = kind
        return reader.message, response, kind

    async def _initialize(self, deadline: float) -> McpSession:
        params = {"protocolVersion": CLIENT_PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO}
        try:
            reply, response, kind = await self._exchange("initialize", params, None, deadline)
        except GatewayError as exc:
            if exc.code == "not_found":
                raise GatewayError("upstream_protocol") from None
            raise
        if _refuses_version(reply.get("error")):
            raise UnsupportedProtocol()
        result = reply.get("result")
        if not isinstance(result, dict):
            raise GatewayError("upstream_protocol")
        if result.get("protocolVersion") not in SUPPORTED_PROTOCOLS:
            raise UnsupportedProtocol()
        session_id = response.headers.get("mcp-session-id")
        if session_id is not None and not _SESSION_ID_RE.fullmatch(session_id):
            raise GatewayError("upstream_protocol")
        info = result.get("serverInfo")
        info = info if isinstance(info, dict) else {}
        session = McpSession(
            protocol=result["protocolVersion"],
            session_id=session_id,
            server_name=_info(info.get("name")),
            server_version=_info(info.get("version")),
            answer_kind=kind,
        )
        try:
            await self.upstream.request_until(
                "POST",
                self.backend.mcp_path,
                json_body={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=self._headers(session),
                deadline_s=self._remaining(deadline),
                until=_never,
            )
        except GatewayError as exc:
            if exc.code == "not_found":
                raise GatewayError("upstream_protocol") from None
            raise
        return session

    async def session(self, deadline: float) -> McpSession:
        """The shared session, initialized on first use. It is published only
        once the initialized notification was accepted, so a call cancelled
        part-way leaves no half session behind."""
        current = self._session
        if current is not None:
            return current
        async with self._lock:
            if self._session is None:
                self._session = await self._initialize(deadline)
            return self._session

    async def _renew(self, expired: McpSession, deadline: float) -> McpSession:
        async with self._lock:
            if self._session is expired or self._session is None:
                self._session = None
                self._session = await self._initialize(deadline)
            return self._session

    async def request(self, method: str, params: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        """One request's result. A 404 on a request carrying a session id
        means the session expired: initialize again once and retry once; a
        second 404, or a 404 without a session, is `upstream_protocol`. A
        JSON-RPC error is `upstream_error` (its text is never kept)."""
        session = await self.session(deadline)
        try:
            reply, _, _ = await self._exchange(method, params, session, deadline)
        except GatewayError as exc:
            if exc.code != "not_found":
                raise
            if session.session_id is None:
                raise GatewayError("upstream_protocol") from None
            session = await self._renew(session, deadline)
            try:
                reply, _, _ = await self._exchange(method, params, session, deadline)
            except GatewayError as again:
                if again.code == "not_found":
                    raise GatewayError("upstream_protocol") from None
                raise
        if "error" in reply:
            raise GatewayError("upstream_error")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise GatewayError("upstream_protocol")
        return result

    async def list_tools(self, deadline: float) -> list[dict[str, Any]]:
        """Every tool the server lists, following `nextCursor` for at most
        MAX_TOOL_PAGES pages (more is `upstream_protocol`)."""
        found: list[dict[str, Any]] = []
        cursor: object = None
        for _ in range(MAX_TOOL_PAGES):
            result = await self.request("tools/list", {} if cursor is None else {"cursor": cursor}, deadline)
            page = result.get("tools")
            if not isinstance(page, list):
                raise GatewayError("upstream_protocol")
            found.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if cursor is None:
                return found
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 1024:
                raise GatewayError("upstream_protocol")
        raise GatewayError("upstream_protocol")

    async def call_tool(self, name: str, arguments: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": dict(arguments)}, deadline)

    async def run_query(self, query: str, deadline: float) -> dict[str, Any]:
        """The run-query tool's result for `query`, with the configured constant
        arguments."""
        arguments: dict[str, Any] = {**self.backend.extra_arguments, self.backend.query_argument: query}
        return await self.call_tool(self.backend.tool_name, arguments, deadline)

    async def aclose(self) -> None:
        await self.upstream.aclose()


def tool_check(backend: SplunkBackend, tools: Sequence[Mapping[str, Any]]) -> str | None:
    """None when the run-query tool is listed and takes the configured
    arguments; otherwise a fixed reason."""
    tool = next((t for t in tools if t.get("name") == backend.tool_name), None)
    if tool is None:
        return "tool_missing"
    schema = tool.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return "tool_schema_unreadable"
    if backend.query_argument not in props:
        return "query_argument_missing"
    if set(backend.extra_arguments) - set(props):
        return "extra_argument_missing"
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        return "tool_schema_unreadable"
    if set(required) - {backend.query_argument} - set(backend.extra_arguments):
        return "required_argument_unset"
    return None


# --- macros (ADR-0015 D9) ----------------------------------------------------------------

MAX_MACRO_DEPTH: Final = 5
# What a non-integer parameter placeholder stands for while a definition is
# checked: an identifier no index allowlist holds, so an index that comes
# from a parameter is refused, and one that must stay inside double quotes
# (a caller's value placed bare could be an operator, a field name, an index
# term or a command). Integer parameters stand as 1.
_PARAM_MARKER: Final = "mfrparamvalue"
_TIME_TERM_RE: Final = re.compile(
    r"(?i)(?<![A-Za-z0-9_.])(?:earliest|latest|_index_earliest|_index_latest"
    r"|starttime|endtime|starttimeu|endtimeu|timeformat|searchtimespan[a-z]*"
    r"|(?:start|end)?(?:days|hours|minutes|months)ago|savedsearch|eventtype)"
    r"\s*(?:!=|=|<|>|::|in\b)"
)
_INDEX_TERM_RE: Final = re.compile(r"(?i)(?<![A-Za-z0-9_.])index\s*(!=|=|<|>|::|in\b)\s*")
_INDEX_VALUE_RE: Final = re.compile(r"[^\s()]+")
_INDEX_LIST_RE: Final = re.compile(r"\(([^()]*)\)")
_OR_RE: Final = re.compile(r"(?<![A-Za-z0-9_])OR(?![A-Za-z0-9_])")
_NOT_BEFORE_RE: Final = re.compile(r"(?<![A-Za-z0-9_])NOT\s*$")
_TRUE_WORDS: Final = frozenset({"1", "true", "t", "yes", "y"})


class MacroCheckError(Exception):
    """A macro definition failed a check. `macro` is the allowlisted macro
    to blame; the definition text never enters the error."""

    def __init__(self, reason: str, macro: str | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.macro = macro


@dataclass(frozen=True)
class MacroDefinition:
    definition: str
    args: tuple[str, ...] = ()
    iseval: bool = False


def _at(value: object, path: Sequence[str]) -> object:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def macro_definitions(source: MacroSource, result: Mapping[str, Any]) -> dict[str, MacroDefinition] | None:
    """The definitions in a knowledge-object result, by stanza name; None
    when the rows cannot be found."""
    if result.get("isError") not in (None, False):
        return None
    rows = find_rows_at(result, source.result_source, source.result_path)
    if rows is None:
        return None
    out: dict[str, MacroDefinition] = {}
    conflicting: set[str] = set()
    for row in rows:
        name, definition = _at(row, source.name_path), _at(row, source.definition_path)
        if not isinstance(name, str) or not isinstance(definition, str) or name in conflicting:
            continue
        raw_args = _at(row, source.args_path)
        if isinstance(raw_args, str):
            args = tuple(a.strip() for a in raw_args.split(",") if a.strip())
        elif isinstance(raw_args, list):
            args = tuple(str(a).strip() for a in raw_args)
        else:
            args = ()
        iseval = _at(row, source.iseval_path)
        entry = MacroDefinition(definition, args, iseval is True or iseval == 1 or str(iseval).lower() in _TRUE_WORDS)
        if out.setdefault(name, entry) != entry:
            # The same name from two apps with different definitions: which
            # one a search uses depends on app context, so neither is checked.
            del out[name]
            conflicting.add(name)
    return out


async def read_macro_definitions(
    client: SplunkMcpClient, tools: Sequence[Mapping[str, Any]], deadline: float
) -> dict[str, MacroDefinition] | None:
    """Every macro definition the knowledge-object tool lists, or None when
    they cannot be read: the tool is not listed, its schema lacks the
    configured arguments or requires others, the call fails or is refused,
    or no rows are found."""
    source = client.backend.macro_source
    tool = next((t for t in tools if t.get("name") == source.tool_name), None)
    schema = tool.get("inputSchema") if tool is not None else None
    props = schema.get("properties") if isinstance(schema, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else None
    sent = {source.type_argument, *source.extra_arguments}
    if (
        not isinstance(props, dict)
        or not sent <= set(props)
        or not isinstance(required, list)
        or not all(isinstance(r, str) for r in required)
        or set(required) - sent
    ):
        return None
    arguments = {**source.extra_arguments, source.type_argument: source.type_value}
    try:
        result = await client.call_tool(source.tool_name, arguments, deadline)
    except GatewayError:
        return None
    return macro_definitions(source, result)


def _mask_or_fail(text: str, macro: str | None) -> list[bool]:
    try:
        return _quoted_mask(text, "macro")
    except GatewayConfigError:
        raise MacroCheckError("macro_unbalanced_quote", macro) from None


def lint_expansion(text: str, allowed_indexes: Sequence[str], macro: str | None) -> None:
    """The template rules on a macro's expansion: no subsearch, unbalanced
    parentheses, refused command or time modifier; index terms only with =,
    :: or IN and only allowlisted names (no wildcard, none from a
    parameter)."""
    mask = _mask_or_fail(text, macro)
    unquoted = "".join(" " if quoted else char for char, quoted in zip(text, mask, strict=True))
    if "[" in unquoted or "]" in unquoted:
        raise MacroCheckError("macro_subsearch", macro)
    depth = 0
    for char in unquoted:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            break
    if depth != 0:
        raise MacroCheckError("macro_unbalanced_parentheses", macro)
    for command in _COMMAND_RE.findall(unquoted):
        if command.lower() in REFUSED_COMMANDS:
            raise MacroCheckError("macro_refused_command", macro)
    stripped = text.replace('"', " ")
    if _TIME_TERM_RE.search(stripped):
        raise MacroCheckError("macro_time_modifier", macro)
    for name in _index_names(stripped, macro):
        if name not in allowed_indexes:
            raise MacroCheckError("macro_index_not_allowed", macro)
    for match in re.finditer(_PARAM_MARKER, text):
        if not mask[match.start()]:
            raise MacroCheckError("macro_param_unquoted", macro)


def _index_names(stripped: str, macro: str | None) -> list[str]:
    names: list[str] = []
    for match in _INDEX_TERM_RE.finditer(stripped):
        op, rest = match.group(1).lower(), stripped[match.end() :]
        if op in ("=", "::"):
            value = _INDEX_VALUE_RE.match(rest)
            names.append(value.group(0) if value else "")
        elif op == "in":
            listed = _INDEX_LIST_RE.match(rest)
            if listed is None:
                raise MacroCheckError("macro_index_not_allowed", macro)
            names.extend(n for n in re.split(r"[\s,]+", listed.group(1)) if n)
        else:
            raise MacroCheckError("macro_index_not_allowed", macro)  # !=, <, > widen the search
    return names


def expand_macros(
    text: str,
    defs: Mapping[str, MacroDefinition],
    backend: SplunkBackend,
    *,
    owner: str | None = None,
    depth: int = 0,
    stack: tuple[str, ...] = (),
    seen: dict[str, int] | None = None,
) -> tuple[str, int]:
    """`text` with every macro call replaced by its checked expansion, and
    the deepest nesting reached. Each definition's arguments are substituted
    (`$name$`), nested calls expanded first (bounded depth, cycle-safe, each
    nested macro allowlisted), and the expansion linted; the innermost
    failing macro is blamed."""
    mask = _mask_or_fail(text, owner)
    try:
        spans = backtick_spans(text, mask, "macro")
    except GatewayConfigError:
        raise MacroCheckError("macro_backtick", owner) from None
    deepest = depth
    for start, end in reversed(spans):
        match = _MACRO_CALL_RE.fullmatch(text[start + 1 : end - 1])
        if match is None:
            raise MacroCheckError("macro_backtick", owner)  # a comment or other backtick text
        name, raw = match.group(1), match.group(2)
        if name not in backend.allowed_macros:
            raise MacroCheckError("macro_nested_not_allowed", owner)
        args = tuple(raw.split(",")) if raw is not None else ()
        stanza = name if not args else f"{name}({len(args)})"
        if stanza in stack:
            raise MacroCheckError("macro_cycle", name)
        if depth + 1 > MAX_MACRO_DEPTH:
            raise MacroCheckError("macro_too_deep", name)
        definition = defs.get(stanza)
        if definition is None or definition.iseval or len(definition.args) != len(args):
            raise MacroCheckError("macro_unreadable", name)
        body = definition.definition
        for arg_name, value in zip(definition.args, args, strict=True):
            body = body.replace(f"${arg_name}$", value.strip())
        expanded, reached = expand_macros(
            body, defs, backend, owner=name, depth=depth + 1, stack=(*stack, stanza), seen=seen
        )
        lint_expansion(expanded, backend.allowed_indexes, name)
        if seen is not None:
            seen[name] = max(seen.get(name, 0), reached - depth)
        text = text[:start] + expanded + text[end:]
        deepest = max(deepest, reached)
    return text, deepest


def _marked(template: Template, text: str) -> str:
    kinds = {p.name: p.kind for p in template.params}
    return _PLACEHOLDER_RE.sub(lambda m: "1" if kinds.get(m.group(1)) == "integer" else _PARAM_MARKER, text)


_INDEX_GROUP_RE: Final = re.compile(
    r"(?i)\s*index\s*(?:=|::)\s*[^\s()]+(?:\s+OR\s+index\s*(?:=|::)\s*[^\s()]+)*\s*"
)


def index_bounded(terms: str, allowed_indexes: Sequence[str]) -> bool:
    """Whether expanded search terms restrict the search to allowlisted
    indexes on their own: no unquoted OR at the top level (Splunk's OR binds
    before AND, so it could add other data), and at least one top-level
    positive index restriction (not preceded by NOT): an index term with =,
    :: or IN, or a group holding only index terms joined by OR. Every index
    named anywhere was already checked against the allowlist."""
    mask = _quoted_mask(terms, "terms")
    stripped = terms.replace('"', " ")
    depths: list[int] = []
    depth = 0
    groups: list[tuple[int, int]] = []
    opened = 0
    for at, (char, quoted) in enumerate(zip(terms, mask, strict=True)):
        if not quoted and char == "(":
            if depth == 0:
                opened = at
            depth += 1
        depths.append(depth)
        if not quoted and char == ")":
            depth -= 1
            if depth == 0:
                groups.append((opened, at))
    top_unquoted = "".join(
        c if d == 0 and not q else " " for c, d, q in zip(terms, depths, mask, strict=True)
    )
    if _OR_RE.search(top_unquoted):
        return False

    def positive(start: int, names: Sequence[str]) -> bool:
        return (
            not _NOT_BEFORE_RE.search(top_unquoted[:start])
            and bool(names)
            and all(n in allowed_indexes for n in names)
        )

    for match in _INDEX_TERM_RE.finditer(stripped):
        if mask[match.start()]:
            continue  # text inside a string restricts nothing
        names = _index_names(stripped[match.start() :], None)
        if match.group(1).lower() != "in":
            names = names[:1]
        if depths[match.start()] == 0 and positive(match.start(), names):
            return True
    for start, end in groups:
        inner = stripped[start + 1 : end]
        keywords = [start + 1 + m.start() for m in _INDEX_TERM_RE.finditer(inner)]
        if (
            _INDEX_GROUP_RE.fullmatch(inner)
            and not any(mask[at] for at in keywords)
            and positive(start, _index_names(inner, None))
        ):
            return True
    return False


@dataclass
class MacroVerdict:
    """The site probe's view of one allowlisted macro: whether every
    definition a template needs was readable, the check's verdict code
    (`ok`, `unused` or a failure reason) and the deepest nesting."""

    readable: bool = True
    verdict: str = "unused"
    depth: int = 0


@dataclass(frozen=True)
class MacroAssessment:
    readable: bool
    verdicts: Mapping[str, MacroVerdict]
    failures: tuple[tuple[str, str | None], ...]


def assess_macros(backend: SplunkBackend, defs: Mapping[str, MacroDefinition] | None) -> MacroAssessment:
    """Check every template macro call (and, for an index_from_macro
    template, that its expanded terms bound the indexes). Never raises."""
    verdicts = {name: MacroVerdict() for name in backend.allowed_macros}
    failures: list[tuple[str, str | None]] = []
    if defs is None:
        for template in backend.templates:
            for call in template.macros:
                verdicts[call.name] = MacroVerdict(readable=False, verdict="macro_unreadable")
        return MacroAssessment(False, verdicts, ())
    readable = True
    for template in backend.templates:
        ok = True
        for call in template.macros:
            text = _marked(template, f"`{call.name}{'(' + ','.join(call.args) + ')' if call.args else ''}`")
            verdict = verdicts[call.name]
            seen: dict[str, int] = {}
            try:
                expanded, reached = expand_macros(text, defs, backend, seen=seen)
                if call.in_terms and "|" in "".join(
                    " " if q else c for c, q in zip(expanded, _quoted_mask(expanded, "macro"), strict=True)
                ):
                    raise MacroCheckError("macro_pipe_in_terms", call.name)
            except MacroCheckError as exc:
                ok = False
                blamed = verdicts.get(exc.macro or call.name, verdict)
                if exc.reason == "macro_unreadable":
                    readable = False
                    blamed.readable = False
                else:
                    failures.append((exc.reason, exc.macro or call.name))
                if blamed.verdict in ("unused", "ok"):
                    blamed.verdict = exc.reason
                if verdict is not blamed and verdict.verdict in ("unused", "ok"):
                    verdict.verdict = exc.reason
                continue
            for name, reach in seen.items():
                nested = verdicts[name]
                nested.depth = max(nested.depth, reach)
                if nested.verdict == "unused":
                    nested.verdict = "ok"
            del reached
        if not ok:
            continue
        failure = whole_template_failure(template, defs, backend)
        if failure is None and template.index_from_macro:
            first = next(c.name for c in template.macros if c.in_terms)
            terms, _ = split_terms(template.search)
            try:
                expanded, _ = expand_macros(_marked(template, terms), defs, backend)
                bounded = index_bounded(expanded, backend.allowed_indexes)
            except (MacroCheckError, GatewayConfigError):
                bounded = False
            if not bounded:
                failure = ("index_not_bounded", first)
        if failure is not None:
            failures.append(failure)
            if verdicts[failure[1]].verdict == "ok":
                verdicts[failure[1]].verdict = failure[0]
    return MacroAssessment(readable, verdicts, tuple(failures))


def whole_template_failure(
    template: Template, defs: Mapping[str, MacroDefinition], backend: SplunkBackend
) -> tuple[str, str] | None:
    """The expansion rules over the template's whole search, expanding one
    more call at a time, so a macro used after the template's own pipe (a
    definition such as `delete` with no pipe of its own) or text split
    across the template and a macro is checked too. Blames the call whose
    expansion first breaks a rule."""
    marked = _marked(template, template.search)
    spans = backtick_spans(marked, _quoted_mask(marked, "template"), "template")
    for upto, call in enumerate(template.macros, start=1):
        text = list(marked)
        for start, end in spans[upto:]:
            text[start:end] = " " * (end - start)
        try:
            expanded, _ = expand_macros("".join(text), defs, backend)
            lint_expansion(expanded, backend.allowed_indexes, call.name)
        except MacroCheckError as exc:
            return exc.reason, call.name
    return None


def enforce_macros(backend: SplunkBackend, assessment: MacroAssessment) -> str:
    """The start-up decision: any failed check refuses (naming the macro);
    unreadable definitions refuse unless `allow_unverified_macros`, which
    leaves the token user's role as the only bound on what a macro reads or
    runs. Returns `verified` or `unverified`."""
    if assessment.failures:
        reason, macro = assessment.failures[0]
        raise BackendCheckError(reason, macro)
    if assessment.readable:
        return "verified"
    if backend.allow_unverified_macros:
        return "unverified"
    unreadable = next((n for n, v in assessment.verdicts.items() if not v.readable), None)
    raise BackendCheckError("macros_unverifiable", unreadable)


@dataclass(frozen=True)
class BackendReport:
    protocol: str
    session_issued: bool
    tools: int
    macros: str | None = None  # "verified", "unverified", or None when no template uses one


async def check_backend(client: SplunkMcpClient) -> BackendReport:
    """The start-up check (ADR-0015 D4): initialize, a supported protocol,
    and the run-query tool with the configured arguments. Fails closed with
    a fixed reason; a backend that cannot answer is a refusal too."""
    deadline = asyncio.get_running_loop().time() + client.backend.timeout_s
    try:
        session = await client.session(deadline)
        tools = await client.list_tools(deadline)
    except UnsupportedProtocol:
        raise BackendCheckError("protocol_unsupported") from None
    except GatewayError as exc:
        raise BackendCheckError(exc.code) from None
    reason = tool_check(client.backend, tools)
    if reason is not None:
        raise BackendCheckError(reason)
    macros: str | None = None
    if any(t.macros for t in client.backend.templates):
        defs = await read_macro_definitions(client, tools, deadline)
        macros = enforce_macros(client.backend, assess_macros(client.backend, defs))
    return BackendReport(session.protocol, session.session_id is not None, len(tools), macros)


# --- results ----------------------------------------------------------------------------------


def _text_items(result: Mapping[str, Any]) -> list[str]:
    content = result.get("content")
    if not isinstance(content, list):
        return []
    return [
        item["text"]
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]


def _follow(value: object, path: Sequence[str]) -> list[Any] | None:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value
    return None


def find_rows(backend: SplunkBackend, result: Mapping[str, Any]) -> list[Any] | None:
    """The result rows at `result_path` in `structuredContent` or in JSON
    text content, as `result_source` allows; None when neither holds them."""
    return find_rows_at(result, backend.result_source, backend.result_path)


def find_rows_at(result: Mapping[str, Any], source: str, path: Sequence[str]) -> list[Any] | None:
    candidates: list[object] = []
    if source in ("auto", "structured") and "structuredContent" in result:
        candidates.append(result["structuredContent"])
    if source in ("auto", "text_json"):
        for text in _text_items(result):
            try:
                candidates.append(json.loads(text))
            except ValueError, RecursionError:
                continue
    for candidate in candidates:
        rows = _follow(candidate, path)
        if rows is not None:
            return rows
    return None


def result_output(backend: SplunkBackend, result: Mapping[str, Any], rows: int) -> ToolOutput:
    """The tool output for one run-query result. `isError` (Splunk's one-minute
    and 1000-event failures, guardrail refusals) is `search_refused`; a result whose
    rows cannot be found is `upstream_protocol`. Upstream text is never returned
    outside the untrusted frame."""
    if result.get("isError") not in (None, False):
        raise GatewayError("search_refused")
    if backend.result_source == "text":
        texts = _text_items(result)
        if not texts:
            raise GatewayError("upstream_protocol")
        return ToolOutput(text="\n".join(texts))
    found = find_rows(backend, result)
    if found is None:
        raise GatewayError("upstream_protocol")
    return ToolOutput(rows=found[:rows])


def shape(value: object, depth: int = 0) -> object:
    """The keys and JSON types of `value`, never its values (the site probe):
    an object maps its first 50 keys to their shapes, an array gives its
    length and its first item's shape."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if depth >= 6:
        return "nested"
    if isinstance(value, list):
        return {"array_length": len(value), "item": shape(value[0], depth + 1) if value else None}
    if isinstance(value, dict):
        return {clean_text(str(k))[:64]: shape(v, depth + 1) for k, v in list(value.items())[:50]}
    return "unknown"


# --- tools ----------------------------------------------------------------------------------------


def _time_schema(description: str) -> dict[str, Any]:
    return {"type": "string", "pattern": f"^(?:{_TIME_PATTERN})$", "maxLength": 16, "description": description}


def template_schema(backend: SplunkBackend, template: Template) -> dict[str, Any]:
    props: dict[str, Any] = {p.name: p.schema() for p in template.params}
    props["earliest"] = _time_schema(
        f"Start of the time range, relative to now: now, -<n><s|m|h|d|w>, optionally snapped"
        f" with @<unit>, or @<unit>. Default: {template.default_earliest}."
    )
    props["latest"] = _time_schema("End of the time range, in the same form. Default: now.")
    props["rows"] = {
        "type": "integer",
        "minimum": 1,
        "maximum": backend.max_rows,
        "description": f"Most rows to return. Default: {template.default_rows or backend.max_rows}.",
    }
    required = [p.name for p in template.params if p.default is None]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _description(backend: SplunkBackend, template: Template) -> str:
    return (
        f"{template.description} Runs a fixed Splunk search; the results are untrusted data."
        f" The time range spans at most {template.max_window_s} seconds; a snapped start"
        f" counts its whole snap unit. At most {backend.max_rows} rows."
    )


def template_tools(client: SplunkMcpClient) -> list[Tool]:
    backend = client.backend

    def handler_for(template: Template) -> Any:
        async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
            spl, rows = compose_spl(backend, template, args, ctx)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + backend.timeout_s
            try:
                async with asyncio.timeout_at(deadline):
                    result = await client.run_query(spl, deadline)
            except TimeoutError:
                raise GatewayError("upstream_timeout") from None
            return result_output(backend, result, rows)

        return handler

    return [
        Tool(
            name=t.name,
            description=_description(backend, t),
            input_schema=template_schema(backend, t),
            handler=handler_for(t),
        )
        for t in backend.templates
    ]


# --- assembly and entry point ---------------------------------------------------------------


@dataclass
class Gateway:
    app: ASGIApp
    client: SplunkMcpClient
    mode: str
    limits: Limits
    backend: SplunkBackend


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
    `transport` (a fake Splunk MCP Server) and `jwks`; nothing here touches
    the network."""
    env = os.environ if environ is None else environ
    mode = auth_mode(env)
    config = load_config(
        GATEWAY,
        env,
        require_auth=mode == "oidc",
        limit_defaults={**LIMIT_DEFAULTS, **(BEARER_LIMIT_DEFAULTS if mode == "bearer" else {})},
    )
    backend = backend_from_config(config)
    if backend.timeout_s >= config.limits.tool_timeout_s:
        # The backend call must end, with the gateway's own code, before the call is cancelled.
        raise GatewayConfigError("backend.timeout_s must be below limits.tool_timeout_s")
    if backend.max_rows > config.limits.max_rows:
        raise GatewayConfigError("backend.max_rows must be at most limits.max_rows")
    client = SplunkMcpClient(backend, env.get(TOKEN_ENV, ""), transport=transport)
    tools = template_tools(client)
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
    return Gateway(app=app, client=client, mode=mode, limits=config.limits, backend=backend)


async def _checked(gateway: Gateway) -> BackendReport:
    """The start-up check on its own client, closed in the same event loop
    (uvicorn serves on another loop with a fresh client and session)."""
    try:
        return await check_backend(gateway.client)
    finally:
        await gateway.client.aclose()


def _site_probe(argv: Sequence[str]) -> int:
    """`--site-probe`: the read-only ADR-0015 site checks with the gateway's
    configuration and client (splunk_probe.py). Exit codes as the other probes."""
    import argparse
    from pathlib import Path

    from mainframe_rag.gateways import splunk_probe
    from mainframe_rag.probe_receipt import write_receipt

    parser = argparse.ArgumentParser(prog="python -m mainframe_rag.gateways.splunk")
    parser.add_argument("--site-probe", action="store_true", required=True)
    parser.add_argument("--receipt", type=Path, default=None, help="also write the receipt (0600)")
    args = parser.parse_args(list(argv))
    try:
        gateway = build_gateway()
    except GatewayConfigError as exc:
        print(f"splunk site probe: invalid configuration ({exc})", file=sys.stderr)
        return 2
    receipt, detail = asyncio.run(splunk_probe.run_splunk_probe(gateway))
    try:
        write_receipt(receipt, sys.stdout, args.receipt)
    except OSError:
        print("splunk site probe: the receipt file could not be written", file=sys.stderr)
        return 2
    print(json.dumps({"probe": "splunk", "site_private_detail": detail}, sort_keys=True))
    return receipt.exit_code()


def main(argv: Sequence[str] | None = None) -> int:
    """Start-up: configuration, then the backend check, then serve. Any
    failure exits non-zero with a fixed reason; the token is never logged.
    `--site-probe` runs the read-only site probe instead of serving."""
    from mainframe_rag.gateways.common import serve

    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if "--site-probe" in args:
            return _site_probe(args)
        print("splunk gateway: the only option is --site-probe", file=sys.stderr)
        return 2
    configure_gateway_logging()
    try:
        report = asyncio.run(_checked(build_gateway()))
        gateway = build_gateway()
    except GatewayConfigError as exc:
        log.error(
            json.dumps(
                {"action": "splunk_gateway_start", "decision": "config_error", "detail": str(exc)}
            )
        )
        return 2
    except BackendCheckError as exc:
        line = {"action": "splunk_gateway_start", "decision": exc.reason}
        if exc.macro is not None:
            line["macro"] = exc.macro  # the allowlisted name, never its definition
        log.error(json.dumps(line))
        return 3
    log.info(
        json.dumps(
            {
                "action": "splunk_gateway_start",
                "decision": "ok",
                "mode": gateway.mode,
                "protocol": report.protocol,
                "session_issued": report.session_issued,
                "templates": len(gateway.backend.templates),
                "macros": report.macros,
            }
        )
    )
    serve(gateway.app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
