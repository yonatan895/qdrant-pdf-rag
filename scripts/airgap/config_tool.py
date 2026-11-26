#!/usr/bin/env python3
"""Operator configuration inspection and static checking (issue #1284).

Implements `airgap:config show` and `airgap:config check`.
Strictly stdlib-only (`python3 -I`-safe): runs on the air-gap bastion without
third-party packages or `mainframe_rag` imports.

Receipt-safe contract:
- `show` prints only key names, non-secret values, secret-ref names (never
  secret contents), `<redacted>` for sensitive credentials, and default/set
  provenance.
- `check` emits fixed messages naming keys and rules only, never echoing
  operator/secret values.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

try:
    from scripts.airgap import model_config, operator_keys
except ModuleNotFoundError:
    # Run as a file (`python3 -I`): search sibling after stdlib.
    sys.path.append(str(Path(__file__).resolve().parent))
    import model_config  # type: ignore[no-redef]
    import operator_keys  # type: ignore[no-redef]

_DNS_SUBDOMAIN = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_SECRET_DATA_KEY = re.compile(r"[A-Za-z0-9._-]{1,253}")
_REGISTRY = re.compile(
    r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]+([._-][a-z0-9]+)*)*"
)
_QDRANT_SIZE = re.compile(r"[1-9][0-9]*(?:\.[0-9]+)?(?:Ki|Mi|Gi|Ti)")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_BASE_URL_KEYS = (
    "GATEWAY_BASE_URL",
    "EMBED_BASE_URL",
    "LLM_BASE_URL",
    "RERANK_BASE_URL",
    "CONTEXT_LLM_BASE_URL",
)
_GATEWAY_FALLBACK_KEYS = (
    "EMBED_BASE_URL",
    "LLM_BASE_URL",
    "RERANK_BASE_URL",
    "CONTEXT_LLM_BASE_URL",
)
_COLLECTION_POLICY_PRESET = {
    "QDRANT_SHARD_NUMBER": "6",
    "QDRANT_REPLICATION_FACTOR": "3",
    "QDRANT_WRITE_CONSISTENCY_FACTOR": "2",
}
_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;&|({\s])"
    r"(?:(?:export|readonly|declare|typeset)(?:\s+-[A-Za-z]+|\s+--)*\s+)*"
    r"([A-Za-z_][A-Za-z0-9_]*)\+?=([^\s;#&|)]*(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|[^\s;#&|)]*)*)"
)


def scan_env_file(path: Path) -> dict[str, bool]:
    """Return `{key: is_non_empty}` for every uncommented assignment in `path`.

    Pure text scan: never executes the file and never returns assigned values.
    """
    if not path.is_file():
        return {}
    found: dict[str, bool] = {}
    in_single_quote = False
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if in_single_quote:
            if "'" in raw_line:
                in_single_quote = False
            continue
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip inline comment outside quotes for scanning.
        code = _strip_comment(raw_line)
        if code.count("'") % 2 == 1:
            in_single_quote = True
        for match in _ASSIGNMENT_RE.finditer(code):
            key = match.group(1)
            rhs = match.group(2).strip()
            non_empty = rhs not in ("", '""', "''")
            found[key] = found.get(key, False) or non_empty
    return found


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and (idx == 0 or line[idx - 1].isspace()):
            return line[:idx]
    return line


def _single_line(value: str) -> str:
    if "\n" in value or "\r" in value:
        return value.replace("\r", "\\r").replace("\n", "\\n")
    return value


def _format_default(entry: operator_keys.OperatorKey) -> str:
    if entry.default is None or entry.default == "":
        return "<unset>"
    if isinstance(entry.default, bool):
        return "true" if entry.default else "false"
    if isinstance(entry.default, float) and entry.default.is_integer():
        return str(int(entry.default))
    return str(entry.default)


def render_show(
    values: Mapping[str, str],
    *,
    env_file: Path | None = None,
    cli_keys: set[str] | None = None,
) -> str:
    """Format effective configuration grouped by registry section (receipt-safe)."""
    file_assignments = scan_env_file(env_file) if env_file is not None else {}
    file_keys = {k for k, non_empty in file_assignments.items() if non_empty}
    gateway_base = values.get("GATEWAY_BASE_URL", "")
    storage_class = values.get("STORAGE_CLASS", "")

    file_label = str(env_file) if env_file is not None and env_file.is_file() else "none"
    lines: list[str] = [f"# Effective air-gap configuration (env file: {file_label})"]

    by_section: dict[str, list[operator_keys.OperatorKey]] = {sec: [] for sec in operator_keys.SECTIONS}
    for entry in operator_keys.OPERATOR_KEYS:
        by_section[entry.section].append(entry)

    for section in operator_keys.SECTIONS:
        entries = by_section[section]
        if not entries:
            continue
        lines.append("")
        lines.append(f"[{section}]")
        for entry in entries:
            raw = values.get(entry.name, "")
            is_cli = (
                (entry.name in cli_keys)
                if cli_keys is not None
                else (raw != "" and entry.name not in file_keys)
            )
            is_file = entry.name in file_keys

            # Determine provenance: explicit env/CLI > file > default/preset
            if is_cli and raw != "":
                source = "set (env)"
                is_set = True
            elif is_file and raw != "":
                source = "set (file)"
                is_set = True
            else:
                source = "default"
                is_set = False

            # Format effective value without ever leaking secret values.
            if entry.sensitive:
                rendered_val = "<redacted>" if is_set else "<unset>"
            elif entry.key_type == "secret-ref":
                rendered_val = f"{_single_line(raw)} (secret-ref)" if is_set else "<unset> (secret-ref)"
            elif is_set:
                rendered_val = _single_line(raw)
            elif entry.name in _GATEWAY_FALLBACK_KEYS and gateway_base:
                rendered_val = _single_line(gateway_base)
                source = "default (from GATEWAY_BASE_URL)"
            elif entry.name == "SNAPSHOT_STORAGE_CLASS" and storage_class:
                rendered_val = _single_line(storage_class)
                source = "default (from STORAGE_CLASS)"
            elif entry.name in _COLLECTION_POLICY_PRESET and raw != "":
                rendered_val = _single_line(raw)
                source = "default"
            elif entry.name == "AIRGAP_DRYRUN":
                rendered_val = "false" if raw in ("", "0", "false", "no") else "true"
                source = "default"
            else:
                rendered_val = _format_default(entry)

            lines.append(f"  {entry.name} = {rendered_val}  [{source}]")

    return "\n".join(lines) + "\n"


def _add_unique(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def _effective_with_aliases(values: Mapping[str, str]) -> dict[str, str]:
    eff = dict(values)
    gw = eff.get("GATEWAY_BASE_URL", "")
    if gw:
        for key in _GATEWAY_FALLBACK_KEYS:
            if not eff.get(key):
                eff[key] = gw
    return eff


def check_config(
    values: Mapping[str, str],
    *,
    env_file: Path | None = None,
    ci_rehearsal_process_env: str | None = None,
) -> tuple[list[str], list[str]]:
    """Run static configuration checks. Returns `(errors, warnings)` with fixed messages."""
    errors: list[str] = []
    warnings: list[str] = []

    known_operator = operator_keys.OPERATOR_KEYS_BY_NAME
    retired_names = set(operator_keys.retired_key_names())
    forbidden_names = set(operator_keys.FORBIDDEN_ENV_FILE_KEYS)
    process_only_names = set(operator_keys.PROCESS_ENV_ONLY_KEYS)
    ci_only_names = set(operator_keys.ci_only_example_keys(Path(__file__).resolve().parents[2]))
    suggestion_pool = sorted(set(known_operator) | retired_names)

    # 1. Inspect raw env file for unknown keys, forbidden keys, process-env-only keys
    if env_file is not None and env_file.is_file():
        file_assignments = scan_env_file(env_file)
        for key, non_empty in sorted(file_assignments.items()):
            if key in forbidden_names:
                if non_empty:
                    _add_unique(
                        errors,
                        f"{key} must not be set in airgap.env; store gateway keys in the cluster Secret named by GATEWAY_API_KEY_SECRET",
                    )
                continue
            if key in process_only_names:
                if non_empty:
                    _add_unique(
                        errors,
                        f"{key} is honored from the process environment only and must not be set in airgap.env",
                    )
                continue
            if key in ci_only_names:
                if non_empty:
                    _add_unique(
                        warnings,
                        f"{key} is a GitLab CI/CD variable and is ignored by air-gap scripts",
                    )
                continue
            if key in retired_names:
                # Reported with migration details in step 2 below if non-empty.
                continue
            if key not in known_operator:
                matches = difflib.get_close_matches(key, suggestion_pool, n=3, cutoff=0.6)
                if matches:
                    _add_unique(errors, f"unknown key {key} (did you mean {', '.join(matches)}?)")
                else:
                    _add_unique(errors, f"unknown key {key}")

    effective = _effective_with_aliases(values)

    # 2. Retired keys (from env or file)
    try:
        model_config.require_no_retired_keys(effective)
    except ValueError as exc:
        for line in str(exc).splitlines()[1:]:
            cleaned = line.strip()
            if cleaned:
                _add_unique(errors, cleaned)

    # 3. Deprecated keys & CI-rehearsal-only overrides
    ci_rehearsal = (
        ci_rehearsal_process_env
        if ci_rehearsal_process_env is not None
        else values.get("CI_REHEARSAL", "")
    )
    for entry in operator_keys.OPERATOR_KEYS:
        raw = values.get(entry.name, "")
        if not raw:
            continue
        if entry.deprecated is not None:
            if entry.name == "INGEST_ALIAS_PUBLISH":
                try:
                    model_config.require_in_place_retired(raw)
                except ValueError as exc:
                    _add_unique(errors, str(exc))
                else:
                    _add_unique(warnings, f"{entry.name} is deprecated: {entry.deprecated}")
            else:
                _add_unique(warnings, f"{entry.name} is deprecated: {entry.deprecated}")
        if entry.ci_rehearsal_only and ci_rehearsal != "1":
            _add_unique(
                errors,
                f"{entry.name} is a CI-rehearsal override and is refused on the release path; use the site sizing keys instead",
            )

    # 4. Product hard rules
    if values.get("EMBED_MODE", "") == "hash":
        _add_unique(errors, "EMBED_MODE=hash is CI/dev only; air-gap uses the in-cluster vLLM endpoint")
    for sc_key in ("STORAGE_CLASS", "SNAPSHOT_STORAGE_CLASS"):
        sc_val = values.get(sc_key, "")
        if sc_val and "nfs" in sc_val.lower():
            _add_unique(errors, f"{sc_key} must not use NFS; Qdrant-adjacent volumes require RWO block storage")

    # 5. Per-key type, bound, enum, and syntax validation
    for entry in operator_keys.OPERATOR_KEYS:
        raw = values.get(entry.name, "")
        if raw == "":
            continue
        if entry.key_type == "bool":
            try:
                model_config.parse_strict_bool(entry.name, raw, False)
            except ValueError as exc:
                _add_unique(errors, str(exc))
        elif entry.key_type == "int":
            if not re.fullmatch(r"[0-9]+", raw):
                _add_unique(errors, _int_bound_message(entry))
            else:
                num = int(raw)
                if (entry.min_value is not None and num < entry.min_value) or (
                    entry.max_value is not None and num > entry.max_value
                ):
                    _add_unique(errors, _int_bound_message(entry))
        elif entry.key_type == "float":
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
                _add_unique(errors, _float_bound_message(entry))
            else:
                fnum = float(raw)
                if (entry.min_value is not None and fnum < entry.min_value) or (
                    entry.max_value is not None and fnum > entry.max_value
                ):
                    _add_unique(errors, _float_bound_message(entry))
        elif entry.key_type == "enum":
            if entry.name == "EMBED_MODE" and raw == "hash":
                continue  # Already reported with specific product-rule message above.
            if raw not in entry.allowed_values:
                allowed = ", ".join(entry.allowed_values)
                _add_unique(errors, f"{entry.name} must be one of: {allowed}")
        elif entry.key_type == "secret-ref":
            if entry.name == "GATEWAY_API_KEY_SECRET_KEY":
                if _SECRET_DATA_KEY.fullmatch(raw) is None:
                    _add_unique(errors, "GATEWAY_API_KEY_SECRET_KEY must be a Kubernetes Secret data key")
            elif len(raw) > 253 or _DNS_SUBDOMAIN.fullmatch(raw) is None:
                _add_unique(errors, f"{entry.name} must be a DNS-subdomain Secret name")

    # Additional syntax checks matching common.sh / map_values.py
    ns = values.get("NAMESPACE", "")
    if ns and (len(ns) > 63 or _DNS_LABEL.fullmatch(ns) is None):
        _add_unique(errors, "NAMESPACE must be a DNS label of at most 63 characters")
    qrel = values.get("QDRANT_RELEASE", "")
    if qrel and (len(qrel) > 253 or _DNS_LABEL.fullmatch(qrel) is None):
        _add_unique(errors, "QDRANT_RELEASE must be a DNS-1123 label")
    for dns_key in ("STORAGE_CLASS", "SNAPSHOT_STORAGE_CLASS", "GATEWAY_CA_CONFIGMAP"):
        val = values.get(dns_key, "")
        if val and (len(val) > 253 or _DNS_SUBDOMAIN.fullmatch(val) is None):
            _add_unique(errors, f"{dns_key} must be a DNS-1123 name (lowercase alphanumerics, '-', '.')")
    qtag = values.get("QDRANT_TAG", "")
    if qtag and _IMAGE_TAG.fullmatch(qtag) is None:
        _add_unique(errors, "QDRANT_TAG must be an image tag (letters, digits, '_', '.', '-')")
    for sz_key in ("QDRANT_STORAGE_SIZE", "INGEST_WORK_SIZE"):
        val = values.get(sz_key, "")
        if val and _QDRANT_SIZE.fullmatch(val) is None:
            _add_unique(errors, f"{sz_key} must be a positive quantity with a unit (Ki, Mi, Gi or Ti)")
    reg = values.get("INTERNAL_REGISTRY", "")
    if reg and (len(reg) > 253 or _REGISTRY.fullmatch(reg) is None):
        _add_unique(errors, "INTERNAL_REGISTRY must be host[:port][/path] with no spaces, commas or '='")
    sha = values.get("IMAGE_SHA", "")
    if sha and _SHA40.fullmatch(sha) is None:
        _add_unique(errors, "IMAGE_SHA must be a full 40-character lowercase hex git SHA")
    rev = values.get("EMBED_MODEL_REVISION", "")
    if rev and not rev.strip():
        _add_unique(errors, "EMBED_MODEL_REVISION must be a non-blank immutable revision")
    for url_key in _BASE_URL_KEYS:
        val = values.get(url_key, "")
        if val and not val.startswith(("http://", "https://")):
            _add_unique(errors, f"{url_key} must begin with http:// or https://")
    otel = values.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otel and otel.lower() not in ("off", "none", "false", "0") and not otel.startswith(("http://", "https://")):
        _add_unique(errors, "OTEL_EXPORTER_OTLP_ENDPOINT must be http(s)://... or off/none/false/0")
    retire_raw = values.get("INGEST_RETIRE_DOCS", "")
    if retire_raw:
        for chunk in re.split(r"[,\n]", retire_raw):
            entry_str = chunk.strip(" \t")
            if not entry_str:
                continue
            if entry_str.startswith("@") or entry_str.endswith("@"):
                _add_unique(errors, "INGEST_RETIRE_DOCS entries must be DOCID or DOCID@SOURCEREV (no empty side of '@')")
            elif any(ch in entry_str for ch in ('"', "\\", "*", "?")):
                _add_unique(errors, "INGEST_RETIRE_DOCS must not contain quotes, backslashes or wildcards")
            elif any(ord(ch) < 32 or ord(ch) == 127 for ch in entry_str):
                _add_unique(errors, "INGEST_RETIRE_DOCS must not contain control characters")

    # 6. Cross-key & mutually exclusive validators (reusing model_config owners)
    _check_cross_key_rules(effective, errors)

    return errors, warnings


def _int_bound_message(entry: operator_keys.OperatorKey) -> str:
    if entry.min_value is not None and entry.max_value is not None:
        return f"{entry.name} must be an integer from {entry.min_value} to {entry.max_value}"
    if entry.min_value == 1:
        return f"{entry.name} must be a positive integer"
    return f"{entry.name} must be an integer"


def _float_bound_message(entry: operator_keys.OperatorKey) -> str:
    if entry.min_value is not None and entry.max_value is not None:
        lo = int(entry.min_value) if float(entry.min_value).is_integer() else entry.min_value
        hi = int(entry.max_value) if float(entry.max_value).is_integer() else entry.max_value
        return f"{entry.name} must be a number from {lo} to {hi}"
    return f"{entry.name} must be a number"


def _safe_bool(values: Mapping[str, str], key: str, default: bool) -> bool | None:
    try:
        return model_config.parse_strict_bool(key, values.get(key, ""), default)
    except ValueError:
        return None


def _check_cross_key_rules(effective: Mapping[str, str], errors: list[str]) -> None:
    # Follow-up modes mutual exclusion (#1268)
    try:
        model_config.require_one_follow_up_mode(effective)
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # Reasoning overrides (efforts membership, extra body, effort transport)
    try:
        model_config.reasoning_overrides(effective)
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # Request bounds (deadline < route timeout, queue wait > 0 while concurrency > 0;
    # body budget >= body cap with empty sides at the chart defaults, #1352, raised by request_overrides)
    try:
        req = model_config.request_overrides(effective)
        conc = req.get("requestMaxConcurrent", 4)
        wait = req.get("requestQueueWaitSeconds", 30)
        if conc > 0 and wait == 0:
            _add_unique(
                errors,
                "REQUEST_QUEUE_WAIT_S must be greater than 0 while REQUEST_MAX_CONCURRENT is greater than 0",
            )
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # NetworkPolicy
    try:
        model_config.network_policy(effective)
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # Feedback service
    try:
        model_config.feedback_config(effective)
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # Open WebUI (shapes, ratings vs feedback, required keys when enabled)
    try:
        model_config.openwebui_config(effective)
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # Team tools: the Splunk gateway (#1077; shapes, required keys and the
    # site config/CA files when enabled), mirroring map_values.py
    try:
        model_config.team_tools_config(effective)
    except ValueError as exc:
        _add_unique(errors, str(exc))

    # Qdrant, ingest & agent sizing (shapes and limit >= request)
    for fn in (model_config.qdrant_sizing, model_config.ingest_resources, model_config.agent_resources):
        try:
            fn(effective)
        except ValueError as exc:
            _add_unique(errors, str(exc))

    # INGEST_AUTHORIZE_FROM_CATALOG requires INGEST_RETIRE_DOCS (#1005)
    if _safe_bool(effective, "INGEST_AUTHORIZE_FROM_CATALOG", False) is True:
        retire = [c.strip(" \t") for c in re.split(r"[,\n]", effective.get("INGEST_RETIRE_DOCS", "")) if c.strip(" \t")]
        if not retire:
            _add_unique(errors, "INGEST_AUTHORIZE_FROM_CATALOG requires INGEST_RETIRE_DOCS")

    # SERVICEMONITOR_ENABLED=true requires METRICS_ENABLED=true (#529);
    # map_values.py enables metrics only on exact `METRICS_ENABLED=true`.
    sm = _safe_bool(effective, "SERVICEMONITOR_ENABLED", False)
    metrics = effective.get("METRICS_ENABLED", "") == "true"
    if sm is True and not metrics:
        _add_unique(
            errors,
            "SERVICEMONITOR_ENABLED=true requires METRICS_ENABLED=true: refusing to monitor a disabled endpoint",
        )

    # GATEWAY_API_KEY_SECRET_KEY requires GATEWAY_API_KEY_SECRET
    if effective.get("GATEWAY_API_KEY_SECRET_KEY", "") and not effective.get("GATEWAY_API_KEY_SECRET", ""):
        _add_unique(errors, "GATEWAY_API_KEY_SECRET_KEY requires GATEWAY_API_KEY_SECRET")

    # OPENOBSERVE_RUN_AS_ID requires bundled OpenObserve (tracing on, empty endpoint)
    if effective.get("OPENOBSERVE_RUN_AS_ID", ""):
        otel = effective.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if otel != "":
            _add_unique(
                errors,
                "OPENOBSERVE_RUN_AS_ID requires the bundled OpenObserve (tracing on, empty OTEL_EXPORTER_OTLP_ENDPOINT)",
            )

    # Collection policy completeness & W <= RF
    pol_keys = ("QDRANT_SHARD_NUMBER", "QDRANT_REPLICATION_FACTOR", "QDRANT_WRITE_CONSISTENCY_FACTOR")
    pol_vals = {k: effective.get(k, "") for k in pol_keys}
    present = [k for k, v in pol_vals.items() if v != ""]
    if 0 < len(present) < 3:
        missing = [k for k in pol_keys if not pol_vals[k]]
        _add_unique(errors, f"collection distribution policy is incomplete (missing: {' '.join(missing)})")
    elif len(present) == 3 and all(re.fullmatch(r"[0-9]+", pol_vals[k]) for k in pol_keys):
        if int(pol_vals["QDRANT_WRITE_CONSISTENCY_FACTOR"]) > int(pol_vals["QDRANT_REPLICATION_FACTOR"]):
            _add_unique(errors, "QDRANT_WRITE_CONSISTENCY_FACTOR must not exceed QDRANT_REPLICATION_FACTOR")

    # Reasoning model needs LLM_BASE_URL (or GATEWAY_BASE_URL)
    if effective.get("LLM_MODEL_REASONING", "") and not effective.get("LLM_BASE_URL", ""):
        _add_unique(errors, "LLM_BASE_URL is required when LLM_MODEL_REASONING is set")

    # Contextual embed needs CONTEXT_LLM_BASE_URL + CONTEXT_LLM_MODEL
    if _safe_bool(effective, "CONTEXTUAL_EMBED_ENABLED", False) is True:
        for req_key in ("CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL"):
            if not effective.get(req_key, ""):
                _add_unique(errors, f"{req_key} is required when CONTEXTUAL_EMBED_ENABLED is set")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="config_tool.py",
        description="Inspect and validate air-gap operator configuration (#1284).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", help="Print effective configuration grouped by section.")
    p_show.add_argument("--env-file", default="", help="Selected airgap.env path (if any).")
    p_show.add_argument("--cli-keys", default=None, help="Space-separated keys set in caller env/CLI.")

    p_check = sub.add_parser("check", help="Check operator configuration for errors and warnings.")
    p_check.add_argument("--env-file", default="", help="Selected airgap.env path (if any).")
    p_check.add_argument(
        "--warn-only",
        action="store_true",
        help="Emit findings as WARNING lines and exit 0 (used by validate.sh).",
    )

    args = parser.parse_args(argv)
    env_file = Path(args.env_file) if args.env_file else None

    if args.command == "show":
        cli_keys = set(args.cli_keys.split()) if args.cli_keys is not None else None
        sys.stdout.write(render_show(os.environ, env_file=env_file, cli_keys=cli_keys))
        return 0

    errors, warnings = check_config(os.environ, env_file=env_file)
    if args.warn_only:
        for msg in errors:
            print(f"WARNING: config check: {msg}", file=sys.stderr)
        for msg in warnings:
            print(f"WARNING: config check: {msg}", file=sys.stderr)
        return 0

    for msg in errors:
        print(f"ERROR: {msg}", file=sys.stderr)
    for msg in warnings:
        print(f"WARNING: {msg}", file=sys.stderr)
    if errors:
        return 1
    if warnings:
        print(f"OK: configuration check passed ({len(warnings)} warning(s))")
    else:
        print("OK: configuration check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
